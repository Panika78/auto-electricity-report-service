"""Готовность расчёта и правила экспорта — без UI и SQL."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping, Any

from .calculations import decimal_value


@dataclass(frozen=True)
class ValidationResult:
    ready: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    zero_consumption: bool

    def can_export(self, *, bulk: bool = False, confirm_zero: bool = False) -> bool:
        return self.ready and (not self.zero_consumption or (not bulk and confirm_zero))


def validate_calculation(calculation: Mapping[str, Any], tariff: Decimal | None) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        year, month = calculation["year"], calculation["month"]
        start = date.fromisoformat(str(calculation["period_start"]))
        end = date.fromisoformat(str(calculation["period_end"]))
        if (type(year) is not int or type(month) is not int or start > end
                or (start.year, start.month) != (year, month)
                or (end.year, end.month) != (year, month)):
            errors.append("Период должен находиться внутри выбранного месяца.")
    except (KeyError, ValueError, TypeError):
        errors.append("Некорректные даты расчётного периода.")
    readings: list[Decimal] = []
    for key, label in (("start_reading", "Начальное"), ("end_reading", "Конечное")):
        value = calculation.get(key)
        if value is None:
            errors.append(f"{label} показание не заполнено.")
        else:
            try:
                readings.append(decimal_value(value, 3))
            except (ValueError, TypeError):
                errors.append(f"{label} показание некорректно.")
    if len(readings) == 2 and readings[1] < readings[0]:
        errors.append("Конечное показание меньше начального.")
    consumption = calculation.get("consumption")
    if consumption is None:
        errors.append("Расход не рассчитан.")
    else:
        try:
            decimal_value(consumption, 3)
        except (ValueError, TypeError):
            errors.append("Расход отрицательный или некорректный.")
    if tariff is None:
        errors.append("Тариф за месяц не установлен.")
    else:
        try:
            decimal_value(tariff, 4)
        except (ValueError, TypeError):
            errors.append("Тариф некорректен.")
    amount = calculation.get("total_amount")
    if amount is None:
        errors.append("Сумма не рассчитана.")
    else:
        try:
            decimal_value(amount, 2)
        except (ValueError, TypeError):
            errors.append("Сумма отрицательная или некорректная.")
    if calculation.get("manual_consumption"):
        warnings.append("Расход изменён вручную.")
    if calculation.get("manual_amount"):
        warnings.append("Сумма изменена вручную.")
    zero = consumption is not None and Decimal(str(consumption)) == 0 if not isinstance(consumption, float) else False
    if zero:
        warnings.append("Нулевой расход: индивидуальная выгрузка требует подтверждения, массовая пропускает.")
    return ValidationResult(not errors, tuple(errors), tuple(warnings), zero)
