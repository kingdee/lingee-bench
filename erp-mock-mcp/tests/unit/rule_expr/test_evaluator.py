"""erp_mock.domain.rule_expr.evaluator: values, three-valued logic and the rule outcome.

The env below stands in for the one the engine builds from the sandbox: a purchase order
with three lines, a current user and a few entity tables, so that every form of
requirement 10.1 has a value. Its fields hold one value of each class and, for the
three-valued logic, a true, a false and a null boolean.

Every expression in this module is well typed in that shape, as requirement 10.11 asks:
the checker, not the evaluator, rejects the others.
"""

import dataclasses
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

import pytest

from erp_mock.domain.rule_expr.evaluator import (
    CheckResult,
    EvalEnv,
    Value,
    eval_check,
    eval_expr,
    references_line,
)
from erp_mock.domain.rule_expr.parser import parse_expr

# The anchor date: context.today, and context.period is its month.
ANCHOR: Final = date(2026, 3, 18)


@dataclass(frozen=True)
class FakeRecord:
    """A stand-in Record: the value of each field, and what each ref field refers to.

    Both lookups assert that the name is declared, so an expression that reads a field
    the record does not have fails the test instead of passing silently.
    """

    values: Mapping[str, Value]
    refs: Mapping[str, "FakeRecord | None"] = dataclasses.field(default_factory=dict)

    def field(self, name: str) -> Value:
        assert name in self.values, f"no field {name!r} in this record"
        return self.values[name]

    def ref(self, name: str) -> "FakeRecord | None":
        assert name in self.refs, f"no ref field {name!r} in this record"
        return self.refs[name]


@dataclass(frozen=True)
class FakeEnv:
    """A stand-in EvalEnv; ``reads`` counts how often each target table was read."""

    header: FakeRecord
    lines: tuple[FakeRecord, ...] = ()
    user: FakeRecord | None = None
    tables: Mapping[str, tuple[FakeRecord, ...]] = dataclasses.field(default_factory=dict)
    today: date = ANCHOR
    reads: Counter[str] = dataclasses.field(default_factory=Counter)

    def records(self, target: str) -> tuple[FakeRecord, ...]:
        assert target in self.tables, f"no table {target!r}"
        self.reads[target] += 1
        return self.tables[target]


UNIT_PCS: Final = FakeRecord({"code": "PCS", "precision": Decimal("2")})
# Material A has a unit, material B's unit_id is null, so a path through it is null.
MATERIAL_A: Final = FakeRecord(
    {"code": "MA", "kind": "raw", "tax_rate": Decimal("0.13"), "unit_id": "unit_pcs"},
    {"unit_id": UNIT_PCS},
)
MATERIAL_B: Final = FakeRecord(
    {"code": "MB", "kind": "semi", "tax_rate": Decimal("0.09"), "unit_id": None},
    {"unit_id": None},
)
SUPPLIER_1: Final = FakeRecord({"code": "S1", "enabled": True, "credit": Decimal("100.00")})
SUPPLIER_2: Final = FakeRecord({"code": "S2", "enabled": False, "credit": None})
ORG_HQ: Final = FakeRecord({"name": "hq"})
USER: Final = FakeRecord({"role": "buyer", "org_id": "org_hq"}, {"org_id": ORG_HQ})

HEADER: Final = FakeRecord(
    {
        "number": "PO-0001",
        "status": "approved",
        "biz_date": date(2026, 2, 10),
        "period": "2026-02",
        "total_amount": Decimal("300.00"),
        "discount": Decimal("0.1000"),
        "note": None,
        "approved": True,
        "closed": False,
        "urgent": None,
        "supplier_id": "sup_1",
        "buyer_id": None,
    },
    {"supplier_id": SUPPLIER_1, "buyer_id": None},
)
# Three lines: the amounts add up to the header total, line 2 has a null amount and line 3
# a qty of 0, so a check on the lines can fail on one line or on two.
LINES: Final = (
    FakeRecord(
        {
            "qty": Decimal("2"),
            "price": Decimal("10.5"),
            "amount": Decimal("21.00"),
            "material_id": "mat_a",
        },
        {"material_id": MATERIAL_A},
    ),
    FakeRecord(
        {"qty": Decimal("1"), "price": Decimal("8"), "amount": None, "material_id": "mat_b"},
        {"material_id": MATERIAL_B},
    ),
    FakeRecord(
        {
            "qty": Decimal("0"),
            "price": Decimal("0"),
            "amount": Decimal("279.00"),
            "material_id": "mat_a",
        },
        {"material_id": MATERIAL_A},
    ),
)
STOCK: Final = (
    FakeRecord({"material_id": "mat_a", "qty": Decimal("5")}),
    FakeRecord({"material_id": "mat_a", "qty": Decimal("2")}),
    FakeRecord({"material_id": "mat_b", "qty": None}),
)
TABLES: Final[Mapping[str, tuple[FakeRecord, ...]]] = {
    "supplier": (SUPPLIER_1, SUPPLIER_2),
    "material": (MATERIAL_A, MATERIAL_B),
    "stock_balance": STOCK,
    "warehouse": (),
}

# The truth values of the three-valued logic, as a boolean header field of each.
TRUTH: Final[Mapping[bool | None, str]] = {
    True: "header.approved",
    False: "header.closed",
    None: "header.urgent",
}


def bill_env(lines: tuple[FakeRecord, ...] = LINES, user: FakeRecord | None = USER) -> FakeEnv:
    """A fresh env for the purchase order, with its own read counter."""
    return FakeEnv(header=HEADER, lines=lines, user=user, tables=TABLES)


ENV: Final = bill_env()


def value_of(text: str, env: EvalEnv = ENV, line: int | None = None) -> Value:
    """The value of ``text`` in ``env``; ``line`` is the 1-based line to evaluate it on."""
    current = None if line is None else env.lines[line - 1]
    return eval_expr(parse_expr(text), env, current)


def check_of(text: str, env: EvalEnv = ENV) -> CheckResult:
    """Decide the rule whose check is ``text``."""
    return eval_check(parse_expr(text), env)


# --- the value of a path and of the context (requirements 10.5, 10.6) --------------------


@pytest.mark.req("10.5")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # header.<field>: one field of each class, the ref field itself included.
        ("header.number", "PO-0001"),
        ("header.total_amount", Decimal("300.00")),
        ("header.biz_date", date(2026, 2, 10)),
        ("header.period", "2026-02"),
        ("header.approved", True),
        ("header.supplier_id", "sup_1"),
        # line.<field>: the line the expression is evaluated on.
        ("line.qty", Decimal("2")),
        ("line.price", Decimal("10.5")),
        # A ref field expands to the referenced record, through any number of refs.
        ("header.supplier.code", "S1"),
        ("line.material.kind", "raw"),
        ("line.material.unit.code", "PCS"),
        # The context: the anchor date, its month and the current user's record.
        ("context.today", ANCHOR),
        ("context.period", "2026-03"),
        ("context.user.role", "buyer"),
        ("context.user.org.name", "hq"),
    ],
)
def test_reads_every_form_of_path(text: str, expected: Value) -> None:
    assert value_of(text, line=1) == expected


@pytest.mark.req("10.6")
@pytest.mark.parametrize(
    ("text", "line"),
    [
        # A field whose value is null.
        ("header.note", None),
        ("line.amount", 2),
        # A path through a ref field whose value is null, at the first and a later segment.
        ("header.buyer.name", None),
        ("line.material.unit.code", 2),
        # Arithmetic with a null operand, on either side.
        ("header.total_amount + line.amount", 2),
        ("line.amount * 2", 2),
        # A division whose divisor is zero, from a field and from a literal.
        ("header.total_amount / line.qty", 3),
        ("header.total_amount / 0", None),
        # A comparison or in with a null operand is unknown.
        ("line.amount > 0", 2),
        ("header.note == 'x'", None),
        ("header.note != 'x'", None),
        ("header.note in ['x']", None),
    ],
)
def test_null_spreads_through_paths_arithmetic_and_comparison(text: str, line: int | None) -> None:
    assert value_of(text, line=line) is None


@pytest.mark.req("10.6")
def test_without_a_current_user_every_user_path_is_null() -> None:
    env = bill_env(user=None)
    assert value_of("context.user.role", env) is None
    assert value_of("context.user.org.name", env) is None
    # Unknown, not false: such a rule is satisfied.
    assert value_of("context.user.role == 'buyer'", env) is None
    assert check_of("context.user.role == 'buyer'", env).satisfied is True


@pytest.mark.req("10.5")
def test_a_line_path_has_no_value_without_a_current_line() -> None:
    with pytest.raises(ValueError, match="no current line"):
        eval_expr(parse_expr("line.qty > 0"), ENV)


@pytest.mark.req("10.13")
def test_an_unprefixed_path_has_no_value_outside_a_condition() -> None:
    with pytest.raises(ValueError, match="exists or sum_of"):
        eval_expr(parse_expr("credit > 0"), ENV)


# --- three-valued logic (requirement 10.6) ----------------------------------------------


@pytest.mark.req("10.6")
@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (True, None, None),
        (False, True, False),
        (False, False, False),
        # False on either side decides, whatever the other side is.
        (False, None, False),
        (None, True, None),
        (None, False, False),
        (None, None, None),
    ],
)
def test_kleene_and(left: bool | None, right: bool | None, expected: bool | None) -> None:
    assert value_of(f"{TRUTH[left]} and {TRUTH[right]}") is expected


@pytest.mark.req("10.6")
@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (True, True, True),
        (True, False, True),
        # True on either side decides, whatever the other side is.
        (True, None, True),
        (False, True, True),
        (False, False, False),
        (False, None, None),
        (None, True, True),
        (None, False, None),
        (None, None, None),
    ],
)
def test_kleene_or(left: bool | None, right: bool | None, expected: bool | None) -> None:
    assert value_of(f"{TRUTH[left]} or {TRUTH[right]}") is expected


@pytest.mark.req("10.6")
@pytest.mark.parametrize(("operand", "expected"), [(True, False), (False, True), (None, None)])
def test_kleene_not(operand: bool | None, expected: bool | None) -> None:
    assert value_of(f"not {TRUTH[operand]}") is expected


# --- the operations on values that are not null (requirement 10.12) ---------------------


@pytest.mark.req("10.12")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("line.qty * line.price", Decimal("21")),
        ("header.total_amount - 1", Decimal("299")),
        ("line.qty + line.price", Decimal("12.5")),
        # 28 significant digits, and no rounding to the precision of a field.
        ("1 / 3", Decimal("0." + "3" * 28)),
        ("2 / 3", Decimal("0." + "6" * 27 + "7")),
        ("1 / 8", Decimal("0.125")),
        ("line.material.tax_rate / 3", Decimal("0.04" + "3" * 27)),
        # Left associative, and * and / bind tighter than + and -.
        ("10 - 3 - 2", Decimal("5")),
        ("2 + 3 * 4", Decimal("14")),
        ("(2 + 3) * 4", Decimal("20")),
    ],
)
def test_arithmetic_runs_in_the_rule_context(text: str, expected: Decimal) -> None:
    assert value_of(text, line=1) == expected


@pytest.mark.req("10.12")
def test_arithmetic_rounds_half_up() -> None:
    # 29 significant digits: what is dropped is exactly half of the last kept place, so
    # the result moves away from zero. ROUND_HALF_EVEN would keep the even digit instead.
    tie = "1." + "0" * 27 + "5"
    assert value_of(f"{tie} + 0") == Decimal("1." + "0" * 26 + "1")


@pytest.mark.req("10.12")
def test_numbers_compare_by_magnitude() -> None:
    # 0.1000 equals 0.1 and 300.00 equals 300: the number of decimals does not matter.
    assert value_of("header.discount == 0.1") is True
    assert value_of("0.10 != 0.1") is False
    assert value_of("header.total_amount >= 300 and header.total_amount <= 300") is True
    assert value_of("header.discount > 0.09") is True


@pytest.mark.req("10.12")
def test_dates_and_periods_compare_chronologically() -> None:
    assert value_of("header.biz_date < context.today") is True
    assert value_of("header.biz_date == context.today") is False
    assert value_of("context.today > header.biz_date") is True
    assert value_of("header.period < context.period") is True
    assert value_of("header.period == context.period") is False


@pytest.mark.req("10.12")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Upper case sorts before lower case, and case tells two strings apart.
        ("'Z' < 'a'", True),
        ("'a' < 'A'", False),
        ("header.status == 'Approved'", False),
        ("header.status == 'approved'", True),
        # Code point order, not a locale's: U+4E01 comes after U+4E00.
        ("'丁' > '一'", True),
        # A prefix sorts before a longer string.
        ("'ab' < 'b'", True),
        ("header.supplier.code < 'S2'", True),
    ],
)
def test_strings_compare_by_code_point(text: str, expected: bool) -> None:
    assert value_of(text) is expected


@pytest.mark.req("10.12")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("header.status in ['approved', 'closed']", True),
        ("header.status in ['draft']", False),
        ("line.qty in [1, 2]", True),
        ("line.qty in [0.5]", False),
        # Membership compares numbers by magnitude too.
        ("header.total_amount in [300]", True),
    ],
)
def test_in_is_membership_in_the_list(text: str, expected: bool) -> None:
    assert value_of(text, line=1) is expected


# --- the functions (requirement 10.13) --------------------------------------------------


@pytest.mark.req("10.13")
def test_sum_and_count_read_the_lines_of_the_bill() -> None:
    # Line 2's amount is null and is skipped; 21.00 + 279.00 is the header total.
    assert value_of("sum(lines.amount)") == Decimal("300.00")
    assert value_of("sum(lines.amount) == header.total_amount") is True
    assert value_of("sum(lines.qty)") == Decimal("3")
    assert value_of("count(lines)") == Decimal("3")
    # The whole bill is read, whichever line the expression is evaluated on.
    assert value_of("sum(lines.qty)", line=3) == Decimal("3")


@pytest.mark.req("10.13")
def test_a_bill_without_lines_sums_to_zero_and_counts_zero() -> None:
    env = bill_env(lines=())
    assert value_of("sum(lines.amount)", env) == Decimal("0")
    assert value_of("count(lines)", env) == Decimal("0")


@pytest.mark.req("10.13")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # True for at least one record.
        ("exists(supplier, enabled)", True),
        ("exists(supplier, not enabled)", True),
        ("exists(supplier, credit > 50)", True),
        # No record makes it true; supplier 2's credit is null, which does not count.
        ("exists(supplier, credit > 500)", False),
        ("exists(supplier, code == 'S3')", False),
        # A table without records.
        ("exists(warehouse, code == 'W1')", False),
    ],
)
def test_exists_is_true_only_where_a_record_makes_the_condition_true(
    text: str, expected: bool
) -> None:
    assert value_of(text) is expected


@pytest.mark.req("10.13")
def test_sum_of_adds_the_field_over_the_matching_records() -> None:
    total = "sum_of(stock_balance.qty, material_id == line.material_id)"
    # Material A has 5 and 2 in stock.
    assert value_of(total, line=1) == Decimal("7")
    # Material B's only record has a null qty, which is skipped, so nothing is left.
    assert value_of(total, line=2) == Decimal("0")
    assert value_of("sum_of(stock_balance.qty, material_id == 'nope')") == Decimal("0")
    # Only records whose condition is true are added: supplier 2 is not enabled.
    assert value_of("sum_of(supplier.credit, enabled)") == Decimal("100.00")
    assert value_of("sum_of(warehouse.qty, code == 'W1')") == Decimal("0")


@pytest.mark.req("10.13")
def test_a_condition_reads_the_examined_record_and_the_evaluated_object() -> None:
    # An unprefixed path is a field of the record being examined.
    assert value_of("exists(supplier, credit == 100)") is True
    # header., line. and context. paths still name the object being evaluated.
    assert value_of("exists(supplier, credit > line.qty)", line=1) is True
    assert value_of("exists(supplier, credit < line.qty)", line=1) is False
    assert value_of("exists(supplier, code == header.number)") is False
    assert value_of("exists(supplier, enabled and context.today > header.biz_date)") is True


@pytest.mark.req("10.13")
def test_a_nested_condition_reads_the_inner_target() -> None:
    assert value_of("exists(supplier, exists(material, kind == 'raw'))") is True
    # code is the material's here, so no record matches the supplier code.
    assert value_of("exists(material, exists(supplier, code == 'MA'))") is False
    assert value_of("exists(material, exists(supplier, code == 'S1'))") is True


@pytest.mark.req("10.13")
def test_each_target_table_is_read_once_per_check() -> None:
    env = bill_env()
    # A check on the lines is evaluated three times and names the table twice.
    text = "exists(supplier, credit > line.qty) and sum_of(supplier.credit, enabled) > 0"
    assert eval_check(parse_expr(text), env).per_line is True
    assert env.reads == Counter({"supplier": 1})


@pytest.mark.req("10.13")
def test_another_evaluation_reads_the_table_again() -> None:
    env = bill_env()
    eval_expr(parse_expr("exists(supplier, enabled)"), env)
    eval_expr(parse_expr("exists(supplier, enabled)"), env)
    assert env.reads == Counter({"supplier": 2})


# --- the outcome of one rule (requirement 10.7) -----------------------------------------


@pytest.mark.req("10.7")
@pytest.mark.parametrize(
    ("text", "satisfied"),
    [
        ("header.total_amount > 0", True),
        ("sum(lines.amount) == header.total_amount", True),
        ("header.approved", True),
        ("count(lines) <= 200", True),
        ("header.closed", False),
        ("count(lines) > 5", False),
        ("header.status in ['draft']", False),
        # Unknown counts as satisfied, as an SQL CHECK constraint does.
        ("header.urgent", True),
        ("header.note == 'x'", True),
        ("context.user.role == 'buyer' and header.note == 'x'", True),
    ],
)
def test_a_check_that_names_no_line_is_decided_once(text: str, satisfied: bool) -> None:
    assert check_of(text) == CheckResult(satisfied=satisfied, per_line=False)


@pytest.mark.req("10.7")
@pytest.mark.parametrize(
    ("text", "failing"),
    [
        # Line 3's qty is 0.
        ("line.qty > 0", (3,)),
        # Lines 2 and 3, reported in line number order.
        ("line.qty > 1", (2, 3)),
        ("line.qty >= 0", ()),
        # Line 2's amount is null, so its value is unknown, which satisfies the rule.
        ("line.amount > 0", ()),
        # Line 2's material is not raw.
        ("line.material.kind == 'raw'", (2,)),
        # Material B has no stock left, so line 2's qty is not covered.
        ("sum_of(stock_balance.qty, material_id == line.material_id) >= line.qty", (2,)),
        ("exists(supplier, credit > line.qty)", ()),
    ],
)
def test_a_check_that_names_a_line_is_decided_per_line(text: str, failing: tuple[int, ...]) -> None:
    expected = CheckResult(satisfied=not failing, per_line=True, failing_lines=failing)
    assert check_of(text) == expected


@pytest.mark.req("10.7")
def test_a_bill_without_lines_satisfies_a_check_on_the_lines() -> None:
    env = bill_env(lines=())
    assert check_of("line.qty > 0", env) == CheckResult(satisfied=True, per_line=True)
    # The expression is never evaluated, so no table is read.
    assert check_of("exists(supplier, credit > line.qty)", env).satisfied is True
    assert env.reads == Counter()


@pytest.mark.req("10.5", "10.7")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("line.qty > 0", True),
        ("not (line.qty > 0)", True),
        ("header.total_amount > line.qty", True),
        ("line.qty in [1, 2]", True),
        # A line path inside a function argument or a condition counts too.
        ("exists(supplier, credit > line.qty)", True),
        ("sum_of(supplier.credit, credit > line.qty) > 0", True),
        # lines reads the whole bill, so sum and count do not reference line.
        ("sum(lines.qty) > 0", False),
        ("count(lines) > 0", False),
        ("header.approved", False),
        ("exists(supplier, enabled)", False),
        ("context.today > header.biz_date", False),
    ],
)
def test_references_line_decides_how_a_check_is_evaluated(text: str, expected: bool) -> None:
    expr = parse_expr(text)
    assert references_line(expr) is expected
    assert eval_check(expr, ENV).per_line is expected


@pytest.mark.req("10.7")
def test_a_check_result_holds_only_a_consistent_outcome() -> None:
    assert CheckResult(satisfied=True, per_line=False).failing_lines == ()
    with pytest.raises(ValueError, match="references line"):
        CheckResult(satisfied=False, per_line=False, failing_lines=(1,))
    with pytest.raises(ValueError, match="ascending order"):
        CheckResult(satisfied=False, per_line=True, failing_lines=(2, 1))
    with pytest.raises(ValueError, match="line numbers start at 1"):
        CheckResult(satisfied=False, per_line=True, failing_lines=(0,))
    with pytest.raises(ValueError, match="no line fails"):
        CheckResult(satisfied=True, per_line=True, failing_lines=(1,))
