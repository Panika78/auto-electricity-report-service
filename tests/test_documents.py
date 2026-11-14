"""PDF content and failure-path tests. All names/accounts are synthetic."""
from __future__ import annotations

import copy
import io
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader

from app.documents import (ExportCancelled, ExportError, export_document,
                           export_documents, render_act, render_invoice, safe_filename)
from app.documents.common import DocumentRenderError


def snapshot(kind="act"):
    return {
        "calculation": {"period_start": "2026-10-05", "period_end": "2026-10-30",
                        "start_reading": D("100.125"), "end_reading": D("109.625"),
                        "consumption": D("9.500"), "total_amount": D("105.17")},
        "tariff": {"rate": D("11.0704")},
        "tenant": {"name": "ООО «Лаборатория & Ко»", "legal_address": "г. Примерск, ул. Синтетическая, 7",
                   "contract_number": "Ф-11", "contract_date": "2025-01-09",
                   "additional_agreement": "Допсоглашение № 2 от 01.10.2026"},
        "performer": {"performer_name": "ИП Елена Вымышленная", "address": "г. Тестоград\nул. Тестовая, 1",
                      "inn": "000000000000", "kpp": "", "bank_name": "Учебный банк",
                      "bik": "000000000", "account_number": "00000000000000000000",
                      "correspondent_account": "11111111111111111111",
                      "signature_path": None, "stamp_path": None},
        "document": {"type": kind, "id": 3, "number": 15, "document_date": "2026-11-02",
                     "updated_at": "r1"},
        "amount_in_words": "Сто пять рублей 17 копеек",
        "revision": {"calculation": "r1", "tenant": "r1", "document": "r1"},
    }


def text_of(pdf: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


class RenderTests(unittest.TestCase):
    def test_act_essential_text_and_cyrillic(self):
        data = render_act(snapshot())
        reader = PdfReader(io.BytesIO(data))
        self.assertEqual(len(reader.pages), 1)
        text = text_of(data)
        for token in ("АКТ № 15", "02.11.2026", "ООО «Лаборатория & Ко»",
                      "ИП Елена Вымышленная", "05.10.2026", "30.10.2026",
                      "100.125", "109.625", "9.500", "11-0704", "105-17",
                      "Сто пять рублей 17 копеек", "Сумма НДС:", "0,00",
                      "Ф-11", "Допсоглашение № 2", "Заказчик:"):
            self.assertIn(token, text)

    def test_invoice_essential_text_and_cyrillic(self):
        data = render_invoice(snapshot("invoice"))
        self.assertEqual(len(PdfReader(io.BytesIO(data)).pages), 1)
        text = text_of(data)
        for token in ("Счет на оплату № 15", "2 ноября 2026", "Учебный банк",
                      "000000000", "11111111111111111111", "00000000000000000000",
                      "Лаборатория & Ко", "ИП Елена Вымышленная", "05.10.2026",
                      "30.10.2026", "9.500", "11.0704", "105.17", "Сто пять рублей",
                      "В том числе НДС:", "0-00", "Бухгалтер", "Руководитель"):
            self.assertIn(token, text)

    def test_snapshot_same_pdf_bytes(self):
        for kind, func in (("act", render_act), ("invoice", render_invoice)):
            values = snapshot(kind)
            self.assertEqual(func(values), func(values))

    def test_manual_values_are_not_recalculated(self):
        for kind, func in (("act", render_act), ("invoice", render_invoice)):
            values = snapshot(kind)
            values["calculation"].update(consumption=D("1.250"), total_amount=D("88.42"))
            values["amount_in_words"] = "Восемьдесят восемь рублей 42 копейки"
            text = text_of(func(values))
            self.assertIn("1.250", text)
            self.assertIn("88.42" if kind == "invoice" else "88-42", text)
            self.assertIn("Восемьдесят восемь", text)

    def test_zero_and_large_amounts(self):
        for kind, func in (("act", render_act), ("invoice", render_invoice)):
            values = snapshot(kind)
            values["calculation"].update(consumption=D("0.000"), total_amount=D("0.00"))
            values["amount_in_words"] = "Ноль рублей 00 копеек"
            self.assertIn("Ноль рублей 00 копеек", text_of(func(values)))
            values["calculation"].update(consumption=D("1000000.000"),
                                         total_amount=D("1000000000.01"))
            values["amount_in_words"] = "Один миллиард рублей 01 копейка"
            self.assertIn("Один миллиард", text_of(func(values)))

    def test_escaped_html_very_long_name_and_multiline_address(self):
        for kind, func in (("act", render_act), ("invoice", render_invoice)):
            values = snapshot(kind)
            values["tenant"]["name"] = "ООО <Проверка> & 'Кавычки' " + "ОченьДлинноеИмя" * 19
            values["performer"]["address"] = "Строка & <один>\n" * 7
            text = text_of(func(values))
            self.assertIn("<Проверка>", text)
            self.assertIn("ОченьДлинноеИмя", text)
            if kind == "act":
                self.assertIn("<один>", text)

    def test_absent_optional_fields(self):
        for kind, func in (("act", render_act), ("invoice", render_invoice)):
            values = snapshot(kind)
            values["tenant"].update(contract_number="", contract_date=None,
                                    additional_agreement="", legal_address="")
            values["performer"].update(bank_name="", bik="", signature_path=None, stamp_path=None)
            self.assertTrue(func(values).startswith(b"%PDF-"))

    def test_missing_and_corrupt_optional_images_raise(self):
        for kind, func in (("act", render_act), ("invoice", render_invoice)):
            values = snapshot(kind)
            values["performer"]["signature_path"] = "/unavailable/sample-sign.png"
            with self.assertRaises(DocumentRenderError):
                func(values)
            with tempfile.TemporaryDirectory() as d:
                bad = Path(d) / "broken.png"
                bad.write_bytes(b"not an image")
                values["performer"]["signature_path"] = None
                values["performer"]["stamp_path"] = str(bad)
                with self.assertRaises(DocumentRenderError):
                    func(values)

    def test_signature_and_stamp_are_independent(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            image = Path(d) / "im.png"
            Image.new("RGBA", (240, 90), (20, 40, 60, 150)).save(image)
            for kind, func in (("act", render_act), ("invoice", render_invoice)):
                for field in ("signature_path", "stamp_path"):
                    values = snapshot(kind)
                    values["performer"][field] = str(image)
                    data = func(values)
                    self.assertTrue(data.startswith(b"%PDF-"))
                    self.assertIn("Заказчик:" if kind == "act" else "Бухгалтер", text_of(data))


class FakeService:
    def __init__(self):
        self.records = {}
        self.source_revision = 1
        self.marked = []
        self.reserved = []
        self.fail_mark = False

    def reserve_document(self, calc_id, kind, **kw):
        self.reserved.append((calc_id, kind, kw))
        if kind not in self.records:
            self.records[kind] = {"type": kind, "id": len(self.records) + 1,
                                  "number": kw.get("number") or 1,
                                  "document_date": kw.get("document_date") or "2026-11-02"}
        return copy.deepcopy(self.records[kind])

    def prepare_document(self, calc_id, kind, **kw):
        data = snapshot(kind)
        data["document"] = copy.deepcopy(self.records[kind])
        data["revision"] = {"revision": str(self.source_revision)}
        return data

    def mark_document_generated(self, document_id, *, expected_revision):
        if self.fail_mark or expected_revision != {"revision": str(self.source_revision)}:
            raise ValueError("revision conflict")
        self.marked.append(document_id)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "act.pdf"
        self.service = FakeService()

    def test_export_and_existing_target_protection(self):
        result = export_document(self.service, 5, "act", self.target)
        self.assertEqual(result.number, 1)
        self.assertEqual(self.service.marked, [1])
        previous = self.target.read_bytes()
        with self.assertRaises(FileExistsError):
            export_document(self.service, 5, "act", self.target)
        self.assertEqual(self.target.read_bytes(), previous)

    def test_cancel_before_reservation(self):
        with self.assertRaises(ExportCancelled):
            export_document(self.service, 5, "act", self.target, cancelled=True)
        self.assertEqual(self.service.reserved, [])
        self.assertFalse(self.target.exists())

    def test_bad_path_without_metadata_change(self):
        with self.assertRaises(FileNotFoundError):
            export_document(self.service, 5, "act", self.target.parent / "missing" / "x.pdf")
        self.assertFalse(self.service.reserved)

    def test_failed_render_does_not_overwrite(self):
        self.target.write_bytes(b"existing correct pdf")
        with patch("app.documents.export._RENDERERS", {"act": lambda _: (_ for _ in ()).throw(ValueError("bad render"))}):
            with self.assertRaises(ValueError):
                export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertEqual(self.target.read_bytes(), b"existing correct pdf")
        self.assertFalse(self.service.marked)

    def test_failed_publish_does_not_overwrite(self):
        self.target.write_bytes(b"old pdf")
        with patch("app.documents.export.os.replace", side_effect=OSError("file locked")):
            with self.assertRaises(ExportError):
                export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertEqual(self.target.read_bytes(), b"old pdf")
        self.assertFalse(self.service.marked)

    def test_failed_new_file_link_leaves_no_partial_file(self):
        import errno
        with patch("app.documents.export.os.link", side_effect=OSError(errno.EACCES, "blocked")):
            with self.assertRaises(ExportError):
                export_document(self.service, 5, "act", self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.service.marked)

    def test_revision_change_during_render_does_not_touch_target(self):
        self.target.write_bytes(b"old pdf")
        real_render = render_act
        def changing_render(data):
            self.service.source_revision += 1
            return real_render(data)
        with patch("app.documents.export._RENDERERS", {"act": changing_render}):
            with self.assertRaises(ExportError):
                export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertEqual(self.target.read_bytes(), b"old pdf")
        self.assertFalse(self.service.marked)

    def test_conflict_at_mark_restores_prior_file(self):
        self.target.write_bytes(b"previous complete pdf")
        self.service.fail_mark = True
        with self.assertRaises(ExportError):
            export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertEqual(self.target.read_bytes(), b"previous complete pdf")
        self.assertFalse(self.service.marked)

    def test_conflict_at_mark_deletes_new_file(self):
        self.service.fail_mark = True
        with self.assertRaises(ExportError):
            export_document(self.service, 5, "act", self.target)
        self.assertFalse(self.target.exists())

    def test_second_failure_is_separately_reported(self):
        invoice = self.target.parent / "invoice.pdf"
        invoice.write_bytes(b"old")
        result = export_documents(self.service, 5, {"act": self.target, "invoice": invoice})
        self.assertEqual(list(result.successful), ["act"])
        self.assertEqual(list(result.failed), ["invoice"])
        self.assertEqual(self.service.marked, [1])
        self.assertEqual(invoice.read_bytes(), b"old")
        self.assertTrue(self.target.exists())

    def test_same_target_is_rejected(self):
        with self.assertRaises(ValueError):
            export_documents(self.service, 5, {"act": self.target, "invoice": self.target})
        self.assertFalse(self.service.reserved)

    def test_safe_filename_windows(self):
        name = safe_filename("invoice", 3, 'CON / тест: \\ * ? "', 2026, 11)
        self.assertTrue(name.endswith("_11-2026.pdf"))
        self.assertNotIn("/", name)
        self.assertNotIn(":", name)


class RealServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        from app.core.service import CalculationService
        from app.data.database import Database
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "data.sqlite")
        self.db.initialize()
        self.service = CalculationService(self.db)
        self.service.update_settings(performer_name="ИП Вымышленная Елена", address="Тестовый адрес",
                                     bank_name="Синтетический банк", inn="000000000000",
                                     bik="000000000", account_number="00000000000000000000")
        self.tenant = self.service.create_tenant(name="ООО Тестовый арендатор",
                                                legal_address="г. Воображаемый", contract_number="Д-33")
        self.service.set_tariff(2026, 10, "11.0704")
        self.calc = self.service.create_calculation(self.tenant["id"], 2026, 10,
                                                    start_reading="10.125", end_reading="19.625",
                                                    period_start="2026-10-05", period_end="2026-10-30")

    def path(self, label):
        return Path(self.tmp.name) / (label + ".pdf")

    def test_both_independent_numbers_and_statuses(self):
        result = export_documents(self.service, self.calc["id"],
                                  {"act": self.path("a"), "invoice": self.path("b")})
        self.assertEqual(result.failed, {})
        self.assertEqual(len(result.successful), 2)
        self.assertEqual((result.successful["act"].number, result.successful["invoice"].number), (1, 1))
        self.assertEqual(self.service.document_status(self.calc["id"], "act"), "current")
        self.assertEqual(self.service.document_status(self.calc["id"], "invoice"), "current")
        self.assertIn("11.0704", text_of(self.path("b").read_bytes()))

    def test_regeneration_uses_current_values_and_keeps_date(self):
        first = export_document(self.service, self.calc["id"], "act", self.path("a"),
                                document_date="2027-01-03", number=19)
        self.service.update_tenant(self.tenant["id"], name="ООО Обновлённое учебное")
        self.service.update_calculation(self.calc["id"], end_reading="21.625")
        self.assertEqual(self.service.document_status(self.calc["id"], "act"), "needs_update")
        second = export_document(self.service, self.calc["id"], "act", self.path("a"), overwrite=True)
        self.assertEqual((first.number, first.document_date), (second.number, second.document_date))
        pdf_text = text_of(self.path("a").read_bytes())
        self.assertIn("Обновлённое учебное", pdf_text)
        self.assertIn("21.625", pdf_text)
        self.assertEqual(self.service.document_status(self.calc["id"], "act"), "current")
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("select count(*) from documents").fetchone()[0], 1)

    def test_manual_amount_and_consumption(self):
        self.service.update_calculation(self.calc["id"], consumption="8.000", confirm_manual=True)
        self.service.update_calculation(self.calc["id"], total_amount="46.02", confirm_manual=True)
        export_document(self.service, self.calc["id"], "invoice", self.path("i"))
        invoice = text_of(self.path("i").read_bytes())
        self.assertIn("8.000", invoice)
        self.assertIn("46.02", invoice)

    def test_missing_tariff_and_invalid_readings_block(self):
        bad = self.service.create_calculation(self.tenant["id"], 2026, 11,
                                              start_reading="11", end_reading="10")
        with self.assertRaises(ValueError):
            export_document(self.service, bad["id"], "act", self.path("invalid"))
        self.assertFalse(self.path("invalid").exists())
        draft = self.service.create_calculation(self.tenant["id"], 2026, 12,
                                                start_reading="10", end_reading="11")
        with self.assertRaises(ValueError):
            export_document(self.service, draft["id"], "invoice", self.path("no-tariff"))

    def test_zero_needs_explicit_confirmation(self):
        self.service.set_tariff(2026, 11, "7.0000")
        zero = self.service.create_calculation(self.tenant["id"], 2026, 11,
                                               start_reading="0", end_reading="0")
        from app.core.service import ConfirmationRequired
        with self.assertRaises(ConfirmationRequired):
            export_document(self.service, zero["id"], "invoice", self.path("zero"))
        export_document(self.service, zero["id"], "invoice", self.path("zero"), confirm_zero=True)
        self.assertIn("Ноль рублей", text_of(self.path("zero").read_bytes()))


class ReviewExportRegressionTests(unittest.TestCase):
    """Fault injections exercise the real publication/rollback machinery."""

    def setUp(self):
        ExportTests.setUp(self)

    def test_failed_restore_keeps_private_backup_and_original_cause(self):
        self.target.write_bytes(b"Previous valid PDF")
        self.service.fail_mark = True
        replace = __import__("os").replace

        def fail_only_restore(source, destination):
            if Path(source).name.startswith(".kontakt-backup-"):
                raise PermissionError("restore denied")
            return replace(source, destination)

        with patch("app.documents.export.os.replace", side_effect=fail_only_restore):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        err = ctx.exception
        self.assertTrue(err.recovery_failed)
        self.assertIsInstance(err.__cause__, ValueError)
        self.assertIsInstance(err.recovery_error, PermissionError)
        self.assertIsNotNone(err.backup_path)
        self.assertTrue(err.backup_path.exists())
        self.assertEqual(err.backup_path.read_bytes(), b"Previous valid PDF")
        self.assertEqual(self.service.marked, [])
        self.assertIn("резервная копия", str(err))
        self.assertEqual(err.path, self.target)
        # Explicit cleanup in a test with entirely synthetic bytes.
        err.backup_path.unlink()

    def test_successful_restore_consumes_backup_and_preserves_prior_file(self):
        self.target.write_bytes(b"Prior file")
        self.service.fail_mark = True
        with self.assertRaises(ExportError) as ctx:
            export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertFalse(ctx.exception.recovery_failed)
        self.assertEqual(self.target.read_bytes(), b"Prior file")
        self.assertEqual(list(self.target.parent.glob(".kontakt-backup-*.pdf")), [])
        self.assertEqual(self.service.marked, [])

    def test_backup_cleanup_failure_is_success_with_diagnostic(self):
        self.target.write_bytes(b"old")
        unlink = Path.unlink

        def deny_backup_unlink(self, *args, **kwargs):
            if self.name.startswith(".kontakt-backup-"):
                raise PermissionError("backup cleanup denied")
            return unlink(self, *args, **kwargs)

        with patch("app.documents.export.Path.unlink", new=deny_backup_unlink):
            published = export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertEqual(self.service.marked, [1])
        self.assertTrue(self.target.read_bytes().startswith(b"%PDF-"))
        self.assertIsNotNone(published.cleanup_warning)
        self.assertIsNotNone(published.leftover_backup)
        self.assertTrue(published.leftover_backup.is_file())
        self.assertEqual(published.leftover_backup.read_bytes(), b"old")
        published.leftover_backup.unlink()

    def test_unsupported_hardlinks_fall_back_to_exclusive_creation(self):
        import errno
        with patch("app.documents.export.os.link", side_effect=OSError(errno.EOPNOTSUPP, "no hardlink")):
            result = export_document(self.service, 5, "act", self.target)
        self.assertEqual(result.path, self.target)
        self.assertTrue(self.target.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(self.service.marked, [1])

    def test_exclusive_fallback_write_failure_removes_only_partial_file(self):
        import errno

        def partial_copy(source, dest, **kwargs):
            dest.write(b"incomplete")
            raise OSError(errno.ENOSPC, "disk full")

        with patch("app.documents.export.os.link", side_effect=OSError(errno.EOPNOTSUPP, "no hardlink")), \
             patch("app.documents.export.shutil.copyfileobj", side_effect=partial_copy):
            with self.assertRaises(ExportError):
                export_document(self.service, 5, "act", self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.service.marked)

    def test_exclusive_fallback_reports_unremovable_partial_file(self):
        import errno
        unlink = Path.unlink

        def deny_partial_delete(self, *args, **kwargs):
            if self == self_target:
                raise PermissionError("partial cleanup denied")
            return unlink(self, *args, **kwargs)

        def partial_copy(source, dest, **kwargs):
            dest.write(b"incomplete")
            raise OSError(errno.ENOSPC, "disk full")

        self_target = self.target
        with patch("app.documents.export.os.link", side_effect=OSError(errno.EOPNOTSUPP, "no hardlink")), \
             patch("app.documents.export.shutil.copyfileobj", side_effect=partial_copy), \
             patch("app.documents.export.Path.unlink", new=deny_partial_delete):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target)
        err = ctx.exception
        self.assertTrue(err.recovery_failed)
        self.assertIsInstance(err.cleanup_error, PermissionError)
        self.assertEqual(err.path, self.target)
        self.assertEqual(self.target.read_bytes(), b"incomplete")
        self.assertFalse(self.service.marked)
        self.target.unlink()

    def test_racing_new_file_is_never_overwritten(self):
        link = __import__("os").link

        def other_writer(source, dest, *args, **kwargs):
            Path(dest).write_bytes(b"other owner's PDF")
            raise FileExistsError("other writer created destination")

        with patch("app.documents.export.os.link", side_effect=other_writer):
            with self.assertRaises(ExportError):
                export_document(self.service, 5, "act", self.target)
        self.assertEqual(self.target.read_bytes(), b"other owner's PDF")
        self.assertEqual(self.service.marked, [])

    def test_same_destination_via_relative_and_dot_dot_paths(self):
        other = self.target.parent / "sub"
        other.mkdir()
        alias = other / ".." / "act.pdf"
        with self.assertRaises(ValueError):
            export_documents(self.service, 5, {"act": self.target, "invoice": alias})
        self.assertFalse(self.service.reserved)

    def test_symlinked_directory_alias_is_rejected_before_any_publication(self):
        alias = self.target.parent / "alias"
        try:
            alias.symlink_to(self.target.parent, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Cannot create directory symlink: {exc}")
        with self.assertRaises(ValueError):
            export_documents(self.service, 5, {"act": self.target, "invoice": alias / "act.pdf"})
        self.assertFalse(self.target.exists())
        self.assertFalse(self.service.reserved)

    def test_symlinked_file_alias_is_rejected_before_any_publication(self):
        self.target.write_bytes(b"valid old")
        alias = self.target.parent / "alias.pdf"
        try:
            alias.symlink_to(self.target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Cannot create file symlink: {exc}")
        with self.assertRaises(ValueError):
            export_documents(self.service, 5, {"act": self.target, "invoice": alias}, overwrite=True)
        self.assertEqual(self.target.read_bytes(), b"valid old")
        self.assertFalse(self.service.reserved)

    def test_hardlinks_to_same_existing_file_rejected(self):
        self.target.write_bytes(b"valid old")
        alias = self.target.parent / "alias.pdf"
        try:
            __import__("os").link(self.target, alias)
        except OSError as exc:
            self.skipTest(f"Hard links unsupported here: {exc}")
        with self.assertRaises(ValueError):
            export_documents(self.service, 5, {"act": self.target, "invoice": alias}, overwrite=True)
        self.assertFalse(self.service.reserved)

    def test_case_variants_match_actual_filesystem_rules(self):
        other = self.target.with_name("ACT.PDF")
        from app.documents.export import _case_sensitive_directory
        sensitive = _case_sensitive_directory(self.target.parent)
        if sensitive:
            result = export_documents(self.service, 5, {"act": self.target, "invoice": other})
            self.assertEqual(set(result.successful), {"act", "invoice"})
            self.assertTrue(self.target.exists())
            self.assertTrue(other.exists())
        else:
            with self.assertRaises(ValueError):
                export_documents(self.service, 5, {"act": self.target, "invoice": other})
            self.assertFalse(self.service.reserved)

    def test_distinct_paths_do_not_false_positive(self):
        other = self.target.with_name("other.pdf")
        result = export_documents(self.service, 5, {"act": self.target, "invoice": other})
        self.assertEqual(set(result.successful), {"act", "invoice"})

    @unittest.skipUnless(__import__("os").name == "nt", "junction is a Windows filesystem feature")
    def test_windows_junction_directory_alias(self):
        import subprocess
        junction = self.target.parent / "junction"
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(self.target.parent)],
                              capture_output=True, text=True)
        if proc.returncode:
            self.skipTest("Junction creation unavailable in test environment")
        try:
            with self.assertRaises(ValueError):
                export_documents(self.service, 5, {"act": self.target, "invoice": junction / "act.pdf"})
            self.assertFalse(self.service.reserved)
        finally:
            junction.rmdir()


class ReviewRenderRegressionTests(unittest.TestCase):
    def test_very_long_real_table_cells_split_without_losing_text(self):
        for kind, renderer, fields in (
            ("act", render_act, [("tenant", "legal_address"), ("tenant", "additional_agreement")]),
            ("invoice", render_invoice, [("performer", "bank_name"), ("performer", "account_number")]),
        ):
            with self.subTest(kind=kind):
                data = snapshot(kind)
                for obj, field in fields:
                    data[obj][field] = ("Длинный тестовый реквизит с переносом\n" * 75
                                        + "КОНЕЦ_" + field)
                pdf = renderer(data)
                reader = PdfReader(io.BytesIO(pdf))
                self.assertGreater(len(reader.pages), 1)
                content = text_of(pdf)
                for _, field in fields:
                    self.assertIn("КОНЕЦ_" + field, content)
                self.assertIn("Итого:", content)
                self.assertIn("Руководитель" if kind == "invoice" else "Заказчик:", content)

    def test_one_giant_word_and_multiline_fields_in_both_generators(self):
        for kind, renderer in (("act", render_act), ("invoice", render_invoice)):
            with self.subTest(kind=kind):
                data = snapshot(kind)
                data["tenant"]["name"] = "МегаСлово" * 300 + " КОНЕЦ_ИМЕНИ"
                data["performer"]["performer_name"] = ("Строка\n" * 145 + " КОНЕЦ_ИСПОЛНИТЕЛЯ")
                pdf = renderer(data)
                content = text_of(pdf)
                self.assertGreater(len(PdfReader(io.BytesIO(pdf)).pages), 1)
                self.assertIn("КОНЕЦ_ИМЕНИ", content)
                self.assertIn("КОНЕЦ_ИСПОЛНИТЕЛЯ", content)


class ReviewImageRegressionTests(unittest.TestCase):
    @staticmethod
    def image_placements(pdf: bytes):
        reader = PdfReader(io.BytesIO(pdf))
        images = []
        for page_index, page in enumerate(reader.pages):
            def visitor(op, operands, ctm, tm):
                if op == b"Do":
                    # ReportLab scales a unit-square image XObject with cm.
                    images.append((page_index, float(ctm[4]), float(ctm[5]),
                                   float(ctm[0]), float(ctm[3])))
            page.extract_text(visitor_operand_before=visitor)
        return reader, images

    def test_signature_stamp_xobject_count_dimensions_and_left_zone(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            sign = Path(folder) / "sign.png"
            stamp = Path(folder) / "stamp.png"
            Image.new("RGB", (240, 60), (150, 80, 50)).save(sign)
            Image.new("RGB", (90, 90), (25, 70, 100)).save(stamp)
            for kind, renderer in (("act", render_act), ("invoice", render_invoice)):
                for active in ((), ("signature_path",), ("stamp_path",),
                               ("signature_path", "stamp_path")):
                    with self.subTest(kind=kind, active=active):
                        data = snapshot(kind)
                        data["performer"]["signature_path"] = str(sign) if "signature_path" in active else None
                        data["performer"]["stamp_path"] = str(stamp) if "stamp_path" in active else None
                        reader, placements = self.image_placements(renderer(data))
                        self.assertEqual(len(placements), len(active))
                        self.assertEqual(len(reader.pages[0].images), len(active))
                        page = reader.pages[0]
                        limit = 56 + (float(page.mediabox.width) - 112) * (.52 if kind == "act" else .54)
                        for _, x, y, w, h in placements:
                            self.assertGreater(w, 0)
                            self.assertGreater(h, 0)
                            self.assertGreaterEqual(x, 56)
                            self.assertGreaterEqual(y, 0)
                            self.assertLessEqual(x + w, limit)  # Not in customer/accountant region.
                            self.assertLessEqual(y + h, float(page.mediabox.height))
                            aspect = w / h
                            self.assertTrue(abs(aspect - 4.0) < .05 or abs(aspect - 1.0) < .05,
                                            f"Distorted image: {aspect}")

    def test_corrupt_image_preflight_leaves_no_images_or_partial_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            corrupt = Path(folder) / "broken.png"
            corrupt.write_bytes(b"garbage")
            for kind, renderer in (("act", render_act), ("invoice", render_invoice)):
                data = snapshot(kind)
                data["performer"]["stamp_path"] = str(corrupt)
                with self.assertRaises(DocumentRenderError):
                    renderer(data)



class ExtraReviewReliabilityTests(unittest.TestCase):
    def setUp(self):
        ExportTests.setUp(self)

    def test_symlink_retarget_between_exports_blocks_second_publish(self):
        first_dir = self.target.parent / "first"
        second_dir = self.target.parent / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        first_target = first_dir / "document.pdf"
        link_dir = self.target.parent / "aliasdir"
        try:
            link_dir.symlink_to(second_dir, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Directory symlink unsupported: {exc}")
        second_target = link_dir / "document.pdf"
        original_mark = self.service.mark_document_generated
        def replace_alias_after_first(document_id, *, expected_revision):
            original_mark(document_id, expected_revision=expected_revision)
            if document_id == 1:
                link_dir.unlink()
                link_dir.symlink_to(first_dir, target_is_directory=True)
        self.service.mark_document_generated = replace_alias_after_first
        result = export_documents(self.service, 5,
                                  {"act": first_target, "invoice": second_target})
        self.assertEqual(set(result.successful), {"act"})
        self.assertEqual(set(result.failed), {"invoice"})
        self.assertTrue(first_target.exists())
        self.assertFalse((second_dir / "document.pdf").exists())
        self.assertEqual(self.service.marked, [1])

    def test_stale_rollback_does_not_delete_other_process_file(self):
        from app.documents import export as exp
        self.service.fail_mark = True
        old_mark = self.service.mark_document_generated
        def replace_at_mark(document_id, *, expected_revision):
            self.target.unlink()
            self.target.write_bytes(b"foreign replacement")
            return old_mark(document_id, expected_revision=expected_revision)
        self.service.mark_document_generated = replace_at_mark
        with self.assertRaises(ExportError) as ctx:
            export_document(self.service, 5, "act", self.target)
        self.assertTrue(ctx.exception.recovery_failed)
        self.assertEqual(self.target.read_bytes(), b"foreign replacement")
        self.assertFalse(self.service.marked)

    def test_existing_symlink_target_is_not_replaced_by_single_export(self):
        original = self.target.parent / "original.pdf"
        original.write_bytes(b"original")
        try:
            self.target.symlink_to(original)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(str(exc))
        with self.assertRaises(ValueError):
            export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertEqual(original.read_bytes(), b"original")
        self.assertFalse(self.service.reserved)


class ReviewZeroPolicyTests(unittest.TestCase):
    def setUp(self):
        RealServiceIntegrationTests.setUp(self)

    def path(self, name):
        return RealServiceIntegrationTests.path(self, name)

    # Existing behavior is preserved intentionally pending PRD clarification.
    def test_nonzero_consumption_manual_zero_amount_does_not_require_confirmation(self):
        self.service.update_calculation(self.calc["id"], total_amount="0.00", confirm_manual=True)
        self.assertTrue(self.service.validate(self.calc["id"]).can_export())
        export_document(self.service, self.calc["id"], "invoice", self.path("zero-manual"))
        self.assertIn("0.00", text_of(self.path("zero-manual").read_bytes()))

    def test_zero_consumption_manual_nonzero_amount_still_requires_confirmation(self):
        from app.core.service import ConfirmationRequired
        self.service.set_tariff(2026, 11, "7")
        zero = self.service.create_calculation(self.tenant["id"], 2026, 11,
                                               start_reading="10", end_reading="10")
        self.service.update_calculation(zero["id"], total_amount="75.00", confirm_manual=True)
        checked = self.service.validate(zero["id"])
        self.assertFalse(checked.can_export())
        self.assertTrue(checked.can_export(confirm_zero=True))
        with self.assertRaises(ConfirmationRequired):
            self.service.reserve_document(zero["id"], "act")
        with self.assertRaises(ConfirmationRequired):
            self.service.prepare_document(zero["id"], "act")
        export_document(self.service, zero["id"], "act", self.path("zero-nonzero"),
                        confirm_zero=True)
        self.assertIn("75-00", text_of(self.path("zero-nonzero").read_bytes()))


if __name__ == "__main__":
    unittest.main()
