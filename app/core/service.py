"""Транзакционный API бизнес-правил для будущих GUI и генератора документов.

Методы записи открывают собственную транзакцию. ConfirmationRequired означает,
что ничего не записано; UI может показать причину и повторить вызов с флагом.
"""
from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from app.data import database as db
from app.data import queries
from .calculations import amount_in_words, decimal_value, recalculate
from .numbering import require_document_date, require_document_number, require_document_type
from .validation import ValidationResult, validate_calculation


class ConfirmationRequired(ValueError):
    def __init__(self, code: str, affected_ids: tuple[int, ...] = ()) -> None:
        self.code = code
        self.affected_ids = affected_ids
        super().__init__(f"Необходимо подтверждение: {code}; связанные ID: {affected_ids}")


@dataclass(frozen=True)
class StartReadingProposal:
    value: Decimal | None
    source: str
    requires_confirmation: bool = False


class CalculationService:
    def __init__(self, database: db.Database) -> None:
        self.database = database

    @staticmethod
    def _month(year: int, month: int) -> tuple[str, str]:
        if type(year) is not int or type(month) is not int or not (1 <= year <= 9999 and 1 <= month <= 12):
            raise ValueError("Некорректный год или месяц.")
        return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"

    @staticmethod
    def _previous_month(year: int, month: int) -> tuple[int, int] | None:
        if month == 1:
            return (year - 1, 12) if year > 1 else None
        return year, month - 1

    @staticmethod
    def _require_tenant(conn: Any, tenant_id: int) -> dict[str, Any]:
        tenant = db.get_tenant(conn, tenant_id)
        if tenant is None:
            raise LookupError(f"Арендатор {tenant_id} не найден.")
        return tenant

    @staticmethod
    def _require_calculation(conn: Any, calculation_id: int) -> dict[str, Any]:
        calculation = db.get_calculation(conn, calculation_id)
        if calculation is None:
            raise LookupError(f"Расчёт {calculation_id} не найден.")
        return calculation

    @staticmethod
    def _calculation_fingerprint(calc: Mapping[str, Any]) -> str:
        """Обнаруживает технические изменения даже при сохранении updated_at.

        Используется только для проверки подготовленного PDF перед отметкой
        генерации; статус актуальности по-прежнему хранится в documents.
        """
        fields = ("tenant_id", "year", "month", "period_start", "period_end",
                  "start_reading", "end_reading", "consumption", "total_amount",
                  "manual_consumption", "manual_amount", "start_reading_source",
                  "is_ready")
        payload = repr(tuple(str(calc[field]) for field in fields))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _refresh_inherited_calculation(self, conn: Any, calc: Mapping[str, Any], *,
                                      force: bool = False) -> None:
        """Синхронизирует одно автоматическое начало с календарным месяцем записи.

        При переносе периода force=True сбрасывает ручные overrides даже при
        совпавшем числовом показании: источник теперь другой месяц.
        """
        if calc["start_reading_source"] != "previous_month":
            return
        previous = self._previous_month(calc["year"], calc["month"])
        inherited = queries.previous_month_reading(
            conn, calc["tenant_id"], *previous) if previous else None
        if not force and inherited == calc["start_reading"]:
            return
        tariff = queries.month_tariff(conn, calc["year"], calc["month"])
        consumption, amount = recalculate(
            inherited, calc["end_reading"], tariff["rate"] if tariff else None)
        values = {**calc, "start_reading": inherited, "consumption": consumption,
                  "total_amount": amount, "manual_consumption": False, "manual_amount": False}
        values["is_ready"] = validate_calculation(
            values, tariff["rate"] if tariff else None).ready
        queries.update_inherited_reading(
            conn, calc["id"], start_reading=inherited, consumption=consumption,
            amount=amount, is_ready=values["is_ready"])
        queries.invalidate_documents(conn, (calc["id"],))

    def _refresh_auto_starts(self, conn: Any, tenant_id: int, since: str) -> None:
        """Синхронизирует зависимые месяцы в той же транзакции, сохраняя порядок правок.

        Не перезаписывает ручные начальные показания и не меняет updated_at
        технически пересчитанных записей.
        """
        for calc in queries.list_future_calculations(conn, tenant_id, since):
            self._refresh_inherited_calculation(conn, calc)

    def _suggest_start(self, conn: Any, tenant_id: int, year: int, month: int,
                       period_start: str) -> StartReadingProposal:
        tenant = self._require_tenant(conn, tenant_id)
        previous = self._previous_month(year, month)
        if previous:
            value = queries.previous_month_reading(conn, tenant_id, *previous)
            if value is not None:
                return StartReadingProposal(value, "previous_month")
            settings = db.get_settings(conn)
            if settings and settings["reuse_manual_start_readings"]:
                manual = queries.previous_manual_start(conn, tenant_id, *previous)
                if manual is not None:
                    # Начало без соответствующего конца не обязано совпадать с
                    # показанием на конец месяца. Требуем отдельного подтверждения.
                    return StartReadingProposal(manual, "previous_month", True)
        if (tenant["initial_reading"] is not None and tenant["initial_reading_date"] is not None
                and tenant["initial_reading_date"] <= period_start
                and not queries.has_earlier_calculation(conn, tenant_id, period_start)):
            return StartReadingProposal(tenant["initial_reading"], "initial")
        return StartReadingProposal(None, "unspecified")

    def suggest_start_reading(self, tenant_id: int, year: int, month: int,
                              period_start: date | str | None = None) -> StartReadingProposal:
        default_start, _ = self._month(year, month)
        start, _ = db._require_period(year, month, period_start or default_start,
                                      period_start or default_start)
        with self.database.connection() as conn:
            return self._suggest_start(conn, tenant_id, year, month, start)

    def overlapping_calculations(self, tenant_id: int, year: int, month: int,
                                 period_start: date | str, period_end: date | str) -> tuple[int, ...]:
        start, end = db._require_period(year, month, period_start, period_end)
        with self.database.connection() as conn:
            return queries.overlapping_ids(conn, tenant_id, year, month, start, end)

    def create_calculation(self, tenant_id: int, year: int, month: int, *,
                           period_start: date | str | None = None,
                           period_end: date | str | None = None,
                           start_reading: Decimal | str | int | None = None,
                           end_reading: Decimal | str | int | None = None,
                           confirm_overlap: bool = False,
                           confirm_manual_reuse: bool = False,
                           confirm_history: bool = False) -> dict[str, Any]:
        default_start, default_end = self._month(year, month)
        start, end = db._require_period(year, month, period_start or default_start,
                                        period_end or default_end)
        with self.database.transaction() as conn:
            self._require_tenant(conn, tenant_id)
            overlaps = queries.overlapping_ids(conn, tenant_id, year, month, start, end)
            if overlaps and not confirm_overlap:
                raise ConfirmationRequired("overlapping_period", overlaps)
            if start_reading is not None:
                initial = decimal_value(start_reading, 3)
                source = "manual"
            else:
                proposed = self._suggest_start(conn, tenant_id, year, month, start)
                if proposed.requires_confirmation and not confirm_manual_reuse:
                    raise ConfirmationRequired("reuse_manual_start_reading")
                initial, source = proposed.value, proposed.source
            finish = decimal_value(end_reading, 3, allow_none=True)
            tariff = queries.month_tariff(conn, year, month)
            consumption, amount = recalculate(initial, finish, tariff["rate"] if tariff else None)
            values = dict(tenant_id=tenant_id, year=year, month=month,
                          period_start=start, period_end=end, start_reading=initial,
                          end_reading=finish, consumption=consumption, total_amount=amount,
                          start_reading_source=source)
            values["is_ready"] = validate_calculation(values, tariff["rate"] if tariff else None).ready
            # Добавление завершённого источника задним числом может изменить
            # автоподстановку у уже существующих последующих расчётов.
            dependents = (queries.later_ids(conn, tenant_id, start)
                          if finish is not None else ())
            if dependents and not confirm_history:
                raise ConfirmationRequired("historical_readings", dependents)
            calc_id = db.create_calculation(conn, **values)
            if dependents:
                self._refresh_auto_starts(conn, tenant_id, start)
            return db.get_calculation(conn, calc_id)

    def get_calculation(self, calculation_id: int) -> dict[str, Any]:
        with self.database.connection() as conn:
            return self._require_calculation(conn, calculation_id)

    def validate(self, calculation_id: int) -> ValidationResult:
        with self.database.connection() as conn:
            calc = self._require_calculation(conn, calculation_id)
            tariff = queries.month_tariff(conn, calc["year"], calc["month"])
            return validate_calculation(calc, tariff["rate"] if tariff else None)

    def later_calculations(self, calculation_id: int) -> tuple[int, ...]:
        with self.database.connection() as conn:
            calc = self._require_calculation(conn, calculation_id)
            return queries.later_ids(conn, calc["tenant_id"], calc["period_start"],
                                     exclude_id=calculation_id)

    def update_calculation(self, calculation_id: int, *, confirm_manual: bool = False,
                           confirm_history: bool = False, **changes: Any) -> dict[str, Any]:
        allowed = {"start_reading", "end_reading", "consumption", "total_amount",
                   "period_start", "period_end", "year", "month"}
        if not changes or not set(changes) <= allowed:
            raise ValueError("Неизвестные или отсутствующие изменения расчёта.")
        with self.database.transaction() as conn:
            current = self._require_calculation(conn, calculation_id)
            values = {**current}
            for key, value in changes.items():
                if key in ("start_reading", "end_reading", "consumption"):
                    value = decimal_value(value, 3, allow_none=key != "consumption")
                elif key == "total_amount":
                    value = decimal_value(value, 2)
                elif key in ("period_start", "period_end"):
                    value = db._date_text(value)
                values[key] = value
            values["period_start"], values["period_end"] = db._require_period(
                values["year"], values["month"], values["period_start"], values["period_end"])
            reading_changed = any(key in changes and values[key] != current[key]
                                  for key in ("start_reading", "end_reading"))
            period_changed = any(key in changes and values[key] != current[key]
                                 for key in ("year", "month", "period_start", "period_end"))
            month_changed = ((values["year"], values["month"]) !=
                             (current["year"], current["month"]))
            consumption_changed = "consumption" in changes and values["consumption"] != current["consumption"]
            amount_changed = "total_amount" in changes and values["total_amount"] != current["total_amount"]
            if reading_changed and "consumption" in changes or (reading_changed or consumption_changed or period_changed) and "total_amount" in changes:
                raise ValueError("Ручные корректировки вводятся отдельно от изменения исходных данных.")
            if (consumption_changed or amount_changed) and not confirm_manual:
                raise ConfirmationRequired("manual_override")
            if reading_changed or period_changed:
                history_start = min(current["period_start"], values["period_start"])
                dependents = queries.later_ids(conn, current["tenant_id"], history_start,
                                               exclude_id=calculation_id)
                if dependents and not confirm_history:
                    raise ConfirmationRequired("historical_readings", dependents)
            if reading_changed:
                values["manual_consumption"] = False
                values["manual_amount"] = False
            if "start_reading" in changes:
                # Явный ввод = ручное начало, даже если число не отличается
                # от ранее подставленного. Иначе последующее исправление
                # истории незаметно перезапишет выбор пользователя.
                values["start_reading_source"] = (
                    "manual" if values["start_reading"] is not None else "unspecified")
            if consumption_changed:
                values["manual_consumption"] = True
                values["manual_amount"] = False
            if period_changed:
                values["manual_amount"] = False
            if month_changed and "start_reading" in changes:
                # Даже когда явное значение численно совпало с прежним,
                # пользователь указал новое ручное начало перенесённого периода.
                values["start_reading_source"] = (
                    "manual" if values["start_reading"] is not None else "unspecified")
                values["manual_consumption"] = False
            if amount_changed:
                values["manual_amount"] = True
            tariff = queries.month_tariff(conn, values["year"], values["month"])
            values["consumption"], values["total_amount"] = recalculate(
                values["start_reading"], values["end_reading"], tariff["rate"] if tariff else None,
                consumption=values["consumption"], amount=values["total_amount"],
                manual_consumption=bool(values["manual_consumption"]),
                manual_amount=bool(values["manual_amount"]))
            values["is_ready"] = validate_calculation(values, tariff["rate"] if tariff else None).ready
            updates = {key: value for key, value in values.items()
                       if key in db._CALCULATION_COLUMNS and value != current[key]}
            if updates:
                db.update_calculation(conn, calculation_id, **updates)
                queries.invalidate_documents(conn, (calculation_id,))
                if month_changed:
                    # Сначала обновить само унаследованное начало после
                    # переноса: оно может иметь period_start == history_start,
                    # поэтому не попадёт в выборку последующих расчётов.
                    moved = self._require_calculation(conn, calculation_id)
                    self._refresh_inherited_calculation(conn, moved, force=True)
                if reading_changed or period_changed:
                    self._refresh_auto_starts(
                        conn, current["tenant_id"],
                        min(current["period_start"], values["period_start"]))
            return db.get_calculation(conn, calculation_id)

    def set_tariff(self, year: int, month: int, rate: Decimal | str | int, *,
                   confirm_recalculation: bool = False) -> dict[str, Any]:
        self._month(year, month)
        new_rate = decimal_value(rate, 4)
        with self.database.transaction() as conn:
            old = queries.month_tariff(conn, year, month)
            if old and old["rate"] == new_rate:
                return old
            calculations = db.list_calculations(conn, year=year, month=month)
            ids = tuple(calc["id"] for calc in calculations)
            if old is not None and ids and not confirm_recalculation:
                raise ConfirmationRequired("tariff_recalculation", ids)
            if old is None:
                tariff_id = db.create_tariff(conn, year=year, month=month, rate=new_rate)
            else:
                tariff_id = old["id"]
                db.update_tariff(conn, tariff_id, rate=new_rate)
            for calc in calculations:
                consumption, amount = recalculate(calc["start_reading"], calc["end_reading"], new_rate,
                                                  consumption=calc["consumption"],
                                                  manual_consumption=bool(calc["manual_consumption"]))
                values = {**calc, "consumption": consumption, "total_amount": amount,
                          "manual_amount": False}
                values["is_ready"] = validate_calculation(values, new_rate).ready
                queries.update_tariff_derived_values(
                    conn, calc["id"], consumption=consumption, amount=amount,
                    is_ready=values["is_ready"])
            queries.invalidate_documents(conn, ids)
            return queries.month_tariff(conn, year, month)

    def create_tenant(self, *, name: str, **details: Any) -> dict[str, Any]:
        """Создать карточку с теми же проверками начальных показаний, что и при правке."""
        if "initial_reading" in details:
            details["initial_reading"] = decimal_value(
                details["initial_reading"], 3, allow_none=True)
        with self.database.transaction() as conn:
            tenant_id = db.create_tenant(conn, name=name, **details)
            return db.get_tenant(conn, tenant_id)

    def update_tenant(self, tenant_id: int, **changes: Any) -> dict[str, Any]:
        if "initial_reading" in changes:
            changes["initial_reading"] = decimal_value(
                changes["initial_reading"], 3, allow_none=True)
        with self.database.transaction() as conn:
            current = self._require_tenant(conn, tenant_id)
            if not changes:
                return current
            db.update_tenant(conn, tenant_id, **changes)
            updated = db.get_tenant(conn, tenant_id)
            if any(updated[k] != current[k] for k in changes):
                queries.invalidate_documents(conn, tenant_id=tenant_id)
            return updated

    def update_settings(self, **changes: Any) -> dict[str, Any]:
        with self.database.transaction() as conn:
            previous = db.get_settings(conn)
            if not changes:
                return previous
            db.update_settings(conn, **changes)
            updated = db.get_settings(conn)
            if any(k != "reuse_manual_start_readings" and updated[k] != previous[k] for k in changes):
                queries.invalidate_documents(conn, all_documents=True)
            return updated

    def delete_calculation(self, calculation_id: int, *, confirm_delete: bool = False,
                           confirm_history: bool = False) -> None:
        with self.database.transaction() as conn:
            calc = self._require_calculation(conn, calculation_id)
            dependents = queries.later_ids(conn, calc["tenant_id"], calc["period_start"],
                                           exclude_id=calculation_id)
            if not confirm_delete:
                raise ConfirmationRequired("delete_calculation", dependents)
            if dependents and not confirm_history:
                raise ConfirmationRequired("historical_readings", dependents)
            db.delete_calculation(conn, calculation_id)
            self._refresh_auto_starts(conn, calc["tenant_id"], calc["period_start"])

    def next_document_number(self, kind: str, document_date: date | str) -> int:
        require_document_type(kind)
        document_year = date.fromisoformat(require_document_date(document_date)).year
        with self.database.connection() as conn:
            return queries.next_document_number(conn, kind, document_year)

    def document_number_duplicates(self, kind: str, number: int, document_date: date | str,
                                   *, exclude_id: int | None = None) -> tuple[int, ...]:
        require_document_type(kind)
        require_document_number(number)
        document_year = date.fromisoformat(require_document_date(document_date)).year
        with self.database.connection() as conn:
            return queries.duplicate_document_ids(conn, kind, document_year, number, exclude_id)

    def reserve_document(self, calculation_id: int, kind: str, *,
                         document_date: date | str | None = None, number: int | None = None,
                         confirm_duplicate: bool = False, confirm_zero: bool = False) -> dict[str, Any]:
        """Создаёт/меняет лишь метаданные; никогда не объявляет PDF созданным."""
        require_document_type(kind)
        with self.database.transaction() as conn:
            calc = self._require_calculation(conn, calculation_id)
            tariff = queries.month_tariff(conn, calc["year"], calc["month"])
            result = validate_calculation(calc, tariff["rate"] if tariff else None)
            if not result.ready:
                raise ValueError("Документ недоступен: " + "; ".join(result.errors))
            if result.zero_consumption and not confirm_zero:
                raise ConfirmationRequired("zero_consumption")
            existing = queries.document_for_calculation(conn, calculation_id, kind)
            d = require_document_date(document_date or (existing["document_date"] if existing else date.today()))
            n = require_document_number(number if number is not None else
                                        (existing["number"] if existing else queries.next_document_number(
                                            conn, kind, date.fromisoformat(d).year)))
            duplicates = queries.duplicate_document_ids(conn, kind, date.fromisoformat(d).year,
                                                        n, existing["id"] if existing else None)
            if duplicates and not confirm_duplicate and (
                    existing is None or existing["number"] != n or existing["document_date"] != d):
                raise ConfirmationRequired("duplicate_document_number", duplicates)
            if existing:
                if existing["number"] != n or existing["document_date"] != d:
                    db.update_document(conn, existing["id"], number=n, document_date=d, is_current=False)
                return db.get_document(conn, existing["id"])
            document_id = db.create_document(conn, calculation_id=calculation_id, type=kind,
                                             number=n, document_date=d)
            return db.get_document(conn, document_id)

    def document_status(self, calculation_id: int, kind: str) -> str:
        require_document_type(kind)
        with self.database.connection() as conn:
            self._require_calculation(conn, calculation_id)
            document = queries.document_for_calculation(conn, calculation_id, kind)
            if not document or not document["last_generated_at"]:
                return "not_created"
            return "current" if document["is_current"] else "needs_update"

    def prepare_document(self, calculation_id: int, kind: str, *,
                         confirm_zero: bool = False) -> dict[str, Any]:
        """Снимок для PDF: только данные, без выдачи номера и отметки генерации."""
        require_document_type(kind)
        with self.database.connection() as conn:
            calc = self._require_calculation(conn, calculation_id)
            tariff = queries.month_tariff(conn, calc["year"], calc["month"])
            checked = validate_calculation(calc, tariff["rate"] if tariff else None)
            if not checked.ready:
                raise ValueError("Документ недоступен: " + "; ".join(checked.errors))
            if checked.zero_consumption and not confirm_zero:
                raise ConfirmationRequired("zero_consumption")
            document = queries.document_for_calculation(conn, calculation_id, kind)
            tenant = self._require_tenant(conn, calc["tenant_id"])
            settings = db.get_settings(conn)
            return {"calculation": calc, "tariff": tariff, "tenant": tenant,
                    "performer": settings, "document": document,
                    "amount_in_words": amount_in_words(calc["total_amount"]),
                    "revision": {"calculation": calc["updated_at"],
                                 "calculation_values": self._calculation_fingerprint(calc),
                                 "tariff": tariff["updated_at"],
                                 "tenant": tenant["updated_at"], "performer": settings["updated_at"],
                                 "document": document["updated_at"] if document else None}}

    def mark_document_generated(self, document_id: int, *, expected_revision: Mapping[str, str | None]) -> dict[str, Any]:
        """Вызывать ТОЛЬКО после успешного сохранения PDF; защищает от гонки правок."""
        with self.database.transaction() as conn:
            document = db.get_document(conn, document_id)
            if document is None:
                raise LookupError(f"Документ {document_id} не найден.")
            calc = self._require_calculation(conn, document["calculation_id"])
            tariff = queries.month_tariff(conn, calc["year"], calc["month"])
            tenant = self._require_tenant(conn, calc["tenant_id"])
            settings = db.get_settings(conn)
            current_revision = {"calculation": calc["updated_at"],
                                "calculation_values": self._calculation_fingerprint(calc),
                                "tariff": tariff["updated_at"] if tariff else None,
                                "tenant": tenant["updated_at"], "performer": settings["updated_at"],
                                "document": document["updated_at"]}
            if current_revision != dict(expected_revision) or not validate_calculation(
                    calc, tariff["rate"] if tariff else None).ready:
                raise ValueError("Данные изменились во время генерации; документ не отмечен актуальным.")
            db.update_document(conn, document_id, is_current=True, last_generated_at=db._timestamp())
            return db.get_document(conn, document_id)

    def bulk_candidates(self, year: int, month: int) -> dict[str, Any]:
        """Последний готовый для каждого активного арендатора; нули пропускаются."""
        self._month(year, month)
        selected: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        with self.database.connection() as conn:
            tariff = queries.month_tariff(conn, year, month)
            for tenant in db.list_tenants(conn):
                calculations = db.list_calculations(conn, tenant_id=tenant["id"], year=year, month=month)
                latest = next((c for c in calculations if validate_calculation(
                    c, tariff["rate"] if tariff else None).ready), None)
                if not calculations:
                    skipped.append({"tenant_id": tenant["id"], "reason": "no_calculation",
                                    "errors": ("Расчёты за выбранный месяц отсутствуют.",)})
                elif latest is None:
                    newest = calculations[0]
                    result = validate_calculation(newest, tariff["rate"] if tariff else None)
                    if tariff is None:
                        reason = "missing_tariff"
                    elif newest["start_reading"] is None or newest["end_reading"] is None:
                        reason = "incomplete_readings"
                    elif newest["end_reading"] < newest["start_reading"]:
                        reason = "invalid_readings"
                    else:
                        reason = "not_ready"
                    skipped.append({"tenant_id": tenant["id"], "reason": reason,
                                    "calculation_id": newest["id"], "errors": result.errors})
                elif latest["consumption"] == 0:
                    skipped.append({"tenant_id": tenant["id"], "reason": "zero_consumption",
                                    "calculation_id": latest["id"]})
                else:
                    selected.append(latest)
        return {"selected": selected, "skipped": skipped}
