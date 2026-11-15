"""Main native Qt Widgets application shell and period/tenant navigation."""
from __future__ import annotations

import calendar
import sqlite3
from decimal import Decimal
from functools import wraps

from PySide6.QtCore import QDate, Qt, QLocale
from PySide6.QtGui import QColor
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
from app.ui.widgets import (CompanyDelegate, ChipDelegate, META_ROLE, TONE_ROLE,
                            MetricCard, TenantCard, display_number,
                            month_label, russian_date)


THEME = """
QMainWindow, QWidget#workspace {background: #f6f4ef; color: #1b2b30;}
QFrame#sidebar {background: #192a2f;}
QFrame#panel, QFrame#metricCard, QFrame#tenantCard, QFrame#formCard,
QFrame#previewCard {background: #fffefa; border: 1px solid #dfe2df; border-radius: 14px;}
QFrame#metricCard {border: 1px solid #e6e7e2;}
QFrame#tenantCard:hover {border: 1px solid #d3a28c;}
QFrame#helpStrip {background: #f0eee7; border-left: 3px solid #c55e3c;
                  border-radius: 8px; color: #3b5051;}
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


def button(label: str, callback, *, primary: bool = False) -> QPushButton:
    control = QPushButton(label)
    control.setProperty("primary", primary)
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
        wordmark = QLabel('<span style="color:#ec946d;font-size:24px">⚡</span> '
                          '<span style="color:#ffffff;font-size:23px"><b>конт</b></span>'
                          '<span style="color:#ec946d;font-size:23px"><b>акт</b></span>')
        wordmark.setTextFormat(Qt.TextFormat.RichText)
        nav_layout.addWidget(wordmark)
        hint = QLabel("УЧЁТ ЭЛЕКТРОЭНЕРГИИ")
        hint.setStyleSheet("color:#9bb1b2; font-size:10px; padding-bottom:19px;")
        nav_layout.addWidget(hint)
        self.nav_buttons = {}
        for page, label in (("calculations", "▦  Расчёты"), ("tenants", "▤  Арендаторы"),
                            ("archive", "▧  Архив"), ("settings", "⚙  Настройки")):
            control = QPushButton(label)
            control.setCheckable(True)
            control.setAccessibleName(label.strip())
            control.clicked.connect(lambda _=False, key=page: self.navigate(key))
            nav_layout.addWidget(control)
            self.nav_buttons[page] = control
        nav_layout.addStretch()
        foot = QLabel("●  Локальные данные\nБез интернета и аккаунта")
        foot.setStyleSheet("color:#9db6b2; padding:10px;")
        nav_layout.addWidget(foot)
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
        detail.setStyleSheet("color:#657276;")
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
        filters.addWidget(button("Создать расчёт", self.new_calculation, primary=True))
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
        actions.addWidget(button("Открыть расчёт", self.open_selected_calculation))
        actions.addWidget(button("Арендатор / история", self.open_tenant_for_selected_calculation))
        actions.addWidget(button("PDF акта / счёта", self.export_selected_calculation))
        actions.addStretch()
        contents.addLayout(actions)
        self.calc_footer = QLabel()
        self.calc_footer.setObjectName("mutedLabel")
        self.calc_footer.setWordWrap(True)
        contents.addWidget(self.calc_footer)
        body.addWidget(frame, 1)

        help_frame = QFrame()
        help_frame.setObjectName("helpStrip")
        help_line = QVBoxLayout(help_frame)
        help_line.setContentsMargins(18, 15, 18, 15)
        title = QLabel("Как проходит работа")
        title.setProperty("kontaktHeadingSize", 19)
        help_line.addWidget(title)
        explanation = QLabel(
            "Выберите месяц → задайте тариф → откройте расчёт и внесите показания → "
            "сформируйте акт, счёт или оба. Причина неготовности указана у расчёта. "
            "Изменения сохраняются после завершения ввода поля.")
        explanation.setWordWrap(True)
        help_line.addWidget(explanation)
        body.addWidget(help_frame)
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
                                    primary=True))
        else:
            header.addWidget(button("Открыть архив", lambda: self.navigate("archive")))
            header.addWidget(button("Добавить арендатора", self.new_tenant, primary=True))
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
            control.setChecked(name == page)
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
            tenants = {t["id"]: t for t in db.list_tenants(conn, include_archived=True)}
            tariff = queries.month_tariff(conn, self.year, self.month)
            documents = db.list_documents(conn)
        self.tariff_input.blockSignals(True)
        if not self.tariff_input.hasFocus():
            self.tariff_input.setText(show_number(tariff["rate"] if tariff else None))
        self.tariff_input.blockSignals(False)
        docs = {(d["calculation_id"], d["type"]): d for d in documents}
        self.calc_table.setRowCount(0)
        ready_count, generated_count = 0, 0
        q = self.calc_search.text().casefold().strip()
        selection = self.calc_filter.currentIndex()
        for calc in calculations:
            tenant = tenants.get(calc["tenant_id"], {})
            name = tenant.get("name", f"ID {calc['tenant_id']}")
            checked = validate_calculation(calc, tariff["rate"] if tariff else None)
            act = docs.get((calc["id"], "act"))
            invoice = docs.get((calc["id"], "invoice"))
            def doc_label(doc):
                if not doc or not doc["last_generated_at"]:
                    return "Не сформирован"
                return "Сформирован" if doc["is_current"] else "Обновить"
            a, i = doc_label(act), doc_label(invoice)
            if checked.ready:
                ready_count += 1
            if a == i == "Сформирован":
                generated_count += 1
            if q and q not in name.casefold() and q not in str(calc["id"]) and q not in tenant.get("inn", ""):
                continue
            if selection == 1 and not checked.ready or selection == 2 and checked.ready:
                continue
            if selection == 3 and a != "Не сформирован" and i != "Не сформирован":
                continue
            if selection == 4 and a != "Обновить" and i != "Обновить":
                continue
            if selection == 5 and (a != "Сформирован" or i != "Сформирован"):
                continue
            row = self.calc_table.rowCount()
            self.calc_table.insertRow(row)
            values = [calc["id"], name, f"{calc['period_start']} — {calc['period_end']}",
                      show_number(calc["start_reading"]), show_number(calc["end_reading"]),
                      show_number(calc["consumption"]),
                      show_number(tariff["rate"] if tariff else None),
                      show_number(calc["total_amount"]),
                      "Готов" if checked.ready else "Не готов: " + (checked.errors[0] if checked.errors else "неполные данные"), a, i]
            for column, value in enumerate(values):
                tooltip = "; ".join(checked.errors + checked.warnings) if column == 8 else ""
                if column == 1 and tenant.get("is_archived"):
                    tooltip = "Арендатор находится в архиве"
                color = ""
                if column == 8:
                    color = "ok" if checked.ready else "bad"
                elif column in (9, 10):
                    color = "ok" if value == "Сформирован" else "warn" if value == "Обновить" else "neutral"
                self.calc_table.setItem(row, column, item(value, tooltip=tooltip, kind=color))
        self.summary.setText(
            f"За {self.month:02d}.{self.year}: расчётов — {len(calculations)}  ·  "
            f"готовы — {ready_count}  ·  комплект документов — {generated_count}  ·  "
            f"тариф: {show_number(tariff['rate']) if tariff else 'не задан'}")
        self.calc_table.resizeRowsToContents()

    @database_error_dialog
    def refresh_tenants(self, archived: bool = False) -> None:
        listing = self.archive_table if archived else self.tenant_table
        search = self.archive_search if archived else self.tenant_search
        with self.database.connection() as conn:
            tenants = db.list_tenants(conn, include_archived=True)
        listing.setRowCount(0)
        for tenant in tenants:
            if bool(tenant["is_archived"]) != archived:
                continue
            if search.text().casefold() not in (tenant["name"] + tenant["inn"]).casefold():
                continue
            row = listing.rowCount()
            listing.insertRow(row)
            values = [tenant["id"], tenant["name"], tenant["inn"],
                      tenant["contract_number"], show_number(tenant["initial_reading"]),
                      tenant["updated_at"]]
            for col, value in enumerate(values):
                listing.setItem(row, col, item(value))
        listing.resizeRowsToContents()
        self.nav_buttons["archive"].setText(
            f"▧  Архив ({sum(1 for t in tenants if t['is_archived'])})")

    def selected_id(self, listing: QTableWidget) -> int | None:
        selected = listing.currentRow()
        if selected < 0 or listing.item(selected, 0) is None:
            QMessageBox.information(self, "Выберите запись", "Сначала выделите строку таблицы.")
            return None
        return int(listing.item(selected, 0).text())

    @database_error_dialog
    def new_calculation(self) -> None:
        dlg = CalculationDialog(self.service, self.database, year=self.year, month=self.month, parent=self)
        if dlg.exec():
            self._refresh_all()

    @database_error_dialog
    def open_selected_calculation(self) -> None:
        calc_id = self.selected_id(self.calc_table)
        if calc_id is not None:
            dlg = CalculationDialog(self.service, self.database, calculation_id=calc_id, parent=self)
            dlg.exec()
            self._refresh_all()

    @database_error_dialog
    def open_tenant_for_selected_calculation(self) -> None:
        calculation_id = self.selected_id(self.calc_table)
        if calculation_id is None:
            return
        calc = self.service.get_calculation(calculation_id)
        tenant_dialog = TenantDialog(
            self.service, self.database, tenant_id=calc["tenant_id"], parent=self)
        tenant_dialog.exec()
        self._refresh_all()

    @database_error_dialog
    def export_selected_calculation(self) -> None:
        calc_id = self.selected_id(self.calc_table)
        if calc_id is not None:
            ExportDialog(self.service, self.database, calc_id, self).exec()
            self.refresh_calculations()

    @database_error_dialog
    def new_tenant(self) -> None:
        if TenantDialog(self.service, self.database, parent=self).exec():
            self._refresh_all()

    @database_error_dialog
    def open_selected_tenant(self, archived: bool = False) -> None:
        tenant_id = self.selected_id(self.archive_table if archived else self.tenant_table)
        if tenant_id is not None:
            TenantDialog(self.service, self.database, tenant_id=tenant_id, parent=self).exec()
            self._refresh_all()

    @database_error_dialog
    def toggle_selected_archive(self, archived: bool) -> None:
        tenant_id = self.selected_id(self.archive_table if archived else self.tenant_table)
        if tenant_id is None:
            return
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
