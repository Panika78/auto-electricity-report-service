"""SQLite: расположение базы, атомарные миграции и конкретные операции с данными.

Все функции изменения данных получают открытое соединение. Вызывать их следует
внутри `Database.transaction()`, чтобы несколько связанных операций откатывались
одновременно. Никакие SQL-запросы не выполняются в UI.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


class UnsupportedSchemaVersion(RuntimeError):
    """База была создана более новой версией приложения."""


def get_database_path() -> Path:
    """Путь к пользовательской БД, независимый от exe и текущего каталога."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise RuntimeError("Не определён системный каталог LOCALAPPDATA.")
        return Path(base) / "ElectricityReports" / "app.db"

    base = os.environ.get("XDG_DATA_HOME")
    data_root = Path(base) if base else Path.home() / ".local" / "share"
    return data_root / "ElectricityReports" / "app.db"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _decimal_text(value: Decimal | str | int | None, places: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, (float, bool)):
        raise TypeError("Десятичные значения передаются как Decimal или строка, не float.")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Неверное десятичное число: {value!r}") from exc
    if not number.is_finite():
        raise ValueError("NaN и бесконечность не допускаются.")
    if number.as_tuple().exponent < -places:
        raise ValueError(f"Допустимо не более {places} знаков после запятой.")
    return str(number)


def _date_text(value: date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise ValueError("Ожидается календарная дата без времени.")
    return date.fromisoformat(value if isinstance(value, str) else value.isoformat()).isoformat()


def _require_period(year: int, month: int, start: date | str, end: date | str) -> tuple[str, str]:
    _require_month(year, month)
    start_value = _date_text(start)
    end_value = _date_text(end)
    if start_value is None or end_value is None:
        raise ValueError("Начальная и конечная даты обязательны.")
    if start_value > end_value or any(
        date.fromisoformat(d).year != year or date.fromisoformat(d).month != month
        for d in (start_value, end_value)
    ):
        raise ValueError("Период должен находиться внутри одного календарного месяца.")
    return start_value, end_value


def _require_month(year: int, month: int) -> None:
    if type(year) is not int or type(month) is not int or not (1 <= year <= 9999 and 1 <= month <= 12):
        raise ValueError("Неверный год или месяц.")


def _as_dict(row: sqlite3.Row | None, decimal_fields: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for field in decimal_fields:
        if result[field] is not None:
            result[field] = Decimal(result[field])
    return result


# SQLite не умеет атомарно выполнять executescript(): он неявно делает COMMIT.
# Поэтому каждое выражение выполняется отдельно внутри BEGIN IMMEDIATE.
_INITIAL_SCHEMA = (
    """CREATE TABLE tenants (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL CHECK (trim(name) <> ''),
        inn TEXT NOT NULL DEFAULT '',
        kpp TEXT NOT NULL DEFAULT '',
        ogrn TEXT NOT NULL DEFAULT '',
        legal_address TEXT NOT NULL DEFAULT '',
        postal_address TEXT NOT NULL DEFAULT '',
        bank_name TEXT NOT NULL DEFAULT '',
        bik TEXT NOT NULL DEFAULT '',
        account_number TEXT NOT NULL DEFAULT '',
        correspondent_account TEXT NOT NULL DEFAULT '',
        contract_number TEXT NOT NULL DEFAULT '',
        contract_date TEXT,
        additional_agreement TEXT NOT NULL DEFAULT '',
        initial_reading TEXT,
        initial_reading_date TEXT,
        is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE tariffs (
        id INTEGER PRIMARY KEY,
        year INTEGER NOT NULL CHECK (year BETWEEN 1 AND 9999),
        month INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
        rate TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (year, month)
    )""",
    """CREATE TABLE calculations (
        id INTEGER PRIMARY KEY,
        tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
        year INTEGER NOT NULL CHECK (year BETWEEN 1 AND 9999),
        month INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        start_reading TEXT,
        end_reading TEXT,
        consumption TEXT,
        total_amount TEXT,
        manual_consumption INTEGER NOT NULL DEFAULT 0 CHECK (manual_consumption IN (0, 1)),
        manual_amount INTEGER NOT NULL DEFAULT 0 CHECK (manual_amount IN (0, 1)),
        start_reading_source TEXT NOT NULL DEFAULT 'unspecified'
          CHECK (start_reading_source IN ('unspecified', 'manual', 'initial', 'previous_month')),
        is_ready INTEGER NOT NULL DEFAULT 0 CHECK (is_ready IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (period_start <= period_end),
        CHECK (substr(period_start, 1, 7) = printf('%04d-%02d', year, month)),
        CHECK (substr(period_end, 1, 7) = printf('%04d-%02d', year, month))
    )""",
    "CREATE INDEX idx_calculations_tenant_period ON calculations(tenant_id, year, month, updated_at DESC, id DESC)",
    """CREATE TABLE documents (
        id INTEGER PRIMARY KEY,
        calculation_id INTEGER NOT NULL REFERENCES calculations(id) ON DELETE CASCADE,
        type TEXT NOT NULL CHECK (type IN ('act', 'invoice')),
        number INTEGER NOT NULL,
        document_date TEXT NOT NULL,
        is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
        last_generated_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (calculation_id, type)
    )""",
    """CREATE TABLE settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        performer_name TEXT NOT NULL DEFAULT '',
        inn TEXT NOT NULL DEFAULT '',
        kpp TEXT NOT NULL DEFAULT '',
        ogrn TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL DEFAULT '',
        bank_name TEXT NOT NULL DEFAULT '',
        bik TEXT NOT NULL DEFAULT '',
        account_number TEXT NOT NULL DEFAULT '',
        correspondent_account TEXT NOT NULL DEFAULT '',
        signature_path TEXT,
        stamp_path TEXT,
        reuse_manual_start_readings INTEGER NOT NULL DEFAULT 0
            CHECK (reuse_manual_start_readings IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
)


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    for sql in _INITIAL_SCHEMA:
        conn.execute(sql)
    now = _timestamp()
    conn.execute("INSERT INTO settings(id, created_at, updated_at) VALUES(1, ?, ?)", (now, now))


MIGRATIONS = (_migrate_to_v1,)


class Database:
    """Фабрика короткоживущих SQLite-соединений с явными транзакциями."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path is not None else get_database_path()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    f"Версия БД {version} новее версии приложения ({SCHEMA_VERSION}). "
                    "Используйте более новую версию программы."
                )
            for migration_version in range(version + 1, SCHEMA_VERSION + 1):
                MIGRATIONS[migration_version - 1](conn)
                conn.execute(f"PRAGMA user_version = {migration_version}")


_TENANT_COLUMNS = frozenset({
    "name", "inn", "kpp", "ogrn", "legal_address", "postal_address",
    "bank_name", "bik", "account_number", "correspondent_account",
    "contract_number", "contract_date", "additional_agreement", "initial_reading",
    "initial_reading_date", "is_archived",
})
_CALCULATION_COLUMNS = frozenset({
    "tenant_id", "year", "month", "period_start", "period_end", "start_reading",
    "end_reading", "consumption", "total_amount", "manual_consumption",
    "manual_amount", "start_reading_source", "is_ready",
})
_SETTINGS_COLUMNS = frozenset({
    "performer_name", "inn", "kpp", "ogrn", "address", "bank_name", "bik",
    "account_number", "correspondent_account", "signature_path", "stamp_path",
    "reuse_manual_start_readings",
})


def _update_columns(
    conn: sqlite3.Connection, table: str, record_id: int,
    changes: dict[str, Any], allowed: frozenset[str],
) -> None:
    """Только для фиксированных таблиц/белых списков, без произвольного SQL снаружи."""
    if not changes:
        raise ValueError("Нет изменений для сохранения.")
    if not set(changes) <= allowed:
        raise ValueError(f"Неизвестные поля: {', '.join(sorted(set(changes) - allowed))}")
    columns = ", ".join(f"{column} = ?" for column in changes)
    values = [*changes.values(), _timestamp(), record_id]
    cursor = conn.execute(
        f"UPDATE {table} SET {columns}, updated_at = ? WHERE id = ?", values
    )
    if cursor.rowcount != 1:
        raise LookupError(f"Запись {table} с ID {record_id} не найдена.")


def create_tenant(
    conn: sqlite3.Connection, *, name: str, inn: str = "", kpp: str = "", ogrn: str = "",
    legal_address: str = "", postal_address: str = "", bank_name: str = "",
    bik: str = "", account_number: str = "", correspondent_account: str = "",
    contract_number: str = "", contract_date: date | str | None = None,
    additional_agreement: str = "", initial_reading: Decimal | str | int | None = None,
    initial_reading_date: date | str | None = None, is_archived: bool = False,
) -> int:
    now = _timestamp()
    cursor = conn.execute(
        """INSERT INTO tenants(
            name, inn, kpp, ogrn, legal_address, postal_address, bank_name, bik,
            account_number, correspondent_account, contract_number, contract_date,
            additional_agreement, initial_reading, initial_reading_date, is_archived,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, inn, kpp, ogrn, legal_address, postal_address, bank_name, bik,
         account_number, correspondent_account, contract_number, _date_text(contract_date),
         additional_agreement, _decimal_text(initial_reading, 3),
         _date_text(initial_reading_date), int(is_archived), now, now),
    )
    return cursor.lastrowid


def get_tenant(conn: sqlite3.Connection, tenant_id: int) -> dict[str, Any] | None:
    return _as_dict(conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone(), ("initial_reading",))


def list_tenants(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM tenants" + ("" if include_archived else " WHERE is_archived = 0") + " ORDER BY id"
    return [_as_dict(row, ("initial_reading",)) for row in conn.execute(sql).fetchall()]


def update_tenant(conn: sqlite3.Connection, tenant_id: int, **changes: Any) -> None:
    if "initial_reading" in changes:
        changes["initial_reading"] = _decimal_text(changes["initial_reading"], 3)
    for field in ("initial_reading_date", "contract_date"):
        if field in changes:
            changes[field] = _date_text(changes[field])
    if "is_archived" in changes:
        changes["is_archived"] = int(changes["is_archived"])
    _update_columns(conn, "tenants", tenant_id, changes, _TENANT_COLUMNS)


def create_tariff(conn: sqlite3.Connection, *, year: int, month: int, rate: Decimal | str | int) -> int:
    _require_month(year, month)
    now = _timestamp()
    return conn.execute(
        "INSERT INTO tariffs(year, month, rate, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
        (year, month, _decimal_text(rate, 4), now, now),
    ).lastrowid


def get_tariff(conn: sqlite3.Connection, tariff_id: int) -> dict[str, Any] | None:
    return _as_dict(conn.execute("SELECT * FROM tariffs WHERE id = ?", (tariff_id,)).fetchone(), ("rate",))


def list_tariffs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [_as_dict(row, ("rate",)) for row in conn.execute("SELECT * FROM tariffs ORDER BY year, month").fetchall()]


def update_tariff(conn: sqlite3.Connection, tariff_id: int, **changes: Any) -> None:
    if "rate" in changes:
        changes["rate"] = _decimal_text(changes["rate"], 4)
    if "year" in changes or "month" in changes:
        current = get_tariff(conn, tariff_id)
        if current is None:
            raise LookupError(f"Тариф {tariff_id} не найден.")
        _require_month(changes.get("year", current["year"]), changes.get("month", current["month"]))
    _update_columns(conn, "tariffs", tariff_id, changes, frozenset({"year", "month", "rate"}))


def create_calculation(
    conn: sqlite3.Connection, *, tenant_id: int, year: int, month: int,
    period_start: date | str, period_end: date | str,
    start_reading: Decimal | str | int | None = None,
    end_reading: Decimal | str | int | None = None,
    consumption: Decimal | str | int | None = None,
    total_amount: Decimal | str | int | None = None,
    manual_consumption: bool = False, manual_amount: bool = False,
    start_reading_source: str = "unspecified", is_ready: bool = False,
) -> int:
    start, end = _require_period(year, month, period_start, period_end)
    now = _timestamp()
    return conn.execute(
        """INSERT INTO calculations (
          tenant_id, year, month, period_start, period_end, start_reading, end_reading,
          consumption, total_amount, manual_consumption, manual_amount,
          start_reading_source, is_ready, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tenant_id, year, month, start, end, _decimal_text(start_reading, 3),
         _decimal_text(end_reading, 3), _decimal_text(consumption, 3),
         _decimal_text(total_amount, 2), int(manual_consumption), int(manual_amount),
         start_reading_source, int(is_ready), now, now),
    ).lastrowid


def get_calculation(conn: sqlite3.Connection, calculation_id: int) -> dict[str, Any] | None:
    return _as_dict(conn.execute("SELECT * FROM calculations WHERE id = ?", (calculation_id,)).fetchone(),
                    ("start_reading", "end_reading", "consumption", "total_amount"))


def list_calculations(
    conn: sqlite3.Connection, *, tenant_id: int | None = None,
    year: int | None = None, month: int | None = None,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    args: list[int] = []
    for field, value in (("tenant_id", tenant_id), ("year", year), ("month", month)):
        if value is not None:
            filters.append(f"{field} = ?")
            args.append(value)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    rows = conn.execute(
        "SELECT * FROM calculations" + where + " ORDER BY updated_at DESC, id DESC", args
    ).fetchall()
    return [_as_dict(row, ("start_reading", "end_reading", "consumption", "total_amount")) for row in rows]


def update_calculation(conn: sqlite3.Connection, calculation_id: int, **changes: Any) -> None:
    if any(field in changes for field in ("year", "month", "period_start", "period_end")):
        current = get_calculation(conn, calculation_id)
        if current is None:
            raise LookupError(f"Расчёт {calculation_id} не найден.")
        changes["period_start"], changes["period_end"] = _require_period(
            changes.get("year", current["year"]), changes.get("month", current["month"]),
            changes.get("period_start", current["period_start"]),
            changes.get("period_end", current["period_end"]),
        )
    for field, places in (("start_reading", 3), ("end_reading", 3),
                          ("consumption", 3), ("total_amount", 2)):
        if field in changes:
            changes[field] = _decimal_text(changes[field], places)
    for field in ("manual_consumption", "manual_amount", "is_ready"):
        if field in changes:
            changes[field] = int(changes[field])
    _update_columns(conn, "calculations", calculation_id, changes, _CALCULATION_COLUMNS)


def delete_calculation(conn: sqlite3.Connection, calculation_id: int) -> None:
    conn.execute("DELETE FROM calculations WHERE id = ?", (calculation_id,))


def create_document(
    conn: sqlite3.Connection, *, calculation_id: int, type: str,
    number: int, document_date: date | str, is_current: bool = False,
    last_generated_at: str | None = None,
) -> int:
    now = _timestamp()
    return conn.execute(
        """INSERT INTO documents (
          calculation_id, type, number, document_date, is_current,
          last_generated_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (calculation_id, type, number, _date_text(document_date),
         int(is_current), last_generated_at, now, now),
    ).lastrowid


def get_document(conn: sqlite3.Connection, document_id: int) -> dict[str, Any] | None:
    return _as_dict(conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone())


def list_documents(conn: sqlite3.Connection, *, calculation_id: int | None = None) -> list[dict[str, Any]]:
    if calculation_id is None:
        rows = conn.execute("SELECT * FROM documents ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM documents WHERE calculation_id = ? ORDER BY id", (calculation_id,)).fetchall()
    return [_as_dict(row) for row in rows]


def update_document(conn: sqlite3.Connection, document_id: int, **changes: Any) -> None:
    if "document_date" in changes:
        changes["document_date"] = _date_text(changes["document_date"])
    if "is_current" in changes:
        changes["is_current"] = int(changes["is_current"])
    _update_columns(conn, "documents", document_id, changes,
                    frozenset({"calculation_id", "type", "number", "document_date",
                               "is_current", "last_generated_at"}))


def get_settings(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return _as_dict(conn.execute("SELECT * FROM settings WHERE id = 1").fetchone())


def update_settings(conn: sqlite3.Connection, **changes: Any) -> None:
    if "reuse_manual_start_readings" in changes:
        changes["reuse_manual_start_readings"] = int(changes["reuse_manual_start_readings"])
    _update_columns(conn, "settings", 1, changes, _SETTINGS_COLUMNS)
