"""Native GUI integration tests against temporary SQLite and real Qt (offscreen)."""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

HAS_QT = importlib.util.find_spec("PySide6") is not None
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@unittest.skipUnless(HAS_QT, "PySide6 is not installed")
class NativeWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from app.data.database import Database
        from app.core.service import CalculationService
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.database = Database(Path(self.dir.name) / "kontakt.db")
        self.database.initialize()
        self.service = CalculationService(self.database)
        # Forbid silent access to the real user profile DB in ALL GUI tests.
        profile_guard = mock.patch(
            "app.data.database.get_database_path",
            side_effect=AssertionError("GUI tests must use a temporary SQLite database"))
        profile_guard.start()
        self.addCleanup(profile_guard.stop)
        # A missing mock must FAIL immediately rather than block headless CI.
        from PySide6.QtWidgets import QMessageBox, QFileDialog
        for owner, names in (
            (QMessageBox, ("question", "warning", "critical", "information")),
            (QFileDialog, ("getSaveFileName", "getOpenFileName")),
        ):
            for name in names:
                patcher = mock.patch.object(
                    owner, name, side_effect=AssertionError(
                        f"Unexpected modal UI: {owner.__name__}.{name}"))
                patcher.start()
                self.addCleanup(patcher.stop)

    def _tenant(self, **kwargs):
        return self.service.create_tenant(name="ООО Тест", **kwargs)

    def test_empty_database_has_no_sample_tenants_and_archive_is_reachable(self):
        from PySide6.QtCore import QDate
        from app.ui.main_window import MainWindow
        window = MainWindow(self.database)
        try:
            self.assertEqual(window.tenant_table.rowCount(), 0)
            self.assertEqual(window.calc_table.rowCount(), 0)
            self.assertEqual(window.archive_table.rowCount(), 0)
            previous = QDate.currentDate().addMonths(-1)
            self.assertEqual((window.year, window.month), (previous.year(), previous.month()))
            window.navigate("archive")
            self.assertEqual(window.pages.currentIndex(), 2)
        finally:
            window.close()

    def test_tenant_creation_edit_archive_and_restore_persist(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import TenantDialog
        from app.data import database as db
        dialog = TenantDialog(self.service, self.database)
        dialog.fields["name"].setText("ООО Пример")
        dialog.fields["initial_reading"].setText("100,000")
        dialog._create()
        tenant_id = dialog.tenant_id
        self.assertIsInstance(tenant_id, int)
        dialog.fields["inn"].setText("1234567890")
        dialog._mark_pending("inn")
        dialog._save_field("inn")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            dialog._toggle_archive()
            with self.database.connection() as conn:
                self.assertEqual(db.get_tenant(conn, tenant_id)["is_archived"], 1)
            dialog._toggle_archive()
        with self.database.connection() as conn:
            self.assertEqual(db.get_tenant(conn, tenant_id)["inn"], "1234567890")
            self.assertEqual(db.get_tenant(conn, tenant_id)["is_archived"], 0)
        dialog.close()

    def test_draft_autosave_then_ready_calculation_survives_new_service(self):
        from app.ui.forms import CalculationDialog
        from app.core.service import CalculationService
        from app.data.database import Database
        self._tenant()
        from PySide6.QtCore import QDate
        current = QDate.currentDate().addMonths(-1)
        self.service.set_tariff(current.year(), current.month(), "11.0700")
        dialog = CalculationDialog(self.service, self.database,
                                   year=current.year(), month=current.month())
        dialog.start_edit.setText("193630")
        dialog.end_edit.setText("")
        dialog._create()
        cid = dialog.calculation_id
        self.assertIsNotNone(cid)
        self.assertFalse(self.service.validate(cid).ready)
        dialog.end_edit.setText("197030")
        dialog._mark_pending("end_reading")
        dialog._save_reading("end_reading")
        self.assertTrue(self.service.validate(cid).ready)
        self.assertEqual(str(self.service.get_calculation(cid)["total_amount"]), "37638.00")
        fresh = CalculationService(Database(self.database.path))
        self.assertEqual(str(fresh.get_calculation(cid)["end_reading"]), "197030")
        dialog.close()

    def test_cancel_historical_change_does_not_modify_data(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import CalculationDialog
        tenant = self._tenant()
        self.service.set_tariff(2026, 9, "5")
        self.service.set_tariff(2026, 10, "5")
        older = self.service.create_calculation(
            tenant["id"], 2026, 9, start_reading="10", end_reading="20")
        self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="20", end_reading="30")
        editor = CalculationDialog(self.service, self.database, calculation_id=older["id"])
        editor.end_edit.setText("21")
        editor._mark_pending("end_reading")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            editor._save_reading("end_reading")
        self.assertEqual(str(self.service.get_calculation(older["id"])["end_reading"]), "20")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            editor.reject()
        self.assertEqual(str(self.service.get_calculation(older["id"])["end_reading"]), "20")

    def test_zero_document_cancel_keeps_metadata_unchanged(self):
        from PySide6.QtWidgets import QMessageBox, QFileDialog
        from app.ui.forms import ExportDialog
        from app.data import database as db
        tenant = self._tenant()
        self.service.update_settings(performer_name="ООО Исполнитель")
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="50", end_reading="50")
        dialog = ExportDialog(self.service, self.database, calc["id"])
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirmation, \
             mock.patch.object(QFileDialog, "getSaveFileName") as chooser:
            dialog._export()
            confirmation.assert_called_once()
            self.assertEqual(confirmation.call_args.args[1], "Нулевой расход")
            chooser.assert_not_called()
        with self.database.connection() as conn:
            self.assertEqual(db.list_documents(conn, calculation_id=calc["id"]), [])
        dialog.close()

    def test_export_creates_real_act_and_invoice_and_status_after_write(self):
        from PySide6.QtWidgets import QMessageBox, QFileDialog
        from app.ui.forms import ExportDialog
        self._tenant()
        with self.database.connection() as conn:
            from app.data import database as db
            tenant_id = db.list_tenants(conn)[0]["id"]
        self.service.update_settings(performer_name="ООО Исполнитель")
        self.service.set_tariff(2026, 10, "11.0700")
        calc = self.service.create_calculation(
            tenant_id, 2026, 10, start_reading="193630", end_reading="197030")
        dialog = ExportDialog(self.service, self.database, calc["id"])
        paths = [str(Path(self.dir.name) / "act.pdf"), str(Path(self.dir.name) / "invoice.pdf")]
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               side_effect=[(p, "PDF (*.pdf)") for p in paths]), \
             mock.patch.object(QMessageBox, "information"):
            dialog._export()
        for kind, filename in zip(("act", "invoice"), paths):
            self.assertTrue(Path(filename).read_bytes().startswith(b"%PDF-"))
            self.assertEqual(self.service.document_status(calc["id"], kind), "current")

    def test_font_preferences_do_not_invalidate_documents(self):
        from app.ui.forms import SettingsPage
        from app.ui.common import appearance_settings
        from app.data import database as db
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="0", end_reading="1")
        with self.database.transaction() as conn:
            db.create_document(conn, calculation_id=calc["id"], type="act",
                               number=1, document_date="2026-11-01", is_current=True,
                               last_generated_at="2026-11-01T00:00:00")
        page = SettingsPage(self.service)
        settings = appearance_settings()
        previous_size = settings.value("font_size_percent")
        previous_weight = settings.value("font_weight_value")
        try:
            page.size_slider.setValue(150)
            page.weight_slider.setValue(700)
            self.assertEqual(int(settings.value("font_size_percent")), 150)
            self.assertEqual(int(settings.value("font_weight_value")), 700)
            self.assertEqual(self.service.document_status(calc["id"], "act"), "current")
        finally:
            if previous_size is None:
                settings.remove("font_size_percent")
            else:
                settings.setValue("font_size_percent", previous_size)
            if previous_weight is None:
                settings.remove("font_weight_value")
            else:
                settings.setValue("font_weight_value", previous_weight)
            settings.sync()
            from app.ui.common import apply_appearance
            apply_appearance()

    def test_overlapping_calculations_can_be_created_after_confirmation(self):
        from PySide6.QtWidgets import QMessageBox
        from app.data import database as db
        from app.ui.forms import CalculationDialog
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        self.service.create_calculation(
            tenant["id"], 2026, 10, period_start="2026-10-01",
            period_end="2026-10-20", start_reading="0", end_reading="10")
        editor = CalculationDialog(self.service, self.database, year=2026, month=10)
        editor.date_start.setDate(__import__("PySide6.QtCore", fromlist=["QDate"]).QDate(2026, 10, 15))
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            editor._create()
        self.assertIsInstance(editor.calculation_id, int)
        with self.database.connection() as conn:
            self.assertEqual(len(db.list_calculations(
                conn, tenant_id=tenant["id"], year=2026, month=10)), 2)
        editor.close()

    def test_reverse_meter_reading_persists_as_invalid_draft(self):
        from app.ui.forms import CalculationDialog
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="50", end_reading="70")
        dialog = CalculationDialog(self.service, self.database, calculation_id=calc["id"])
        dialog.end_edit.setText("40")
        dialog._mark_pending("end_reading")
        dialog._save_reading("end_reading")
        result = self.service.validate(calc["id"])
        self.assertFalse(result.ready)
        self.assertTrue(any("меньше" in error for error in result.errors))
        self.assertEqual(str(self.service.get_calculation(calc["id"])["end_reading"]), "40")
        dialog.close()

    def test_failed_export_does_not_mark_generated(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from app.ui.forms import ExportDialog
        from app.data import database as db
        tenant = self._tenant()
        self.service.update_settings(performer_name="ООО Исполнитель")
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="50", end_reading="70")
        dialog = ExportDialog(self.service, self.database, calc["id"])
        dialog.options["invoice"][0].setChecked(False)
        destination = Path(self.dir.name) / "nonexistent" / "act.pdf"
        from app.documents import export_document
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(str(destination), "PDF (*.pdf)")), \
             mock.patch.object(QMessageBox, "warning") as warning, \
             mock.patch("app.ui.forms.export_document", wraps=export_document) as exporter:
            dialog._export()
            exporter.assert_called_once()  # reached the real filesystem preflight
            warning.assert_called_once()
            self.assertEqual(warning.call_args.args[1], "Результат экспорта")
            self.assertIn("Каталог для PDF не существует", warning.call_args.args[2])
            self.assertNotIn("Не заполнен исполнитель", warning.call_args.args[2])
        self.assertFalse(destination.exists())
        self.assertEqual(self.service.document_status(calc["id"], "act"), "not_created")
        with self.database.connection() as conn:
            self.assertEqual(db.list_documents(conn, calculation_id=calc["id"]), [])
        dialog.close()

    def test_repeated_pdf_export_preserves_document_date_and_number(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from app.ui.forms import ExportDialog
        from app.data import database as db
        tenant = self._tenant()
        self.service.update_settings(performer_name="ООО Исполнитель")
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="50", end_reading="70")
        path = Path(self.dir.name) / "act.pdf"
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(str(path), "PDF (*.pdf)")), \
             mock.patch.object(QMessageBox, "information"):
            first = ExportDialog(self.service, self.database, calc["id"])
            first.options["invoice"][0].setChecked(False)
            first._export()
        with self.database.connection() as conn:
            original = next(d for d in db.list_documents(conn, calculation_id=calc["id"]) if d["type"] == "act")
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(str(path), "PDF (*.pdf)")), \
             mock.patch.object(QMessageBox, "information"), \
             mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            second = ExportDialog(self.service, self.database, calc["id"])
            second.options["invoice"][0].setChecked(False)
            second._export()
        with self.database.connection() as conn:
            updated = next(d for d in db.list_documents(conn, calculation_id=calc["id"]) if d["type"] == "act")
        self.assertEqual((updated["number"], updated["document_date"]),
                         (original["number"], original["document_date"]))
        self.assertEqual(self.service.document_status(calc["id"], "act"), "current")

    def test_signature_upload_and_removal_use_owned_app_directory(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from PySide6.QtGui import QImage
        from app.ui.forms import SettingsPage
        from app.data import database as db
        source = Path(self.dir.name) / "synthetic.png"
        image = QImage(6, 6, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFFFF)
        self.assertTrue(image.save(str(source)))
        page = SettingsPage(self.service)
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(str(source), "Изображения (*.png *.jpg *.jpeg)")):
            page._select_image("signature_path")
        with self.database.connection() as conn:
            path = db.get_settings(conn)["signature_path"]
        self.assertTrue(path)
        self.assertTrue(Path(path).is_file())
        self.assertNotEqual(Path(path).resolve(), source.resolve())
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            page._remove_image("signature_path")
        with self.database.connection() as conn:
            self.assertIsNone(db.get_settings(conn)["signature_path"])
        self.assertFalse(Path(path).exists())

    def test_editing_one_field_preserves_other_unsaved_input(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import CalculationDialog
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="50", end_reading="70")
        dialog = CalculationDialog(self.service, self.database, calculation_id=calc["id"])
        dialog.start_edit.setText("wrong")
        dialog._mark_pending("start_reading")
        dialog.end_edit.setText("80")
        dialog._mark_pending("end_reading")
        dialog._save_reading("end_reading")
        self.assertEqual(dialog.start_edit.text(), "wrong")
        self.assertIn("start_reading", dialog.pending)
        self.assertEqual(str(self.service.get_calculation(calc["id"])["end_reading"]), "80")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            dialog.reject()
        self.assertEqual(str(self.service.get_calculation(calc["id"])["start_reading"]), "50")

    def test_settings_reload_preserves_unsaved_text_after_failure(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import SettingsPage
        from app.data import database as db
        page = SettingsPage(self.service)
        field = page.fields["bank_name"]
        field.setText("Not persisted")
        page.pending.add("bank_name")
        with mock.patch.object(self.service, "update_settings", side_effect=sqlite3.OperationalError("readonly")), \
             mock.patch.object(QMessageBox, "critical"):
            page._save_field("bank_name")
        page.reload()
        self.assertEqual(field.text(), "Not persisted")
        self.assertIn("bank_name", page.pending)
        with self.database.connection() as conn:
            self.assertEqual(db.get_settings(conn)["bank_name"], "")

    def _ready_for_export(self):
        tenant = self._tenant()
        self.service.update_settings(performer_name="ООО Тестовый исполнитель")
        self.service.set_tariff(2026, 10, "7")
        return self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="20")

    def _assert_alias_blocked(self, act_path, invoice_path, original_bytes):
        from PySide6.QtWidgets import QMessageBox, QFileDialog
        from app.ui.forms import ExportDialog
        from app.data import database as db
        from app.documents import export_document
        calc = self._ready_for_export()
        dialog = ExportDialog(self.service, self.database, calc["id"])
        with mock.patch.object(
            QFileDialog, "getSaveFileName", side_effect=[
                (str(act_path), "PDF (*.pdf)"), (str(invoice_path), "PDF (*.pdf)")
            ]
        ) as chooser, mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ), mock.patch.object(QMessageBox, "critical") as critical, mock.patch(
            "app.ui.forms.export_document", wraps=export_document
        ) as exporter:
            dialog._export()
            self.assertEqual(chooser.call_count, 2)
            critical.assert_called_once()
            self.assertEqual(critical.call_args.args[1], "Совпадающие пути PDF")
            self.assertIn("физический целевой файл", critical.call_args.args[2])
            exporter.assert_not_called()
        self.assertEqual(act_path.read_bytes(), original_bytes)
        self.assertEqual(invoice_path.read_bytes(), original_bytes)
        with self.database.connection() as conn:
            self.assertEqual(db.list_documents(conn, calculation_id=calc["id"]), [])
        self.assertEqual(self.service.document_status(calc["id"], "act"), "not_created")
        self.assertEqual(self.service.document_status(calc["id"], "invoice"), "not_created")
        dialog.close()

    def test_pdf_alias_with_dotdot_does_not_write_any_document(self):
        directory = Path(self.dir.name)
        (directory / "nested").mkdir()
        path = directory / "existing.pdf"
        content = b"existing-private-PDF"
        path.write_bytes(content)
        self._assert_alias_blocked(path, directory / "nested" / ".." / path.name, content)

    def test_pdf_alias_with_hardlink_does_not_write_any_document(self):
        original = Path(self.dir.name) / "act.pdf"
        alias = Path(self.dir.name) / "invoice.pdf"
        content = b"previous-owner-content"
        original.write_bytes(content)
        try:
            os.link(original, alias)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        self._assert_alias_blocked(original, alias, content)

    def test_pdf_alias_with_symlinked_parent_does_not_write_any_document(self):
        real = Path(self.dir.name) / "real"
        real.mkdir()
        alias_parent = Path(self.dir.name) / "alias"
        try:
            alias_parent.symlink_to(real, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        original = real / "existing.pdf"
        content = b"keep-old-file"
        original.write_bytes(content)
        self._assert_alias_blocked(original, alias_parent / original.name, content)

    def test_click_from_calculation_opens_correct_tenant_and_history(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QPushButton
        from app.ui.forms import CalculationDialog, TenantDialog
        from app.ui.main_window import MainWindow
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="1", end_reading="2")
        observed = []
        def record_tenant(dialog):
            observed.append((dialog.tenant_id, dialog.history.rowCount(),
                             dialog.history.item(0, 0).text()))
            return 0
        with mock.patch.object(TenantDialog, "exec", new=record_tenant):
            detail = CalculationDialog(self.service, self.database, calculation_id=calc["id"])
            detail.show()
            QApplication.processEvents()
            QTest.mouseClick(detail.open_tenant_button, Qt.MouseButton.LeftButton)
            detail.close()
            main = MainWindow(self.database)
            main.show()
            main.period_picker.setDate(
                __import__("PySide6.QtCore", fromlist=["QDate"]).QDate(2026, 10, 1))
            main.calc_table.selectRow(0)
            navigation = next(x for x in main.findChildren(QPushButton)
                              if x.text() == "Арендатор / история")
            QTest.mouseClick(navigation, Qt.MouseButton.LeftButton)
            main.close()
        self.assertEqual(observed,
                         [(tenant["id"], 1, str(calc["id"]))] * 2)

    def test_search_refresh_reports_sqlite_failure_without_stale_rows(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        from app.ui.main_window import MainWindow
        tenant = self._tenant()
        self.service.create_calculation(tenant["id"], 2026, 10)
        window = MainWindow(self.database)
        window.period_picker.setDate(
            __import__("PySide6.QtCore", fromlist=["QDate"]).QDate(2026, 10, 1))
        self.assertEqual(window.calc_table.rowCount(), 1)
        window.show()
        QApplication.processEvents()
        with mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical:
            window.calc_search.setText("ООО")  # real textChanged signal
            critical.assert_called_once()
            self.assertEqual(window.calc_table.rowCount(), 0)
            self.assertIn("не загружены", window.summary.text())
            self.assertIn("database is locked", critical.call_args.args[2])
            self.assertIn("Ошибка доступа", window.statusBar().currentMessage())
        window.refresh_calculations()
        self.assertEqual(window.calc_table.rowCount(), 1)
        window.close()

    def test_tariff_read_failure_shows_error_and_does_not_claim_saved(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.main_window import MainWindow
        from app.data import queries
        window = MainWindow(self.database)
        window.tariff_input.setText("77")
        with mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("readonly")), \
             mock.patch.object(QMessageBox, "critical") as critical:
            window.save_tariff()
            critical.assert_called_once()
            self.assertIn("readonly", critical.call_args.args[2])
            self.assertIn("Ошибка доступа", window.statusBar().currentMessage())
        with self.database.connection() as conn:
            self.assertIsNone(queries.month_tariff(conn, window.year, window.month))
        window.close()

    def test_locked_settings_read_preserves_unsaved_text_then_retries(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import SettingsPage
        from app.data import database as db
        page = SettingsPage(self.service)
        field = page.fields["bank_name"]
        field.setText("НОВЫЙ БАНК")
        page.pending.add("bank_name")
        with mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical:
            page._save_field("bank_name")
            critical.assert_called_once()
            self.assertIn("database is locked", critical.call_args.args[2])
            self.assertEqual(field.text(), "НОВЫЙ БАНК")
            self.assertIn("bank_name", page.pending)
            self.assertIn("не сохранены", page.note.text())
            page.reload()  # failed reload must not discard the draft either
            self.assertEqual(field.text(), "НОВЫЙ БАНК")
        with self.database.connection() as conn:
            self.assertEqual(db.get_settings(conn)["bank_name"], "")
        page._save_field("bank_name")
        with self.database.connection() as conn:
            self.assertEqual(db.get_settings(conn)["bank_name"], "НОВЫЙ БАНК")
        self.assertNotIn("bank_name", page.pending)
        page.close()

    def test_locked_tenant_read_preserves_edit_then_retries(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import TenantDialog
        from app.data import database as db
        tenant = self._tenant()
        dialog = TenantDialog(self.service, self.database, tenant_id=tenant["id"])
        dialog.fields["bank_name"].setText("ДРУГОЙ БАНК")
        dialog._mark_pending("bank_name")
        with mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical:
            dialog._save_field("bank_name")
            critical.assert_called_once()
            self.assertIn("database is locked", critical.call_args.args[2])
            self.assertEqual(dialog.fields["bank_name"].text(), "ДРУГОЙ БАНК")
            self.assertIn("bank_name", dialog.pending)
        with self.database.connection() as conn:
            self.assertEqual(db.get_tenant(conn, tenant["id"])["bank_name"], "")
        dialog._save_field("bank_name")
        with self.database.connection() as conn:
            self.assertEqual(db.get_tenant(conn, tenant["id"])["bank_name"], "ДРУГОЙ БАНК")
        self.assertNotIn("bank_name", dialog.pending)
        dialog.close()

    def test_locked_calculation_read_preserves_input_and_recovers(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import CalculationDialog
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="20")
        dialog = CalculationDialog(self.service, self.database, calculation_id=calc["id"])
        dialog.end_edit.setText("25")
        dialog._mark_pending("end_reading")
        with mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical:
            dialog._save_reading("end_reading")
            critical.assert_called_once()
            self.assertIn("database is locked", critical.call_args.args[2])
            self.assertEqual(dialog.end_edit.text(), "25")
            self.assertIn("end_reading", dialog.pending)
            critical.reset_mock()
            dialog._refresh()
            critical.assert_called_once()
            self.assertFalse(dialog._filling)
            self.assertEqual(dialog.end_edit.text(), "25")
        self.assertEqual(str(self.service.get_calculation(calc["id"])["end_reading"]), "20")
        dialog._save_reading("end_reading")
        self.assertEqual(str(self.service.get_calculation(calc["id"])["end_reading"]), "25")
        self.assertNotIn("end_reading", dialog.pending)
        dialog.close()

    def test_locked_calculation_period_read_keeps_pending_dates(self):
        from PySide6.QtCore import QDate
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import CalculationDialog
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="20")
        dialog = CalculationDialog(self.service, self.database, calculation_id=calc["id"])
        with mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical:
            # QDateEdit may autosave on setDate(): inject the failure BEFORE
            # changing the widget, not after the date already reached SQLite.
            dialog.date_end.setDate(QDate(2026, 10, 27))
            critical.reset_mock()
            dialog._save_period()
            critical.assert_called_once()
            self.assertIn("period", dialog.pending)
            self.assertEqual(dialog.date_end.date(), QDate(2026, 10, 27))
        self.assertEqual(self.service.get_calculation(calc["id"])["period_end"], "2026-10-31")
        dialog._save_period()
        self.assertEqual(self.service.get_calculation(calc["id"])["period_end"], "2026-10-27")
        self.assertNotIn("period", dialog.pending)
        dialog.close()

    def test_locked_export_preflight_does_not_open_file_picker_or_change_metadata(self):
        from PySide6.QtWidgets import QMessageBox, QFileDialog
        from app.ui.forms import ExportDialog
        from app.data import database as db
        calc = self._ready_for_export()
        dialog = ExportDialog(self.service, self.database, calc["id"])
        numbers = {key: widgets[1].value() for key, widgets in dialog.options.items()}
        verified = self.service.validate(calc["id"])
        with mock.patch.object(self.service, "validate", return_value=verified), \
             mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical, \
             mock.patch.object(QFileDialog, "getSaveFileName") as chooser:
            dialog._export()
            critical.assert_called_once()
            self.assertIn("database is locked", critical.call_args.args[2])
            chooser.assert_not_called()
            self.assertEqual(numbers, {
                key: widgets[1].value() for key, widgets in dialog.options.items()})
        with self.database.connection() as conn:
            self.assertEqual(db.list_documents(conn, calculation_id=calc["id"]), [])
        paths = [str(Path(self.dir.name) / "good-act.pdf"),
                 str(Path(self.dir.name) / "good-invoice.pdf")]
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               side_effect=[(p, "PDF (*.pdf)") for p in paths]), \
             mock.patch.object(QMessageBox, "information"):
            dialog._export()
        for kind, path in zip(("act", "invoice"), paths):
            self.assertEqual(self.service.document_status(calc["id"], kind), "current")
            self.assertTrue(Path(path).read_bytes().startswith(b"%PDF-"))

    def test_document_date_number_lookup_failure_keeps_number_editable(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import ExportDialog
        from PySide6.QtCore import QDate
        calc = self._ready_for_export()
        dialog = ExportDialog(self.service, self.database, calc["id"])
        checkbox, number, date = dialog.options["act"]
        original = number.value()
        with mock.patch.object(self.service, "next_document_number",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical:
            date.setDate(QDate(2026, 11, 2))
            critical.assert_called_once()
            self.assertEqual(number.value(), original)
        with mock.patch.object(self.service, "next_document_number", return_value=42):
            date.setDate(QDate(2026, 11, 3))
        self.assertEqual(number.value(), 42)
        dialog.close()

    def test_tenant_history_load_failure_can_be_retried(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import TenantDialog
        tenant = self._tenant()
        self.service.create_calculation(tenant["id"], 2026, 10)
        dialog = TenantDialog(self.service, self.database, tenant_id=tenant["id"])
        self.assertEqual(dialog.history.rowCount(), 1)
        with mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical:
            dialog._reload_history()
            critical.assert_called_once()
            self.assertIn("database is locked", critical.call_args.args[2])
        dialog._reload_history()
        self.assertEqual(dialog.history.rowCount(), 1)
        dialog.close()

    def test_initial_archived_tenant_load_fails_closed_and_opens_after_recovery(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import TenantDialog
        from app.ui.main_window import MainWindow
        from app.data import database as db
        tenant = self._tenant(inn="1234567890", initial_reading="42")
        calc = self.service.create_calculation(tenant["id"], 2026, 10)
        self.service.update_tenant(tenant["id"], is_archived=True)
        window = MainWindow(self.database)
        self.assertEqual(window.archive_table.rowCount(), 1)
        window.archive_table.selectRow(0)
        with mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical, \
             mock.patch.object(TenantDialog, "exec",
                               side_effect=AssertionError("Unloaded card was displayed")) as exec_dialog:
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                TenantDialog(self.service, self.database, tenant_id=tenant["id"])
            critical.assert_not_called()  # caller, not half-built dialog, owns error UI
            window.open_selected_tenant(archived=True)
            critical.assert_called_once()
            self.assertIn("database is locked", critical.call_args.args[2])
            exec_dialog.assert_not_called()
        with self.database.connection() as conn:
            record = db.get_tenant(conn, tenant["id"])
            self.assertEqual(record["name"], "ООО Тест")
            self.assertEqual(record["inn"], "1234567890")
            self.assertEqual(record["is_archived"], 1)
        recovered = TenantDialog(self.service, self.database, tenant_id=tenant["id"])
        self.assertEqual(recovered.fields["name"].text(), "ООО Тест")
        self.assertEqual(recovered.fields["inn"].text(), "1234567890")
        self.assertEqual(recovered.archive_button.text(), "Восстановить арендатора")
        self.assertEqual(recovered.history.rowCount(), 1)
        self.assertEqual(recovered.history.item(0, 0).text(), str(calc["id"]))
        recovered.close()
        window.close()

    def test_initial_tenant_history_failure_aborts_open_instead_of_lying(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import TenantDialog
        from app.data import database as db
        tenant = self._tenant(inn="24680")
        # The first connection (card data) works; history read then fails.
        original = self.database.connection
        attempts = []
        def fail_second_read():
            attempts.append(1)
            if len(attempts) == 2:
                raise sqlite3.OperationalError("database is locked")
            return original()
        with mock.patch.object(self.database, "connection", side_effect=fail_second_read), \
             mock.patch.object(QMessageBox, "critical") as critical:
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                TenantDialog(self.service, self.database, tenant_id=tenant["id"])
            critical.assert_not_called()
        recovered = TenantDialog(self.service, self.database, tenant_id=tenant["id"])
        self.assertEqual(recovered.fields["inn"].text(), "24680")
        with self.database.connection() as conn:
            self.assertEqual(db.get_tenant(conn, tenant["id"])["inn"], "24680")
        recovered.close()

    def test_initial_new_calculation_load_fails_closed_and_opens_after_recovery(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import CalculationDialog
        from app.ui.main_window import MainWindow
        tenant = self._tenant()
        window = MainWindow(self.database)
        with mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical, \
             mock.patch.object(CalculationDialog, "exec",
                               side_effect=AssertionError("Unloaded calculation was displayed")) as exec_dialog:
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                CalculationDialog(self.service, self.database, year=2026, month=10)
            critical.assert_not_called()
            window.new_calculation()
            critical.assert_called_once()
            self.assertIn("database is locked", critical.call_args.args[2])
            exec_dialog.assert_not_called()
        dialog = CalculationDialog(self.service, self.database, year=2026, month=10)
        self.assertEqual(dialog.tenant_picker.count(), 1)
        self.assertEqual(dialog.tenant_picker.currentData(), tenant["id"])
        dialog.close()
        window.close()

    def test_initial_existing_calculation_refresh_fails_closed(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import CalculationDialog
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="25")
        with mock.patch("app.ui.forms.queries.month_tariff",
                        side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical:
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                CalculationDialog(self.service, self.database, calculation_id=calc["id"])
            critical.assert_not_called()
        dialog = CalculationDialog(self.service, self.database, calculation_id=calc["id"])
        self.assertEqual(dialog.end_edit.text(), "25")
        self.assertIn("Готов", dialog.status.text())
        dialog.close()

    def test_initial_performer_settings_load_fails_closed_and_recovers(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import SettingsPage
        from app.data import database as db
        self.service.update_settings(performer_name="Исходный исполнитель")
        with mock.patch.object(self.database, "connection",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(QMessageBox, "critical") as critical:
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                SettingsPage(self.service)
            critical.assert_not_called()
        page = SettingsPage(self.service)
        self.assertEqual(page.fields["performer_name"].text(), "Исходный исполнитель")
        with self.database.connection() as conn:
            self.assertEqual(db.get_settings(conn)["performer_name"], "Исходный исполнитель")
        page.close()

    def test_dashboard_metrics_separate_document_states_and_russian_dates(self):
        from PySide6.QtCore import QDate, Qt
        from app.ui.main_window import MainWindow
        from app.ui.widgets import ChipDelegate, CompanyDelegate
        from app.data import database as db
        long_name = "ООО " + "Очень длинное название организации " * 5
        first = self.service.create_tenant(
            name=long_name, inn="7700000000", contract_number="Д-2026/53")
        second = self.service.create_tenant(name="ЗАО Второй", contract_number="Д-54")
        self.service.set_tariff(2026, 10, "5")
        first_calc = self.service.create_calculation(
            first["id"], 2026, 10, start_reading="10", end_reading="20")
        self.service.create_calculation(
            second["id"], 2026, 10, start_reading="25", end_reading="30")
        with self.database.transaction() as conn:
            db.create_document(conn, calculation_id=first_calc["id"], type="act",
                               number=1, document_date="2026-11-01", is_current=False,
                               last_generated_at="2026-11-01T10:00:00")
        window = MainWindow(self.database)
        try:
            window.period_picker.setDate(QDate(2026, 10, 1))
            self.assertEqual(window.period_title.text(), "Октябрь 2026")
            self.assertEqual(window.kpi_cards["active"].value.text(), "2")
            self.assertEqual(window.kpi_cards["ready"].value.text(), "2 / 2")
            self.assertIn("75,00 ₽", window.kpi_cards["total"].value.text())
            self.assertEqual(window.kpi_cards["stale"].value.text(), "1")
            self.assertEqual(window.calc_table.rowCount(), 2)
            self.assertIsInstance(window.calc_table.itemDelegateForColumn(0), CompanyDelegate)
            self.assertIsInstance(window.calc_table.itemDelegateForColumn(7), ChipDelegate)
            self.assertEqual(window.calc_table.columnCount(), 11)
            names = [window.calc_table.item(i, 0).text()
                     for i in range(window.calc_table.rowCount())]
            self.assertIn(long_name, names)
            row = names.index(long_name)
            self.assertIn("Д-2026/53", window.calc_table.item(row, 0).data(
                int(Qt.ItemDataRole.UserRole) + 1))
            self.assertIn("01.10.2026", window.calc_table.item(row, 1).text())
            self.assertNotIn("2026-10-", window.calc_table.item(row, 1).text())
            self.assertEqual(window.calc_table.item(row, 8).text(), "Обновить")
            self.assertEqual(window.calc_table.item(row, 9).text(), "Не сформирован")
            self.assertEqual(window.calc_table.item(row, 8).background().style(),
                             Qt.BrushStyle.NoBrush)
            self.assertGreater(window.calc_table.rowHeight(row), 80)
            self.assertEqual(len(window.calc_table.cellWidget(row, 10).findChildren(
                __import__("PySide6.QtWidgets", fromlist=["QPushButton"]).QPushButton)), 2)
            window.calc_search.setText("Д-2026/53")
            self.assertEqual(window.calc_table.rowCount(), 1)
            window.calc_search.clear()
            window.calc_sort.setCurrentIndex(2)  # descending amount
            self.assertEqual(window.calc_table.item(0, 0).text(), long_name)
            self.assertFalse(window.bulk_action.isEnabled())
        finally:
            window.close()

    def test_tenant_cards_are_responsive_real_and_archivable(self):
        from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
        from PySide6.QtCore import Qt
        from app.ui.main_window import MainWindow
        from app.ui.widgets import TenantCard
        from app.data import database as db
        long_name = "ООО " + "Длинное название договора и организации " * 5
        one = self.service.create_tenant(
            name=long_name, inn="1234567890", contract_number="К-9876",
            initial_reading="123456.789")
        two = self._tenant()
        window = MainWindow(self.database)
        try:
            window.show()
            window.resize(1440, 850)
            QApplication.processEvents()
            window.navigate("tenants")
            self.assertEqual(len(window._tenant_cards), 2)
            card = next(card for card in window._tenant_cards if card.tenant_id == one["id"])
            self.assertIsInstance(card, TenantCard)
            self.assertIn("В архив", [b.text() for b in card.findChildren(QPushButton)])
            self.assertTrue(all(not control.icon().isNull()
                                for control in card.findChildren(QPushButton)))
            self.assertTrue(any(long_name in label.text() for label in
                                card.findChildren(__import__("PySide6.QtWidgets",
                                                            fromlist=["QLabel"]).QLabel)))
            self.assertFalse(window.tenant_table.isVisible())
            wide_positions = [window.tenant_cards_layout.getItemPosition(i)[:2]
                              for i in range(window.tenant_cards_layout.count())]
            window.resize(790, 590)
            QApplication.processEvents()
            window._reflow_layouts()
            narrow_positions = [window.tenant_cards_layout.getItemPosition(i)[:2]
                                for i in range(window.tenant_cards_layout.count())]
            self.assertEqual(len(wide_positions), 2)
            self.assertEqual(len(narrow_positions), 2)
            self.assertTrue(all(row >= 0 and col >= 0 for row, col in narrow_positions))
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.StandardButton.Yes):
                button = next(b for b in card.findChildren(QPushButton)
                              if b.text() == "В архив")
                button.click()
            with self.database.connection() as conn:
                self.assertEqual(db.get_tenant(conn, one["id"])["is_archived"], 1)
            self.assertEqual(window.tenant_table.rowCount(), 1)
            self.assertEqual(window.archive_table.rowCount(), 1)
            window.navigate("archive")
            self.assertTrue(any(c.tenant_id == one["id"] for c in window._archive_cards))
            archived_card = window._archive_cards[0]
            self.assertIn("Восстановить",
                          [b.text() for b in archived_card.findChildren(QPushButton)])
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.StandardButton.Yes):
                next(b for b in archived_card.findChildren(QPushButton)
                     if b.text() == "Восстановить").click()
            with self.database.connection() as conn:
                self.assertEqual(db.get_tenant(conn, one["id"])["is_archived"], 0)
            self.assertEqual(window.archive_table.rowCount(), 0)
            self.assertEqual(window.tenant_table.rowCount(), 2)
        finally:
            window.close()

    def test_typography_sliders_persist_independently_and_scale_without_drift(self):
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        from app.ui.forms import SettingsPage
        from app.ui.common import (apply_appearance, effective_font_weight,
                                   available_font_weights, read_appearance)
        private = QSettings(str(Path(self.dir.name) / "ui.ini"), QSettings.Format.IniFormat)
        private.clear()
        with mock.patch("app.ui.common.appearance_settings", return_value=private), \
             mock.patch("app.ui.forms.appearance_settings", return_value=private):
            page = SettingsPage(self.service)
            try:
                self.assertEqual((page.size_slider.minimum(), page.size_slider.maximum()), (80, 200))
                self.assertEqual(page.size_slider.singleStep(), 5)
                self.assertEqual((page.weight_slider.minimum(), page.weight_slider.maximum()),
                                 (300, 800))
                self.assertEqual(page.weight_slider.singleStep(), 100)
                self.assertEqual(read_appearance(), (100, 400))
                base = QApplication.instance().property("_kontakt_base_font")
                self.assertIsNotNone(base)
                for size in (80, 100, 150, 175, 200, 150):
                    page.size_slider.setValue(size)
                    self.assertEqual(read_appearance()[0], size)
                    self.assertEqual(page.size_value.text(), f"{size}%")
                    self.assertAlmostEqual(QApplication.instance().font().pointSizeF(),
                                           max(7, base.pointSizeF()) * size / 100, delta=1.0)
                size_at_150 = QApplication.instance().font().pointSizeF()
                page.weight_slider.setValue(800)
                self.assertEqual(read_appearance(), (150, 800))
                self.assertAlmostEqual(QApplication.instance().font().pointSizeF(),
                                       size_at_150, delta=0.01)
                self.assertEqual(int(QApplication.instance().font().weight()),
                                 effective_font_weight(800, available_font_weights(base.family())))
                self.assertIn("Расчёты электроэнергии", page.preview_title.text())
                page_again = SettingsPage(self.service)
                self.assertEqual(page_again.size_slider.value(), 150)
                self.assertEqual(page_again.weight_slider.value(), 800)
                page_again._reset_appearance()
                self.assertEqual(read_appearance(), (100, 400))
                page_again.close()
            finally:
                page.close()
        apply_appearance()

    def test_navigation_only_three_entries_but_archive_stays_reachable(self):
        from PySide6.QtWidgets import QApplication, QPushButton, QLabel
        from app.ui.main_window import MainWindow
        window = MainWindow(self.database)
        try:
            window.show()
            QApplication.processEvents()
            self.assertEqual(set(window.nav_buttons), {"calculations", "tenants", "settings"})
            self.assertEqual(set(button.text() for button in window.nav_buttons.values()),
                             {"Расчёты", "Арендаторы", "Настройки"})
            self.assertFalse(any("Локальные данные" in label.text()
                                 for label in window.findChildren(QLabel)))
            self.assertFalse(any("Как проходит работа" in label.text()
                                 for label in window.findChildren(QLabel)))
            self.assertFalse(any(child.objectName() == "helpStrip"
                                 for child in window.findChildren(
                                     __import__("PySide6.QtWidgets",
                                                fromlist=["QFrame"]).QFrame)))
            self.assertTrue(all(not b.icon().isNull()
                                for b in window.nav_buttons.values()))
            window.navigate("tenants")
            self.assertIn("Открыть архив", window.open_archive_button.text())
            self.assertFalse(window.open_archive_button.icon().isNull())
            window.open_archive_button.click()
            self.assertEqual(window.pages.currentIndex(), 2)
            self.assertTrue(window.nav_buttons["tenants"].isChecked())
            self.assertNotIn("archive", window.nav_buttons)
            self.assertTrue(any(b.text() == "К активным арендаторам"
                                for b in window.archive_page.findChildren(QPushButton)))
            window.navigate("calculations")
            self.assertFalse(window.bulk_action.icon().isNull())
            self.assertFalse(window.fit_columns_button.icon().isNull())
            self.assertFalse(window.bulk_action.isEnabled())
        finally:
            window.close()

    def test_manual_company_column_resize_reflows_long_names(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QDate
        from app.ui.main_window import MainWindow
        tenant = self.service.create_tenant(
            name="ООО " + "Очень длинное название арендатора " * 10,
            contract_number="Очень длинный номер договора / " * 4)
        self.service.create_calculation(tenant["id"], 2026, 10)
        window = MainWindow(self.database)
        try:
            window.resize(1360, 820)
            window.show()
            window.period_picker.setDate(QDate(2026, 10, 1))
            QApplication.processEvents()
            table = window.calc_table
            self.assertEqual(table.rowCount(), 1)
            old_height = table.rowHeight(0)
            table.setColumnWidth(0, 90)
            self.assertFalse(window._calc_columns_auto)
            narrow_height = table.rowHeight(0)
            self.assertGreater(narrow_height, old_height)
            self.assertIn("Очень длинное название", table.item(0, 0).toolTip())
            window.reset_calculation_columns()
            QApplication.processEvents()
            self.assertTrue(window._calc_columns_auto)
            self.assertLessEqual(table.rowHeight(0), narrow_height)
        finally:
            window.close()

    def test_typical_calculation_table_starts_fitted_at_1360_px(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest
        from PySide6.QtCore import QDate
        from app.ui.main_window import MainWindow
        tenant = self._tenant(contract_number="Д-2026/1")
        self.service.set_tariff(2026, 10, "5")
        self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="100", end_reading="120")
        window = MainWindow(self.database)
        try:
            window.resize(1360, 820)
            window.show()
            window.period_picker.setDate(QDate(2026, 10, 1))
            QApplication.processEvents()
            QApplication.processEvents()
            QTest.qWait(85)  # Qt event loop settles the native scroll-area width
            table = window.calc_table
            self.assertTrue(window._calc_columns_auto)
            self.assertEqual(table.item(0, 2).text(), "100")
            self.assertEqual(table.item(0, 3).text(), "120")
            self.assertEqual(table.item(0, 4).text(), "20")
            self.assertEqual(table.item(0, 5).text(), "5")
            self.assertEqual(table.item(0, 6).text(), "100,00")
            self.assertGreater(table.viewport().width(), 700)
            widths = [table.columnWidth(i) for i in range(table.columnCount())]
            app_font = QApplication.instance().property("_kontakt_base_font")
            self.assertLessEqual(
                sum(widths),
                table.viewport().width() + 4,
                f"widths={widths}, viewport={table.viewport().width()}, "
                f"base_font={app_font.pointSizeF() if app_font else None}, "
                f"current={table.font().pointSizeF()}, "
                f"scale={window._font_scale():.2f}, "
                f"hscroll={table.horizontalScrollBar().maximum()}")
        finally:
            window.close()

    def test_calculation_table_fits_window_until_manual_resize_then_reset(self):
        from PySide6.QtWidgets import QApplication, QHeaderView
        from PySide6.QtCore import QDate
        from app.ui.main_window import MainWindow
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "7")
        self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="1", end_reading="9999999999.999")
        window = MainWindow(self.database)
        try:
            window.resize(1880, 940)
            window.show()
            window.period_picker.setDate(QDate(2026, 10, 1))
            QApplication.processEvents()
            table = window.calc_table
            header = table.horizontalHeader()
            self.assertTrue(window._calc_columns_auto)
            self.assertTrue(all(
                header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
                for column in range(table.columnCount())))
            self.assertEqual(table.rowCount(), 1)
            viewport = table.viewport().width()
            total = sum(table.columnWidth(i) for i in range(table.columnCount()))
            self.assertLessEqual(abs(viewport - total), 4)
            self.assertGreaterEqual(table.columnWidth(2), table.fontMetrics().horizontalAdvance(
                table.item(0, 2).text()) + 16)
            self.assertGreaterEqual(table.columnWidth(3), table.fontMetrics().horizontalAdvance(
                table.item(0, 3).text()) + 16)
            manual = table.columnWidth(0) + 67
            table.setColumnWidth(0, manual)  # same signal a user drag sends
            self.assertFalse(window._calc_columns_auto)
            self.assertEqual(table.columnWidth(0), manual)
            window.resize(1580, 820)
            QApplication.processEvents()
            window.refresh_calculations()
            QApplication.processEvents()
            self.assertEqual(table.columnWidth(0), manual)
            self.assertFalse(window._calc_columns_auto)
            window.fit_columns_button.click()
            QApplication.processEvents()
            self.assertTrue(window._calc_columns_auto)
            self.assertNotEqual(table.columnWidth(0), manual)
            reset_viewport = table.viewport().width()
            reset_total = sum(table.columnWidth(i) for i in range(table.columnCount()))
            if reset_total > reset_viewport + 4:
                # Different Windows fonts/DPI can require a horizontal scroll
                # for the deliberately huge number; never clip that value.
                self.assertGreater(table.horizontalScrollBar().maximum(), 0)
            else:
                self.assertLessEqual(abs(reset_viewport - reset_total), 4)
            window.resize(2000, 940)
            QApplication.processEvents()
            self.assertTrue(window._calc_columns_auto)
            self.assertLessEqual(abs(
                table.viewport().width() -
                sum(table.columnWidth(i) for i in range(table.columnCount()))), 4)
            # Auto-fit leaves unreadably large numbers intact rather than
            # forcing every column to squeeze into a narrow viewport.
            window.resize(790, 560)
            QApplication.processEvents()
            self.assertGreaterEqual(table.columnWidth(3),
                                    table.fontMetrics().horizontalAdvance(
                                        table.item(0, 3).text()) + 16)
            self.assertTrue(table.horizontalScrollBar().maximum() > 0)
        finally:
            window.close()

    def test_live_preview_reacts_to_readings_and_inline_manual_checkboxes(self):
        from app.ui.forms import CalculationDialog
        from app.data import database as db
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5.7500")
        dialog = CalculationDialog(self.service, self.database, year=2026, month=10)
        try:
            self.assertEqual(dialog.tenant_picker.currentData(), tenant["id"])
            self.assertEqual(dialog.preview_status.text(), "Черновик")
            self.assertFalse(dialog.manual_amount_check.isChecked())
            self.assertFalse(dialog.manual_consumption_check.isChecked())
            self.assertFalse(dialog.manual_amount_edit.isEnabled())
            self.assertFalse(dialog.manual_consumption_edit.isEnabled())
            dialog.start_edit.setText("100")
            dialog.end_edit.setText("135")
            self.assertEqual(dialog.consumption.text(), "35 кВт·ч")
            self.assertEqual(dialog.tariff_label.text(), "5,75 ₽")
            self.assertEqual(dialog.amount.text(), "201,25 ₽")
            self.assertEqual(dialog.preview_status.text(), "Готов")
            dialog.manual_amount_check.setChecked(True)
            self.assertTrue(dialog.manual_amount_edit.isEnabled())
            self.assertEqual(dialog.manual_amount_edit.text(), "201,25")
            dialog.manual_amount_edit.setText("250,50")
            self.assertEqual(dialog.amount.text(), "250,50 ₽")
            dialog.manual_amount_check.setChecked(False)
            self.assertFalse(dialog.manual_amount_edit.isEnabled())
            self.assertEqual(dialog.amount.text(), "201,25 ₽")
            dialog.manual_consumption_check.setChecked(True)
            dialog.manual_consumption_edit.setText("40,125")
            self.assertEqual(dialog.consumption.text(), "40,125 кВт·ч")
            self.assertEqual(dialog.amount.text(), "230,72 ₽")
            dialog.manual_consumption_check.setChecked(False)
            self.assertEqual(dialog.amount.text(), "201,25 ₽")
            dialog.end_edit.setText("90")
            self.assertEqual(dialog.preview_status.text(), "Черновик")
            self.assertIn("меньше начального", dialog.preview_details.text())
            dialog.end_edit.setText("invalid")
            self.assertEqual(dialog.preview_status.text(), "Некорректный ввод")
            with self.database.connection() as conn:
                self.assertEqual(db.list_calculations(conn), [])
        finally:
            from PySide6.QtWidgets import QMessageBox
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.StandardButton.Yes):
                dialog.close()

    def test_manual_amount_checkbox_saves_and_unchecking_resets_formula(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import CalculationDialog
        from app.data import database as db
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="20")
        with self.database.transaction() as conn:
            db.create_document(conn, calculation_id=calc["id"], type="act",
                               number=1, document_date="2026-10-31",
                               last_generated_at="2026-11-01T00:00:00",
                               is_current=True)
        editor = CalculationDialog(self.service, self.database, calculation_id=calc["id"])
        try:
            self.assertEqual(editor.amount.text(), "50,00 ₽")
            editor.manual_amount_check.setChecked(True)
            editor.manual_amount_edit.setText("75,25")
            editor._mark_pending("total_amount")
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.StandardButton.Yes) as question:
                editor._save_manual("total_amount")
                question.assert_called_once()
            changed = self.service.get_calculation(calc["id"])
            self.assertEqual(str(changed["total_amount"]), "75.25")
            self.assertTrue(changed["manual_amount"])
            self.assertTrue(editor.manual_amount_check.isChecked())
            self.assertEqual(editor.amount.text(), "75,25 ₽")
            with self.database.connection() as conn:
                self.assertFalse(db.list_documents(conn, calculation_id=calc["id"])[0]["is_current"])
            editor.manual_amount_check.setChecked(False)
            restored = self.service.get_calculation(calc["id"])
            self.assertFalse(restored["manual_amount"])
            self.assertEqual(str(restored["total_amount"]), "50.00")
            self.assertFalse(editor.manual_amount_edit.isEnabled())
            self.assertEqual(editor.amount.text(), "50,00 ₽")
            reopened = CalculationDialog(
                self.service, self.database, calculation_id=calc["id"])
            self.assertFalse(reopened.manual_amount_check.isChecked())
            reopened.close()
        finally:
            editor.close()

    def test_manual_consumption_resets_amount_and_cancel_preserves_draft(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import CalculationDialog
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="20")
        editor = CalculationDialog(self.service, self.database, calculation_id=calc["id"])
        try:
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.StandardButton.Yes):
                editor.manual_amount_check.setChecked(True)
                editor.manual_amount_edit.setText("75")
                editor._mark_pending("total_amount")
                editor._save_manual("total_amount")
                self.assertTrue(self.service.get_calculation(calc["id"])["manual_amount"])
                editor.manual_consumption_check.setChecked(True)
                editor.manual_consumption_edit.setText("12")
                editor._mark_pending("consumption")
                editor._save_manual("consumption")
            data = self.service.get_calculation(calc["id"])
            self.assertTrue(data["manual_consumption"])
            self.assertFalse(data["manual_amount"])
            self.assertEqual(str(data["total_amount"]), "60.00")
            self.assertTrue(editor.manual_consumption_check.isChecked())
            self.assertFalse(editor.manual_amount_check.isChecked())
            # Rejected manual edit must not change SQLite or discard input.
            editor.manual_amount_check.setChecked(True)
            editor.manual_amount_edit.setText("120")
            editor._mark_pending("total_amount")
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.StandardButton.No):
                editor._save_manual("total_amount")
            self.assertIn("total_amount", editor.pending)
            self.assertEqual(editor.manual_amount_edit.text(), "120")
            self.assertEqual(str(self.service.get_calculation(calc["id"])["total_amount"]), "60.00")
            editor.manual_amount_check.setChecked(False)
            self.assertNotIn("total_amount", editor.pending)
            editor.manual_consumption_check.setChecked(False)
            original = self.service.get_calculation(calc["id"])
            self.assertFalse(original["manual_consumption"])
            self.assertEqual(str(original["consumption"]), "10.000")
            self.assertEqual(str(original["total_amount"]), "50.00")
        finally:
            editor.close()

    def test_create_with_inline_manual_values_uses_existing_service_confirmations(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import CalculationDialog
        self._tenant()
        self.service.set_tariff(2026, 10, "5")
        editor = CalculationDialog(self.service, self.database, year=2026, month=10)
        try:
            editor.start_edit.setText("10")
            editor.end_edit.setText("20")
            editor.manual_consumption_check.setChecked(True)
            editor.manual_consumption_edit.setText("14")
            editor.manual_amount_check.setChecked(True)
            editor.manual_amount_edit.setText("90")
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.StandardButton.Yes) as question:
                editor._create()
                self.assertGreaterEqual(question.call_count, 2)
            self.assertIsInstance(editor.calculation_id, int)
            saved = self.service.get_calculation(editor.calculation_id)
            self.assertTrue(saved["manual_consumption"])
            self.assertTrue(saved["manual_amount"])
            self.assertEqual(str(saved["consumption"]), "14.000")
            self.assertEqual(str(saved["total_amount"]), "90.00")
            self.assertFalse(editor.pending)
            self.assertEqual(editor.amount.text(), "90,00 ₽")
        finally:
            editor.close()

    def test_failed_manual_reset_keeps_checkbox_and_stored_amount(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.forms import CalculationDialog
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="20")
        editor = CalculationDialog(self.service, self.database, calculation_id=calc["id"])
        try:
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.StandardButton.Yes):
                editor.manual_amount_check.setChecked(True)
                editor.manual_amount_edit.setText("85")
                editor._mark_pending("total_amount")
                editor._save_manual("total_amount")
            with mock.patch.object(self.database, "transaction",
                                   side_effect=sqlite3.OperationalError("database is locked")), \
                 mock.patch.object(QMessageBox, "critical") as critical:
                editor.manual_amount_check.setChecked(False)
                critical.assert_called_once()
            self.assertTrue(editor.manual_amount_check.isChecked())
            self.assertTrue(editor.manual_amount_edit.isEnabled())
            self.assertTrue(self.service.get_calculation(calc["id"])["manual_amount"])
            self.assertEqual(str(self.service.get_calculation(calc["id"])["total_amount"]), "85.00")
        finally:
            editor.close()

    def test_200_percent_layout_keeps_pages_cards_and_dialog_actions_accessible(self):
        from PySide6.QtCore import QDate, QSettings
        from PySide6.QtWidgets import QApplication, QScrollArea, QPushButton
        from app.ui.main_window import MainWindow
        from app.ui.forms import CalculationDialog, ExportDialog
        from app.ui.common import apply_appearance
        tenant = self._tenant()
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="20")
        private = QSettings(str(Path(self.dir.name) / "scaled.ini"), QSettings.Format.IniFormat)
        private.clear()
        with mock.patch("app.ui.common.appearance_settings", return_value=private), \
             mock.patch("app.ui.forms.appearance_settings", return_value=private):
            window = MainWindow(self.database)
            detail = export = None
            try:
                window.show()
                window.resize(790, 590)
                window.period_picker.setDate(QDate(2026, 10, 1))
                page = window.settings_page
                page.size_slider.setValue(200)
                page.weight_slider.setValue(800)
                QApplication.processEvents()
                self.assertEqual(window.pages.count(), 4)
                self.assertTrue(all(isinstance(window.pages.widget(i), QScrollArea)
                                    for i in range(4)))
                self.assertEqual(window.kpi_layout.count(), 4)
                self.assertEqual(window.calc_table.rowCount(), 1)
                self.assertIsNotNone(window.calc_table.cellWidget(0, 10))
                self.assertFalse(window.bulk_action.isEnabled())
                window.navigate("tenants")
                self.assertEqual(len(window._tenant_cards), 1)
                card = window._tenant_cards[0]
                self.assertEqual(len(card.findChildren(QPushButton)), 3)
                self.assertTrue(all(control.isEnabled()
                                    for control in card.findChildren(QPushButton)))
                detail = CalculationDialog(
                    self.service, self.database, calculation_id=calc["id"])
                self.assertTrue(detail.findChildren(QScrollArea))
                self.assertTrue(detail.open_tenant_button.isEnabled())
                export = ExportDialog(self.service, self.database, calc["id"])
                self.assertTrue(export.findChildren(QScrollArea))
                self.assertEqual(export.options["act"][0].isChecked(), True)
                page._reset_appearance()
            finally:
                if detail is not None:
                    detail.close()
                if export is not None:
                    export.close()
                window.close()
        apply_appearance()

    def test_headless_export_negative_cases_exit_in_separate_processes(self):
        root = Path(__file__).resolve().parent.parent
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment["PYTHONPATH"] = str(root) + os.pathsep + environment.get("PYTHONPATH", "")
        for method in (
            "test_zero_document_cancel_keeps_metadata_unchanged",
            "test_failed_export_does_not_mark_generated",
            "test_repeated_pdf_export_preserves_document_date_and_number",
        ):
            with self.subTest(method=method):
                child = subprocess.run(
                    [sys.executable, "-m", "unittest", "-v",
                     "test_gui_workflows.NativeWorkflowTests." + method],
                    cwd=root / "tests", env=environment, capture_output=True,
                    text=True, timeout=45, check=False)
                self.assertEqual(child.returncode, 0, child.stdout + "\n" + child.stderr)


if __name__ == "__main__":
    unittest.main()
