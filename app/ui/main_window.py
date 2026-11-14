"""Main native Qt Widgets application shell and period/tenant navigation."""
from __future__ import annotations

import calendar
import sqlite3
from functools import wraps

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView,
)

from app.core.service import CalculationService
from app.core.validation import validate_calculation
from app.data import database as db
from app.data import queries
from app.ui.common import CANCELLED, apply_appearance, parse_number, run_confirmed, show_error, show_number
from app.ui.forms import CalculationDialog, ExportDialog, SettingsPage, TenantDialog


THEME = """
QMainWindow, QWidget#workspace {background: #f6f4ef; color: #1b2b30;}
QFrame#sidebar {background: #192a2f;}
QFrame#panel {background: #fffefa; border: 1px solid #dfe2df; border-radius: 14px;}
QFrame#sidebar QPushButton {background: transparent; border: none; border-radius: 9px;
  color: #b7c7c7; text-align: left; padding: 12px;}
QFrame#sidebar QPushButton:hover {background: #304248; color: #ffffff;}
QFrame#sidebar QPushButton:checked {background: #344a4d; color: white; border-left: 3px solid #ec946d;}
QPushButton {background: #fffefa; color: #1b2b30; border: 1px solid #d4dcd7;
  border-radius: 9px; padding: 8px 13px; min-height: 20px;}
QPushButton:hover:enabled {background: #f0f1ec; border-color: #abbbb1;}
QPushButton[primary="true"] {background: #c55e3c; border-color: #c55e3c; color: white;}
QPushButton[primary="true"]:hover:enabled {background: #a94e31;}
QPushButton:disabled {color: #889397; background: #eef0ed;}
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QDateEdit:focus,
QTableWidget:focus {border: 2px solid #c55e3c;}
QLineEdit, QComboBox, QDateEdit {background: #fffefa; border: 1px solid #d8dfda;
  border-radius: 8px; padding: 7px; min-height: 20px;}
QTableWidget {background: #fffefa; gridline-color: #eceeeb;
  selection-background-color: #f5e6dc; selection-color: #1b2b30;
  alternate-background-color: #fafaf7;}
QHeaderView::section {background: #f1f3ef; color: #576a6d;
  border: none; border-bottom: 1px solid #dfe2df; padding: 9px; font-weight: 600;}
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
    widget.setWordWrap(False)
    widget.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    widget.verticalHeader().setVisible(False)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    widget.setMinimumHeight(260)
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
                (self.archive_table if kwargs.get("archived", args[0] if args else False)
                 else self.tenant_table).setRowCount(0)
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
            self.pages.addWidget(page)
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
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(16)
        heading = QHBoxLayout()
        heading.addLayout(self._heading("Расчёты", "Месячные показания, суммы и документы"))
        heading.addStretch()
        heading.addWidget(button("Новый расчёт", self.new_calculation, primary=True))
        body.addLayout(heading)

        period_frame, period = panel()
        period_bar = QHBoxLayout()
        period_bar.addWidget(QLabel("Расчётный месяц"))
        period_bar.addWidget(button("‹", lambda: self.shift_month(-1)))
        self.period_picker = QDateEdit(QDate(self.year, self.month, 1))
        self.period_picker.setCalendarPopup(True)
        self.period_picker.setDisplayFormat("MMMM yyyy")
        self.period_picker.dateChanged.connect(self._period_changed)
        period_bar.addWidget(self.period_picker)
        period_bar.addWidget(button("›", lambda: self.shift_month(1)))
        period_bar.addSpacing(30)
        period_bar.addWidget(QLabel("Тариф, ₽/кВт·ч"))
        self.tariff_input = QLineEdit()
        self.tariff_input.setMaximumWidth(130)
        self.tariff_input.setPlaceholderText("Не установлен")
        self.tariff_input.editingFinished.connect(self.save_tariff)
        period_bar.addWidget(self.tariff_input)
        period_bar.addStretch()
        period.addLayout(period_bar)
        self.summary = QLabel()
        self.summary.setStyleSheet("color:#64777b;")
        period.addWidget(self.summary)
        body.addWidget(period_frame)

        frame, contents = panel()
        controls = QHBoxLayout()
        self.calc_search = QLineEdit()
        self.calc_search.setPlaceholderText("Поиск арендатора или ID…")
        self.calc_search.textChanged.connect(lambda _text: self.refresh_calculations())
        controls.addWidget(self.calc_search, 1)
        self.calc_filter = QComboBox()
        self.calc_filter.addItems(["Все расчёты", "Готовые", "Не готовы", "Документы не сформированы",
                                   "Требуют обновления", "Все документы сформированы"])
        self.calc_filter.currentIndexChanged.connect(lambda _index: self.refresh_calculations())
        controls.addWidget(self.calc_filter)
        controls.addWidget(button("Расчёт", self.open_selected_calculation))
        controls.addWidget(button("Арендатор / история", self.open_tenant_for_selected_calculation))
        controls.addWidget(button("PDF", self.export_selected_calculation))
        contents.addLayout(controls)
        self.calc_table = table(["ID", "Арендатор", "Период", "Начало", "Конец",
                                 "Расход, кВт·ч", "Тариф", "Сумма, ₽", "Готовность", "Акт", "Счёт"])
        self.calc_table.doubleClicked.connect(lambda _index: self.open_selected_calculation())
        contents.addWidget(self.calc_table, 1)
        bulk = QHBoxLayout()
        self.bulk_action = QPushButton("Массовая ZIP-выгрузка")
        self.bulk_action.setEnabled(False)
        self.bulk_action.setToolTip("Доступно после реализации движка массового экспорта в issue #5")
        bulk.addWidget(self.bulk_action)
        bulk.addWidget(QLabel("ZIP будет доступен после подключения задачи #5"))
        bulk.addStretch()
        contents.addLayout(bulk)
        body.addWidget(frame, 1)
        return page

    def _build_tenants(self, *, archived: bool) -> QWidget:
        page = QWidget()
        page.setObjectName("workspace")
        body = QVBoxLayout(page)
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(16)
        header = QHBoxLayout()
        header.addLayout(self._heading("Архив" if archived else "Арендаторы",
                                      "История сохраняется при архивировании" if archived
                                      else "Реквизиты, договоры и история показаний"))
        header.addStretch()
        if not archived:
            header.addWidget(button("Открыть архив", lambda: self.navigate("archive")))
            header.addWidget(button("Добавить арендатора", self.new_tenant, primary=True))
        else:
            header.addWidget(button("К арендаторам", lambda: self.navigate("tenants")))
        body.addLayout(header)
        frame, inside = panel()
        search = QLineEdit()
        search.setPlaceholderText("Поиск по названию или ИНН…")
        search.textChanged.connect(lambda _t, is_archive=archived: self.refresh_tenants(is_archive))
        inside.addWidget(search)
        listing = table(["ID", "Организация", "ИНН", "Договор", "Начальное показание", "Изменён"])
        listing.doubleClicked.connect(lambda _index, is_archive=archived: self.open_selected_tenant(is_archive))
        inside.addWidget(listing, 1)
        toolbar = QHBoxLayout()
        toolbar.addWidget(button("Открыть карточку", lambda: self.open_selected_tenant(archived)))
        toolbar.addWidget(button("Восстановить" if archived else "В архив",
                                 lambda: self.toggle_selected_archive(archived)))
        toolbar.addStretch()
        inside.addLayout(toolbar)
        body.addWidget(frame, 1)
        if archived:
            self.archive_search, self.archive_table = search, listing
        else:
            self.tenant_search, self.tenant_table = search, listing
        return page

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
