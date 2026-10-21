"""erp_mock.domain.rules: the business rules step (requirements 10.5, 10.7 to 10.9, 12.11).

The tests run against a real sandbox built by :mod:`tests.helpers.engine_env`: an empty
database over the tables of the engine test fixture with one standard set of basic data,
every record of it created through the engine. The bills are created, submitted, pushed and
deleted through the engine as well, so the rules step runs exactly where a tools/call runs
it - step 7 of one operation, one transaction - and what the tests read is the error that
ended the call.

The fixture declares three enforced rules and one described one
(tests/fixtures/packs/fx_trade/rules): ``R-FXT-001`` asks the two warehouses of a stock
move to differ, ``R-FXT-003`` keeps the quantity of one goods entry line at 1000 or less
and ``R-FXT-004`` the quantities of one goods entry at 2000 or less in total, while
``R-FXT-002`` is text the model reads and nothing the engine evaluates. The two rules of
the goods entry are declared in the opposite order of their numbers and the ``on`` of both
is create, update and submit, so one bill carries a rule that names a ``line.`` path and
one that does not, with a quantity that breaks either, both or neither. The header of a
goods entry also carries the unique ``entry_ref``, which is how one call breaks a rule and
a unique value at once (requirement 12.11).

What the tests pin:

- the order of the checks: an engine rule is reported before a pack rule, and the pack
  rules are checked by rule id in code point order rather than in the order the packs
  declared them, the first one that does not hold ending the call (requirement 10.8);
- the error: the ``error_code``, the id and the ``message.zh`` of the rule, with one detail
  item for a check that names no ``line.`` path, none at all where the rule declares no
  field, and one per failing line, by line number, for a check that does (requirement 10.7);
- which rules apply: the bill and the ``on`` of the rule, the ``create`` of an ``on``
  covering the push that generates the bill, while what a writeback and an automatic close
  change on a *source* bill is no operation on it, and a described rule is never evaluated
  (requirement 10.9);
- what a check reads: the candidate of a create, a push and an update, the stored bill with
  the state the operation writes for a state operation, and the bill as it stands for a
  delete (requirement 10.5);
- the uniqueness check that follows: a call that breaks a rule and a unique value at once
  reports the rule (requirements 5.8 and 12.11).

A rule the fixture does not declare - one on the source bill of a conversion, one with
``delete`` in its ``on``, one that reads ``header.status`` - is added to a copy of the
resolved model and run through an engine of its own over the same sandbox
(:meth:`~tests.helpers.engine_env.EngineEnv.run_with`): such a model has the tables of the
fixture, so only the rules of the instance differ.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.base import Label
from erp_mock.config.model import LINE_SUFFIX, DomainModel, FieldRef, RuleModel
from erp_mock.config.pack import RuleErrorCode, WriteOperation
from erp_mock.domain.engine import CreateOp, DeleteOp, Engine, TransitionOp
from erp_mock.domain.rules import (
    RULE_CHECK_FAILED_REASON,
    RuleStep,
    applicable_rules,
    check_rules,
    evaluated_bill,
    rule_applies,
    rule_checks,
)
from erp_mock.domain.state_machine import (
    APPROVED,
    CLOSED,
    DRAFT,
    REQUIRED_MISSING_REASON,
    REQUIRED_RULE,
    STATUS,
    SUBMITTED,
)
from erp_mock.state.schema import BILL_ID_COLUMN, ID_COLUMN
from erp_mock.state.store import Record, Value
from tests.helpers.engine_env import (
    MAIN_DEPOT,
    MAIN_VENDOR,
    SPARE_DEPOT,
    WASHER,
    EngineEnv,
    arguments,
    as_record,
    engine_env,
    line_arguments,
    record_lines,
)

# The bills of the fixture a test names, the lines of one of them and the conversion
# between two of them.
ENTRY: Final = "goods_entry"
ENTRY_LINE: Final = f"{ENTRY}{LINE_SUFFIX}"
MOVE: Final = "stock_move"
ORDER: Final = "supply_order"
ORDER_LINE: Final = f"{ORDER}{LINE_SUFFIX}"
ORDER_TO_ENTRY: Final = "supply_order_to_entry"

# The short name the push tool of that conversion names its source bill under (req 11.13).
ORDER_SHORT: Final = "order"

# The rules of the fixture (tests/fixtures/packs/fx_trade/rules): the two warehouses of a
# stock move differ, one goods entry line stays at 1000 or less, one goods entry stays at
# 2000 or less in total, and the described rule the engine never evaluates.
DEPOTS_RULE: Final = "R-FXT-001"
DESCRIBED_RULE: Final = "R-FXT-002"
LINE_QTY_RULE: Final = "R-FXT-003"
TOTAL_QTY_RULE: Final = "R-FXT-004"
LINE_QTY_MESSAGE: Final = "单行收料数量不得超过 1000"
TOTAL_QTY_MESSAGE: Final = "一张收料单的收料数量合计不得超过 2000"

# The id of a rule no business pack of the fixture declares, which a test adds to a copy of
# the resolved model; it sorts after every rule of the fixture, so it is checked last.
ADDED_RULE: Final = "R-FXT-005"

# The fields of the bills a test writes and reads.
VENDOR: Final = "vendor_id"
ENTRY_REF: Final = "entry_ref"
ARTICLE: Final = "article_id"
DEPOT: Final = "depot_id"
ENTRY_QTY: Final = "entry_qty"
MOVE_QTY: Final = "move_qty"
FROM_DEPOT: Final = "from_depot_id"
TO_DEPOT: Final = "to_depot_id"
LINKED_QTY: Final = "linked_qty"
SOURCE_LINE_ID: Final = "source_line_id"

# The operations the ``on`` of the two goods entry rules lists, and the ones it does not.
CHECKED: Final[tuple[WriteOperation, ...]] = ("create", "update", "submit")
UNCHECKED: Final[tuple[WriteOperation, ...]] = (
    "delete",
    "unsubmit",
    "approve",
    "unapprove",
    "close",
    "unclose",
)

# The day after the anchor date: the required date of a supply order.
NEXT_DAY: Final = date(2026, 10, 10)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


# --- building the bills -------------------------------------------------------------------


def entry_line(env: EngineEnv, qty: str, depot: str = MAIN_DEPOT) -> dict[str, Value]:
    """One line of a goods entry: ``qty`` of the washer, which is kept in its base unit."""
    return {ARTICLE: env.id(WASHER), DEPOT: env.id(depot), ENTRY_QTY: Decimal(qty)}


def entry_header(env: EngineEnv, entry_ref: str | None = None) -> dict[str, Value]:
    """The header of a goods entry: its vendor and, where a test needs one, its reference."""
    header: dict[str, Value] = {VENDOR: env.id(MAIN_VENDOR)}
    if entry_ref is not None:
        header[ENTRY_REF] = entry_ref
    return header


def create_entry(
    env: EngineEnv, quantities: Sequence[str] = ("10",), entry_ref: str | None = None
) -> CreateOp:
    """The create of a goods entry with one line per quantity (requirement 5.1)."""
    return CreateOp(
        ENTRY,
        arguments(entry_header(env, entry_ref)),
        line_arguments([entry_line(env, qty) for qty in quantities]),
    )


def new_entry(
    env: EngineEnv, quantities: Sequence[str] = ("10",), entry_ref: str | None = None
) -> str:
    """Create a goods entry draft and return its id (requirement 5.1)."""
    return record_id(env.run(create_entry(env, quantities, entry_ref)))


def new_order(env: EngineEnv, qty: str = "10") -> str:
    """Create an approved supply order of ``qty`` washers and return its id (req 7.2)."""
    header: dict[str, Value] = {VENDOR: env.id(MAIN_VENDOR), "need_date": NEXT_DAY}
    line: dict[str, Value] = {
        ARTICLE: env.id(WASHER),
        "qty": Decimal(qty),
        "price": Decimal("0.85"),
    }
    order = record_id(env.create(ORDER, header, [line]))
    env.transition(ORDER, order, "submit")
    env.transition(ORDER, order, "approve")
    return order


def push_line(env: EngineEnv, order: str, qty: str) -> dict[str, Value]:
    """One line of a push to a goods entry: the ``required_line_inputs`` of the conversion."""
    return {
        SOURCE_LINE_ID: f"{order}-1",
        ENTRY_QTY: Decimal(qty),
        DEPOT: env.id(MAIN_DEPOT),
    }


def push_entry(env: EngineEnv, order: str, qty: str) -> str:
    """Push ``order`` to a goods entry draft of ``qty`` and return its id (req 8.1)."""
    return record_id(
        env.push(ORDER_TO_ENTRY, order, [push_line(env, order, qty)], source_name=ORDER_SHORT)
    )


def new_move(env: EngineEnv, quantities: Sequence[str] = ()) -> str:
    """Create a stock move draft between the two warehouses and return its id (req 5.1)."""
    header: dict[str, Value] = {FROM_DEPOT: env.id(MAIN_DEPOT), TO_DEPOT: env.id(SPARE_DEPOT)}
    lines: list[dict[str, Value]] = [
        {ARTICLE: env.id(WASHER), MOVE_QTY: Decimal(qty)} for qty in quantities
    ]
    return record_id(env.create(MOVE, header, lines))


# --- reading the sandbox and the errors --------------------------------------------------


def record_id(result: object) -> str:
    """The internal id of the record a write operation returned (requirement 5.16)."""
    assert hasattr(result, "view")
    return str(as_record(result.view).fields[ID_COLUMN])


def stored(env: EngineEnv, table: str, record: str) -> Record:
    """The record ``record`` of ``table`` as storage holds it; it has to be there."""
    found = env.record(table, record)
    assert found is not None, f"{table} {record} is not in the sandbox"
    return found


def status_of(env: EngineEnv, bill: str, record: str) -> Value:
    """The state the bill ``record`` is in (requirement 7.1)."""
    return stored(env, bill, record)[STATUS]


def linked_qty(env: EngineEnv, order: str) -> list[Value]:
    """The link field of every line of the supply order ``order``, by line (req 8.8)."""
    return [line[LINKED_QTY] for line in env.records(ORDER_LINE) if line[BILL_ID_COLUMN] == order]


def tamper(env: EngineEnv, table: str, record: str, values: Mapping[str, Value]) -> None:
    """Write ``values`` into a stored record behind the engine's back.

    A bill no operation of the engine produces - a stock move whose warehouses are the same,
    a goods entry line over the quantity a rule allows - is how a test reaches an operation
    whose rule is already broken before the call (requirement 10.9).
    """
    with env.store.transaction() as uow:
        uow.update(table, record, values)


def details(error: ErpError) -> list[dict[str, object]]:
    """The detail items of ``error`` as plain dicts, in report order (requirement 13.6)."""
    return [dict(item) for item in error.details]


def rule_items(*paths: str) -> list[dict[str, object]]:
    """The detail items a pack rule reports for ``paths`` (design.md data model 6)."""
    return [{"field": path, "reason": RULE_CHECK_FAILED_REASON} for path in paths]


def refused(call: Callable[[], object]) -> ErpError:
    """Run ``call`` and return the error it ended in (requirement 13.6)."""
    with pytest.raises(ErpError) as raised:
        call()
    return raised.value


# --- a model with a rule the fixture does not declare ------------------------------------


def added_rule(
    bill: str,
    on: Sequence[WriteOperation],
    check: str,
    *,
    message: str,
    error_code: RuleErrorCode = "VALIDATION_FAILED",
    field: FieldRef | None = None,
) -> RuleModel:
    """One enforced rule as loading step 5 resolves it (:class:`RuleModel`)."""
    return RuleModel(
        id=ADDED_RULE,
        pack="fx_trade",
        file=f"rules/{ADDED_RULE}.yaml",
        enforcement="enforced",
        bill=bill,
        on=tuple(on),
        check=check,
        error_code=error_code,
        field=field,
        message=Label(zh=message),
    )


def with_rules(domain: DomainModel, *rules: RuleModel) -> DomainModel:
    """``domain`` with ``rules`` among the rules of its packs, replacing one by its id.

    The tables of the result are the tables of ``domain``: a rule declares no field, so a
    sandbox over the fixture serves an engine over this model as it stands.
    """
    found = {rule.id: rule for rule in domain.rules.values()}
    found.update({rule.id: rule for rule in rules})
    return replace(domain, rules=FrozenMap(found))


def engine_of(domain: DomainModel, *rules: RuleModel) -> Engine:
    """An engine over ``domain`` and ``rules``, whose checks it parses (requirement 10.2)."""
    return Engine(with_rules(domain, *rules))


# --- which rules the step holds (requirements 10.2 and 10.9) -----------------------------


@pytest.mark.req("10.9")
def test_only_enforced_rules_of_the_bill_and_the_operation_apply(env: EngineEnv) -> None:
    domain = env.domain

    # The two rules of the goods entry, by rule id and not in the order the pack declared
    # them, for each operation their ``on`` lists (requirement 10.8).
    for operation in CHECKED:
        found = applicable_rules(domain, ENTRY, operation)
        assert [rule.id for rule in found] == [LINE_QTY_RULE, TOTAL_QTY_RULE], operation
    # An operation their ``on`` does not list, and a bill they are not about.
    for operation in UNCHECKED:
        assert applicable_rules(domain, ENTRY, operation) == (), operation
    assert [rule.id for rule in applicable_rules(domain, MOVE, "create")] == [DEPOTS_RULE]
    assert applicable_rules(domain, ORDER, "create") == ()
    # The described rule applies to nothing: it names no bill, no operation and no check
    # (requirement 10.9).
    described = domain.rules[DESCRIBED_RULE]
    assert described.enforcement == "described"
    assert not any(
        rule_applies(described, bill, operation)
        for bill in (ENTRY, MOVE, ORDER)
        for operation in CHECKED + UNCHECKED
    )


@pytest.mark.req("10.2")
def test_the_engine_holds_the_parsed_check_of_every_enforced_rule(env: EngineEnv) -> None:
    checks = rule_checks(env.domain)

    # One tree per enforced rule and none for the described one, which the engine holds, so
    # no call parses a check text again.
    assert set(checks) == {DEPOTS_RULE, LINE_QTY_RULE, TOTAL_QTY_RULE}
    assert dict(env.engine.checks) == dict(checks)


# --- the order of the checks (requirement 10.8) ------------------------------------------


@pytest.mark.req("10.8")
def test_an_engine_rule_is_reported_before_a_pack_rule(env: EngineEnv) -> None:
    move = new_move(env)
    # A stock move without lines whose warehouses are the same breaks R-GEN-002 and
    # R-FXT-001 at once; no create of the engine writes one, R-FXT-001 being checked on
    # create as well (requirement 10.9).
    tamper(env, MOVE, move, {TO_DEPOT: env.id(MAIN_DEPOT)})

    error = refused(lambda: env.transition(MOVE, move, "submit"))

    # The engine rules come first, whatever the pack rules say (requirement 10.8).
    assert (error.code, error.rule_id) == (ErrorCode.VALIDATION_FAILED, REQUIRED_RULE)
    assert details(error) == [{"field": "lines", "reason": REQUIRED_MISSING_REASON}]
    assert status_of(env, MOVE, move) == DRAFT


@pytest.mark.req("10.8")
def test_pack_rules_are_checked_by_rule_id_and_not_by_model_order(env: EngineEnv) -> None:
    # The pack declares R-FXT-004 before R-FXT-003, and one line of 5000 breaks both.
    declared = [rule.id for rule in env.domain.rules.values() if rule.bill == ENTRY]
    assert declared == [TOTAL_QTY_RULE, LINE_QTY_RULE]

    error = refused(lambda: env.run(create_entry(env, ["5000"])))

    # The lower number is reported and the call stops there (requirement 10.8).
    assert (error.code, error.rule_id) == (ErrorCode.BUSINESS_RULE_VIOLATION, LINE_QTY_RULE)
    assert env.records(ENTRY) == []


# --- the error of a pack rule (requirement 10.7) -----------------------------------------


@pytest.mark.req("10.7")
def test_a_rule_that_names_a_line_reports_every_failing_line(env: EngineEnv) -> None:
    error = refused(lambda: env.run(create_entry(env, ["1500", "400", "1200"])))

    # The error code, the id and the message of the rule itself (requirements 10.7, 13.7).
    assert error.code is ErrorCode.BUSINESS_RULE_VIOLATION
    assert error.rule_id == LINE_QTY_RULE
    assert error.message == LINE_QTY_MESSAGE
    # One item per line whose value is false, by line number, at lines[i].<field> with i the
    # line number minus one; the line that holds 400 is not among them.
    assert details(error) == rule_items("lines[0].entry_qty", "lines[2].entry_qty")


@pytest.mark.req("10.7")
def test_a_rule_that_names_no_line_and_no_field_reports_no_item(env: EngineEnv) -> None:
    # Three lines of 800: each one is within what R-FXT-003 allows, their total is not.
    error = refused(lambda: env.run(create_entry(env, ["800", "800", "800"])))

    assert error.code is ErrorCode.VALIDATION_FAILED
    assert error.rule_id == TOTAL_QTY_RULE
    assert error.message == TOTAL_QTY_MESSAGE
    # The rule declares no field, so a check that names no ``line.`` path reports no detail
    # item at all (requirement 10.7).
    assert details(error) == []


@pytest.mark.req("10.7")
def test_a_rule_without_a_field_names_the_failing_line_alone(env: EngineEnv) -> None:
    # The same rule without its ``field``: the path of a failing line is lines[i].
    engine = engine_of(env.domain, replace(env.domain.rules[LINE_QTY_RULE], field=None))

    error = refused(lambda: env.run_with(engine, create_entry(env, ["1500"])))

    assert (error.rule_id, details(error)) == (LINE_QTY_RULE, rule_items("lines[0]"))


@pytest.mark.req("10.7")
def test_a_bill_without_lines_satisfies_a_rule_that_names_a_line(env: EngineEnv) -> None:
    # R-FXT-003 is evaluated per line, so a goods entry without lines satisfies it, and the
    # total of no line is 0, which R-FXT-004 allows (requirements 10.7 and 10.13).
    created = as_record(env.run(create_entry(env, ())).view)

    assert record_lines(created) == ()
    assert created.fields[STATUS] == DRAFT


# --- which operations a rule applies to (requirement 10.9) -------------------------------


@pytest.mark.req("10.9")
def test_an_operation_the_on_does_not_list_evaluates_no_rule(env: EngineEnv) -> None:
    entry = new_entry(env)
    # A line over what R-FXT-003 allows, which no create and no update of the engine writes.
    tamper(env, ENTRY_LINE, f"{entry}-1", {ENTRY_QTY: Decimal("5000")})

    # submit is in the ``on`` of the rule, so the draft does not leave draft.
    submit = refused(lambda: env.transition(ENTRY, entry, "submit"))
    assert submit.rule_id == LINE_QTY_RULE
    # delete is not, so the same draft is removed without the check being evaluated.
    env.delete(ENTRY, entry)

    assert env.records(ENTRY) == []


@pytest.mark.req("10.9")
def test_a_push_is_the_create_of_the_bill_it_generates(env: EngineEnv) -> None:
    order = new_order(env, qty="2000")

    # The draft a push generates takes the rules of the ``create`` of its bill, over the
    # lines the conversion and the caller settled (requirements 8.2 and 10.9).
    error = refused(
        lambda: env.push(
            ORDER_TO_ENTRY, order, [push_line(env, order, "1500")], source_name=ORDER_SHORT
        )
    )
    assert (error.rule_id, details(error)) == (LINE_QTY_RULE, rule_items("lines[0].entry_qty"))
    assert env.records(ENTRY) == []

    # The quota of the source allows either push, so 800 of the 2000 ordered goes through.
    entry = push_entry(env, order, "800")

    assert status_of(env, ENTRY, entry) == DRAFT


@pytest.mark.req("10.9")
def test_a_writeback_is_no_operation_on_the_source_bill(env: EngineEnv) -> None:
    # A rule on the source bill of the conversion whose check is false once a downstream
    # bill has written back to it, on the two operations such a write would look like.
    engine = engine_of(env.domain, occupied_order_rule())
    order = new_order(env, qty="10")
    entry = push_entry(env, order, "4")

    # The approve of the goods entry adds 4 to the link field of the source line. That
    # write belongs to this call and is no operation on the source bill, so no rule of the
    # source bill is evaluated for it (requirements 8.8 and 10.9).
    env.run_with(engine, TransitionOp(ENTRY, entry, "submit"))
    env.run_with(engine, TransitionOp(ENTRY, entry, "approve"))

    assert linked_qty(env, order) == [Decimal("4")]
    # The rule is in force: the close operation of that same bill is refused for it.
    error = refused(lambda: env.run_with(engine, TransitionOp(ORDER, order, "close")))
    assert error.rule_id == ADDED_RULE
    assert status_of(env, ORDER, order) == APPROVED


@pytest.mark.req("10.9")
def test_an_automatic_close_is_no_operation_on_the_source_bill(env: EngineEnv) -> None:
    engine = engine_of(env.domain, occupied_order_rule())
    order = new_order(env, qty="10")
    entry = push_entry(env, order, "10")

    # Every line of the source is linked in full, so the approve of the goods entry closes
    # it; that close is a write of this call and evaluates no rule of the source bill
    # either (requirements 8.13 and 10.9).
    env.run_with(engine, TransitionOp(ENTRY, entry, "submit"))
    env.run_with(engine, TransitionOp(ENTRY, entry, "approve"))

    assert status_of(env, ORDER, order) == CLOSED


def occupied_order_rule() -> RuleModel:
    """A rule the fixture does not declare: a linked supply order is not changed or closed."""
    return added_rule(
        ORDER,
        ("update", "close"),
        "sum(lines.linked_qty) == 0",
        message="已被下游单据占用的供货订单不得修改或关闭",
    )


@pytest.mark.req("10.9")
def test_a_described_rule_is_never_evaluated(env: EngineEnv) -> None:
    # The rule that keeps a line at 1000, described rather than enforced: its text is what
    # the model reads and the engine evaluates nothing (requirements 3.6 and 10.9).
    described = replace(env.domain.rules[LINE_QTY_RULE], enforcement="described")
    domain = with_rules(env.domain, described)
    engine = Engine(domain)

    created = as_record(env.run_with(engine, create_entry(env, ["1500"])).view)

    assert created.fields[STATUS] == DRAFT
    assert applicable_rules(domain, ENTRY, "create") == (domain.rules[TOTAL_QTY_RULE],)
    assert LINE_QTY_RULE not in engine.checks


# --- what a check is evaluated on (requirement 10.5) -------------------------------------


@pytest.mark.req("10.5")
def test_a_create_is_evaluated_on_the_record_it_writes(env: EngineEnv) -> None:
    # The candidate, the computed fields of requirement 6 included: a rule that reads the
    # base unit quantity of a line sees the value this create computed for it.
    engine = engine_of(
        env.domain,
        added_rule(
            ENTRY,
            ("create",),
            "line.qty_base <= 100",
            message="收料单每行的基本单位数量不得超过 100",
            field=FieldRef("line", ENTRY_QTY),
        ),
    )

    error = refused(lambda: env.run_with(engine, create_entry(env, ["200"])))

    assert (error.rule_id, details(error)) == (ADDED_RULE, rule_items("lines[0].entry_qty"))
    assert env.records(ENTRY) == []


@pytest.mark.req("10.5")
def test_an_update_is_evaluated_on_the_merged_bill(env: EngineEnv) -> None:
    entry = new_entry(env, ("800",))

    # The lines the update replaces are what the rule reads, not the stored ones
    # (requirements 5.3 and 10.5).
    error = refused(lambda: env.update(ENTRY, entry, {}, [entry_line(env, "1500")]))

    assert (error.rule_id, details(error)) == (LINE_QTY_RULE, rule_items("lines[0].entry_qty"))
    # An update whose lines stay within the rule goes through.
    updated = as_record(env.update(ENTRY, entry, {}, [entry_line(env, "900")]).view)
    assert [line.fields[ENTRY_QTY] for line in record_lines(updated)] == [Decimal("900")]


@pytest.mark.req("10.5")
def test_a_state_operation_is_evaluated_on_the_state_it_writes(env: EngineEnv) -> None:
    # A rule on the submit of a goods entry that reads the state of the bill: the stored
    # record is still a draft, and the check reads the state the submit writes.
    engine = engine_of(
        env.domain,
        added_rule(ENTRY, ("submit",), f"header.status != '{SUBMITTED}'", message="收料单不得提交"),
    )
    entry = new_entry(env)
    assert status_of(env, ENTRY, entry) == DRAFT

    error = refused(lambda: env.run_with(engine, TransitionOp(ENTRY, entry, "submit")))

    assert error.rule_id == ADDED_RULE
    assert status_of(env, ENTRY, entry) == DRAFT


@pytest.mark.req("10.5")
def test_a_delete_is_evaluated_on_the_bill_before_it_is_removed(env: EngineEnv) -> None:
    engine = engine_of(
        env.domain,
        added_rule(ENTRY, ("delete",), "count(lines) == 0", message="还有分录的收料单不得删除"),
    )
    entry = new_entry(env)

    error = refused(lambda: env.run_with(engine, DeleteOp(ENTRY, entry)))

    assert error.rule_id == ADDED_RULE
    assert status_of(env, ENTRY, entry) == DRAFT


@pytest.mark.req("10.5")
def test_the_evaluated_bill_of_a_state_operation_carries_the_stored_lines(
    env: EngineEnv,
) -> None:
    entry = new_entry(env, ("10", "20"))
    header = stored(env, ENTRY, entry)

    with env.store.transaction() as uow:
        evaluated = evaluated_bill(
            uow, RuleStep(operation="submit", record=header, bill=env.domain.bills[ENTRY])
        )

    assert evaluated is not None
    # The stored fields with the state the submit writes, and the stored lines by line
    # number (requirement 10.5).
    assert (evaluated.header[STATUS], header[STATUS]) == (SUBMITTED, DRAFT)
    assert [line[ENTRY_QTY] for line in evaluated.lines] == [Decimal("10"), Decimal("20")]


# --- the uniqueness check that follows (requirements 5.8 and 12.11) ----------------------


@pytest.mark.req("12.11")
def test_a_call_that_breaks_a_rule_and_a_unique_value_reports_the_rule(
    env: EngineEnv,
) -> None:
    reference = "GR-0001"
    new_entry(env, ("800",), entry_ref=reference)

    # The same reference again, with a line over what R-FXT-003 allows: the business rules
    # step runs before the uniqueness check, so the rule is what the call reports
    # (requirement 12.11).
    error = refused(lambda: env.run(create_entry(env, ["1500"], reference)))

    assert error.code is ErrorCode.BUSINESS_RULE_VIOLATION
    assert error.rule_id == LINE_QTY_RULE
    assert details(error) == rule_items("lines[0].entry_qty")
    # The unique value is checked where the rules hold: the same call with a quantity the
    # rule allows ends in CONFLICT, which no rule decides (requirements 5.8 and 13.8).
    conflict = refused(lambda: env.run(create_entry(env, ["800"], reference)))
    assert (conflict.code, conflict.rule_id) == (ErrorCode.CONFLICT, None)
    assert len(env.records(ENTRY)) == 1


# --- the engine rules of the step (requirement 10.8) -------------------------------------


@pytest.mark.req("10.8")
def test_the_step_runs_the_engine_rules_before_the_pack_rules(env: EngineEnv) -> None:
    entry = new_entry(env, ())
    step = RuleStep(
        operation="submit",
        record=stored(env, ENTRY, entry),
        bill=env.domain.bills[ENTRY],
    )

    with env.store.transaction() as uow:
        error = refused(
            lambda: check_rules(
                uow,
                env.domain,
                step,
                checks=env.engine.checks,
                anchor_date=env.anchor_date,
                current_user_id=env.current_user_id,
            )
        )

    # R-GEN-002 of the state_machine primitive, reached through the one entry point of the
    # step: a goods entry without lines does not leave draft (requirements 7.5 and 10.8).
    assert error.rule_id == REQUIRED_RULE


@pytest.mark.req("10.2")
def test_a_check_the_caller_holds_no_tree_of_is_parsed_by_the_step(env: EngineEnv) -> None:
    entry = new_entry(env)
    # A line over what R-FXT-003 allows, which no create and no update of the engine writes.
    tamper(env, ENTRY_LINE, f"{entry}-1", {ENTRY_QTY: Decimal("5000")})
    step = RuleStep(
        operation="submit",
        record=stored(env, ENTRY, entry),
        bill=env.domain.bills[ENTRY],
    )

    with env.store.transaction() as uow:
        error = refused(
            lambda: check_rules(
                uow,
                env.domain,
                step,
                anchor_date=env.anchor_date,
                current_user_id=env.current_user_id,
            )
        )

    # The engine holds the tree of every enforced rule and hands them to the step, so no
    # call of its own parses a check text again; a caller that hands over none has the
    # checks it needs parsed here instead (requirement 10.2).
    assert error.rule_id == LINE_QTY_RULE
