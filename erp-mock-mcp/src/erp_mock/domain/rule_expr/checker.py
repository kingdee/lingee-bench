"""Checking a rule expression: names, operator types, result type (requirements 10.2, 10.11).

The parser accepts every text that follows the grammar; the checker decides whether the
names of the tree resolve in the object the expression is evaluated on and whether its
operators and functions get the types they take. An :class:`ExprScope` describes that
object: the loader builds one per enforced rule, from the bill the rule applies to, and
one per declared query, from its source (requirement 10.10), and the checker asks it to
resolve every name.

The checks run in this order, each collecting every error it finds; the first one that
found any raises its error with the smallest position (requirement 10.2):

- names: a field path the scope cannot resolve, a segment that expands a field which is
  not a ref field, an entity or bill name that does not exist, ``line.`` where the
  evaluated records are not bill lines, ``lines`` where they belong to no bill, and an
  unprefixed path outside an exists or sum_of condition. The position is the first
  character of the path or name that does not resolve: ``pos`` of a path, ``lines_pos``
  of sum and count, ``target_pos`` of exists and sum_of.
- operators: an operand, list item, summed field or condition of a type the operator or
  function does not take (requirement 10.11). The position is the first character of the
  operator or of the function name.
- result: the expression as a whole is not of the boolean class. The position is 1.

The five type classes are :class:`ExprType`. A node's class follows from the node alone:
a comparison, ``in``, ``and``, ``or``, ``not`` and ``exists`` are boolean, arithmetic,
``sum``, ``count`` and ``sum_of`` are number, and only a path depends on the scope. So
every expression has a type, an ill-typed one included, and one walk of the tree finds
all of its name and operator errors.

Scopes. ``header.F`` is a field of the bill header, or of the record a query's source
names; ``line.F`` a field of the current bill line; ``context.user.F`` a field of the
current user's record; ``context.today`` and ``context.period`` take no name. Every
segment but the last expands a ref field named by the segment plus ``_id`` (requirement
10.1), so ``line.material.kind`` resolves ``material_id`` in the lines and then ``kind``
in the entity it refers to. The last segment is a field name as declared, so the ref field
itself is ``header.supplier_id``, of the string class, while ``header.supplier`` resolves
only if a field of that name exists. In an exists or sum_of condition an
unprefixed path is a field of the target's records, a bill's header fields included
(requirement 10.13), while prefixed paths still name the object being evaluated; a
condition inside another condition takes the inner target.

Only ExprError is raised, for every tree the parser can produce.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from types import MappingProxyType
from typing import Final, Protocol, assert_never

from erp_mock.config.fields import FieldType
from erp_mock.domain.rule_expr.errors import ExprError
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
    is_name,
)

__all__ = [
    "FIELD_EXPR_TYPES",
    "ExprScope",
    "ExprType",
    "FieldInfo",
    "check_expr",
    "expr_type",
]

_ID_SUFFIX: Final = "_id"

_NO_LINE: Final = "line paths are not available here, the evaluated records are not bill lines"
_NO_LINES: Final = "lines are not available here, the evaluated records belong to no bill"
_UNPREFIXED: Final = "an unprefixed field path stands only in an exists or sum_of condition"


class ExprType(StrEnum):
    """The five type classes of requirement 10.11; the value names the class in an error."""

    NUM = "number"
    STR = "string"
    DATE = "date"
    PERIOD = "period"
    BOOL = "boolean"


# The class of each field type (requirement 10.11). The loader types a field with it.
FIELD_EXPR_TYPES: Final[Mapping[FieldType, ExprType]] = MappingProxyType(
    {
        "int": ExprType.NUM,
        "qty": ExprType.NUM,
        "price": ExprType.NUM,
        "money": ExprType.NUM,
        "rate": ExprType.NUM,
        "string": ExprType.STR,
        "text": ExprType.STR,
        "enum": ExprType.STR,
        "ref": ExprType.STR,
        "date": ExprType.DATE,
        "period": ExprType.PERIOD,
        "bool": ExprType.BOOL,
    }
)


@dataclass(frozen=True, slots=True)
class FieldInfo:
    """A field a scope resolved: its type class and, for a ref field, what it refers to.

    ``ref`` is the name of the entity or bill the field refers to, and None for every
    other field; only a ref field can be expanded. A ref field is of the string class
    (requirement 10.11), so ``type`` is then ExprType.STR.
    """

    type: ExprType
    ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, ExprType):
            raise TypeError(f"type must be an ExprType, got {type(self.type).__name__}")
        if self.ref is None:
            return
        if not is_name(self.ref):
            raise ValueError(f"ref must match [a-z][a-z0-9_]*, got {self.ref!r}")
        if self.type is not ExprType.STR:
            raise ValueError(f"a ref field is of the string class, got {self.type}")


class ExprScope(Protocol):
    """What the names of one expression resolve to: the object it is evaluated on.

    A method returns None for a name the scope does not know, which the checker reports
    as a names error; it raises nothing. The loader (load step 5) implements the protocol
    over the domain model, with the ``_id`` of a ref field included in the field names it
    knows, and the unit tests of the checker with stand-ins.
    """

    @property
    def has_current_line(self) -> bool:
        """Whether ``line.<field>`` paths resolve.

        True for a rule on a bill and for a query whose source is a bill's lines; False
        for a query over an entity or over bill headers (requirement 10.2).
        """

    @property
    def has_bill_lines(self) -> bool:
        """Whether ``sum(lines.<field>)`` and ``count(lines)`` resolve.

        True when the evaluated record belongs to a bill, so also for a query over bill
        headers, where the record's own lines are summed and ``line.`` does not resolve.
        """

    def header_field(self, name: str) -> FieldInfo | None:
        """The field ``name`` of the bill header, or of the record a query's source names."""

    def line_field(self, name: str) -> FieldInfo | None:
        """The field ``name`` of the bill lines; also the field summed by ``sum``."""

    def user_field(self, name: str) -> FieldInfo | None:
        """The field ``name`` of the current user's record (``context.user.<name>``)."""

    def has_target(self, target: str) -> bool:
        """Whether ``target`` names an entity or bill of the configuration."""

    def target_field(self, target: str, name: str) -> FieldInfo | None:
        """The field ``name`` of the records of ``target``, a bill's header fields.

        ``target`` is the target of an exists or sum_of, or the entity or bill a ref field
        refers to.
        """


def expr_type(expr: Expr, scope: ExprScope) -> ExprType:
    """Return the type class of ``expr`` in ``scope`` after the names and operator checks.

    Raises ExprError with phase "names" or "operators" as requirement 10.2 describes. The
    result type is not checked, so the type returned is any of the five classes; use
    :func:`check_expr` for a rule's check or a query's filter.
    """
    checker = _Checker(scope)
    found = checker.visit(expr, None)
    checker.report()
    if found is None:  # Unreachable: a path without a type is a names error, just raised.
        raise ExprError("names", 1, "the expression has a name that does not resolve")
    return found


def check_expr(expr: Expr, scope: ExprScope) -> ExprType:
    """Check ``expr`` as the check of a rule or the filter of a query in ``scope``.

    Runs the names, operator type and result type checks of requirement 10.2 and returns
    the type of ``expr``, which is ExprType.BOOL: a check or filter must be of the boolean
    class (requirement 10.11), and a result error has phase "result" and position 1.
    """
    found = expr_type(expr, scope)
    if found is not ExprType.BOOL:
        raise ExprError("result", 1, f"the expression must be boolean, got {found}")
    return found


# --- walking the tree --------------------------------------------------------------------


def _no_field(name: str) -> FieldInfo | None:
    """Field lookup of a scope whose records are unknown: nothing resolves."""
    return None


@dataclass(frozen=True, slots=True)
class _Scope:
    """One record scope: ``lookup`` resolves a field name, ``describe`` names it in an error.

    ``known`` is False for the condition of an exists or sum_of whose target does not
    exist: that names error is already reported, so no field of the condition is.
    """

    describe: str
    lookup: Callable[[str], FieldInfo | None]
    known: bool = True


def _literal_type(item: Num | Str) -> ExprType:
    return ExprType.NUM if isinstance(item, Num) else ExprType.STR


class _Checker:
    """One check of one expression: walks the tree and collects the errors of two phases."""

    __slots__ = ("_names", "_operators", "_scope")

    def __init__(self, scope: ExprScope) -> None:
        self._scope = scope
        self._names: list[ExprError] = []
        self._operators: list[ExprError] = []

    def report(self) -> None:
        """Raise the smallest-position error of the first phase that found any."""
        for errors in (self._names, self._operators):
            if errors:
                raise min(errors, key=_position)

    def visit(self, expr: Expr, condition: _Scope | None) -> ExprType | None:
        """The type of ``expr``, or None if a name of it does not resolve.

        ``condition`` is the scope an unprefixed path takes, and None outside an exists or
        sum_of condition. Errors are collected, not raised.
        """
        match expr:
            case Num():
                return ExprType.NUM
            case Str():
                return ExprType.STR
            case CtxToday():
                return ExprType.DATE
            case CtxPeriod():
                return ExprType.PERIOD
            case Path():
                return self._path(expr, condition)
            case Not(operand=operand, pos=pos):
                found = self.visit(operand, condition)
                self._require(pos, found, ExprType.BOOL, "the operand of not")
                return ExprType.BOOL
            case Binary(op=op, left=left, right=right, pos=pos):
                left_type = self.visit(left, condition)
                right_type = self.visit(right, condition)
                return self._binary(op, left_type, right_type, pos)
            case In(left=left, right=items, pos=pos):
                self._in(self.visit(left, condition), items, pos)
                return ExprType.BOOL
            case Sum(field=name, pos=pos, lines_pos=lines_pos):
                return self._sum(name, pos, lines_pos)
            case Count(lines_pos=lines_pos):
                if not self._scope.has_bill_lines:
                    self._name_error(lines_pos, _NO_LINES)
                return ExprType.NUM
            case Exists(target=target, cond=cond, pos=pos, target_pos=target_pos):
                scope = self._target_scope(target, target_pos)
                found = self.visit(cond, scope)
                self._require(pos, found, ExprType.BOOL, "the condition of exists")
                return ExprType.BOOL
            case SumOf(target=target, field=name, cond=cond, pos=pos, target_pos=target_pos):
                scope = self._target_scope(target, target_pos)
                self._sum_of_field(scope, name, pos, target_pos)
                found = self.visit(cond, scope)
                self._require(pos, found, ExprType.BOOL, "the condition of sum_of")
                return ExprType.NUM
            case _:
                assert_never(expr)

    # --- errors --------------------------------------------------------------------------

    def _name_error(self, position: int, reason: str) -> None:
        self._names.append(ExprError("names", position, reason))

    def _operator_error(self, position: int, reason: str) -> None:
        self._operators.append(ExprError("operators", position, reason))

    def _require(self, pos: int, found: ExprType | None, expected: ExprType, subject: str) -> None:
        """Record an operator error unless ``found`` is ``expected`` or did not resolve."""
        if found is not None and found is not expected:
            self._operator_error(pos, f"{subject} must be {expected}, got {found}")

    def _require_same(
        self, pos: int, left: ExprType | None, right: ExprType | None, op: BinaryOp
    ) -> bool:
        """Whether both types resolved and are one class; records an operator error if not."""
        if left is None or right is None:
            return False
        if left is not right:
            reason = f"the operands of {op} must be of one type, got {left} and {right}"
            self._operator_error(pos, reason)
            return False
        return True

    # --- operators -----------------------------------------------------------------------

    def _binary(
        self, op: BinaryOp, left: ExprType | None, right: ExprType | None, pos: int
    ) -> ExprType:
        match op:
            case "and" | "or":
                self._require(pos, left, ExprType.BOOL, f"the left operand of {op}")
                self._require(pos, right, ExprType.BOOL, f"the right operand of {op}")
                return ExprType.BOOL
            case "==" | "!=":
                self._require_same(pos, left, right, op)
                return ExprType.BOOL
            case "<" | "<=" | ">" | ">=":
                if self._require_same(pos, left, right, op) and left is ExprType.BOOL:
                    reason = f"the operands of {op} must be number, date, period or string"
                    self._operator_error(pos, f"{reason}, got boolean")
                return ExprType.BOOL
            case _:
                self._require(pos, left, ExprType.NUM, f"the left operand of {op}")
                self._require(pos, right, ExprType.NUM, f"the right operand of {op}")
                return ExprType.NUM

    def _in(self, left: ExprType | None, items: ListLit, pos: int) -> None:
        """Requirement 10.11: the left operand and every item are number, or all string."""
        if left is None:
            return
        if left is not ExprType.NUM and left is not ExprType.STR:
            self._operator_error(
                pos, f"the left operand of in must be number or string, got {left}"
            )
            return
        for item in items.items:
            found = _literal_type(item)
            if found is not left:
                reason = f"the list items of in must be {left}, like the left operand, got {found}"
                self._operator_error(pos, reason)
                return

    def _sum(self, name: str, pos: int, lines_pos: int) -> ExprType:
        """``sum(lines.<name>)``: the lines of the current bill and a number field."""
        if not self._scope.has_bill_lines:
            self._name_error(lines_pos, _NO_LINES)
            return ExprType.NUM
        info = self._scope.line_field(name)
        if info is None:
            self._name_error(lines_pos, f"no field {name!r} in lines")
            return ExprType.NUM
        self._require(pos, info.type, ExprType.NUM, "the field of sum")
        return ExprType.NUM

    def _sum_of_field(self, scope: _Scope, name: str, pos: int, target_pos: int) -> None:
        """``sum_of(<target>.<name>, ...)``: a number field of the target's records."""
        if not scope.known:
            return
        info = scope.lookup(name)
        if info is None:
            self._name_error(target_pos, f"no field {name!r} in {scope.describe}")
            return
        self._require(pos, info.type, ExprType.NUM, "the field of sum_of")

    # --- names ---------------------------------------------------------------------------

    def _target_scope(self, target: str, target_pos: int) -> _Scope:
        """The records of an exists or sum_of target; ``known`` is False if it does not exist."""
        if not self._scope.has_target(target):
            self._name_error(target_pos, f"no entity or bill named {target!r}")
            return _Scope(target, _no_field, known=False)
        return _Scope(target, partial(self._scope.target_field, target))

    def _path(self, path: Path, condition: _Scope | None) -> ExprType | None:
        scope = self._path_scope(path, condition)
        if scope is None:
            return None
        return self._resolve(path, scope)

    def _path_scope(self, path: Path, condition: _Scope | None) -> _Scope | None:
        """Where the first segment of ``path`` is looked up; None if the prefix cannot be used."""
        match path.root:
            case "header":
                return _Scope("header", self._scope.header_field)
            case "line":
                if not self._scope.has_current_line:
                    self._name_error(path.pos, _NO_LINE)
                    return None
                return _Scope("line", self._scope.line_field)
            case "context.user":
                return _Scope("context.user", self._scope.user_field)
            case None:
                if condition is None:
                    self._name_error(path.pos, _UNPREFIXED)
                    return None
                return condition

    def _resolve(self, path: Path, scope: _Scope) -> ExprType | None:
        """Resolve the segments of ``path`` from ``scope``, expanding a ref field each time."""
        current = scope
        *expanded, last = path.segments
        for segment in expanded:
            info = current.lookup(f"{segment}{_ID_SUFFIX}")
            if info is None or info.ref is None:
                if current.known:
                    self._name_error(path.pos, self._expansion_reason(current, segment, info))
                return None
            if not self._scope.has_target(info.ref):
                reason = f"field {segment}{_ID_SUFFIX} in {current.describe} refers to"
                self._name_error(path.pos, f"{reason} {info.ref!r}, which is no entity or bill")
                return None
            current = _Scope(info.ref, partial(self._scope.target_field, info.ref))
        found = current.lookup(last)
        if found is None:
            if current.known:
                self._name_error(path.pos, f"no field {last!r} in {current.describe}")
            return None
        return found.type

    def _expansion_reason(self, scope: _Scope, segment: str, info: FieldInfo | None) -> str:
        """Why ``segment`` cannot be expanded: no such ref field, or the field is not a ref."""
        if info is not None:
            field = f"field {segment}{_ID_SUFFIX} in {scope.describe}"
            return f"{field} is not a ref field, so {segment} cannot be expanded"
        if scope.lookup(segment) is not None:
            field = f"field {segment!r} in {scope.describe}"
            return f"{field} is not a ref field, it cannot be expanded"
        return f"no ref field {segment}{_ID_SUFFIX} in {scope.describe}, which {segment} expands"


def _position(error: ExprError) -> int:
    return error.position
