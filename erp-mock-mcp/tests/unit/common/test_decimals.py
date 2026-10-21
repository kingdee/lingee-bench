"""erp_mock.common.decimals: rounding to field precision, canonical text, number input."""

import decimal
import math
from decimal import Decimal
from fractions import Fraction

import pytest

from erp_mock.common.decimals import (
    PLACES,
    WIDE,
    DecimalKind,
    format_decimal,
    round_fraction,
    round_half_up,
    to_decimal,
)

# --- rounding (requirement 4.7) ----------------------------------------------------------


@pytest.mark.req("4.7")
def test_field_precisions() -> None:
    assert dict(PLACES) == {"qty": 4, "price": 6, "money": 2, "rate": 4}


@pytest.mark.req("4.7")
@pytest.mark.parametrize(
    ("value", "places", "expected"),
    [
        ("1.005", 2, "1.01"),
        ("1.0049", 2, "1.00"),
        ("-1.005", 2, "-1.01"),  # the half moves away from zero
        ("-1.0049", 2, "-1.00"),
        ("0.00005", 4, "0.0001"),
        ("0.000049999999", 4, "0.0000"),
        ("0.1234565", 6, "0.123457"),
        ("2000", 2, "2000.00"),  # the exponent is always -places
        ("1E+3", 4, "1000.0000"),
        ("-0.001", 2, "0.00"),  # rounds to +0, not -0
        ("-0", 4, "0.0000"),
    ],
)
def test_round_half_up(value: str, places: int, expected: str) -> None:
    # as_tuple compares sign, coefficient and exponent, so -0 and 0 differ.
    assert round_half_up(Decimal(value), places).as_tuple() == Decimal(expected).as_tuple()


@pytest.mark.req("4.7")
def test_round_half_up_rejects_what_it_cannot_round() -> None:
    for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            round_half_up(Decimal(value), 2)
    with pytest.raises(ValueError):
        round_half_up(Decimal("1.5"), -1)
    # 1E+78 at 2 places needs 81 significant digits, one more than WIDE has.
    with pytest.raises(decimal.InvalidOperation):
        round_half_up(Decimal("1E+78"), 2)
    assert round_half_up(Decimal("1E+77"), 2) == Decimal("1E+77")


@pytest.mark.req("4.7")
def test_wide_multiplies_values_of_30_significant_digits_exactly() -> None:
    a = Decimal("12345678901234567890.1234567891")
    b = Decimal("-98765432109876543210.9876543219")
    assert Fraction(WIDE.multiply(a, b)) == Fraction(a) * Fraction(b)


@pytest.mark.req("4.7")
@pytest.mark.parametrize(
    ("num", "den", "places", "expected"),
    [
        (1, 8, 2, "0.13"),  # 0.125, a half
        (-1, 8, 2, "-0.13"),
        (170000, 113, 2, "1504.42"),  # 1700.00 / 1.13 = 1504.4247...
        (2, 1, 2, "2.00"),
        (0, 7, 4, "0.0000"),
        (-1, 1000, 2, "0.00"),  # rounds to +0, not -0
    ],
)
def test_round_fraction(num: int, den: int, places: int, expected: str) -> None:
    assert round_fraction(num, den, places).as_tuple() == Decimal(expected).as_tuple()


@pytest.mark.req("4.7")
def test_round_fraction_rounds_the_exact_quotient_once() -> None:
    # 0.005 - 10**-33 is below the half and rounds down. Dividing with 28 significant
    # digits first gives 0.005000... and then rounds up.
    num, den = 5 * 10**30 - 1, 10**33
    assert round_fraction(num, den, 2) == Decimal("0.00")
    two_step = round_half_up(decimal.Context(prec=28).divide(Decimal(num), Decimal(den)), 2)
    assert two_step == Decimal("0.01")


@pytest.mark.req("4.7")
@pytest.mark.parametrize(("den", "places"), [(0, 2), (-3, 2), (3, -1)])
def test_round_fraction_rejects_bad_arguments(den: int, places: int) -> None:
    with pytest.raises(ValueError):
        round_fraction(1, den, places)


# --- canonical text (requirement 4.8) ----------------------------------------------------


@pytest.mark.req("4.8")
@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("money", "1700", "1700.00"),
        ("money", "1700.00", "1700.00"),
        ("money", "0", "0.00"),
        ("money", "-12.5", "-12.50"),
        ("money", "-.5", "-0.50"),
        ("qty", "2000", "2000"),
        ("qty", "2000.0000", "2000"),
        ("qty", "2E+3", "2000"),  # never exponent notation
        ("qty", "-12.5", "-12.5"),
        ("qty", "-12.5000", "-12.5"),
        ("qty", ".5", "0.5"),
        ("qty", "00012.30", "12.3"),
        ("price", "0.85", "0.85"),
        ("price", "0.000001", "0.000001"),
        ("price", "1E-6", "0.000001"),
        ("price", "123456789012.345678", "123456789012.345678"),
        ("rate", "0.13", "0.13"),
        ("rate", "0", "0"),
        ("rate", "10", "10"),  # zeros of the integer part stay
    ],
)
def test_format_decimal(kind: DecimalKind, value: str, expected: str) -> None:
    assert format_decimal(kind, Decimal(value)) == expected


@pytest.mark.req("4.8")
@pytest.mark.parametrize("value", ["-0", "-0.000000", "-0E+3", "-0.0000001"])
@pytest.mark.parametrize(
    ("kind", "zero"), [("qty", "0"), ("price", "0"), ("money", "0.00"), ("rate", "0")]
)
def test_zero_has_no_sign(kind: DecimalKind, zero: str, value: str) -> None:
    assert format_decimal(kind, Decimal(value)) == zero


@pytest.mark.req("4.8")
@pytest.mark.parametrize("kind", ["qty", "price", "money", "rate"])
def test_equal_values_have_the_same_text(kind: DecimalKind) -> None:
    assert format_decimal(kind, Decimal("0.850000")) == "0.85"
    assert format_decimal(kind, Decimal("0.85")) == "0.85"


@pytest.mark.req("4.7", "4.8")
@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("money", "1.005", "1.01"),
        ("money", "1.0049", "1.00"),
        ("qty", "0.00005", "0.0001"),
        ("price", "0.1234565", "0.123457"),
        ("rate", "0.12995", "0.13"),
    ],
)
def test_format_decimal_rounds_to_field_precision(
    kind: DecimalKind, value: str, expected: str
) -> None:
    assert format_decimal(kind, Decimal(value)) == expected


@pytest.mark.req("4.8")
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_format_decimal_rejects_non_finite_values(value: str) -> None:
    with pytest.raises(ValueError):
        format_decimal("money", Decimal(value))


# --- number input (requirement 4.7: no binary floating point) ----------------------------


@pytest.mark.req("4.7")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2000, "2000"),
        (-5, "-5"),
        (10**40 + 1, "1" + "0" * 39 + "1"),  # more digits than any context, still exact
        (0.85, "0.85"),
        (0.1, "0.1"),
        (1e-7, "1E-7"),
        (123456789.123456, "123456789.123456"),
        (2.0, "2.0"),
        ("1700.00", "1700.00"),
        ("-12.5", "-12.5"),
        ("0.000001", "0.000001"),
        ("007", "7"),
        ("-0", "-0"),
    ],
)
def test_to_decimal(value: int | float | str, expected: str) -> None:
    assert to_decimal(value).as_tuple() == Decimal(expected).as_tuple()


@pytest.mark.req("4.7")
@pytest.mark.parametrize(
    "value",
    [
        "1e3",
        " 5",
        "5 ",
        "5\n",
        "1,000",
        "1_000",
        "+5",
        ".5",
        "5.",
        "",
        "-",
        "--5",
        "1.2.3",
        "NaN",
        "Infinity",
        "\uff15",  # full-width digit five
        "\u0665",  # Arabic-Indic digit five
    ],
)
def test_to_decimal_rejects_other_strings(value: str) -> None:
    with pytest.raises(ValueError):
        to_decimal(value)


@pytest.mark.req("4.7")
@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_to_decimal_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(ValueError):
        to_decimal(value)


@pytest.mark.req("4.7")
@pytest.mark.parametrize("value", [True, False, None, Decimal("1"), b"1"])
def test_to_decimal_rejects_bools_and_other_types(value: object) -> None:
    with pytest.raises(TypeError):
        to_decimal(value)  # type: ignore[arg-type]


# --- determinism -------------------------------------------------------------------------


@pytest.mark.req("4.7", "4.8")
def test_results_do_not_depend_on_the_thread_context() -> None:
    with decimal.localcontext(prec=3, rounding=decimal.ROUND_DOWN, Emin=-10, Emax=10):
        assert round_half_up(Decimal("1.005"), 2) == Decimal("1.01")
        assert round_fraction(170000, 113, 2) == Decimal("1504.42")
        assert format_decimal("price", Decimal("123456789.123456")) == "123456789.123456"
        assert to_decimal("12345.6789") == Decimal("12345.6789")
