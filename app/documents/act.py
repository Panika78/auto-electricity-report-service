"""Word-reference act layout, dynamically populated from a service snapshot."""
from __future__ import annotations

from io import BytesIO
from typing import Any, Mapping

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .common import (BODY_LEADING, BODY_SIZE, HEADING_LEADING, HEADING_SIZE, PAGE_BOTTOM,
                     PAGE_LEFT, PAGE_RIGHT, PAGE_TOP, PDF_FONT, SMALL_LEADING,
                     SMALL_SIZE, TABLE_BORDER, TABLE_PADDING, Signature, act_money,
                     agreement_text, check_images, paragraph, quantity, register_fonts,
                     required_snapshot, russian_date, style)


def render_act(snapshot: Mapping[str, Any]) -> bytes:
    """Return a complete PDF; never query or mutate storage."""
    calc, tariff, tenant, performer, document = required_snapshot(snapshot)
    if document["type"] != "act":
        raise ValueError("Снимок принадлежит не акту.")
    register_fonts()
    check_images(performer)
    body = style("act_body", PDF_FONT, BODY_SIZE, leading=BODY_LEADING)
    small = style("act_small", PDF_FONT, SMALL_SIZE, leading=SMALL_LEADING)
    head = style("act_heading", PDF_FONT, HEADING_SIZE, bold=True,
                 leading=HEADING_LEADING, align=TA_CENTER)
    bold_right = style("act_total", PDF_FONT, BODY_SIZE, bold=True, align=TA_RIGHT)
    right = style("act_right", PDF_FONT, BODY_SIZE, align=TA_RIGHT)
    center = style("act_center", PDF_FONT, SMALL_SIZE, align=TA_CENTER)
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=PAGE_LEFT, rightMargin=PAGE_RIGHT,
                            topMargin=PAGE_TOP, bottomMargin=PAGE_BOTTOM,
                            title="Акт", pageCompression=1)
    width = doc.width

    performer_info = "\n".join(filter(None, ("Исполнитель:", performer.get("performer_name"),
                          performer.get("address"),
                          "ИНН " + performer["inn"] if performer.get("inn") else "")))
    tenant_info = "\n".join(filter(None, ("Заказчик:", tenant.get("name"),
                         tenant.get("legal_address"))))
    parties = Table([[paragraph(performer_info, body), paragraph(tenant_info, body)]],
                    colWidths=[width * .65, width * .35], splitInRow=1)
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                                  ("TOPPADDING", (0, 0), (-1, -1), 0)]))

    agreement = agreement_text(tenant)
    item = ("Возмещение расходов за потребленную электроэнергию за период\n"
            f"с {russian_date(calc['period_start'])} по {russian_date(calc['period_end'])}\n"
            "(Показания счётчика на\n"
            f"{russian_date(calc['period_start'])}: {quantity(calc['start_reading'])} кВт·ч\n"
            f"{russian_date(calc['period_end'])}: {quantity(calc['end_reading'])} кВт·ч)")
    if agreement:
        item += "\n" + agreement
    widths = [width * .07, width * .39, width * .13, width * .16, width * .11, width * .14]
    table = Table([
        [paragraph(s, center) for s in ("№", "Наименование работ, услуг", "Ед. изм.", "Кол-во", "Цена", "Сумма")],
        [paragraph("1.", small), paragraph(item, body), paragraph("кВт·ч", small),
         paragraph(quantity(calc["consumption"]), right),
         paragraph(quantity(tariff["rate"]), right),
         paragraph(act_money(calc["total_amount"]), right)]
    ], colWidths=widths, hAlign="LEFT", repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), TABLE_BORDER, colors.black),
        ("LINEBELOW", (0, 0), (-1, 0), TABLE_BORDER, colors.black),
        ("LINEBELOW", (0, 1), (-1, 1), TABLE_BORDER, colors.black),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("VALIGN", (0, 1), (-1, 1), "BOTTOM"),
        ("VALIGN", (0, 1), (0, 1), "TOP"),
        ("TOPPADDING", (0, 1), (-1, 1), 10),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 16),
        ("LEFTPADDING", (0, 0), (-1, -1), TABLE_PADDING),
        ("RIGHTPADDING", (0, 0), (-1, -1), TABLE_PADDING),
    ]))
    totals = Table([[paragraph("Итого:", bold_right), paragraph(act_money(calc["total_amount"]), bold_right)],
                    [paragraph("Сумма НДС:", right), paragraph("0,00", right)]],
                   colWidths=[width * .84, width * .16])
    totals.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                                ("TOPPADDING", (0, 0), (-1, -1), 2),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    signatures = Table([[
        Signature("Исполнитель:", width * .48, signature_path=performer.get("signature_path"),
                  stamp_path=performer.get("stamp_path"), font=PDF_FONT, size=BODY_SIZE),
        Signature("Заказчик:", width * .48, font=PDF_FONT, size=BODY_SIZE)
    ]], colWidths=[width * .52, width * .48])
    signatures.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                    ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story = [
        parties, Spacer(1, 40),
        paragraph(f"АКТ № {document['number']} от {russian_date(document['document_date'])}г.", head),
        Spacer(1, 42), table, totals, Spacer(1, 20),
        paragraph(f"Всего оказано услуг 1, на сумму {act_money(calc['total_amount'])} "
                  f"({snapshot['amount_in_words']}).", body),
        Spacer(1, 20),
        paragraph("Вышеперечисленные услуги выполнены полностью и в срок. "
                  "Заказчик претензий по объёму, качеству и срокам оказания услуг не имеет.", body),
        Spacer(1, 42), signatures,
    ]
    doc.build(story, canvasmaker=_invariant_canvas)
    return buffer.getvalue()


def _invariant_canvas(*args: Any, **kwargs: Any):
    from reportlab.pdfgen.canvas import Canvas
    kwargs["invariant"] = 1
    return Canvas(*args, **kwargs)
