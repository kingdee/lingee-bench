"""Property 7: the canonical text of rule expressions and its round trip.

**Validates: Requirements 10.1, 10.3, 10.4**

Two properties of every syntax tree t of requirement 10.1:

- Round trip (requirement 10.4): parse_expr(print_expr(t)) is a tree equal to t, numbers
  compared by value, and printing that tree gives the same text again. The numbers drawn
  carry leading zeros, trailing zeros and an exponent ("007.50E+0" is 7.5), which the
  canonical text drops, so the trees are equal only when numbers compare by value.
- Canonical text (requirement 10.3): a variant text of t that differs from print_expr(t)
  only in the whitespace between tokens and in redundant parentheses parses to t again,
  so print_expr(parse_expr(variant)) is print_expr(t).

FORMS has one strategy per form of requirement 10.1, each drawing a tree with that form at
the root and subtrees of every form below: the number and string literals, the six field
path forms, the four functions, not, in, and each of the twelve binary operators. Every
form has its own run of 100 examples, so each run covers every form, which one run of 100
examples spread over all forms could miss. String literals are drawn from pieces that
favour the single quotes of requirement 10.4, and list literals, the one form that stands
only as the right operand of in, come with the in form.

variants() writes a tree as a list of tokens, parenthesizes every operand that is not
self-delimited, adds up to two more pairs of parentheses around any node and joins the
tokens with whitespace, which the parser ignores between tokens. It keeps at least one
whitespace character where two tokens would otherwise read as one.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Final, assert_never

import pytest
from hypothesis import given, note, settings
from hypothesis import strategies as st

from erp_mock.domain.rule_expr.nodes import (
    BINARY_OPS,
    RESERVED_WORDS,
    Binary,
    BinaryOp,
    Count,
    CtxPeriod,
    CtxToday,
    Exists,
    Expr,
    In,
    ListLit,
    Not,
    Num,
    Path,
    PathRoot,
    Str,
    Sum,
    SumOf,
)
from erp_mock.domain.rule_expr.parser import MAX_DEPTH, parse_expr
from erp_mock.domain.rule_expr.printer import print_expr

# --- names and literals ------------------------------------------------------------------

# Field, entity and bill names: names of the configuration's form, and the reserved words
# of requirement 10.1, which stand as names after ".", as the target of exists and sum_of
# and as the field of sum and sum_of (header.count, line.in, sum(lines.not)).
NAME_POOL: Final = (
    "a",
    "x1",
    "qty",
    "qty_base",
    "material",
    "kind",
    "tax_rate",
    "status",
    *sorted(RESERVED_WORDS),
)
NAMES: Final = st.sampled_from(NAME_POOL)
# The first segment of an unprefixed path, which no reserved word can start.
FREE_NAMES: Final = st.sampled_from([name for name in NAME_POOL if name not in RESERVED_WORDS])


@st.composite
def number_values(draw: st.DrawFn) -> Decimal:
    """Draw a finite, non-negative Decimal, written as the canonical text would not.

    The spelling carries leading zeros of the integer part, trailing zeros of the fraction
    and an exponent, all of which print_expr drops, so the round trip holds only when
    number literals compare by value (requirement 10.4). A zero may be written with a
    sign ("-0"), which Num accepts and print_expr writes as 0.
    """
    integer = draw(st.text("0123456789", min_size=1, max_size=8))
    fraction = draw(st.text("0123456789", max_size=8))
    exponent = draw(st.integers(min_value=-12, max_value=12))
    digits = f"{integer}.{fraction}" if fraction else integer
    value = Decimal(f"{digits}E{exponent:+d}")
    if value.is_zero() and draw(st.booleans()):
        return -value
    return value


# Pieces of string literals: the single quotes of requirement 10.4 above all, characters of
# the grammar, whitespace the parser skips between tokens, and characters outside the Basic
# Multilingual Plane. A literal may also be empty ("''").
STRING_PIECES: Final = (
    "'",
    "''",
    "it's",
    "a",
    "and",
    " or ",
    "0.13",
    "(",
    ")",
    "[",
    "]",
    ",",
    ".",
    " ",
    "\t",
    "\n",
    "\r",
    "\u3000",
    "甲",
    "\U0001f600",
)
STRING_VALUES: Final = st.one_of(
    st.lists(st.sampled_from(STRING_PIECES), max_size=5).map("".join),
    st.text(st.characters(codec="utf-8"), max_size=8),
)

NUMBERS: Final = st.builds(Num, number_values())
STRINGS: Final = st.builds(Str, STRING_VALUES)
LITERALS: Final[st.SearchStrategy[Num | Str]] = st.one_of(NUMBERS, STRINGS)
# A list literal has one or more literals, which need not be of one type.
LISTS: Final = st.builds(ListLit, st.lists(LITERALS, min_size=1, max_size=3).map(tuple))

# --- trees -------------------------------------------------------------------------------

# Names for the parametrized runs of the twelve binary operators.
OP_NAMES: Final[dict[BinaryOp, str]] = {
    "or": "or",
    "and": "and",
    "==": "eq",
    "!=": "ne",
    "<": "lt",
    "<=": "le",
    ">": "gt",
    ">=": "ge",
    "+": "add",
    "-": "sub",
    "*": "mul",
    "/": "div",
}

SEGMENTS: Final = st.lists(NAMES, min_size=1, max_size=3).map(tuple)


def _with_first(first: str, rest: Sequence[str]) -> tuple[str, ...]:
    return (first, *rest)


BARE_SEGMENTS: Final = st.builds(_with_first, FREE_NAMES, st.lists(NAMES, max_size=2))


def paths(
    root: PathRoot | None, segments: st.SearchStrategy[tuple[str, ...]]
) -> st.SearchStrategy[Expr]:
    """Field paths starting at ``root``, one segment per ref expansion."""
    return st.builds(Path, st.just(root), segments)


# The field path forms of requirement 10.1.
PATH_FORMS: Final[dict[str, st.SearchStrategy[Expr]]] = {
    "header": paths("header", SEGMENTS),
    "line": paths("line", SEGMENTS),
    "context_user": paths("context.user", SEGMENTS),
    "context_today": st.builds(CtxToday),
    "context_period": st.builds(CtxPeriod),
    "bare": paths(None, BARE_SEGMENTS),
}
SUMS: Final = st.builds(Sum, NAMES)
COUNTS: Final = st.builds(Count)

# Every node that writes a self-delimited piece of text: a literal, a path or a call that
# takes no subexpression.
ATOMS: Final[st.SearchStrategy[Expr]] = st.one_of(
    NUMBERS, STRINGS, *PATH_FORMS.values(), SUMS, COUNTS
)


def branches(child: st.SearchStrategy[Expr]) -> dict[str, st.SearchStrategy[Expr]]:
    """One strategy per operator and per function that takes a condition, over ``child``."""
    forms: dict[str, st.SearchStrategy[Expr]] = {
        "not": st.builds(Not, child),
        "in": st.builds(In, child, LISTS),
        "exists": st.builds(Exists, NAMES, child),
        "sum_of": st.builds(SumOf, NAMES, NAMES, child),
    }
    for op in BINARY_OPS:
        forms[OP_NAMES[op]] = st.builds(Binary, st.just(op), child, child)
    return forms


def expressions(depth: int) -> st.SearchStrategy[Expr]:
    """Trees of at most ``depth`` levels, of every form of requirement 10.1."""
    if depth <= 1:
        return ATOMS
    return st.one_of(ATOMS, *branches(expressions(depth - 1)).values())


TREE_DEPTH: Final = 4
# A level of a tree adds at most four levels of nesting to a variant text: the parentheses
# of an operand, up to two redundant pairs, and the level the parser counts for not, exists
# and sum_of. The deepest variant text stays well inside the parser's limit.
assert TREE_DEPTH * 4 <= MAX_DEPTH

# One strategy per form of requirement 10.1, each with that form at the root.
FORMS: Final[dict[str, st.SearchStrategy[Expr]]] = {
    "number": NUMBERS,
    "string": STRINGS,
    **PATH_FORMS,
    "sum": SUMS,
    "count": COUNTS,
    **branches(expressions(TREE_DEPTH - 1)),
}

# --- variant texts -----------------------------------------------------------------------

# The whitespace the parser skips between tokens.
GAPS: Final = st.text(" \t\n\r", max_size=2)
NEEDED_GAPS: Final = st.text(" \t\n\r", min_size=1, max_size=2)
# Pairs of redundant parentheses around a node.
EXTRA_PARENTHESES: Final = st.integers(min_value=0, max_value=2)

WORD_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def needs_gap(left: str, right: str) -> bool:
    """Whether two neighbouring tokens must be separated by whitespace.

    Two words, or a number and a word, would read as one token ("nota", "1and"), and the
    closing quote of a string literal followed by the opening quote of the next would read
    as one doubled quote inside one literal.
    """
    return (left[-1] in WORD_CHARS and right[0] in WORD_CHARS) or (
        left.endswith("'") and right.startswith("'")
    )


def needs_parentheses(expr: Expr) -> bool:
    """Whether ``expr`` must be parenthesized to stand as an operand of another node."""
    return isinstance(expr, Not | Binary | In)


def dotted(names: Sequence[str]) -> list[str]:
    """The tokens of ``names`` joined by ".", each name and each "." a token of its own."""
    tokens = [names[0]]
    for name in names[1:]:
        tokens += [".", name]
    return tokens


def list_tokens(literals: ListLit) -> list[str]:
    """The tokens of a list literal, which stands only as the right operand of in."""
    tokens = ["["]
    for index, item in enumerate(literals.items):
        if index:
            tokens.append(",")
        tokens.append(print_expr(item))
    return [*tokens, "]"]


def tokens(draw: st.DrawFn, expr: Expr, *, parenthesize: bool) -> list[str]:
    """The tokens of ``expr``, in parentheses when its position or the draw asks for them."""
    inner = bare_tokens(draw, expr)
    pairs = draw(EXTRA_PARENTHESES)
    if parenthesize and needs_parentheses(expr):
        pairs += 1
    return ["("] * pairs + inner + [")"] * pairs


def bare_tokens(draw: st.DrawFn, expr: Expr) -> list[str]:
    """The tokens of ``expr`` itself, its operands parenthesized where the grammar needs it."""
    match expr:
        case Num() | Str():
            return [print_expr(expr)]
        case Path(root=root, segments=segments):
            prefix = () if root is None else root.split(".")
            return dotted([*prefix, *segments])
        case CtxToday():
            return dotted(["context", "today"])
        case CtxPeriod():
            return dotted(["context", "period"])
        case Sum(field=field):
            return ["sum", "(", *dotted(["lines", field]), ")"]
        case Count():
            return ["count", "(", "lines", ")"]
        case Exists(target=target, cond=cond):
            return ["exists", "(", target, ",", *tokens(draw, cond, parenthesize=False), ")"]
        case SumOf(target=target, field=field, cond=cond):
            condition = tokens(draw, cond, parenthesize=False)
            return ["sum_of", "(", *dotted([target, field]), ",", *condition, ")"]
        case Not(operand=operand):
            return ["not", *tokens(draw, operand, parenthesize=True)]
        case Binary(op=op, left=left, right=right):
            left_tokens = tokens(draw, left, parenthesize=True)
            return [*left_tokens, op, *tokens(draw, right, parenthesize=True)]
        case In(left=left, right=right):
            return [*tokens(draw, left, parenthesize=True), "in", *list_tokens(right)]
        case _:
            assert_never(expr)


@st.composite
def variants(draw: st.DrawFn, expr: Expr) -> str:
    """Draw a text of ``expr`` with added whitespace and redundant parentheses.

    Whitespace also goes before the first and after the last token. Parentheses that the
    canonical text does not have are redundant: they enclose exactly the text of one node,
    which the operator precedence groups the same way without them.
    """
    written = tokens(draw, expr, parenthesize=False)
    parts = [draw(GAPS)]
    for index, token in enumerate(written):
        if index:
            parts.append(draw(NEEDED_GAPS if needs_gap(written[index - 1], token) else GAPS))
        parts.append(token)
    return "".join([*parts, draw(GAPS)])


# --- properties --------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 7: 规则表达式的规范输出与往返
@pytest.mark.req("10.1", "10.3", "10.4")
@pytest.mark.parametrize("form", list(FORMS))
@settings(max_examples=100)
@given(data=st.data())
def test_canonical_text_parses_back_to_the_tree(form: str, data: st.DataObject) -> None:
    tree = data.draw(FORMS[form], label="tree")
    text = print_expr(tree)
    note(f"canonical text: {text!r}")
    parsed = parse_expr(text)
    assert parsed == tree
    assert print_expr(parsed) == text


# Feature: erp-mock-mcp-m0, Property 7: 规则表达式的规范输出与往返
@pytest.mark.req("10.1", "10.3")
@pytest.mark.parametrize("form", list(FORMS))
@settings(max_examples=100)
@given(data=st.data())
def test_whitespace_and_redundant_parentheses_print_the_same(
    form: str, data: st.DataObject
) -> None:
    tree = data.draw(FORMS[form], label="tree")
    variant = data.draw(variants(tree), label="variant")
    note(f"variant: {variant!r}")
    assert parse_expr(variant) == tree
    assert print_expr(parse_expr(variant)) == print_expr(tree)
