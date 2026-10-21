"""erp_mock.common.date_expr: evaluating date expressions at an anchor date (requirement 16.5)."""

from datetime import date
from typing import Final, assert_type

import pytest

from erp_mock.common.date_expr import (
    Category,
    DateExprError,
    DateExprRangeError,
    DayName,
    Edge,
    MonthDay,
    MonthPeriod,
    Today,
    WeekDay,
    eval_date_expr,
    parse_date_expr,
)

DATE = Category.DATE
PERIOD = Category.PERIOD

# A Friday; its week runs from Monday 2026-10-05 to Sunday 2026-10-11.
ANCHOR: Final = date(2026, 10, 9)
FIRST_DAY: Final = date(1, 1, 1)  # a Monday
LAST_DAY: Final = date(9999, 12, 31)  # a Friday
# More digits than int() and str() convert by default (4300).
DIGITS: Final = "9" * 5000


def date_at(anchor: date, text: str) -> date:
    """Value at ``anchor`` of the date category expression ``text``."""
    value = eval_date_expr(parse_date_expr(text, DATE), anchor)
    assert isinstance(value, date)
    return value


def period_at(anchor: date, text: str) -> str:
    """Value at ``anchor`` of the period category expression ``text``."""
    value = eval_date_expr(parse_date_expr(text, PERIOD), anchor)
    assert isinstance(value, str)
    return value


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("today", date(2026, 10, 9)),
        ("today+1d", date(2026, 10, 10)),
        ("today+23d", date(2026, 11, 1)),
        ("today-9d", date(2026, 9, 30)),
        ("today+365d", date(2027, 10, 9)),
        ("today-366d", date(2025, 10, 8)),
        ("this_week.mon", date(2026, 10, 5)),
        ("this_week.fri", date(2026, 10, 9)),
        ("this_week.sun", date(2026, 10, 11)),
        ("next_week.mon", date(2026, 10, 12)),
        ("next_week.sun", date(2026, 10, 18)),
        ("month(0).start", date(2026, 10, 1)),
        ("month(0).day(15)", date(2026, 10, 15)),
        ("month(0).end", date(2026, 10, 31)),
        ("month(1).end", date(2026, 11, 30)),
        ("month(-1).start", date(2026, 9, 1)),
        ("month(3).start", date(2027, 1, 1)),
        ("month(-10).end", date(2025, 12, 31)),
        ("month(1200).day(31)", date(2126, 10, 31)),
        ("quarter(0).start", date(2026, 10, 1)),
        ("quarter(0).end", date(2026, 12, 31)),
        ("quarter(1).start", date(2027, 1, 1)),
        ("quarter(-1).start", date(2026, 7, 1)),
        ("quarter(-1).end", date(2026, 9, 30)),
        ("quarter(2).end", date(2027, 6, 30)),
        ("quarter(-4).end", date(2025, 12, 31)),
        ("year(0).start", date(2026, 1, 1)),
        ("year(0).end", date(2026, 12, 31)),
        ("year(1).start", date(2027, 1, 1)),
        ("year(-1).end", date(2025, 12, 31)),
        ("year(-1200).start", date(826, 1, 1)),
    ],
)
def test_every_form_at_the_anchor(text: str, expected: date) -> None:
    assert date_at(ANCHOR, text) == expected


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("month(0)", "2026-10"),
        ("month(-1)", "2026-09"),
        ("month(2)", "2026-12"),
        ("month(3)", "2027-01"),
        ("month(-10)", "2025-12"),
        ("month(-1200)", "1926-10"),
        ("month(1200)", "2126-10"),
    ],
)
def test_period_is_the_year_and_month(text: str, expected: str) -> None:
    assert period_at(ANCHOR, text) == expected


@pytest.mark.req("16.5")
def test_period_year_has_four_digits() -> None:
    assert period_at(date(999, 5, 20), "month(0)") == "0999-05"
    assert period_at(date(1, 1, 1), "month(11)") == "0001-12"


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    ("anchor", "text", "expected"),
    [
        # A day past the end of the month gives its last day.
        (date(2026, 1, 31), "month(1).day(31)", date(2026, 2, 28)),
        (date(2026, 1, 31), "month(1).day(28)", date(2026, 2, 28)),
        (date(2026, 1, 31), "month(1).end", date(2026, 2, 28)),
        (date(2026, 1, 31), "month(2).day(31)", date(2026, 3, 31)),
        (date(2026, 1, 31), "month(3).day(31)", date(2026, 4, 30)),
        (date(2026, 1, 31), "today+1d", date(2026, 2, 1)),
        (ANCHOR, "month(-1).day(31)", date(2026, 9, 30)),
        (ANCHOR, "month(1).day(31)", date(2026, 11, 30)),
        (ANCHOR, "month(4).day(29)", date(2027, 2, 28)),
        (ANCHOR, "month(-32).day(30)", date(2024, 2, 29)),
        # Leap years: 2024 and 2000 are, 1900 is not.
        (date(2024, 1, 31), "month(1).day(31)", date(2024, 2, 29)),
        (date(2024, 1, 31), "month(1).end", date(2024, 2, 29)),
        (date(2024, 1, 31), "today+29d", date(2024, 2, 29)),
        (date(2024, 2, 29), "month(12).day(29)", date(2025, 2, 28)),
        (date(2024, 2, 29), "month(48).day(29)", date(2028, 2, 29)),
        (date(2024, 2, 29), "month(-12).end", date(2023, 2, 28)),
        (date(2024, 2, 29), "today+366d", date(2025, 3, 1)),
        (date(2024, 2, 29), "today-365d", date(2023, 3, 1)),
        (date(2000, 2, 1), "month(0).end", date(2000, 2, 29)),
        (date(1900, 2, 1), "month(0).end", date(1900, 2, 28)),
    ],
)
def test_ends_of_months(anchor: date, text: str, expected: date) -> None:
    assert date_at(anchor, text) == expected


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    ("anchor", "text", "expected"),
    [
        # Quarters start in January, April, July and October.
        (date(2026, 3, 31), "quarter(0).start", date(2026, 1, 1)),
        (date(2026, 3, 31), "quarter(0).end", date(2026, 3, 31)),
        (date(2026, 3, 31), "quarter(1).start", date(2026, 4, 1)),
        (date(2026, 4, 1), "quarter(0).start", date(2026, 4, 1)),
        (date(2026, 4, 1), "quarter(0).end", date(2026, 6, 30)),
        (date(2026, 4, 1), "quarter(-1).end", date(2026, 3, 31)),
        (date(2026, 8, 15), "quarter(0).start", date(2026, 7, 1)),
        (date(2026, 8, 15), "quarter(0).end", date(2026, 9, 30)),
        (date(2026, 2, 15), "quarter(-1).start", date(2025, 10, 1)),
        (date(2026, 12, 31), "quarter(0).start", date(2026, 10, 1)),
        (date(2026, 12, 31), "quarter(1).end", date(2027, 3, 31)),
        # Years.
        (date(2026, 1, 1), "year(0).start", date(2026, 1, 1)),
        (date(2026, 1, 1), "year(0).end", date(2026, 12, 31)),
        (date(2026, 1, 1), "year(-1).end", date(2025, 12, 31)),
        (date(2026, 12, 31), "year(0).start", date(2026, 1, 1)),
        (date(2026, 12, 31), "year(1).start", date(2027, 1, 1)),
    ],
)
def test_quarter_and_year_boundaries(anchor: date, text: str, expected: date) -> None:
    assert date_at(anchor, text) == expected


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    ("anchor", "text", "expected"),
    [
        # Thursday 2026-12-31: its week runs from 2026-12-28 to 2027-01-03.
        (date(2026, 12, 31), "today+1d", date(2027, 1, 1)),
        (date(2026, 12, 31), "this_week.mon", date(2026, 12, 28)),
        (date(2026, 12, 31), "this_week.sun", date(2027, 1, 3)),
        (date(2026, 12, 31), "next_week.fri", date(2027, 1, 8)),
        (date(2026, 12, 31), "month(1).start", date(2027, 1, 1)),
        # Friday 2027-01-01.
        (date(2027, 1, 1), "today-1d", date(2026, 12, 31)),
        (date(2027, 1, 1), "this_week.mon", date(2026, 12, 28)),
        (date(2027, 1, 1), "month(-1).end", date(2026, 12, 31)),
        (date(2027, 1, 1), "month(-13).day(31)", date(2025, 12, 31)),
        (date(2027, 1, 1), "quarter(-1).start", date(2026, 10, 1)),
    ],
)
def test_across_the_end_of_a_year(anchor: date, text: str, expected: date) -> None:
    assert date_at(anchor, text) == expected


@pytest.mark.req("16.5")
def test_across_the_end_of_a_year_for_periods() -> None:
    assert period_at(date(2026, 12, 31), "month(1)") == "2027-01"
    assert period_at(date(2027, 1, 1), "month(-1)") == "2026-12"
    assert period_at(date(2027, 1, 1), "month(-13)") == "2025-12"


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    ("anchor", "text", "expected"),
    [
        # Weeks run from Monday to Sunday: a Sunday ends its week.
        (date(2026, 10, 11), "this_week.mon", date(2026, 10, 5)),
        (date(2026, 10, 11), "this_week.sat", date(2026, 10, 10)),
        (date(2026, 10, 11), "this_week.sun", date(2026, 10, 11)),
        (date(2026, 10, 11), "next_week.mon", date(2026, 10, 12)),
        (date(2026, 10, 11), "next_week.sun", date(2026, 10, 18)),
        # A Monday starts its week.
        (date(2026, 10, 5), "this_week.mon", date(2026, 10, 5)),
        (date(2026, 10, 5), "this_week.sun", date(2026, 10, 11)),
        (date(2026, 10, 5), "next_week.mon", date(2026, 10, 12)),
    ],
)
def test_weeks_start_on_monday(anchor: date, text: str, expected: date) -> None:
    assert date_at(anchor, text) == expected


@pytest.mark.req("16.5")
def test_every_weekday() -> None:
    days: list[DayName] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    for offset, day in enumerate(days):
        assert eval_date_expr(WeekDay("this_week", day), ANCHOR) == date(2026, 10, 5 + offset)
        assert eval_date_expr(WeekDay("next_week", day), ANCHOR) == date(2026, 10, 12 + offset)


@pytest.mark.req("16.5")
def test_trees_evaluate_to_precise_types() -> None:
    day = eval_date_expr(Edge("quarter", -1, "end"), ANCHOR)
    assert_type(day, date)
    assert type(day) is date
    assert day == date(2026, 9, 30)
    period = eval_date_expr(MonthPeriod(-1), ANCHOR)
    assert_type(period, str)
    assert period == "2026-09"
    assert eval_date_expr(MonthDay(-1, 31), ANCHOR) == date(2026, 9, 30)
    assert eval_date_expr(Today(-9), ANCHOR) == date(2026, 9, 30)


# --- the ends of the calendar ------------------------------------------------------------


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    ("anchor", "text", "expected"),
    [
        (FIRST_DAY, "today", FIRST_DAY),
        (FIRST_DAY, "this_week.mon", FIRST_DAY),
        (date(1, 1, 7), "this_week.mon", FIRST_DAY),
        (FIRST_DAY, "month(0).start", FIRST_DAY),
        (FIRST_DAY, "quarter(0).start", FIRST_DAY),
        (date(1, 12, 31), "year(0).start", FIRST_DAY),
        (FIRST_DAY, "today+3652058d", LAST_DAY),
        (FIRST_DAY, "month(119987).end", LAST_DAY),
        (LAST_DAY, "today", LAST_DAY),
        (LAST_DAY, "this_week.fri", LAST_DAY),
        (date(9999, 12, 26), "next_week.fri", LAST_DAY),
        (LAST_DAY, "month(0).end", LAST_DAY),
        (date(9999, 10, 1), "quarter(0).end", LAST_DAY),
        (date(9999, 1, 1), "year(0).end", LAST_DAY),
        (LAST_DAY, "today-3652058d", FIRST_DAY),
        (LAST_DAY, "month(-119987).start", FIRST_DAY),
    ],
)
def test_values_at_the_ends_of_the_calendar(anchor: date, text: str, expected: date) -> None:
    assert date_at(anchor, text) == expected


@pytest.mark.req("16.5")
def test_periods_at_the_ends_of_the_calendar() -> None:
    assert period_at(FIRST_DAY, "month(0)") == "0001-01"
    assert period_at(LAST_DAY, "month(0)") == "9999-12"
    assert period_at(LAST_DAY, "month(-119987)") == "0001-01"


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    ("anchor", "text", "category"),
    [
        # Before 0001-01-01.
        (FIRST_DAY, "today-1d", DATE),
        (FIRST_DAY, "month(-1).start", DATE),
        (date(1, 12, 31), "month(-12).day(31)", DATE),
        (date(1, 3, 1), "quarter(-1).end", DATE),
        (date(1, 6, 1), "year(-1).end", DATE),
        (LAST_DAY, "month(-119988).end", DATE),
        (FIRST_DAY, "month(-1)", PERIOD),
        # After 9999-12-31.
        (LAST_DAY, "today+1d", DATE),
        (LAST_DAY, "this_week.sat", DATE),
        (LAST_DAY, "this_week.sun", DATE),
        (date(9999, 12, 27), "next_week.mon", DATE),
        (date(9999, 12, 26), "next_week.sat", DATE),
        (date(9999, 12, 1), "month(1).start", DATE),
        (date(9999, 1, 1), "month(12).day(1)", DATE),
        (date(9999, 10, 1), "quarter(1).start", DATE),
        (date(9999, 1, 1), "year(1).end", DATE),
        (FIRST_DAY, "month(119988).start", DATE),
        (LAST_DAY, "month(1)", PERIOD),
    ],
)
def test_values_outside_the_calendar_are_errors(
    anchor: date, text: str, category: Category
) -> None:
    expr = parse_date_expr(text, category)
    with pytest.raises(DateExprRangeError) as info:
        eval_date_expr(expr, anchor)
    assert (info.value.text, info.value.anchor) == (text, anchor)


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    ("template", "category"),
    [
        ("today+{}d", DATE),
        ("today-{}d", DATE),
        ("month({}).day(31)", DATE),
        ("month(-{}).end", DATE),
        ("quarter(-{}).start", DATE),
        ("year({}).end", DATE),
        ("month({})", PERIOD),
        ("month(-{})", PERIOD),
    ],
)
def test_offsets_of_any_length_are_errors(template: str, category: Category) -> None:
    # 10 digits exceed timedelta's limit of 999999999 days; DIGITS exceeds int()'s limit.
    for digits in ["1000000000", DIGITS]:
        text = template.format(digits)
        expr = parse_date_expr(text, category)
        with pytest.raises(DateExprRangeError) as info:
            eval_date_expr(expr, ANCHOR)
        assert (info.value.text, info.value.anchor) == (text, ANCHOR)


@pytest.mark.req("16.5")
def test_range_error_names_the_expression_and_the_anchor() -> None:
    with pytest.raises(DateExprRangeError) as info:
        eval_date_expr(Today(1), LAST_DAY)
    error = info.value
    assert (error.text, error.anchor) == ("today+1d", LAST_DAY)
    assert str(error) == (
        "date expression 'today+1d' at anchor date 9999-12-31 falls outside the years 0001 to 9999"
    )
    # A range error is a ValueError but not a parse error.
    assert isinstance(error, ValueError)
    assert not isinstance(error, DateExprError)
