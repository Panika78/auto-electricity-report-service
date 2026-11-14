"""Excel-reference invoice layout; intentionally distinct from the act."""
from __future__ import annotations

from io import BytesIO
from typing import Any, Mapping

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, HRFlowable

from .common import (BODY_LEADING, BODY_SIZE, HEADING_LEADING, HEADING_SIZE,
                     PAGE_BOTTOM, PAGE_LEFT, PAGE_RIGHT, PAGE_TOP, PDF_FONT,
                     SMALL_LEADING, SMALL_SIZE, TABLE_BORDER, TABLE_PADDING,
                     Signature, check_images, invoice_money, paragraph, quantity,
                     register_fonts, required_snapshot, russian_date, style)


def render_invoice(snapshot: Mapping[str, Any]) -> bytes:
    calc, tariff, tenant, performer, document = required_snapshot(snapshot)
    if document["type"] != "invoice":
        raise ValueError("Снимок принадлежит не счёту.")
    register_fonts()
    check_images(performer)
    body = style("invoice_body", PDF_FONT, BODY_SIZE, leading=BODY_LEADING)
    small = style("invoice_small", PDF_FONT, SMALL_SIZE, leading=SMALL_LEADING)
    heading = style("invoice_header", PDF_FONT, HEADING_SIZE, bold=True,
                    leading=HEADING_LEADING)
    center = style("invoice_center", PDF_FONT, SMALL_SIZE,
                   leading=SMALL_LEADING, align=TA_CENTER)
    bold_right = style("invoice_bright", PDF_FONT, BODY_SIZE, bold=True, align=TA_RIGHT)
    bold = style("invoice_bold", PDF_FONT, BODY_SIZE, bold=True)
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=PAGE_LEFT, rightMargin=PAGE_RIGHT,
                            topMargin=PAGE_TOP, bottomMargin=PAGE_BOTTOM,
                            title="Счёт на оплату", pageCompression=1)
    width = doc.width
    # The upper banking block follows the original spreadsheet, not the act.
    bank = Table([
        [paragraph(performer.get("bank_name", ""), bold), paragraph("БИК", small),
         paragraph(performer.get("bik", ""), small)],
        [paragraph("Банк получателя", small), paragraph("Сч. №", small),
         paragraph(performer.get("correspondent_account", ""), small)],
        [paragraph("ИНН " + str(performer.get("inn", "")) +
                   ("     КПП " + str(performer["kpp"]) if performer.get("kpp") else ""), small),
         paragraph("Сч. №", small), paragraph(performer.get("account_number", ""), small)],
        [paragraph(performer.get("performer_name", ""), body), "", ""],
        [paragraph("Получатель", small), "", ""],
    ], colWidths=[width * .57, width * .095, width * .335], hAlign="LEFT", splitInRow=1)
    bank.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), TABLE_BORDER, colors.black),
        ("SPAN", (0, 3), (0, 3)),
        ("SPAN", (0, 4), (0, 4)),
        ("SPAN", (1, 2), (1, 4)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), TABLE_PADDING),
        ("RIGHTPADDING", (0, 0), (-1, -1), TABLE_PADDING),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    # A single service line; no contract/readings are added to this template.
    service_text = ("Возмещение расходов за потребление эл.энергии "
                    f"за период с {russian_date(calc['period_start'])} "
                    f"по {russian_date(calc['period_end'])}")
    widths = [width * .06, width * .42, width * .14, width * .08, width * .14, width * .16]
    items = Table([
        [paragraph(t, center) for t in
         ("№", "Товары (работы, услуги)", "Кол-во", "Ед.", "Цена", "Сумма")],
        [paragraph("1", center), paragraph(service_text, center),
         paragraph(quantity(calc["consumption"]), center), paragraph("кВт·ч", center),
         paragraph(quantity(tariff["rate"]), center),
         paragraph(invoice_money(calc["total_amount"]), center)],
    ], colWidths=widths, repeatRows=1, splitInRow=1)
    items.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), TABLE_BORDER, colors.black),
        ("BOX", (0, 0), (-1, -1), TABLE_BORDER, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), TABLE_PADDING),
        ("RIGHTPADDING", (0, 0), (-1, -1), TABLE_PADDING),
        ("TOPPADDING", (0, 1), (-1, 1), 10),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
        ("TOPPADDING", (0, 0), (-1, 0), 2),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
    ]))
    totals = Table([
        [paragraph("Итого:", bold_right), paragraph(invoice_money(calc["total_amount"]), bold_right)],
        [paragraph("В том числе НДС:", bold_right), paragraph("0,00", bold_right)],
        [paragraph("Всего к оплате:", bold_right), paragraph(invoice_money(calc["total_amount"]), bold_right)]
    ], colWidths=[width * .82, width * .18])
    totals.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                                ("TOPPADDING", (0, 0), (-1, -1), 1),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    signs = Table([[
        Signature("Руководитель", width * .54, signature_path=performer.get("signature_path"),
                  stamp_path=performer.get("stamp_path"), font=PDF_FONT, size=BODY_SIZE),
        Signature("Бухгалтер", width * .46, font=PDF_FONT, size=BODY_SIZE)
    ]], colWidths=[width * .54, width * .46])
    signs.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story = [
        bank, Spacer(1, 20),
        paragraph(f"Счет на оплату № {document['number']}  от "
                  f"{russian_date(document['document_date'], words=True)}", heading),
        Spacer(1, 7), HRFlowable(width="100%", thickness=TABLE_BORDER, color=colors.black),
        Spacer(1, 12),
        paragraph("Поставщик:     " + str(performer.get("performer_name", "")), body),
        Spacer(1, 18), paragraph("Покупатель:     " + str(tenant["name"]), body),
        Spacer(1, 22), items, Spacer(1, 8), totals,
        paragraph(f"Всего наименований один, на сумму: {snapshot['amount_in_words']}", body),
        Spacer(1, 22), HRFlowable(width="100%", thickness=TABLE_BORDER, color=colors.black),
        Spacer(1, 6), signs,
    ]
    doc.build(story, canvasmaker=_invariant_canvas)
    return buffer.getvalue()


def _invariant_canvas(*args: Any, **kwargs: Any):
    from reportlab.pdfgen.canvas import Canvas
    kwargs["invariant"] = 1
    return Canvas(*args, **kwargs)
