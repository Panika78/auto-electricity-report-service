"""Нумерация документов по году *даты документа*, а не расчётного месяца."""
from __future__ import annotations
from datetime import date, datetime

DOCUMENT_TYPES = frozenset({"act", "invoice"})


def require_document_type(value: str) -> str:
    if value not in DOCUMENT_TYPES:
        raise ValueError("Тип документа должен быть act или invoice.")
    return value


def require_document_date(value: date | str) -> str:
    if isinstance(value, datetime):
        raise ValueError("Требуется дата без времени.")
    try:
        return date.fromisoformat(value if isinstance(value, str) else value.isoformat()).isoformat()
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Некорректная дата документа.") from exc


def require_document_number(number: int) -> int:
    if type(number) is not int or number < 1:
        raise ValueError("Номер документа должен быть положительным целым числом.")
    return number
