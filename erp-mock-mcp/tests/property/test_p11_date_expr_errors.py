"""Property 11: the error position of date expressions.

**Validates: Requirements 16.6**

When a text is not a legal expression of the required category, parse_date_expr raises
DateExprError with the text and a position: the length of the longest beginning of the
text that is still a prefix of some legal expression of the category, plus 1
(requirement 16.6). reference_position finds that length by checking every beginning of
the text, from the empty one to the whole text, with is_legal_prefix of
tests/property/models/date_prefix.py, a prefix judge written independently of
erp_mock.common.date_expr. Its is_legal tells the texts that are legal, which must parse
without error.

Most texts are legal expressions of either category edited one to three times: a
character replaced, inserted or deleted, characters upper-cased, whitespace inserted, the
text cut short or extended, a 0 put before a number or in its place, or D moved out of 1
to 31. The others are the beginning of a form followed by pieces of legal expressions
joined at random, or short random texts.
"""

import re
from collections.abc import Callable
from typing import Final

import pytest
from hypothesis import example, given, note, settings
from hypothesis import strategies as st

from erp_mock.common.date_expr import Category, DateExprError, parse_date_expr
from tests.property.models.date_prefix import is_legal, is_legal_prefix

DATE = Category.DATE
PERIOD = Category.PERIOD
OTHER_CATEGORY: Final = {DATE: PERIOD, PERIOD: DATE}

# --- reference ---------------------------------------------------------------------------


def reference_position(text: str, category: Category) -> int:
    """Return the length of the longest beginning of ``text`` that is a legal prefix, plus 1.

    Every beginning is checked, from the empty one, which is always a legal prefix, to
    ``text`` itself.
    """
    return 1 + max(k for k in range(len(text) + 1) if is_legal_prefix(text[:k], category))


# --- generators --------------------------------------------------------------------------

WEEKS: Final = ("this_week", "next_week")
DAYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
UNITS: Final = ("month", "quarter", "year")
EDGES: Final = ("start", "end")
WORDS: Final = ("today", *WEEKS, *DAYS, *UNITS, "day", *EDGES)

# N of today+Nd and today-Nd: often one or two digits, up to 31 digits.
POSITIVE_TEXTS: Final = st.one_of(
    st.integers(min_value=1, max_value=99), st.integers(min_value=1, max_value=10**30)
).map(str)
# Every other N.
INTEGER_TEXTS: Final = st.one_of(st.just("0"), POSITIVE_TEXTS, POSITIVE_TEXTS.map("-{}".format))
DAY_TEXTS: Final = st.integers(min_value=1, max_value=31).map(str)

# The legal expressions of each category, form by form (requirement 16.4). month(N).day(D),
# the form with the most to get wrong, comes first: Hypothesis favours the first option.
LEGAL_TEXTS: Final[dict[Category, st.SearchStrategy[str]]] = {
    DATE: st.one_of(
        st.builds("month({}).day({})".format, INTEGER_TEXTS, DAY_TEXTS),
        st.builds(
            "{}({}).{}".format, st.sampled_from(UNITS), INTEGER_TEXTS, st.sampled_from(EDGES)
        ),
        st.builds("{}.{}".format, st.sampled_from(WEEKS), st.sampled_from(DAYS)),
        st.builds("today{}{}d".format, st.sampled_from("+-"), POSITIVE_TEXTS),
        st.just("today"),
    ),
    PERIOD: INTEGER_TEXTS.map("month({})".format),
}

# The characters legal expressions are made of.
GRAMMAR_CHARS: Final = "".join(sorted(set("".join(WORDS) + "()+-.0123456789")))
# Characters of no legal expression: upper case letters, whitespace, and look-alikes of
# digits, signs, brackets and letters, some of which str.isdigit() or case-insensitive
# matching would take for legal ones.
FOREIGN_CHARS: Final = (
    "DMT"
    " \t\n\u00a0\u3000"  # with a no-break space and an ideographic space
    "\uff10\uff11\u0663\U0001d7ce"  # full-width 0 and 1, Arabic-Indic 3, mathematical bold 0
    "\u2212\uff08\uff09\uff0e"  # minus sign, full-width ( ) and .
    "\u212a\u017f\u0131"  # Kelvin sign, long s and dotless i
    "\U0001f600"  # outside the Basic Multilingual Plane
)
CHARS: Final = st.one_of(
    st.sampled_from(GRAMMAR_CHARS), st.sampled_from(FOREIGN_CHARS), st.characters(codec="utf-8")
)
WHITESPACE: Final = st.sampled_from(" \t\n\r\u00a0\u2028\u3000")
# Pieces of legal expressions, joined at random or appended to a text, and the beginnings
# of the forms of each category that joined pieces start with.
PIECES: Final = st.sampled_from(
    [*WORDS, *"( ) . _ + - d 0 1 3 12 31 32 -1 (0) ). .day( .start .end week".split()]
)
LEADS: Final = {
    DATE: ("today", "this_week.", "next_week.", "month(", "quarter(", "year("),
    PERIOD: ("month(",),
}
# D outside 1 to 31: two digits from 32 to 99 first, which go wrong after 3 to 9; then
# three digits, a leading zero, or no number at all.
BAD_DAYS: Final = st.one_of(
    st.integers(min_value=32, max_value=99).map(str),
    st.integers(min_value=100, max_value=999).map(str),
    st.integers(min_value=0, max_value=31).map("0{}".format),
    st.sampled_from(["0", "", "-1", "+1"]),
)

# An edit draws what it needs and returns the edited text.
Edit = Callable[[st.DrawFn, str], str]


def index_from_end(draw: st.DrawFn, last: int) -> int:
    """Draw an index from 0 to ``last``, counted down from ``last``.

    Hypothesis draws the lower bound of an integer range far more often than any other
    value. Counted from the end, the edits it favours are those near the end of a text,
    which leave long legal beginnings, rather than those at position 1.
    """
    return last - draw(st.integers(min_value=0, max_value=last))


def replace_char(draw: st.DrawFn, text: str) -> str:
    """Replace one character; an empty text gets one instead."""
    if not text:
        return draw(CHARS)
    i = index_from_end(draw, len(text) - 1)
    return text[:i] + draw(CHARS) + text[i + 1 :]


def insert_char(draw: st.DrawFn, text: str) -> str:
    i = index_from_end(draw, len(text))
    return text[:i] + draw(CHARS) + text[i:]


def delete_char(draw: st.DrawFn, text: str) -> str:
    if not text:
        return text
    i = index_from_end(draw, len(text) - 1)
    return text[:i] + text[i + 1 :]


def upper_case(draw: st.DrawFn, text: str) -> str:
    """Upper-case a run of one or more characters, up to the whole text."""
    if not text:
        return text
    i = index_from_end(draw, len(text) - 1)
    j = draw(st.integers(min_value=i + 1, max_value=len(text)))
    return text[:i] + text[i:j].upper() + text[j:]


def insert_whitespace(draw: st.DrawFn, text: str) -> str:
    i = index_from_end(draw, len(text))
    return text[:i] + draw(WHITESPACE) + text[i:]


def cut_short(draw: st.DrawFn, text: str) -> str:
    """Keep a proper beginning of the text."""
    if not text:
        return text
    return text[: index_from_end(draw, len(text) - 1)]


def extend(draw: st.DrawFn, text: str) -> str:
    """Append a character or a piece of a legal expression."""
    return text + draw(st.one_of(CHARS, PIECES))


def zero_number(draw: st.DrawFn, text: str) -> str:
    """Put a 0 before a run of digits or in its place: month(05), today+0d, month(-0)."""
    runs = [match.span() for match in re.finditer("[0-9]+", text)]
    if not runs:
        return text
    start, end = draw(st.sampled_from(runs))
    rest = text[end:] if draw(st.booleans()) else text[start:]
    return text[:start] + "0" + rest


def bad_day(draw: st.DrawFn, text: str) -> str:
    """Replace D of day(D), if the text has one, by a value from BAD_DAYS."""
    match = re.search(r"(?<=day\()[0-9]+", text)
    if match is None:
        return text
    return text[: match.start()] + draw(BAD_DAYS) + text[match.end() :]


# bad_day comes first, for the same reason as month(N).day(D) in LEGAL_TEXTS.
EDITS: Final[tuple[Edit, ...]] = (
    bad_day,
    zero_number,
    replace_char,
    insert_char,
    delete_char,
    upper_case,
    insert_whitespace,
    cut_short,
    extend,
)


@st.composite
def edited_texts(draw: st.DrawFn, category: Category) -> str:
    """Draw a legal expression of ``category`` and edit it one to three times.

    Some edits leave the text legal: a digit replaced by another one, an edit of D in a
    text without D, a case change of digits and punctuation.
    """
    text = draw(LEGAL_TEXTS[category])
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        edit = draw(st.sampled_from(EDITS))
        text = edit(draw, text)
    return text


@st.composite
def joined_pieces(draw: st.DrawFn, category: Category) -> str:
    """Draw the beginning of a form of ``category`` followed by up to five pieces."""
    lead = draw(st.sampled_from(LEADS[category]))
    return lead + "".join(draw(st.lists(PIECES, max_size=5)))


@st.composite
def cases(draw: st.DrawFn) -> tuple[str, Category]:
    """Draw a text and the category to parse it as.

    Most texts are edited legal expressions, some of them of the other category:
    month(N) begins date expressions such as month(N).end, and date expressions that
    begin with month(N) go on after it. The others are joined pieces of legal
    expressions, or random texts of up to eight characters.
    """
    category = draw(st.sampled_from(Category))
    roll = draw(st.integers(min_value=0, max_value=9))
    if roll < 7:
        source = draw(st.sampled_from([category, category, category, OTHER_CATEGORY[category]]))
        text = draw(edited_texts(source))
    elif roll < 9:
        text = draw(joined_pieces(category))
    else:
        text = draw(st.text(CHARS, max_size=8))
    return text, category


# Digits to put in place of a number: none to three, so that each first digit meets each
# second one (05, 32, 45, 99) and three digits (100, 310) come up as well.
DIGIT_TEXTS: Final = st.text("0123456789", max_size=3)


@st.composite
def renumbered_cases(draw: st.DrawFn) -> tuple[str, Category]:
    """Draw a category and a legal expression of it with one number replaced by DIGIT_TEXTS.

    The last number is the one most often replaced: D in month(N).day(D), otherwise the
    only N. Texts without numbers stay legal.
    """
    category = draw(st.sampled_from(Category))
    text = draw(LEGAL_TEXTS[category])
    runs = [match.span() for match in re.finditer("[0-9]+", text)]
    if runs:
        start, end = draw(st.sampled_from(runs[::-1]))
        text = text[:start] + draw(DIGIT_TEXTS) + text[end:]
    return text, category


# --- property ----------------------------------------------------------------------------


def check_error_position(text: str, category: Category) -> None:
    """Parse ``text``: a legal text must parse, any other must fail at reference_position."""
    if is_legal(text, category):
        parse_date_expr(text, category)
        return
    expected = reference_position(text, category)
    note(f"longest legal prefix: {text[: expected - 1]!r}")
    with pytest.raises(DateExprError) as info:
        parse_date_expr(text, category)
    assert info.value.text == text
    assert info.value.position == expected


# Feature: erp-mock-mcp-m0, Property 11: 日期表达式的错误位置
@pytest.mark.req("16.6")
@settings(max_examples=100)
@given(case=cases())
@example(case=("Today", DATE))  # 1: nothing is case-folded
@example(case=("today+0d", DATE))  # 7: N of today+Nd is positive
@example(case=("today + 3d", DATE))  # 6: no whitespace
@example(case=("month(-1).day(32)", DATE))  # 16: D is 1 to 31
@example(case=("month(0)", DATE))  # 9: a date needs more, the text ends early
@example(case=("month(0).end", PERIOD))  # 9: a period is month(N) alone
@example(case=("month(-12)", PERIOD))  # legal
def test_error_position_is_one_past_the_longest_legal_prefix(case: tuple[str, Category]) -> None:
    check_error_position(*case)


# Feature: erp-mock-mcp-m0, Property 11: 日期表达式的错误位置
@pytest.mark.req("16.6")
@settings(max_examples=100)
@given(case=renumbered_cases())
@example(case=("month(0).day(310)", DATE))  # 16: "month(0).day(31" is a legal prefix
@example(case=("month(-05).end", DATE))  # 8: no leading zeros
def test_error_position_in_numbers(case: tuple[str, Category]) -> None:
    check_error_position(*case)
