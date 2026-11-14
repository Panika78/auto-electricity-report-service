"""Pure PDF presentation helpers; no Qt, SQLite or calculation rules."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable, Paragraph
from reportlab.lib.utils import ImageReader

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_FILES = {
    "Act": "PT_Serif-Web-Regular.ttf",
    "ActBold": "PT_Serif-Web-Bold.ttf",
    "Invoice": "PT_Sans-Web-Regular.ttf",
    "InvoiceBold": "PT_Sans-Web-Bold.ttf",
}


class DocumentRenderError(ValueError):
    """Unusable source image or invalid PDF snapshot."""


def register_fonts() -> None:
    for name, filename in FONT_FILES.items():
        if name not in pdfmetrics.getRegisteredFontNames():
            path = FONT_DIR / filename
            if not path.is_file():
                raise DocumentRenderError(f"Не найден шрифт PDF: {filename}.")
            pdfmetrics.registerFont(TTFont(name, str(path)))
    pdfmetrics.registerFontFamily("Act", normal="Act", bold="ActBold")
    pdfmetrics.registerFontFamily("Invoice", normal="Invoice", bold="InvoiceBold")


def style(name: str, font: str, size: float, *, bold: bool = False,
          leading: float | None = None, align: int = TA_LEFT) -> ParagraphStyle:
    return ParagraphStyle(name=name, fontName=font + ("Bold" if bold else ""),
                          fontSize=size, leading=leading or size * 1.22,
                          alignment=align, splitLongWords=1, allowWidows=0,
                          allowOrphans=0, spaceBefore=0, spaceAfter=0)


def ptext(value: Any) -> str:
    """Literal user data in ReportLab paragraphs (never interpreted as markup)."""
    return escape("" if value is None else str(value), quote=True).replace("\n", "<br/>")


def paragraph(value: Any, paragraph_style: ParagraphStyle) -> Paragraph:
    return Paragraph(ptext(value), paragraph_style)


def required_snapshot(snapshot: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    try:
        calc = snapshot["calculation"]
        tariff = snapshot["tariff"]
        tenant = snapshot["tenant"]
        performer = snapshot["performer"]
        document = snapshot["document"]
        if not all((calc, tariff, tenant, performer, document)):
            raise KeyError("empty snapshot section")
        if not isinstance(snapshot["amount_in_words"], str):
            raise KeyError("amount_in_words")
        return calc, tariff, tenant, performer, document
    except (KeyError, TypeError) as exc:
        raise DocumentRenderError("Неполные данные подготовленного документа.") from exc


def russian_date(value: date | str, *, words: bool = False) -> str:
    parsed = value if isinstance(value, date) else date.fromisoformat(str(value))
    if not words:
        return parsed.strftime("%d.%m.%Y")
    months = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")
    return f"{parsed.day} {months[parsed.month - 1]} {parsed.year}"


def quantity(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        raise DocumentRenderError("Количество или тариф не является Decimal.")
    return format(value, "f")


def act_money(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        raise DocumentRenderError("Сумма не является Decimal.")
    return f"{value:.2f}".replace(".", "-")


def invoice_money(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        raise DocumentRenderError("Сумма не является Decimal.")
    return f"{value:,.2f}"


def agreement_text(tenant: Mapping[str, Any]) -> str:
    lines = []
    if tenant.get("additional_agreement"):
        lines.append(str(tenant["additional_agreement"]))
    if tenant.get("contract_number"):
        contract = f"Договор № {tenant['contract_number']}"
        if tenant.get("contract_date"):
            contract += " от " + russian_date(tenant["contract_date"])
        lines.append(contract)
    return "\n".join(lines)


class Signature(Flowable):
    """A signer line with optional independent image overlays."""

    def __init__(self, label: str, width: float, *, signature_path: str | None = None,
                 stamp_path: str | None = None, font: str = "Act", size: float = 10):
        super().__init__()
        self.label = label
        self.width = width
        self.height = 46
        self.signature_path = signature_path
        self.stamp_path = stamp_path
        self.font = font
        self.size = size

    def draw(self) -> None:
        c = self.canv
        c.setFillColor(colors.black)
        c.setFont(self.font, self.size)
        c.drawString(0, 8, self.label)
        x = pdfmetrics.stringWidth(self.label, self.font, self.size) + 5
        c.setLineWidth(0.65)
        c.line(x, 7, self.width - 4, 7)
        if self.signature_path:
            self._draw_image(self.signature_path, x + 10, 2, self.width - x - 23, 35)
        if self.stamp_path:
            self._draw_image(self.stamp_path, x + 12, -8, min(92, self.width - x - 15), 50)

    def _draw_image(self, path: str, x: float, y: float, max_width: float,
                    max_height: float) -> None:
        try:
            image = ImageReader(str(path))
            iw, ih = image.getSize()
            if iw <= 0 or ih <= 0:
                raise ValueError("empty image")
            scale = min(max_width / iw, max_height / ih)
            w, h = iw * scale, ih * scale
            self.canv.drawImage(image, x + (max_width - w) / 2, y + (max_height - h) / 2,
                                width=w, height=h, mask="auto")
        except (OSError, ValueError, TypeError, ZeroDivisionError) as exc:
            raise DocumentRenderError("Не удалось прочитать изображение подписи или печати.") from exc


def check_images(performer: Mapping[str, Any]) -> None:
    # Validate up front, before opening the export destination.
    for key in ("signature_path", "stamp_path"):
        path = performer.get(key)
        if path:
            try:
                image = ImageReader(str(path))
                image.getSize()
                image.getRGBData()  # Forces decoding of truncated or corrupted files.
            except (OSError, ValueError, TypeError) as exc:
                raise DocumentRenderError(f"Недоступно или повреждено изображение: {key}.") from exc