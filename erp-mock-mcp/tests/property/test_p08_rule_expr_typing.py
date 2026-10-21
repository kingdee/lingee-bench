"""Property 8: the soundness of the type check.

**Validates: Requirements 10.11**

For any scope drawn by :func:`~tests.property.models.rule_expr.scopes` and any tree built
by the typing rules of requirement 10.11 to be of the class T:

- the check accepts the tree and reports T. A rule's check and a query's filter must be
  boolean, which is the last clause of requirement 10.11, so ``check_expr`` reports a
  result error for every other class; the class of a tree of any of the five is read with
  ``expr_type``, which runs the same names and operator type checks, and a boolean tree
  goes through ``check_expr`` as well.
- evaluating the tree in any env that matches the scope raises nothing and gives a value of
  T or the null value. A period is a str of the form ``YYYY-MM``, which tells it from
  another string.

The scope, the tree and the env are drawn by ``tests.property.models.rule_expr``, which
property 9 draws from as well; that module documents each of the three draws.
"""

import re
from datetime import date
from decimal import Decimal
from typing import Final, assert_never

import pytest
from hypothesis import given, note, settings
from hypothesis import strategies as st

from erp_mock.domain.rule_expr.checker import ExprType, check_expr, expr_type
from erp_mock.domain.rule_expr.evaluator import Value, eval_expr
from erp_mock.domain.rule_expr.printer import print_expr
from tests.property.models.rule_expr import BOOL, TREE_DEPTH, Place, envs, scopes, typed

# The text of a period value, which tells it from any other string.
PERIOD_FORM: Final = re.compile(r"[0-9]{4}-[0-9]{2}")


def is_value_of(type_: ExprType, value: Value) -> bool:
    """Whether ``value``, which is not null, belongs to the class ``type_`` (requirement 10.11)."""
    match type_:
        case ExprType.NUM:
            return isinstance(value, Decimal)
        case ExprType.STR:
            return isinstance(value, str)
        case ExprType.DATE:
            return isinstance(value, date)
        case ExprType.PERIOD:
            return isinstance(value, str) and PERIOD_FORM.fullmatch(value) is not None
        case ExprType.BOOL:
            return isinstance(value, bool)
        case _:
            assert_never(type_)


# --- properties --------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 8: 类型检查的可靠性
@pytest.mark.req("10.11")
@pytest.mark.parametrize("type_", list(ExprType))
@settings(max_examples=100)
@given(data=st.data())
def test_the_check_reports_the_class_the_tree_was_built_with(
    type_: ExprType, data: st.DataObject
) -> None:
    scope = data.draw(scopes(), label="scope")
    expr = data.draw(typed(Place(scope), type_, TREE_DEPTH), label="expression")
    note(f"expression: {print_expr(expr)}")
    assert expr_type(expr, scope) is type_
    # A check or a filter must be boolean, and check_expr accepts exactly those.
    if type_ is BOOL:
        assert check_expr(expr, scope) is BOOL


# Feature: erp-mock-mcp-m0, Property 8: 类型检查的可靠性
@pytest.mark.req("10.11")
@pytest.mark.parametrize("type_", list(ExprType))
@settings(max_examples=100)
@given(data=st.data())
def test_evaluation_gives_a_value_of_that_class_or_null(
    type_: ExprType, data: st.DataObject
) -> None:
    scope = data.draw(scopes(), label="scope")
    expr = data.draw(typed(Place(scope), type_, TREE_DEPTH), label="expression")
    env = data.draw(envs(scope), label="env")
    note(f"expression: {print_expr(expr)}")
    # The tree passed the check, which is what the evaluator may rely on.
    assert expr_type(expr, scope) is type_
    # The record a line. path reads: one of the bill's lines, where the scope has one.
    line = data.draw(st.sampled_from(env.lines), label="line") if scope.has_current_line else None
    value = eval_expr(expr, env, line)
    note(f"value: {value!r}")
    assert value is None or is_value_of(type_, value)
