"""Scopes, well-typed trees and evaluation environments of the rule expression properties.

Properties 8 and 9 both need the same three draws, so they share this generator: a random
scope, a well-typed tree in that scope and an env that matches it. Only the assertions
differ, the class of the tree against the checker for property 8 and the value against a
reference evaluator for property 9.

:func:`scopes` draws the scope: the fields of the header, of the lines and of the current
user, the fields of each entity an exists or sum_of may name, and which of the three
reaches of requirement 10.2 the scope has (a rule on a bill or a query over its lines, a
query over bill headers, a query over an entity). Each plain field gets a drawn class and
each ref field a drawn entity, so a drawn path may expand up to :data:`EXPANSIONS` refs.

:func:`typed` builds a tree of a requested class from what the scope offers: every path of
that class, the literals and context values of that class, and the operators and functions
whose result is of that class, their operands drawn by the same rules. Number and string
leaves are always available, so the comparisons are too and every class is reachable in
every scope, with or without a field of it.

:func:`envs` draws the env from the same scope: a record per level, the lines of the bill,
a table per entity and an anchor date, where every field holds a value of the class the
scope declares for it, or the null value.

:class:`FakeScope`, :class:`FakeRecord` and :class:`FakeEnv` are the stand-ins the draws
build: an ExprScope for the checker, and a Record and an EvalEnv for the evaluator.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from hypothesis import strategies as st

from erp_mock.domain.rule_expr.checker import ExprType, FieldInfo
from erp_mock.domain.rule_expr.evaluator import Value
from erp_mock.domain.rule_expr.nodes import (
    BINARY_OPS,
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
from erp_mock.domain.rule_expr.parser import MAX_DEPTH

NUM: Final = ExprType.NUM
STR: Final = ExprType.STR
DATE: Final = ExprType.DATE
PERIOD: Final = ExprType.PERIOD
BOOL: Final = ExprType.BOOL

# The fields of one record, as a scope resolves them.
Fields = Mapping[str, FieldInfo]

ID_SUFFIX: Final = "_id"

# Field names: plain fields, whose class is drawn, and ref fields, whose entity is drawn.
# No name is a reserved word, so each may also start an unprefixed path in a condition.
PLAIN_FIELDS: Final = ("a", "b", "c", "d")
REF_FIELDS: Final = ("ref_id", "other_id")
# The entities an exists or sum_of may name and a ref field may point at.
TARGETS: Final = ("supplier", "material")

# Ref expansions in a drawn path, so a path has at most EXPANSIONS + 1 segments, and the
# levels of referenced records a drawn record carries, enough for every path to resolve.
EXPANSIONS: Final = 2

# Levels of a drawn tree: a node of every form, its operands and their leaves. The text of
# such a tree stays far inside the parser's nesting limit, so a tree noted below parses.
TREE_DEPTH: Final = 3
assert TREE_DEPTH <= MAX_DEPTH

# --- values ------------------------------------------------------------------------------

# Numbers of a few magnitudes, with at most four decimals, so arithmetic at any depth stays
# far inside RULE_CTX's exponent range; 0 is drawn too, as the divisor that gives null. A
# number literal carries no sign (requirement 10.1), so only a field holds a negative one.
LITERAL_NUMBERS: Final = st.one_of(
    st.sampled_from(
        [Decimal(0), Decimal(1), Decimal("0.0001"), Decimal(10**12), Decimal("0." + "1" * 28)]
    ),
    st.decimals(min_value=0, max_value=10**6, places=4, allow_nan=False, allow_infinity=False),
)
FIELD_NUMBERS: Final = st.one_of(LITERAL_NUMBERS, LITERAL_NUMBERS.map(lambda value: -value))

STRING_VALUES: Final = st.one_of(
    st.sampled_from(["", "a", "A", "it's", "0.1", "2026-03", "甲"]), st.text(max_size=4)
)

DATE_VALUES: Final = st.dates()


def maybe_null(values: st.SearchStrategy[Value]) -> st.SearchStrategy[Value]:
    """``values``, and the null value one draw in four (requirement 10.6).

    Any field may be null, but a comparison of two paths is unknown as soon as one side
    is, and so is everything above it: drawing null half the time would leave the
    operations on values that are not null (requirement 10.12) to a quarter of the
    comparisons. Repeating ``values`` weights the one_of, which draws evenly among its
    branches.
    """
    return st.one_of(st.none(), values, values, values)


def period_text(day: date) -> str:
    """The ``YYYY-MM`` of the month of ``day``: what a period field and context.period hold."""
    return f"{day.year:04d}-{day.month:02d}"


# The values a field of each class may hold, null aside (requirement 10.6).
FIELD_VALUES: Final[Mapping[ExprType, st.SearchStrategy[Value]]] = {
    NUM: FIELD_NUMBERS,
    STR: STRING_VALUES,
    DATE: DATE_VALUES,
    PERIOD: DATE_VALUES.map(period_text),
    BOOL: st.booleans(),
}

NUMBERS: Final = st.builds(Num, LITERAL_NUMBERS)
STRINGS: Final = st.builds(Str, STRING_VALUES)
# The literals a list may hold; in takes a number or a string on the left (requirement
# 10.11), and every item is then of that class.
LIST_ITEMS: Final[Mapping[ExprType, st.SearchStrategy[Num | Str]]] = {NUM: NUMBERS, STR: STRINGS}

# --- scopes ------------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeScope:
    """A drawn ExprScope: the fields of each level and of each entity it knows."""

    header: Fields
    line: Fields
    user: Fields
    targets: Mapping[str, Fields]
    has_current_line: bool
    has_bill_lines: bool

    def header_field(self, name: str) -> FieldInfo | None:
        return self.header.get(name)

    def line_field(self, name: str) -> FieldInfo | None:
        return self.line.get(name)

    def user_field(self, name: str) -> FieldInfo | None:
        return self.user.get(name)

    def has_target(self, target: str) -> bool:
        return target in self.targets

    def target_field(self, target: str, name: str) -> FieldInfo | None:
        return self.targets.get(target, {}).get(name)


# What a scope reaches (requirement 10.2), as (has_current_line, has_bill_lines): a rule on
# a bill and a query over a bill's lines have a current line and the bill's lines, a query
# over bill headers only the lines of the record it reads, a query over an entity neither.
REACHES: Final[tuple[tuple[bool, bool], ...]] = ((True, True), (False, True), (False, False))


@st.composite
def record_fields(draw: st.DrawFn) -> Fields:
    """Draw the fields of one record: plain fields of a drawn class and ref fields.

    Each class is drawn on its own, so a record may hold two fields of one class, which
    two paths of a comparison may read, or none of a class, which leaves the paths of that
    class out of the trees drawn in the record; a record may also have no field at all.
    """
    plain = draw(st.lists(st.sampled_from(PLAIN_FIELDS), unique=True, max_size=len(PLAIN_FIELDS)))
    found = {name: FieldInfo(draw(st.sampled_from(ExprType))) for name in plain}
    refs = draw(st.lists(st.sampled_from(REF_FIELDS), unique=True, max_size=len(REF_FIELDS)))
    for name in refs:
        # A ref field is of the string class and points at an entity the scope knows.
        found[name] = FieldInfo(STR, draw(st.sampled_from(TARGETS)))
    return found


@st.composite
def scopes(draw: st.DrawFn, reach: tuple[bool, bool] | None = None) -> FakeScope:
    """Draw the scope one tree is checked in.

    ``reach`` fixes what the scope reaches, one of :data:`REACHES`; it is drawn when the
    caller asks for no particular one. A test that needs trees with a ``line.`` path draws
    the reach that has a current line, since only such a scope resolves one.
    """
    has_current_line, has_bill_lines = (
        reach if reach is not None else draw(st.sampled_from(REACHES))
    )
    return FakeScope(
        header=draw(record_fields()),
        line=draw(record_fields()),
        user=draw(record_fields()),
        targets={target: draw(record_fields()) for target in TARGETS},
        has_current_line=has_current_line,
        has_bill_lines=has_bill_lines,
    )


@dataclass(frozen=True)
class Place:
    """Where a tree stands: its scope and the target of the condition that encloses it.

    ``condition`` is the entity whose fields an unprefixed path reads, and None outside an
    exists or sum_of condition, where no unprefixed path resolves.
    """

    scope: FakeScope
    condition: str | None = None


def field_paths(
    scope: FakeScope, fields: Fields, expansions: int
) -> dict[ExprType, list[tuple[str, ...]]]:
    """The segments that resolve in ``fields``, by class, expanding at most ``expansions`` refs.

    A ref field gives both a path of its own, of the string class, and the paths of the
    record it refers to, under the segment that names it without its ``_id`` suffix.
    """
    found: dict[ExprType, list[tuple[str, ...]]] = {type_: [] for type_ in ExprType}
    for name, info in fields.items():
        found[info.type].append((name,))
        if info.ref is None or expansions == 0:
            continue
        segment = name.removesuffix(ID_SUFFIX)
        expanded = field_paths(scope, scope.targets[info.ref], expansions - 1)
        for type_, tails in expanded.items():
            found[type_].extend((segment, *tail) for tail in tails)
    return found


def roots(place: Place) -> list[tuple[PathRoot | None, Fields]]:
    """The record each path root reads in ``place``; only the roots that resolve there."""
    scope = place.scope
    found: list[tuple[PathRoot | None, Fields]] = [("header", scope.header)]
    if scope.has_current_line:
        found.append(("line", scope.line))
    found.append(("context.user", scope.user))
    if place.condition is not None:
        found.append((None, scope.targets[place.condition]))
    return found


# --- trees -------------------------------------------------------------------------------

# The operand classes of each operator (requirement 10.11): == and != take two operands of
# one class, <, <=, > and >= two of one class but boolean, and or take booleans, the
# arithmetic operators numbers, and in a number or a string with items of that class.
EQUAL_OPS: Final[tuple[BinaryOp, ...]] = ("==", "!=")
ORDER_OPS: Final[tuple[BinaryOp, ...]] = ("<", "<=", ">", ">=")
LOGICAL_OPS: Final[tuple[BinaryOp, ...]] = ("and", "or")
ARITHMETIC_OPS: Final[tuple[BinaryOp, ...]] = ("+", "-", "*", "/")
assert set(EQUAL_OPS + ORDER_OPS + LOGICAL_OPS + ARITHMETIC_OPS) == set(BINARY_OPS)

ORDERED_TYPES: Final[tuple[ExprType, ...]] = (NUM, STR, DATE, PERIOD)
MEMBER_TYPES: Final[tuple[ExprType, ...]] = (NUM, STR)


def typed(place: Place, type_: ExprType, depth: int) -> st.SearchStrategy[Expr]:
    """Well-typed trees of class ``type_`` in ``place``, at most ``depth`` levels deep.

    At depth 1 only leaves are drawn, and a scope need not have a boolean field, so there
    is no boolean leaf to draw: the strategy is then empty, and the enclosing one_of drops
    it. Two levels always suffice, since a comparison of two literals is boolean.
    """
    options = typed_leaves(place, type_)
    if depth > 1:
        options += typed_branches(place, type_, depth - 1)
    return st.one_of(options) if options else st.nothing()


def typed_leaves(place: Place, type_: ExprType) -> list[st.SearchStrategy[Expr]]:
    """Strategies for the leaves of class ``type_``: paths, literals, the context, the lines."""
    scope = place.scope
    options: list[st.SearchStrategy[Expr]] = []
    for root, fields in roots(place):
        segments = field_paths(scope, fields, EXPANSIONS)[type_]
        if segments:
            options.append(st.sampled_from([Path(root, found) for found in segments]))
    match type_:
        case ExprType.NUM:
            options.append(NUMBERS)
            if scope.has_bill_lines:
                options.append(st.builds(Count))
                summed = [name for name, info in scope.line.items() if info.type is NUM]
                if summed:
                    options.append(st.builds(Sum, st.sampled_from(summed)))
        case ExprType.STR:
            options.append(STRINGS)
        case ExprType.DATE:
            options.append(st.builds(CtxToday))
        case ExprType.PERIOD:
            options.append(st.builds(CtxPeriod))
        case ExprType.BOOL:
            pass  # The only boolean leaf is a bool field; a comparison is a branch.
    return options


def typed_branches(place: Place, type_: ExprType, depth: int) -> list[st.SearchStrategy[Expr]]:
    """Strategies for the nodes of class ``type_`` whose operands are ``depth`` levels deep.

    A node's class follows from the node alone (requirement 10.11): the arithmetic
    operators and sum_of are of the number class, the comparisons, in, and, or, not and
    exists of the boolean class, and no node is of the string, date or period class, so a
    tree of one of those three is a leaf.
    """
    if type_ is NUM:
        numbers = typed(place, NUM, depth)
        arithmetic = st.builds(Binary, st.sampled_from(ARITHMETIC_OPS), numbers, numbers)
        return [arithmetic, *sum_of_calls(place, depth)]
    if type_ is not BOOL:
        return []
    booleans = typed(place, BOOL, depth)
    return [
        st.builds(Not, booleans),
        st.builds(Binary, st.sampled_from(LOGICAL_OPS), booleans, booleans),
        *(compared(place, EQUAL_OPS, operands, depth) for operands in ExprType),
        *(compared(place, ORDER_OPS, operands, depth) for operands in ORDERED_TYPES),
        *(memberships(place, operands, depth) for operands in MEMBER_TYPES),
        *exists_calls(place, depth),
    ]


def compared(
    place: Place, ops: Sequence[BinaryOp], type_: ExprType, depth: int
) -> st.SearchStrategy[Expr]:
    """``left op right`` with both operands of class ``type_`` (requirement 10.11)."""
    operand = typed(place, type_, depth)
    return st.builds(Binary, st.sampled_from(ops), operand, operand)


def memberships(place: Place, type_: ExprType, depth: int) -> st.SearchStrategy[Expr]:
    """``left in [...]`` with the left operand and every item of class ``type_``."""
    items = st.lists(LIST_ITEMS[type_], min_size=1, max_size=3).map(tuple)
    return st.builds(In, typed(place, type_, depth), st.builds(ListLit, items))


def exists_calls(place: Place, depth: int) -> list[st.SearchStrategy[Expr]]:
    """``exists(<target>, <condition>)`` for each entity the scope knows."""
    return [
        st.builds(Exists, st.just(target), typed(Place(place.scope, target), BOOL, depth))
        for target in place.scope.targets
    ]


def sum_of_calls(place: Place, depth: int) -> list[st.SearchStrategy[Expr]]:
    """``sum_of(<target>.<field>, <condition>)`` for each number field of each entity."""
    options: list[st.SearchStrategy[Expr]] = []
    for target, fields in place.scope.targets.items():
        summed = [name for name, info in fields.items() if info.type is NUM]
        if not summed:
            continue
        condition = typed(Place(place.scope, target), BOOL, depth)
        options.append(st.builds(SumOf, st.just(target), st.sampled_from(summed), condition))
    return options


# --- evaluation environments -------------------------------------------------------------


@dataclass(frozen=True)
class FakeRecord:
    """A drawn Record: the value of each field and the record each ref field points at.

    Both lookups assert that the name is declared, so an expression that reads a field the
    record does not have fails the test instead of passing silently.
    """

    values: Mapping[str, Value]
    refs: Mapping[str, "FakeRecord | None"]

    def field(self, name: str) -> Value:
        assert name in self.values, f"no field {name!r} in this record"
        return self.values[name]

    def ref(self, name: str) -> "FakeRecord | None":
        assert name in self.refs, f"no ref field {name!r} in this record"
        return self.refs[name]


@dataclass(frozen=True)
class FakeEnv:
    """A drawn EvalEnv: the bill, the current user, the entity tables and the anchor date."""

    header: FakeRecord
    lines: tuple[FakeRecord, ...]
    user: FakeRecord | None
    tables: Mapping[str, tuple[FakeRecord, ...]]
    today: date

    def records(self, target: str) -> tuple[FakeRecord, ...]:
        assert target in self.tables, f"no table {target!r}"
        return self.tables[target]


@st.composite
def rows(draw: st.DrawFn, scope: FakeScope, fields: Fields, depth: int) -> FakeRecord:
    """Draw one record of ``fields``, with ``depth`` further levels of referenced records.

    Any field may be null (requirement 10.6). A ref field's own value, the ID it holds, and
    the record it points at are drawn apart: a reference that resolves to no record is a
    value the Record protocol allows, and at depth 0 none resolves.
    """
    values: dict[str, Value] = {}
    refs: dict[str, FakeRecord | None] = {}
    for name, info in fields.items():
        values[name] = draw(maybe_null(FIELD_VALUES[info.type]))
        if info.ref is None:
            continue
        referenced = scope.targets[info.ref]
        if depth == 0:
            refs[name] = None
        else:
            refs[name] = draw(st.none() | rows(scope, referenced, depth - 1))
    return FakeRecord(values, refs)


def row_tables(
    scope: FakeScope, fields: Fields, min_size: int = 0
) -> st.SearchStrategy[tuple[FakeRecord, ...]]:
    """The records of one table: the lines of a bill, or every record of an entity."""
    return st.lists(rows(scope, fields, EXPANSIONS), min_size=min_size, max_size=2).map(tuple)


@st.composite
def envs(draw: st.DrawFn, scope: FakeScope) -> FakeEnv:
    """Draw an env matching ``scope``: every field holds a value of the class it declares.

    A scope with a current line gets at least one line, since that is the record a
    ``line.`` path reads; the other scopes draw from no line up, the bill on which sum adds
    up to 0 and count counts 0. The current user may be missing, which makes every
    ``context.user`` path null (requirement 10.6).
    """
    return FakeEnv(
        header=draw(rows(scope, scope.header, EXPANSIONS)),
        lines=draw(row_tables(scope, scope.line, min_size=int(scope.has_current_line))),
        user=draw(st.none() | rows(scope, scope.user, EXPANSIONS)),
        tables={
            target: draw(row_tables(scope, fields)) for target, fields in scope.targets.items()
        },
        today=draw(DATE_VALUES),
    )
