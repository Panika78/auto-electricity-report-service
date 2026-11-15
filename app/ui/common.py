"""Reusable native Qt presentation helpers; no domain rules are duplicated here."""
from __future__ import annotations

import os
import shutil
from functools import wraps
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from app.data import database as db
from app.data.database import get_database_path
from app.qa_profile import validate_qa_profile
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
    # Windows NativeFormat normally uses HKCU. During release QA, use a
    # dedicated INI under the explicitly isolated synthetic profile instead.
    # Ordinary users keep the existing native Qt settings without migration.
    if os.environ.get("KONTAKT_RELEASE_SMOKE") == "1":
        root = validate_qa_profile()
        settings = QSettings(str(root / "qa-appearance.ini"), QSettings.Format.IniFormat)
        settings.setFallbacksEnabled(False)
        return settings
    return QSettings("Kontakt", "Kontakt")


SIZE_MIN, SIZE_MAX, SIZE_STEP = 80, 200, 5
WEIGHT_MIN, WEIGHT_MAX, WEIGHT_STEP = 300, 800, 100


def read_appearance() -> tuple[int, int]:
    """Read percentages and CSS weights; migrate legacy three-way settings."""
    settings = appearance_settings()
    old_size = {"compact": 90, "standard": 100, "large": 120}
    old_weight = {"light": 300, "normal": 400, "bold": 700}
    size = settings.value(
        "font_size_percent", old_size.get(settings.value("font_size", "standard"), 100))
    weight = settings.value(
        "font_weight_value", old_weight.get(settings.value("font_weight", "normal"), 400))
    try:
        size = int(size)
        weight = int(weight)
    except (ValueError, TypeError):
        return 100, 400
    if not SIZE_MIN <= size <= SIZE_MAX or (size - SIZE_MIN) % SIZE_STEP:
        size = 100
    if not WEIGHT_MIN <= weight <= WEIGHT_MAX or (weight - WEIGHT_MIN) % WEIGHT_STEP:
        weight = 400
    return size, weight


def available_font_weights(family: str | None = None) -> tuple[int, ...]:
    """Find actually installed faces instead of promising nonexistent weight steps."""
    app = QApplication.instance()
    family = family or (app.font().family() if app is not None else QFont().family())
    try:
        faces = QFontDatabase.styles(family)
        weights = {
            int(QFontDatabase.font(family, face, 10).weight())
            for face in faces
        }
        return tuple(sorted(weights)) if weights else (400,)
    except (ValueError, RuntimeError):
        return (400, 700)


def effective_font_weight(requested: int, available: tuple[int, ...]) -> int:
    """Map a CSS step to the closest real face; prefer lighter in ties."""
    return min(available or (400,), key=lambda actual: (abs(actual - requested), actual))


def apply_appearance() -> None:
    """Scale from the immutable OS base font, not from the previous slider value.

    Point sizes let Qt handle Windows DPI once; no extra devicePixelRatio
    multiplier is applied. The QSettings keys affect only Qt presentation.
    """
    app = QApplication.instance()
    if app is None:
        return
    base = app.property("_kontakt_base_font")
    if not isinstance(base, QFont):
        base = QFont(app.font())
        app.setProperty("_kontakt_base_font", base)
    size, requested = read_appearance()
    scale = size / 100
    effective = effective_font_weight(requested, available_font_weights(base.family()))
    font = QFont(base)
    point_size = base.pointSizeF()
    font.setPointSizeF(max(7, point_size if point_size > 0 else 10) * scale)
    font.setWeight(QFont.Weight(effective))
    app.setFont(font)
    # Headings preserve contrast with the text weight instead of flattening
    # every label to one global font weight.
    headings = []
    for nominal in (19, 20, 27):
        bold_weight = min(900, effective + 200)
        headings.append(
            f'QLabel[kontaktHeadingSize="{nominal}"] '
            f'{{font-size:{nominal * scale:.1f}pt;font-weight:{bold_weight};color:#1b2b30;}}')
    headings.append(
        f'QLabel#tenantName {{font-size:{15 * scale:.1f}pt;'
        f'font-weight:{min(900, effective + 200)};color:#1b2b30;}}')
    app.setStyleSheet("\n".join(headings))



def image_directory(database: db.Database | None = None) -> Path:
    """Keep images beside the SQLite database used by this UI/service.

    Existing absolute image references remain readable; do not migrate/delete
    files on an upgrade. Passing the explicit Database isolates test profiles.
    """
    db_path = database.path if database is not None else get_database_path()
    path = db_path.parent / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def import_image(source: str, kind: str, *, database: db.Database | None = None) -> Path:
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
    directory = image_directory(database)
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


def remove_managed_image(path: str | None, *, database: db.Database | None = None) -> None:
    """Remove only image copies located in our own application directory."""
    if not path:
        return
    target = Path(path)
    if target.is_symlink():
        raise OSError("Ссылка на файл не удалена из соображений безопасности.")
    base = image_directory(database).resolve()
    if target.resolve().parent != base:
        return  # old externally referenced file belongs to the user
    if target.name.startswith(("signature-", "stamp-")):
        target.unlink(missing_ok=True)
