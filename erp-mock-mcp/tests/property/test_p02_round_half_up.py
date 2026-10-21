"""Property 2: ROUND_HALF_UP rounding.

round_half_up and round_fraction are compared with reference_round, an independent
reference in exact rational arithmetic: the kept part moves one unit away from zero when
the discarded part is at least half a unit in the last kept place (requirement 4.7).
Every result also has exponent -places and is +0, never -0, when it rounds to zero.

The other half of the property is the rounding of a qty, price, money or rate argument in
argument normalization (requirement 12.6): the last section sends values with more decimals
than their field precision through :func:`~erp_mock.tools.validate.validate_args`, as JSON
numbers and as decimal strings, and checks each normalized argument against the same
reference.
"""

import math
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction
from typing import Final

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from erp_mock.common.decimals import PLACES, DecimalKind, round_fraction, round_half_up
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import DomainModel, FieldRef, QueryModel, QueryParamModel
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.input_schema import query_input
from erp_mock.tools.validate import validate_args

# Decimal places of money (2), qty and rate (4), and price (6), requirement 4.7.
FIELD_PLACES = (2, 4, 6)

# The field precisions, plus 0 and larger place counts.
PLACE_COUNTS = st.one_of(st.sampled_from(FIELD_PLACES), st.integers(min_value=0, max_value=24))

# --- reference ---------------------------------------------------------------------------


def reference_round(value: Fraction, places: int) -> Fraction:
    """Round ``value`` to ``places`` decimal places, ROUND_HALF_UP, in exact arithmetic.

    floor(|value| * 10**places + 1/2) is one more than the kept units exactly when the
    discarded part is at least half a unit; reapplying the sign of ``value`` afterwards
    moves halves away from zero.
    """
    magnitude = math.floor(abs(value) * 10**places + Fraction(1, 2))
    return Fraction(-magnitude if value < 0 else magnitude, 10**places)


def assert_rounded(result: Decimal, value: Fraction, places: int) -> None:
    """Assert that ``result`` is ``value`` rounded to ``places`` decimal places.

    Value, exponent and sign bit together fix ``result.as_tuple()``. The sign bit may be
    set only on a negative result, so a result of zero must be +0.
    """
    expected = reference_round(value, places)
    sign, _, exponent = result.as_tuple()
    assert Fraction(result) == expected
    assert exponent == -places
    assert sign == (1 if expected < 0 else 0)


# --- generators --------------------------------------------------------------------------


@st.composite
def roundings(draw: st.DrawFn) -> tuple[Decimal, int]:
    """Draw a finite Decimal and a place count to round it to.

    Half the values combine any sign, a coefficient of up to 30 digits and an exponent
    from -45 to 15: they include 0 and -0, values with no digits to discard and values
    far below one unit. The other half follow ``places`` kept decimals with 1 to 12
    discarded digits that are exactly half a unit (x.xx5 at 2 places) or differ from it
    by one in the last digit (x.xx49, x.xx51); the kept part is sometimes all nines, so
    that rounding up carries.

    Every result has at most 69 significant digits; round_half_up raises
    decimal.InvalidOperation above the 80 of its WIDE context.
    """
    places = draw(PLACE_COUNTS)
    sign = draw(st.sampled_from(["", "-"]))
    if draw(st.booleans()):
        coefficient = draw(st.integers(min_value=0, max_value=10**30 - 1))
        exponent = draw(st.integers(min_value=-45, max_value=15))
    else:
        extra = draw(st.integers(min_value=1, max_value=12))
        kept = draw(
            st.one_of(
                st.integers(min_value=0, max_value=10**18),
                st.integers(min_value=0, max_value=18).map(lambda digits: 10**digits - 1),
            )
        )
        half = 5 * 10 ** (extra - 1)  # half a unit in the last kept place
        discarded = half + draw(st.sampled_from([0, -1, 1]))
        coefficient = kept * 10**extra + discarded
        exponent = -(places + extra)
    return Decimal(f"{sign}{coefficient}E{exponent}"), places


@st.composite
def quotients(draw: st.DrawFn) -> tuple[int, int, int]:
    """Draw ``num``, a positive ``den`` and a place count for round_fraction.

    Half the draws take any numerator up to 10**40 in magnitude over any denominator up
    to 10**20, like the gross amount over 1 + tax_rate (requirement 6.2). The other half
    write the value of a ``roundings`` Decimal, which may lie exactly at half a unit, as
    a fraction whose numerator and denominator share a common factor.
    """
    if draw(st.booleans()):
        num = draw(st.integers(min_value=-(10**40), max_value=10**40))
        den = draw(st.integers(min_value=1, max_value=10**20))
        return num, den, draw(PLACE_COUNTS)
    x, places = draw(roundings())
    num, den = x.as_integer_ratio()
    factor = draw(st.integers(min_value=1, max_value=10**6))
    return num * factor, den * factor, places


# --- round_half_up and round_fraction (requirement 4.7) ----------------------------------


# Feature: erp-mock-mcp-m0, Property 2: ROUND_HALF_UP 舍入
@pytest.mark.req("4.7")
@settings(max_examples=100)
@given(case=roundings())
@example(case=(Decimal("1.005"), 2))  # half a unit rounds up: 1.01
@example(case=(Decimal("1.0049"), 2))  # below half: 1.00
@example(case=(Decimal("-1.005"), 2))  # a half moves away from zero: -1.01
@example(case=(Decimal("-0.00005"), 4))  # -0.0001
@example(case=(Decimal("9.995"), 2))  # the carry adds a digit: 10.00
@example(case=(Decimal("0.1234565"), 6))  # 0.123457
@example(case=(Decimal("-0.004"), 2))  # rounds to +0.00, not -0.00
@example(case=(Decimal("-0"), 4))  # +0.0000
@example(case=(Decimal("2.5"), 0))  # 3
@example(case=(Decimal("2E+3"), 6))  # nothing to discard: 2000.000000
def test_round_half_up_matches_the_reference(case: tuple[Decimal, int]) -> None:
    x, places = case
    assert_rounded(round_half_up(x, places), Fraction(x), places)


# Feature: erp-mock-mcp-m0, Property 2: ROUND_HALF_UP 舍入
@pytest.mark.req("4.7")
@settings(max_examples=100)
@given(case=quotients())
@example(case=(1, 8, 2))  # 0.125, exactly half: 0.13
@example(case=(-1, 8, 2))  # -0.13
@example(case=(-1, 1000, 2))  # -0.001 rounds to +0.00
@example(case=(170000, 113, 2))  # 1700.00 / 1.13: 1504.42
@example(case=(0, 7, 4))  # 0.0000
def test_round_fraction_matches_the_reference(case: tuple[int, int, int]) -> None:
    num, den, places = case
    assert_rounded(round_fraction(num, den, places), Fraction(num, den), places)


# --- argument normalization (requirement 12.6) -------------------------------------------

# The parameter each kind is read through. A query tool is the lightest tool that has one
# parameter per field type: its parameters carry the type the exposure declares and nothing
# else (requirement 5.13), so the validator reads the precision of each off the model
# itself (``_levels`` of erp_mock.tools.validate).
NUMBER_TOOL: Final = "fx_round_query"
NUMBER_PARAMS: Final[Mapping[DecimalKind, str]] = {kind: f"v_{kind}" for kind in PLACES}

# A JSON number of at most 15 significant digits denotes exactly the decimal it is written
# as (requirement 12.4), so the values drawn stay within that and the reference is the same
# value whichever of the two ways it was sent.
MAX_DIGITS: Final = 15


def number_tool() -> tuple[DomainModel, ToolSpec]:
    """A tool with one optional parameter per decimal kind, and the model behind it."""
    params = FrozenMap(
        {
            name: QueryParamModel(name=name, type=kind, maps_to=FieldRef("record", name))
            for kind, name in NUMBER_PARAMS.items()
        }
    )
    query = QueryModel(tool=NUMBER_TOOL, pack="fx_trade", file="tools/exposure.yaml", params=params)
    built = query_input(query)
    spec = ToolSpec(
        name=NUMBER_TOOL,
        generated_name=NUMBER_TOOL,
        pack=query.pack,
        kind="query",
        action="query",
        target=NUMBER_TOOL,
        input_schema=built.schema,
        param_paths=built.param_paths,
    )
    return DomainModel(queries=FrozenMap({NUMBER_TOOL: query})), spec


@st.composite
def arguments(draw: st.DrawFn) -> tuple[DecimalKind, Decimal, bool]:
    """Draw a kind, a value for a parameter of that kind, and how the value is sent.

    The value is not negative, which every qty, price, money and rate parameter requires
    (requirement 11.11). Some values have exactly the decimals of the field, so nothing is
    discarded; the others carry 1 to 8 digits past it that are exactly half a unit in the
    last kept place or differ from it by one in the last digit, so the boundary of
    ROUND_HALF_UP is drawn as often as the middle of a step. ``True`` as the third element
    sends the value as a decimal string, which a number parameter accepts as well
    (requirement 12.5), and False as a JSON number.
    """
    kind = draw(st.sampled_from(sorted(PLACES)))
    places = PLACES[kind]
    extra = draw(st.integers(min_value=0, max_value=8))
    kept = draw(st.integers(min_value=0, max_value=10 ** (MAX_DIGITS - extra) - 1))
    coefficient = kept
    if extra > 0:
        half = 5 * 10 ** (extra - 1)  # half a unit in the last kept place
        discarded = draw(
            st.one_of(
                st.sampled_from([half, half - 1, half + 1]),
                st.integers(min_value=0, max_value=10**extra - 1),
            )
        )
        coefficient = kept * 10**extra + discarded
    return kind, Decimal(f"{coefficient}E{-(places + extra)}"), draw(st.booleans())


# Feature: erp-mock-mcp-m0, Property 2: ROUND_HALF_UP 舍入
@pytest.mark.req("12.6")
@settings(max_examples=100)
@given(case=arguments())
@example(case=("price", Decimal("0.1234565"), False))  # 0.123457 (requirement 12.6)
@example(case=("price", Decimal("0.1234565"), True))  # the decimal string of the same
@example(case=("money", Decimal("1.005"), True))  # 1.01
@example(case=("money", Decimal("0.004"), False))  # rounds to +0.00
@example(case=("qty", Decimal("20.00005"), True))  # 20.0001
@example(case=("rate", Decimal("0.13005"), False))  # 0.1301
@example(case=("qty", Decimal("20"), False))  # nothing to discard: 20.0000
def test_argument_normalization_rounds_to_the_field_precision(
    case: tuple[DecimalKind, Decimal, bool],
) -> None:
    kind, value, as_text = case
    param = NUMBER_PARAMS[kind]
    model, spec = number_tool()
    sent: str | float = format(value, "f") if as_text else float(value)
    # The JSON number denotes the value drawn, so both ways send the same number (12.4).
    assert Decimal(sent if isinstance(sent, str) else repr(sent)) == value

    args = validate_args(model, spec, {param: sent})

    assert args.error is None, f"unexpected error: {args.error}"
    found = args.values[param]
    assert isinstance(found, Decimal)
    assert_rounded(found, Fraction(value), PLACES[kind])
