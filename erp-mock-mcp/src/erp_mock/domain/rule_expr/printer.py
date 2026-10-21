"""Rule_Expression_Printer: the canonical text of a syntax tree (requirement 10.3).

The text depends only on the tree, never on positions, so texts that differ only in
whitespace or redundant parentheses, which parse to equal trees, print the same. For a
tree t within the parser's MAX_DEPTH, ``parse_expr(print_expr(t)) == t`` (requirement
10.4). The canonical form:

- One space on each side of a binary operator and ``in``, one after ``not``; ``, ``
  between function arguments and list items; no other whitespace.
- Parentheses only where the tree needs them. Operators bind, from loosest to tightest:
  or, and, not, the comparisons (``==`` ``!=`` ``<`` ``<=`` ``>`` ``>=`` ``in``), ``+``
  and ``-``, ``*`` and ``/``. The left operand of a left-associative operator (or, and,
  arithmetic) is parenthesized when it binds more loosely than the operator, the right
  operand when it binds no more tightly. Both operands of a comparison, which does not
  chain, are parenthesized when they bind no more tightly than a comparison, and the
  operand of not when it binds more loosely than not.
- Numbers in plain decimal notation without exponent, leading zeros or trailing zeros of
  the fraction: 1.50 is ``1.5``, 1E+2 is ``100``, 0.000 is ``0``.
- Strings in single quotes, each quote inside doubled: it's is ``'it''s'``.
"""

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Final, assert_never

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

__all__ = ["print_expr"]

# Binding strength, loosest first.
_OR: Final = 1
_AND: Final = 2
_NOT: Final = 3
_COMPARISON: Final = 4
_ADDITIVE: Final = 5
_MULTIPLICATIVE: Final = 6
_ATOM: Final = 7

_PRECEDENCE: Final[Mapping[BinaryOp, int]] = MappingProxyType(
    {
        "or": _OR,
        "and": _AND,
        "==": _COMPARISON,
        "!=": _COMPARISON,
        "<": _COMPARISON,
        "<=": _COMPARISON,
        ">": _COMPARISON,
        ">=": _COMPARISON,
        "+": _ADDITIVE,
        "-": _ADDITIVE,
        "*": _MULTIPLICATIVE,
        "/": _MULTIPLICATIVE,
    }
)


def print_expr(expr: Expr) -> str:
    """Return the canonical text of ``expr`` (Rule_Expression_Printer)."""
    match expr:
        case Num(value=value):
            return _number_text(value)
        case Str(value=value):
            return _string_text(value)
        case Path(root=root, segments=segments):
            names = ".".join(segments)
            return names if root is None else f"{root}.{names}"
        case CtxToday():
            return "context.today"
        case CtxPeriod():
            return "context.period"
        case Not(operand=operand):
            return f"not {_operand(operand, _precedence(operand) < _NOT)}"
        case Binary(op=op, left=left, right=right):
            precedence = _PRECEDENCE[op]
            if precedence == _COMPARISON:
                wrap_left = _precedence(left) <= precedence
            else:
                wrap_left = _precedence(left) < precedence
            wrap_right = _precedence(right) <= precedence
            return f"{_operand(left, wrap_left)} {op} {_operand(right, wrap_right)}"
        case In(left=left, right=right):
            return f"{_operand(left, _precedence(left) <= _COMPARISON)} in {_list_text(right)}"
        case Sum(field=field):
            return f"sum(lines.{field})"
        case Count():
            return "count(lines)"
        case Exists(target=target, cond=cond):
            return f"exists({target}, {print_expr(cond)})"
        case SumOf(target=target, field=field, cond=cond):
            return f"sum_of({target}.{field}, {print_expr(cond)})"
        case _:
            assert_never(expr)


def _precedence(expr: Expr) -> int:
    match expr:
        case Binary(op=op):
            return _PRECEDENCE[op]
        case Not():
            return _NOT
        case In():
            return _COMPARISON
        case _:
            return _ATOM


def _operand(expr: Expr, parenthesize: bool) -> str:
    text = print_expr(expr)
    return f"({text})" if parenthesize else text


def _list_text(items: ListLit) -> str:
    return "[" + ", ".join(print_expr(item) for item in items.items) + "]"


def _number_text(value: Decimal) -> str:
    """Plain decimal text of a finite, non-negative ``value``, without needless zeros.

    ``format(value, "f")`` is exact and never uses exponent notation, whatever the
    decimal context; Decimal.normalize() would depend on the context's precision.
    """
    if value.is_zero():
        return "0"  # also for -0 and 0E-5
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _string_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
