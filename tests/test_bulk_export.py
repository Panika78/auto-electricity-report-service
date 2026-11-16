"""End-to-end Issue #5 tests against synthetic SQLite, real ZIP and PDF bytes."""
from __future__ import annotations

import errno
import importlib.util
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path, PureWindowsPath
from unittest import mock

from pypdf import PdfReader

from app.core.service import CalculationService
from app.data import database as db
from app.documents import export_bulk_zip


class BulkZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = db.Database(self.root / "synthetic.db")
        self.database.initialize()
        self.service = CalculationService(self.database)
        with self.database.transaction() as conn:
            db.update_settings(conn, performer_name="ИП Синтетический")
        self.service.set_tariff(2026, 10, "11.07")

    def tenant(self, name="ООО Вымышленный", *, archived=False):
        return self.service.create_tenant(name=name, is_archived=archived)["id"]

    def calc(self, tenant_id, start="100", end="110", **kwargs):
        return self.service.create_calculation(
            tenant_id, 2026, 10, start_reading=start, end_reading=end, **kwargs)["id"]

    def docs(self, calculation_id):
        with self.database.connection() as conn:
            return {d["type"]: d for d in db.list_documents(conn, calculation_id=calculation_id)}

    def entries(self, result):
        self.assertIsNotNone(result.path)
        with zipfile.ZipFile(result.path) as z:
            self.assertIsNone(z.testzip())
            entries = {name: z.read(name) for name in z.namelist()}
        self.assertEqual(len(entries), len(result.successful))
        self.assertEqual(set(entries), {d.entry_name for d in result.successful})
        for name, content in entries.items():
            self.assertTrue(content.startswith(b"%PDF-"), name)
            self.assertTrue(PdfReader(io.BytesIO(content)).pages, name)
            self.assertEqual(len(name.split("/")), 2)
            self.assertNotIn("\\", name)
            self.assertNotIn("..", name.split("/"))
            self.assertFalse(name.startswith("/"))
        self.assertEqual(len({name.casefold() for name in entries}), len(entries))
        return entries

    def test_selects_previous_ready_skips_zero_drafts_missing_and_archived(self):
        chosen = self.tenant("Синтетический готовый")
        older = self.calc(chosen, end="120")
        self.calc(chosen, end=None, confirm_overlap=True)  # newer draft
        zero = self.tenant("Синтетический ноль")
        self.calc(zero, end="120")
        self.calc(zero, end="100", confirm_overlap=True)  # latest ready is zero
        absent = self.tenant("Синтетический без расчёта")
        invalid = self.tenant("Синтетический некорректный")
        self.calc(invalid, start="200", end="100")
        archived = self.tenant("Синтетический архив", archived=True)
        self.calc(archived)
        result = export_bulk_zip(self.service, 2026, 10, self.root / "selected.zip",
                                 kinds=("invoice",))
        self.assertTrue(result.statuses_confirmed)
        self.assertEqual({d.calculation_id for d in result.successful}, {older})
        self.assertEqual({s["tenant_id"] for s in result.skipped}, {zero, absent, invalid})
        self.assertEqual({s["reason"] for s in result.skipped},
                         {"zero_consumption", "no_calculation", "invalid_readings"})
        content = next(iter(self.entries(result).values()))
        text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)
        self.assertIn("Синтетический готовый", text)
        self.assertNotIn("Синтетический архив", text)
        self.assertEqual(set(self.docs(older)), {"invoice"})

    def test_missing_tariff_skips_without_creating_zip(self):
        tenant = self.tenant()
        self.service.create_calculation(tenant, 2026, 9, start_reading="10", end_reading="20")
        result = export_bulk_zip(self.service, 2026, 9, self.root / "empty.zip")
        self.assertFalse(result.successful)
        self.assertFalse((self.root / "empty.zip").exists())
        self.assertEqual(result.skipped[0]["reason"], "missing_tariff")

    def test_two_types_collisions_and_stable_numbers_dates_on_repeat(self):
        a = self.tenant("Одинаковая / компания: <опасное>")
        b = self.tenant("Одинаковая / компания: <опасное>")
        ids = [self.calc(a), self.calc(b)]
        # Explicit manual duplicate is allowed and must not lose either PDF.
        self.service.reserve_document(ids[0], "act", number=51,
                                      document_date="2027-01-09")
        self.service.reserve_document(ids[1], "act", number=51,
                                      document_date="2027-01-09", confirm_duplicate=True)
        target = self.root / "first.zip"
        first = export_bulk_zip(self.service, 2026, 10, target)
        self.assertEqual(len(first.successful), 4)
        self.assertEqual(len(self.entries(first)), 4)
        self.assertFalse(first.failed)
        before = {cid: self.docs(cid) for cid in ids}
        second = export_bulk_zip(self.service, 2026, 10, self.root / "second.zip")
        self.assertEqual(len(self.entries(second)), 4)
        after = {cid: self.docs(cid) for cid in ids}
        for cid in ids:
            self.assertEqual(
                [(d["id"], d["number"], d["document_date"]) for d in before[cid].values()],
                [(d["id"], d["number"], d["document_date"]) for d in after[cid].values()])
            self.assertTrue(all(d["is_current"] and d["last_generated_at"]
                                for d in after[cid].values()))
        self.assertEqual({d.number for d in first.successful if d.kind == "act"}, {51})
        self.assertEqual(len({d.entry_name.split("/")[0].casefold()
                              for d in first.successful}), 2)

    def test_renderer_fails_for_one_type_without_losing_other(self):
        cid = self.calc(self.tenant())
        from app.documents import bulk
        def broken(_snapshot):
            raise RuntimeError("Синтетическая ошибка рендера счёта")
        with mock.patch.dict(bulk._RENDERERS, {"invoice": broken}):
            result = export_bulk_zip(self.service, 2026, 10, self.root / "partial.zip")
        self.assertEqual([d.kind for d in result.successful], ["act"])
        self.assertEqual([f.kind for f in result.failed], ["invoice"])
        self.assertIn("Синтетическая", str(result.failed[0].error))
        self.assertEqual(len(self.entries(result)), 1)
        self.assertEqual(self.service.document_status(cid, "act"), "current")
        self.assertEqual(self.service.document_status(cid, "invoice"), "not_created")

    def test_missing_performer_name_blocks_pdf_as_in_individual_ui(self):
        cid = self.calc(self.tenant())
        self.service.update_settings(performer_name="")
        result = export_bulk_zip(self.service, 2026, 10, self.root / "no-performer.zip")
        self.assertEqual(len(result.failed), 2)
        self.assertTrue(all("исполнителя" in str(item.error) for item in result.failed))
        self.assertFalse((self.root / "no-performer.zip").exists())
        self.assertEqual(self.service.document_status(cid, "act"), "not_created")

    def test_all_renderers_fail_does_not_publish_empty_zip_or_mark_success(self):
        cid = self.calc(self.tenant())
        from app.documents import bulk
        with mock.patch.dict(bulk._RENDERERS, {
            "act": mock.Mock(side_effect=ValueError("акт")),
            "invoice": mock.Mock(side_effect=ValueError("счёт"))}):
            result = export_bulk_zip(self.service, 2026, 10, self.root / "empty.zip")
        self.assertEqual(len(result.failed), 2)
        self.assertEqual(result.successful, [])
        self.assertFalse((self.root / "empty.zip").exists())
        self.assertEqual(self.service.document_status(cid, "act"), "not_created")
        self.assertEqual(self.service.document_status(cid, "invoice"), "not_created")

    def test_archive_collision_refused_before_any_document_reservation(self):
        cid = self.calc(self.tenant())
        target = self.root / "existing.zip"
        target.write_bytes(b"PREVIOUS ARCHIVE")
        with self.assertRaises(FileExistsError):
            export_bulk_zip(self.service, 2026, 10, target)
        self.assertEqual(target.read_bytes(), b"PREVIOUS ARCHIVE")
        self.assertEqual(self.docs(cid), {})

    def test_overwrite_failure_preserves_existing_zip_and_ungenerated_status(self):
        cid = self.calc(self.tenant())
        target = self.root / "existing.zip"
        target.write_bytes(b"PREVIOUS ARCHIVE")
        with mock.patch("app.documents.bulk.os.replace", side_effect=PermissionError("denied")):
            result = export_bulk_zip(self.service, 2026, 10, target, overwrite=True)
        self.assertIsNone(result.path)
        self.assertIsInstance(result.publication_error, PermissionError)
        self.assertEqual(target.read_bytes(), b"PREVIOUS ARCHIVE")
        self.assertEqual(self.service.document_status(cid, "act"), "not_created")
        self.assertFalse(list(self.root.glob(".kontakt-zip-*")))

    def test_concurrent_change_of_existing_archive_prevents_overwrite(self):
        cid = self.calc(self.tenant())
        target = self.root / "changed.zip"
        target.write_bytes(b"ORIGINAL")
        from app.documents import bulk
        render = bulk._RENDERERS["act"]

        def foreign_edit(snapshot):
            target.write_bytes(b"OTHER WRITER")
            return render(snapshot)

        with mock.patch.dict(bulk._RENDERERS, {"act": foreign_edit}):
            result = export_bulk_zip(self.service, 2026, 10, target,
                                     kinds=("act",), overwrite=True)
        self.assertIsNone(result.path)
        self.assertIsNotNone(result.publication_error)
        self.assertEqual(target.read_bytes(), b"OTHER WRITER")
        self.assertEqual(self.service.document_status(cid, "act"), "not_created")

    def test_foreign_change_after_publication_does_not_mark_success(self):
        cid = self.calc(self.tenant())
        target = self.root / "foreign.zip"
        from app.documents import bulk
        original_publish = bulk._publish_new_zip

        def foreign_edit(staged, published):
            moved = original_publish(staged, published)
            published.write_bytes(b"FOREIGN BYTES")
            return moved

        with mock.patch.object(bulk, "_publish_new_zip", side_effect=foreign_edit):
            result = export_bulk_zip(self.service, 2026, 10, target,
                                     kinds=("act",))
        self.assertEqual(result.path, target)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"FOREIGN BYTES")
        self.assertIsNotNone(result.integrity_error)
        self.assertIsNone(result.metadata_error)
        self.assertEqual(result.successful, [])
        self.assertEqual(len(result.unverified), 1)
        self.assertFalse(result.statuses_confirmed)
        self.assertEqual(self.service.document_status(cid, "act"), "not_created")

    def test_new_zip_publish_failure_does_not_report_success(self):
        cid = self.calc(self.tenant())
        target = self.root / "unwritable.zip"
        with mock.patch("app.documents.export.os.link",
                        side_effect=OSError(errno.EACCES, "denied")):
            result = export_bulk_zip(self.service, 2026, 10, target)
        self.assertIsNone(result.path)
        self.assertFalse(result.successful)
        self.assertIsNotNone(result.publication_error)
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob(".kontakt-zip-*")))
        self.assertEqual(self.service.document_status(cid, "act"), "not_created")

    def test_no_hardlinks_uses_atomic_no_replace_and_real_zip(self):
        cid = self.calc(self.tenant())
        target = self.root / "no-link.zip"
        with mock.patch("app.documents.bulk.os.link",
                        side_effect=OSError(errno.ENOTSUP, "hardlinks not supported")):
            result = export_bulk_zip(self.service, 2026, 10, target)
        self.assertTrue(result.statuses_confirmed)
        self.assertEqual(len(self.entries(result)), 2)
        self.assertFalse(list(self.root.glob(".kontakt-zip-*")))
        self.assertEqual(self.service.document_status(cid, "act"), "current")

    def test_no_hardlinks_racing_creator_is_never_overwritten(self):
        cid = self.calc(self.tenant())
        target = self.root / "race.zip"
        from app.documents import bulk
        rename = bulk._rename_no_replace

        def concurrent_creator(staged, destination):
            destination.write_bytes(b"ANOTHER OWNER")
            return rename(staged, destination)

        with mock.patch("app.documents.bulk.os.link",
                        side_effect=OSError(errno.ENOTSUP, "hardlinks not supported")), \
             mock.patch.object(bulk, "_rename_no_replace",
                               side_effect=concurrent_creator):
            result = export_bulk_zip(self.service, 2026, 10, target)
        self.assertIsNone(result.path)
        self.assertIsNotNone(result.publication_error)
        self.assertEqual(target.read_bytes(), b"ANOTHER OWNER")
        self.assertFalse(list(self.root.glob(".kontakt-zip-*")))
        self.assertEqual(self.service.document_status(cid, "act"), "not_created")

    def test_no_hardlinks_without_safe_rename_fails_closed(self):
        cid = self.calc(self.tenant())
        target = self.root / "unsupported.zip"
        with mock.patch("app.documents.bulk.os.link",
                        side_effect=OSError(errno.ENOTSUP, "hardlinks not supported")), \
             mock.patch("app.documents.bulk._rename_no_replace",
                        side_effect=OSError(errno.ENOTSUP, "atomic rename unavailable")):
            result = export_bulk_zip(self.service, 2026, 10, target)
        self.assertIsNone(result.path)
        self.assertEqual(target.exists(), False)
        self.assertIsNotNone(result.publication_error)
        self.assertFalse(list(self.root.glob(".kontakt-zip-*")))
        self.assertEqual(self.service.document_status(cid, "act"), "not_created")

    def test_crash_before_fallback_atomic_rename_leaves_no_final_zip(self):
        # A real process termination cannot trigger exception handlers.
        # Unlike the former exclusive-create streaming copy, the new fallback
        # does NOT touch the final path before its single atomic rename.
        staged = self.root / "private-stage.tmp"
        target = self.root / "after-crash.zip"
        staged.write_bytes(b"COMPLETED STAGED ARCHIVE")
        program = (
            "import errno, os, sys\n"
            "from pathlib import Path\n"
            "from unittest import mock\n"
            "from app.documents import bulk\n"
            "with mock.patch('app.documents.bulk.os.link', "
            "side_effect=OSError(errno.ENOTSUP, 'no links')), "
            "mock.patch.object(bulk, '_rename_no_replace', "
            "side_effect=lambda *_: os._exit(73)):\n"
            "    bulk._publish_new_zip(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", program, str(staged), str(target)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(proc.returncode, 73, proc.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(staged.read_bytes(), b"COMPLETED STAGED ARCHIVE")

    def test_deleted_zip_after_publication_is_integrity_not_sqlite_failure(self):
        cid = self.calc(self.tenant())
        target = self.root / "gone.zip"
        from app.documents import bulk
        original = bulk._publish_new_zip

        def disappear(staged, destination):
            moved = original(staged, destination)
            destination.unlink()
            return moved

        with mock.patch.object(bulk, "_publish_new_zip", side_effect=disappear):
            result = export_bulk_zip(self.service, 2026, 10, target, kinds=("act",))
        self.assertEqual(result.path, target)
        self.assertFalse(target.exists())
        self.assertIsInstance(result.integrity_error, FileNotFoundError)
        self.assertIsNone(result.metadata_error)
        self.assertFalse(result.successful)
        self.assertEqual(len(result.unverified), 1)
        self.assertFalse(result.statuses_confirmed)
        self.assertEqual(self.service.document_status(cid, "act"), "not_created")

    def test_newer_ready_zero_during_render_prevents_stale_statuses(self):
        tenant = self.tenant("Новый нулевой расчёт")
        original_id = self.calc(tenant)
        other = self.calc(self.tenant("Неизменённый второй арендатор"))
        from app.documents import bulk
        renderer = bulk._RENDERERS["act"]
        created = []

        def concurrent_write(snapshot):
            if not created:
                other_service = CalculationService(db.Database(self.database.path))
                newer = other_service.create_calculation(
                    tenant, 2026, 10, start_reading="100", end_reading="100",
                    confirm_overlap=True)
                created.append(newer["id"])
            return renderer(snapshot)

        with mock.patch.dict(bulk._RENDERERS, {"act": concurrent_write}):
            result = export_bulk_zip(self.service, 2026, 10, self.root / "race-calc.zip",
                                     kinds=("act",))
        self.assertEqual(len(created), 1)
        self.assertEqual(len(self.entries(result)), 2)
        self.assertIsInstance(result.metadata_error, ValueError)
        self.assertIn("Последний готовый расчёт", str(result.metadata_error))
        self.assertFalse(result.statuses_confirmed)
        self.assertEqual(self.service.document_status(original_id, "act"), "not_created")
        self.assertEqual(self.service.document_status(other, "act"), "not_created")
        self.assertEqual(self.service.bulk_candidates(2026, 10)["skipped"][0]["reason"],
                         "zero_consumption")

    def test_post_publication_db_failure_preserves_zip_without_false_status(self):
        cid = self.calc(self.tenant())
        target = self.root / "published.zip"
        with mock.patch.object(self.service, "mark_documents_generated",
                               side_effect=OSError("synthetic locked database")):
            result = export_bulk_zip(self.service, 2026, 10, target)
        self.assertEqual(len(self.entries(result)), 2)
        self.assertFalse(result.statuses_confirmed)
        self.assertIsNotNone(result.metadata_error)
        for kind in ("act", "invoice"):
            self.assertEqual(self.service.document_status(cid, kind), "not_created")
        # Retrying a later export preserves reservations without regenerating numbers.
        old = self.docs(cid)
        retry = export_bulk_zip(self.service, 2026, 10, self.root / "retry.zip")
        self.assertTrue(retry.statuses_confirmed)
        new = self.docs(cid)
        self.assertEqual([(old[k]["number"], old[k]["document_date"]) for k in old],
                         [(new[k]["number"], new[k]["document_date"]) for k in new])

    def test_second_sqlite_update_failure_rolls_back_first_status(self):
        cid = self.calc(self.tenant())
        original = db.update_document
        calls = 0

        def fail_second(conn, document_id, **changes):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("synthetic status write failure")
            return original(conn, document_id, **changes)

        with mock.patch("app.core.service.db.update_document", side_effect=fail_second):
            result = export_bulk_zip(self.service, 2026, 10, self.root / "rollback.zip")
        self.assertEqual(len(self.entries(result)), 2)
        self.assertEqual(calls, 2)
        self.assertIsInstance(result.metadata_error, sqlite3.OperationalError)
        for kind in ("act", "invoice"):
            self.assertEqual(self.service.document_status(cid, kind), "not_created")
            self.assertIsNone(self.docs(cid)[kind]["last_generated_at"])

    def test_zip_write_error_cleans_stage_and_preserves_old_archive(self):
        cid = self.calc(self.tenant())
        target = self.root / "old.zip"
        target.write_bytes(b"OLD ZIP")
        with mock.patch("app.documents.bulk.zipfile.ZipFile.writestr",
                        side_effect=OSError("synthetic disk full")):
            result = export_bulk_zip(self.service, 2026, 10, target, overwrite=True)
        self.assertIsNotNone(result.publication_error)
        self.assertEqual(result.successful, [])
        self.assertEqual(target.read_bytes(), b"OLD ZIP")
        self.assertEqual(self.service.document_status(cid, "act"), "not_created")
        self.assertFalse(list(self.root.glob(".kontakt-zip-*")))

    def test_revision_change_after_publication_prevents_all_status_updates(self):
        first = self.calc(self.tenant("Изменённый"))
        second = self.calc(self.tenant("Неизменённый"))
        original = self.service.mark_documents_generated
        def mutate(revisions, **kwargs):
            self.service.update_calculation(first, end_reading="115")
            return original(revisions, **kwargs)
        with mock.patch.object(self.service, "mark_documents_generated", side_effect=mutate):
            result = export_bulk_zip(self.service, 2026, 10, self.root / "stale.zip")
        self.assertEqual(len(self.entries(result)), 4)
        self.assertIsInstance(result.metadata_error, ValueError)
        for cid in (first, second):
            for kind in ("act", "invoice"):
                self.assertEqual(self.service.document_status(cid, kind), "not_created")

    def test_rejects_invalid_kind_and_unsafe_zip_target_before_writing_metadata(self):
        cid = self.calc(self.tenant())
        for kinds in ((), ("act", "act"), ("doc",)):
            with self.subTest(kinds=kinds), self.assertRaises(ValueError):
                export_bulk_zip(self.service, 2026, 10, self.root / "test.zip", kinds=kinds)
        for name in ("CON.zip", "unsafe?.zip", "wrong.pdf"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                export_bulk_zip(self.service, 2026, 10, self.root / name)
        self.assertEqual(self.docs(cid), {})

    def test_named_windows_device_and_casefold_directory_avoidance(self):
        # Windows reserves these names even with an extension followed by
        # _T{id}, and recognises superscript digits as COM/LPT port numbers.
        names = (
            "CON", "con", "..", "A\\B",
            "COM¹.txt", "COM².txt", "COM³.txt",
            "LPT¹.txt", "LPT².txt", "LPT³.txt",
            "CONIN$.txt", "CONOUT$.txt",
        )
        ids = [self.calc(self.tenant(name)) for name in names]
        result = export_bulk_zip(self.service, 2026, 10, self.root / "safe.zip",
                                 kinds=("act",))
        entries = self.entries(result)
        self.assertTrue(result.statuses_confirmed)
        self.assertEqual(len(entries), len(names))
        self.assertEqual(len({n.casefold() for n in entries}), len(names))
        for entry in entries:
            folder, filename = entry.split("/")
            self.assertFalse(PureWindowsPath(folder).is_reserved(), folder)
            self.assertFalse(PureWindowsPath(filename).is_reserved(), filename)
            self.assertFalse(folder.endswith((" ", ".")), folder)
            self.assertFalse(filename.endswith((" ", ".")), filename)
            self.assertTrue(filename.endswith(".pdf"))
        for doc in result.successful:
            if doc.tenant_name.startswith(("COM¹", "COM²", "COM³",
                                           "LPT¹", "LPT²", "LPT³",
                                           "CONIN$", "CONOUT$")):
                self.assertTrue(doc.entry_name.split("/")[0].startswith("_"),
                                doc.entry_name)

    def test_superscript_windows_archive_names_rejected_before_reservations(self):
        cid = self.calc(self.tenant("Проверка пути ZIP"))
        for name in ("COM¹.zip", "COM².zip", "COM³.zip",
                     "LPT¹.zip", "LPT².zip", "LPT³.zip",
                     "CONIN$.zip", "CONOUT$.zip", "COM¹.txt.zip"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                export_bulk_zip(self.service, 2026, 10, self.root / name)
            # Windows resolves reserved DOS device names as existing paths,
            # even if no directory entry was created. Inspect actual entries.
            self.assertNotIn(name, {item.name for item in self.root.iterdir()})
        self.assertEqual(self.docs(cid), {})

    def test_individual_pdf_superscript_reserved_name_rejected_before_reservation(self):
        from app.documents.export import export_document
        cid = self.calc(self.tenant("Проверка пути PDF"))
        for name in ("COM¹.pdf", "COM².pdf", "LPT³.pdf", "CONIN$.pdf"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                export_document(self.service, cid, "act", self.root / name)
            # Windows resolves reserved DOS device names as existing paths,
            # even if no directory entry was created. Inspect actual entries.
            self.assertNotIn(name, {item.name for item in self.root.iterdir()})
        self.assertEqual(self.docs(cid), {})


HAS_QT = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_QT, "PySide6 недоступен")
class BulkQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        import gc
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
        for widget in QApplication.topLevelWidgets():
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        gc.collect()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = db.Database(Path(self.tmp.name) / "qt.db")
        self.database.initialize()
        self.service = CalculationService(self.database)
        with self.database.transaction() as conn:
            db.update_settings(conn, performer_name="ИП Тестовый")
        self.service.set_tariff(2026, 10, "10")

    def test_bulk_button_enabled_and_uses_selected_month(self):
        from app.ui.main_window import MainWindow
        from app.ui.forms import BulkExportDialog
        window = MainWindow(self.database)
        try:
            window.year, window.month = 2026, 10
            self.assertTrue(window.bulk_action.isEnabled())
            with mock.patch.object(BulkExportDialog, "exec", return_value=0) as dialog:
                window.bulk_action.click()
                dialog.assert_called_once()
        finally:
            window.close()

    def test_no_selection_blocks_file_picker(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from app.ui.forms import BulkExportDialog
        dialog = BulkExportDialog(self.service, 2026, 10)
        try:
            dialog.act_check.setChecked(False)
            dialog.invoice_check.setChecked(False)
            with mock.patch.object(QMessageBox, "warning") as warning, \
                 mock.patch.object(QFileDialog, "getSaveFileName") as picker:
                dialog._export()
            warning.assert_called_once()
            picker.assert_not_called()
        finally:
            dialog.close()

    def test_existing_zip_cannot_be_overwritten_without_confirmation(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from app.ui.forms import BulkExportDialog
        tenant = self.service.create_tenant(name="Синтетический GUI")
        cid = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="15")["id"]
        target = Path(self.tmp.name) / "previous.zip"
        target.write_bytes(b"PREVIOUS FILE")
        dialog = BulkExportDialog(self.service, 2026, 10)
        try:
            with mock.patch.object(QFileDialog, "getSaveFileName",
                                   return_value=(str(target), "ZIP (*.zip)")), \
                 mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.StandardButton.No) as question:
                dialog._export()
            question.assert_called_once()
            self.assertEqual(target.read_bytes(), b"PREVIOUS FILE")
            with self.database.connection() as conn:
                self.assertEqual(db.list_documents(conn, calculation_id=cid), [])
        finally:
            dialog.close()

    def test_dialog_does_not_claim_success_for_changed_or_deleted_zip(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from app.ui.forms import BulkExportDialog
        from app.documents import bulk
        tenant = self.service.create_tenant(name="Проверка сообщения")
        cid = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="15")["id"]
        for delete in (False, True):
            with self.subTest(delete=delete):
                dialog = BulkExportDialog(self.service, 2026, 10)
                dialog.invoice_check.setChecked(False)
                target = Path(self.tmp.name) / ("removed.zip" if delete else "modified.zip")
                original = bulk._publish_new_zip

                def interfere(stage, destination):
                    moved = original(stage, destination)
                    if delete:
                        destination.unlink()
                    else:
                        destination.write_bytes(b"INVALID ZIP CONTENT")
                    return moved

                try:
                    with mock.patch.object(QFileDialog, "getSaveFileName",
                                           return_value=(str(target), "ZIP (*.zip)")), \
                         mock.patch.object(QMessageBox, "warning") as warning, \
                         mock.patch.object(QMessageBox, "information") as information, \
                         mock.patch.object(bulk, "_publish_new_zip",
                                           side_effect=interfere):
                        dialog._export()
                    warning.assert_called_once()
                    report = warning.call_args.args[2]
                    self.assertIn("содержимое ZIP после публикации НЕ подтверждено", report)
                    self.assertIn("статусы документов не обновлены", report)
                    self.assertNotIn("ZIP сохранён:", report)
                    self.assertNotIn("PDF в архиве:", report)
                    self.assertNotIn("Выгружены:", report)
                    information.assert_not_called()
                    self.assertEqual(dialog.result(), 0)
                    self.assertEqual(self.service.document_status(cid, "act"), "not_created")
                finally:
                    dialog.close()

    def test_dialog_distinguishes_valid_zip_with_failed_sqlite_commit(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from app.ui.forms import BulkExportDialog
        dialog = BulkExportDialog(self.service, 2026, 10)
        self.service.create_calculation(
            self.service.create_tenant(name="Ошибка статуса")["id"],
            2026, 10, start_reading="10", end_reading="15")
        target = Path(self.tmp.name) / "valid-but-unconfirmed.zip"
        try:
            with mock.patch.object(QFileDialog, "getSaveFileName",
                                   return_value=(str(target), "ZIP (*.zip)")), \
                 mock.patch.object(QMessageBox, "warning") as warning, \
                 mock.patch.object(self.service, "mark_documents_generated",
                                   side_effect=sqlite3.OperationalError("locked")):
                dialog._export()
            warning.assert_called_once()
            report = warning.call_args.args[2]
            self.assertIn("ZIP сохранён:", report)
            self.assertIn("статусы документов в базе НЕ подтверждены", report)
            self.assertNotIn("содержимое ZIP после публикации НЕ подтверждено", report)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(len(archive.namelist()), 2)
        finally:
            dialog.close()

    def test_dialog_selects_one_type_and_publishes_real_zip(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from app.ui.forms import BulkExportDialog
        tenant = self.service.create_tenant(name="Арендатор GUI")
        cid = self.service.create_calculation(
            tenant["id"], 2026, 10, start_reading="10", end_reading="15")["id"]
        dialog = BulkExportDialog(self.service, 2026, 10)
        target = Path(self.tmp.name) / "gui.zip"
        try:
            dialog.invoice_check.setChecked(False)
            with mock.patch.object(QFileDialog, "getSaveFileName",
                                   return_value=(str(target), "ZIP (*.zip)")), \
                 mock.patch.object(QMessageBox, "information") as info:
                dialog._export()
            self.assertTrue(target.exists())
            info.assert_called_once()
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(len(archive.namelist()), 1)
                self.assertIn("Акт_", archive.namelist()[0])
            self.assertEqual(self.service.document_status(cid, "act"), "current")
            self.assertEqual(self.service.document_status(cid, "invoice"), "not_created")
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
