"""Выборки для бизнес-слоя. Весь SQL сосредоточен в app.data."""
from __future__ import annotations
import sqlite3
from decimal import Decimal
from typing import Any


def month_tariff(conn: sqlite3.Connection, year: int, month: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM tariffs WHERE year = ? AND month = ?", (year, month)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["rate"] = Decimal(result["rate"])
    return result


def previous_month_reading(conn: sqlite3.Connection, tenant_id: int, year: int,
                           month: int) -> Decimal | None:
    # FR-06 / Issue #2: выбираем наиболее свежую ЗАПОЛНЕННУЮ запись
    # предыдущего календарного месяца. Черновик без end_reading не блокирует
    # более раннюю запись с конечным показанием. В другие месяцы не заглядываем.
    row = conn.execute("""SELECT end_reading FROM calculations
        WHERE tenant_id = ? AND year = ? AND month = ? AND end_reading IS NOT NULL
        ORDER BY updated_at DESC, id DESC LIMIT 1""", (tenant_id, year, month)).fetchone()
    return Decimal(row[0]) if row else None


def previous_manual_start(conn: sqlite3.Connection, tenant_id: int, year: int,
                          month: int) -> Decimal | None:
    row = conn.execute("""SELECT start_reading FROM calculations
        WHERE tenant_id = ? AND year = ? AND month = ?
          AND start_reading_source = 'manual' AND start_reading IS NOT NULL
        ORDER BY updated_at DESC, id DESC LIMIT 1""", (tenant_id, year, month)).fetchone()
    return Decimal(row[0]) if row else None


def has_earlier_calculation(conn: sqlite3.Connection, tenant_id: int, start: str) -> bool:
    return conn.execute("""SELECT 1 FROM calculations WHERE tenant_id = ?
        AND period_start <= ? LIMIT 1""", (tenant_id, start)).fetchone() is not None


def overlapping_ids(conn: sqlite3.Connection, tenant_id: int, year: int, month: int,
                    start: str, end: str, *, exclude_id: int | None = None) -> tuple[int, ...]:
    rows = conn.execute("""SELECT id FROM calculations WHERE tenant_id = ?
        AND year = ? AND month = ? AND period_start <= ? AND period_end >= ?
        AND (? IS NULL OR id != ?) ORDER BY id""",
        (tenant_id, year, month, end, start, exclude_id, exclude_id)).fetchall()
    return tuple(row[0] for row in rows)


def later_ids(conn: sqlite3.Connection, tenant_id: int, start: str,
              *, exclude_id: int | None = None) -> tuple[int, ...]:
    rows = conn.execute("""SELECT id FROM calculations WHERE tenant_id = ?
        AND period_start > ? AND (? IS NULL OR id != ?)
        ORDER BY period_start, id""", (tenant_id, start, exclude_id, exclude_id)).fetchall()
    return tuple(row[0] for row in rows)



def update_tariff_derived_values(conn: sqlite3.Connection, calculation_id: int, *,
                                 consumption: Decimal | None, amount: Decimal | None,
                                 is_ready: bool) -> None:
    """Технический перерасчёт не является пользовательским редактированием.

    Сохраняем updated_at, используемый для выбора последнего расчёта.
    Типы Decimal приводятся к тексту с той же проверкой, что и в database.py.
    """
    from .database import _decimal_text

    cursor = conn.execute("""UPDATE calculations
        SET consumption = ?, total_amount = ?, manual_amount = 0, is_ready = ?
        WHERE id = ?""", (_decimal_text(consumption, 3), _decimal_text(amount, 2),
                         int(is_ready), calculation_id))
    if cursor.rowcount != 1:
        raise LookupError(f"Расчёт {calculation_id} не найден.")


def list_future_calculations(conn: sqlite3.Connection, tenant_id: int, since: str) -> list[dict[str, Any]]:
    """Хронологический порядок без изменения отметок пользовательского редактирования."""
    from .database import _as_dict

    rows = conn.execute("""SELECT * FROM calculations
        WHERE tenant_id = ? AND period_start > ?
        ORDER BY period_start, id""", (tenant_id, since)).fetchall()
    return [_as_dict(row, ("start_reading", "end_reading", "consumption", "total_amount"))
            for row in rows]


def update_inherited_reading(conn: sqlite3.Connection, calculation_id: int, *,
                             start_reading: Decimal | None, consumption: Decimal | None,
                             amount: Decimal | None, is_ready: bool) -> None:
    """Согласовать *автоматически* унаследованное начало, не трогая updated_at.

    manual_consumption и manual_amount сбрасываются по FR-09; вручную
    заданное начало не передаётся сюда. Если источник исчез, оставляем
    provenance previous_month для будущей повторной синхронизации.
    """
    from .database import _decimal_text

    cursor = conn.execute("""UPDATE calculations SET start_reading = ?, consumption = ?,
        total_amount = ?, manual_consumption = 0, manual_amount = 0, is_ready = ?
        WHERE id = ? AND start_reading_source = 'previous_month'""",
        (_decimal_text(start_reading, 3), _decimal_text(consumption, 3),
         _decimal_text(amount, 2), int(is_ready), calculation_id))
    if cursor.rowcount != 1:
        raise LookupError(f"Автоматический расчёт {calculation_id} не найден.")


def invalidate_documents(conn: sqlite3.Connection, calculation_ids: tuple[int, ...] | None = None,
                         *, tenant_id: int | None = None, all_documents: bool = False) -> None:
    # Не перезаписываем документы, которые уже неактуальны; состояние генерации сохраняется.
    if all_documents:
        conn.execute("UPDATE documents SET is_current = 0 WHERE is_current != 0")
    elif tenant_id is not None:
        conn.execute("""UPDATE documents SET is_current = 0 WHERE is_current != 0
            AND calculation_id IN (SELECT id FROM calculations WHERE tenant_id = ?)""", (tenant_id,))
    elif calculation_ids:
        conn.executemany("UPDATE documents SET is_current = 0 WHERE calculation_id = ? AND is_current != 0",
                         ((i,) for i in calculation_ids))


def document_for_calculation(conn: sqlite3.Connection, calculation_id: int, kind: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM documents WHERE calculation_id = ? AND type = ?",
                       (calculation_id, kind)).fetchone()
    return dict(row) if row else None


def next_document_number(conn: sqlite3.Connection, kind: str, year: int) -> int:
    row = conn.execute("""SELECT MAX(number) FROM documents
        WHERE type = ? AND substr(document_date, 1, 4) = ?""", (kind, f"{year:04d}")).fetchone()
    return (row[0] or 0) + 1


def duplicate_document_ids(conn: sqlite3.Connection, kind: str, year: int,
                           number: int, exclude_id: int | None = None) -> tuple[int, ...]:
    rows = conn.execute("""SELECT id FROM documents WHERE type = ? AND number = ?
        AND substr(document_date, 1, 4) = ? AND (? IS NULL OR id != ?) ORDER BY id""",
        (kind, number, f"{year:04d}", exclude_id, exclude_id)).fetchall()
    return tuple(row[0] for row in rows)
