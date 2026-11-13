"""Чистые Decimal-расчёты. Не импортирует SQLite, Qt и PDF."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

KOPECK = Decimal("0.01")


def decimal_value(value: Decimal | str | int | None, places: int, *,
                  allow_none: bool = False, non_negative: bool = True) -> Decimal | None:
    if value is None and allow_none:
        return None
    if isinstance(value, (float, bool)) or value is None:
        raise TypeError("Ожидается Decimal, строка или целое число (не float).")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Неверное десятичное значение.") from exc
    if not number.is_finite() or number.as_tuple().exponent < -places:
        raise ValueError(f"Число должно быть конечным, с точностью до {places} знаков.")
    if non_negative and number < 0:
        raise ValueError("Отрицательные значения не допускаются.")
    return number


def calculate_consumption(start: Decimal | str | int, end: Decimal | str | int) -> Decimal:
    """Отрицательный результат допустим как *ошибка черновика*, не для экспорта."""
    first = decimal_value(start, 3)
    last = decimal_value(end, 3)
    # Decimal uses the active context even for subtraction. Preserve every
    # integer and fractional digit instead of silently rounding large readings.
    with localcontext() as context:
        context.prec = max(
            28,
            len(first.as_tuple().digits) + len(last.as_tuple().digits) + 2,
            max(first.adjusted(), last.adjusted()) + 5,
        )
        return last - first


def calculate_total(consumption: Decimal | str | int, tariff: Decimal | str | int) -> Decimal:
    quantity = decimal_value(consumption, 3)
    rate = decimal_value(tariff, 4)
    with localcontext() as context:
        # Scientific notation can represent large integers with very few
        # coefficient digits (e.g. Decimal("1E+30")). Account for magnitude
        # as well as coefficient length before rounding to kopecks.
        context.prec = max(
            28,
            len(quantity.as_tuple().digits) + len(rate.as_tuple().digits) + 5,
            quantity.adjusted() + rate.adjusted() + 5,
        )
        return (quantity * rate).quantize(KOPECK, rounding=ROUND_HALF_UP)


_ONES = ("", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_ONES_F = ("", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_TEENS = ("десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать")
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят",
         "семьдесят", "восемьдесят", "девяносто")
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот",
             "семьсот", "восемьсот", "девятьсот")
_UNITS = (
    ("", "", "", False),
    ("тысяча", "тысячи", "тысяч", True),
    ("миллион", "миллиона", "миллионов", False),
    ("миллиард", "миллиарда", "миллиардов", False),
    ("триллион", "триллиона", "триллионов", False),
    ("квадриллион", "квадриллиона", "квадриллионов", False),
)


def _form(value: int, forms: tuple[str, str, str] | tuple[str, str, str, bool]) -> str:
    if 11 <= value % 100 <= 14:
        return forms[2]
    last = value % 10
    return forms[0] if last == 1 else forms[1] if 2 <= last <= 4 else forms[2]


def _triad(value: int, feminine: bool) -> list[str]:
    parts = []
    hundreds, remainder = divmod(value, 100)
    tens, ones = divmod(remainder, 10)
    if hundreds:
        parts.append(_HUNDREDS[hundreds])
    if tens == 1:
        parts.append(_TEENS[ones])
    else:
        if tens:
            parts.append(_TENS[tens])
        if ones:
            parts.append((_ONES_F if feminine else _ONES)[ones])
    return parts


def amount_in_words(amount: Decimal | str | int) -> str:
    """Рубли прописью, копейки двумя цифрами; поддерживаются суммы до 10**18 рублей."""
    number = decimal_value(amount, 2)
    rubles = int(number)
    # Extract kopecks exactly, regardless of the caller's decimal context.
    with localcontext() as context:
        context.prec = max(28, len(number.as_tuple().digits) + 3, number.adjusted() + 5)
        kopecks = int((number - rubles) * 100)
    if rubles >= 1000 ** len(_UNITS):
        raise ValueError("Сумма слишком велика для записи прописью.")
    words: list[str] = []
    remainder = rubles
    groups = []
    while remainder:
        remainder, triad = divmod(remainder, 1000)
        groups.append(triad)
    for index in range(len(groups) - 1, -1, -1):
        triad = groups[index]
        if not triad:
            continue
        words.extend(_triad(triad, _UNITS[index][3]))
        if index:
            words.append(_form(triad, _UNITS[index]))
    if not words:
        words.append("ноль")
    words.append(_form(rubles, ("рубль", "рубля", "рублей")))
    words.extend((f"{kopecks:02d}", _form(kopecks, ("копейка", "копейки", "копеек"))))
    result = " ".join(words)
    return result[0].upper() + result[1:]


def recalculate(start: Decimal | None, end: Decimal | None, tariff: Decimal | None, *,
                consumption: Decimal | None = None, amount: Decimal | None = None,
                manual_consumption: bool = False, manual_amount: bool = False) -> tuple[Decimal | None, Decimal | None]:
    """Пересчёт без побочных эффектов. Отрицательная разница — незавершённый черновик."""
    if not manual_consumption:
        consumption = calculate_consumption(start, end) if start is not None and end is not None else None
    if not manual_amount:
        amount = (calculate_total(consumption, tariff)
                  if consumption is not None and consumption >= 0 and tariff is not None else None)
    return consumption, amount
