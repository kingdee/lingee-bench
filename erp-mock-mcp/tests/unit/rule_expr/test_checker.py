"""erp_mock.domain.rule_expr.checker: names, operator types, result type (requirement 10).

The scopes below stand in for the ones the loader builds from the domain model: a bill with
a header, lines and a few entities, so that every form of requirement 10.1 resolves.
"""

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import pytest

from erp_mock.config.fields import FIELD_TYPES
from erp_mock.domain.rule_expr.checker import (
    FIELD_EXPR_TYPES,
    ExprScope,
    ExprType,
    FieldInfo,
    check_expr,
    expr_type,
)
from erp_mock.domain.rule_expr.errors import ExprError
from erp_mock.domain.rule_expr.parser import parse_expr

NUM: Final = ExprType.NUM
STR: Final = ExprType.STR
DATE: Final = ExprType.DATE
PERIOD: Final = ExprType.PERIOD
BOOL: Final = ExprType.BOOL

Fields = Mapping[str, FieldInfo]


def ref(target: str) -> FieldInfo:
    """A ref field pointing at ``target``."""
    return FieldInfo(STR, target)


@dataclass(frozen=True)
class FakeScope:
    """A stand-in ExprScope: the fields of each level and of the named entities and bills."""

    header: Fields
    line: Fields
    user: Fields
    targets: Mapping[str, Fields]
    has_current_line: bool = True
    has_bill_lines: bool = True

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


# A purchase order: one field per class, including an int (line_count) and a ref whose
# target the scope does not know (ghost_id), which only a broken configuration has.
HEADER: Final[Fields] = {
    "number": FieldInfo(STR),
    "status": FieldInfo(STR),
    "notes": FieldInfo(STR),
    "biz_date": FieldInfo(DATE),
    "delivery_date": FieldInfo(DATE),
    "period": FieldInfo(PERIOD),
    "total_amount": FieldInfo(NUM),
    "line_count": FieldInfo(NUM),
    "closed": FieldInfo(BOOL),
    "supplier_id": ref("supplier"),
    "ghost_id": ref("ghost"),
    # A field whose name ends in _id that is no ref field, which only a broken
    # configuration has: ``header.legacy`` has nothing to expand to.
    "legacy_id": FieldInfo(STR),
}
LINE: Final[Fields] = {
    "qty": FieldInfo(NUM),
    "price": FieldInfo(NUM),
    "amount": FieldInfo(NUM),
    "tax_rate": FieldInfo(NUM),
    "qty_base": FieldInfo(NUM),
    "remark": FieldInfo(STR),
    "due_date": FieldInfo(DATE),
    "material_id": ref("material"),
}
USER: Final[Fields] = {"role": FieldInfo(STR), "org_id": ref("org")}
TARGETS: Final[Mapping[str, Fields]] = {
    "supplier": {
        "code": FieldInfo(STR),
        "status": FieldInfo(STR),
        "credit": FieldInfo(NUM),
        "enabled": FieldInfo(BOOL),
    },
    "material": {
        "code": FieldInfo(STR),
        "kind": FieldInfo(STR),
        "tax_rate": FieldInfo(NUM),
        "unit_id": ref("unit"),
    },
    "unit": {"code": FieldInfo(STR), "precision": FieldInfo(NUM)},
    "org": {"name": FieldInfo(STR)},
    "warehouse": {"code": FieldInfo(STR)},
    "stock_balance": {
        "qty": FieldInfo(NUM),
        "material_id": ref("material"),
        "warehouse_id": ref("warehouse"),
    },
}

# The scope of a rule on the bill, and of a query whose source is the bill's lines.
SCOPE: Final = FakeScope(header=HEADER, line=LINE, user=USER, targets=TARGETS)
# A query over the bill headers: no current line, but the bill's lines can be summed.
HEADER_SCOPE: Final = dataclasses.replace(SCOPE, has_current_line=False)
# A query over an entity: header. is the entity's record and no bill is in reach.
ENTITY_SCOPE: Final = dataclasses.replace(SCOPE, has_current_line=False, has_bill_lines=False)


def position_of(text: str, marker: str) -> int:
    """The 1-based code point position of ``marker``, which stands exactly once in ``text``."""
    assert text.count(marker) == 1, f"{marker!r} must stand once in {text!r}"
    return text.index(marker) + 1


def error_of(text: str, scope: ExprScope = SCOPE) -> ExprError:
    """Check ``text``, which must fail, and return the one error reported."""
    with pytest.raises(ExprError) as info:
        check_expr(parse_expr(text), scope)
    return info.value


def check(text: str, scope: ExprScope = SCOPE) -> ExprType:
    return check_expr(parse_expr(text), scope)


def type_of(text: str, scope: ExprScope = SCOPE) -> ExprType:
    return expr_type(parse_expr(text), scope)


# --- types of fields, literals, operators and functions (requirement 10.11) --------------


@pytest.mark.req("10.11")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Number: int, qty, price, money and rate fields, literals, arithmetic, the
        # aggregate functions.
        ("header.line_count", NUM),
        ("line.qty", NUM),
        ("line.price", NUM),
        ("header.total_amount", NUM),
        ("line.tax_rate", NUM),
        ("0.13", NUM),
        ("line.qty * line.price - 1", NUM),
        ("count(lines)", NUM),
        ("sum(lines.amount)", NUM),
        ("sum_of(stock_balance.qty, qty > 0)", NUM),
        # String: string, text, enum and ref fields and string literals.
        ("header.number", STR),
        ("header.notes", STR),
        ("header.status", STR),
        ("header.supplier_id", STR),
        ("'raw'", STR),
        # Date and period, from a field and from the context.
        ("header.biz_date", DATE),
        ("context.today", DATE),
        ("header.period", PERIOD),
        ("context.period", PERIOD),
        # Boolean: bool fields, comparisons, in, the logical operators, exists.
        ("header.closed", BOOL),
        ("line.qty > 0", BOOL),
        ("header.biz_date == context.today", BOOL),
        ("header.status in ['approved', 'closed']", BOOL),
        ("header.closed and not header.closed", BOOL),
        ("exists(supplier, enabled)", BOOL),
        # A ref field expands to the referenced record's field, through any number of refs.
        ("line.material.kind", STR),
        ("line.material.tax_rate", NUM),
        ("line.material.unit.precision", NUM),
        ("context.user.role", STR),
        ("context.user.org.name", STR),
    ],
)
def test_types_every_form(text: str, expected: ExprType) -> None:
    assert type_of(text) == expected


@pytest.mark.req("10.11")
def test_field_expr_types_classify_every_field_type() -> None:
    assert set(FIELD_EXPR_TYPES) == set(FIELD_TYPES)
    by_class = {
        expr_class: {name for name, found in FIELD_EXPR_TYPES.items() if found is expr_class}
        for expr_class in ExprType
    }
    assert by_class == {
        NUM: {"int", "qty", "price", "money", "rate"},
        STR: {"string", "text", "enum", "ref"},
        DATE: {"date"},
        PERIOD: {"period"},
        BOOL: {"bool"},
    }


@pytest.mark.req("10.11")
def test_a_ref_field_is_of_the_string_class() -> None:
    assert FieldInfo(STR, "supplier") == FieldInfo(STR, ref="supplier")
    assert FieldInfo(NUM).ref is None
    with pytest.raises(ValueError, match="string class"):
        FieldInfo(NUM, "supplier")
    with pytest.raises(ValueError, match="a-z"):
        FieldInfo(STR, "Supplier")


@pytest.mark.req("10.11")
@pytest.mark.parametrize(
    "text",
    [
        "line.qty > 0 and line.price >= 0",
        "header.delivery_date >= header.biz_date",
        "header.period == context.period or header.closed",
        "not (header.status in ['approved', 'closed'])",
        "sum(lines.amount) == header.total_amount",
        "count(lines) <= 200",
        "exists(supplier, code == header.supplier.code and status == 'enabled')",
        "sum_of(stock_balance.qty, material_id == line.material_id) >= line.qty_base",
        "line.material.kind != 'raw' or line.qty_base > 0",
        "context.user.role == 'buyer'",
        "header.total_amount / 2 + 1 * 3 > 0",
    ],
)
def test_accepts_well_typed_expressions(text: str) -> None:
    assert check(text) is BOOL


@pytest.mark.req("10.11")
@pytest.mark.parametrize(
    ("text", "marker", "reason"),
    [
        # == and != take two operands of one class.
        ("header.total_amount == 'x'", "==", "must be of one type, got number and string"),
        ("header.biz_date != header.period", "!=", "must be of one type, got date and period"),
        # <, <=, > and >= take two numbers, dates, periods or strings.
        ("header.period < header.biz_date", "<", "must be of one type, got period and date"),
        ("header.closed >= header.closed", ">=", "must be number, date, period or string"),
        # in takes a number or string on the left and list items of that class.
        ("header.biz_date in ['2026-01-01']", "in [", "must be number or string, got date"),
        ("header.notes in [1, 2]", "in [", "must be string, like the left operand, got number"),
        ("line.qty in [1, 'a']", "in [", "must be number, like the left operand, got string"),
        # Arithmetic takes numbers on both sides.
        ("line.remark * 2 > 0", "*", "the left operand of * must be number, got string"),
        ("line.qty + header.notes > 0", "+", "the right operand of + must be number, got string"),
        ("header.biz_date - 1 > 0", "-", "the left operand of - must be number, got date"),
        # sum and sum_of take a number field.
        ("sum(lines.remark) > 0", "sum", "the field of sum must be number, got string"),
        ("sum_of(supplier.code, enabled) > 0", "sum_of", "the field of sum_of must be number"),
        # and, or, not take boolean operands, exists and sum_of a boolean condition.
        ("line.qty and header.closed", "and", "the left operand of and must be boolean"),
        ("header.closed or line.qty", "or", "the right operand of or must be boolean, got number"),
        ("not line.qty", "not", "the operand of not must be boolean, got number"),
        ("exists(supplier, credit)", "exists", "the condition of exists must be boolean"),
        (
            "sum_of(supplier.credit, credit) > 0",
            "sum_of",
            "the condition of sum_of must be boolean",
        ),
    ],
)
def test_operator_type_errors(text: str, marker: str, reason: str) -> None:
    error = error_of(text)
    assert error.phase == "operators"
    assert error.position == position_of(text, marker)
    assert reason in error.reason


@pytest.mark.req("10.11")
@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("header.total_amount", "number"),
        ("header.notes", "string"),
        ("context.today", "date"),
        ("header.period", "period"),
        ("sum(lines.qty)", "number"),
    ],
)
def test_a_check_must_be_boolean(text: str, found: str) -> None:
    error = error_of(text)
    assert error.phase == "result"
    # Requirement 10.2: the position of a result type error is 1.
    assert error.position == 1
    assert error.reason == f"the expression must be boolean, got {found}"
    # The same expression has a type; only check_expr demands a boolean one.
    assert type_of(text) == found


# --- names that do not resolve (requirement 10.2) ----------------------------------------


@pytest.mark.req("10.2")
@pytest.mark.parametrize(
    ("text", "marker", "reason"),
    [
        # A field that the level does not declare.
        ("header.nope == 'x'", "header.nope", "no field 'nope' in header"),
        ("line.nope > 0", "line.nope", "no field 'nope' in line"),
        ("context.user.nope == 'x'", "context.user.nope", "no field 'nope' in context.user"),
        ("sum(lines.nope) > 0", "lines.nope", "no field 'nope' in lines"),
        ("sum_of(supplier.nope, enabled) > 0", "supplier.nope", "no field 'nope' in supplier"),
        ("exists(supplier, nope == 'x')", "nope", "no field 'nope' in supplier"),
        # A field of the referenced record that it does not declare.
        ("header.supplier.nope == 'x'", "header.supplier.nope", "no field 'nope' in supplier"),
        ("line.material.unit.nope > 0", "line.material.unit.nope", "no field 'nope' in unit"),
        # Expanding a field that is not a ref field.
        ("header.notes.kind == 'x'", "header.notes.kind", "'notes' in header is not a ref field"),
        ("line.qty.kind == 'x'", "line.qty.kind", "'qty' in line is not a ref field"),
        ("header.nothing.kind == 'x'", "header.nothing.kind", "no ref field nothing_id in header"),
        # Expanding over a field named like a ref field that is not one.
        (
            "header.legacy.kind == 'x'",
            "header.legacy.kind",
            "field legacy_id in header is not a ref field",
        ),
        # A name that does not resolve is reported for an operand of in as well, and the
        # type of the list it is compared with is then left alone (requirement 10.11).
        ("header.nope in ['a', 'b']", "header.nope", "no field 'nope' in header"),
        # An entity or bill name that does not exist.
        ("exists(nope, code == 'x')", "nope", "no entity or bill named 'nope'"),
        ("sum_of(nope.qty, code == 'x') > 0", "nope.qty", "no entity or bill named 'nope'"),
        ("header.ghost.name == 'x'", "header.ghost.name", "'ghost', which is no entity or bill"),
        # An unprefixed path outside an exists or sum_of condition.
        ("status == 'approved'", "status", "only in an exists or sum_of condition"),
        ("exists(supplier, enabled) and status == 'x'", "status", "only in an exists or sum_of"),
        ("sum_of(supplier.credit, enabled) > amount", "amount", "only in an exists or sum_of"),
    ],
)
def test_name_errors(text: str, marker: str, reason: str) -> None:
    error = error_of(text)
    assert error.phase == "names"
    assert error.position == position_of(text, marker)
    assert reason in error.reason


@pytest.mark.req("10.2")
def test_line_paths_need_a_line() -> None:
    text = "line.qty > 0"
    error = error_of(text, HEADER_SCOPE)
    assert error.phase == "names"
    assert error.position == position_of(text, "line.qty")
    assert "not bill lines" in error.reason
    # The lines of the bill the record belongs to can still be summed.
    assert check("sum(lines.qty) > 0", HEADER_SCOPE) is BOOL
    assert check("count(lines) > 0", HEADER_SCOPE) is BOOL
    # A line path inside a condition names the current line too, so it needs one as well.
    assert check("exists(supplier, line.qty > 0)") is BOOL
    assert error_of("exists(supplier, line.qty > 0)", HEADER_SCOPE).phase == "names"


@pytest.mark.req("10.2")
@pytest.mark.parametrize(
    ("text", "marker"), [("sum(lines.qty) > 0", "lines.qty"), ("count(lines) > 0", "lines")]
)
def test_lines_need_a_bill(text: str, marker: str) -> None:
    error = error_of(text, ENTITY_SCOPE)
    assert error.phase == "names"
    assert error.position == position_of(text, marker)
    assert "belong to no bill" in error.reason
    # header. names the record of the query's source, so it keeps resolving.
    assert check("header.total_amount > 0", ENTITY_SCOPE) is BOOL


@pytest.mark.req("10.2")
def test_a_condition_resolves_unprefixed_paths_in_its_target() -> None:
    assert check("exists(supplier, code == header.supplier.code)") is BOOL
    assert check("sum_of(stock_balance.qty, material_id == line.material_id) > 0") is BOOL
    # A nested condition takes the inner target: kind is a field of material, not supplier.
    assert check("exists(supplier, exists(material, kind == 'raw'))") is BOOL
    error = error_of("exists(material, exists(supplier, kind == 'raw'))")
    assert error.phase == "names"
    assert "no field 'kind' in supplier" in error.reason


@pytest.mark.req("10.2")
def test_an_unknown_target_hides_the_fields_of_its_condition() -> None:
    text = "exists(nope, whatever == other)"
    error = error_of(text)
    assert error.phase == "names"
    assert error.position == position_of(text, "nope")
    assert error.reason == "no entity or bill named 'nope'"


# --- order of the checks and of the positions (requirement 10.2) -------------------------


@pytest.mark.req("10.2")
def test_reports_the_earliest_error_of_the_phase() -> None:
    text = "header.nope == line.nope"
    error = error_of(text)
    assert error.phase == "names"
    assert error.position == position_of(text, "header.nope")
    assert "no field 'nope' in header" in error.reason


@pytest.mark.req("10.2")
def test_names_are_reported_before_operator_types() -> None:
    # The operator error at + comes first in the text, the names error wins all the same.
    text = "1 + 'x' == header.nope"
    error = error_of(text)
    assert error.phase == "names"
    assert error.position == position_of(text, "header.nope")


@pytest.mark.req("10.2")
def test_operator_types_are_reported_before_the_result_type() -> None:
    # The expression is a number, so the result check would fail too.
    text = "header.total_amount + 'x'"
    error = error_of(text)
    assert error.phase == "operators"
    assert error.position == position_of(text, "+")


@pytest.mark.req("10.2")
def test_a_syntax_error_is_left_to_the_parser() -> None:
    with pytest.raises(ExprError) as info:
        parse_expr("header.nope >")
    assert info.value.phase == "syntax"
