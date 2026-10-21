"""Property 9: the reference model of rule evaluation.

**Validates: Requirements 10.6, 10.7, 10.12, 10.13**

For any well-typed tree and any env drawn by ``tests.property.models.rule_expr``, which
property 8 draws from as well:

- ``eval_expr`` gives the value of the reference evaluator below, which spells out
  requirements 10.6, 10.12 and 10.13 once more, on its own terms: arithmetic on exact
  rationals, rounded to 28 significant digits with ROUND_HALF_UP, the Kleene logic as the
  lower and the higher of two truth values, null spreading through paths and arithmetic, a
  divisor of zero giving null, and strings compared by code point.
- ``eval_check`` decides the rule as requirement 10.7 does: a check that names no
  ``line.`` path is evaluated once and the rule is unsatisfied only where that value is
  false, a check that names one is evaluated per line and the lines whose value is false
  are listed by ascending line number, which is what the error details report, and a bill
  without lines satisfies the rule. Two tests draw this half, one over any scope and one
  over the scopes and trees that give a check on the lines, which are a tenth of them.

The reference evaluator keeps numbers as ``fractions.Fraction``, so no intermediate result
is rounded by the model itself: each arithmetic node rounds its exact result once, which
is what RULE_CTX's 28 significant digits do to the Decimal arithmetic of the evaluator.
``same_value`` compares the two as exact rationals.
"""

import dataclasses
from collections.abc import Iterable
from dataclasses import fields
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Final, Literal, assert_never

import pytest
from hypothesis import given, note, settings
from hypothesis import strategies as st

from erp_mock.domain.rule_expr.checker import ExprType
from erp_mock.domain.rule_expr.evaluator import CheckResult, Value, eval_check, eval_expr
from erp_mock.domain.rule_expr.nodes import (
    Binary,
    BinaryOp,
    Count,
    CtxPeriod,
    CtxToday,
    Exists,
    Expr,
    In,
    ListLit,
    Node,
    Not,
    Num,
    Path,
    Str,
    Sum,
    SumOf,
)
from erp_mock.domain.rule_expr.printer import print_expr
from tests.property.models.rule_expr import (
    BOOL,
    ID_SUFFIX,
    REACHES,
    TREE_DEPTH,
    FakeEnv,
    FakeRecord,
    Place,
    envs,
    period_text,
    scopes,
    typed,
)

# The operator groups of _binary; each is a part of BinaryOp.
type _LogicalOp = Literal["and", "or"]
type _EqualOp = Literal["==", "!="]
type _OrderOp = Literal["<", "<=", ">", ">="]
type _ArithmeticOp = Literal["+", "-", "*", "/"]

# A value of the reference evaluator: a number is an exact rational, so nothing but the
# rounding of an arithmetic result loses a digit. None is the null value and the unknown of
# the three-valued logic, as it is in the evaluator; a period is the str YYYY-MM.
type RefValue = Fraction | str | date | bool | None

# Significant digits of an arithmetic result (requirement 10.12), RULE_CTX's precision.
RULE_DIGITS: Final = 28

# Kleene's order of the truth values: and is the lower of its two operands and or the
# higher, which is requirement 10.6 read as an order (and is false where either operand is
# false, true where both are true and unknown otherwise; or the other way round).
TRUTH_ORDER: Final[tuple[bool | None, ...]] = (False, None, True)

# Whether the bill's lines are dropped before a rule is decided, so that one example in
# four has no line: the env of a scope with a current line always draws at least one.
NO_LINES: Final = st.sampled_from([False, False, False, True])

# What a scope that resolves a ``line.`` path reaches: a current line and the bill's lines,
# which is a rule on a bill or a query over its lines (requirement 10.2).
ON_LINES: Final = (True, True)
assert ON_LINES in REACHES


# --- rounding ----------------------------------------------------------------------------


def round_significant(value: Fraction, digits: int = RULE_DIGITS) -> Fraction:
    """``value`` rounded to ``digits`` significant digits, ROUND_HALF_UP (requirement 10.12).

    Integer arithmetic on the exact rational decides, so the result does not depend on any
    rounding that came before: the kept part moves one unit away from zero when what is
    dropped is at least half a unit of the last kept place.
    """
    if value == 0:
        return Fraction(0)
    magnitude = abs(value)
    scale = Fraction(10) ** (digits - 1 - decimal_exponent(magnitude))
    scaled = magnitude * scale
    whole, remainder = divmod(scaled.numerator, scaled.denominator)
    if 2 * remainder >= scaled.denominator:
        whole += 1
    rounded = Fraction(whole) / scale
    return rounded if value > 0 else -rounded


def decimal_exponent(value: Fraction) -> int:
    """The ``k`` with ``10 ** k <= value < 10 ** (k + 1)``; ``value`` is positive.

    The digit counts of the numerator and the denominator place ``k`` within one, and the
    two loops below each take at most a step from there.
    """
    found = len(str(value.numerator)) - len(str(value.denominator))
    while Fraction(10) ** found > value:
        found -= 1
    while Fraction(10) ** (found + 1) <= value:
        found += 1
    return found


# --- the reference evaluator -------------------------------------------------------------


def ref_value(expr: Expr, env: FakeEnv, line: FakeRecord | None = None) -> RefValue:
    """The value requirements 10.6, 10.12 and 10.13 give ``expr`` in ``env``.

    ``line`` is the record a ``line.`` path reads, as it is for ``eval_expr``.
    """
    return _value(expr, env, line, None)


def _value(
    expr: Expr, env: FakeEnv, line: FakeRecord | None, condition: FakeRecord | None
) -> RefValue:
    """The value of ``expr``; ``condition`` is the record an unprefixed path reads."""
    match expr:
        case Num(value=number):
            return Fraction(number)
        case Str(value=text):
            return text
        case CtxToday():
            return env.today
        case CtxPeriod():
            return period_text(env.today)
        case Path():
            return _path(expr, env, line, condition)
        case Not(operand=operand):
            return _not(_truth(_value(operand, env, line, condition)))
        case Binary(op=op, left=left, right=right):
            return _binary(
                op,
                _value(left, env, line, condition),
                _value(right, env, line, condition),
            )
        case In(left=left, right=items):
            return _member(_value(left, env, line, condition), items)
        case Sum(field=name):
            return _total(_as_value(record.field(name)) for record in env.lines)
        case Count():
            return Fraction(len(env.lines))
        case Exists(target=target, cond=cond):
            return any(_value(cond, env, line, record) is True for record in env.records(target))
        case SumOf(target=target, field=name, cond=cond):
            return _total(
                _as_value(record.field(name))
                for record in env.records(target)
                if _value(cond, env, line, record) is True
            )
        case _:
            assert_never(expr)


def _path(
    path: Path, env: FakeEnv, line: FakeRecord | None, condition: FakeRecord | None
) -> RefValue:
    """The value of ``path``: a null anywhere along it makes the whole path null (req 10.6)."""
    record = _root(path, env, line, condition)
    for segment in path.segments[:-1]:
        if record is None:
            return None
        record = record.ref(f"{segment}{ID_SUFFIX}")
    if record is None:
        return None
    return _as_value(record.field(path.segments[-1]))


def _root(
    path: Path, env: FakeEnv, line: FakeRecord | None, condition: FakeRecord | None
) -> FakeRecord | None:
    """The record the first segment of ``path`` is read from, None where it is null."""
    match path.root:
        case "header":
            return env.header
        case "line":
            assert line is not None, f"{print_expr(path)} needs a current line"
            return line
        case "context.user":
            return env.user
        case None:
            assert condition is not None, f"{print_expr(path)} needs a condition"
            return condition


def _as_value(value: Value) -> RefValue:
    """A field's value as the reference holds it: a number becomes an exact rational."""
    return Fraction(value) if isinstance(value, Decimal) else value


def _truth(value: RefValue) -> bool | None:
    """``value`` as a truth value; a null value is the unknown of the logic (req 10.6)."""
    assert value is None or isinstance(value, bool), f"not a boolean value: {value!r}"
    return value


def _number(value: RefValue) -> Fraction:
    """``value`` as a number; the caller has handled null."""
    assert isinstance(value, Fraction), f"not a number: {value!r}"
    return value


def _not(value: bool | None) -> bool | None:
    """Kleene ``not``: not unknown is unknown (requirement 10.6)."""
    return None if value is None else not value


def _logical(op: _LogicalOp, left: bool | None, right: bool | None) -> bool | None:
    """Kleene ``and`` and ``or`` (requirement 10.6), as the lower and the higher value."""
    ranks = (TRUTH_ORDER.index(left), TRUTH_ORDER.index(right))
    return TRUTH_ORDER[min(ranks) if op == "and" else max(ranks)]


def _binary(op: BinaryOp, left: RefValue, right: RefValue) -> RefValue:
    """``left op right``; both operands have been evaluated, neither decides alone."""
    match op:
        case "and" | "or":
            return _logical(op, _truth(left), _truth(right))
        case "==" | "!=":
            return _equality(op, left, right)
        case "<" | "<=" | ">" | ">=":
            return _ordered(op, left, right)
        case "+" | "-" | "*" | "/":
            return _arithmetic(op, left, right)
        case _:
            assert_never(op)


def _equality(op: _EqualOp, left: RefValue, right: RefValue) -> bool | None:
    """``==`` and ``!=``: unknown where either side is null (requirement 10.6)."""
    if left is None or right is None:
        return None
    equal = _equal(left, right)
    return equal if op == "==" else not equal


def _ordered(op: _OrderOp, left: RefValue, right: RefValue) -> bool | None:
    """``<``, ``<=``, ``>`` and ``>=``: unknown where either side is null (req 10.6)."""
    if left is None or right is None:
        return None
    less, equal = _less(left, right), _equal(left, right)
    greater = not less and not equal
    match op:
        case "<":
            return less
        case "<=":
            return less or equal
        case ">":
            return greater
        case ">=":
            return greater or equal


def _arithmetic(op: _ArithmeticOp, left: RefValue, right: RefValue) -> Fraction | None:
    """The four operations: null where either side is null or the divisor is 0 (req 10.6).

    The operands are exact rationals, so the operation is exact and its result is rounded
    once, to 28 significant digits, whatever the precision of the fields involved
    (requirement 10.12).
    """
    if left is None or right is None:
        return None
    first, second = _number(left), _number(right)
    match op:
        case "+":
            exact = first + second
        case "-":
            exact = first - second
        case "*":
            exact = first * second
        case "/":
            if second == 0:
                return None
            exact = first / second
    return round_significant(exact)


def _member(left: RefValue, items: ListLit) -> bool | None:
    """``left in [...]``: true where an item equals ``left`` (requirement 10.12).

    A list item is a literal, never null, so only a null left operand is unknown.
    """
    if left is None:
        return None
    return any(_equal(left, _literal(item)) for item in items.items)


def _literal(item: Num | Str) -> RefValue:
    """The value of a list item: a number literal or a string literal."""
    return Fraction(item.value) if isinstance(item, Num) else item.value


def _total(values: Iterable[RefValue]) -> Fraction:
    """The sum of ``values``, nulls skipped and 0 where none is left (requirement 10.13).

    The values are added exactly and the total is rounded once, so no order of the records
    gives another result.
    """
    total = Fraction(0)
    for value in values:
        if value is not None:
            total += _number(value)
    return round_significant(total)


def _equal(left: RefValue, right: RefValue) -> bool:
    """Whether two values of one class are equal (requirement 10.12).

    Numbers are compared by magnitude, so 0.10 equals 0.1; strings and periods by their
    code points, so case tells two strings apart; dates by year, month and day.
    """
    match left, right:
        case (bool(), bool()):
            return left is right
        case (Fraction(), Fraction()):
            return left == right
        case (str(), str()):
            return _code_points(left) == _code_points(right)
        case (date(), date()):
            return _day(left) == _day(right)
    raise AssertionError(f"cannot compare {left!r} and {right!r}")


def _less(left: RefValue, right: RefValue) -> bool:
    """Whether ``left`` comes before ``right`` in the order of its class (req 10.12).

    Numbers compare by magnitude and dates chronologically. Strings compare character by
    character by code point, and a period, whose value is its ``YYYY-MM`` text, then
    compares chronologically as requirement 10.12 asks.
    """
    match left, right:
        case (Fraction(), Fraction()):
            return left < right
        case (str(), str()):
            return _code_points(left) < _code_points(right)
        case (date(), date()):
            return _day(left) < _day(right)
    raise AssertionError(f"cannot order {left!r} and {right!r}")


def _code_points(text: str) -> tuple[int, ...]:
    """The code points of ``text``, which decide its order and its equality (req 10.12)."""
    return tuple(ord(char) for char in text)


def _day(value: date) -> tuple[int, int, int]:
    """``value`` as year, month and day, which order dates chronologically (req 10.12)."""
    return value.year, value.month, value.day


# --- the outcome of one rule -------------------------------------------------------------


def names_line(node: object) -> bool:
    """Whether the tree ``node`` holds a ``line.`` path anywhere (requirement 10.5).

    Every field of every node is walked, so a path inside a function argument or inside an
    exists or sum_of condition counts too. ``sum(lines.F)`` and ``count(lines)`` read the
    whole bill: they name a field of the lines, not a path, so they hold none.
    """
    if isinstance(node, Path):
        return node.root == "line"
    if isinstance(node, Node):
        return any(names_line(getattr(node, field.name)) for field in fields(node))
    if isinstance(node, tuple):
        return any(names_line(item) for item in node)
    return False


def ref_check(check: Expr, env: FakeEnv) -> CheckResult:
    """What requirement 10.7 decides for the rule whose check is ``check``.

    A check that names no ``line.`` path is evaluated once on the bill, and the rule is
    unsatisfied only where that value is false; unknown counts as satisfied (requirement
    10.6). A check that names one is evaluated once per line, and the lines whose value is
    false are reported by ascending line number, the order of ``env.lines``; a bill without
    lines is never evaluated, so the rule holds.
    """
    if not names_line(check):
        return CheckResult(satisfied=ref_value(check, env) is not False, per_line=False)
    failing = tuple(
        number
        for number, line in enumerate(env.lines, start=1)
        if ref_value(check, env, line) is False
    )
    return CheckResult(satisfied=not failing, per_line=True, failing_lines=failing)


def same_value(found: Value, expected: RefValue) -> bool:
    """Whether the evaluator's ``found`` is the ``expected`` value of the reference model.

    A number is a Decimal in the evaluator and a Fraction here, so the two are compared as
    exact rationals; every other class is one type in both. The class is checked as well as
    the value, since in Python ``Decimal(1) == True``.
    """
    if expected is None:
        return found is None
    if isinstance(expected, bool):
        return isinstance(found, bool) and found is expected
    if isinstance(expected, Fraction):
        return isinstance(found, Decimal) and Fraction(found) == expected
    if isinstance(expected, str):
        return isinstance(found, str) and found == expected
    return isinstance(found, date) and found == expected


# --- properties --------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 9: 规则求值的参考模型
@pytest.mark.req("10.6", "10.12", "10.13")
@pytest.mark.parametrize("type_", list(ExprType))
@settings(max_examples=100)
@given(data=st.data())
def test_the_value_is_the_one_the_reference_evaluator_gives(
    type_: ExprType, data: st.DataObject
) -> None:
    scope = data.draw(scopes(), label="scope")
    expr = data.draw(typed(Place(scope), type_, TREE_DEPTH), label="expression")
    env = data.draw(envs(scope), label="env")
    # The record a line. path reads: one of the bill's lines, where the scope has one.
    line = data.draw(st.sampled_from(env.lines), label="line") if scope.has_current_line else None
    note(f"expression: {print_expr(expr)}")
    expected = ref_value(expr, env, line)
    found = eval_expr(expr, env, line)
    note(f"found: {found!r}, expected: {expected!r}")
    assert same_value(found, expected)


# Feature: erp-mock-mcp-m0, Property 9: 规则求值的参考模型
@pytest.mark.req("10.6", "10.7")
@settings(max_examples=100)
@given(data=st.data())
def test_the_rule_outcome_is_the_one_the_reference_model_gives(data: st.DataObject) -> None:
    scope = data.draw(scopes(), label="scope")
    check = data.draw(typed(Place(scope), BOOL, TREE_DEPTH), label="check")
    env = data.draw(envs(scope), label="env")
    note(f"check: {print_expr(check)}")
    expected = ref_check(check, env)
    found = eval_check(check, env)
    note(f"found: {found!r}, expected: {expected!r}")
    assert found == expected


# Feature: erp-mock-mcp-m0, Property 9: 规则求值的参考模型
@pytest.mark.req("10.6", "10.7")
@settings(max_examples=100)
@given(data=st.data())
def test_a_check_that_names_a_line_is_decided_line_by_line(data: st.DataObject) -> None:
    """The same property, narrowed to the checks requirement 10.7 decides line by line.

    Those are the checks whose details list the failing lines. Only a third of the scopes
    resolve a ``line.`` path and only a third of the trees drawn in one name it, so the
    test above reaches this half of the property in one example in ten; here the scope and
    the tree are drawn to name one.
    """
    scope = data.draw(scopes(ON_LINES), label="scope")
    check = data.draw(typed(Place(scope), BOOL, TREE_DEPTH).filter(names_line), label="check")
    env = data.draw(envs(scope), label="env")
    # A bill without lines satisfies a check on the lines (requirement 10.7); the env of
    # this scope always draws a line, so the lines are dropped here instead.
    if data.draw(NO_LINES, label="no lines"):
        env = dataclasses.replace(env, lines=())
    note(f"check: {print_expr(check)}")
    expected = ref_check(check, env)
    found = eval_check(check, env)
    note(f"found: {found!r}, expected: {expected!r}")
    assert found == expected
