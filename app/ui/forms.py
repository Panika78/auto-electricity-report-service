"""Real tenant, calculation, performer and document dialogs backed by project services."""
from __future__ import annotations

import calendar
import os
import sqlite3
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QPixmap

from app.core.service import CalculationService
from app.data import database as db
from app.data import queries
from app.documents import ExportError, export_document, safe_filename
from app.documents.export import _assert_unique_destinations
from .common import (CANCELLED, appearance_settings, apply_appearance, guard_database_errors,
                     import_image, remove_managed_image, parse_number, run_confirmed, show_error, show_number)


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
        self.history.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.horizontalHeader().setStretchLastSection(True)
        self.history.doubleClicked.connect(lambda _index: self._open_calculation())
        history_layout.addWidget(self.history)
        open_history = QPushButton("Открыть расчёт")
        open_history.clicked.connect(self._open_calculation)
        history_layout.addWidget(open_history)
        tabs.addTab(history, "История")

        actions = QHBoxLayout()
        self.archive_button = QPushButton("Переместить в архив")
        self.archive_button.clicked.connect(self._toggle_archive)
        actions.addWidget(self.archive_button)
        actions.addStretch()
        self.note = QLabel("Изменения сохраняются после завершения ввода поля")
        self.note.setWordWrap(True)
        actions.addWidget(self.note)
        self.create_button = QPushButton("Создать арендатора")
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
                      f"{calc['period_start']} — {calc['period_end']}",
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
        self.setWindowTitle("Расчёт электроэнергии")
        self.resize(650, 610)
        outer = QVBoxLayout(self)
        head = QLabel("Расчёт за выбранный период")
        head.setProperty("kontaktHeadingSize", 20)
        outer.addWidget(head)
        form = QFormLayout()
        self.tenant_picker = QComboBox()
        self._load_tenants()
        self.tenant_picker.currentIndexChanged.connect(self._suggest_start)
        form.addRow("Арендатор", self.tenant_picker)
        date_start = QDate(self.year, self.month, 1)
        date_end = QDate(self.year, self.month, calendar.monthrange(self.year, self.month)[1])
        self.date_start = QDateEdit(date_start)
        self.date_end = QDateEdit(date_end)
        for picker in (self.date_start, self.date_end):
            picker.setCalendarPopup(True)
            picker.setDisplayFormat("dd.MM.yyyy")
            picker.userDateChanged.connect(lambda _d: self._save_period())
        form.addRow("С", self.date_start)
        form.addRow("По", self.date_end)
        self.start_edit = _line()
        self.end_edit = _line()
        self.start_edit.setPlaceholderText("Автоподстановка, если доступна")
        self.end_edit.setPlaceholderText("Можно оставить пустым")
        for key, field in (("start_reading", self.start_edit), ("end_reading", self.end_edit)):
            field.textEdited.connect(lambda _v, name=key: self._mark_pending(name))
            field.editingFinished.connect(lambda name=key: self._save_reading(name))
        form.addRow("Начальные показания", self.start_edit)
        self.start_hint = QLabel()
        self.start_hint.setStyleSheet("color:#657276;")
        form.addRow("", self.start_hint)
        form.addRow("Конечные показания", self.end_edit)
        self.consumption = _line()
        self.amount = _line()
        for field in (self.consumption, self.amount):
            field.setReadOnly(True)
        form.addRow("Расход, кВт·ч", self.consumption)
        form.addRow("Сумма, ₽", self.amount)
        self.tariff_label = QLabel()
        form.addRow("Тариф", self.tariff_label)
        outer.addLayout(form)

        actions = QHBoxLayout()
        self.open_tenant_button = self._button("Карточка арендатора", self._open_tenant)
        actions.addWidget(self.open_tenant_button)
        actions.addWidget(self._button("Ручной расход", self._edit_consumption))
        actions.addWidget(self._button("Ручная сумма", self._edit_amount))
        actions.addWidget(self._button("PDF акта / счёта", self._export))
        outer.addLayout(actions)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("background:#f0f1ec; color:#3a5155; padding:11px; border-radius:8px;")
        outer.addWidget(self.status)

        bottom = QHBoxLayout()
        self.delete_button = self._button("Удалить", self._delete)
        bottom.addWidget(self.delete_button)
        bottom.addStretch()
        self.create_button = self._button("Создать расчёт", self._create)
        bottom.addWidget(self.create_button)
        bottom.addWidget(self._button("Готово", self.accept))
        outer.addLayout(bottom)
        self._refresh()
        self._initial_load = False

    @staticmethod
    def _button(label, callback):
        control = QPushButton(label)
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
            if proposal.value is None:
                self.start_hint.setText("Автоматический источник не найден. Укажите показание вручную.")
            else:
                suffix = " (потребуется подтверждение)" if proposal.requires_confirmation else ""
                self.start_hint.setText("Автоподстановка: " + show_number(proposal.value) + suffix)
        except (LookupError, ValueError) as error:
            self.start_hint.setText(str(error))

    @guard_database_errors("Ошибка базы данных")
    def _create(self) -> None:
        tenant_id = self.tenant_picker.currentData()
        if tenant_id is None:
            QMessageBox.warning(self, "Арендатор отсутствует", "Сначала создайте арендатора в справочнике.")
            return
        try:
            start = parse_number(self.start_edit.text(), 3, optional=True)
            end = parse_number(self.end_edit.text(), 3, optional=True)
        except (TypeError, ValueError) as error:
            show_error(self, error)
            return
        result = run_confirmed(self, lambda flags: self.service.create_calculation(
            tenant_id, self.year, self.month,
            period_start=self.date_start.date().toString("yyyy-MM-dd"),
            period_end=self.date_end.date().toString("yyyy-MM-dd"),
            start_reading=start, end_reading=end, **flags))
        if result is CANCELLED:
            return
        self.calculation_id = result["id"]
        self.tenant_picker.setEnabled(False)
        self.create_button.hide()
        self.delete_button.show()
        self.pending.clear()
        self._refresh()
        self.status.setText("Расчёт создан и сохранён. Дальнейшие изменения сохраняются автоматически.")

    @guard_database_errors("Ошибка загрузки расчёта")
    def _refresh(self) -> None:
        # Read all database state before mutating form widgets. A locked DB must
        # not partially overwrite unsaved user input or strand _filling=True.
        unsaved = set(self.pending)
        unsaved_start = self.start_edit.text()
        unsaved_end = self.end_edit.text()
        unsaved_dates = (self.date_start.date(), self.date_end.date())
        calc = None
        checked = None
        if self.calculation_id is not None:
            calc = self.service.get_calculation(self.calculation_id)
            checked = self.service.validate(self.calculation_id)
        with self.database.connection() as conn:
            tariff = queries.month_tariff(conn, self.year, self.month)
        self._filling = True
        try:
            self.create_button.setVisible(self.calculation_id is None)
            self.delete_button.setVisible(self.calculation_id is not None)
            if calc is not None:
                index = self.tenant_picker.findData(calc["tenant_id"])
                self.tenant_picker.setCurrentIndex(index)
                self.tenant_picker.setEnabled(False)
                self.date_start.setDate(QDate.fromString(calc["period_start"], "yyyy-MM-dd"))
                self.date_end.setDate(QDate.fromString(calc["period_end"], "yyyy-MM-dd"))
                self.start_edit.setText(show_number(calc["start_reading"]))
                self.end_edit.setText(show_number(calc["end_reading"]))
                self.consumption.setText(show_number(calc["consumption"]))
                self.amount.setText(show_number(calc["total_amount"]))
                details = checked.warnings + checked.errors
                self.status.setText(("Готов к экспорту" if checked.ready else "Черновик") +
                                    ("\n" + "\n".join(details) if details else ""))
                self.start_hint.setText(
                    {"manual": "Введено вручную", "previous_month": "Из предыдущего месяца",
                     "initial": "Из карточки арендатора", "unspecified": "Источник не задан"}.get(
                        calc["start_reading_source"], ""))
            else:
                self.consumption.setText("")
                self.amount.setText("")
                self.status.setText("Новый расчёт будет сохранён как черновик, даже без конечного показания.")
                self._suggest_start()
            self.tariff_label.setText(show_number(tariff["rate"]) if tariff else "Не установлен")
            if calc is not None and unsaved:
                if "start_reading" in unsaved:
                    self.start_edit.setText(unsaved_start)
                if "end_reading" in unsaved:
                    self.end_edit.setText(unsaved_end)
                if "period" in unsaved:
                    self.date_start.setDate(unsaved_dates[0])
                    self.date_end.setDate(unsaved_dates[1])
                self.status.setText(
                    self.status.text() + "\nЕсть несохранённые изменения: " + ", ".join(sorted(unsaved)))
        finally:
            self._filling = False

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
        heading = QLabel("Настройки")
        heading.setProperty("kontaktHeadingSize", 27)
        outer.addWidget(heading)
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)
        scrolling = QScrollArea()
        scrolling.setWidgetResizable(True)
        performer = QWidget()
        form = QFormLayout(performer)
        self.fields = {}
        for key, label in SETTINGS_FIELDS:
            field = _line()
            field.editingFinished.connect(lambda name=key: self._save_field(name))
            field.textEdited.connect(lambda _text, name=key: self.pending.add(name))
            self.fields[key] = field
            form.addRow(label, field)
        self.reuse = QCheckBox("Использовать ручные начальные показания прошлого месяца")
        self.reuse.toggled.connect(self._save_reuse)
        form.addRow("Автоподстановка", self.reuse)
        self.images = {}
        for key, title in (("signature_path", "Подпись"), ("stamp_path", "Печать")):
            group = QWidget()
            controls = QHBoxLayout(group)
            controls.setContentsMargins(0, 0, 0, 0)
            info = QLabel("Не загружено")
            info.setWordWrap(True)
            controls.addWidget(info, 1)
            select = QPushButton("Загрузить")
            select.clicked.connect(lambda _=False, name=key: self._select_image(name))
            remove = QPushButton("Удалить")
            remove.clicked.connect(lambda _=False, name=key: self._remove_image(name))
            controls.addWidget(select)
            controls.addWidget(remove)
            self.images[key] = (info, remove)
            form.addRow(title, group)
        scrolling.setWidget(performer)
        tabs.addTab(scrolling, "Исполнитель")

        appearance = QWidget()
        appearance_form = QFormLayout(appearance)
        self.size_picker = QComboBox()
        self.size_picker.addItem("Меньше", "compact")
        self.size_picker.addItem("Обычный", "standard")
        self.size_picker.addItem("Крупнее", "large")
        self.weight_picker = QComboBox()
        self.weight_picker.addItem("Лёгкий", "light")
        self.weight_picker.addItem("Обычный", "normal")
        self.weight_picker.addItem("Жирный", "bold")
        appearance_form.addRow("Размер шрифта", self.size_picker)
        appearance_form.addRow("Насыщенность", self.weight_picker)
        appearance_form.addRow(QLabel("Оформление применяется сразу и не меняет PDF и статусы документов."))
        self.size_picker.currentIndexChanged.connect(
            lambda _i: self._change_appearance("font_size", self.size_picker.currentData()))
        self.weight_picker.currentIndexChanged.connect(
            lambda _i: self._change_appearance("font_weight", self.weight_picker.currentData()))
        tabs.addTab(appearance, "Оформление")
        self.note = QLabel("Все изменения реквизитов сохраняются после выхода из поля.")
        self.note.setStyleSheet("color:#657276;")
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
        settings = appearance_settings()
        for widget, key, fallback in ((self.size_picker, "font_size", "standard"),
                                      (self.weight_picker, "font_weight", "normal")):
            index = widget.findData(settings.value(key, fallback))
            widget.setCurrentIndex(index if index >= 0 else widget.findData(fallback))
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

    def _change_appearance(self, key: str, value: str) -> None:
        if self._filling or value is None:
            return
        settings = appearance_settings()
        settings.setValue(key, value)
        settings.sync()
        if settings.status() != type(settings).Status.NoError:
            QMessageBox.warning(self, "Оформление", "Не удалось сохранить настройку оформления.")
        apply_appearance()
        self.note.setText("Оформление применено. Статус документов не изменён.")


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
        outer.addWidget(QLabel("Выберите документы, номера, даты и место сохранения"))
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
            outer.addWidget(group)
        actions = QHBoxLayout()
        actions.addStretch()
        close = QPushButton("Отмена")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        send = QPushButton("Выбрать файлы и выгрузить")
        send.clicked.connect(self._export)
        actions.addWidget(send)
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
