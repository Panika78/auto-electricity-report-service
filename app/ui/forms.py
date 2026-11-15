"""Real tenant, calculation, performer and document dialogs backed by project services."""
from __future__ import annotations

import calendar
import os
import sqlite3
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSlider, QSpinBox, QTabWidget, QTableWidget, QFrame,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QPixmap

from app.core.calculations import recalculate
from app.core.validation import validate_calculation
from app.core.service import CalculationService
from app.data import database as db
from app.data import queries
from app.documents import ExportError, export_document, safe_filename
from app.documents.export import _assert_unique_destinations
from .common import (CANCELLED, appearance_settings, apply_appearance, guard_database_errors,
                     import_image, remove_managed_image, parse_number, run_confirmed, show_error, show_number,
                     read_appearance, available_font_weights, effective_font_weight)
from .widgets import StatusChip, russian_date, icon_button, display_number, month_label


TENANT_FIELDS = [
    ("name", "Название *"), ("inn", "ИНН"), ("kpp", "КПП"), ("ogrn", "ОГРН / ОГРНИП"),
    ("legal_address", "Юридический адрес"), ("postal_address", "Почтовый адрес"),
    ("bank_name", "Банк"), ("bik", "БИК"), ("account_number", "Расчётный счёт"),
    ("correspondent_account", "Корреспондентский счёт"),
    ("contract_number", "Номер договора"), ("additional_agreement", "Допсоглашения"),
    ("initial_reading", "Первоначальные показания, кВт·ч"),
]
SETTINGS_FIELDS = [
    ("performer_name", "Исполнитель *"), ("inn", "ИНН"), ("kpp", "КПП"),
    ("ogrn", "ОГРН / ОГРНИП"), ("address", "Адрес"), ("bank_name", "Банк"),
    ("bik", "БИК"), ("account_number", "Расчётный счёт"),
    ("correspondent_account", "Корреспондентский счёт"),
]


def _line(value=None) -> QLineEdit:
    widget = QLineEdit(show_number(value) if value is not None else "")
    widget.setClearButtonEnabled(True)
    return widget


def _optional_date(value=None):
    outer = QWidget()
    layout = QHBoxLayout(outer)
    layout.setContentsMargins(0, 0, 0, 0)
    enabled = QCheckBox("Указать")
    picker = QDateEdit()
    picker.setCalendarPopup(True)
    picker.setDisplayFormat("dd.MM.yyyy")
    picker.setDate(QDate.fromString(str(value), "yyyy-MM-dd") if value else QDate.currentDate())
    enabled.setChecked(bool(value))
    picker.setEnabled(bool(value))
    enabled.toggled.connect(picker.setEnabled)
    layout.addWidget(enabled)
    layout.addWidget(picker, 1)
    return outer, enabled, picker


def _date_value(enabled: QCheckBox, picker: QDateEdit) -> str | None:
    return picker.date().toString("yyyy-MM-dd") if enabled.isChecked() else None


def _field_values(fields):
    return {key: field.text() for key, field in fields.items()}


class TenantDialog(QDialog):
    """A new card is explicitly created; subsequent valid changes autosave."""
    def __init__(self, service: CalculationService, database: db.Database,
                 tenant_id: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self.service, self.database, self.tenant_id = service, database, tenant_id
        self.pending: set[str] = set()
        self._filling = False
        # Initial loading is fail-closed: never expose a blank editable card.
        self._initial_load = True
        self.setWindowTitle("Карточка арендатора" if tenant_id else "Новый арендатор")
        self.resize(760, 720)
        outer = QVBoxLayout(self)
        header = QLabel("Реквизиты арендатора и договор")
        header.setProperty("kontaktHeadingSize", 19)
        outer.addWidget(header)
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)

        form_area = QScrollArea()
        form_area.setWidgetResizable(True)
        form_content = QWidget()
        form = QFormLayout(form_content)
        self.fields = {}
        for key, title in TENANT_FIELDS:
            field = _line()
            field.setObjectName("tenant_" + key)
            field.editingFinished.connect(lambda name=key: self._save_field(name))
            field.textEdited.connect(lambda _t, name=key: self._mark_pending(name))
            self.fields[key] = field
            form.addRow(title, field)
        self.contract_date_row, self.contract_checked, self.contract_date = _optional_date()
        form.addRow("Дата договора", self.contract_date_row)
        self.initial_date_row, self.initial_checked, self.initial_date = _optional_date()
        form.addRow("Дата первых показаний", self.initial_date_row)
        for key, toggle, picker in (
            ("contract_date", self.contract_checked, self.contract_date),
            ("initial_reading_date", self.initial_checked, self.initial_date),
        ):
            toggle.toggled.connect(lambda _on, name=key: self._save_date(name))
            picker.dateChanged.connect(lambda _d, name=key: self._save_date(name))
        form_area.setWidget(form_content)
        tabs.addTab(form_area, "Карточка")

        history = QWidget()
        history_layout = QVBoxLayout(history)
        history_layout.addWidget(QLabel("Все периоды, включая черновики и архивные записи"))
        self.history = QTableWidget(0, 6)
        self.history.setHorizontalHeaderLabels(
            ["ID", "Месяц", "Даты", "Расход", "Сумма", "Готовность"])
        self.history.setColumnHidden(0, True)  # internal row identity, not user-facing data
        self.history.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.horizontalHeader().setStretchLastSection(True)
        self.history.doubleClicked.connect(lambda _index: self._open_calculation())
        history_layout.addWidget(self.history)
        open_history = QPushButton("Открыть расчёт")
        icon_button(open_history, "edit")
        open_history.clicked.connect(self._open_calculation)
        history_layout.addWidget(open_history)
        tabs.addTab(history, "История")

        actions = QHBoxLayout()
        self.archive_button = QPushButton("Переместить в архив")
        icon_button(self.archive_button, "archive")
        self.archive_button.clicked.connect(self._toggle_archive)
        actions.addWidget(self.archive_button)
        actions.addStretch()
        self.note = QLabel("Изменения сохраняются после завершения ввода поля")
        self.note.setWordWrap(True)
        actions.addWidget(self.note)
        self.create_button = QPushButton("Создать арендатора")
        icon_button(self.create_button, "plus")
        self.create_button.clicked.connect(self._create)
        actions.addWidget(self.create_button)
        close = QPushButton("Готово")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        outer.addLayout(actions)

        self._load()
        self._initial_load = False
        self.history.setEnabled(self.tenant_id is not None)
        self.archive_button.setVisible(self.tenant_id is not None)
        self.create_button.setVisible(self.tenant_id is None)

    def _mark_pending(self, name: str) -> None:
        if self.tenant_id is not None and not self._filling:
            self.pending.add(name)
            self.note.setText("Изменения поля ещё не сохранены")

    @guard_database_errors("Ошибка базы данных")
    def _load(self) -> None:
        if self.tenant_id is None:
            return
        with self.database.connection() as conn:
            record = db.get_tenant(conn, self.tenant_id)
        if record is None:
            raise LookupError("Карточка арендатора не найдена.")
        self._filling = True
        for key, field in self.fields.items():
            field.setText(show_number(record[key]) if key == "initial_reading" else str(record[key] or ""))
        for key, check, picker in (
            ("contract_date", self.contract_checked, self.contract_date),
            ("initial_reading_date", self.initial_checked, self.initial_date),
        ):
            check.setChecked(bool(record[key]))
            picker.setEnabled(bool(record[key]))
            if record[key]:
                picker.setDate(QDate.fromString(record[key], "yyyy-MM-dd"))
        self.archive_button.setText("Восстановить арендатора" if record["is_archived"]
                                    else "Переместить в архив")
        icon_button(self.archive_button, "restore" if record["is_archived"] else "archive")
        self._filling = False
        self.pending.clear()
        self._reload_history()

    def _value(self, key: str):
        if key == "initial_reading":
            return parse_number(self.fields[key].text(), 3, optional=True)
        if key == "contract_date":
            return _date_value(self.contract_checked, self.contract_date)
        if key == "initial_reading_date":
            return _date_value(self.initial_checked, self.initial_date)
        return self.fields[key].text().strip()

    def _save_field(self, key: str) -> None:
        if self._filling or self.tenant_id is None or key not in self.pending:
            return
        self._save(key)

    def _save_date(self, key: str) -> None:
        if not self._filling and self.tenant_id is not None:
            self.pending.add(key)
            self._save(key)

    @guard_database_errors("Ошибка базы данных")
    def _save(self, key: str) -> None:
        try:
            value = self._value(key)
        except (ValueError, TypeError) as error:
            show_error(self, error, "Поле не сохранено")
            return
        with self.database.connection() as conn:
            record = db.get_tenant(conn, self.tenant_id)
        if record[key] == value:
            self.pending.discard(key)
            self.note.setText("Все изменения сохранены")
            return
        if key in ("initial_reading", "initial_reading_date"):
            with self.database.connection() as conn:
                calculations = db.list_calculations(conn, tenant_id=self.tenant_id)
            if calculations and QMessageBox.question(
                self, "Исходные показания",
                "Есть существующие расчёты. Изменение карточки не заменяет вручную "
                "указанные показания в истории. Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            ) != QMessageBox.StandardButton.Yes:
                return
        result = run_confirmed(self, lambda _flags: self.service.update_tenant(
            self.tenant_id, **{key: value}))
        if result is not CANCELLED:
            self.pending.discard(key)
            self.note.setText("Изменение сохранено")
            self._reload_history()

    @guard_database_errors("Ошибка базы данных")
    def _create(self) -> None:
        try:
            details = {key: self._value(key) for key, _ in TENANT_FIELDS}
            details["contract_date"] = self._value("contract_date")
            details["initial_reading_date"] = self._value("initial_reading_date")
        except (ValueError, TypeError) as error:
            show_error(self, error, "Некорректные реквизиты")
            return
        name = details.pop("name")
        result = run_confirmed(self, lambda _flags: self.service.create_tenant(
            name=name, **details))
        if result is CANCELLED:
            return
        self.tenant_id = result["id"]
        self.create_button.hide()
        self.archive_button.show()
        self.history.setEnabled(True)
        self.note.setText("Арендатор создан. Дальнейшие изменения сохраняются автоматически.")
        self._load()

    @guard_database_errors("Ошибка базы данных")
    def _toggle_archive(self) -> None:
        if self.pending:
            QMessageBox.warning(
                self, "Несохранённые реквизиты",
                "Сначала сохраните или исправьте незавершённые поля перед архивированием.")
            return
        with self.database.connection() as conn:
            record = db.get_tenant(conn, self.tenant_id)
        next_archived = not record["is_archived"]
        action = "переместить в архив" if next_archived else "восстановить"
        if QMessageBox.question(
            self, "Подтверждение", f"Вы хотите {action} этого арендатора?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        result = run_confirmed(self, lambda _flags: self.service.update_tenant(
            self.tenant_id, is_archived=next_archived))
        if result is not CANCELLED:
            self._load()

    @guard_database_errors("Ошибка базы данных")
    def _reload_history(self) -> None:
        if self.tenant_id is None:
            return
        with self.database.connection() as conn:
            calculations = db.list_calculations(conn, tenant_id=self.tenant_id)
        self.history.setRowCount(0)
        for calc in calculations:
            row = self.history.rowCount()
            self.history.insertRow(row)
            values = [calc["id"], f"{calc['month']:02d}.{calc['year']}",
                      f"{russian_date(calc['period_start'])} — {russian_date(calc['period_end'])}",
                      show_number(calc["consumption"]), show_number(calc["total_amount"]),
                      "Готов" if calc["is_ready"] else "Черновик"]
            for column, value in enumerate(values):
                self.history.setItem(row, column, QTableWidgetItem(str(value)))
        self.history.resizeColumnsToContents()

    @guard_database_errors("Ошибка базы данных")
    def _open_calculation(self) -> None:
        row = self.history.currentRow()
        if row < 0:
            QMessageBox.information(self, "История", "Выберите расчёт из таблицы.")
            return
        calculation_id = int(self.history.item(row, 0).text())
        CalculationDialog(self.service, self.database, calculation_id=calculation_id, parent=self).exec()
        self._reload_history()

    def accept(self) -> None:
        if self.tenant_id is None and any(field.text().strip() for field in self.fields.values()):
            if QMessageBox.question(self, "Несозданная карточка",
                                    "Карточка ещё не создана. Закрыть без сохранения?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        if self.pending and QMessageBox.question(
            self, "Несохранённые поля",
            "Не все изменения удалось сохранить: " + ", ".join(sorted(self.pending)) +
            ". Закрыть и оставить их без сохранения?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        super().accept()

    def reject(self) -> None:
        self.accept()  # closing with X must not silently discard pending edits


class CalculationDialog(QDialog):
    """Edit fields independently, committing only validated edits through the service."""
    def __init__(self, service: CalculationService, database: db.Database,
                 calculation_id: int | None = None, year: int | None = None,
                 month: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self.service, self.database, self.calculation_id = service, database, calculation_id
        self.year, self.month = year, month
        self.pending: set[str] = set()
        self._filling = False
        self._initial_load = True
        if calculation_id is not None:
            record = self.service.get_calculation(calculation_id)
            self.year, self.month = record["year"], record["month"]
        self._loaded_calculation = None
        self._current_tariff = None
        self._start_proposal = None
        self.setWindowTitle("Расчёт электроэнергии")
        self.resize(780, 720)
        outer = QVBoxLayout(self)
        outer.setSpacing(12)
        title = QLabel("Редактирование расчёта" if calculation_id else "Новый расчёт")
        title.setProperty("kontaktHeadingSize", 20)
        outer.addWidget(title)
        period_note = QLabel(month_label(self.year, self.month))
        period_note.setObjectName("mutedLabel")
        outer.addWidget(period_note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)
        form_content = QWidget()
        contents = QVBoxLayout(form_content)
        contents.setContentsMargins(7, 6, 7, 14)
        contents.setSpacing(13)

        def section(title: str, description: str = ""):
            card = QFrame()
            card.setObjectName("formCard")
            card.setStyleSheet(
                "QFrame#formCard {background:#fffefa;border:1px solid #dfe2df;"
                "border-radius:12px;} QLabel {border:0;background:transparent;}")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(19, 17, 19, 18)
            layout.setSpacing(11)
            heading = QLabel(title)
            heading.setProperty("kontaktHeadingSize", 19)
            layout.addWidget(heading)
            if description:
                hint = QLabel(description)
                hint.setObjectName("mutedLabel")
                hint.setWordWrap(True)
                layout.addWidget(hint)
            contents.addWidget(card)
            return layout

        period_group = section(
            "Арендатор и расчётный период",
            "Обе даты должны находиться внутри выбранного месяца. "
            "Пересекающиеся периоды допускаются после подтверждения.")
        period_form = QFormLayout()
        period_form.setSpacing(11)
        self.tenant_picker = QComboBox()
        self._load_tenants()
        self.tenant_picker.currentIndexChanged.connect(self._suggest_start)
        period_form.addRow("Арендатор", self.tenant_picker)
        date_start = QDate(self.year, self.month, 1)
        date_end = QDate(self.year, self.month, calendar.monthrange(self.year, self.month)[1])
        self.date_start = QDateEdit(date_start)
        self.date_end = QDateEdit(date_end)
        for picker in (self.date_start, self.date_end):
            picker.setCalendarPopup(True)
            picker.setDisplayFormat("dd.MM.yyyy")
            picker.userDateChanged.connect(lambda _d: self._save_period())
        date_grid = QGridLayout()
        date_grid.setSpacing(10)
        date_grid.addWidget(QLabel("Начало периода"), 0, 0)
        date_grid.addWidget(QLabel("Конец периода"), 0, 1)
        date_grid.addWidget(self.date_start, 1, 0)
        date_grid.addWidget(self.date_end, 1, 1)
        period_form.addRow(date_grid)
        period_group.addLayout(period_form)

        readings_group = section("Показания счётчика")
        readings = QGridLayout()
        readings.setSpacing(10)
        self.start_edit, self.end_edit = _line(), _line()
        self.start_edit.setPlaceholderText("0,000 · автоподстановка при создании")
        self.end_edit.setPlaceholderText("0,000 · можно оставить пустым")
        readings.addWidget(QLabel("Начальные, кВт·ч"), 0, 0)
        readings.addWidget(QLabel("Конечные, кВт·ч"), 0, 1)
        readings.addWidget(self.start_edit, 1, 0)
        readings.addWidget(self.end_edit, 1, 1)
        readings_group.addLayout(readings)
        self.start_hint = QLabel()
        self.start_hint.setObjectName("mutedLabel")
        self.start_hint.setWordWrap(True)
        readings_group.addWidget(self.start_hint)
        for name, field in (("start_reading", self.start_edit), ("end_reading", self.end_edit)):
            field.textEdited.connect(lambda _v, key=name: self._mark_pending(key))
            field.editingFinished.connect(lambda key=name: self._save_reading(key))

        corrections = section(
            "Ручные корректировки",
            "Автоматические значения рассчитываются из показаний и тарифа. "
            "Ручное изменение требует подтверждения.")
        manual_grid = QGridLayout()
        manual_grid.setSpacing(12)
        self.manual_consumption_check = QCheckBox("Изменить расход вручную")
        self.manual_amount_check = QCheckBox("Изменить сумму вручную")
        self.manual_consumption_edit = _line()
        self.manual_amount_edit = _line()
        self.manual_consumption_edit.setObjectName("manualConsumptionInput")
        self.manual_amount_edit.setObjectName("manualAmountInput")
        self.manual_consumption_edit.setPlaceholderText("Например, 130,000")
        self.manual_amount_edit.setPlaceholderText("Например, 1500,00")
        self.manual_consumption_edit.setEnabled(False)
        self.manual_amount_edit.setEnabled(False)
        manual_grid.addWidget(self.manual_consumption_check, 0, 0)
        manual_grid.addWidget(self.manual_amount_check, 0, 1)
        manual_grid.addWidget(self.manual_consumption_edit, 1, 0)
        manual_grid.addWidget(self.manual_amount_edit, 1, 1)
        corrections.addLayout(manual_grid)
        tip = QLabel(
            "Изменение показаний сбрасывает ручные расход и сумму. "
            "Изменение расхода или тарифа сбрасывает ручную сумму.")
        tip.setWordWrap(True)
        tip.setObjectName("mutedLabel")
        corrections.addWidget(tip)

        preview = QFrame()
        preview.setObjectName("calculationPreview")
        preview.setStyleSheet(
            "QFrame#calculationPreview {background:#f2f4ee;border:1px solid #dce3dc;"
            "border-radius:12px;} QFrame#calculationPreview QLabel {border:0;background:transparent;}")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(19, 17, 19, 16)
        preview_layout.setSpacing(11)
        preview_heading = QLabel("Предварительный расчёт")
        preview_heading.setProperty("kontaktHeadingSize", 19)
        preview_layout.addWidget(preview_heading)
        preview_grid = QGridLayout()
        preview_grid.setSpacing(10)
        self.consumption = QLabel("—")
        self.tariff_label = QLabel("—")
        self.amount = QLabel("—")
        for col, (caption, value) in enumerate((
            ("Расход", self.consumption),
            ("Тариф месяца", self.tariff_label),
            ("К оплате", self.amount),
        )):
            label = QLabel(caption)
            label.setObjectName("mutedLabel")
            value.setProperty("kontaktHeadingSize", 19)
            value.setWordWrap(True)
            preview_grid.addWidget(label, 0, col)
            preview_grid.addWidget(value, 1, col)
        preview_layout.addLayout(preview_grid)
        self.preview_status = StatusChip("Черновик", "neutral")
        preview_layout.addWidget(self.preview_status)
        self.preview_details = QLabel("")
        self.preview_details.setObjectName("mutedLabel")
        self.preview_details.setWordWrap(True)
        preview_layout.addWidget(self.preview_details)
        contents.addWidget(preview)

        actions = QGridLayout()
        self.open_tenant_button = self._button("Карточка арендатора", self._open_tenant)
        actions.addWidget(self.open_tenant_button, 0, 0)
        actions.addWidget(self._button("PDF акта / счёта", self._export), 0, 1)
        contents.addLayout(actions)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(
            "background:#f0f1ec;color:#3a5155;padding:11px;border-radius:8px;")
        contents.addWidget(self.status)
        scroll.setWidget(form_content)

        footer = QHBoxLayout()
        self.delete_button = self._button("Удалить", self._delete)
        footer.addWidget(self.delete_button)
        footer.addStretch()
        self.create_button = self._button("Создать расчёт", self._create)
        footer.addWidget(self.create_button)
        footer.addWidget(self._button("Готово", self.accept))
        outer.addLayout(footer)

        for field in (self.start_edit, self.end_edit,
                      self.manual_consumption_edit, self.manual_amount_edit):
            field.textChanged.connect(self._update_preview)
        for picker in (self.date_start, self.date_end):
            picker.dateChanged.connect(self._update_preview)
        self.manual_consumption_check.toggled.connect(
            lambda enabled: self._manual_toggled("consumption", enabled))
        self.manual_amount_check.toggled.connect(
            lambda enabled: self._manual_toggled("total_amount", enabled))
        self.manual_consumption_edit.textEdited.connect(
            lambda _v: self._mark_pending("consumption"))
        self.manual_amount_edit.textEdited.connect(
            lambda _v: self._mark_pending("total_amount"))
        self.manual_consumption_edit.editingFinished.connect(
            lambda: self._save_manual("consumption"))
        self.manual_amount_edit.editingFinished.connect(
            lambda: self._save_manual("total_amount"))
        self._refresh()
        self._initial_load = False

    @staticmethod
    def _button(label, callback):
        control = QPushButton(label)
        symbol = {
            "Карточка арендатора": "tenants",
            "Ручной расход": "edit",
            "Ручная сумма": "edit",
            "PDF акта / счёта": "file",
            "Удалить": "trash",
            "Создать расчёт": "plus",
            "Готово": "check",
        }.get(label)
        if symbol:
            icon_button(control, symbol)
        control.clicked.connect(callback)
        return control

    @guard_database_errors("Ошибка базы данных")
    def _load_tenants(self) -> None:
        # Read first: a locked database must not erase the old tenant choices
        # or leave Qt signals blocked forever.
        with self.database.connection() as conn:
            tenants = db.list_tenants(conn, include_archived=True)
        if self.calculation_id is None:
            tenants = [t for t in tenants if not t["is_archived"]]
        self.tenant_picker.blockSignals(True)
        try:
            self.tenant_picker.clear()
            for record in tenants:
                self.tenant_picker.addItem(record["name"], record["id"])
        finally:
            self.tenant_picker.blockSignals(False)

    def _mark_pending(self, name: str) -> None:
        if self.calculation_id is not None and not self._filling:
            self.pending.add(name)
            self.status.setText("Есть ещё не сохранённые изменения поля.")

    @guard_database_errors("Ошибка базы данных")
    def _suggest_start(self) -> None:
        if self.calculation_id is not None or self.tenant_picker.currentData() is None:
            return
        try:
            proposal = self.service.suggest_start_reading(
                self.tenant_picker.currentData(), self.year, self.month,
                self.date_start.date().toString("yyyy-MM-dd"))
            self._start_proposal = proposal
            if proposal.value is None:
                self.start_hint.setText(
                    "Автоматический источник не найден. Укажите показание вручную.")
            else:
                suffix = " (потребуется подтверждение)" if proposal.requires_confirmation else ""
                self.start_hint.setText(
                    "Автоподстановка при создании: " + show_number(proposal.value)
                    + suffix + ". Начальные показания можно изменить вручную.")
        except (LookupError, ValueError) as error:
            self._start_proposal = None
            self.start_hint.setText(str(error))
        if hasattr(self, "preview_status"):
            self._update_preview()

    def _update_preview(self, *_ignored) -> None:
        """Read the visible draft and calculate live with pure Decimal domain code.

        This must never write SQLite or claim that a draft is already saved.
        The backend service remains authoritative for persistence and export.
        """
        if self._filling or not hasattr(self, "preview_status"):
            return
        tariff = self._current_tariff
        try:
            start = parse_number(self.start_edit.text(), 3, optional=True)
            end = parse_number(self.end_edit.text(), 3, optional=True)
            if start is None and self.calculation_id is None and self._start_proposal:
                start = self._start_proposal.value
            use_consumption = self.manual_consumption_check.isChecked()
            use_amount = self.manual_amount_check.isChecked()
            consumption = (parse_number(self.manual_consumption_edit.text(), 3)
                           if use_consumption else None)
            amount = (parse_number(self.manual_amount_edit.text(), 2)
                      if use_amount else None)
            consumption, amount = recalculate(
                start, end, tariff, consumption=consumption, amount=amount,
                manual_consumption=use_consumption, manual_amount=use_amount)
            draft = {
                "year": self.year, "month": self.month,
                "period_start": self.date_start.date().toString("yyyy-MM-dd"),
                "period_end": self.date_end.date().toString("yyyy-MM-dd"),
                "start_reading": start, "end_reading": end,
                "consumption": consumption, "total_amount": amount,
                "manual_consumption": use_consumption, "manual_amount": use_amount,
            }
            validation = validate_calculation(draft, tariff)
            self.consumption.setText(display_number(consumption, 3, "кВт·ч", trim=True))
            self.tariff_label.setText(display_number(tariff, 4, "₽", trim=True))
            self.amount.setText(display_number(amount, 2, "₽"))
            self.preview_status.setText("Готов" if validation.ready else "Черновик")
            self.preview_status.set_tone("ok" if validation.ready else "warn")
            details = validation.errors + validation.warnings
            self.preview_details.setText(
                ("Предпросмотр, не сохранено. " if self.calculation_id is None or self.pending else "")
                + ("; ".join(details) if details else "Все обязательные значения указаны."))
        except (ValueError, TypeError, ArithmeticError) as error:
            self.consumption.setText("—")
            self.tariff_label.setText(display_number(tariff, 4, "₽", trim=True))
            self.amount.setText("—")
            self.preview_status.setText("Некорректный ввод")
            self.preview_status.set_tone("bad")
            self.preview_details.setText(
                "Предварительный расчёт недоступен: " + str(error))

    def _manual_controls(self, field: str):
        if field == "consumption":
            return self.manual_consumption_check, self.manual_consumption_edit, 3
        if field == "total_amount":
            return self.manual_amount_check, self.manual_amount_edit, 2
        raise ValueError("Неизвестная ручная корректировка.")

    @guard_database_errors("Ошибка базы данных")
    def _manual_toggled(self, field: str, enabled: bool) -> None:
        checkbox, editor, _places = self._manual_controls(field)
        editor.setEnabled(enabled)
        if self._filling:
            return
        current = self._loaded_calculation
        if enabled:
            if not editor.text().strip():
                # As in HTML, suggest the current automatic value, but do not
                # write anything until a meaningful manual edit is confirmed.
                suggested = (self.consumption.text().split(" ", 1)[0]
                             if field == "consumption" else self.amount.text().split(" ", 1)[0])
                if suggested != "—":
                    editor.setText(suggested.replace("\u202f", ""))
            if self.calculation_id is not None:
                self.pending.add(field)
            self._update_preview()
            return
        if self.calculation_id is not None and current and current[
                "manual_consumption" if field == "consumption" else "manual_amount"]:
            if self.pending - {field}:
                QMessageBox.warning(
                    self, "Несохранённые изменения",
                    "Завершите другие изменения перед сбросом ручной корректировки.")
                checkbox.blockSignals(True)
                checkbox.setChecked(True)
                checkbox.blockSignals(False)
                editor.setEnabled(True)
                return
            result = run_confirmed(
                self, lambda _flags: self.service.reset_manual_override(
                    self.calculation_id, field))
            if result is CANCELLED:
                checkbox.blockSignals(True)
                checkbox.setChecked(True)
                checkbox.blockSignals(False)
                editor.setEnabled(True)
                return
            self.pending.discard(field)
            self._refresh()
            return
        self.pending.discard(field)
        editor.clear()
        self._update_preview()

    @guard_database_errors("Ошибка базы данных")
    def _save_manual(self, field: str) -> None:
        checkbox, editor, places = self._manual_controls(field)
        if self._filling or self.calculation_id is None or not checkbox.isChecked():
            return
        if field not in self.pending:
            return
        if self.pending - {"consumption", "total_amount", field}:
            self.status.setText(
                "Завершите изменения показаний или дат перед ручной корректировкой.")
            return
        try:
            value = parse_number(editor.text(), places)
        except (ValueError, TypeError) as error:
            show_error(self, error, "Некорректное ручное значение")
            return
        current = self.service.get_calculation(self.calculation_id)
        flag = "manual_consumption" if field == "consumption" else "manual_amount"
        if value == current[field]:
            # The service deliberately does not flag an unchanged automatic
            # value as a manual correction. Never claim otherwise.
            self.pending.discard(field)
            if not current[flag]:
                checkbox.blockSignals(True)
                checkbox.setChecked(False)
                checkbox.blockSignals(False)
                editor.setEnabled(False)
                editor.clear()
            self._update_preview()
            return
        result = run_confirmed(self, lambda flags: self.service.update_calculation(
            self.calculation_id, **{field: value}, **flags))
        if result is CANCELLED:
            self.status.setText("Ручная корректировка не сохранена. Можно исправить и повторить.")
            return
        self.pending.discard(field)
        self._refresh()

    @guard_database_errors("Ошибка базы данных")
    def _create(self) -> None:
        tenant_id = self.tenant_picker.currentData()
        if tenant_id is None:
            QMessageBox.warning(self, "Арендатор отсутствует",
                                "Сначала создайте арендатора в справочнике.")
            return
        try:
            start = parse_number(self.start_edit.text(), 3, optional=True)
            end = parse_number(self.end_edit.text(), 3, optional=True)
            # Reject invalid manual drafts before creating a partial record.
            if self.manual_consumption_check.isChecked():
                parse_number(self.manual_consumption_edit.text(), 3)
            if self.manual_amount_check.isChecked():
                parse_number(self.manual_amount_edit.text(), 2)
        except (TypeError, ValueError) as error:
            show_error(self, error, "Некорректный ввод расчёта")
            return
        result = run_confirmed(self, lambda flags: self.service.create_calculation(
            tenant_id, self.year, self.month,
            period_start=self.date_start.date().toString("yyyy-MM-dd"),
            period_end=self.date_end.date().toString("yyyy-MM-dd"),
            start_reading=start, end_reading=end, **flags))
        if result is CANCELLED:
            return
        manual_drafts = [
            key for key, checked in (
                ("consumption", self.manual_consumption_check.isChecked()),
                ("total_amount", self.manual_amount_check.isChecked()),
            ) if checked
        ]
        self.calculation_id = result["id"]
        self.tenant_picker.setEnabled(False)
        self.create_button.hide()
        self.delete_button.show()
        self.pending.clear()
        self.pending.update(manual_drafts)
        self._refresh()
        for field in manual_drafts:
            if field == "total_amount" and "consumption" in self.pending:
                # Never calculate the final override using a consumption
                # correction that the user has not confirmed/saved.
                break
            self._save_manual(field)
        if self.pending:
            self.status.setText(
                "Расчёт создан, но ручные корректировки ещё не сохранены. "
                "Завершите ввод и подтвердите изменения.")
        else:
            self.status.setText(
                "Расчёт создан и сохранён. Дальнейшие изменения сохраняются автоматически.")

    @guard_database_errors("Ошибка загрузки расчёта")
    def _refresh(self) -> None:
        """Read everything first; keep unsaved fields after a failed operation."""
        unsaved = set(self.pending)
        unsaved_start, unsaved_end = self.start_edit.text(), self.end_edit.text()
        unsaved_dates = (self.date_start.date(), self.date_end.date())
        unsaved_manual = {
            key: (checkbox.isChecked(), field.text())
            for key, checkbox, field in (
                ("consumption", self.manual_consumption_check, self.manual_consumption_edit),
                ("total_amount", self.manual_amount_check, self.manual_amount_edit),
            )
        }
        calc = checked = None
        if self.calculation_id is not None:
            calc = self.service.get_calculation(self.calculation_id)
            checked = self.service.validate(self.calculation_id)
        with self.database.connection() as conn:
            tariff = queries.month_tariff(conn, self.year, self.month)
        self._loaded_calculation = calc
        self._current_tariff = tariff["rate"] if tariff else None
        self._filling = True
        try:
            self.create_button.setVisible(calc is None)
            self.delete_button.setVisible(calc is not None)
            if calc is not None:
                index = self.tenant_picker.findData(calc["tenant_id"])
                self.tenant_picker.setCurrentIndex(index)
                self.tenant_picker.setEnabled(False)
                self.date_start.setDate(QDate.fromString(calc["period_start"], "yyyy-MM-dd"))
                self.date_end.setDate(QDate.fromString(calc["period_end"], "yyyy-MM-dd"))
                self.start_edit.setText(show_number(calc["start_reading"]))
                self.end_edit.setText(show_number(calc["end_reading"]))
                details = checked.warnings + checked.errors
                self.status.setText(
                    ("Готов к экспорту" if checked.ready else "Черновик") +
                    ("\n" + "\n".join(details) if details else ""))
                self.start_hint.setText({
                    "manual": "Начальные показания введены вручную.",
                    "previous_month": "Из предыдущего месяца.",
                    "initial": "Из карточки арендатора.",
                    "unspecified": "Источник начальных показаний не задан.",
                }.get(calc["start_reading_source"], ""))
            else:
                self.status.setText(
                    "Новый расчёт будет сохранён как черновик, "
                    "даже если конечное показание ещё не указано.")

            for key, flag, checkbox, editor in (
                ("consumption", "manual_consumption",
                 self.manual_consumption_check, self.manual_consumption_edit),
                ("total_amount", "manual_amount",
                 self.manual_amount_check, self.manual_amount_edit),
            ):
                enabled = bool(calc and calc[flag])
                checkbox.setChecked(enabled)
                editor.setEnabled(enabled)
                editor.setText(show_number(calc[key]) if enabled else "")
                if key in unsaved:
                    pending_checked, pending_text = unsaved_manual[key]
                    checkbox.setChecked(pending_checked)
                    editor.setEnabled(pending_checked)
                    editor.setText(pending_text)
            if calc is not None and unsaved:
                if "start_reading" in unsaved:
                    self.start_edit.setText(unsaved_start)
                if "end_reading" in unsaved:
                    self.end_edit.setText(unsaved_end)
                if "period" in unsaved:
                    self.date_start.setDate(unsaved_dates[0])
                    self.date_end.setDate(unsaved_dates[1])
                self.status.setText(
                    self.status.text() +
                    "\nЕсть несохранённые изменения: " + ", ".join(sorted(unsaved)))
        finally:
            self._filling = False
        if calc is None:
            self._suggest_start()
        self._update_preview()

    @guard_database_errors("Ошибка базы данных")
    def _save_reading(self, key: str) -> None:
        if self.calculation_id is None or self._filling or key not in self.pending:
            return
        field = self.start_edit if key == "start_reading" else self.end_edit
        try:
            value = parse_number(field.text(), 3, optional=True)
        except (ValueError, TypeError) as error:
            show_error(self, error)
            return
        current = self.service.get_calculation(self.calculation_id)
        if current[key] == value and (key != "start_reading" or current["start_reading_source"] == "manual"):
            self.pending.discard(key)
            self._refresh()
            return
        result = run_confirmed(self, lambda flags: self.service.update_calculation(
            self.calculation_id, **{key: value}, **flags))
        if result is CANCELLED:
            return
        self.pending.discard(key)
        self._refresh()

    @guard_database_errors("Ошибка базы данных")
    def _save_period(self) -> None:
        if self.calculation_id is None:
            self._suggest_start()
            return
        if self._filling:
            return
        # Mark the new date as pending BEFORE trying SQLite; otherwise a read
        # failure would allow the dialog to close and silently discard it.
        self.pending.add("period")
        start = self.date_start.date().toString("yyyy-MM-dd")
        end = self.date_end.date().toString("yyyy-MM-dd")
        current = self.service.get_calculation(self.calculation_id)
        changes = {}
        if start != current["period_start"]:
            changes["period_start"] = start
        if end != current["period_end"]:
            changes["period_end"] = end
        if not changes:
            self.pending.discard("period")
            return
        self.pending.add("period")
        if (start > end or start[:7] != f"{self.year:04d}-{self.month:02d}"
                or end[:7] != f"{self.year:04d}-{self.month:02d}"):
            self.status.setText("Даты должны находиться в одном месяце, начало не позже конца. Не сохранено.")
            return
        result = run_confirmed(self, lambda flags: self.service.update_calculation(
            self.calculation_id, **changes, **flags))
        if result is not CANCELLED:
            self.pending.discard("period")
            self._refresh()

    @guard_database_errors("Ошибка базы данных")
    def _manual(self, field: str, label: str, places: int) -> None:
        if self.pending:
            QMessageBox.warning(self, label, "Сначала сохраните или исправьте незавершённые поля.")
            return
        if self.calculation_id is None:
            QMessageBox.information(self, label, "Сначала создайте расчёт.")
            return
        from PySide6.QtWidgets import QInputDialog
        current = self.service.get_calculation(self.calculation_id)
        value, ok = QInputDialog.getText(self, label,
                                         f"{label} — новое значение:",
                                         QLineEdit.EchoMode.Normal,
                                         show_number(current[field]))
        if not ok:
            return
        try:
            number = parse_number(value, places)
        except (ValueError, TypeError) as error:
            show_error(self, error, "Некорректное число")
            return
        result = run_confirmed(self, lambda flags: self.service.update_calculation(
            self.calculation_id, **{field: number}, **flags))
        if result is not CANCELLED:
            self._refresh()

    @guard_database_errors("Ошибка базы данных")
    def _open_tenant(self) -> None:
        """Navigate directly from a calculation to its tenant and full history."""
        if self.pending:
            QMessageBox.warning(
                self, "Несохранённые изменения",
                "Сначала сохраните или исправьте незавершённые поля.")
            return
        tenant_id = self.tenant_picker.currentData()
        if tenant_id is None:
            QMessageBox.information(self, "Арендатор", "Выберите арендатора.")
            return
        try:
            dialog = TenantDialog(self.service, self.database, tenant_id=tenant_id, parent=self)
            dialog.exec()
            self._load_tenants()
            self._refresh()
        except (sqlite3.Error, OSError, LookupError, RuntimeError) as error:
            show_error(self, error, "Не удалось открыть арендатора")

    def _edit_consumption(self) -> None:
        self._manual("consumption", "Ручной расход", 3)

    def _edit_amount(self) -> None:
        self._manual("total_amount", "Ручная сумма", 2)

    @guard_database_errors("Ошибка базы данных")
    def _export(self) -> None:
        if self.pending:
            QMessageBox.warning(self, "PDF", "Есть несохранённые изменения. Исправьте их перед выгрузкой.")
            return
        if self.calculation_id is None:
            QMessageBox.information(self, "PDF", "Сначала создайте расчёт.")
            return
        ExportDialog(self.service, self.database, self.calculation_id, self).exec()
        self._refresh()

    def _delete(self) -> None:
        if self.calculation_id is None:
            return
        result = run_confirmed(self, lambda flags: self.service.delete_calculation(
            self.calculation_id, **flags))
        if result is not CANCELLED:
            self.calculation_id = None
            super().accept()

    def accept(self) -> None:
        if self.calculation_id is None and (
            self.start_edit.text().strip() or self.end_edit.text().strip()
        ):
            if QMessageBox.question(self, "Несозданный расчёт",
                                    "Расчёт ещё не создан. Закрыть и потерять введённые данные?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        if self.pending and QMessageBox.question(
            self, "Несохранённые изменения",
            "Не все изменения удалось сохранить. Закрыть без них?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        super().accept()

    def reject(self) -> None:
        self.accept()  # closing with X must not silently discard pending edits


class SettingsPage(QWidget):
    """Performer data in SQLite, purely visual preferences in QSettings."""
    def __init__(self, service: CalculationService, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self._filling = False
        self.pending: set[str] = set()
        self._initial_load = True
        self.setObjectName("workspace")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 20)
        outer.setSpacing(13)
        eyebrow = QLabel("ПАРАМЕТРЫ ДОКУМЕНТОВ")
        eyebrow.setObjectName("eyebrow")
        outer.addWidget(eyebrow)
        heading = QLabel("Настройки")
        heading.setProperty("kontaktHeadingSize", 27)
        outer.addWidget(heading)
        explanation = QLabel("Единый исполнитель, банковские реквизиты, подпись, печать и оформление.")
        explanation.setObjectName("subtitle")
        explanation.setWordWrap(True)
        outer.addWidget(explanation)

        tabs = QTabWidget()
        tabs.setObjectName("settingsTabs")
        outer.addWidget(tabs, 1)
        scrolling = QScrollArea()
        scrolling.setWidgetResizable(True)
        scrolling.setFrameShape(QFrame.Shape.NoFrame)
        performer = QWidget()
        performer_layout = QVBoxLayout(performer)
        performer_layout.setContentsMargins(8, 15, 8, 15)
        performer_layout.setSpacing(16)
        self.fields = {}

        def section(title: str, description: str = ""):
            card = QFrame()
            card.setObjectName("formCard")
            content = QVBoxLayout(card)
            content.setContentsMargins(22, 19, 22, 20)
            content.setSpacing(12)
            title_label = QLabel(title)
            title_label.setProperty("kontaktHeadingSize", 19)
            content.addWidget(title_label)
            if description:
                hint = QLabel(description)
                hint.setObjectName("mutedLabel")
                hint.setWordWrap(True)
                content.addWidget(hint)
            performer_layout.addWidget(card)
            return content

        def add_fields(content, choices):
            grid = QFormLayout()
            grid.setSpacing(12)
            grid.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            for key, label in choices:
                field = _line()
                field.setObjectName("settings_" + key)
                field.setMinimumWidth(130)
                field.editingFinished.connect(lambda name=key: self._save_field(name))
                field.textEdited.connect(lambda _text, name=key: self.pending.add(name))
                self.fields[key] = field
                grid.addRow(label, field)
            content.addLayout(grid)

        add_fields(section(
            "Исполнитель", "Реквизиты автоматически используются во всех новых и повторно "
            "сформированных документах."), SETTINGS_FIELDS[:5])
        add_fields(section("Банковские реквизиты"), SETTINGS_FIELDS[5:])

        images_layout = section(
            "Подпись и печать",
            "Изображения PNG/JPG необязательны. После загрузки будут доступны в PDF.")
        self.images = {}
        self.image_previews = {}
        for key, title in (("signature_path", "Подпись исполнителя"),
                           ("stamp_path", "Печать исполнителя")):
            row = QHBoxLayout()
            row.setSpacing(15)
            preview = QLabel("Не загружено")
            preview.setObjectName("imagePreview")
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setMinimumSize(120, 85)
            preview.setMaximumWidth(180)
            preview.setWordWrap(True)
            preview.setStyleSheet(
                "border:1px dashed #cfd6d1;background:#f9faf6;"
                "border-radius:9px;padding:8px;color:#657276;")
            self.image_previews[key] = preview
            row.addWidget(preview)
            controls = QVBoxLayout()
            controls.addWidget(QLabel(title))
            info = QLabel("Не загружено")
            info.setObjectName("mutedLabel")
            info.setWordWrap(True)
            controls.addWidget(info)
            buttons = QHBoxLayout()
            select = QPushButton("Загрузить PNG/JPG")
            icon_button(select, "download")
            select.clicked.connect(lambda _=False, name=key: self._select_image(name))
            remove = QPushButton("Удалить")
            icon_button(remove, "trash")
            remove.clicked.connect(lambda _=False, name=key: self._remove_image(name))
            buttons.addWidget(select)
            buttons.addWidget(remove)
            controls.addLayout(buttons)
            self.images[key] = (info, remove)
            row.addLayout(controls, 1)
            images_layout.addLayout(row)

        reuse_layout = section(
            "Начальные показания",
            "Для редких случаев без достоверной истории предыдущего месяца.")
        self.reuse = QCheckBox("Переиспользовать вручную заданные начальные показания")
        self.reuse.toggled.connect(self._save_reuse)
        reuse_layout.addWidget(self.reuse)
        performer_layout.addStretch()
        scrolling.setWidget(performer)
        tabs.addTab(scrolling, "Исполнитель")

        appearance_scroll = QScrollArea()
        appearance_scroll.setWidgetResizable(True)
        appearance_scroll.setFrameShape(QFrame.Shape.NoFrame)
        appearance = QWidget()
        appearance_body = QVBoxLayout(appearance)
        appearance_body.setContentsMargins(8, 15, 8, 15)
        appearance_body.setSpacing(17)
        controls_card = QFrame()
        controls_card.setObjectName("formCard")
        sliders = QVBoxLayout(controls_card)
        sliders.setContentsMargins(22, 19, 22, 20)
        sliders.setSpacing(17)
        heading = QLabel("Текст интерфейса")
        heading.setProperty("kontaktHeadingSize", 20)
        sliders.addWidget(heading)
        intro = QLabel(
            "Размер и насыщенность настраиваются независимо. "
            "Изменения видны сразу и сохраняются на этом компьютере.")
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        sliders.addWidget(intro)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Размер текста"), 1)
        self.size_value = QLabel("100%")
        self.size_value.setObjectName("fontValue")
        size_row.addWidget(self.size_value)
        size_reset = QPushButton("Сбросить размер")
        icon_button(size_reset, "reset")
        size_reset.clicked.connect(lambda: self.size_slider.setValue(100))
        size_row.addWidget(size_reset)
        sliders.addLayout(size_row)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setObjectName("fontSizeSlider")
        self.size_slider.setRange(80, 200)
        self.size_slider.setSingleStep(5)
        self.size_slider.setPageStep(10)
        self.size_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.size_slider.setTickInterval(20)
        self.size_slider.setAccessibleName("Масштаб интерфейсного текста, проценты")
        self.size_slider.valueChanged.connect(
            lambda value: self._change_appearance("font_size_percent", value))
        sliders.addWidget(self.size_slider)
        scale_marks = QHBoxLayout()
        for name in ("80%", "100% · стандарт", "200%"):
            label = QLabel(name)
            label.setObjectName("mutedLabel")
            scale_marks.addWidget(label)
        sliders.addLayout(scale_marks)

        weight_row = QHBoxLayout()
        weight_row.addWidget(QLabel("Насыщенность текста"), 1)
        self.weight_value = QLabel("400")
        self.weight_value.setObjectName("fontValue")
        weight_row.addWidget(self.weight_value)
        weight_reset = QPushButton("Сбросить насыщенность")
        icon_button(weight_reset, "reset")
        weight_reset.clicked.connect(lambda: self.weight_slider.setValue(400))
        weight_row.addWidget(weight_reset)
        sliders.addLayout(weight_row)
        self.weight_slider = QSlider(Qt.Orientation.Horizontal)
        self.weight_slider.setObjectName("fontWeightSlider")
        self.weight_slider.setRange(300, 800)
        self.weight_slider.setSingleStep(100)
        self.weight_slider.setPageStep(100)
        self.weight_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.weight_slider.setTickInterval(100)
        self.weight_slider.setAccessibleName("Насыщенность интерфейсного текста, CSS font-weight")
        self.weight_slider.valueChanged.connect(
            lambda value: self._change_appearance("font_weight_value", value))
        sliders.addWidget(self.weight_slider)
        weight_marks = QHBoxLayout()
        weight_marks.addWidget(QLabel("300 · Лёгкий"))
        weight_marks.addStretch()
        weight_marks.addWidget(QLabel("800 · Очень жирный"))
        sliders.addLayout(weight_marks)
        self.weight_effective = QLabel()
        self.weight_effective.setObjectName("mutedLabel")
        self.weight_effective.setWordWrap(True)
        sliders.addWidget(self.weight_effective)
        reset_all = QPushButton("Вернуть стандартное оформление")
        icon_button(reset_all, "reset")
        reset_all.clicked.connect(self._reset_appearance)
        sliders.addWidget(reset_all)
        appearance_body.addWidget(controls_card)

        preview = QFrame()
        preview.setObjectName("previewCard")
        preview_body = QVBoxLayout(preview)
        preview_body.setContentsMargins(23, 20, 23, 22)
        preview_body.setSpacing(9)
        kicker = QLabel("ПРЕДПРОСМОТР · ИНТЕРАКТИВНО")
        kicker.setObjectName("eyebrow")
        preview_body.addWidget(kicker)
        self.preview_title = QLabel("Расчёты электроэнергии")
        self.preview_title.setProperty("kontaktHeadingSize", 20)
        self.preview_title.setWordWrap(True)
        preview_body.addWidget(self.preview_title)
        self.preview_text = QLabel(
            "Показания, тарифы, акты и счета отображаются в выбранном размере и начертании.")
        self.preview_text.setWordWrap(True)
        preview_body.addWidget(self.preview_text)
        self.preview_number = QLabel("37 638,00 ₽ · 3 400,000 кВт·ч")
        self.preview_number.setWordWrap(True)
        preview_body.addWidget(self.preview_number)
        self.preview_status = StatusChip("Готов к формированию", "ok")
        preview_body.addWidget(self.preview_status)
        appearance_body.addWidget(preview)
        note = QLabel(
            "Это только оформление интерфейса. Расчёты, статусы и PDF не изменяются. "
            "На слабых системных шрифтах некоторые соседние значения насыщенности "
            "могут использовать одно и то же доступное начертание.")
        note.setWordWrap(True)
        note.setObjectName("mutedLabel")
        appearance_body.addWidget(note)
        appearance_body.addStretch()
        appearance_scroll.setWidget(appearance)
        tabs.addTab(appearance_scroll, "Оформление")

        self.note = QLabel("Все изменения реквизитов сохраняются после выхода из поля.")
        self.note.setObjectName("mutedLabel")
        self.note.setWordWrap(True)
        outer.addWidget(self.note)
        self.reload()
        self._initial_load = False

    @guard_database_errors("Ошибка базы данных")
    def reload(self) -> None:
        with self.service.database.connection() as conn:
            record = db.get_settings(conn)
        self._filling = True
        for key, field in self.fields.items():
            if key not in self.pending:
                field.setText(record[key] or "")
        self.reuse.setChecked(bool(record["reuse_manual_start_readings"]))
        for key, (label, remove) in self.images.items():
            path = record[key]
            label.setText(Path(path).name if path else "Не загружено")
            label.setToolTip(str(path or ""))
            remove.setEnabled(bool(path))
            preview = self.image_previews[key]
            if path:
                picture = QPixmap(str(path))
                if not picture.isNull():
                    preview.setPixmap(picture.scaled(
                        150, 75, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
                    preview.setToolTip(str(path))
                else:
                    preview.setPixmap(QPixmap())
                    preview.setText("Изображение недоступно")
            else:
                preview.setPixmap(QPixmap())
                preview.setText("Не загружено")
        size, weight = read_appearance()
        self.size_slider.blockSignals(True)
        self.weight_slider.blockSignals(True)
        try:
            self.size_slider.setValue(size)
            self.weight_slider.setValue(weight)
        finally:
            self.size_slider.blockSignals(False)
            self.weight_slider.blockSignals(False)
        self._update_appearance_labels()
        self._filling = False

    @guard_database_errors("Ошибка базы данных")
    def _save_field(self, key: str) -> None:
        if self._filling or key not in self.pending:
            return
        value = self.fields[key].text().strip()
        with self.service.database.connection() as conn:
            previous = db.get_settings(conn)[key]
        if previous == value:
            self.pending.discard(key)
            return
        result = run_confirmed(self, lambda _flags: self.service.update_settings(
            **{key: value}))
        if result is not CANCELLED:
            self.pending.discard(key)
            self.note.setText("Реквизиты сохранены. Ранее сформированные документы требуют обновления.")
        else:
            self.note.setText("Поле не сохранено. Исправьте ошибку перед уходом со страницы.")

    @guard_database_errors("Ошибка базы данных")
    def _save_reuse(self, enabled: bool) -> None:
        if self._filling:
            return
        result = run_confirmed(self, lambda _flags: self.service.update_settings(
            reuse_manual_start_readings=enabled))
        if result is CANCELLED:
            self.reload()

    @guard_database_errors("Ошибка базы данных")
    def _select_image(self, key: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать изображение", "", "Изображения (*.png *.jpg *.jpeg)")
        if not path:
            return
        uploaded = None
        try:
            with self.service.database.connection() as conn:
                previous = db.get_settings(conn)[key]
            uploaded = import_image(path, "signature" if key == "signature_path" else "stamp")
            result = self.service.update_settings(**{key: str(uploaded)})
            if result is None:
                raise RuntimeError("Изображение не сохранено.")
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            if uploaded is not None:
                uploaded.unlink(missing_ok=True)
            show_error(self, error, "Ошибка загрузки изображения")
            return
        self.reload()
        try:
            remove_managed_image(previous)
        except OSError as error:
            QMessageBox.warning(self, "Очистка старого изображения", str(error))
        self.note.setText("Изображение сохранено в локальной папке приложения.")

    @guard_database_errors("Ошибка базы данных")
    def _remove_image(self, key: str) -> None:
        if QMessageBox.question(
            self, "Удаление изображения", "Удалить изображение из настроек исполнителя?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        with self.service.database.connection() as conn:
            previous = db.get_settings(conn)[key]
        result = run_confirmed(self, lambda _flags: self.service.update_settings(
            **{key: None}))
        if result is not CANCELLED:
            self.reload()
            try:
                remove_managed_image(previous)
            except OSError as error:
                QMessageBox.warning(self, "Очистка изображения",
                                    "Ссылка удалена, но файл остался: " + str(error))

    def _update_appearance_labels(self) -> None:
        size, weight = read_appearance()
        self.size_value.setText(f"{size}%")
        self.weight_value.setText(str(weight))
        available = available_font_weights()
        effective = effective_font_weight(weight, available)
        if effective == weight:
            self.weight_effective.setText(
                f"Выбрано начертание {effective}. Доступные веса текущего шрифта: "
                + ", ".join(map(str, available)) + ".")
        else:
            self.weight_effective.setText(
                f"Запрошено {weight}; системный шрифт предоставляет ближайшее "
                f"начертание {effective}. Доступно: "
                + ", ".join(map(str, available)) + ".")

    def _change_appearance(self, key: str, value: int) -> None:
        if self._filling or key not in ("font_size_percent", "font_weight_value"):
            return
        number = int(value)
        if key == "font_size_percent" and not (80 <= number <= 200 and number % 5 == 0):
            return
        if key == "font_weight_value" and not (300 <= number <= 800 and number % 100 == 0):
            return
        settings = appearance_settings()
        settings.setValue(key, number)
        settings.sync()
        if settings.status() != type(settings).Status.NoError:
            QMessageBox.warning(self, "Оформление", "Не удалось сохранить настройку оформления.")
        apply_appearance()
        self._update_appearance_labels()
        self.note.setText("Оформление применено. Статус документов не изменён.")
        parent = self.window()
        if hasattr(parent, "_reflow_layouts"):
            parent._reflow_layouts()

    def _reset_appearance(self) -> None:
        if self._filling:
            return
        settings = appearance_settings()
        settings.setValue("font_size_percent", 100)
        settings.setValue("font_weight_value", 400)
        settings.sync()
        if settings.status() != type(settings).Status.NoError:
            QMessageBox.warning(self, "Оформление", "Не удалось сохранить настройку оформления.")
        self.size_slider.blockSignals(True)
        self.weight_slider.blockSignals(True)
        try:
            self.size_slider.setValue(100)
            self.weight_slider.setValue(400)
        finally:
            self.size_slider.blockSignals(False)
            self.weight_slider.blockSignals(False)
        apply_appearance()
        self._update_appearance_labels()
        self.note.setText("Восстановлено стандартное оформление: 100% / 400.")
        parent = self.window()
        if hasattr(parent, "_reflow_layouts"):
            parent._reflow_layouts()


class ExportDialog(QDialog):
    """Collect all choices before writing any PDF; report partial successes accurately."""
    def __init__(self, service: CalculationService, database: db.Database,
                 calculation_id: int, parent=None) -> None:
        super().__init__(parent)
        self.service, self.database, self.calculation_id = service, database, calculation_id
        calc = self.service.get_calculation(calculation_id)
        with database.connection() as conn:
            self.tenant = db.get_tenant(conn, calc["tenant_id"])
            current = {d["type"]: d for d in db.list_documents(conn, calculation_id=calculation_id)}
        self.current, self.calc = current, calc
        self.setWindowTitle("Выгрузить акт и счёт в PDF")
        self.resize(530, 360)
        outer = QVBoxLayout(self)
        hint = QLabel("Выберите документы, номера, даты и место сохранения")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        choices = QWidget()
        choices_layout = QVBoxLayout(choices)
        choices_layout.setSpacing(11)
        outer.addWidget(scroll, 1)
        self.options = {}
        for kind, title in (("act", "Акт"), ("invoice", "Счёт")):
            group = QGroupBox(title)
            form = QFormLayout(group)
            checkbox = QCheckBox("Выгрузить")
            checkbox.setChecked(True)
            form.addRow("", checkbox)
            number = QSpinBox()
            number.setRange(1, 2_147_483_647)
            current_document = current.get(kind)
            day = QDate.fromString(current_document["document_date"], "yyyy-MM-dd") if current_document else QDate.currentDate()
            number.setValue(current_document["number"] if current_document else
                            self.service.next_document_number(kind, day.toString("yyyy-MM-dd")))
            date = QDateEdit(day)
            date.setDisplayFormat("dd.MM.yyyy")
            date.setCalendarPopup(True)
            form.addRow("Номер", number)
            form.addRow("Дата", date)
            checkbox.toggled.connect(number.setEnabled)
            checkbox.toggled.connect(date.setEnabled)
            if not current_document:
                date.dateChanged.connect(
                    lambda new_date, k=kind, control=number: self._update_number_for_date(
                        k, new_date, control))
            self.options[kind] = (checkbox, number, date)
            choices_layout.addWidget(group)
        scroll.setWidget(choices)
        actions = QVBoxLayout()
        close = QPushButton("Отмена")
        close.clicked.connect(self.reject)
        send = QPushButton("Выбрать файлы и выгрузить")
        icon_button(send, "download")
        send.clicked.connect(self._export)
        actions.addWidget(send)
        actions.addWidget(close)
        outer.addLayout(actions)

    @guard_database_errors("Ошибка получения номера документа")
    def _update_number_for_date(self, kind: str, new_date: QDate, control: QSpinBox) -> None:
        next_number = self.service.next_document_number(
            kind, new_date.toString("yyyy-MM-dd"))
        control.setValue(next_number)

    @guard_database_errors("Ошибка базы данных")
    def _export(self) -> None:
        chosen = {kind: (spin.value(), date.date().toString("yyyy-MM-dd"))
                  for kind, (check, spin, date) in self.options.items() if check.isChecked()}
        if not chosen:
            QMessageBox.warning(self, "Нет документов", "Выберите акт, счёт или оба документа.")
            return
        checked = self.service.validate(self.calculation_id)
        with self.database.connection() as conn:
            performer = db.get_settings(conn)
        if not performer["performer_name"].strip():
            QMessageBox.warning(self, "Не заполнен исполнитель",
                                "Укажите название исполнителя в разделе «Настройки» перед PDF.")
            return
        if not checked.ready:
            QMessageBox.warning(self, "Расчёт не готов", "\n".join(checked.errors))
            return
        confirm_zero = False
        if checked.zero_consumption:
            if QMessageBox.question(
                self, "Нулевой расход",
                "Расход равен нулю. Выгрузить выбранные документы?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            confirm_zero = True
        duplicated = {}
        paths = {}
        overwrite = {}
        for kind, (number, day) in chosen.items():
            existing = self.current.get(kind)
            ids = self.service.document_number_duplicates(
                kind, number, day, exclude_id=existing["id"] if existing else None)
            if ids and (not existing or existing["number"] != number or existing["document_date"] != day):
                if QMessageBox.question(
                    self, "Совпадающий номер",
                    f"Номер {number} уже используется для этого типа документа в {day[:4]} году. "
                    "Выгрузить с повтором номера?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    return
                duplicated[kind] = True
            name = safe_filename(kind, number, self.tenant["name"], self.calc["year"], self.calc["month"])
            output, _ = QFileDialog.getSaveFileName(
                self, "Сохранить " + ("акт" if kind == "act" else "счёт"),
                str(Path.home() / name), "PDF (*.pdf)")
            if not output:
                return
            path = Path(output).expanduser().absolute()
            if path.suffix.lower() != ".pdf":
                path = path.with_suffix(".pdf")
            exists = path.exists() or path.is_symlink()
            if exists and QMessageBox.question(
                self, "Файл уже существует",
                f"Файл {path} уже существует. Заменить его?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            paths[kind] = path
            overwrite[kind] = exists

        # Compare physical destinations as in app.documents.export: covers
        # path/../ aliases, symlinked parent directories and existing hardlinks.
        # The whole pair must pass before the FIRST PDF or DB metadata change.
        try:
            _assert_unique_destinations(paths)
        except (ValueError, OSError) as error:
            show_error(self, error, "Совпадающие пути PDF")
            return

        success = []
        errors = []
        for kind, path in paths.items():
            number, day = chosen[kind]
            try:
                result = export_document(
                    self.service, self.calculation_id, kind, path,
                    overwrite=overwrite[kind], number=number, document_date=day,
                    confirm_zero=confirm_zero, confirm_duplicate=duplicated.get(kind, False),
                    # Repeat the same check immediately before publication,
                    # since a file or parent directory can change between dialogs.
                    _pre_publish=lambda: _assert_unique_destinations(paths))
                summary = ("Акт" if kind == "act" else "Счёт") + f": {result.path}"
                if result.cleanup_warning:
                    summary += "\nПРЕДУПРЕЖДЕНИЕ: " + result.cleanup_warning
                    for leftover in (result.leftover_backup, result.leftover_staged):
                        if leftover is not None:
                            summary += f"\nОстаточный файл: {leftover}"
                success.append(summary)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                from .common import explain_error
                errors.append(("Акт" if kind == "act" else "Счёт") + ": " + explain_error(error))
        message = ("Сохранено:\n" + "\n".join(success) if success else "PDF не сохранены.")
        if errors:
            message += "\n\nОшибки:\n" + "\n".join(errors)
            QMessageBox.warning(self, "Результат экспорта", message)
        else:
            QMessageBox.information(self, "PDF сохранены", message)
        if success:
            self.accept()
