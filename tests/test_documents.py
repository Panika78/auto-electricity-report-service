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
                      "100,125", "109,625", "9,500", "11,0704", "105,17", "кВт·ч",
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
                      "30.10.2026", "9,500", "11,0704", "105,17", "кВт·ч", "Сто пять рублей",
                      "В том числе НДС:", "0,00", "Бухгалтер", "Руководитель"):
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
            self.assertIn("1,250", text)
            self.assertIn("88,42", text)
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



class ActHeaderLayoutTests(unittest.TestCase):
    """The act uses balanced, borderless party columns with three text levels."""

    def test_party_labels_align_and_names_use_bolder_larger_type(self):
        page = PdfReader(io.BytesIO(render_act(snapshot("act")))).pages[0]
        fragments = []

        def collect(text, cm, tm, font, size):
            if text.strip():
                fragments.append((text.strip(), round(float(size), 1), float(cm[4] + tm[4]),
                                  str(font.get("/BaseFont", "")) if font else ""))

        page.extract_text(visitor_text=collect)
        left = next(f for f in fragments if f[0] == "Исполнитель:" and f[1] == 8.4)
        right = next(f for f in fragments if f[0] == "Заказчик:" and f[1] == 8.4)
        self.assertAlmostEqual(left[2], 56, delta=2)
        self.assertAlmostEqual(right[2], 308, delta=3)
        self.assertGreater(right[2] - left[2], 235)  # Consistent ~20 pt gutter.
        for party_name in ("ИП Елена Вымышленная", "ООО «Лаборатория & Ко»"):
            name = next(f for f in fragments if f[0] == party_name)
            self.assertAlmostEqual(name[1], 10.1, delta=.1)
            self.assertIn("Bold", name[3])
        address = next(f for f in fragments if f[0].startswith("г. Тестоград"))
        self.assertAlmostEqual(address[1], 9.0, delta=.1)
        self.assertNotIn("Bold", address[3])

    def test_different_lengths_missing_fields_and_html_escaping(self):
        values = snapshot("act")
        values["tenant"]["name"] = "ООО <Проверка> & «Лаборатория» " * 7
        values["tenant"]["legal_address"] = "г. Тестовый, улица Большая\\nстроение 11 & <корпус>"
        values["performer"]["performer_name"] = "ИП Краткая"
        values["performer"].update(address=None, inn="")
        pdf = render_act(values)
        reader = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 1)
        text = text_of(pdf)
        self.assertIn("<Проверка> & «Лаборатория»", text)
        self.assertIn("<корпус>", text)
        self.assertNotIn("ИНН 000000000000", text)
        self.assertIn("Исполнитель:", text)
        self.assertIn("Заказчик:", text)
        self.assertIn("АКТ № 15", text)
        self.assertIn("Сумма НДС:", text)
        self.assertIn("Заказчик претензий", text)

    def test_both_multi_page_party_columns_preserve_last_lines(self):
        values = snapshot("act")
        values["performer"]["performer_name"] = "Индивидуальный предприниматель " * 22
        values["performer"]["address"] = ("Адрес исполнителя\\n" * 96) + "КОНЕЦ_ИСПОЛНИТЕЛЯ"
        values["tenant"]["name"] = ("Синтетическое юридическое лицо " * 130) + "КОНЕЦ_НАЗВАНИЯ"
        values["tenant"]["legal_address"] = ("Адрес заказчика\\n" * 105) + "КОНЕЦ_ЗАКАЗЧИКА"
        document = render_act(values)
        self.assertGreater(len(PdfReader(io.BytesIO(document)).pages), 1)
        text = text_of(document)
        for part in ("КОНЕЦ_ИСПОЛНИТЕЛЯ", "КОНЕЦ_НАЗВАНИЯ",
                     "КОНЕЦ_ЗАКАЗЧИКА", "Итого:", "Заказчик:"):
            self.assertIn(part, text)


class DocumentVisualStyleTests(unittest.TestCase):
    """Shared typography/formatting; retained original document texts."""

    def test_shared_font_family_a4_and_no_foreign_fonts(self):
        from reportlab.lib.pagesizes import A4
        for kind, render in (("act", render_act), ("invoice", render_invoice)):
            with self.subTest(kind=kind):
                page = PdfReader(io.BytesIO(render(snapshot(kind)))).pages[0]
                self.assertAlmostEqual(float(page.mediabox.width), A4[0], delta=.1)
                self.assertAlmostEqual(float(page.mediabox.height), A4[1], delta=.1)
                resources = page["/Resources"]["/Font"].get_object()
                used = {str(obj.get_object()["/BaseFont"]) for obj in resources.values()
                        if "PTSans-" in str(obj.get_object()["/BaseFont"])
                        or "PTSerif-" in str(obj.get_object()["/BaseFont"])}
                self.assertTrue(used, "Bundled font was not used")
                self.assertTrue(all("PTSans-" in name for name in used), used)
                self.assertTrue(any("Bold" in name for name in used), used)

    def test_common_page_dimensions_and_multiline_content(self):
        # ReportLab uses nested text matrices that make generic pypdf visitor
        # coordinates unreliable; do not claim this is an overlap detector.
        from reportlab.lib.pagesizes import A4
        for kind, render in (("act", render_act), ("invoice", render_invoice)):
            with self.subTest(kind=kind):
                data = snapshot(kind)
                data["tenant"]["name"] = "ООО Длинное учебное название " * 13
                data["performer"]["address"] = ("Учебный реквизит\\n" * 5).rstrip()
                pdf = PdfReader(io.BytesIO(render(data)))
                self.assertGreaterEqual(len(pdf.pages), 1)
                content = " ".join(" ".join(page.extract_text() for page in pdf.pages).split())
                self.assertIn("ООО Длинное учебное название", content)
                # The source invoice has no address block; only the act has it.
                if kind == "act":
                    self.assertIn("Учебный реквизит", content)
                else:
                    self.assertNotIn("Учебный реквизит", content)
                for page in pdf.pages:
                    self.assertAlmostEqual(float(page.mediabox.width), A4[0], delta=.1)
                    self.assertAlmostEqual(float(page.mediabox.height), A4[1], delta=.1)

    def test_exact_decimal_presentation_and_grouping(self):
        from app.documents.common import quantity, money, act_money, invoice_money
        from app.documents.common import DocumentRenderError
        cases = (
            (D("9.500"), "9,500"),
            (D("11.0704"), "11,0704"),
            (D("1000000.000"), "1 000 000,000"),
            (D("0.000"), "0,000"),
            (D("1234567.0080"), "1 234 567,0080"),
        )
        for value, expected in cases:
            with self.subTest(value=str(value)):
                self.assertEqual(quantity(value), expected)
                self.assertEqual(D(quantity(value).replace(" ", "").replace(",", ".")), value)
        for func in (money, act_money, invoice_money):
            self.assertEqual(func(D("105.17")), "105,17")
            self.assertEqual(func(D("1234567.8")), "1 234 567,80")
            self.assertEqual(func(D("0")), "0,00")
            with self.assertRaises(DocumentRenderError):
                func(D("4.001"))  # No silent change of the financial value.

    def test_both_documents_have_identical_numeric_and_unit_conventions(self):
        for kind, render in (("act", render_act), ("invoice", render_invoice)):
            with self.subTest(kind=kind):
                extracted = text_of(render(snapshot(kind)))
                for token in ("9,500", "11,0704", "105,17", "кВт·ч", "0,00"):
                    self.assertIn(token, extracted)
                for forbidden in ("9.500", "11.0704", "105.17", "кВт.ч.", "0-00"):
                    self.assertNotIn(forbidden, extracted)
                self.assertNotRegex(extracted, r"(?<!\\S)кВт(?!·ч)")
                self.assertIn("Сумма НДС:" if kind == "act" else "В том числе НДС:", extracted)

    def test_invoice_removes_only_product_delivery_warning(self):
        invoice = text_of(render_invoice(snapshot("invoice")))
        for removed in ("Внимание!", "условиями поставки товара", "наличие товара на складе",
                        "самовывозом", "доверенности и паспорта"):
            self.assertNotIn(removed, invoice)
        for required in ("Банк получателя", "Получатель", "Поставщик:", "Покупатель:",
                         "Счет на оплату", "Товары (работы, услуги)", "Всего к оплате:",
                         "Руководитель", "Бухгалтер"):
            self.assertIn(required, invoice)

    def test_original_wordings_and_sequence_remain(self):
        act = " ".join(text_of(render_act(snapshot("act"))).split())
        invoice = " ".join(text_of(render_invoice(snapshot("invoice"))).split())
        for phrase in ("Возмещение расходов за потребленную электроэнергию",
                       "Вышеперечисленные услуги выполнены полностью и в срок.",
                       "Заказчик претензий по объёму, качеству и срокам оказания услуг не имеет."):
            self.assertIn(phrase, act)
        for phrase in ("Возмещение расходов за потребление эл.энергии",
                       "Всего наименований один, на сумму:", "В том числе НДС:",
                       "Руководитель", "Бухгалтер"):
            self.assertIn(phrase, invoice)
        self.assertLess(invoice.index("Банк получателя"), invoice.index("Счет на оплату"))
        self.assertLess(invoice.index("Покупатель:"), invoice.index("Итого:"))
        self.assertLess(act.index("Исполнитель:"), act.index("АКТ №"))
        self.assertLess(act.index("АКТ №"), act.index("Итого:"))

    def test_large_amounts_and_multi_page_fields_preserve_last_tokens(self):
        for kind, render in (("act", render_act), ("invoice", render_invoice)):
            with self.subTest(kind=kind):
                data = snapshot(kind)
                data["performer"]["performer_name"] = ("Строка синтетического реквизита\n" * 90
                                                       + "КОНЕЦ_РЕКВИЗИТА")
                data["tenant"]["name"] = ("ОченьДлинноеНазвание " * 180
                                         + "КОНЕЦ_АРЕНДАТОРА")
                data["calculation"]["consumption"] = D("1000000.000")
                data["calculation"]["total_amount"] = D("1234567.80")
                data["amount_in_words"] = "Один миллион двести тридцать четыре тысячи рублей 80 копеек"
                pdf = render(data)
                pages = PdfReader(io.BytesIO(pdf)).pages
                self.assertGreater(len(pages), 1)
                extracted = " ".join(text_of(pdf).split())
                for token in ("КОНЕЦ_РЕКВИЗИТА", "КОНЕЦ_АРЕНДАТОРА",
                              "1 000 000,000", "1 234 567,80", "кВт·ч",
                              "Итого:"):
                    self.assertIn(token, extracted)

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
        self.assertIn("11,0704", text_of(self.path("b").read_bytes()))

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
        self.assertIn("21,625", pdf_text)
        self.assertEqual(self.service.document_status(self.calc["id"], "act"), "current")
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("select count(*) from documents").fetchone()[0], 1)

    def test_manual_amount_and_consumption(self):
        self.service.update_calculation(self.calc["id"], consumption="8.000", confirm_manual=True)
        self.service.update_calculation(self.calc["id"], total_amount="46.02", confirm_manual=True)
        export_document(self.service, self.calc["id"], "invoice", self.path("i"))
        invoice = text_of(self.path("i").read_bytes())
        self.assertIn("8,000", invoice)
        self.assertIn("46,02", invoice)

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
        from app.documents import export as exp
        publish = exp._publish_new_file

        def fail_only_restore(source, destination):
            if Path(source).name.startswith(".kontakt-backup-"):
                raise PermissionError("restore denied")
            return publish(source, destination)

        with patch("app.documents.export._publish_new_file", side_effect=fail_only_restore):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        err = ctx.exception
        self.assertTrue(err.recovery_failed)
        self.assertIsInstance(err.__cause__, ValueError)
        self.assertIsInstance(err.recovery_error, OSError)
        self.assertIsInstance(err.recovery_error.__cause__, PermissionError)
        self.assertIsNotNone(err.quarantine_path)
        self.assertTrue(err.quarantine_path.exists())
        self.assertIsNotNone(err.backup_path)
        self.assertTrue(err.backup_path.exists())
        self.assertEqual(err.backup_path.read_bytes(), b"Previous valid PDF")
        self.assertEqual(self.service.marked, [])
        self.assertIn("резервная копия", str(err))
        self.assertEqual(err.path, self.target)
        # Explicit cleanup in a test with entirely synthetic bytes.
        err.backup_path.unlink()
        err.quarantine_path.unlink()

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
        self.assertEqual(err.partial_path, self.target)
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



class SecondReviewExportRegressionTests(unittest.TestCase):
    """R-08/R-09 reproduce publication failures using synthetic bytes only."""

    def setUp(self):
        ExportTests.setUp(self)

    def test_r08_identity_failure_after_publication_keeps_backup(self):
        from app.documents import export as exp
        self.target.write_bytes(b"OLD")
        real_identity = exp._identity

        def stat_failure(path):
            if Path(path) == self.target and self.target.read_bytes().startswith(b"%PDF-"):
                raise PermissionError("simulated stat failure after publish")
            return real_identity(path)

        with patch.object(exp, "_identity", side_effect=stat_failure):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        err = ctx.exception
        self.assertTrue(err.recovery_failed)
        self.assertIsInstance(err.__cause__, PermissionError)
        self.assertIsNotNone(err.recovery_error)
        self.assertEqual(err.path, self.target)
        self.assertIsNotNone(err.backup_path)
        self.assertEqual(err.backup_path.read_bytes(), b"OLD")
        self.assertTrue(self.target.read_bytes().startswith(b"%PDF-"))
        self.assertFalse(self.service.marked)

    def test_r08_foreign_replacement_during_identity_failure_is_untouched(self):
        import os
        from app.documents import export as exp
        self.target.write_bytes(b"OLD")
        identity = exp._identity
        foreign = self.target.with_name("foreign.pdf")
        replaced = False

        def fail_after_external_replacement(path):
            nonlocal replaced
            if Path(path) == self.target and not replaced and self.target.read_bytes().startswith(b"%PDF-"):
                foreign.write_bytes(b"FOREIGN")
                os.replace(foreign, self.target)
                replaced = True
                raise PermissionError("stat failure after a foreign rename")
            return identity(path)

        with patch.object(exp, "_identity", side_effect=fail_after_external_replacement):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertTrue(replaced)
        self.assertEqual(self.target.read_bytes(), b"FOREIGN")
        self.assertTrue(ctx.exception.recovery_failed)
        self.assertEqual(ctx.exception.backup_path.read_bytes(), b"OLD")
        self.assertFalse(self.service.marked)

    def test_r08_unchanged_target_restores_old_on_mark_conflict(self):
        self.target.write_bytes(b"OLD")
        self.service.fail_mark = True
        with self.assertRaises(ExportError) as ctx:
            export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertIsInstance(ctx.exception.__cause__, ValueError)
        self.assertFalse(ctx.exception.recovery_failed)
        self.assertEqual(self.target.read_bytes(), b"OLD")
        self.assertEqual(list(self.target.parent.glob(".kontakt-backup-*.pdf")), [])
        self.assertEqual(list(self.target.parent.glob(".kontakt-rollback-*.pdf")), [])
        self.assertFalse(self.service.marked)

    def test_r08_failed_restore_preserves_backup_and_quarantine(self):
        from app.documents import export as exp
        self.target.write_bytes(b"OLD")
        self.service.fail_mark = True
        publish = exp._publish_new_file

        def deny_restoring_backup(source, target):
            if Path(source).name.startswith(".kontakt-backup-"):
                raise PermissionError("restore denied")
            return publish(source, target)

        with patch.object(exp, "_publish_new_file", side_effect=deny_restoring_backup):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        err = ctx.exception
        self.assertTrue(err.recovery_failed)
        self.assertIsInstance(err.__cause__, ValueError)
        self.assertIsInstance(err.recovery_error.__cause__, PermissionError)
        self.assertEqual(err.backup_path.read_bytes(), b"OLD")
        self.assertTrue(err.quarantine_path.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(err.path, self.target)
        self.assertFalse(self.service.marked)

    def test_r08_external_rename_between_check_and_quarantine_is_preserved(self):
        import os
        from app.documents import export as exp
        self.target.write_bytes(b"OLD")
        self.service.fail_mark = True
        foreign = self.target.with_name("external.pdf")
        original_replace = os.replace
        raced = False

        def replace_at_quarantine(source, destination):
            nonlocal raced
            if Path(source) == self.target and Path(destination).name.startswith(".kontakt-rollback-") and not raced:
                foreign.write_bytes(b"FOREIGN")
                original_replace(foreign, self.target)
                raced = True
            return original_replace(source, destination)

        with patch.object(exp.os, "replace", side_effect=replace_at_quarantine):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertTrue(raced)
        self.assertTrue(ctx.exception.recovery_failed)
        self.assertEqual(self.target.read_bytes(), b"FOREIGN")
        self.assertEqual(ctx.exception.backup_path.read_bytes(), b"OLD")
        self.assertFalse(self.service.marked)

    def test_r08_foreign_creator_during_restore_cannot_be_overwritten(self):
        from app.documents import export as exp
        self.target.write_bytes(b"OLD")
        self.service.fail_mark = True
        publish = exp._publish_new_file

        def create_foreign_before_restore(source, target):
            if Path(source).name.startswith(".kontakt-backup-"):
                Path(target).write_bytes(b"FOREIGN")
            return publish(source, target)

        with patch.object(exp, "_publish_new_file", side_effect=create_foreign_before_restore):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        err = ctx.exception
        self.assertTrue(err.recovery_failed)
        self.assertEqual(self.target.read_bytes(), b"FOREIGN")
        self.assertEqual(err.backup_path.read_bytes(), b"OLD")
        self.assertIsNotNone(err.quarantine_path)
        self.assertTrue(err.quarantine_path.read_bytes().startswith(b"%PDF-"))
        self.assertFalse(self.service.marked)

    def test_r08_no_prior_pdf_keeps_new_file_when_identity_unknown(self):
        from app.documents import export as exp
        identity = exp._identity

        def fail_published_target(path):
            if Path(path) == self.target and self.target.exists():
                raise PermissionError("identity unavailable")
            return identity(path)

        with patch.object(exp, "_identity", side_effect=fail_published_target):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target)
        self.assertTrue(self.target.read_bytes().startswith(b"%PDF-"))
        self.assertTrue(ctx.exception.recovery_failed)
        self.assertEqual(ctx.exception.path, self.target)
        self.assertIsNone(ctx.exception.backup_path)
        self.assertFalse(self.service.marked)

    def test_r09_successful_export_reports_leftover_staged_file(self):
        unlink = Path.unlink

        def deny_staged(self, *args, **kwargs):
            if self.name.startswith(".kontakt-pdf-"):
                raise PermissionError("stage cleanup denied")
            return unlink(self, *args, **kwargs)

        with patch("app.documents.export.Path.unlink", new=deny_staged):
            result = export_document(self.service, 5, "act", self.target)
        self.assertEqual(self.service.marked, [1])
        self.assertIsNotNone(result.cleanup_warning)
        self.assertIsNotNone(result.leftover_staged)
        self.assertEqual(result.leftover_staged.parent, self.target.parent)
        self.assertTrue(result.leftover_staged.name.startswith(".kontakt-pdf-"))
        self.assertEqual(result.leftover_staged.read_bytes(), self.target.read_bytes())

    def test_r09_successful_backup_cleanup_reports_exact_path(self):
        self.target.write_bytes(b"OLD")
        unlink = Path.unlink

        def deny_backup(self, *args, **kwargs):
            if self.name.startswith(".kontakt-backup-"):
                raise PermissionError("backup cleanup denied")
            return unlink(self, *args, **kwargs)

        with patch("app.documents.export.Path.unlink", new=deny_backup):
            result = export_document(self.service, 5, "act", self.target, overwrite=True)
        self.assertTrue(self.target.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(self.service.marked, [1])
        self.assertIsNotNone(result.cleanup_warning)
        self.assertEqual(result.leftover_backup.read_bytes(), b"OLD")
        self.assertIsNone(result.leftover_staged)

    def test_r09_publish_error_and_staged_cleanup_error_keep_both_causes(self):
        import errno
        unlink = Path.unlink

        def deny_stage(self, *args, **kwargs):
            if self.name.startswith(".kontakt-pdf-"):
                raise PermissionError("stage cleanup denied")
            return unlink(self, *args, **kwargs)

        with patch("app.documents.export.os.link", side_effect=OSError(errno.EACCES, "publish denied")), \
             patch("app.documents.export.Path.unlink", new=deny_stage):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target)
        err = ctx.exception
        self.assertIsInstance(err.__cause__, OSError)
        self.assertEqual(err.__cause__.errno, errno.EACCES)
        self.assertIsInstance(err.cleanup_error, PermissionError)
        self.assertEqual(err.staged_path.parent, self.target.parent)
        self.assertTrue(err.staged_path.read_bytes().startswith(b"%PDF-"))
        self.assertFalse(self.target.exists())
        self.assertFalse(self.service.marked)

    def test_r09_failed_stage_write_and_cleanup_reports_file(self):
        import errno
        unlink = Path.unlink

        def deny_stage(self, *args, **kwargs):
            if self.name.startswith(".kontakt-pdf-"):
                raise PermissionError("stage cleanup denied")
            return unlink(self, *args, **kwargs)

        with patch("app.documents.export.os.fsync", side_effect=OSError(errno.ENOSPC, "disk full")), \
             patch("app.documents.export.Path.unlink", new=deny_stage):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target)
        err = ctx.exception
        self.assertEqual(err.__cause__.errno, errno.ENOSPC)
        self.assertIsInstance(err.cleanup_error, PermissionError)
        self.assertTrue(err.staged_path.exists())
        self.assertFalse(self.target.exists())
        self.assertFalse(self.service.marked)

    def test_r09_failed_backup_write_and_cleanup_reports_file(self):
        import errno
        self.target.write_bytes(b"OLD")
        unlink = Path.unlink

        def partial_backup(source, destination, **kwargs):
            destination.write(b"partial-backup")
            raise OSError(errno.ENOSPC, "disk full")

        def deny_backup(self, *args, **kwargs):
            if self.name.startswith(".kontakt-backup-"):
                raise PermissionError("backup cleanup denied")
            return unlink(self, *args, **kwargs)

        with patch("app.documents.export.shutil.copyfileobj", side_effect=partial_backup), \
             patch("app.documents.export.Path.unlink", new=deny_backup):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        err = ctx.exception
        self.assertEqual(err.__cause__.errno, errno.ENOSPC)
        self.assertIsInstance(err.cleanup_error, PermissionError)
        self.assertEqual(err.backup_path.read_bytes(), b"partial-backup")
        self.assertEqual(self.target.read_bytes(), b"OLD")
        self.assertFalse(self.service.marked)

    def test_r09_interrupt_with_cleanup_failure_keeps_paths_and_original(self):
        unlink = Path.unlink

        def deny_stage(self, *args, **kwargs):
            if self.name.startswith(".kontakt-pdf-"):
                raise PermissionError("stage cleanup denied")
            return unlink(self, *args, **kwargs)

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt("controlled interruption")

        with patch("app.documents.export.Path.unlink", new=deny_stage), \
             patch.object(self.service, "mark_document_generated", side_effect=interrupt):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target)
        err = ctx.exception
        self.assertIsInstance(err.__cause__, KeyboardInterrupt)
        self.assertIsNotNone(err.staged_path)
        self.assertTrue(err.staged_path.exists())
        self.assertFalse(self.target.exists())
        self.assertFalse(self.service.marked)

    def test_r09_replaced_staged_file_is_not_unlinked(self):
        from app.documents import export as exp
        real_publish = exp._publish_new_file

        def replace_stage_before_cleanup(staged, target):
            Path(staged).unlink()
            Path(staged).write_bytes(b"FOREIGN-STAGED")
            raise PermissionError("publication failed")

        with patch.object(exp, "_publish_new_file", side_effect=replace_stage_before_cleanup):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target)
        err = ctx.exception
        self.assertIsInstance(err.__cause__, PermissionError)
        self.assertEqual(err.staged_path.read_bytes(), b"FOREIGN-STAGED")
        self.assertFalse(self.target.exists())
        self.assertFalse(self.service.marked)


class ChecksumRollbackRegressionTests(unittest.TestCase):
    """Spoof unchanged stat metadata while altering the generated PDF bytes."""

    GENERATED = b"%PDF-GENERATED"
    FOREIGN = b"%PDF-USER-EDIT"  # Exactly the same byte length.

    def setUp(self):
        ExportTests.setUp(self)

    def _edit_without_metadata_change(self):
        import os
        from app.documents import export as exp

        self.assertEqual(self.target.read_bytes(), self.GENERATED)
        before = exp._identity(self.target)
        info = self.target.stat()
        self.target.write_bytes(self.FOREIGN)
        os.utime(self.target, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(exp._identity(self.target), before)
        self.assertNotEqual(self.target.read_bytes(), self.GENERATED)

    def test_same_size_and_mtime_edit_before_rollback_preserves_user_pdf(self):
        self.target.write_bytes(b"OLD")
        self.service.fail_mark = True
        original_mark = self.service.mark_document_generated

        def edit_before_conflict(document_id, *, expected_revision):
            self._edit_without_metadata_change()
            return original_mark(document_id, expected_revision=expected_revision)

        self.service.mark_document_generated = edit_before_conflict
        with patch("app.documents.export._RENDERERS",
                   {"act": lambda _: self.GENERATED}):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        error = ctx.exception
        self.assertTrue(error.recovery_failed)
        self.assertIsInstance(error.__cause__, ValueError)
        self.assertIn("SHA-256", str(error.recovery_error))
        self.assertEqual(error.path, self.target)
        self.assertEqual(self.target.read_bytes(), self.FOREIGN)
        self.assertEqual(error.backup_path.read_bytes(), b"OLD")
        self.assertIsNone(error.quarantine_path)
        self.assertEqual(self.service.marked, [])

    def test_same_size_and_mtime_edit_without_backup_is_not_deleted(self):
        self.service.fail_mark = True
        original_mark = self.service.mark_document_generated

        def edit_before_conflict(document_id, *, expected_revision):
            self._edit_without_metadata_change()
            return original_mark(document_id, expected_revision=expected_revision)

        self.service.mark_document_generated = edit_before_conflict
        with patch("app.documents.export._RENDERERS",
                   {"act": lambda _: self.GENERATED}):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target)
        self.assertTrue(ctx.exception.recovery_failed)
        self.assertIsNone(ctx.exception.backup_path)
        self.assertEqual(ctx.exception.path, self.target)
        self.assertEqual(self.target.read_bytes(), self.FOREIGN)
        self.assertEqual(self.service.marked, [])

    def test_same_size_edit_after_precheck_is_detected_in_quarantine(self):
        import os
        from app.documents import export as exp

        self.target.write_bytes(b"OLD")
        self.service.fail_mark = True
        original_replace = os.replace
        raced = False

        def edit_just_before_quarantine(source, destination):
            nonlocal raced
            if (Path(source) == self.target
                    and Path(destination).name.startswith(".kontakt-rollback-")
                    and not raced):
                self._edit_without_metadata_change()
                raced = True
            return original_replace(source, destination)

        with patch("app.documents.export._RENDERERS",
                   {"act": lambda _: self.GENERATED}), \
             patch.object(exp.os, "replace", side_effect=edit_just_before_quarantine):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        error = ctx.exception
        self.assertTrue(raced)
        self.assertTrue(error.recovery_failed)
        self.assertIsInstance(error.__cause__, ValueError)
        self.assertEqual(error.backup_path.read_bytes(), b"OLD")
        # The changed file was moved and returned to its original location
        # using no-clobber publication, not overwritten with the backup.
        self.assertEqual(self.target.read_bytes(), self.FOREIGN)
        self.assertEqual(self.service.marked, [])

    def test_checksum_read_failure_before_rollback_preserves_backup(self):
        from app.documents import export as exp

        self.target.write_bytes(b"OLD")
        self.service.fail_mark = True
        original_digest = exp._sha256_file

        def fail_on_target(path):
            if Path(path) == self.target:
                raise PermissionError("checksum unavailable")
            return original_digest(path)

        with patch.object(exp, "_sha256_file", side_effect=fail_on_target), \
             patch("app.documents.export._RENDERERS",
                   {"act": lambda _: self.GENERATED}):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        error = ctx.exception
        self.assertTrue(error.recovery_failed)
        self.assertIsInstance(error.__cause__, ValueError)
        self.assertIsInstance(error.recovery_error, PermissionError)
        self.assertEqual(self.target.read_bytes(), self.GENERATED)
        self.assertEqual(error.backup_path.read_bytes(), b"OLD")
        self.assertEqual(self.service.marked, [])


    def test_checksum_read_failure_after_move_preserves_quarantine_and_backup(self):
        from app.documents import export as exp

        self.target.write_bytes(b"OLD")
        self.service.fail_mark = True
        real_digest = exp._sha256_file

        def deny_quarantine_read(path):
            if Path(path).name.startswith(".kontakt-rollback-"):
                raise PermissionError("quarantined PDF cannot be read")
            return real_digest(path)

        with patch.object(exp, "_sha256_file", side_effect=deny_quarantine_read), \
             patch("app.documents.export._RENDERERS",
                   {"act": lambda _: self.GENERATED}):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True)
        err = ctx.exception
        self.assertTrue(err.recovery_failed)
        self.assertIsInstance(err.__cause__, ValueError)
        self.assertIsInstance(err.recovery_error.__cause__, PermissionError)
        self.assertEqual(err.backup_path.read_bytes(), b"OLD")
        self.assertEqual(err.quarantine_path.read_bytes(), self.GENERATED)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.service.marked, [])

    def test_staged_file_edit_does_not_become_trusted_baseline(self):
        import os

        self.target.write_bytes(b"OLD")
        self.service.fail_mark = True

        def corrupt_stage_before_publication():
            staged = list(self.target.parent.glob(".kontakt-pdf-*.tmp"))
            self.assertEqual(len(staged), 1)
            path = staged[0]
            info = path.stat()
            self.assertEqual(path.read_bytes(), self.GENERATED)
            path.write_bytes(self.FOREIGN)
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

        with patch("app.documents.export._RENDERERS",
                   {"act": lambda _: self.GENERATED}):
            with self.assertRaises(ExportError) as ctx:
                export_document(self.service, 5, "act", self.target, overwrite=True,
                                _pre_publish=corrupt_stage_before_publication)
        err = ctx.exception
        self.assertTrue(err.recovery_failed)
        self.assertEqual(self.target.read_bytes(), self.FOREIGN)
        self.assertEqual(err.backup_path.read_bytes(), b"OLD")
        self.assertIsInstance(err.__cause__, ValueError)
        self.assertEqual(self.service.marked, [])


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
        self.assertIn("0,00", text_of(self.path("zero-manual").read_bytes()))

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
        self.assertIn("75,00", text_of(self.path("zero-nonzero").read_bytes()))


if __name__ == "__main__":
    unittest.main()
