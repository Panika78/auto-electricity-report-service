"""Reusable native Qt presentation helpers; no domain rules are duplicated here."""
from __future__ import annotations

import os
import shutil
from functools import wraps
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings, QStandardPaths
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from app.core.calculations import decimal_value
from app.core.service import ConfirmationRequired
from app.documents import ExportError

CANCELLED = object()
CONFIRMATIONS = {
    "overlapping_period": ("Совпадение периодов", "У этого арендатора уже есть расчёты с пересекающимися датами. Создать ещё один?", "confirm_overlap"),
    "reuse_manual_start_reading": ("Подстановка показаний", "Использовать ручное начальное показание прошлого месяца?", "confirm_manual_reuse"),
    "historical_readings": ("Зависимые расчёты", "Изменение может пересчитать автоматические начальные показания следующих месяцев. Продолжить?", "confirm_history"),
    "manual_override": ("Ручная корректировка", "Ручное значение заменит автоматически вычисленное. Продолжить?", "confirm_manual"),
    "tariff_recalculation": ("Изменение тарифа", "Тариф уже используется в расчётах. Они будут пересчитаны, а документы потребуют обновления. Продолжить?", "confirm_recalculation"),
    "delete_calculation": ("Удаление расчёта", "Удалить расчёт и связанные метаданные актов и счетов без возможности восстановления?", "confirm_delete"),
    "duplicate_document_number": ("Повтор номера", "Этот номер уже использован для документа такого типа в выбранном году. Продолжить?", "confirm_duplicate"),
    "zero_consumption": ("Нулевой расход", "Расход равен нулю. Всё равно сформировать документ?", "confirm_zero"),
}


def explain_error(error: BaseException) -> str:
    message = str(error) or type(error).__name__
    if isinstance(error, ExportError):
        paths = [(label, getattr(error, field, None)) for label, field in
                 (("Резервная копия", "backup_path"), ("Временный PDF", "staged_path"),
                  ("Частичный файл", "partial_path"), ("Файл карантина", "quarantine_path"),
                  ("Проверяемый файл", "path"))]
        for label, value in paths:
            if value is not None:
                message += f"\n{label}: {value}"
        if error.__cause__:
            message += f"\nПричина: {error.__cause__}"
    return message


def show_error(parent: QWidget, error: BaseException, title: str = "Не удалось сохранить") -> None:
    QMessageBox.critical(parent, title, explain_error(error))


def guard_database_errors(title: str = "Ошибка доступа к базе данных"):
    """Protect Qt slots against SQLite read failures before service calls.

    A failed operation must not be reported as saved or close the dialog.
    Pending form values are intentionally left untouched for a later retry.
    """
    def decorate(slot):
        @wraps(slot)
        def protected(self, *args, **kwargs):
            try:
                return slot(self, *args, **kwargs)
            except (sqlite3.Error, OSError) as error:
                # A constructor cannot safely continue with an unpopulated,
                # editable card. Let its owner report the error and abort open.
                # Already-open dialogs retain the normal friendly error UI.
                if getattr(self, "_initial_load", False):
                    raise
                if getattr(self, "note", None) is not None:
                    self.note.setText("Ошибка базы данных. Введённые значения не сохранены.")
                elif getattr(self, "status", None) is not None:
                    self.status.setText("Ошибка базы данных. Повторите операцию после восстановления доступа.")
                show_error(self, error, title)
                return CANCELLED
        return protected
    return decorate


def run_confirmed(parent: QWidget, operation: Callable[[dict[str, bool]], Any]) -> Any:
    """Service raises before writing; only an explicit yes retries with a flag."""
    confirmations: dict[str, bool] = {}
    for _ in range(8):
        try:
            return operation(confirmations)
        except ConfirmationRequired as error:
            entry = CONFIRMATIONS.get(error.code)
            if entry is None:
                show_error(parent, error)
                return CANCELLED
            title, message, flag = entry
            if confirmations.get(flag):
                show_error(parent, error)
                return CANCELLED
            if error.affected_ids:
                message += "\n\nЗатронутые расчёты (ID): " + ", ".join(map(str, error.affected_ids))
            answer = QMessageBox.question(parent, title, message,
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return CANCELLED
            confirmations[flag] = True
        except (ValueError, TypeError, LookupError, sqlite3.Error, OSError, RuntimeError) as error:
            show_error(parent, error)
            return CANCELLED
    QMessageBox.critical(parent, "Ошибка", "Слишком много повторных подтверждений.")
    return CANCELLED


def parse_number(text: str, places: int, *, optional: bool = False):
    value = text.strip().replace(",", ".")
    if optional and not value:
        return None
    return decimal_value(value, places)


def show_number(value: Any) -> str:
    return "" if value is None else str(value).replace(".", ",")


def appearance_settings() -> QSettings:
    return QSettings("Kontakt", "Kontakt")


def apply_appearance() -> None:
    settings = appearance_settings()
    size = settings.value("font_size", "standard")
    weight = settings.value("font_weight", "normal")
    font = QApplication.instance().font()
    font.setPointSize({"compact": 9, "standard": 10, "large": 12}.get(size, 10))
    font.setWeight({"light": QFont.Weight.Light,
                    "normal": QFont.Weight.Normal,
                    "bold": QFont.Weight.DemiBold}.get(weight, QFont.Weight.Normal))
    QApplication.instance().setFont(font)
    scale = {"compact": .93, "standard": 1, "large": 1.16}.get(size, 1)
    heading_weight = {"light": 530, "normal": 700, "bold": 850}.get(weight, 700)
    # App-wide stylesheet is inherited by future dialogs too. Avoid retaining or
    # restyling ephemeral PySide wrappers from QApplication.allWidgets().
    app = QApplication.instance()
    rules = [
        f'QLabel[kontaktHeadingSize="{nominal}"] '
        f'{{font-size:{round(nominal * scale)}px; font-weight:{heading_weight}; color:#1b2b30;}}'
        for nominal in (19, 20, 27)
    ]
    app.setStyleSheet("\n".join(rules))


def image_directory() -> Path:
    root = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    if not root:
        raise OSError("Не удалось определить каталог данных приложения.")
    path = Path(root) / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def import_image(source: str, kind: str) -> Path:
    """Copy image into app data, without exposing an external removable path."""
    if kind not in ("signature", "stamp"):
        raise ValueError("Недопустимый тип изображения.")
    path = Path(source)
    if not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ValueError("Выберите изображение PNG или JPEG.")
    from PySide6.QtGui import QImageReader
    reader = QImageReader(str(path))
    if not reader.canRead() or reader.read().isNull():
        raise ValueError("Изображение повреждено или не поддерживается.")
    directory = image_directory()
    fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=directory)
    os.close(fd)
    try:
        shutil.copyfile(path, tmp)
        # Preserve the old image when updating settings fails: each upload has its own name.
        import uuid
        target = directory / (kind + "-" + uuid.uuid4().hex + path.suffix.lower())
        os.replace(tmp, target)
        return target
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def remove_managed_image(path: str | None) -> None:
    """Remove only image copies located in our own application directory."""
    if not path:
        return
    target = Path(path)
    if target.is_symlink():
        raise OSError("Ссылка на файл не удалена из соображений безопасности.")
    base = image_directory().resolve()
    if target.resolve().parent != base:
        return  # old externally referenced file belongs to the user
    if target.name.startswith(("signature-", "stamp-")):
        target.unlink(missing_ok=True)
