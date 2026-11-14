"""Service-backed individual PDF export with independently reported outcomes.

The filesystem and SQLite cannot form a single atomic transaction. A temporary
file protects an existing destination during rendering; a backup is used if
revision validation fails after replacing an explicitly overwritten PDF.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Mapping

from app.core.service import CalculationService

from .act import render_act
from .invoice import render_invoice

_RENDERERS = {"act": render_act, "invoice": render_invoice}
_INVALID_WINDOWS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


class ExportError(RuntimeError):
    """PDF export failed. The logical document was not marked generated."""

    def __init__(self, message: str, *, path: Path | None = None,
                 recovery_failed: bool = False) -> None:
        super().__init__(message)
        self.path = path
        self.recovery_failed = recovery_failed


class ExportCancelled(ExportError):
    """Cancellation before reserving document metadata or opening a destination."""


@dataclass(frozen=True)
class ExportedDocument:
    kind: str
    path: Path
    document_id: int
    number: int
    document_date: str


@dataclass
class ExportBatchResult:
    successful: dict[str, ExportedDocument] = field(default_factory=dict)
    failed: dict[str, Exception] = field(default_factory=dict)


def safe_filename(kind: str, number: int, tenant_name: str, year: int, month: int) -> str:
    """Portable PDF filename for UI defaults and future ZIP export."""
    if kind not in _RENDERERS:
        raise ValueError("Неизвестный вид документа.")
    cleaned = _INVALID_WINDOWS.sub("_", str(tenant_name))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")[:75].rstrip(" .") or "Арендатор"
    if cleaned.split(".")[0].upper() in _RESERVED_WINDOWS:
        cleaned = "_" + cleaned
    label = "Акт" if kind == "act" else "Счет"
    return f"{label}_{number}_{cleaned}_{month:02d}-{year:04d}.pdf"


def _validate_target(path: Path, overwrite: bool) -> None:
    if path.suffix.lower() != ".pdf" or not path.name or _INVALID_WINDOWS.search(path.name):
        raise ValueError("Имя файла должно быть допустимым и иметь расширение .pdf.")
    if path.stem.split(".")[0].upper() in _RESERVED_WINDOWS or path.name.endswith((" ", ".")):
        raise ValueError("Недопустимое имя файла Windows.")
    if not path.parent.is_dir():
        raise FileNotFoundError("Каталог для PDF не существует.")
    if path.is_dir():
        raise IsADirectoryError(str(path))
    if not overwrite and path.exists():
        raise FileExistsError(f"Файл уже существует: {path}")


def _stage_pdf(path: Path, data: bytes) -> Path:
    fd, temp_name = tempfile.mkstemp(prefix=".kontakt-pdf-", suffix=".tmp", dir=path.parent)
    staged = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _backup(path: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=".kontakt-backup-", suffix=".pdf", dir=path.parent)
    os.close(fd)
    result = Path(name)
    try:
        shutil.copy2(path, result)
        return result
    except BaseException:
        result.unlink(missing_ok=True)
        raise


def export_document(service: CalculationService, calculation_id: int, kind: str,
                    path: str | Path, *, overwrite: bool = False,
                    document_date: date | str | None = None, number: int | None = None,
                    confirm_duplicate: bool = False, confirm_zero: bool = False,
                    cancelled: bool = False) -> ExportedDocument:
    """Export one document. Caller supplies GUI confirmations as explicit flags.

    On revision conflict the newly saved PDF is rolled back where possible.
    A filesystem or recovery failure is reported, never a false success.
    """
    if cancelled:
        raise ExportCancelled("Экспорт отменён до создания документа.")
    if kind not in _RENDERERS:
        raise ValueError("Неизвестный вид документа.")
    target = Path(path).expanduser().absolute()
    _validate_target(target, overwrite)
    document = service.reserve_document(
        calculation_id, kind, document_date=document_date, number=number,
        confirm_duplicate=confirm_duplicate, confirm_zero=confirm_zero)
    snapshot = service.prepare_document(calculation_id, kind, confirm_zero=confirm_zero)
    if snapshot["document"]["id"] != document["id"]:
        raise ExportError("Метаданные документа изменились до генерации.")
    pdf_bytes = _RENDERERS[kind](snapshot)
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ExportError("Генератор не вернул корректный заголовок PDF.")
    # Catch a revision change during the slow render BEFORE touching a destination.
    latest = service.prepare_document(calculation_id, kind, confirm_zero=confirm_zero)
    if latest["revision"] != snapshot["revision"]:
        raise ExportError("Данные изменились во время подготовки PDF; файл не записан.")

    staged: Path | None = None
    backup: Path | None = None
    saved = False
    try:
        staged = _stage_pdf(target, pdf_bytes)
        _validate_target(target, overwrite)
        if overwrite:
            if target.exists():
                backup = _backup(target)
            os.replace(staged, target)
        else:
            # NTFS and POSIX hard links offer exclusive no-clobber publication.
            os.link(staged, target)
        saved = True
        staged.unlink(missing_ok=True)
        staged = None
        service.mark_document_generated(document["id"], expected_revision=snapshot["revision"])
        return ExportedDocument(kind, target, document["id"], document["number"],
                                document["document_date"])
    except BaseException as exc:
        recovery_failed = False
        if saved:
            try:
                if backup is not None:
                    os.replace(backup, target)
                    backup = None
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                recovery_failed = True
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, ExportError):
            raise
        raise ExportError("Не удалось завершить экспорт PDF; отметка генерации не установлена."
                          + (" Восстановление предыдущего файла не удалось." if recovery_failed else ""),
                          path=target if recovery_failed else None,
                          recovery_failed=recovery_failed) from exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)


def export_documents(service: CalculationService, calculation_id: int,
                     paths: Mapping[str, str | Path], *, overwrite: bool = False,
                     confirm_zero: bool = False, confirm_duplicate: bool = False,
                     cancelled: bool = False) -> ExportBatchResult:
    """Act and/or invoice; a second failure does not erase the first success."""
    result = ExportBatchResult()
    normalized = {kind: Path(path).expanduser().absolute() for kind, path in paths.items()}
    if len(set(normalized.values())) != len(normalized):
        raise ValueError("Акт и счёт не могут иметь одинаковый целевой путь.")
    for kind, path in normalized.items():
        try:
            result.successful[kind] = export_document(
                service, calculation_id, kind, path, overwrite=overwrite,
                confirm_zero=confirm_zero, confirm_duplicate=confirm_duplicate,
                cancelled=cancelled)
        except Exception as exc:
            result.failed[kind] = exc
    return result