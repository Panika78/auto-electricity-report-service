"""Bulk ZIP export: isolate PDF failures, publish once, then confirm SQLite statuses.

This is deliberately separate from individual PDF publication. Filesystem and
SQLite cannot form a shared transaction: if the post-publication commit fails,
the complete ZIP stays available and its document statuses remain unconfirmed.
"""
from __future__ import annotations

import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from app.core.service import CalculationService

from .export import (
    _INVALID_WINDOWS, _RENDERERS, _RESERVED_WINDOWS, _identity,
    _publish_new_file, _sha256_file, _unlink_if_owned, ExportError, safe_filename,
)


@dataclass(frozen=True)
class BulkDocument:
    tenant_id: int
    tenant_name: str
    calculation_id: int
    kind: str
    document_id: int
    number: int
    document_date: str
    entry_name: str


@dataclass(frozen=True)
class BulkFailure:
    tenant_id: int
    tenant_name: str
    calculation_id: int
    kind: str
    error: Exception


@dataclass
class BulkExportResult:
    path: Path | None = None
    successful: list[BulkDocument] = field(default_factory=list)
    failed: list[BulkFailure] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    publication_error: Exception | None = None
    metadata_error: Exception | None = None
    cleanup_errors: list[Exception] = field(default_factory=list)
    leftover_staged: Path | None = None

    @property
    def statuses_confirmed(self) -> bool:
        return self.path is not None and self.metadata_error is None


def _safe_component(name: str, *, fallback: str, limit: int = 65) -> str:
    """One Windows-safe ZIP path component, never a traversal or device name."""
    cleaned = _INVALID_WINDOWS.sub("_", str(name))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned[:limit].rstrip(" .") or fallback
    if cleaned.split(".")[0].upper() in _RESERVED_WINDOWS:
        cleaned = "_" + cleaned
    return cleaned


def _entry_name(snapshot: dict, kind: str) -> str:
    tenant = snapshot["tenant"]
    document = snapshot["document"]
    calculation = snapshot["calculation"]
    folder = f'{_safe_component(tenant["name"], fallback="Арендатор")}_T{tenant["id"]}'
    filename = safe_filename(
        kind, document["number"], tenant["name"],
        calculation["year"], calculation["month"])
    # The IDs also distinguish duplicate manual document numbers and casefold
    # collisions, without giving unstable "copy (2)" names on re-export.
    pdf_name = f'{filename[:-4]}_D{document["id"]}.pdf'
    return f"{folder}/{pdf_name}"


def _validate_target(target: Path, overwrite: bool) -> None:
    if (target.suffix.lower() != ".zip" or not target.stem
            or _INVALID_WINDOWS.search(target.name)
            or target.name.endswith((" ", "."))
            or target.stem.split(".")[0].upper() in _RESERVED_WINDOWS):
        raise ValueError("Имя архива должно быть допустимым для Windows и оканчиваться на .zip.")
    if not target.parent.is_dir():
        raise FileNotFoundError("Каталог для сохранения ZIP не существует.")
    if target.is_symlink():
        raise ValueError("Нельзя сохранять ZIP поверх символической ссылки.")
    if target.is_dir():
        raise IsADirectoryError(str(target))
    if not overwrite and target.exists():
        raise FileExistsError(f"ZIP уже существует: {target}")


def _file_fingerprint(path: Path) -> tuple[tuple[int, ...], bytes]:
    """Best-effort detect foreign changes, including same-size replacement."""
    identity = _identity(path)
    digest = _sha256_file(path)
    if _identity(path) != identity:
        raise ExportError("Архив изменился при чтении; перезапись запрещена.", path=path)
    return identity, digest


def _stage_zip(target: Path, documents: list[tuple[str, bytes]]) -> tuple[Path, tuple[int, int]]:
    """Write and fsync a private sibling ZIP; clean up a failed stage by identity."""
    fd, name = tempfile.mkstemp(prefix=".kontakt-zip-", suffix=".tmp", dir=target.parent)
    stage = Path(name)
    stat = os.fstat(fd)
    owned = (stat.st_dev, stat.st_ino)
    descriptor_open = True
    try:
        output = os.fdopen(fd, "wb")
        descriptor_open = False
        with output:
            with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                for entry, pdf in documents:
                    archive.writestr(entry, pdf)
            output.flush()
            os.fsync(output.fileno())
        return stage, owned
    except BaseException as cause:
        if descriptor_open:
            os.close(fd)
        try:
            _unlink_if_owned(stage, owned)
        except BaseException as cleanup:
            raise ExportError(
                "ZIP не удалось подготовить, временный файл требует ручной очистки.",
                staged_path=stage, cleanup_error=cleanup) from cause
        raise


def export_bulk_zip(service: CalculationService, year: int, month: int,
                    path: str | Path, *, kinds: tuple[str, ...] = ("act", "invoice"),
                    overwrite: bool = False) -> BulkExportResult:
    """Create selected PDFs for active tenants and safely publish a single ZIP.

    A PDF is counted successful only if the ZIP was published. Preparing a
    document can reserve its number/date, but never marks it generated.
    On a failed SQLite confirmation the published ZIP is NOT rolled back.
    """
    if not kinds or len(kinds) != len(set(kinds)) or any(k not in _RENDERERS for k in kinds):
        raise ValueError("Выберите акт, счёт или оба вида документов без повторов.")
    target = Path(path).expanduser().absolute()
    _validate_target(target, overwrite)  # preflight before any document reservation
    if overwrite and not target.exists():
        raise FileNotFoundError("Исходный ZIP для замены больше не существует.")
    old_fingerprint = _file_fingerprint(target) if overwrite else None
    candidates = service.bulk_candidates(year, month)
    result = BulkExportResult(skipped=list(candidates["skipped"]))
    prepared: list[tuple[str, bytes]] = []
    completed: list[BulkDocument] = []
    revisions: dict[int, Mapping[str, str | None]] = {}
    seen_entries: set[str] = set()

    for calc in candidates["selected"]:
        for kind in kinds:
            tenant_name = ""
            try:
                document = service.reserve_document(calc["id"], kind)
                snapshot = service.prepare_document(calc["id"], kind)
                tenant_name = snapshot["tenant"]["name"]
                if not (snapshot["performer"].get("performer_name") or "").strip():
                    raise ValueError("Не заполнено имя исполнителя в настройках.")
                if (snapshot["document"] is None
                        or snapshot["document"]["id"] != document["id"]):
                    raise ExportError("Метаданные документа изменились во время подготовки.")
                pdf = _RENDERERS[kind](snapshot)
                if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-"):
                    raise ExportError("Генератор не вернул корректный PDF.")
                # Detect edits made while PDF was being rendered, even though
                # a later transactional check also protects the status update.
                latest = service.prepare_document(calc["id"], kind)
                if latest["revision"] != snapshot["revision"]:
                    raise ExportError("Данные изменились во время подготовки PDF.")
                entry = _entry_name(snapshot, kind)
                if (entry.casefold() in seen_entries
                        or any(part in ("", ".", "..") for part in entry.split("/"))):
                    raise ExportError("Обнаружено совпадающее или небезопасное имя в ZIP.")
                seen_entries.add(entry.casefold())
                prepared.append((entry, pdf))
                completed.append(BulkDocument(
                    calc["tenant_id"], tenant_name, calc["id"], kind,
                    document["id"], document["number"], document["document_date"], entry))
                revisions[document["id"]] = snapshot["revision"]
            except Exception as exc:
                # Even a renderer failure must not prevent the other document
                # type from being generated for this same tenant.
                result.failed.append(BulkFailure(
                    calc["tenant_id"], tenant_name or f'Арендатор #{calc["tenant_id"]}',
                    calc["id"], kind, exc))

    if not prepared:
        return result  # never publish an empty ZIP

    stage: Path | None = None
    stage_identity: tuple[int, int] | None = None
    staged_digest: bytes | None = None
    try:
        stage, stage_identity = _stage_zip(target, prepared)
        stage_stat, staged_digest = _file_fingerprint(stage)
        if stage_stat[:2] != stage_identity:
            raise ExportError("Временный ZIP был заменён до публикации.", staged_path=stage)
        _validate_target(target, overwrite)  # re-check after rendering
        if overwrite:
            # A second actor could edit the existing ZIP after confirmation.
            # Never knowingly overwrite different bytes; the final rename
            # still has an unavoidable race with uncoordinated processes.
            if _file_fingerprint(target) != old_fingerprint:
                raise ExportError("Существующий ZIP изменился; замена отменена.", path=target)
            # Atomic replacement on the same filesystem; an old ZIP remains
            # unchanged if construction or os.replace fails.
            os.replace(stage, target)
            stage = None
        else:
            # Publish without clobbering an archive created after preflight.
            _publish_new_file(stage, target)
        result.path = target
        result.successful = completed
    except Exception as exc:
        result.publication_error = exc
        if isinstance(exc, ExportError):
            if exc.staged_path is not None:
                result.leftover_staged = exc.staged_path
            result.cleanup_errors.extend(exc.cleanup_errors)
    finally:
        if stage is not None:
            try:
                if stage_identity is None:
                    raise OSError("Неизвестно, принадлежит ли временный ZIP операции.")
                _unlink_if_owned(stage, stage_identity)
            except Exception as exc:
                result.cleanup_errors.append(exc)
                result.leftover_staged = stage

    if result.path is not None:
        try:
            # A foreign process may replace or modify the published archive.
            # Never assert "formed" if its bytes are no longer our ZIP.
            if _sha256_file(target) != staged_digest:
                raise ExportError(
                    "Опубликованный ZIP изменился до записи статусов; отметки не обновлены.",
                    path=target)
            service.mark_documents_generated(revisions, require_active=True)
        except Exception as exc:
            # Crucial: do not delete the already published ZIP or claim its
            # logical documents have confirmed statuses.
            result.metadata_error = exc
    return result
