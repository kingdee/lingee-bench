"""erp_mock.domain.pricing: the amounts of every line and of the header (requirement 6).

The primitive is pure arithmetic, so the tests call it with the field values themselves,
without a sandbox. What they pin:

- the two readings of a price, each with the example of its acceptance criterion: a net
  price (requirement 6.1) and one that includes tax (requirement 6.2). The row
  ``qty 2000, price 0.85, tax_rate 0.13`` appears in both tables: the same inputs give a
  different split, which is what an update of ``price_includes_tax`` alone recomputes;
- an empty qty, price or tax rate leaves the three amounts of that line empty, whichever
  way the price reads (requirement 6.10);
- the header is the sum of the non-empty line values, and zero when there is none - a bill
  without lines and a bill whose lines are all incomplete (requirement 6.4);
- every amount comes back at money precision and never as -0 (requirement 4.7);
- the default of ``price_includes_tax``: what the pack declared, true when it declared
  nothing (requirement 6.3), read off the two pricing bills of the engine test fixture.
"""

from decimal import Decimal
from typing import Final

import pytest

from erp_mock.config.model import DomainModel
from erp_mock.domain.pricing import (
    AMOUNT_FIELDS,
    NO_AMOUNTS,
    BillAmounts,
    LineAmounts,
    LineInput,
    default_price_includes_tax,
    line_amounts,
    price_bill,
)
from tests.helpers.fixtures import fixture_domain

# A line whose three amounts are there, used where the inputs themselves do not matter.
COMPLETE: Final = LineInput(qty=Decimal("2000"), price=Decimal("0.85"), tax_rate=Decimal("0.13"))


@pytest.fixture(scope="module")
def domain() -> DomainModel:
    return fixture_domain()


def amounts(
    qty: str | None, price: str | None, tax_rate: str | None, *, includes_tax: bool
) -> tuple[str | None, ...]:
    """The three amounts of one line as plain text, None where the amount is empty.

    The text comes from ``str``, not from the canonical formatter, so it shows the decimals
    and the sign the primitive produced: a money value is "0.00" and never "-0.00" or "0".
    """
    line = LineInput(
        qty=_value(qty),
        price=_value(price),
        tax_rate=_value(tax_rate),
    )
    return _texts(line_amounts(line, price_includes_tax=includes_tax))


def _value(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _texts(result: LineAmounts) -> tuple[str | None, ...]:
    values = result.values()
    assert tuple(values) == AMOUNT_FIELDS
    return tuple(None if value is None else str(value) for value in values.values())


def _header_texts(result: BillAmounts) -> tuple[str, ...]:
    values = result.values()
    assert tuple(values) == AMOUNT_FIELDS
    return tuple(str(value) for value in values.values())


# --- a net price (requirement 6.1) -------------------------------------------------------


@pytest.mark.req("6.1")
@pytest.mark.parametrize(
    ("qty", "price", "tax_rate", "expected"),
    [
        # The example of requirement 6.1: 1 × 0.125 is a half and rounds up to 0.13, and
        # 0.13 × 0.13 = 0.0169 rounds to 0.02.
        pytest.param("1", "0.125", "0.13", ("0.13", "0.02", "0.15"), id="criterion-example"),
        # 0.10 × 0.05 = 0.0050, exactly a half, and rounds away from zero.
        pytest.param("1", "0.1", "0.05", ("0.10", "0.01", "0.11"), id="tax-on-a-half"),
        pytest.param("2000", "0.85", "0.13", ("1700.00", "221.00", "1921.00"), id="net-of-1700"),
        # A rate of zero leaves the net amount as the total.
        pytest.param("3", "1.005", "0", ("3.02", "0.00", "3.02"), id="rate-zero"),
        # The product rounds to zero, and a zero amount is never -0.
        pytest.param("-0.0001", "0.000001", "0.13", ("0.00", "0.00", "0.00"), id="rounds-to-zero"),
    ],
)
def test_amounts_when_the_price_is_net(
    qty: str, price: str, tax_rate: str, expected: tuple[str, ...]
) -> None:
    assert amounts(qty, price, tax_rate, includes_tax=False) == expected


# --- a price that includes tax (requirement 6.2) -----------------------------------------


@pytest.mark.req("6.2")
@pytest.mark.parametrize(
    ("qty", "price", "tax_rate", "expected"),
    [
        # The example of requirement 6.2: 2000 × 0.85 = 1700.00 and 1700 / 1.13 is
        # 1504.4247..., so the tax is the rest of the total.
        pytest.param(
            "2000", "0.85", "0.13", ("1504.42", "195.58", "1700.00"), id="criterion-example"
        ),
        # 0.04 / 1.6 = 0.025, exactly a half of the last kept place, and rounds up.
        pytest.param("0.08", "0.5", "0.6", ("0.03", "0.01", "0.04"), id="quotient-on-a-half"),
        pytest.param("1", "0.125", "0.13", ("0.12", "0.01", "0.13"), id="total-of-0.13"),
        pytest.param("3", "1.005", "0", ("3.02", "0.00", "3.02"), id="rate-zero"),
        pytest.param("-0.0001", "0.000001", "0.13", ("0.00", "0.00", "0.00"), id="rounds-to-zero"),
    ],
)
def test_amounts_when_the_price_includes_tax(
    qty: str, price: str, tax_rate: str, expected: tuple[str, ...]
) -> None:
    assert amounts(qty, price, tax_rate, includes_tax=True) == expected


@pytest.mark.req("6.2")
def test_the_quotient_is_rounded_once() -> None:
    """The division runs over the exact rational, with no rounding before the last one.

    0.31 / 1.17 = 0.2649572..., which is 0.26 at two places. Rounding the quotient to the
    four places of a rate first would give 0.2650, a half, and from there 0.27 - a cent of
    difference that would also land in the tax, the rest of the total.
    """
    line = LineInput(qty=Decimal("1"), price=Decimal("0.31"), tax_rate=Decimal("0.17"))

    assert _texts(line_amounts(line, price_includes_tax=True)) == ("0.26", "0.05", "0.31")


# --- an empty input (requirement 6.10) ---------------------------------------------------


@pytest.mark.req("6.10")
@pytest.mark.parametrize("includes_tax", [False, True], ids=["net", "with-tax"])
@pytest.mark.parametrize(
    ("qty", "price", "tax_rate"),
    [
        pytest.param(None, "0.85", "0.13", id="no-qty"),
        pytest.param("2000", None, "0.13", id="no-price"),
        pytest.param("2000", "0.85", None, id="no-tax-rate"),
        pytest.param(None, None, None, id="none-of-the-three"),
    ],
)
def test_an_empty_input_leaves_the_three_amounts_empty(
    qty: str | None, price: str | None, tax_rate: str | None, includes_tax: bool
) -> None:
    assert amounts(qty, price, tax_rate, includes_tax=includes_tax) == (None, None, None)


@pytest.mark.req("6.10")
def test_an_incomplete_line_is_the_empty_amounts() -> None:
    assert line_amounts(LineInput(), price_includes_tax=True) is NO_AMOUNTS
    assert _texts(NO_AMOUNTS) == (None, None, None)


# --- the header (requirement 6.4) --------------------------------------------------------


@pytest.mark.req("6.4")
def test_the_header_sums_the_non_empty_line_values() -> None:
    lines = (
        COMPLETE,
        LineInput(qty=Decimal("1"), price=Decimal("0.125"), tax_rate=Decimal("0.13")),
        LineInput(qty=Decimal("3"), price=None, tax_rate=Decimal("0.13")),
    )

    result = price_bill(lines, price_includes_tax=False)

    # Every line keeps its place, the one without a price among them (requirement 6.10).
    assert _texts(result.lines[0]) == ("1700.00", "221.00", "1921.00")
    assert _texts(result.lines[1]) == ("0.13", "0.02", "0.15")
    assert result.lines[2] is NO_AMOUNTS
    assert _header_texts(result) == ("1700.13", "221.02", "1921.15")


@pytest.mark.req("6.4")
def test_the_header_of_a_bill_without_a_single_amount_is_zero() -> None:
    assert _header_texts(price_bill((), price_includes_tax=True)) == ("0.00", "0.00", "0.00")
    assert price_bill((), price_includes_tax=True).lines == ()

    # Lines that are all incomplete leave the header at zero as well (requirement 6.10).
    incomplete = price_bill((LineInput(), LineInput(qty=Decimal("1"))), price_includes_tax=False)
    assert _header_texts(incomplete) == ("0.00", "0.00", "0.00")
    assert incomplete.lines == (NO_AMOUNTS, NO_AMOUNTS)


@pytest.mark.req("6.4")
def test_a_header_sum_keeps_money_precision_and_no_negative_zero() -> None:
    result = price_bill([COMPLETE], price_includes_tax=True)

    for value in (*result.values().values(), *(result.lines[0].values().values())):
        assert isinstance(value, Decimal)
        assert value.as_tuple().exponent == -2
    # +0, so the canonical text of a zero total is "0.00" and never "-0.00".
    negative_zero = LineInput(qty=Decimal("-1"), price=Decimal("0"), tax_rate=Decimal("0.13"))
    zero = price_bill([negative_zero], price_includes_tax=True)
    assert zero.amount.as_tuple() == Decimal("0.00").as_tuple()


# --- the default of price_includes_tax (requirement 6.3) ---------------------------------


@pytest.mark.req("6.3")
def test_the_default_of_price_includes_tax_is_what_the_pack_declared(domain: DomainModel) -> None:
    # supply_order declares `price_includes_tax: { default: false }` by same-name
    # declaration (requirement 1.5).
    assert default_price_includes_tax(domain.bills["supply_order"]) is False


@pytest.mark.req("6.3")
@pytest.mark.parametrize("bill", ["fee_note", "pay_slip", "goods_entry"])
def test_without_a_declared_default_the_price_includes_tax(domain: DomainModel, bill: str) -> None:
    # The two pricing bills that declare no default, and a bill without pricing, which has
    # no such field at all.
    assert default_price_includes_tax(domain.bills[bill]) is True
