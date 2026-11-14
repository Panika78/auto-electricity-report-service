"""Native GUI integration tests against temporary SQLite and real Qt (offscreen)."""
from __future__ import annotations

import importlib.util
import os
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
        self.service.set_tariff(2026, 10, "5")
        calc = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="50", end_reading="50")
        dialog = ExportDialog(self.service, self.database, calc["id"])
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No), \
             mock.patch.object(QFileDialog, "getSaveFileName") as chooser:
            dialog._export()
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
        previous_size = settings.value("font_size", "standard")
        previous_weight = settings.value("font_weight", "normal")
        try:
            page.size_picker.setCurrentIndex(page.size_picker.findData("large"))
            page.weight_picker.setCurrentIndex(page.weight_picker.findData("bold"))
            self.assertEqual(settings.value("font_size"), "large")
            self.assertEqual(settings.value("font_weight"), "bold")
            self.assertEqual(self.service.document_status(calc["id"], "act"), "current")
        finally:
            settings.setValue("font_size", previous_size)
            settings.setValue("font_weight", previous_weight)
            settings.sync()
            from app.ui.common import apply_appearance
            apply_appearance()

if __name__ == "__main__":
    unittest.main()
