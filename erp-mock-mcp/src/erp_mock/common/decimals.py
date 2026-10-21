"""Decimal arithmetic and canonical text for qty, price, money and rate values.

Engine numbers are always ``decimal.Decimal`` (requirement 4.7). Binary floats appear only
at the JSON input boundary, where :func:`to_decimal` converts them through ``repr``.

Rounding is ROUND_HALF_UP on the exact value: when the discarded part is at least half a
unit in the last kept place, the kept part moves one unit away from zero (money 1.005
becomes 1.01, 1.0049 becomes 1.00). A result that rounds to zero is returned as +0, so -0
never reaches storage or output.

Both contexts set every field explicitly instead of copying the process-wide, mutable
``decimal.DefaultContext``, so their behaviour does not depend on what other code did to
the defaults. Their flags are sticky and shared between callers, but no result depends on
flags: only InvalidOperation, DivisionByZero and Overflow trap, and a trap depends on the
signal raised by the current operation alone.
"""

import decimal
import math
import re
from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

__all__ = [
    "PLACES",
    "RULE_CTX",
    "WIDE",
    "DecimalKind",
    "format_decimal",
    "round_fraction",
    "round_half_up",
    "to_decimal",
]

DecimalKind = Literal["qty", "price", "money", "rate"]

# Decimal places of each field type (requirement 4.7); read-only at runtime.
PLACES: Final[Mapping[DecimalKind, int]] = MappingProxyType(
    {"qty": 4, "price": 6, "money": 2, "rate": 4}
)


def _context(prec: int) -> decimal.Context:
    """Return a ROUND_HALF_UP context of ``prec`` significant digits, all fields explicit."""
    return decimal.Context(
        prec=prec,
        rounding=decimal.ROUND_HALF_UP,
        Emin=-999_999,
        Emax=999_999,
        capitals=1,
        clamp=0,
        traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
    )


# Field arithmetic. The product of two values of at most 30 significant digits each has
# at most 60 digits, so products are exact here and only the final rounding to field
# precision is inexact (requirement 6.1). Do not divide in WIDE: use round_fraction.
WIDE: Final = _context(80)

# Arithmetic in rule expressions: 28 significant digits, independent of field precision
# (requirement 10.12).
RULE_CTX: Final = _context(28)

# Decimal strings accepted where a number is expected (requirement 12.5). Used with
# fullmatch: "$" would also accept a trailing newline. [0-9] matches ASCII digits only.
_DECIMAL_STRING: Final = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


def _check_places(places: int) -> None:
    if places < 0:
        raise ValueError(f"places must not be negative, got {places}")


def round_half_up(x: Decimal, places: int) -> Decimal:
    """Round ``x`` to ``places`` decimal places, ROUND_HALF_UP on its exact value.

    The result has exponent ``-places`` (``round_half_up(Decimal("2000"), 2)`` is
    ``Decimal("2000.00")``) and is +0 when it rounds to zero.

    Raises ValueError for a non-finite ``x`` or a negative ``places``, and
    decimal.InvalidOperation when the result needs more than WIDE's 80 significant digits.
    """
    if not x.is_finite():
        raise ValueError(f"cannot round a non-finite value: {x}")
    _check_places(places)
    rounded = x.quantize(Decimal((0, (1,), -places)), rounding=decimal.ROUND_HALF_UP, context=WIDE)
    return rounded.copy_abs() if rounded.is_zero() else rounded


def round_fraction(num: int, den: int, places: int) -> Decimal:
    """Round the exact rational ``num / den`` to ``places`` decimal places, ROUND_HALF_UP.

    Integer arithmetic only, so the quotient is rounded once, with no intermediate
    rounding (requirement 6.2 divides by 1 + tax_rate this way). ``Decimal.as_integer_ratio``
    turns Decimal operands into integers. The result has exponent ``-places`` and is +0
    when it rounds to zero.

    Raises ValueError unless ``den`` is positive and ``places`` is not negative.
    """
    if den <= 0:
        raise ValueError(f"den must be positive, got {den}")
    _check_places(places)
    quotient, remainder = divmod(abs(num) * 10**places, den)
    if 2 * remainder >= den:
        quotient += 1
    sign = 1 if num < 0 and quotient != 0 else 0
    return Decimal((sign, Decimal(quotient).as_tuple().digits, -places))


def format_decimal(kind: DecimalKind, x: Decimal) -> str:
    """Return the canonical text of ``x`` as a ``kind`` value (Decimal_Formatter, req 4.8).

    ``x`` is first rounded to ``PLACES[kind]`` with round_half_up, so the text never has
    more decimals than the field precision (requirement 4.7). money keeps exactly 2
    decimals ("1700.00", "0.00"); qty, price and rate drop trailing zeros of the fraction
    and then a bare decimal point ("2000", "0.85", "0.000001", "0"). The text is plain
    notation without exponent, plus sign, grouping or whitespace; the integer part has no
    extra leading zeros, a negative value starts with "-", and zero is never "-0".

    Equal values give the same text, and values within the field precision round-trip:
    ``Decimal(format_decimal(kind, x)) == x`` (requirement 4.9).

    Uses ``format(x, "f")`` rather than ``Decimal.normalize()``, which depends on the
    context precision and can produce exponent notation. Raises ValueError for a
    non-finite ``x``.
    """
    text = format(round_half_up(x, PLACES[kind]), "f")
    if kind != "money" and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def to_decimal(value: int | float | str) -> Decimal:
    """Convert a JSON number or a decimal string to Decimal without binary rounding error.

    - int converts exactly.
    - float converts through ``repr``, the shortest text that reads back as the same
      float, so a JSON number of at most 15 significant digits gets exactly its written
      value (0.85 gives ``Decimal("0.85")``, requirement 12.4).
    - str must match ``^-?[0-9]+(\\.[0-9]+)?$`` and converts exactly (requirement 12.5).

    Raises TypeError for bool (a JSON boolean is not a number, requirement 12.13) and for
    any other type, and ValueError for a non-finite float or a string of another form
    (such as "1e3", " 5", "1,000", "+5" or ".5").
    """
    if isinstance(value, bool):
        raise TypeError("a bool is not a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"not a finite number: {value!r}")
        return Decimal(repr(value))
    if isinstance(value, str):
        if _DECIMAL_STRING.fullmatch(value) is None:
            raise ValueError(f"not a decimal string: {value!r}")
        return Decimal(value)
    raise TypeError(f"cannot convert {type(value).__name__} to Decimal")
