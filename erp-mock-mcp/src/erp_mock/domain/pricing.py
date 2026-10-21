"""The pricing primitive: the three money fields of every line and of the header (req 6).

A write to a bill with ``pricing`` recomputes the amounts from the line's ``qty``, ``price``
and ``tax_rate`` and the header's ``price_includes_tax``; all six fields are read-only, so
no caller ever writes them (requirement 6.4). What this module settles, and nothing else:
the computation is pure, takes the values of one candidate bill and returns the values to
store, and reads neither the sandbox nor the operation context.

How the flag reads the price (``round(x, n)`` is ROUND_HALF_UP on the exact value of ``x``,
with no rounding before it):

- false, the price is net (requirement 6.1): ``amount = round(qty × price, 2)``,
  ``tax_amount = round(amount × tax_rate, 2)``, ``amount_with_tax = amount + tax_amount``;
- true, the price includes tax (requirement 6.2): ``amount_with_tax = round(qty × price, 2)``,
  ``amount = round(amount_with_tax ÷ (1 + tax_rate), 2)``,
  ``tax_amount = amount_with_tax - amount``.

Both start from the same product, so a line gets a different split of the same total when
only ``price_includes_tax`` changes (requirement 5.2). The division is the one inexact
step: it runs over the exact rational ``amount_with_tax ÷ (1 + tax_rate)`` through
:func:`~erp_mock.common.decimals.round_fraction`, which rounds the quotient once. With
``(a, b)`` the integer ratio of ``amount_with_tax`` and ``(c, d)`` that of ``tax_rate``,
that quotient is ``(a × d) / (b × (d + c))``.

Difference from design.md 4.2, which writes the same step as
``round_fraction(A × 10^4, 10^4 + r, 2)`` with ``A`` the total in cents and ``r`` the tax
rate times 10^4: that numerator is 100 times too large (1700.00 at 13% gives 150442.48
instead of 1504.42). With ``A`` in cents, requirement 6.2 and its example give
``round_fraction(A × 100, 10^4 + r, 2)``, and ``(a × d) / (b × (d + c))`` above is that
same quotient: the integer ratios take the place of the fixed powers of ten, so the step
needs no assumption about how many decimals the two values carry.

An empty ``qty``, ``price`` or ``tax_rate`` leaves all three amounts of that line empty, and
the write goes through (requirement 6.10); the header is always the sum of the non-empty
line values, 0 when there is none and when the bill has no line (requirement 6.4).

The values arrive as the fields hold them - the tools layer has rounded a number of the
caller to the precision of its field - and the amounts come back at money precision, with
exponent -2 and never -0, so they are stored and formatted as written (requirement 4.7).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from erp_mock.common.decimals import PLACES, WIDE, round_fraction, round_half_up
from erp_mock.config.model import BillModel

__all__ = [
    "AMOUNT_FIELDS",
    "NO_AMOUNTS",
    "PRICE_INCLUDES_TAX",
    "BillAmounts",
    "LineAmounts",
    "LineInput",
    "default_price_includes_tax",
    "line_amounts",
    "price_bill",
]

# The three money fields the primitive keeps, in the order meta-model.md section 4 puts
# them in; they are fields of the header and of every line (requirement 6.4).
AMOUNT_FIELDS: Final[tuple[str, ...]] = ("amount", "tax_amount", "amount_with_tax")

# The header field that says how the price of every line reads (requirement 6.3).
PRICE_INCLUDES_TAX: Final = "price_includes_tax"

# The decimal places of a money field (requirement 4.7).
_MONEY_PLACES: Final = PLACES["money"]


@dataclass(frozen=True, slots=True)
class LineInput:
    """What one line brings to the computation: its quantity, price and tax rate.

    Each of them may be empty, which leaves the amounts of the line empty
    (requirement 6.10).
    """

    qty: Decimal | None = None
    price: Decimal | None = None
    tax_rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LineAmounts:
    """The three money fields of one line, all three empty for an incomplete line."""

    amount: Decimal | None = None
    tax_amount: Decimal | None = None
    amount_with_tax: Decimal | None = None

    def values(self) -> dict[str, Decimal | None]:
        """The three amounts keyed by their field names, as a write stores them."""
        return {
            "amount": self.amount,
            "tax_amount": self.tax_amount,
            "amount_with_tax": self.amount_with_tax,
        }


@dataclass(frozen=True, slots=True)
class BillAmounts:
    """The amounts of one bill: those of every line, in line order, and the header's sums.

    The three header values are never empty: they are the sum of the non-empty values of
    the lines, 0 when there is none (requirement 6.4).
    """

    lines: tuple[LineAmounts, ...]
    amount: Decimal
    tax_amount: Decimal
    amount_with_tax: Decimal

    def values(self) -> dict[str, Decimal]:
        """The three header amounts keyed by their field names, as a write stores them."""
        return {
            "amount": self.amount,
            "tax_amount": self.tax_amount,
            "amount_with_tax": self.amount_with_tax,
        }


# The amounts of a line whose qty, price or tax_rate is empty (requirement 6.10).
NO_AMOUNTS: Final = LineAmounts()


def line_amounts(line: LineInput, *, price_includes_tax: bool) -> LineAmounts:
    """The three amounts of ``line`` under ``price_includes_tax`` (requirements 6.1, 6.2).

    :data:`NO_AMOUNTS` when one of the three values of the line is empty
    (requirement 6.10). Raises ValueError when ``1 + tax_rate`` is not positive, which no
    tax rate of a bill is: a rate below -1 would turn a total into its opposite.
    """
    qty, price, tax_rate = line.qty, line.price, line.tax_rate
    if qty is None or price is None or tax_rate is None:
        return NO_AMOUNTS
    # The product of the two field values, rounded to money once (requirement 6.1): the
    # net amount under a net price, the total under a price that includes tax.
    product = _money(WIDE.multiply(qty, price))
    if price_includes_tax:
        amount = _net_of_tax(product, tax_rate)
        return LineAmounts(
            amount=amount,
            tax_amount=_money(WIDE.subtract(product, amount)),
            amount_with_tax=product,
        )
    tax_amount = _money(WIDE.multiply(product, tax_rate))
    return LineAmounts(
        amount=product,
        tax_amount=tax_amount,
        amount_with_tax=_money(WIDE.add(product, tax_amount)),
    )


def price_bill(lines: Iterable[LineInput], *, price_includes_tax: bool) -> BillAmounts:
    """The amounts of every line of a bill and the sums of its header (requirement 6.4).

    ``lines`` are the lines of the bill after the write, in line number order; the result
    keeps that order. A bill without lines gets three zeros on its header.
    """
    priced = tuple(line_amounts(line, price_includes_tax=price_includes_tax) for line in lines)
    return BillAmounts(
        lines=priced,
        amount=_total(line.amount for line in priced),
        tax_amount=_total(line.tax_amount for line in priced),
        amount_with_tax=_total(line.amount_with_tax for line in priced),
    )


def default_price_includes_tax(bill: BillModel) -> bool:
    """The ``price_includes_tax`` of a new ``bill`` when no value reached it (req 6.3).

    The default the business pack declared for the field, and true when it declared none or
    an empty one - which is also the answer for a bill that does not have the field at all,
    so the caller needs no case of its own.
    """
    field = bill.header.get(PRICE_INCLUDES_TAX)
    declared = field.default if field is not None else None
    return declared if isinstance(declared, bool) else True


def _money(value: Decimal) -> Decimal:
    """``value`` rounded to money precision, ROUND_HALF_UP and never -0 (req 4.7)."""
    return round_half_up(value, _MONEY_PLACES)


def _net_of_tax(amount_with_tax: Decimal, tax_rate: Decimal) -> Decimal:
    """``round(amount_with_tax ÷ (1 + tax_rate), 2)`` over the exact quotient (req 6.2).

    Both operands go in as integer ratios, so the division is the exact rational one and
    is rounded once (see the module docstring).
    """
    num, den = amount_with_tax.as_integer_ratio()
    rate_num, rate_den = tax_rate.as_integer_ratio()
    # The divisor 1 + tax_rate as the fraction (rate_den + rate_num) / rate_den, whose
    # numerator is positive for every rate above -1.
    divisor_num = rate_den + rate_num
    if divisor_num <= 0:
        raise ValueError(f"1 + tax_rate must be positive, got tax_rate {tax_rate}")
    return round_fraction(num * rate_den, den * divisor_num, _MONEY_PLACES)


def _total(values: Iterable[Decimal | None]) -> Decimal:
    """The sum of the non-empty ``values``, 0 when there is none (requirement 6.4)."""
    total = Decimal(0)
    for value in values:
        if value is not None:
            total = WIDE.add(total, value)
    return _money(total)
