"""Relative date expressions: syntax trees, parser, printer and evaluator (requirement 16).

Datasets write business dates relative to the sandbox's anchor date. The caller names the
category of expression it needs (requirement 16.4):

- date: ``today``, ``today+Nd``, ``today-Nd``, ``this_week.W``, ``next_week.W``,
  ``month(N).day(D)``, ``month(N).start``, ``month(N).end``, ``quarter(N).start``,
  ``quarter(N).end``, ``year(N).start`` and ``year(N).end``;
- period: ``month(N)`` only, as in ``periods.closed_before``.

N of ``today+Nd`` and ``today-Nd`` is a positive integer without leading zeros; every other
N is 0 or a nonzero integer without leading zeros, optionally preceded by ``-``. N may have
any number of digits. D is 1 to 31 without leading zeros, and W is mon, tue, wed, thu, fri,
sat or sun. Texts are lowercase and contain no whitespace: nothing is case-folded or
skipped.

Every syntax tree has exactly one text (``Today(0)`` is ``today``; ``today+0d`` and
``month(-0)`` are not legal), and the constructors reject field values that have no legal
text, so print_date_expr and parse_date_expr are inverse to each other (requirement 16.7).

eval_date_expr gives the value of an expression at an anchor date a (requirement 16.5):

- ``today`` is a, and ``today+Nd`` and ``today-Nd`` are the N-th day after and before a;
- ``this_week.W`` is the day W of the week of a, weeks running from Monday to Sunday, and
  ``next_week.W`` is the day 7 days after it;
- ``month(N)``, ``quarter(N)`` and ``year(N)`` stand for the calendar month, quarter
  (3 months from January, April, July or October) or year of a, moved by N periods of
  the same kind; ``.start`` and ``.end`` are the first and last days of that period, and
  ``.day(D)`` is day D of the month, or its last day when the month has fewer days;
- the period ``month(N)`` gives the text ``YYYY-MM`` of that month.

A value outside the years 0001 to 9999 raises DateExprRangeError, which is not a
DateExprError: the text is legal, but its value at that anchor date is not a date.
"""

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import MAXYEAR, MINYEAR, date
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Final, Literal, assert_never, get_args, overload

__all__ = [
    "Category",
    "DateExpr",
    "DateExprError",
    "DateExprRangeError",
    "DayName",
    "Edge",
    "EdgeName",
    "MonthDay",
    "MonthPeriod",
    "Today",
    "UnitName",
    "WeekDay",
    "WeekName",
    "eval_date_expr",
    "parse_date_expr",
    "print_date_expr",
]

WeekName = Literal["this_week", "next_week"]
# Monday first, the order of datetime.date.weekday().
DayName = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
UnitName = Literal["month", "quarter", "year"]
EdgeName = Literal["start", "end"]

_WEEKS: Final[tuple[WeekName, ...]] = get_args(WeekName)
_DAYS: Final[tuple[DayName, ...]] = get_args(DayName)
_UNITS: Final[tuple[UnitName, ...]] = get_args(UnitName)
_EDGES: Final[tuple[EdgeName, ...]] = get_args(EdgeName)


class Category(Enum):
    """The category of expression a caller requires (requirement 16.4)."""

    DATE = "date"
    PERIOD = "period"


# --- syntax trees ------------------------------------------------------------------------


def _check_int(field: str, value: object) -> None:
    # Exactly int: bool is an int subclass, but True has no text.
    if type(value) is not int:
        raise TypeError(f"{field} must be an int, got {type(value).__name__}")


def _check_name(field: str, value: object, names: tuple[str, ...]) -> None:
    if value not in names:
        raise ValueError(f"{field} must be one of {', '.join(names)}, got {value!r}")


@dataclass(frozen=True, slots=True)
class Today:
    """``today`` when days is 0, ``today+Nd`` when days is N > 0, ``today-Nd`` when -N < 0."""

    days: int

    def __post_init__(self) -> None:
        _check_int("days", self.days)


@dataclass(frozen=True, slots=True)
class WeekDay:
    """``this_week.W`` or ``next_week.W``."""

    week: WeekName
    day: DayName

    def __post_init__(self) -> None:
        _check_name("week", self.week, _WEEKS)
        _check_name("day", self.day, _DAYS)


@dataclass(frozen=True, slots=True)
class MonthDay:
    """``month(N).day(D)``, 1 <= D <= 31."""

    n: int
    day: int

    def __post_init__(self) -> None:
        _check_int("n", self.n)
        _check_int("day", self.day)
        if not 1 <= self.day <= 31:
            raise ValueError(f"day must be 1 to 31, got {self.day}")


@dataclass(frozen=True, slots=True)
class Edge:
    """``month(N)``, ``quarter(N)`` or ``year(N)`` followed by ``.start`` or ``.end``."""

    unit: UnitName
    n: int
    edge: EdgeName

    def __post_init__(self) -> None:
        _check_name("unit", self.unit, _UNITS)
        _check_int("n", self.n)
        _check_name("edge", self.edge, _EDGES)


@dataclass(frozen=True, slots=True)
class MonthPeriod:
    """``month(N)``, the only expression of the period category."""

    n: int

    def __post_init__(self) -> None:
        _check_int("n", self.n)


DateExpr = Today | WeekDay | MonthDay | Edge | MonthPeriod


class DateExprError(ValueError):
    """A text that is not a legal expression of the required category (requirement 16.6).

    ``position`` is a 1-based code point index: the length of the longest beginning of
    ``text`` that is still a prefix of some legal expression of the category, plus 1. It
    points at the first character that no legal expression allows there, or is
    ``len(text) + 1`` when the text ends early.
    """

    text: str
    position: int

    def __init__(self, text: str, position: int) -> None:
        super().__init__(text, position)
        self.text = text
        self.position = position

    def __str__(self) -> str:
        if self.position <= len(self.text):
            found = repr(self.text[self.position - 1])
        else:
            found = "end of text"
        return (
            f"invalid date expression {self.text!r}: unexpected {found} at position {self.position}"
        )


# --- parser ------------------------------------------------------------------------------

_DATE_WORDS: Final[tuple[Literal["today"] | WeekName | UnitName, ...]] = (
    "today",
    "this_week",
    "next_week",
    "month",
    "quarter",
    "year",
)
_MONTH_TAILS: Final[tuple[Literal["day"] | EdgeName, ...]] = ("day", "start", "end")
# ASCII only: str.isdigit() would also accept e.g. full-width digits.
_DIGITS: Final = "0123456789"
_NONZERO_DIGITS: Final = "123456789"


class _Reader:
    """A cursor over the text being parsed, advanced one code point at a time.

    Invariant: ``text[:pos]`` is a prefix of some legal expression of the category being
    parsed. A method consumes the next character only if the read part stays such a
    prefix; otherwise it raises DateExprError at ``pos + 1``. Each step below allows
    exactly the characters that can follow the read part in some legal expression, so the
    read part is then the longest beginning of the text that is a legal prefix, and
    ``pos + 1`` is the position requirement 16.6 defines (``len(text) + 1`` when the text
    ends early).
    """

    __slots__ = ("pos", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def error(self) -> DateExprError:
        return DateExprError(self.text, self.pos + 1)

    def at_end(self) -> bool:
        return self.pos == len(self.text)

    def accept(self, allowed: str) -> bool:
        """Consume the next character if it is one of ``allowed``."""
        if self.pos < len(self.text) and self.text[self.pos] in allowed:
            self.pos += 1
            return True
        return False

    def expect(self, allowed: str) -> str:
        """Consume and return the next character, which must be one of ``allowed``."""
        if not self.accept(allowed):
            raise self.error()
        return self.text[self.pos - 1]

    def skip(self, allowed: str) -> None:
        """Consume characters as long as they are in ``allowed``."""
        while self.accept(allowed):
            pass

    def word[W: str](self, words: tuple[W, ...]) -> W:
        """Consume one of ``words`` and return it; no word is a proper prefix of another."""
        start = self.pos
        while True:
            read = self.text[start : self.pos]
            for candidate in words:
                if candidate == read:
                    return candidate
            # read is a proper prefix of some words: allow the characters that follow it.
            self.expect("".join(w[len(read)] for w in words if w.startswith(read)))

    def days(self) -> int:
        """Consume N of ``today+Nd`` or ``today-Nd`` and the ``d``; return N."""
        start = self.pos
        self.expect(_NONZERO_DIGITS)
        self.skip(_DIGITS)
        n = _int_value(self.text[start : self.pos])
        self.expect("d")
        return n

    def offset(self) -> int:
        """Consume ``(N)`` of ``month(N)``, ``quarter(N)`` or ``year(N)``; return N."""
        self.expect("(")
        start = self.pos
        if not self.accept("0"):
            self.accept("-")
            self.expect(_NONZERO_DIGITS)
            self.skip(_DIGITS)
        n = _int_value(self.text[start : self.pos])
        self.expect(")")
        return n

    def day_of_month(self) -> int:
        """Consume ``(D)`` of ``day(D)``; return D, 1 to 31."""
        self.expect("(")
        start = self.pos
        first = self.expect(_NONZERO_DIGITS)
        if first in "12":
            self.accept(_DIGITS)
        elif first == "3":
            self.accept("01")
        day = int(self.text[start : self.pos])
        self.expect(")")
        return day


def parse_date_expr(text: str, category: Category) -> DateExpr:
    """Parse ``text`` as an expression of ``category`` (Date_Expression_Parser).

    Returns a MonthPeriod for the period category and one of the other syntax trees for
    the date category. Raises DateExprError, positioned as requirement 16.6 defines, when
    ``text`` is not a legal expression of ``category``.
    """
    reader = _Reader(text)
    expr: DateExpr
    match category:
        case Category.DATE:
            expr = _read_date(reader)
        case Category.PERIOD:
            reader.word(("month",))
            expr = MonthPeriod(reader.offset())
        case _:
            assert_never(category)
    if not reader.at_end():
        # The read part is a complete expression that no legal expression extends.
        raise reader.error()
    return expr


def _read_date(reader: _Reader) -> Today | WeekDay | MonthDay | Edge:
    word = reader.word(_DATE_WORDS)
    match word:
        case "today":
            if reader.at_end():
                return Today(0)
            sign = reader.expect("+-")
            days = reader.days()
            return Today(days if sign == "+" else -days)
        case "this_week" | "next_week":
            reader.expect(".")
            return WeekDay(word, reader.word(_DAYS))
        case "month":
            n = reader.offset()
            reader.expect(".")
            tail = reader.word(_MONTH_TAILS)
            match tail:
                case "day":
                    return MonthDay(n, reader.day_of_month())
                case "start" | "end":
                    return Edge("month", n, tail)
        case "quarter" | "year":
            n = reader.offset()
            reader.expect(".")
            return Edge(word, n, reader.word(_EDGES))


# --- printer -----------------------------------------------------------------------------


def print_date_expr(expr: DateExpr) -> str:
    """Return the text of ``expr`` (Date_Expression_Printer, requirement 16.7).

    The text is legal in the category of ``expr`` (period for MonthPeriod, date for the
    other trees), and ``parse_date_expr`` of it gives back ``expr``.
    """
    match expr:
        case Today(days=0):
            return "today"
        case Today(days=days):
            sign = "+" if days > 0 else "-"
            return f"today{sign}{_int_text(abs(days))}d"
        case WeekDay(week=week, day=day):
            return f"{week}.{day}"
        case MonthDay(n=n, day=day):
            return f"month({_int_text(n)}).day({day})"
        case Edge(unit=unit, n=n, edge=edge):
            return f"{unit}({_int_text(n)}).{edge}"
        case MonthPeriod(n=n):
            return f"month({_int_text(n)})"
        case _:
            assert_never(expr)


# --- evaluation --------------------------------------------------------------------------
# Days are counted as proleptic Gregorian ordinals (datetime.date.toordinal) and months as
# year * 12 + month - 1, so moving by N days or months adds N, and divmod by 12 turns a
# month count back into its year and month - 1. Python integers have no size limit, so
# offsets of any length stay exact (timedelta stops at 999999999 days), and a value
# outside the years 0001 to 9999 is detected before any date is built.

# Months in each kind of period. A period starts where the month count is a multiple of
# its length: quarters in January, April, July and October, years in January.
_PERIOD_MONTHS: Final[Mapping[UnitName, int]] = MappingProxyType(
    {"month": 1, "quarter": 3, "year": 12}
)
# Days from the Monday of the anchor's week to the Monday of each week.
_WEEK_STARTS: Final[Mapping[WeekName, int]] = MappingProxyType({"this_week": 0, "next_week": 7})
# Ordinals of 0001-01-01 and 9999-12-31.
_MIN_ORDINAL: Final = date.min.toordinal()
_MAX_ORDINAL: Final = date.max.toordinal()
# No month has more than 31 days, so day 31 of a month stands for its last day.
_LAST_DAY: Final = 31


class DateExprRangeError(ValueError):
    """The value of a legal expression at an anchor date is outside the years 0001 to 9999.

    ``text`` is the expression, as print_date_expr writes it, and ``anchor`` the anchor
    date it was evaluated at. Unlike DateExprError, nothing is wrong with the text itself.
    """

    text: str
    anchor: date

    def __init__(self, text: str, anchor: date) -> None:
        super().__init__(text, anchor)
        self.text = text
        self.anchor = anchor

    def __str__(self) -> str:
        return (
            f"date expression {self.text!r} at anchor date {self.anchor.isoformat()}"
            " falls outside the years 0001 to 9999"
        )


@overload
def eval_date_expr(expr: MonthPeriod, anchor: date) -> str: ...
@overload
def eval_date_expr(expr: Today | WeekDay | MonthDay | Edge, anchor: date) -> date: ...
def eval_date_expr(expr: DateExpr, anchor: date) -> date | str:
    """Return the value of ``expr`` at the anchor date ``anchor`` (Clock, requirement 16.5).

    A MonthPeriod gives the text ``YYYY-MM`` of its month, the other trees a date; the
    overloads give callers that know the kind of tree the precise type. Raises
    DateExprRangeError when the value is outside the years 0001 to 9999, however many
    digits N has.
    """
    this_month = anchor.year * 12 + anchor.month - 1
    value: date | str | None
    match expr:
        case Today(days=days):
            value = _from_ordinal(anchor.toordinal() + days)
        case WeekDay(week=week, day=day):
            monday = anchor.toordinal() - anchor.weekday()
            value = _from_ordinal(monday + _WEEK_STARTS[week] + _DAYS.index(day))
        case MonthDay(n=n, day=day):
            value = _day_of_month(this_month + n, day)
        case Edge(unit=unit, n=n, edge=edge):
            length = _PERIOD_MONTHS[unit]
            first = this_month - this_month % length + n * length
            if edge == "start":
                value = _day_of_month(first, 1)
            else:
                value = _day_of_month(first + length - 1, _LAST_DAY)
        case MonthPeriod(n=n):
            value = _month_text(this_month + n)
        case _:
            assert_never(expr)
    if value is None:
        raise DateExprRangeError(print_date_expr(expr), anchor)
    return value


def _from_ordinal(ordinal: int) -> date | None:
    """The date of ``ordinal``, None when it is outside 0001-01-01 to 9999-12-31."""
    if not _MIN_ORDINAL <= ordinal <= _MAX_ORDINAL:
        return None
    return date.fromordinal(ordinal)


def _year_month(month_count: int) -> tuple[int, int] | None:
    """Year and month (1 to 12) of ``month_count``, None outside the years 0001 to 9999."""
    year, month = divmod(month_count, 12)
    if not MINYEAR <= year <= MAXYEAR:
        return None
    return year, month + 1


def _day_of_month(month_count: int, day: int) -> date | None:
    """Day ``day`` of month ``month_count``, or its last day when the month has fewer days.

    None when the month is outside the years 0001 to 9999.
    """
    year_month = _year_month(month_count)
    if year_month is None:
        return None
    year, month = year_month
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _month_text(month_count: int) -> str | None:
    """``YYYY-MM`` of the month, None when it is outside the years 0001 to 9999."""
    year_month = _year_month(month_count)
    if year_month is None:
        return None
    year, month = year_month
    return f"{year:04d}-{month:02d}"


# --- integers of any length --------------------------------------------------------------
# int() and str() refuse decimal texts of more than sys.get_int_max_str_digits() digits
# (4300 by default), but N has no length limit. Decimal converts integers exactly in both
# directions without that limit, whatever the current decimal context.


def _int_value(digits: str) -> int:
    """Value of an ASCII decimal integer text such as ``-12``."""
    return int(Decimal(digits))


def _int_text(n: int) -> str:
    """Decimal text of ``n`` without leading zeros, ``-`` first when negative."""
    return str(Decimal(n))
