"""Presentation-only native widgets for the Kontakt visual language.

No database queries or calculation rules belong in this module.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape

from PySide6.QtCore import Qt, QRectF, QPointF, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPolygonF, QPixmap, QIcon, QPen, QPainterPath
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


class BoltIcon(QWidget):
    """Vector lightning mark; avoids platform-dependent colored emoji glyphs."""
    def sizeHint(self) -> QSize:
        return QSize(30, 38)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ec946d"))
        painter.translate(self.width() / 2, self.height() / 2)
        scale = min(self.width() / 30, self.height() / 38)
        painter.scale(scale, scale)
        polygon = QPolygonF([
            QPointF(3, -17), QPointF(-11, 1), QPointF(-2, 1),
            QPointF(-7, 17), QPointF(12, -4), QPointF(2, -4),
        ])
        painter.drawPolygon(polygon)
        painter.end()


def interface_icon(name: str, *, color: str = "#263a3f") -> QIcon:
    """Small outline icons drawn with Qt, matching the HTML prototype's icon language.

    QPainter avoids adding QtSvg or external icon/font dependencies. All paths
    are presentation-only and scale with Qt's device pixel ratio.
    """
    image = QPixmap(24, 24)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 1.9)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    def path(*points):
        stroke = QPainterPath()
        stroke.moveTo(*points[0])
        for point in points[1:]:
            stroke.lineTo(*point)
        painter.drawPath(stroke)

    if name == "dashboard":
        for x, y, w, h in ((3, 3, 7, 9), (14, 3, 7, 5),
                           (14, 12, 7, 9), (3, 16, 7, 5)):
            painter.drawRoundedRect(x, y, w, h, 1, 1)
    elif name == "tenants":
        painter.drawRoundedRect(3, 5, 18, 16, 2, 2)
        path((7, 5), (7, 3), (17, 3), (17, 5))
        for y in (10, 15):
            path((8, y), (11, y))
            path((13, y), (16, y))
    elif name == "settings":
        painter.drawEllipse(9, 9, 6, 6)
        painter.drawEllipse(4, 4, 16, 16)
        for x1, y1, x2, y2 in (
            (12, 2, 12, 5), (12, 19, 12, 22), (2, 12, 5, 12),
            (19, 12, 22, 12), (5, 5, 7, 7), (17, 17, 19, 19),
            (19, 5, 17, 7), (7, 17, 5, 19),
        ):
            path((x1, y1), (x2, y2))
    elif name == "plus":
        path((12, 5), (12, 19))
        path((5, 12), (19, 12))
    elif name == "download":
        path((12, 3), (12, 15), (7, 10))
        path((12, 15), (17, 10))
        path((4, 17), (4, 21), (20, 21), (20, 17))
    elif name == "search":
        painter.drawEllipse(4, 4, 14, 14)
        path((16, 16), (21, 21))
    elif name == "edit":
        path((4, 20), (8, 20), (20, 8), (16, 4), (4, 16), (4, 20))
        path((14, 6), (18, 10))
    elif name == "archive":
        painter.drawRoundedRect(3, 4, 18, 5, 1, 1)
        path((5, 9), (5, 21), (19, 21), (19, 9))
        path((10, 14), (14, 14))
    elif name in ("history", "restore", "reset"):
        painter.drawArc(3, 3, 18, 18, 40 * 16, 280 * 16)
        path((3, 17), (3, 12), (8, 12))
        if name == "history":
            path((12, 7), (12, 12), (16, 14))
    elif name == "file":
        path((5, 2), (14, 2), (19, 7), (19, 22), (5, 22), (5, 2))
        path((14, 2), (14, 8), (19, 8))
        path((8, 13), (16, 13))
        path((8, 17), (16, 17))
    elif name == "arrow":
        path((4, 12), (20, 12), (14, 6))
        path((20, 12), (14, 18))
    elif name == "fit":
        for points in (
            ((3, 9), (3, 3), (9, 3)),
            ((15, 3), (21, 3), (21, 9)),
            ((21, 15), (21, 21), (15, 21)),
            ((9, 21), (3, 21), (3, 15)),
        ):
            path(*points)
        path((7, 12), (17, 12))
    elif name == "calendar":
        painter.drawRoundedRect(3, 5, 18, 16, 2, 2)
        path((7, 3), (7, 7))
        path((17, 3), (17, 7))
        path((3, 10), (21, 10))
    elif name == "trash":
        path((4, 7), (20, 7))
        path((10, 4), (14, 4))
        path((6, 7), (7, 21), (17, 21), (18, 7))
        path((10, 10), (10, 17))
        path((14, 10), (14, 17))
    elif name == "check":
        path((5, 12), (10, 17), (20, 7))
    else:
        painter.drawEllipse(4, 4, 16, 16)
        path((12, 11), (12, 17))
    painter.end()
    return QIcon(image)


def icon_button(button: QPushButton, name: str, *, light: bool = False) -> QPushButton:
    """Attach an outline icon while preserving button text and accessible name."""
    button.setIcon(interface_icon(name, color="#fffefa" if light else "#263a3f"))
    button.setIconSize(QSize(18, 18))
    return button


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
    """Full multi-line company and contract without destructive elision."""
    def paint(self, painter: QPainter, option, index):
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor("#f5e6dc"))
        rect = option.rect.adjusted(12, 8, -8, -7)
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        meta = str(index.data(META_ROLE) or "")
        wrap = Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap
        font = QFont(option.font)
        font.setBold(True)
        painter.setFont(font)
        main_height = painter.fontMetrics().boundingRect(
            0, 0, max(10, rect.width()), 10_000, int(Qt.TextFlag.TextWordWrap), name).height()
        painter.setPen(QColor("#1b2b30"))
        painter.drawText(rect.left(), rect.top(), rect.width(), main_height, wrap, name)
        painter.setFont(option.font)
        painter.setPen(QColor("#71817f"))
        painter.drawText(rect.left(), rect.top() + main_height + 5,
                         rect.width(), max(0, rect.height() - main_height - 5), wrap, meta)
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
        # Two short actions plus a full-width archive action remain usable
        # under 200% text scaling and on a narrow Windows viewport.
        actions = QVBoxLayout()
        actions.setSpacing(7)
        first_line = QHBoxLayout()
        first_line.setSpacing(7)
        for label, callback in (("Карточка", open_card), ("История", open_history)):
            action = QPushButton(label)
            action.setObjectName("cardAction")
            icon_button(action, "edit" if label == "Карточка" else "history")
            action.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            action.clicked.connect(callback)
            first_line.addWidget(action)
        actions.addLayout(first_line)
        archive_action = QPushButton("Восстановить" if archived else "В архив")
        archive_action.setObjectName("cardAction")
        icon_button(archive_action, "restore" if archived else "archive")
        archive_action.clicked.connect(toggle_archive)
        actions.addWidget(archive_action)
        outer.addLayout(actions)
        self.setAccessibleName(f"{'Архивный' if archived else 'Активный'} арендатор: {tenant['name']}")
