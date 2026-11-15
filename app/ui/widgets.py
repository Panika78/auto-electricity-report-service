"""Presentation-only native widgets for the Kontakt visual language.

No database queries or calculation rules belong in this module.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QStyledItemDelegate,
    QStyle, QVBoxLayout, QWidget, QSizePolicy,
)

META_ROLE = int(Qt.ItemDataRole.UserRole) + 1
TONE_ROLE = int(Qt.ItemDataRole.UserRole) + 2

RU_MONTHS = (
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)


def month_label(year: int, month: int) -> str:
    return f"{RU_MONTHS[month]} {year}"


def russian_date(iso: str | None) -> str:
    """Display an ISO calendar date without changing the stored value."""
    if not iso:
        return "—"
    parts = str(iso).split("T", 1)[0].split("-")
    if len(parts) == 3 and all(x.isdigit() for x in parts):
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    return str(iso)


def display_number(value, places: int = 3, suffix: str = "") -> str:
    if value is None or value == "":
        return "—"
    try:
        number = Decimal(str(value))
        rendered = f"{number:,.{places}f}".replace(",", "\u202f").replace(".", ",")
        return rendered + (f" {suffix}" if suffix else "")
    except (InvalidOperation, ValueError):
        return str(value)


class MetricCard(QFrame):
    """One real-data dashboard metric; visual only."""
    def __init__(self, label: str, detail: str, tag: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 15, 18, 15)
        outer.setSpacing(7)
        top = QHBoxLayout()
        title = QLabel(label)
        title.setObjectName("mutedLabel")
        title.setWordWrap(True)
        top.addWidget(title, 1)
        if tag:
            chip = QLabel(tag)
            chip.setObjectName("metricTag")
            top.addWidget(chip)
        outer.addLayout(top)
        self.value = QLabel("—")
        self.value.setObjectName("metricValue")
        self.value.setProperty("kontaktHeadingSize", 27)
        self.value.setWordWrap(True)
        outer.addWidget(self.value)
        lower = QLabel(detail)
        lower.setObjectName("mutedLabel")
        lower.setWordWrap(True)
        outer.addWidget(lower)


class StatusChip(QLabel):
    """Small status chip rather than a full-cell background."""
    COLORS = {
        "ok": ("#e0efe4", "#216149"),
        "warn": ("#fff0d8", "#8c5e21"),
        "bad": ("#fce8e4", "#a34b3e"),
        "neutral": ("#ebeeeb", "#64716f"),
    }

    def __init__(self, text: str, tone: str = "neutral", parent=None):
        super().__init__(text, parent)
        background, ink = self.COLORS.get(tone, self.COLORS["neutral"])
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"background:{background};color:{ink};padding:5px 9px;"
            "border-radius:10px;border:0;")
        self.setToolTip(text)


class CompanyDelegate(QStyledItemDelegate):
    """Two-line company/contract block, with full data in tooltips."""
    def paint(self, painter: QPainter, option, index):
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor("#f5e6dc"))
        rect = option.rect.adjusted(12, 8, -8, -7)
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        meta = str(index.data(META_ROLE) or "")
        ink = QColor("#1b2b30")
        secondary = QColor("#71817f")
        painter.setPen(ink)
        font = QFont(option.font)
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = max(10, rect.width())
        painter.drawText(rect.left(), rect.top() + metrics.ascent(),
                         metrics.elidedText(name, Qt.TextElideMode.ElideRight, width))
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(secondary)
        secondary_metrics = painter.fontMetrics()
        y = rect.top() + metrics.height() + 4 + secondary_metrics.ascent()
        painter.drawText(rect.left(), y,
                         secondary_metrics.elidedText(meta, Qt.TextElideMode.ElideRight, width))
        painter.restore()


class ChipDelegate(QStyledItemDelegate):
    """Paint a compact badge without coloring the entire table cell."""
    def paint(self, painter: QPainter, option, index):
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor("#f5e6dc"))
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        tone = str(index.data(TONE_ROLE) or "neutral")
        bg, fg = StatusChip.COLORS.get(tone, StatusChip.COLORS["neutral"])
        metrics = option.fontMetrics
        padding = 11
        width = min(option.rect.width() - 12, metrics.horizontalAdvance(text) + 2 * padding)
        width = max(18, width)
        height = max(metrics.height() + 8, 25)
        rect = QRectF(option.rect.left() + 6,
                      option.rect.center().y() - height / 2, width, height)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(bg))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 9, 9)
        painter.setFont(option.font)
        painter.setPen(QColor(fg))
        painter.drawText(rect.toRect(), Qt.AlignmentFlag.AlignCenter,
                         metrics.elidedText(text, Qt.TextElideMode.ElideRight,
                                            max(10, int(rect.width()) - 8)))
        painter.restore()


class TenantCard(QFrame):
    """A responsive tenant card with real actions supplied by the owner."""
    def __init__(self, tenant: dict, *, archived: bool, open_card, open_history,
                 toggle_archive, parent=None):
        super().__init__(parent)
        self.setObjectName("tenantCard")
        self.tenant_id = tenant["id"]
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(230)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 17)
        outer.setSpacing(11)
        outer.addWidget(StatusChip("В архиве" if archived else "Активен",
                                   "neutral" if archived else "ok"))
        name = QLabel(tenant["name"])
        name.setObjectName("tenantName")
        name.setWordWrap(True)
        name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(name)
        for title, value in (
            ("ИНН", tenant.get("inn") or "Не указан"),
            ("Договор", tenant.get("contract_number") or "Не указан"),
            ("Начальные показания", display_number(tenant.get("initial_reading"), 3, "кВт·ч")),
        ):
            block = QHBoxLayout()
            key = QLabel(title)
            key.setObjectName("mutedLabel")
            key.setWordWrap(True)
            block.addWidget(key, 1)
            val = QLabel(str(value))
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            block.addWidget(val, 2)
            outer.addLayout(block)
        outer.addStretch()
        actions = QHBoxLayout()
        actions.setSpacing(7)
        for label, callback in (
            ("Карточка", open_card), ("История", open_history),
            ("Восстановить" if archived else "В архив", toggle_archive),
        ):
            action = QPushButton(label)
            action.setObjectName("cardAction")
            action.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
            action.clicked.connect(callback)
            actions.addWidget(action)
        outer.addLayout(actions)
        self.setAccessibleName(f"{'Архивный' if archived else 'Активный'} арендатор: {tenant['name']}")
