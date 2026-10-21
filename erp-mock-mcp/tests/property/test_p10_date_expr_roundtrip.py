"""Property 10: round trip of date expressions.

**Validates: Requirements 16.4, 16.7, 16.8**

For every syntax tree e of the date or period category, print_date_expr(e) is a text of
that category in the language of requirement 16.4, parse_date_expr of that text in that
category gives back e (requirement 16.8), and printing the parsed tree gives the same text
again (requirement 16.7). MonthPeriod is the only tree of the period category; Today,
WeekDay, MonthDay and Edge belong to the date category.

LANGUAGE writes the texts of requirement 16.4 as regular expressions, independently of the
parser. Each of the five kinds of tree has its own run of 100 examples, so that every run
covers every form of requirement 16.4, both weeks with the 7 day names, and the three units
with both edges, which one run of 100 examples spread over all kinds could miss.
"""

import re
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from erp_mock.common.date_expr import (
    Category,
    DateExpr,
    DayName,
    Edge,
    EdgeName,
    MonthDay,
    MonthPeriod,
    Today,
    UnitName,
    WeekDay,
    WeekName,
    parse_date_expr,
    print_date_expr,
)

# --- the language of requirement 16.4 ----------------------------------------------------

# N of month(N), quarter(N) and year(N) is 0 or a nonzero integer with an optional "-", and
# D is 1 to 31, both without leading zeros.
_N: Final = r"(?:0|-?[1-9][0-9]*)"
_D: Final = r"(?:[1-9]|[12][0-9]|3[01])"

# The texts of each category, to be used with fullmatch. N of today+Nd and today-Nd is a
# positive integer without leading zeros. [0-9] matches ASCII digits only.
LANGUAGE: Final[dict[Category, re.Pattern[str]]] = {
    Category.DATE: re.compile(
        r"today(?:[+-][1-9][0-9]*d)?"
        r"|(?:this|next)_week\.(?:mon|tue|wed|thu|fri|sat|sun)"
        rf"|month\({_N}\)\.(?:day\({_D}\)|start|end)"
        rf"|(?:quarter|year)\({_N}\)\.(?:start|end)"
    ),
    Category.PERIOD: re.compile(rf"month\({_N}\)"),
}

# --- generators --------------------------------------------------------------------------

# The names of requirement 16.4, written out rather than taken from date_expr.
WEEKS: Final[tuple[WeekName, ...]] = ("this_week", "next_week")
DAY_NAMES: Final[tuple[DayName, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
UNITS: Final[tuple[UnitName, ...]] = ("month", "quarter", "year")
EDGES: Final[tuple[EdgeName, ...]] = ("start", "end")

# Exponents of powers of ten: up to 40, and 4290 to 4310, on both sides of the 4300 digits
# beyond which int() and str() refuse decimal texts by default (sys.get_int_max_str_digits).
POWERS: Final = st.one_of(
    st.integers(min_value=1, max_value=40), st.integers(min_value=4290, max_value=4310)
)

# Positive integers: one digit; a few digits, as datasets write them; numbers far beyond
# 64-bit integers and the 28 digits of Decimal's default precision; and both sides of a
# carry into one more digit (9 and 10, 99 and 100, ..., up to more than 4300 digits).
MAGNITUDES: Final = st.one_of(
    st.integers(min_value=1, max_value=9),
    st.integers(min_value=10, max_value=9999),
    st.integers(min_value=1, max_value=10**60),
    POWERS.map(lambda exponent: 10**exponent - 1),
    POWERS.map(lambda exponent: 10**exponent),
)

# Today.days and N of month(N), quarter(N) and year(N): 0, positive or negative.
OFFSETS: Final = st.one_of(st.just(0), MAGNITUDES, MAGNITUDES.map(lambda n: -n))

# The trees of each kind, drawn with every value of every field.
TREES: Final[dict[type[DateExpr], st.SearchStrategy[DateExpr]]] = {
    Today: st.builds(Today, OFFSETS),
    WeekDay: st.builds(WeekDay, st.sampled_from(WEEKS), st.sampled_from(DAY_NAMES)),
    MonthDay: st.builds(MonthDay, OFFSETS, st.integers(min_value=1, max_value=31)),
    Edge: st.builds(Edge, st.sampled_from(UNITS), OFFSETS, st.sampled_from(EDGES)),
    MonthPeriod: st.builds(MonthPeriod, OFFSETS),
}

# --- property ----------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 10: 日期表达式往返
@pytest.mark.req("16.4", "16.7", "16.8")
@pytest.mark.parametrize("kind", list(TREES), ids=lambda kind: kind.__name__)
@settings(max_examples=100)
@given(data=st.data())
def test_print_and_parse_round_trip(kind: type[DateExpr], data: st.DataObject) -> None:
    expr = data.draw(TREES[kind], label="expr")
    category = Category.PERIOD if kind is MonthPeriod else Category.DATE
    text = print_date_expr(expr)
    assert LANGUAGE[category].fullmatch(text), f"not a {category.value} text: {text!r}"
    parsed = parse_date_expr(text, category)
    assert parsed == expr
    assert print_date_expr(parsed) == text
