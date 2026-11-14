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
        with patch("app.documents.export.os.link", side_effect=PermissionError("blocked")):
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


if __name__ == "__main__":
    unittest.main()