"""Property 1: canonical text of Decimal_Formatter and its round trip (requirements 4.8, 4.9).

For every field type T and every finite Decimal x whose value has at most PLACES[T]
decimal places, ``format_decimal(T, x)`` is the canonical text of requirement 4.8, reads
back as x, and is the same for every Decimal equal to x however it is written ("0.85" and
"0.850000", "2E+3" and "2000.0000", "-0" and "0E+3").

Values are drawn up to the largest magnitude format_decimal can round: a value rounded to
PLACES[T] decimals has at most WIDE.prec (80) significant digits, so it is below 10**78
for money and 10**74 for price. Beyond that round_half_up raises decimal.InvalidOperation
by design (tests/unit/common/test_decimals.py).
"""

import re
from decimal import Decimal
from typing import Final, NamedTuple

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from erp_mock.common.decimals import PLACES, WIDE, DecimalKind, format_decimal

KINDS: Final[tuple[DecimalKind, ...]] = ("qty", "price", "money", "rate")


def _canonical_pattern(kind: DecimalKind) -> re.Pattern[str]:
    """Return the requirement 4.8 form of a ``kind`` text, to be used with fullmatch.

    ASCII digits only, an optional leading "-" and an integer part without extra leading
    zeros, so no exponent, plus sign, grouping or whitespace. money has exactly
    PLACES["money"] decimals; qty, price and rate have either no decimal point or at most
    PLACES[kind] decimals ending in a nonzero digit.
    """
    places = PLACES[kind]
    integer_part = "-?(?:0|[1-9][0-9]*)"
    if kind == "money":
        return re.compile(rf"{integer_part}\.[0-9]{{{places}}}")
    return re.compile(rf"{integer_part}(?:\.[0-9]{{0,{places - 1}}}[1-9])?")


CANONICAL: Final[dict[DecimalKind, re.Pattern[str]]] = {
    kind: _canonical_pattern(kind) for kind in KINDS
}

# Exponents of a written zero, on both sides of every field precision ("0E-40" to "0E+40").
ZERO_EXPONENTS: Final = st.integers(min_value=-40, max_value=40)
# Trailing zeros a written value carries beyond its shortest form ("0.850000" for 0.85).
EXTRA_ZEROS: Final = st.integers(min_value=0, max_value=30)


class EqualValues(NamedTuple):
    """A field type and two equal Decimals within its precision, possibly written differently."""

    kind: DecimalKind
    x: Decimal
    y: Decimal


def _decimal(negative: bool, coefficient: int, exponent: int) -> Decimal:
    """Return the Decimal with exactly this sign, coefficient and exponent ("-85E-2")."""
    return Decimal(f"{'-' if negative else ''}{coefficient}E{exponent}")


@st.composite
def equal_values(draw: st.DrawFn) -> EqualValues:
    """Draw a field type and two equal finite Decimals within its precision.

    One draw in eight is zero, written with an independent sign and exponent each ("0",
    "-0", "0.000000", "-0E+3"). Otherwise the value is c * 10**e in lowest terms (c is not a
    multiple of 10): e >= -places keeps it within the field precision, and the bound on c
    keeps it rounded to ``places`` decimals within WIDE.prec significant digits. Each
    Decimal writes it as (c * 10**k) * 10**(e - k) with its own k >= 0, which covers forms
    such as "2E+3", "2000" and "2000.0000".
    """
    kind = draw(st.sampled_from(KINDS))
    places = PLACES[kind]
    if draw(st.integers(min_value=0, max_value=7)) == 0:
        x = _decimal(draw(st.booleans()), 0, draw(ZERO_EXPONENTS))
        y = _decimal(draw(st.booleans()), 0, draw(ZERO_EXPONENTS))
        return EqualValues(kind, x, y)
    # A third of the exponents give fractions (0.000001, 0.85, -12.5), a third integers
    # near the decimal point (2E+3) and a third any magnitude up to the largest values.
    exponent = draw(
        st.one_of(
            st.integers(min_value=-places, max_value=-1),
            st.integers(min_value=0, max_value=3),
            st.integers(min_value=-places, max_value=WIDE.prec - places - 1),
        )
    )
    coefficient = draw(
        st.integers(min_value=1, max_value=10 ** (WIDE.prec - places - exponent) - 1)
    )
    while coefficient % 10 == 0:
        coefficient //= 10
        exponent += 1
    negative = draw(st.booleans())
    k_x = draw(EXTRA_ZEROS)
    k_y = draw(EXTRA_ZEROS)
    return EqualValues(
        kind,
        _decimal(negative, coefficient * 10**k_x, exponent - k_x),
        _decimal(negative, coefficient * 10**k_y, exponent - k_y),
    )


# The largest money magnitude: 80 significant digits once rounded to 2 decimals.
_LARGEST_MONEY: Final = "9" * (WIDE.prec - PLACES["money"]) + ".99"


# Feature: erp-mock-mcp-m0, Property 1: Decimal_Formatter 的规范形式与往返
@pytest.mark.req("4.8", "4.9")
@settings(max_examples=100)
@given(case=equal_values())
@example(case=EqualValues("price", Decimal("0.85"), Decimal("0.850000")))
@example(case=EqualValues("qty", Decimal("2E+3"), Decimal("2000.0000")))
@example(case=EqualValues("money", Decimal("-0"), Decimal("0E+3")))
@example(case=EqualValues("price", Decimal("-0.000001"), Decimal("-1.0E-6")))
@example(case=EqualValues("money", Decimal(f"-{_LARGEST_MONEY}"), Decimal(f"-{_LARGEST_MONEY}0")))
def test_format_decimal_is_canonical_and_round_trips(case: EqualValues) -> None:
    kind, x, y = case
    text = format_decimal(kind, x)
    assert CANONICAL[kind].fullmatch(text), f"not a canonical {kind} text: {text!r}"
    # A negative value starts with "-"; zero, -0 included, does not.
    assert text.startswith("-") == (x < 0)
    assert Decimal(text) == x
    assert format_decimal(kind, y) == text
