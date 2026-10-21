"""erp_mock.common.date_expr: parsing and printing date expressions (requirement 16)."""

import pytest

from erp_mock.common.date_expr import (
    Category,
    DateExpr,
    DateExprError,
    DayName,
    Edge,
    MonthDay,
    MonthPeriod,
    Today,
    WeekDay,
    WeekName,
    parse_date_expr,
    print_date_expr,
)

DATE = Category.DATE
PERIOD = Category.PERIOD


def error_position(text: str, category: Category) -> int:
    """Parse ``text``, which must fail, and return the reported position."""
    with pytest.raises(DateExprError) as info:
        parse_date_expr(text, category)
    assert info.value.text == text
    return info.value.position


# --- accepted texts (requirements 16.4, 16.7) --------------------------------------------

ACCEPTED: list[tuple[str, Category, DateExpr]] = [
    ("today", DATE, Today(0)),
    ("today+3d", DATE, Today(3)),
    ("today-15d", DATE, Today(-15)),
    ("today+36500d", DATE, Today(36500)),
    ("this_week.mon", DATE, WeekDay("this_week", "mon")),
    ("next_week.sun", DATE, WeekDay("next_week", "sun")),
    ("month(0).day(1)", DATE, MonthDay(0, 1)),
    ("month(-1).day(12)", DATE, MonthDay(-1, 12)),
    ("month(10).day(31)", DATE, MonthDay(10, 31)),
    ("month(0).start", DATE, Edge("month", 0, "start")),
    ("month(-12).end", DATE, Edge("month", -12, "end")),
    ("month(1234567890123).end", DATE, Edge("month", 1234567890123, "end")),
    ("quarter(0).start", DATE, Edge("quarter", 0, "start")),
    ("quarter(-1).end", DATE, Edge("quarter", -1, "end")),
    ("year(1).start", DATE, Edge("year", 1, "start")),
    ("year(-1200).end", DATE, Edge("year", -1200, "end")),
    ("month(0)", PERIOD, MonthPeriod(0)),
    ("month(-1)", PERIOD, MonthPeriod(-1)),
    ("month(250)", PERIOD, MonthPeriod(250)),
]


@pytest.mark.req("16.4", "16.7")
@pytest.mark.parametrize(("text", "category", "expr"), ACCEPTED)
def test_parse_and_print(text: str, category: Category, expr: DateExpr) -> None:
    assert parse_date_expr(text, category) == expr
    assert print_date_expr(expr) == text


@pytest.mark.req("16.4", "16.7")
@pytest.mark.parametrize("week", ["this_week", "next_week"])
@pytest.mark.parametrize("day", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
def test_every_weekday(week: WeekName, day: DayName) -> None:
    text = f"{week}.{day}"
    assert parse_date_expr(text, DATE) == WeekDay(week, day)
    assert print_date_expr(WeekDay(week, day)) == text


@pytest.mark.req("16.4", "16.7")
def test_every_day_of_month() -> None:
    for day in range(1, 32):
        text = f"month(0).day({day})"
        assert parse_date_expr(text, DATE) == MonthDay(0, day)
        assert print_date_expr(MonthDay(0, day)) == text


@pytest.mark.req("16.4", "16.7")
def test_numbers_of_any_length() -> None:
    # More digits than int() and str() convert by default (4300).
    digits = "9" * 5000
    for text, category in [
        (f"today-{digits}d", DATE),
        (f"year(-{digits}).end", DATE),
        (f"month({digits})", PERIOD),
    ]:
        assert print_date_expr(parse_date_expr(text, category)) == text
    assert parse_date_expr(f"month({digits})", PERIOD) == MonthPeriod(10**5000 - 1)


# --- error positions (requirement 16.6) --------------------------------------------------


@pytest.mark.req("16.6")
@pytest.mark.parametrize(
    ("text", "position"),
    [
        ("Today", 1),  # no case folding
        ("today+0d", 7),  # "today+" is a legal prefix, "today+0" is not
        ("today + 3d", 6),  # no whitespace
        ("month(-1).day(32)", 16),  # "month(-1).day(3" is a legal prefix
        ("month(0)", 9),  # a date needs .day(D), .start or .end: the text ends early
    ],
)
def test_examples_of_the_requirement(text: str, position: int) -> None:
    assert error_position(text, DATE) == position


@pytest.mark.req("16.6")
def test_error_names_the_text_and_the_position() -> None:
    with pytest.raises(DateExprError) as info:
        parse_date_expr("today+0d", DATE)
    assert (info.value.text, info.value.position) == ("today+0d", 7)
    assert str(info.value) == "invalid date expression 'today+0d': unexpected '0' at position 7"
    with pytest.raises(DateExprError) as info:
        parse_date_expr("month(0)", DATE)
    assert str(info.value) == (
        "invalid date expression 'month(0)': unexpected end of text at position 9"
    )


@pytest.mark.req("16.4", "16.6")
@pytest.mark.parametrize(
    ("text", "position"),
    [
        ("today", 1),
        ("this_week.mon", 1),
        ("quarter(0)", 1),
        ("year(0)", 1),
        ("month(0).start", 9),
        ("month(0).day(1)", 9),
        ("month(0).", 9),
        ("month(0)x", 9),
        ("", 1),
        ("month", 6),
        ("month(", 7),
    ],
)
def test_period_category_accepts_only_month_n(text: str, position: int) -> None:
    assert error_position(text, PERIOD) == position


@pytest.mark.req("16.4", "16.6")
@pytest.mark.parametrize(
    ("text", "position"),
    [
        ("quarter(0)", 11),
        ("year(-2)", 9),
        ("quarter(0).day(1)", 12),  # only month(N) has day(D)
    ],
)
def test_date_category_needs_a_day(text: str, position: int) -> None:
    assert error_position(text, DATE) == position


@pytest.mark.req("16.4", "16.6")
@pytest.mark.parametrize(
    ("text", "position"),
    [
        # N of today+Nd and today-Nd: a positive integer without leading zeros.
        ("today-0d", 7),
        ("today+05d", 7),
        ("today+-5d", 7),
        ("today+d", 7),
        ("today+5", 8),
        ("today+5x", 8),
        # Other N: 0, or a nonzero integer without leading zeros and an optional "-".
        ("month(00).start", 8),
        ("month(07).start", 8),
        ("month(-0).start", 8),
        ("month(-07).start", 8),
        ("month(--1).start", 8),
        ("month(+1).start", 7),
        ("month().start", 7),
        ("month(1.5).start", 8),
        ("quarter(01).end", 10),
        ("year(-0).end", 7),
        # D: 1 to 31 without leading zeros.
        ("month(0).day(0)", 14),
        ("month(0).day(01)", 14),
        ("month(0).day(-1)", 14),
        ("month(0).day()", 14),
        ("month(0).day(32)", 15),
        ("month(0).day(40)", 15),
        ("month(0).day(310)", 16),
        ("month(0).day(100)", 16),
    ],
)
def test_leading_zeros_and_ranges(text: str, position: int) -> None:
    assert error_position(text, DATE) == position


@pytest.mark.req("16.4", "16.6")
@pytest.mark.parametrize(
    ("text", "position"),
    [
        ("TODAY", 1),
        ("this_week.Mon", 11),
        ("month(0).END", 10),
        (" today", 1),
        ("today ", 6),
        ("today\n", 6),
        ("month( 0).start", 7),
        ("month(0) .start", 9),
        ("month(0).day(1 )", 15),
        ("today\u00a0", 6),  # no-break space
        ("month(\uff11).start", 7),  # full-width digit one
    ],
)
def test_no_case_folding_or_whitespace(text: str, position: int) -> None:
    assert error_position(text, DATE) == position


@pytest.mark.req("16.6")
@pytest.mark.parametrize(
    ("text", "position"),
    [
        # The text ends early: the position is its length plus 1.
        ("", 1),
        ("t", 2),
        ("this_week", 10),
        ("next_week.", 11),
        ("this_week.t", 12),
        ("month(0).day(1", 15),
        ("quarter(-3).st", 15),
        # A complete expression followed by more text.
        ("todayx", 6),
        ("today+3dd", 9),
        ("this_week.monday", 14),
        ("year(0).end.", 12),
        ("month(0).day(5))", 16),
        # Characters that no legal expression allows after the read part.
        ("tx", 2),
        ("next_week.thur", 14),
        ("month(0).days(1)", 13),
    ],
)
def test_longest_legal_prefix(text: str, position: int) -> None:
    assert error_position(text, DATE) == position


# --- syntax trees (requirement 16.7) -----------------------------------------------------


@pytest.mark.req("16.7")
@pytest.mark.parametrize(
    ("cls", "args", "error"),
    [
        (MonthDay, (0, 0), ValueError),
        (MonthDay, (0, 32), ValueError),
        (WeekDay, ("this_week", "Mon"), ValueError),
        (WeekDay, ("last_week", "mon"), ValueError),
        (Edge, ("week", 0, "start"), ValueError),
        (Edge, ("month", 0, "middle"), ValueError),
        (Today, (True,), TypeError),
        (MonthDay, (0, 1.0), TypeError),
        (MonthPeriod, ("1",), TypeError),
    ],
)
def test_trees_without_a_legal_text_cannot_be_built(
    cls: type, args: tuple[object, ...], error: type[Exception]
) -> None:
    with pytest.raises(error):
        cls(*args)
