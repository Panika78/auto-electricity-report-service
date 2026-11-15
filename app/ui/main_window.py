"""Main native Qt Widgets application shell and period/tenant navigation."""
from __future__ import annotations

import calendar
import sqlite3
from decimal import Decimal
from functools import wraps

from PySide6.QtCore import QDate, Qt, QLocale, QTimer
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView,
    QScrollArea, QGridLayout, QSizePolicy, QTabWidget,
)

from app.core.service import CalculationService
from app.core.validation import validate_calculation
from app.data import database as db
from app.data import queries
from app.ui.common import CANCELLED, apply_appearance, parse_number, run_confirmed, show_error, show_number
from app.ui.forms import CalculationDialog, ExportDialog, SettingsPage, TenantDialog
from app.ui.widgets import (BoltIcon, CompanyDelegate, ChipDelegate, META_ROLE, TONE_ROLE,
                            MetricCard, TenantCard, display_number,
                            month_label, russian_date, interface_icon, icon_button)


THEME = """
QMainWindow, QWidget#workspace {background: #f6f4ef; color: #1b2b30;}
QFrame#sidebar {background: #192a2f;}
QFrame#panel, QFrame#metricCard, QFrame#tenantCard, QFrame#formCard,
QFrame#previewCard {background: #fffefa; border: 1px solid #dfe2df; border-radius: 14px;}
QFrame#metricCard {border: 1px solid #e6e7e2;}
QFrame#tenantCard:hover {border: 1px solid #d3a28c;}
QFrame#sidebar QPushButton {background: transparent; border: none; border-radius: 9px;
  color: #b7c7c7; text-align: left; padding: 12px;}
QFrame#sidebar QPushButton:hover {background: #304248; color: #ffffff;}
QFrame#sidebar QPushButton:checked {background: #344a4d; color: white;
  border-left: 3px solid #ec946d;}
QLabel#eyebrow {color: #ae634d; font-weight: 700;}
QLabel#mutedLabel, QLabel#subtitle, QLabel#panelDescription {color: #657276;}
QLabel#metricTag {color: #a1543b; background: #f9eee8; padding: 4px 7px;
  border-radius: 7px; font-weight: 600;}
QPushButton {background: #fffefa; color: #1b2b30; border: 1px solid #d4dcd7;
  border-radius: 9px; padding: 8px 13px; min-height: 20px;}
QPushButton:hover:enabled {background: #f0f1ec; border-color: #abbbb1;}
QPushButton[primary="true"] {background: #c55e3c; border-color: #c55e3c; color: white;}
QPushButton[primary="true"]:hover:enabled {background: #a94e31;}
QPushButton[dark="true"] {background: #263a3f; color: white; border-color: #263a3f;}
QPushButton:disabled {color: #889397; background: #eef0ed;}
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QDateEdit:focus,
QTableWidget:focus, QSlider:focus {border: 2px solid #c55e3c;}
QLineEdit, QComboBox, QDateEdit {background: #fffefa; border: 1px solid #d8dfda;
  border-radius: 8px; padding: 7px; min-height: 20px;}
QTableWidget {background: #fffefa; border: 0; gridline-color: #eceeeb;
  selection-background-color: #f5e6dc; selection-color: #1b2b30;
  alternate-background-color: #fafaf7;}
QHeaderView::section {background: #f7f8f5; color: #576a6d;
  border: none; border-bottom: 1px solid #dfe2df; padding: 9px; font-weight: 600;}
QScrollArea {border: none; background: transparent;}
QSlider::groove:horizontal {height: 6px; background: #e5e2da; border-radius: 3px;}
QSlider::sub-page:horizontal {background: #c55e3c; border-radius: 3px;}
QSlider::handle:horizontal {width: 17px; height: 17px; margin: -6px 0;
  border: 2px solid #fffefa; border-radius: 9px; background: #c55e3c;}
QSlider::handle:horizontal:hover {background: #ab4f32;}
QSlider::handle:horizontal:focus {border-color: #263a3f;}
"""


def button(label: str, callback, *, primary: bool = False,
           icon: str | None = None) -> QPushButton:
    control = QPushButton(label)
    control.setProperty("primary", primary)
    if icon:
        icon_button(control, icon, light=primary)
    control.clicked.connect(callback)
    return control


def panel(parent: QWidget | None = None):
    frame = QFrame(parent)
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(12)
    return frame, layout


def table(headers: list[str]) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    widget.setWordWrap(True)
    widget.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    widget.verticalHeader().setVisible(False)
    widget.horizontalHeader().setStretchLastSection(False)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    widget.horizontalHeader().setMinimumSectionSize(82)
    widget.setMinimumHeight(240)
    return widget


def item(value, *, tooltip: str = "", kind: str = "") -> QTableWidgetItem:
    result = QTableWidgetItem(str(value if value is not None else "—"))
    if tooltip:
        result.setToolTip(tooltip)
    if kind:
        bg, fg = {
            "ok": ("#dcebe2", "#25604e"), "warn": ("#fcf0d4", "#80601e"),
            "bad": ("#fbe6e5", "#983d37"), "neutral": ("#eef0ed", "#657276"),
        }[kind]
        result.setBackground(QColor(bg))
        result.setForeground(QColor(fg))
    return result


def database_error_dialog(operation):
    """No SQLite exceptions may escape Qt read slots or display stale rows."""
    @wraps(operation)
    def protected(self, *args, **kwargs):
        try:
            return operation(self, *args, **kwargs)
        except (sqlite3.Error, OSError) as error:
            if operation.__name__ == "refresh_calculations":
                self.calc_table.setRowCount(0)
                self.summary.setText("Расчёты не загружены — ошибка доступа к базе данных.")
            elif operation.__name__ == "refresh_tenants":
                archived = kwargs.get("archived", args[0] if args else False)
                (self.archive_table if archived else self.tenant_table).setRowCount(0)
                self._clear_tenant_cards(archived)
            self.statusBar().showMessage("Ошибка доступа к базе данных — данные не обновлены.")
            show_error(self, error, "Ошибка доступа к базе данных")
            return None
    return protected


class MainWindow(QMainWindow):
    def __init__(self, database: db.Database | None = None) -> None:
        super().__init__()
        self.database = database or db.Database()
        if database is None:
            self.database.initialize()
        self.service = CalculationService(self.database)
        previous = QDate.currentDate().addMonths(-1)
        self.year, self.month = previous.year(), previous.month()
        self.setWindowTitle("контакт — учёт электроэнергии")
        self.resize(1360, 820)
        self.setMinimumSize(790, 540)
        self.setStyleSheet(THEME)
        apply_appearance()
        self._build()
        apply_appearance()
        self._refresh_all()

    def _build(self) -> None:
        root = QWidget(self)
        root.setObjectName("workspace")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(root)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(218)
        self.sidebar = sidebar
        nav_layout = QVBoxLayout(sidebar)
        nav_layout.setContentsMargins(15, 26, 15, 20)
        nav_layout.setSpacing(8)
        brand = QHBoxLayout()
        brand.setSpacing(7)
        brand.addWidget(BoltIcon())
        wordmark = QLabel(
            '<span style="color:#ffffff"><b>конт</b></span>'
            '<span style="color:#ec946d"><b>акт</b></span>')
        wordmark.setTextFormat(Qt.TextFormat.RichText)
        wordmark.setProperty("kontaktHeadingSize", 27)
        brand.addWidget(wordmark, 1)
        nav_layout.addLayout(brand)
        hint = QLabel("УЧЁТ ЭЛЕКТРОЭНЕРГИИ")
        hint.setStyleSheet("color:#9bb1b2; font-size:10px; padding-bottom:19px;")
        nav_layout.addWidget(hint)
        self.nav_buttons = {}
        for page, label, symbol in (
            ("calculations", "Расчёты", "dashboard"),
            ("tenants", "Арендаторы", "tenants"),
            ("settings", "Настройки", "settings"),
        ):
            control = QPushButton(label)
            icon_button(control, symbol, light=True)
            control.setCheckable(True)
            control.setAccessibleName(label)
            control.clicked.connect(lambda _=False, key=page: self.navigate(key))
            nav_layout.addWidget(control)
            self.nav_buttons[page] = control
        nav_layout.addStretch()
        layout.addWidget(sidebar)

        self.pages = QStackedWidget()
        self.pages.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(self.pages, 1)
        self.calc_page = self._build_calculations()
        self.tenant_page = self._build_tenants(archived=False)
        self.archive_page = self._build_tenants(archived=True)
        self.settings_page = SettingsPage(self.service)
        for page in (self.calc_page, self.tenant_page, self.archive_page, self.settings_page):
            self.pages.addWidget(self._scroll_page(page))
        self._reflow_layouts()
        self.navigate("calculations")
        self.statusBar().showMessage("Данные сохраняются локально")

    def _heading(self, title: str, subtitle: str):
        group = QVBoxLayout()
        heading = QLabel(title)
        heading.setProperty("kontaktHeadingSize", 27)
        detail = QLabel(subtitle)
        detail.setObjectName("subtitle")
        detail.setWordWrap(True)
        group.addWidget(heading)
        group.addWidget(detail)
        return group

    def _build_calculations(self) -> QWidget:
        page = QWidget()
        page.setObjectName("workspace")
        body = QVBoxLayout(page)
        body.setContentsMargins(14, 14, 14, 20)
        body.setSpacing(18)

        eyebrow = QLabel("ЕЖЕМЕСЯЧНЫЙ УЧЁТ")
        eyebrow.setObjectName("eyebrow")
        body.addWidget(eyebrow)
        body.addLayout(self._heading(
            "Расчёты электроэнергии",
            "Показания, начисления и документы за один календарный месяц."))

        monthbar = QHBoxLayout()
        monthbar.addWidget(QLabel("Расчётный месяц"))
        monthbar.addWidget(button("‹", lambda: self.shift_month(-1)))
        self.period_picker = QDateEdit(QDate(self.year, self.month, 1))
        self.period_picker.setLocale(QLocale(QLocale.Language.Russian, QLocale.Country.Russia))
        self.period_picker.setCalendarPopup(True)
        self.period_picker.setDisplayFormat("MMMM yyyy")
        self.period_picker.dateChanged.connect(self._period_changed)
        monthbar.addWidget(self.period_picker)
        monthbar.addWidget(button("›", lambda: self.shift_month(1)))
        monthbar.addStretch()
        self.bulk_action = QPushButton("Выгрузить всех · ZIP")
        icon_button(self.bulk_action, "download")
        self.bulk_action.setEnabled(False)
        self.bulk_action.setToolTip("Массовый экспорт станет доступен после issue #5")
        monthbar.addWidget(self.bulk_action)
        body.addLayout(monthbar)

        self.kpi_layout = QGridLayout()
        self.kpi_layout.setSpacing(12)
        self.kpi_cards = {
            "active": MetricCard("Активные арендаторы", "Всего в справочнике", "ВСЕ"),
            "ready": MetricCard("Готовые расчёты", "Для формирования документов", "МЕСЯЦ"),
            "total": MetricCard("Стоимость по готовым", "Сумма расчётов месяца", "₽"),
            "stale": MetricCard("Документы к обновлению", "После изменения данных", "АКТ + СЧЁТ"),
        }
        body.addLayout(self.kpi_layout)

        frame, contents = panel()
        panel_head = QHBoxLayout()
        month_info = QVBoxLayout()
        self.period_title = QLabel(month_label(self.year, self.month))
        self.period_title.setProperty("kontaktHeadingSize", 20)
        month_info.addWidget(self.period_title)
        self.summary = QLabel()
        self.summary.setObjectName("panelDescription")
        self.summary.setWordWrap(True)
        month_info.addWidget(self.summary)
        panel_head.addLayout(month_info, 1)
        tariff_box = QVBoxLayout()
        tariff_box.addWidget(QLabel("Тариф месяца, ₽/кВт·ч"))
        self.tariff_input = QLineEdit()
        self.tariff_input.setObjectName("tariffInput")
        self.tariff_input.setPlaceholderText("Не установлен")
        self.tariff_input.setToolTip("Введите общий тариф месяца и завершите редактирование для сохранения")
        self.tariff_input.editingFinished.connect(self.save_tariff)
        tariff_box.addWidget(self.tariff_input)
        panel_head.addLayout(tariff_box)
        contents.addLayout(panel_head)

        self.calc_search = QLineEdit()
        self.calc_search.setPlaceholderText("Поиск по названию, договору или ИНН…")
        self.calc_search.addAction(interface_icon("search"),
                                   QLineEdit.ActionPosition.LeadingPosition)
        self.calc_search.textChanged.connect(lambda _text: self.refresh_calculations())
        contents.addWidget(self.calc_search)
        filters = QHBoxLayout()
        self.calc_filter = QComboBox()
        self.calc_filter.addItems([
            "Все статусы", "Готовые", "Не готовы", "Без документов",
            "Требуют обновления", "Все документы сформированы"])
        self.calc_filter.currentIndexChanged.connect(lambda _index: self.refresh_calculations())
        filters.addWidget(self.calc_filter, 1)
        self.calc_sort = QComboBox()
        self.calc_sort.addItems(["По названию", "По изменению", "По сумме"])
        self.calc_sort.currentIndexChanged.connect(lambda _index: self.refresh_calculations())
        filters.addWidget(self.calc_sort, 1)
        filters.addWidget(button("Создать расчёт", self.new_calculation,
                                 primary=True, icon="plus"))
        contents.addLayout(filters)

        self.calc_table = table([
            "Арендатор / договор", "Период", "Начальные", "Конечные", "Расход, кВт·ч",
            "Тариф", "Сумма, ₽", "Готовность", "Акт", "Счёт", "Действия"])
        self.calc_table.setItemDelegateForColumn(0, CompanyDelegate(self.calc_table))
        for column in (7, 8, 9):
            self.calc_table.setItemDelegateForColumn(column, ChipDelegate(self.calc_table))
        for column, width in enumerate((290, 160, 120, 120, 137, 105, 160, 168, 136, 136, 172)):
            self.calc_table.setColumnWidth(column, width)
        self.calc_table.doubleClicked.connect(lambda _index: self.open_selected_calculation())
        self.calc_table.setToolTip("Двойной щелчок — открыть расчёт; PDF — в последнем столбце")
        contents.addWidget(self.calc_table, 1)

        actions = QHBoxLayout()
        actions.addWidget(button("Открыть расчёт", self.open_selected_calculation,
                                 icon="edit"))
        actions.addWidget(button("Арендатор / история", self.open_tenant_for_selected_calculation,
                                 icon="history"))
        actions.addWidget(button("PDF акта / счёта", self.export_selected_calculation,
                                 icon="file"))
        actions.addStretch()
        contents.addLayout(actions)
        self.calc_footer = QLabel()
        self.calc_footer.setObjectName("mutedLabel")
        self.calc_footer.setWordWrap(True)
        contents.addWidget(self.calc_footer)
        body.addWidget(frame, 1)

        return page

    def _build_tenants(self, *, archived: bool) -> QWidget:
        page = QWidget()
        page.setObjectName("workspace")
        body = QVBoxLayout(page)
        body.setContentsMargins(14, 14, 14, 20)
        body.setSpacing(17)
        eyebrow = QLabel("СПРАВОЧНИК")
        eyebrow.setObjectName("eyebrow")
        body.addWidget(eyebrow)
        body.addLayout(self._heading(
            "Архив арендаторов" if archived else "Арендаторы",
            "Сохранённые карточки, договоры и история. Можно восстановить."
            if archived else "Организации, договоры, первоначальные показания и история."))

        header = QHBoxLayout()
        if archived:
            header.addWidget(button("К активным арендаторам", lambda: self.navigate("tenants"),
                                    primary=True, icon="arrow"))
        else:
            self.open_archive_button = button(
                "Открыть архив", lambda: self.navigate("archive"), icon="archive")
            header.addWidget(self.open_archive_button)
            header.addWidget(button("Добавить арендатора", self.new_tenant,
                                    primary=True, icon="plus"))
        header.addStretch()
        body.addLayout(header)

        frame, inside = panel()
        title = QLabel("Карточки в архиве" if archived else "Карточки арендаторов")
        title.setProperty("kontaktHeadingSize", 20)
        inside.addWidget(title)
        count = QLabel()
        count.setObjectName("panelDescription")
        inside.addWidget(count)
        search = QLineEdit()
        search.setPlaceholderText("Поиск по названию, ИНН или договору…")
        search.addAction(interface_icon("search"),
                         QLineEdit.ActionPosition.LeadingPosition)
        search.textChanged.connect(lambda _t, is_archive=archived: self.refresh_tenants(is_archive))
        inside.addWidget(search)

        # A non-visible selection model preserves keyboard/bulk selection APIs
        # used by the already tested handlers. The *presented* UI is cards only.
        listing = QTableWidget(0, 1, page)
        listing.setVisible(False)
        inside.addWidget(listing)

        cards_host = QWidget()
        cards_host.setObjectName("cardsHost")
        cards_layout = QGridLayout(cards_host)
        cards_layout.setContentsMargins(0, 3, 0, 4)
        cards_layout.setSpacing(13)
        empty = QLabel()
        empty.setObjectName("mutedLabel")
        empty.setWordWrap(True)
        inside.addWidget(empty)
        inside.addWidget(cards_host, 1)
        body.addWidget(frame, 1)
        if archived:
            self.archive_search, self.archive_table = search, listing
            self.archive_cards_layout, self.archive_cards_host = cards_layout, cards_host
            self.archive_count, self.archive_empty = count, empty
            self._archive_cards = []
        else:
            self.tenant_search, self.tenant_table = search, listing
            self.tenant_cards_layout, self.tenant_cards_host = cards_layout, cards_host
            self.tenant_count, self.tenant_empty = count, empty
            self._tenant_cards = []
        return page

    @staticmethod
    def _scroll_page(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _clear_tenant_cards(self, archived: bool) -> None:
        widgets = self._archive_cards if archived else self._tenant_cards
        layout = self.archive_cards_layout if archived else self.tenant_cards_layout
        while layout.count():
            layout.takeAt(0)
        for widget in widgets:
            widget.hide()
            widget.deleteLater()
        if archived:
            self._archive_cards = []
        else:
            self._tenant_cards = []

    def _relayout_tenant_cards(self, archived: bool) -> None:
        widgets = self._archive_cards if archived else self._tenant_cards
        layout = self.archive_cards_layout if archived else self.tenant_cards_layout
        host = self.archive_cards_host if archived else self.tenant_cards_host
        # Work from the viewport width rather than the previous column geometry,
        # so the grid can really shrink when the user makes the window narrower.
        available = max(230, self.pages.width() - 110)
        scale = max(1, self.font().pointSizeF() / 10)
        minimum = int(285 * scale)
        columns = max(1, min(4, (available + 13) // (minimum + 13)))
        while layout.count():
            layout.takeAt(0)
        for column in range(4):
            layout.setColumnStretch(column, 1 if column < columns else 0)
        for index, widget in enumerate(widgets):
            layout.addWidget(widget, index // columns, index % columns)
        host.updateGeometry()

    def _reflow_layouts(self) -> None:
        if not hasattr(self, "kpi_layout") or not hasattr(self, "tenant_cards_layout"):
            return
        if getattr(self, "_reflowing", False):
            return
        self._reflowing = True
        try:
            scale = max(1, self.font().pointSizeF() / 10)
            if hasattr(self, "sidebar"):
                self.sidebar.setFixedWidth(min(350, max(205, int(218 * scale))))
            columns = 4 if self.pages.width() >= 1140 * scale else (
                2 if self.pages.width() >= 600 * scale else 1)
            while self.kpi_layout.count():
                self.kpi_layout.takeAt(0)
            for column in range(4):
                self.kpi_layout.setColumnStretch(column, 1 if column < columns else 0)
            for index, card in enumerate(self.kpi_cards.values()):
                self.kpi_layout.addWidget(card, index // columns, index % columns)
            self._relayout_tenant_cards(False)
            self._relayout_tenant_cards(True)
        finally:
            self._reflowing = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_layouts()

    def navigate(self, page: str) -> None:
        names = {"calculations": 0, "tenants": 1, "archive": 2, "settings": 3}
        self.pages.setCurrentIndex(names[page])
        for name, control in self.nav_buttons.items():
            # Archive remains a distinct page, but is reached from Tenants only.
            control.setChecked(name == ("tenants" if page == "archive" else page))
        if page == "settings":
            self.settings_page.reload()
        elif page in ("tenants", "archive"):
            self.refresh_tenants(page == "archive")
        else:
            self.refresh_calculations()

    def _period_changed(self, new: QDate) -> None:
        if (new.year(), new.month()) == (self.year, self.month):
            return
        self.year, self.month = new.year(), new.month()
        self.refresh_calculations()

    def shift_month(self, amount: int) -> None:
        self.period_picker.setDate(QDate(self.year, self.month, 1).addMonths(amount))

    def _refresh_all(self) -> None:
        self.refresh_calculations()
        self.refresh_tenants(False)
        self.refresh_tenants(True)

    @database_error_dialog
    def save_tariff(self) -> None:
        with self.database.connection() as conn:
            record = queries.month_tariff(conn, self.year, self.month)
        previous = show_number(record["rate"] if record else None)
        text = self.tariff_input.text().strip()
        if text == previous or (not text and record is None):
            return
        try:
            rate = parse_number(text, 4)
        except (ValueError, TypeError) as error:
            show_error(self, error, "Некорректный тариф")
            self.tariff_input.setText(previous)
            return
        result = run_confirmed(self, lambda flags: self.service.set_tariff(
            self.year, self.month, rate, **flags))
        if result is CANCELLED:
            self.tariff_input.setText(previous)
        self.refresh_calculations()

    @database_error_dialog
    def refresh_calculations(self) -> None:
        with self.database.connection() as conn:
            calculations = db.list_calculations(conn, year=self.year, month=self.month)
            tenants_list = db.list_tenants(conn, include_archived=True)
            tenants = {t["id"]: t for t in tenants_list}
            tariff = queries.month_tariff(conn, self.year, self.month)
            documents = db.list_documents(conn)
        self.period_title.setText(month_label(self.year, self.month))
        self.tariff_input.blockSignals(True)
        if not self.tariff_input.hasFocus():
            self.tariff_input.setText(show_number(tariff["rate"] if tariff else None))
        self.tariff_input.blockSignals(False)

        docs = {(d["calculation_id"], d["type"]): d for d in documents}
        rate = tariff["rate"] if tariff else None

        def doc_label(doc):
            if not doc or not doc["last_generated_at"]:
                return "Не сформирован"
            return "Сформирован" if doc["is_current"] else "Обновить"

        data = []
        ready_active, active_total, generated_both = 0, 0, 0
        amount_ready = Decimal("0")
        stale_docs = 0
        for calc in calculations:
            tenant = tenants.get(calc["tenant_id"], {})
            checked = validate_calculation(calc, rate)
            a = doc_label(docs.get((calc["id"], "act")))
            invoice = doc_label(docs.get((calc["id"], "invoice")))
            archived = bool(tenant.get("is_archived"))
            # Like the agreed HTML, the ready ratio is for active tenants,
            # while monetary and stale-document totals cover the whole month.
            if checked.ready and calc["total_amount"] is not None:
                amount_ready += Decimal(str(calc["total_amount"]))
            stale_docs += int(a == "Обновить") + int(invoice == "Обновить")
            if not archived:
                active_total += 1
                if checked.ready:
                    ready_active += 1
                if a == invoice == "Сформирован":
                    generated_both += 1
            data.append((calc, tenant, checked, a, invoice))

        active_count = sum(not t["is_archived"] for t in tenants_list)
        self.kpi_cards["active"].value.setText(str(active_count))
        self.kpi_cards["ready"].value.setText(f"{ready_active} / {active_total}")
        self.kpi_cards["total"].value.setText(display_number(amount_ready, 2, "₽"))
        self.kpi_cards["stale"].value.setText(str(stale_docs))
        self.summary.setText(
            f"Тариф: {display_number(rate, 4, '₽/кВт·ч') if rate is not None else 'не задан'}"
            f" · {len(calculations)} расчётов за месяц · {generated_both} комплектов документов"
            " · одна строка — один расчёт")

        q = self.calc_search.text().casefold().strip()
        selection = self.calc_filter.currentIndex()
        visible = []
        for calc, tenant, checked, act, invoice in data:
            searchable = " ".join((
                str(tenant.get("name", "")),
                str(tenant.get("inn") or ""),
                str(tenant.get("contract_number") or ""),
                str(calc["id"]),
            )).casefold()
            if q and q not in searchable:
                continue
            if selection == 1 and not checked.ready or selection == 2 and checked.ready:
                continue
            if selection == 3 and act != "Не сформирован" and invoice != "Не сформирован":
                continue
            if selection == 4 and act != "Обновить" and invoice != "Обновить":
                continue
            if selection == 5 and (act != "Сформирован" or invoice != "Сформирован"):
                continue
            visible.append((calc, tenant, checked, act, invoice))
        sort = self.calc_sort.currentIndex()
        if sort == 1:
            visible.sort(key=lambda x: (str(x[0]["updated_at"]), x[0]["id"]), reverse=True)
        elif sort == 2:
            visible.sort(key=lambda x: (
                Decimal(str(x[0]["total_amount"])) if x[0]["total_amount"] is not None
                else Decimal("-Infinity"), x[0]["id"]), reverse=True)
        else:
            visible.sort(key=lambda x: (str(x[1].get("name", "")).casefold(), -x[0]["id"]))

        selected = self.calc_table.currentRow()
        previously_selected = None
        if selected >= 0 and self.calc_table.item(selected, 0):
            previously_selected = self.calc_table.item(selected, 0).data(Qt.ItemDataRole.UserRole)
        self.calc_table.setUpdatesEnabled(False)
        try:
            self.calc_table.setRowCount(0)
            for calc, tenant, checked, act, invoice in visible:
                row = self.calc_table.rowCount()
                self.calc_table.insertRow(row)
                tenant_name = tenant.get("name") or f"Арендатор {calc['tenant_id']}"
                contract = tenant.get("contract_number") or "не указан"
                contract_text = f"Договор {contract}"
                if tenant.get("is_archived"):
                    contract_text += " · архив"
                metrics = QFontMetrics(self.calc_table.font())
                title_height = metrics.boundingRect(
                    0, 0, 260, 10_000, int(Qt.TextFlag.TextWordWrap), tenant_name).height()
                contract_height = metrics.boundingRect(
                    0, 0, 260, 10_000, int(Qt.TextFlag.TextWordWrap), contract_text).height()
                self.calc_table.setRowHeight(
                    row, max(65, title_height + contract_height + 26,
                             self.fontMetrics().height() * 3 + 12))
                name_item = item(tenant_name, tooltip=tenant_name + "\n" + contract_text)
                name_item.setData(Qt.ItemDataRole.UserRole, calc["id"])
                name_item.setData(META_ROLE, contract_text)
                self.calc_table.setItem(row, 0, name_item)

                period = russian_date(calc["period_start"]) + "\n" + russian_date(calc["period_end"])
                fields = (
                    period, display_number(calc["start_reading"], 3),
                    display_number(calc["end_reading"], 3),
                    display_number(calc["consumption"], 3), display_number(rate, 4),
                    display_number(calc["total_amount"], 2),
                )
                for offset, val in enumerate(fields, start=1):
                    entry = item(val, tooltip=val.replace("\n", " — "))
                    entry.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                        if offset >= 2 else Qt.AlignmentFlag.AlignCenter)
                    self.calc_table.setItem(row, offset, entry)
                warning = "; ".join(checked.errors + checked.warnings)
                status_label = "Готов" if checked.ready else "Не готов"
                for col, label, tone, tooltip in (
                    (7, status_label, "ok" if checked.ready else "bad",
                     warning or "Данные расчёта проверены"),
                    (8, act, "ok" if act == "Сформирован" else
                     "warn" if act == "Обновить" else "neutral", act),
                    (9, invoice, "ok" if invoice == "Сформирован" else
                     "warn" if invoice == "Обновить" else "neutral", invoice),
                ):
                    entry = item(label, tooltip=tooltip)
                    entry.setData(TONE_ROLE, tone)
                    self.calc_table.setItem(row, col, entry)

                actions = QWidget()
                action_bar = QHBoxLayout(actions)
                action_bar.setContentsMargins(4, 4, 4, 4)
                action_bar.setSpacing(5)
                edit = QPushButton("Открыть")
                icon_button(edit, "edit")
                edit.setToolTip("Открыть расчёт и изменить показания")
                edit.clicked.connect(
                    lambda _=False, identifier=calc["id"]: self._open_calculation_id(identifier))
                pdf = QPushButton("PDF")
                icon_button(pdf, "file")
                pdf.setToolTip("Экспорт акта, счёта или обоих документов")
                pdf.clicked.connect(
                    lambda _=False, identifier=calc["id"]: self._export_calculation_id(identifier))
                action_bar.addWidget(edit)
                action_bar.addWidget(pdf)
                self.calc_table.setCellWidget(row, 10, actions)
                if calc["id"] == previously_selected:
                    self.calc_table.selectRow(row)
        finally:
            self.calc_table.setUpdatesEnabled(True)
        self.calc_footer.setText(
            f"Показано {len(visible)} из {len(calculations)} расчётов · "
            "Данные сохраняются после изменения поля · "
            "Наведите на статус «Не готов», чтобы увидеть причину.")
        self._reflow_layouts()

    @database_error_dialog
    def refresh_tenants(self, archived: bool = False) -> None:
        listing = self.archive_table if archived else self.tenant_table
        search = self.archive_search if archived else self.tenant_search
        with self.database.connection() as conn:
            tenants = db.list_tenants(conn, include_archived=True)
        active_count = sum(not x["is_archived"] for x in tenants)
        archived_count = len(tenants) - active_count
        self.open_archive_button.setText(f"Открыть архив ({archived_count})")
        self.tenant_count.setText(f"{active_count} активных · {archived_count} в архиве")
        self.archive_count.setText(f"{active_count} активных · {archived_count} в архиве")

        self._clear_tenant_cards(archived)
        listing.setRowCount(0)
        text = search.text().casefold().strip()
        cards = []
        for tenant in sorted(tenants, key=lambda x: x["name"].casefold()):
            if bool(tenant["is_archived"]) != archived:
                continue
            searchable = " ".join((tenant["name"], tenant["inn"] or "",
                                   tenant["contract_number"] or "")).casefold()
            if text not in searchable:
                continue
            row = listing.rowCount()
            listing.insertRow(row)
            listing.setItem(row, 0, item(tenant["id"]))
            identifier = tenant["id"]
            card = TenantCard(
                tenant, archived=archived,
                open_card=lambda _=False, tid=identifier: self._open_tenant_id(tid),
                open_history=lambda _=False, tid=identifier: self._open_tenant_history(tid),
                toggle_archive=lambda _=False, tid=identifier, old=archived:
                    self._toggle_archive_id(tid, old),
                parent=self.archive_cards_host if archived else self.tenant_cards_host)
            cards.append(card)
        if archived:
            self._archive_cards = cards
            self.archive_empty.setText("В архиве нет арендаторов." if not cards else "")
        else:
            self._tenant_cards = cards
            self.tenant_empty.setText(
                "Пока нет арендаторов. Добавьте первую карточку."
                if not cards and not text else
                "Совпадений не найдено. Измените условия поиска." if not cards else "")
        self._relayout_tenant_cards(archived)

    def selected_id(self, listing: QTableWidget) -> int | None:
        selected = listing.currentRow()
        if selected < 0 or listing.item(selected, 0) is None:
            QMessageBox.information(self, "Выберите запись", "Сначала выделите строку таблицы.")
            return None
        stored = listing.item(selected, 0).data(Qt.ItemDataRole.UserRole)
        return int(stored if stored is not None else listing.item(selected, 0).text())

    @database_error_dialog
    def new_calculation(self) -> None:
        dlg = CalculationDialog(self.service, self.database, year=self.year, month=self.month, parent=self)
        dlg.exec()
        self._refresh_all()

    @database_error_dialog
    def _open_calculation_id(self, calculation_id: int) -> None:
        CalculationDialog(self.service, self.database, calculation_id=calculation_id, parent=self).exec()
        self._refresh_all()

    @database_error_dialog
    def open_selected_calculation(self) -> None:
        calculation_id = self.selected_id(self.calc_table)
        if calculation_id is not None:
            self._open_calculation_id(calculation_id)

    @database_error_dialog
    def _open_tenant_id(self, tenant_id: int, *, history: bool = False) -> None:
        dialog = TenantDialog(self.service, self.database, tenant_id=tenant_id, parent=self)
        if history:
            tabs = dialog.findChild(QTabWidget)
            if tabs is not None:
                tabs.setCurrentIndex(1)
        dialog.exec()
        self._refresh_all()

    @database_error_dialog
    def _open_tenant_history(self, tenant_id: int) -> None:
        self._open_tenant_id(tenant_id, history=True)

    @database_error_dialog
    def open_tenant_for_selected_calculation(self) -> None:
        calculation_id = self.selected_id(self.calc_table)
        if calculation_id is None:
            return
        calc = self.service.get_calculation(calculation_id)
        self._open_tenant_id(calc["tenant_id"])

    @database_error_dialog
    def _export_calculation_id(self, calculation_id: int) -> None:
        ExportDialog(self.service, self.database, calculation_id, self).exec()
        self.refresh_calculations()

    @database_error_dialog
    def export_selected_calculation(self) -> None:
        calculation_id = self.selected_id(self.calc_table)
        if calculation_id is not None:
            self._export_calculation_id(calculation_id)

    @database_error_dialog
    def new_tenant(self) -> None:
        TenantDialog(self.service, self.database, parent=self).exec()
        self._refresh_all()

    @database_error_dialog
    def open_selected_tenant(self, archived: bool = False) -> None:
        tenant_id = self.selected_id(self.archive_table if archived else self.tenant_table)
        if tenant_id is not None:
            self._open_tenant_id(tenant_id)

    @database_error_dialog
    def _toggle_archive_id(self, tenant_id: int, archived: bool) -> None:
        action = "восстановить" if archived else "переместить в архив"
        if QMessageBox.question(
            self, "Подтверждение", f"Вы уверены, что хотите {action} выбранного арендатора?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        result = run_confirmed(self, lambda _flags: self.service.update_tenant(
            tenant_id, is_archived=not archived))
        if result is not CANCELLED:
            self._refresh_all()
            self.statusBar().showMessage("Изменение сохранено")

    @database_error_dialog
    def toggle_selected_archive(self, archived: bool) -> None:
        tenant_id = self.selected_id(self.archive_table if archived else self.tenant_table)
        if tenant_id is not None:
            self._toggle_archive_id(tenant_id, archived)


    def closeEvent(self, event) -> None:
        # Persist the currently active performer field before application exit.
        for field in tuple(self.settings_page.pending):
            self.settings_page._save_field(field)
        if self.settings_page.pending:
            answer = QMessageBox.question(
                self, "Несохранённые настройки",
                "Изменения реквизитов не удалось сохранить. Закрыть приложение без них?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                self.navigate("settings")
                return
        event.accept()
