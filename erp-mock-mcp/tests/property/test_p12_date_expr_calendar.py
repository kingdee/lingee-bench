"""Property 12: the calendar behaviour of date expression values.

**Validates: Requirements 16.5, 16.9, 16.11, 16.12**

At any anchor date a from 1900-01-01 to 2100-12-31, with N from -1200 to 1200 and D from
1 to 31:

- month(N).start, month(N).day(D) and month(N).end all fall in the month of a moved by N
  months, and are non-decreasing in that order (requirement 16.9);
- this_week.mon is not later than a and a is not later than this_week.sun, the weekday of
  this_week.W is W, and next_week.W is 7 days after this_week.W (requirement 16.11);
- for each of the periods month, quarter and year X, X(0).start is not later than a and a
  is not later than X(0).end, and the day after X(N).end is X(N+1).start
  (requirement 16.12).

Three more values of requirement 16.5 are checked against independent definitions:
today+Nd and today-Nd against datetime.timedelta, the edges of each period against the
months a period of that kind may start in and the number of months it spans, and the text
of the period month(N) against the first day of that month.

The expected values are computed from the drawn anchor date and offset, not from
erp_mock.common.date_expr: the month of a moved by N months comes from shifted_month
below, weekdays from datetime.date.weekday, and the months of quarters and years are
written out in START_MONTHS and PERIOD_MONTHS. Every value stays inside the years 0001 to
9999, so no expression of these ranges raises DateExprRangeError.
"""

from datetime import date, timedelta
from typing import Final

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from erp_mock.common.date_expr import (
    DayName,
    Edge,
    MonthDay,
    MonthPeriod,
    Today,
    UnitName,
    WeekDay,
    eval_date_expr,
)

# --- generators --------------------------------------------------------------------------

# The ranges of requirement 16 items 9, 11 and 12.
ANCHORS: Final = st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
OFFSETS: Final = st.integers(min_value=-1200, max_value=1200)
DAYS_OF_MONTH: Final = st.integers(min_value=1, max_value=31)
# N of today+Nd and today-Nd: a positive number of days, up to a century, which keeps
# values at any anchor date well inside the years 0001 to 9999.
DAY_COUNTS: Final = st.integers(min_value=1, max_value=36500)

# The day names of requirement 16.4 in the order of datetime.date.weekday(), Monday first,
# and the three kinds of period, written out rather than taken from date_expr.
DAY_NAMES: Final[tuple[DayName, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
UNITS: Final[tuple[UnitName, ...]] = ("month", "quarter", "year")
# Months in each kind of period, and the months such a period may start in: quarters run
# from January, April, July and October, years from January (requirement 16.5).
PERIOD_MONTHS: Final[dict[UnitName, int]] = {"month": 1, "quarter": 3, "year": 12}
START_MONTHS: Final[dict[UnitName, tuple[int, ...]]] = {
    "month": tuple(range(1, 13)),
    "quarter": (1, 4, 7, 10),
    "year": (1,),
}

# Anchor dates worth checking at every example count: the last day of a 31 day month, the
# leap day of 2024 (a Thursday), a Sunday, which ends its week, and the Monday that starts
# the same week.
MONTH_END: Final = date(2026, 1, 31)
LEAP_DAY: Final = date(2024, 2, 29)
SUNDAY: Final = date(2026, 10, 11)
MONDAY: Final = date(2026, 10, 5)


def shifted_month(anchor: date, n: int) -> tuple[int, int]:
    """Year and month (1 to 12) of the month of ``anchor`` moved by ``n`` months."""
    months = anchor.month - 1 + n
    return anchor.year + months // 12, months % 12 + 1


# --- property ----------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 12: 日期表达式求值的日历性质
@pytest.mark.req("16.9")
@settings(max_examples=100)
@given(anchor=ANCHORS, n=OFFSETS, day=DAYS_OF_MONTH)
@example(anchor=MONTH_END, n=1, day=31)  # February has fewer than 31 days
@example(anchor=MONTH_END, n=0, day=31)
@example(anchor=LEAP_DAY, n=-12, day=29)  # February 2023 has no 29th
@example(anchor=LEAP_DAY, n=0, day=31)
@example(anchor=SUNDAY, n=1200, day=1)
def test_month_values_stay_in_the_shifted_month(anchor: date, n: int, day: int) -> None:
    start = eval_date_expr(Edge("month", n, "start"), anchor)
    day_of_month = eval_date_expr(MonthDay(n, day), anchor)
    end = eval_date_expr(Edge("month", n, "end"), anchor)
    year_month = shifted_month(anchor, n)
    for value in (start, day_of_month, end):
        assert (value.year, value.month) == year_month
    assert start <= day_of_month <= end


# Feature: erp-mock-mcp-m0, Property 12: 日期表达式求值的日历性质
@pytest.mark.req("16.11")
@settings(max_examples=100)
@given(anchor=ANCHORS)
@example(anchor=SUNDAY)
@example(anchor=MONDAY)
@example(anchor=LEAP_DAY)
@example(anchor=MONTH_END)
def test_this_week_covers_the_anchor_and_next_week_is_7_days_later(anchor: date) -> None:
    monday = eval_date_expr(WeekDay("this_week", "mon"), anchor)
    sunday = eval_date_expr(WeekDay("this_week", "sun"), anchor)
    assert monday <= anchor <= sunday
    for weekday, name in enumerate(DAY_NAMES):
        this_week = eval_date_expr(WeekDay("this_week", name), anchor)
        assert this_week.weekday() == weekday
        assert monday <= this_week <= sunday
        next_week = eval_date_expr(WeekDay("next_week", name), anchor)
        assert next_week == this_week + timedelta(days=7)


# Feature: erp-mock-mcp-m0, Property 12: 日期表达式求值的日历性质
@pytest.mark.req("16.12")
@pytest.mark.parametrize("unit", UNITS)
@settings(max_examples=100)
@given(anchor=ANCHORS, n=OFFSETS)
@example(anchor=date(1900, 1, 1), n=-1200)
@example(anchor=date(2100, 12, 31), n=1200)
@example(anchor=MONTH_END, n=-1)
@example(anchor=LEAP_DAY, n=0)
@example(anchor=SUNDAY, n=0)
def test_periods_contain_the_anchor_and_follow_each_other(
    unit: UnitName, anchor: date, n: int
) -> None:
    assert eval_date_expr(Edge(unit, 0, "start"), anchor) <= anchor
    assert anchor <= eval_date_expr(Edge(unit, 0, "end"), anchor)
    end = eval_date_expr(Edge(unit, n, "end"), anchor)
    next_start = eval_date_expr(Edge(unit, n + 1, "start"), anchor)
    assert end + timedelta(days=1) == next_start


# Feature: erp-mock-mcp-m0, Property 12: 日期表达式求值的日历性质
@pytest.mark.req("16.5")
@settings(max_examples=100)
@given(anchor=ANCHORS, days=DAY_COUNTS)
@example(anchor=MONTH_END, days=1)  # into February
@example(anchor=LEAP_DAY, days=366)
@example(anchor=SUNDAY, days=7)
def test_day_offsets_match_timedelta(anchor: date, days: int) -> None:
    assert eval_date_expr(Today(0), anchor) == anchor
    assert eval_date_expr(Today(days), anchor) == anchor + timedelta(days=days)
    assert eval_date_expr(Today(-days), anchor) == anchor - timedelta(days=days)


# Feature: erp-mock-mcp-m0, Property 12: 日期表达式求值的日历性质
@pytest.mark.req("16.5")
@pytest.mark.parametrize("unit", UNITS)
@settings(max_examples=100)
@given(anchor=ANCHORS, n=OFFSETS)
@example(anchor=MONTH_END, n=1)
@example(anchor=LEAP_DAY, n=0)
@example(anchor=date(2100, 12, 31), n=1)
def test_period_edges_are_the_first_and_last_days_of_the_period(
    unit: UnitName, anchor: date, n: int
) -> None:
    start = eval_date_expr(Edge(unit, n, "start"), anchor)
    end = eval_date_expr(Edge(unit, n, "end"), anchor)
    assert start.month in START_MONTHS[unit]
    assert start.day == 1
    # The last day of a month: the next day is the first of the following one.
    assert (end + timedelta(days=1)).day == 1
    assert (end.year - start.year) * 12 + end.month - start.month == PERIOD_MONTHS[unit] - 1


# Feature: erp-mock-mcp-m0, Property 12: 日期表达式求值的日历性质
@pytest.mark.req("16.5")
@settings(max_examples=100)
@given(anchor=ANCHORS, n=OFFSETS)
@example(anchor=MONTH_END, n=1)
@example(anchor=LEAP_DAY, n=0)
@example(anchor=date(1900, 1, 1), n=-1200)
def test_period_text_is_the_year_and_month_of_its_month(anchor: date, n: int) -> None:
    period = eval_date_expr(MonthPeriod(n), anchor)
    start = eval_date_expr(Edge("month", n, "start"), anchor)
    assert period == f"{start.year:04d}-{start.month:02d}"
    assert shifted_month(anchor, n) == (start.year, start.month)
