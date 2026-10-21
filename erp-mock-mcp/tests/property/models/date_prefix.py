"""Reference prefix judge for date expressions (property 11, requirement 16.6).

Written independently of erp_mock.common.date_expr, whose parser reads a text one
character at a time. Here the legal expressions of each category (requirement 16.4) are
described form by form with a few combinators. Each combinator builds two regular
expressions, both used with fullmatch: ``full`` matches the texts of its language and
``prefix`` the beginnings of those texts, which follow from these rules:

- the beginnings of a literal text are its first k characters, for k = 0 to its length;
- a beginning of a text of A or B is a beginning of a text of A or of a text of B;
- a beginning of a text of A1 A2 ... An is a text of A1 A2 ... Ak followed by a beginning
  of a text of A(k+1), for some k = 0 to n - 1;
- a beginning of zero or more texts of A, one after another, is zero or more texts of A
  followed by a beginning of a text of A.

The last two rules need every language to have at least one text, which holds for each
language below. So ``is_legal_prefix(text, category)`` holds exactly when some legal
expression of the category begins with ``text``. The patterns take no flags and name
every character they accept, without character classes: nothing is case-folded, and only
ASCII digits are digits.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from erp_mock.common.date_expr import Category

__all__ = ["is_legal", "is_legal_prefix"]


@dataclass(frozen=True, slots=True)
class Language:
    """A set of texts: ``full`` matches its texts and ``prefix`` their beginnings."""

    full: str
    prefix: str


def _alternatives(patterns: Iterable[str]) -> str:
    return "(?:" + "|".join(patterns) + ")"


def literal(text: str) -> Language:
    """The text ``text`` alone."""
    beginnings = (re.escape(text[:k]) for k in range(len(text) + 1))
    return Language(re.escape(text), _alternatives(beginnings))


def either(*options: Language) -> Language:
    """The texts of any one of ``options``."""
    return Language(
        _alternatives(option.full for option in options),
        _alternatives(option.prefix for option in options),
    )


def one_of(chars: str) -> Language:
    """Any one character of ``chars``."""
    return either(*(literal(char) for char in chars))


def sequence(*parts: Language) -> Language:
    """A text of each of ``parts``, one after another."""
    fulls = [part.full for part in parts]
    return Language(
        "".join(fulls),
        _alternatives("".join(fulls[:k]) + part.prefix for k, part in enumerate(parts)),
    )


def repeated(part: Language) -> Language:
    """Zero or more texts of ``part``, one after another."""
    many = _alternatives([part.full]) + "*"
    return Language(many, many + part.prefix)


DIGIT: Final = one_of("0123456789")
NONZERO_DIGIT: Final = one_of("123456789")
# N of today+Nd and today-Nd: a positive integer without leading zeros.
POSITIVE: Final = sequence(NONZERO_DIGIT, repeated(DIGIT))
# Every other N: 0, or a nonzero integer without leading zeros, optionally after a "-".
INTEGER: Final = either(literal("0"), POSITIVE, sequence(literal("-"), POSITIVE))
# D: 1 to 31 without leading zeros.
DAY: Final = either(
    NONZERO_DIGIT, sequence(one_of("12"), DIGIT), sequence(literal("3"), one_of("01"))
)
# W: the days of the week.
WEEKDAY: Final = either(*map(literal, ("mon", "tue", "wed", "thu", "fri", "sat", "sun")))

# The forms of requirement 16.4, in its order.
LANGUAGES: Final[dict[Category, Language]] = {
    Category.DATE: either(
        literal("today"),
        sequence(literal("today+"), POSITIVE, literal("d")),
        sequence(literal("today-"), POSITIVE, literal("d")),
        sequence(literal("this_week."), WEEKDAY),
        sequence(literal("next_week."), WEEKDAY),
        sequence(literal("month("), INTEGER, literal(").day("), DAY, literal(")")),
        sequence(literal("month("), INTEGER, literal(").start")),
        sequence(literal("month("), INTEGER, literal(").end")),
        sequence(literal("quarter("), INTEGER, literal(").start")),
        sequence(literal("quarter("), INTEGER, literal(").end")),
        sequence(literal("year("), INTEGER, literal(").start")),
        sequence(literal("year("), INTEGER, literal(").end")),
    ),
    Category.PERIOD: sequence(literal("month("), INTEGER, literal(")")),
}
_FULL: Final = {category: re.compile(language.full) for category, language in LANGUAGES.items()}
_PREFIX: Final = {category: re.compile(language.prefix) for category, language in LANGUAGES.items()}


def is_legal(text: str, category: Category) -> bool:
    """Whether ``text`` is a legal expression of ``category``."""
    return _FULL[category].fullmatch(text) is not None


def is_legal_prefix(text: str, category: Category) -> bool:
    """Whether some legal expression of ``category`` begins with ``text``.

    The empty text is a legal prefix, and so is every legal expression.
    """
    return _PREFIX[category].fullmatch(text) is not None
