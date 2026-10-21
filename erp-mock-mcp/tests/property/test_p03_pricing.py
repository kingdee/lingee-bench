"""Property 3: the reference model of pricing and the sums of the header.

**Validates: Requirements 6.1, 6.2, 6.4, 6.5, 6.10**

For any bill with pricing drawn over the ranges of requirement 6.5 - 1 to 200 lines, a qty
from 0.0001 to 100000000 with at most 4 decimals, a price from 0 to 100000000 with at most
6 decimals, a tax rate from 0 to 1 with at most 4 decimals, the price and the rate possibly
empty, and ``price_includes_tax`` either way:

- the three amounts of every line are the ones ``ref_line`` gives, which spells
  requirements 6.1, 6.2 and 6.10 out once more on its own terms: exact rational
  arithmetic, one rounding to the cent per amount, and three empty amounts for a line
  whose qty, price or tax rate is empty;
- a line whose amounts are there has ``amount_with_tax`` equal to ``amount`` plus
  ``tax_amount``, whichever way its price reads;
- each of the three header amounts is the sum of the non-empty values of the lines, and 0
  when no line has one, which is INV-3 (requirement 6.4).

The reference keeps every value as a ``fractions.Fraction``, so no intermediate result is
rounded by the model itself: the product of the qty and the price is rounded once, and
under a price that includes tax the quotient by 1 + tax_rate is rounded once as well -
independently of how :mod:`erp_mock.domain.pricing` reaches them, which multiplies in a
wide Decimal context and divides through ``round_fraction``. The two are compared as exact
rationals.

Generators stay inside the ranges above and aim at the values where the rounding decides:
a whole number of units times a price of x.xx5 puts the product exactly half a cent away
from the kept value, and the sampled rates turn such an amount into a tax or a quotient
that sits on a half as well (0.5 of an odd cent, 0.04 over 1.6 = 0.025). Each value is
drawn both at the precision of its field, the form a write stores, and with the trailing
zeros of its fraction dropped, which is the same value written differently.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Final

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from erp_mock.common.decimals import PLACES, DecimalKind
from erp_mock.domain.pricing import AMOUNT_FIELDS, LineAmounts, LineInput, price_bill

# The three amounts of one line or of one header, in AMOUNT_FIELDS order, as exact
# rationals; a value is None where the amount is empty, and all three are for a line whose
# qty, price or tax rate is empty (requirement 6.10).
type RefAmounts = tuple[Fraction | None, ...]

# What the reference gives a line that has no amount (requirement 6.10); the primitive's
# own constant for it is pricing.NO_AMOUNTS.
NO_REF_AMOUNTS: Final[RefAmounts] = (None, None, None)

# Cents of one money unit (requirement 4.7).
CENTS: Final = 10 ** PLACES["money"]

# The largest qty and price of requirement 6.5: 100000000 is 10 ** 8.
LIMIT_EXPONENT: Final = 8

# The most lines one bill gets (requirement 6.5).
MAX_LINES: Final = 200


# --- the reference model -----------------------------------------------------------------


def round_money(value: Fraction) -> Fraction:
    """``value`` rounded to the 2 decimals of a money field, ROUND_HALF_UP (req 4.7).

    ``floor(|value| × 100 + 1/2)`` is one more than the kept cents exactly when what is
    dropped is at least half a cent; the sign goes back on afterwards, so a half moves
    away from zero.
    """
    cents = math.floor(abs(value) * CENTS + Fraction(1, 2))
    return Fraction(-cents if value < 0 else cents, CENTS)


def ref_line(line: LineInput, *, price_includes_tax: bool) -> RefAmounts:
    """The three amounts requirements 6.1, 6.2 and 6.10 give ``line``.

    The product of the qty and the price is the net amount under a net price and the total
    under a price that includes tax; it is rounded to the cent once. The net amount under a
    price that includes tax is the exact quotient of that total by 1 + tax_rate, rounded
    once as well, and the tax is what the rounded net leaves of the total. An empty qty,
    price or tax rate leaves all three amounts empty.
    """
    if line.qty is None or line.price is None or line.tax_rate is None:
        return NO_REF_AMOUNTS
    rate = Fraction(line.tax_rate)
    product = round_money(Fraction(line.qty) * Fraction(line.price))
    if price_includes_tax:
        amount = round_money(product / (1 + rate))
        return (amount, product - amount, product)
    tax_amount = round_money(product * rate)
    return (product, tax_amount, product + tax_amount)


def ref_header(lines: Iterable[RefAmounts]) -> RefAmounts:
    """The three header amounts: the sums of the non-empty line values (requirement 6.4).

    Each is 0 when no line has that amount, a bill whose every line is incomplete included.
    """
    totals = [Fraction(0), Fraction(0), Fraction(0)]
    for amounts in lines:
        for index, value in enumerate(amounts):
            if value is not None:
                totals[index] += value
    return tuple(totals)


def exact(values: Mapping[str, Decimal | None]) -> RefAmounts:
    """The three amounts the primitive produced, as exact rationals in AMOUNT_FIELDS order."""
    assert tuple(values) == AMOUNT_FIELDS
    return tuple(None if value is None else Fraction(value) for value in values.values())


def adds_up(found: LineAmounts) -> bool:
    """Whether ``amount_with_tax`` is ``amount`` plus ``tax_amount`` (requirements 6.1, 6.2).

    The claim is about a line whose amounts are there; a line with an empty amount is left
    to the comparison with the reference, which has all three empty together.
    """
    amount, tax_amount, total = found.amount, found.tax_amount, found.amount_with_tax
    if amount is None or tax_amount is None or total is None:
        return True
    return total == amount + tax_amount


# --- generators --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bill:
    """The lines of one bill with pricing and the reading of their price (requirement 6.3)."""

    lines: tuple[LineInput, ...]
    price_includes_tax: bool


def scaled(units: int, places: int) -> Decimal:
    """``units`` counts of the last of ``places`` decimal places, as a Decimal value."""
    return Decimal(f"{units}E-{places}")


def trimmed(value: Decimal) -> Decimal:
    """``value`` with the trailing zeros of its fraction dropped ("0.85" for "0.850000")."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return Decimal(text)


def field_values(kind: DecimalKind, units: st.SearchStrategy[int]) -> st.SearchStrategy[Decimal]:
    """Draw a ``kind`` value from ``units`` counts of its last decimal place.

    Half the draws keep the precision of the field, the form a write stores (requirement
    12.6 rounds a caller's number to it); the other half write the same value with fewer
    decimals, which must give the same amounts.
    """
    at_precision = units.map(lambda count: scaled(count, PLACES[kind]))
    return st.one_of(at_precision, at_precision.map(trimmed))


# Odd whole quantities, which put the product of a line exactly on half a cent together
# with a price of x.xx5: an odd multiple of such a price is itself of the form x.xx5.
WHOLE_QTY: Final = st.integers(min_value=1, max_value=10**6).map(lambda n: Decimal(2 * n - 1))

# Prices of the form x.xx5, from 0.005 to 1000.005, half a cent away from the cent below
# them on their own.
HALF_CENT_PRICE: Final = st.integers(min_value=0, max_value=10**5).map(
    lambda n: scaled(10 * n + 5, 3)
)

# Tax rates that keep a tax or a quotient on a half as well: the four rates of the example
# pack (0, 0.06, 0.09 and 0.13), and rates whose 1 + rate divides a round total into a
# half (0.04 over 1.6 is 0.025) or whose tax of an odd cent is half a cent (0.5 of 0.07 is
# 0.035).
SAMPLED_RATES: Final = st.sampled_from(
    [Decimal(text) for text in ("0", "0.06", "0.09", "0.13", "0.17", "0.25", "0.5", "0.6", "1")]
)

# The ranges of requirement 6.5, with the sampled values above mixed in: a qty from 0.0001
# to 100000000, a price from 0 to 100000000 and a tax rate from 0 to 1.
QTY: Final = st.one_of(
    field_values("qty", st.integers(min_value=1, max_value=10 ** (LIMIT_EXPONENT + PLACES["qty"]))),
    WHOLE_QTY,
)
PRICE: Final = st.one_of(
    field_values(
        "price", st.integers(min_value=0, max_value=10 ** (LIMIT_EXPONENT + PLACES["price"]))
    ),
    HALF_CENT_PRICE,
)
RATE: Final = st.one_of(
    field_values("rate", st.integers(min_value=0, max_value=10 ** PLACES["rate"])),
    SAMPLED_RATES,
)

# How often the price or the tax rate of a line is empty (requirement 6.10), drawn once per
# bill: in no line, in a third of the values, or in every line - the last leaves the three
# header amounts at 0 (requirement 6.4).
EMPTY_SHARES: Final = st.sampled_from([(False,), (False, False, True), (True,)])

# The 1 to 200 lines of requirement 6.5: short bills shrink and read well, and half of the
# draws reach the long ones, whose header sums up to 200 values.
LINE_COUNTS: Final = st.one_of(
    st.integers(min_value=1, max_value=20),
    st.integers(min_value=1, max_value=MAX_LINES),
)


@st.composite
def line_inputs(draw: st.DrawFn, empty: st.SearchStrategy[bool]) -> LineInput:
    """Draw the qty, price and tax rate of one line (requirement 6.5).

    The price and the tax rate are empty where ``empty`` draws true (requirement 6.10); the
    qty is always there, as requirement 6.5 draws it, and the unit tests cover an empty one.
    """
    return LineInput(
        qty=draw(QTY),
        price=None if draw(empty) else draw(PRICE),
        tax_rate=None if draw(empty) else draw(RATE),
    )


@st.composite
def bills(draw: st.DrawFn) -> Bill:
    """Draw a bill with pricing: its lines and how their price reads (requirement 6.5)."""
    empty = st.sampled_from(draw(EMPTY_SHARES))
    count = draw(LINE_COUNTS)
    lines = draw(st.lists(line_inputs(empty), min_size=count, max_size=count))
    return Bill(lines=tuple(lines), price_includes_tax=draw(st.booleans()))


def line(qty: str | None, price: str | None = None, tax_rate: str | None = None) -> LineInput:
    """One line whose values are written as text, None where the value is empty."""
    return LineInput(qty=_value(qty), price=_value(price), tax_rate=_value(tax_rate))


def _value(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 3: 计价参考模型与表头汇总
@pytest.mark.req("6.1", "6.2", "6.4", "6.5", "6.10")
@settings(max_examples=500)
@given(bill=bills())
# The example of requirement 6.1: 1 × 0.125 is half a cent and rounds up to 0.13.
@example(bill=Bill(lines=(line("1", "0.125", "0.13"),), price_includes_tax=False))
# The example of requirement 6.2: 1700.00 over 1.13 is 1504.4247..., and the tax is the
# rest of the total.
@example(bill=Bill(lines=(line("2000", "0.85", "0.13"),), price_includes_tax=True))
# Both ends of every range of requirement 6.5 in one bill.
@example(
    bill=Bill(
        lines=(line("0.0001", "0", "0"), line("100000000", "100000000", "1")),
        price_includes_tax=True,
    )
)
# An empty price and an empty tax rate leave their lines out of the header sums.
@example(
    bill=Bill(
        lines=(line("1", None, "0.13"), line("1", "0.85"), line("1", "0.85", "0.13")),
        price_includes_tax=False,
    )
)
# No line has a single amount, so the three header amounts are 0 (requirement 6.4).
@example(bill=Bill(lines=(line("2000"), line("3", None, None)), price_includes_tax=True))
def test_the_amounts_are_the_ones_the_reference_model_gives(bill: Bill) -> None:
    result = price_bill(bill.lines, price_includes_tax=bill.price_includes_tax)

    expected = [
        ref_line(values, price_includes_tax=bill.price_includes_tax) for values in bill.lines
    ]
    for number, (found, reference) in enumerate(zip(result.lines, expected, strict=True), start=1):
        assert exact(found.values()) == reference, f"line {number} of {bill}"
        assert adds_up(found), f"line {number} of {bill}"
    assert exact(result.values()) == ref_header(expected)
