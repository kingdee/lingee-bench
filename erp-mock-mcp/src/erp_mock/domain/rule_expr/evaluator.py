"""Evaluating a rule expression and deciding a rule (requirements 10.5 to 10.7, 10.12, 10.13).

The checker decides whether an expression resolves and is well typed; this module gives it
a value. An :class:`EvalEnv` is the object the expression is evaluated on: the bill the
operation produced, for an enforced rule's check (requirement 10.5), or one source record,
for a declared query's filter (requirement 10.10). The engine builds one per check or
query call, and the unit tests with stand-ins.

Values are :data:`Value`: a number is a Decimal, a string and a period (``YYYY-MM``) a
str, a date a ``datetime.date``, a boolean a bool. None is both the null value of a field
and the "unknown" of the three-valued logic, which requirement 10.6 treats alike.

Three-valued logic (requirement 10.6). None spreads:

- a field that is null, a path through a ref field that is null, and ``context.user.F``
  when no current user is set, are null;
- arithmetic with a null operand, and a division whose divisor is zero, are null;
- a comparison or ``in`` with a null operand is unknown;
- ``and`` is false when either operand is false, true when both are true and unknown
  otherwise; ``or`` is true when either operand is true, false when both are false and
  unknown otherwise; ``not`` of unknown is unknown.

A rule is unsatisfied only when the final value is false, as an SQL CHECK constraint is:
unknown counts as satisfied.

Operations on values that are not null (requirement 10.12). Arithmetic runs in
``RULE_CTX``, so every result is rounded to 28 significant digits with ROUND_HALF_UP and
never to the precision of a field. Numbers compare by magnitude (0.10 equals 0.1), dates
and periods chronologically, and strings by code point, so comparison distinguishes case.
``in`` is true when the left operand equals an item of the list.

Functions (requirement 10.13). ``sum(lines.F)`` adds F over the lines of the current bill
and ``count(lines)`` counts them; ``exists(E, cond)`` is true when ``cond`` is true for at
least one record of E, so unknown does not count and no record makes it false;
``sum_of(E.F, cond)`` adds F over the records of E for which ``cond`` is true. Both sums
skip null values and give 0 when nothing is left. Unprefixed paths in a condition name
fields of the record being examined, while ``header.``, ``line.`` and ``context.`` paths
keep naming the object being evaluated. Each target table is read once per check: the
evaluator of one check keeps what ``EvalEnv.records`` returned.

:func:`eval_check` decides one rule (requirement 10.7): a check that names no ``line.``
path is evaluated once, and one that does is evaluated per line, in line number order,
collecting the lines whose value is false; a bill without lines satisfies every check.

The expression must have passed :func:`~erp_mock.domain.rule_expr.checker.check_expr` in a
scope that matches the env. Evaluation then raises nothing for any env. It raises TypeError
for a value of the wrong class and ValueError for a ``line.`` path without a current line
or an unprefixed path outside a condition, which report a bug in the caller or the env,
never a configuration error.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal, Protocol, assert_never

from erp_mock.common.decimals import RULE_CTX, WIDE
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
    Not,
    Num,
    Path,
    Str,
    Sum,
    SumOf,
)
from erp_mock.domain.rule_expr.printer import print_expr

__all__ = [
    "CheckResult",
    "EvalEnv",
    "Record",
    "Value",
    "eval_check",
    "eval_expr",
    "references_line",
]

_ID_SUFFIX: Final = "_id"

# The operator groups of _binary; each is a part of BinaryOp.
type _LogicalOp = Literal["and", "or"]
type _OrderOp = Literal["<", "<=", ">", ">="]
type _ArithmeticOp = Literal["+", "-", "*", "/"]

# The value of an expression and of a field: None is the null value and the unknown of the
# three-valued logic. A period is the str YYYY-MM, as context.period is.
type Value = Decimal | str | date | bool | None


class Record(Protocol):
    """One record an expression reads: a bill header, a bill line or an entity record.

    The checker has resolved every name against the domain model before evaluation, so a
    name reaching these methods is a field of this record. An implementation may report a
    name it does not know as it likes; the evaluator does not call it with another.
    """

    def field(self, name: str) -> Value:
        """The value of the field ``name``, None when it is null.

        A ref field gives its own value, the ID of the record it refers to, as a str; the
        evaluator reads it for a path that ends in the ref field, such as
        ``line.material_id``.
        """

    def ref(self, name: str) -> "Record | None":
        """The record that the ref field ``name`` refers to, None when that field is null.

        ``name`` ends in ``_id``: expanding the segment ``material`` of
        ``line.material.kind`` reads the ref field ``material_id`` (requirement 10.1).
        Referential integrity makes every value that is not null resolve; an
        implementation that finds no record returns None, and the path is null.
        """


class EvalEnv(Protocol):
    """What one expression is evaluated on: the bill or record, the context and the tables.

    For an enforced rule's check this is the bill the operation produced (requirement
    10.5); for a declared query's filter it is one source record (requirement 10.10),
    where ``header`` is the record itself when the source is an entity or a bill, and the
    line's bill when the source is a bill's lines.

    The loader and the tools layer implement the protocol over the sandbox, and the unit
    tests with stand-ins.
    """

    @property
    def today(self) -> date:
        """The anchor date: the value of ``context.today``.

        ``context.period`` is the ``YYYY-MM`` of its month, which the evaluator derives.
        """

    @property
    def header(self) -> Record:
        """The record that ``header.<field>`` reads."""

    @property
    def lines(self) -> Sequence[Record]:
        """The lines of the current bill, by line number; empty when there is no bill.

        Line numbers run from 1 without gaps (requirement 4.4), so the line number of
        ``lines[k]`` is ``k + 1``, which is also the index requirement 10.7 reports in
        ``lines[i].<field>``. ``sum`` and ``count`` read the whole sequence; a check that
        names a ``line.`` path is evaluated once per element.
        """

    @property
    def user(self) -> Record | None:
        """The current user's record, None when the instance has no current user.

        Every ``context.user.<field>`` path is then null (requirement 10.6); a dataset is
        built this way before the employee records exist.
        """

    def records(self, target: str) -> Sequence[Record]:
        """Every record of the entity or bill ``target``, a bill's headers.

        Requirement 10.13: the records are those in the sandbox when the call began, so
        they include the operated bill as it was saved before the call and not a bill the
        call creates. The order decides in which order ``sum_of`` adds, so it should not
        depend on the run. The evaluator asks once per check and keeps the result.
        """


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What one rule's check decided on one bill (requirement 10.7).

    - ``satisfied``: whether the rule holds. False only where a value is false; unknown
      counts as satisfied (requirement 10.6).
    - ``per_line``: whether the check names a ``line.`` path and was therefore evaluated
      per line. The caller builds the details of the error from it: a check that names
      none gets one item with the rule's field, or none when the rule declares no field,
      and one that does gets an item per failing line, with the path
      ``lines[<line number> - 1].<field>``.
    - ``failing_lines``: the line numbers, ascending, whose value is false; empty unless
      ``per_line`` and the rule is unsatisfied.
    """

    satisfied: bool
    per_line: bool
    failing_lines: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.failing_lines, tuple):
            raise TypeError(
                f"failing_lines must be a tuple, got {type(self.failing_lines).__name__}"
            )
        if self.failing_lines != tuple(sorted(set(self.failing_lines))):
            raise ValueError("failing_lines must be line numbers in ascending order, no repeats")
        if self.failing_lines and self.failing_lines[0] < 1:
            raise ValueError(f"line numbers start at 1, got {self.failing_lines[0]}")
        if self.failing_lines and not self.per_line:
            raise ValueError("only a check that references line can have failing lines")
        if self.per_line and self.satisfied == bool(self.failing_lines):
            raise ValueError("a check on the lines is satisfied exactly when no line fails")


def eval_expr(expr: Expr, env: EvalEnv, line: Record | None = None) -> Value:
    """Return the value of ``expr`` in ``env`` (Rule_Engine, requirements 10.6, 10.12, 10.13).

    ``line`` is the record that ``line.<field>`` reads: one of ``env.lines`` for a rule on
    a bill, and the source record for a query whose source is a bill's lines. It may be
    None only for an expression that names no ``line.`` path; otherwise ValueError is
    raised. :func:`eval_check` passes each line in turn.

    Each exists or sum_of target table is read once per call; :func:`eval_check` reads it
    once for the whole check.
    """
    return _Evaluator(env).value(expr, line)


def eval_check(check: Expr, env: EvalEnv) -> CheckResult:
    """Decide whether the rule whose check is ``check`` holds on ``env`` (requirement 10.7).

    A check that names no ``line.`` path is evaluated once on the bill. A check that names
    one is evaluated once per line of ``env.lines``, in line number order, and the lines
    whose value is false are reported; a bill without lines satisfies it. Either way the
    rule is unsatisfied only where a value is false, never where it is unknown.
    """
    evaluator = _Evaluator(env)
    if not references_line(check):
        return CheckResult(satisfied=evaluator.value(check, None) is not False, per_line=False)
    failing = tuple(
        number
        for number, line in enumerate(env.lines, start=1)
        if evaluator.value(check, line) is False
    )
    return CheckResult(satisfied=not failing, per_line=True, failing_lines=failing)


def references_line(expr: Expr) -> bool:
    """Whether ``expr`` names a ``line.<field>`` path (requirement 10.5).

    Function arguments and the conditions of exists and sum_of count, so
    ``exists(supplier, credit > line.qty)`` references line. ``sum(lines.F)`` and
    ``count(lines)`` read the whole bill and do not.
    """
    match expr:
        case Path(root="line"):
            return True
        case Not(operand=operand):
            return references_line(operand)
        case Binary(left=left, right=right):
            return references_line(left) or references_line(right)
        case In(left=left):
            return references_line(left)
        case Exists(cond=cond) | SumOf(cond=cond):
            return references_line(cond)
        case _:
            return False


# --- walking the tree --------------------------------------------------------------------


class _Evaluator:
    """One check: evaluates its expression on any line, reading each target table once."""

    __slots__ = ("_env", "_tables")

    def __init__(self, env: EvalEnv) -> None:
        self._env = env
        self._tables: dict[str, tuple[Record, ...]] = {}

    def value(self, expr: Expr, line: Record | None) -> Value:
        """The value of ``expr`` with ``line`` as the current line."""
        return self._eval(expr, line, None)

    def _eval(self, expr: Expr, line: Record | None, condition: Record | None) -> Value:
        """The value of ``expr``; ``condition`` is the record an unprefixed path reads."""
        match expr:
            case Num(value=value) | Str(value=value):
                return value
            case CtxToday():
                return self._env.today
            case CtxPeriod():
                return _period_text(self._env.today)
            case Path():
                return self._path(expr, line, condition)
            case Not(operand=operand):
                return _not(self._eval(operand, line, condition))
            case Binary(op=op, left=left, right=right):
                return self._binary(op, left, right, line, condition)
            case In(left=left, right=items):
                return _in(self._eval(left, line, condition), items)
            case Sum(field=name):
                return _total(record.field(name) for record in self._env.lines)
            case Count():
                return Decimal(len(self._env.lines))
            case Exists(target=target, cond=cond):
                records = self._table(target)
                return any(self._eval(cond, line, record) is True for record in records)
            case SumOf(target=target, field=name, cond=cond):
                return _total(
                    record.field(name)
                    for record in self._table(target)
                    if self._eval(cond, line, record) is True
                )
            case _:
                assert_never(expr)

    def _binary(
        self, op: BinaryOp, left: Expr, right: Expr, line: Record | None, condition: Record | None
    ) -> Value:
        """``left op right``; both operands are evaluated, neither decides alone."""
        first = self._eval(left, line, condition)
        second = self._eval(right, line, condition)
        match op:
            case "and" | "or":
                return _logical(op, _truth(first), _truth(second))
            case "==" | "!=":
                if first is None or second is None:
                    return None
                equal = first == second
                return equal if op == "==" else not equal
            case "<" | "<=" | ">" | ">=":
                return _ordered(op, first, second)
            case "+" | "-" | "*" | "/":
                return _arithmetic(op, first, second)
            case _:
                assert_never(op)

    def _path(self, path: Path, line: Record | None, condition: Record | None) -> Value:
        """The value of ``path``: expand every segment but the last, then read the field."""
        record = self._root(path, line, condition)
        *expanded, last = path.segments
        for segment in expanded:
            if record is None:
                return None
            record = record.ref(f"{segment}{_ID_SUFFIX}")
        return None if record is None else record.field(last)

    def _root(self, path: Path, line: Record | None, condition: Record | None) -> Record | None:
        """The record the first segment of ``path`` is read from, None when it is null."""
        match path.root:
            case "header":
                return self._env.header
            case "line":
                if line is None:
                    raise ValueError(f"no current line, so {print_expr(path)} has no value")
                return line
            case "context.user":
                return self._env.user
            case None:
                if condition is None:
                    raise ValueError(
                        f"{print_expr(path)} stands outside an exists or sum_of condition"
                    )
                return condition

    def _table(self, target: str) -> tuple[Record, ...]:
        """The records of ``target``, read from the env on the first ask of this check."""
        cached = self._tables.get(target)
        if cached is None:
            cached = tuple(self._env.records(target))
            self._tables[target] = cached
        return cached


# --- operators ---------------------------------------------------------------------------


def _truth(value: Value) -> bool | None:
    """``value`` as a truth value; a null value is unknown (requirement 10.6)."""
    if value is None or isinstance(value, bool):
        return value
    raise TypeError(f"a boolean value was expected, got {type(value).__name__}")


def _number(value: Value) -> Decimal:
    """``value`` as a number; the caller has handled null."""
    if isinstance(value, Decimal):
        return value
    raise TypeError(f"a number was expected, got {type(value).__name__}")


def _not(value: Value) -> bool | None:
    """Kleene ``not``: not unknown is unknown (requirement 10.6)."""
    found = _truth(value)
    return None if found is None else not found


def _logical(op: _LogicalOp, left: bool | None, right: bool | None) -> bool | None:
    """Kleene ``and`` and ``or`` (requirement 10.6); None is the unknown of both sides."""
    if op == "and":
        if left is False or right is False:
            return False
        if left is True and right is True:
            return True
    else:
        if left is True or right is True:
            return True
        if left is False and right is False:
            return False
    return None


def _ordered(op: _OrderOp, left: Value, right: Value) -> bool | None:
    """``left op right`` for ``<``, ``<=``, ``>`` and ``>=``; unknown when a side is null."""
    if left is None or right is None:
        return None
    less, equal = _order(left, right)
    match op:
        case "<":
            return less
        case "<=":
            return less or equal
        case ">":
            return not less and not equal
        case ">=":
            return not less


def _order(left: Value, right: Value) -> tuple[bool, bool]:
    """``(left < right, left == right)`` for two values of one ordered class (req 10.12).

    Numbers compare by magnitude, dates and periods chronologically, since the ``YYYY-MM``
    text of a period orders by month, and strings by code point, case included. The
    checker lets only these three classes be ordered, so another pair is a bug.
    """
    match left, right:
        case (Decimal() as first, Decimal() as second):
            return first < second, first == second
        case (str() as first, str() as second):
            return first < second, first == second
        case (date() as first, date() as second):
            return first < second, first == second
    raise TypeError(f"cannot order {type(left).__name__} and {type(right).__name__}")


def _in(left: Value, items: ListLit) -> bool | None:
    """``left in [...]``: true when ``left`` equals an item (requirement 10.12).

    A list item is a literal, never null, so only a null left operand is unknown.
    """
    if left is None:
        return None
    return any(left == item.value for item in items.items)


def _arithmetic(op: _ArithmeticOp, left: Value, right: Value) -> Decimal | None:
    """``left op right`` in RULE_CTX: null when a side is null or the divisor is 0 (10.6).

    RULE_CTX rounds every result to 28 significant digits with ROUND_HALF_UP, whatever the
    precision of the fields involved (requirement 10.12).
    """
    if left is None or right is None:
        return None
    first, second = _number(left), _number(right)
    match op:
        case "+":
            return RULE_CTX.add(first, second)
        case "-":
            return RULE_CTX.subtract(first, second)
        case "*":
            return RULE_CTX.multiply(first, second)
        case "/":
            return None if second.is_zero() else RULE_CTX.divide(first, second)


def _total(values: Iterable[Value]) -> Decimal:
    """The sum of ``values``, nulls skipped and 0 when none is left (requirement 10.13).

    The values are added in WIDE, whose 80 significant digits make the sum of stored field
    values exact, and the total is rounded once to RULE_CTX's 28 significant digits, so
    the result does not depend on the order in which the values arrive.
    """
    total = Decimal(0)
    for value in values:
        if value is not None:
            total = WIDE.add(total, _number(value))
    return RULE_CTX.plus(total)


def _period_text(day: date) -> str:
    """The ``YYYY-MM`` of the month of ``day``: the value of ``context.period`` (10.5)."""
    return f"{day.year:04d}-{day.month:02d}"
