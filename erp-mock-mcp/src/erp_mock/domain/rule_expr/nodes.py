"""Syntax trees of rule expressions (requirement 10.1).

An expression is a tree of the node classes below; :data:`Expr` is their union. The
parser module gives the grammar. Nodes are frozen, hashable dataclasses:

- Structural equality. ``pos`` and the other ``*_pos`` fields are excluded from
  equality, hashing and repr, so the trees of two texts that differ only in whitespace
  or redundant parentheses are equal. Number literals compare by value
  (``Num(Decimal("1.50")) == Num(Decimal("1.5"))``), as requirement 10.4 asks.
- Positions. ``pos`` is the 1-based code point index, in the parsed text, of the first
  character of the node's literal, path, operator or function name (see each class); the
  checker reports names and operator type errors there (requirement 10.2). Parentheses
  have no node. Nodes built by hand default to position 0.
- Only trees that have a text can be built: the constructors reject field values that
  no text produces, such as an unknown operator, an empty path, a name that is not
  ``[a-z][a-z0-9_]*``, a negative number or an empty list literal. print_expr can then
  write any tree, and parse_expr reads the text back as an equal tree.

Names follow the configuration's name rule ``^[a-z][a-z0-9_]*$``. RESERVED_WORDS (the
keywords, function names and path prefixes of requirement 10.1) cannot start an
unprefixed path, which would be read as that word; every other name may be any name,
reserved or not, because the parser reads the word after ``.``, the target of exists and
sum_of and the field of sum and sum_of as a name.
"""

import dataclasses
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal, get_args

__all__ = [
    "BINARY_OPS",
    "FUNCTION_NAMES",
    "KEYWORDS",
    "PATH_PREFIXES",
    "PATH_ROOTS",
    "RESERVED_WORDS",
    "Binary",
    "BinaryOp",
    "Count",
    "CtxPeriod",
    "CtxToday",
    "Exists",
    "Expr",
    "In",
    "ListLit",
    "Node",
    "Not",
    "Num",
    "Path",
    "PathRoot",
    "Str",
    "Sum",
    "SumOf",
    "is_name",
]

# The binary operators: logical, comparison (in is the In node) and arithmetic.
BinaryOp = Literal["or", "and", "==", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/"]
BINARY_OPS: Final[tuple[BinaryOp, ...]] = get_args(BinaryOp)

# Where a field path starts: header.F, line.F and context.user.F; None for an unprefixed
# path, which names a field of the record an exists or sum_of condition examines.
PathRoot = Literal["header", "line", "context.user"]
PATH_ROOTS: Final[tuple[PathRoot, ...]] = get_args(PathRoot)

KEYWORDS: Final = frozenset({"and", "or", "not", "in"})
FUNCTION_NAMES: Final = frozenset({"sum", "count", "exists", "sum_of"})
PATH_PREFIXES: Final = frozenset({"header", "line", "lines", "context"})
RESERVED_WORDS: Final = KEYWORDS | FUNCTION_NAMES | PATH_PREFIXES

# [a-z] and [0-9] match ASCII only; used with fullmatch.
_NAME: Final = re.compile(r"[a-z][a-z0-9_]*")


def is_name(value: object) -> bool:
    """Whether ``value`` is a str matching ``^[a-z][a-z0-9_]*$``."""
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def _check_name(field: str, value: object) -> None:
    if not is_name(value):
        raise ValueError(f"{field} must match [a-z][a-z0-9_]*, got {value!r}")


def _check_expr(field: str, value: object) -> None:
    if not isinstance(value, _EXPR_TYPES):
        raise TypeError(f"{field} must be an expression node, got {type(value).__name__}")


type Expr = (
    Num | Str | Path | CtxToday | CtxPeriod | Not | Binary | In | Sum | Count | Exists | SumOf
)


@dataclass(frozen=True, slots=True)
class Node:
    """Base of the node classes; ``pos`` locates the node in the parsed text.

    Position fields are keyword-only and default to 0. They are written out in full in
    each class, since mypy's dataclass support recognizes only direct field() calls.
    """

    pos: int = dataclasses.field(default=0, compare=False, repr=False, kw_only=True)


@dataclass(frozen=True, slots=True)
class Num(Node):
    """A number literal such as ``0``, ``100`` or ``0.13``; ``pos`` is its first digit.

    ``value`` is a finite Decimal, not negative (the literal has no sign; -0 equals 0).
    """

    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise TypeError(f"value must be a Decimal, got {type(self.value).__name__}")
        if not self.value.is_finite() or self.value < 0:
            raise ValueError(f"value must be finite and not negative, got {self.value}")


@dataclass(frozen=True, slots=True)
class Str(Node):
    """A string literal such as ``'raw'``; ``pos`` is its opening quote.

    ``value`` is the text between the quotes, with each doubled quote read as one.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError(f"value must be a str, got {type(self.value).__name__}")


@dataclass(frozen=True, slots=True)
class Path(Node):
    """A field path; ``pos`` is its first character.

    ``header.a.b`` is ``Path("header", ("a", "b"))``, ``line.material.kind`` is
    ``Path("line", ("material", "kind"))``, ``context.user.role`` is
    ``Path("context.user", ("role",))`` and the unprefixed ``status`` is
    ``Path(None, ("status",))``. Every segment but the last expands a ref field named by
    the segment plus ``_id`` (requirement 10.1); the checker resolves the names.
    """

    root: PathRoot | None
    segments: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.root is not None and self.root not in PATH_ROOTS:
            raise ValueError(f"root must be one of {', '.join(PATH_ROOTS)} or None")
        if not isinstance(self.segments, tuple):
            raise TypeError(f"segments must be a tuple, got {type(self.segments).__name__}")
        if not self.segments:
            raise ValueError("a path has at least one segment")
        for segment in self.segments:
            _check_name("segment", segment)
        if self.root is None and self.segments[0] in RESERVED_WORDS:
            raise ValueError(f"an unprefixed path cannot start with {self.segments[0]!r}")


@dataclass(frozen=True, slots=True)
class CtxToday(Node):
    """``context.today``, the anchor date; ``pos`` is the ``c`` of ``context``."""


@dataclass(frozen=True, slots=True)
class CtxPeriod(Node):
    """``context.period``, the month of the anchor date; ``pos`` is the ``c`` of ``context``."""


@dataclass(frozen=True, slots=True)
class Not(Node):
    """``not operand``; ``pos`` is the ``not``."""

    operand: Expr

    def __post_init__(self) -> None:
        _check_expr("operand", self.operand)


@dataclass(frozen=True, slots=True)
class Binary(Node):
    """``left op right`` for a logical, comparison or arithmetic operator.

    ``pos`` is the first character of the operator.
    """

    op: BinaryOp
    left: Expr
    right: Expr

    def __post_init__(self) -> None:
        if self.op not in BINARY_OPS:
            raise ValueError(f"op must be one of {' '.join(BINARY_OPS)}, got {self.op!r}")
        _check_expr("left", self.left)
        _check_expr("right", self.right)


@dataclass(frozen=True, slots=True)
class ListLit(Node):
    """A list literal such as ``['approved', 'closed']``; ``pos`` is its ``[``.

    It has one or more number or string literals, which need not be of one type, and is
    only the right operand of in, so it is not an Expr.
    """

    items: tuple[Num | Str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise TypeError(f"items must be a tuple, got {type(self.items).__name__}")
        if not self.items:
            raise ValueError("a list literal has at least one item")
        for item in self.items:
            if not isinstance(item, Num | Str):
                raise TypeError(f"an item must be a Num or Str, got {type(item).__name__}")


@dataclass(frozen=True, slots=True)
class In(Node):
    """``left in [...]``; ``pos`` is the ``in``."""

    left: Expr
    right: ListLit

    def __post_init__(self) -> None:
        _check_expr("left", self.left)
        if not isinstance(self.right, ListLit):
            raise TypeError(f"right must be a ListLit, got {type(self.right).__name__}")


@dataclass(frozen=True, slots=True)
class Sum(Node):
    """``sum(lines.field)``; ``pos`` is the ``sum`` and ``lines_pos`` the ``lines``."""

    field: str
    lines_pos: int = dataclasses.field(default=0, compare=False, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        _check_name("field", self.field)


@dataclass(frozen=True, slots=True)
class Count(Node):
    """``count(lines)``; ``pos`` is the ``count`` and ``lines_pos`` the ``lines``."""

    lines_pos: int = dataclasses.field(default=0, compare=False, repr=False, kw_only=True)


@dataclass(frozen=True, slots=True)
class Exists(Node):
    """``exists(target, cond)``; ``pos`` is the ``exists`` and ``target_pos`` the target.

    ``target`` is an entity or bill name; unprefixed paths in ``cond`` name fields of the
    target's records.
    """

    target: str
    cond: Expr
    target_pos: int = dataclasses.field(default=0, compare=False, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        _check_name("target", self.target)
        _check_expr("cond", self.cond)


@dataclass(frozen=True, slots=True)
class SumOf(Node):
    """``sum_of(target.field, cond)``; ``pos`` is the ``sum_of``.

    ``target_pos`` is the first character of ``target.field``. ``target`` is an entity or
    bill name; unprefixed paths in ``cond`` name fields of the target's records.
    """

    target: str
    field: str
    cond: Expr
    target_pos: int = dataclasses.field(default=0, compare=False, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        _check_name("target", self.target)
        _check_name("field", self.field)
        _check_expr("cond", self.cond)


_EXPR_TYPES: Final = (
    Num,
    Str,
    Path,
    CtxToday,
    CtxPeriod,
    Not,
    Binary,
    In,
    Sum,
    Count,
    Exists,
    SumOf,
)
