"""erp_mock.domain.state_machine: the preset state machines (requirements 7.1 to 7.5, 7.8, 7.9).

The tests run against a real sandbox built by :mod:`tests.helpers.engine_env`: an empty
database over the tables of the engine test fixture with one standard set of basic data,
every record of it created through the engine. The fixture has a bill of each machine - the
standard ``supply_order``, which also prices its lines and measures them, and the simple
``fee_note``, which declares neither submit nor close (requirement 7.11) - so a state
operation is exercised exactly as a tools/call runs it: one operation, one transaction.
What the tests pin:

- the transition table of both machines: a standard bill runs draft, submitted, approved,
  closed and back again, a simple one between draft and approved alone, an update leaves a
  draft in draft and a delete removes it (requirements 7.1 and 7.2);
- R-GEN-001: the update and the delete of a draft go through, and both end in
  INVALID_STATE with that rule id for a bill in any other state of its machine, the detail
  item carrying the state the bill is in (requirement 7.3);
- a state operation whose bill is not in the state its transition starts from ends in
  INVALID_STATE that no rule decided, so with rule id None, and the bill is left as it was
  (requirement 7.4);
- R-GEN-002: a complete bill leaves draft, and one without lines or with an empty required
  value ends in VALIDATION_FAILED with that rule id and one detail item per missing item,
  the header fields by their own names and the line fields as ``lines[i].<name>``
  (requirement 7.5);
- how a bill was closed is kept in an internal column, manual for the close operation
  (requirement 7.8), and the unclose of a bill that was closed automatically is refused
  while the bill stays closed (requirement 7.9).
"""

from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.domain.engine import PIPELINE, GetOp, TransitionOp
from erp_mock.domain.numbering import CREATED_SEQ, UPDATED_SEQ
from erp_mock.domain.state_machine import (
    APPROVED,
    AUTO_CLOSE,
    CLOSED,
    DRAFT,
    DRAFT_RULE,
    INVALID_STATE_REASON,
    MANUAL_CLOSE,
    REQUIRED_MISSING_REASON,
    REQUIRED_RULE,
    STATUS,
    SUBMITTED,
    TRANSITIONS,
    StateAction,
    leaves_draft,
    required_header_fields,
    required_line_fields,
    transition,
    write_status,
)
from erp_mock.state.schema import CLOSE_MODE_COLUMN
from erp_mock.state.store import Value
from tests.helpers.engine_env import (
    BOLT,
    MAIN_VENDOR,
    EngineEnv,
    as_record,
    engine_env,
    fixture_model,
)

# The two bills these tests run: the standard bill of the fixture, which prices and measures
# its lines, and the simple one, which has draft and approved alone (tests/fixtures).
ORDER: Final = "supply_order"
NOTE: Final = "fee_note"

# The key a bill's lines take in a field path (requirement 7.5).
LINES: Final = "lines"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


def order_line(env: EngineEnv, **values: Value) -> dict[str, Value]:
    """One complete line of a supply order: bolts by the box, at a price."""
    line: dict[str, Value] = {
        "article_id": env.id(BOLT),
        "qty": Decimal("2000"),
        "price": Decimal("0.85"),
    }
    line.update(values)
    return line


def new_order(
    env: EngineEnv, lines: Sequence[dict[str, Value]] | None = None, **header: Value
) -> str:
    """Create a supply order and return its id; a complete one by default."""
    values: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR), "need_date": env.anchor_date}
    values.update(header)
    rows = [order_line(env)] if lines is None else list(lines)
    return str(as_record(env.create(ORDER, values, rows).view).fields["id"])


def new_note(
    env: EngineEnv, lines: Sequence[dict[str, Value]] | None = None, **header: Value
) -> str:
    """Create a fee note, the simple bill of the fixture, and return its id."""
    values: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR), "due_date": env.anchor_date}
    values.update(header)
    complete: dict[str, Value] = {
        "item_text": "夹具运费",
        "qty": Decimal("1"),
        "price": Decimal("300"),
    }
    rows = [complete] if lines is None else list(lines)
    return str(as_record(env.create(NOTE, values, rows).view).fields["id"])


def status_of(env: EngineEnv, bill: str, bill_id: str) -> Value:
    """The state the bill ``bill_id`` is in, as storage holds it (requirement 7.1)."""
    record = env.record(bill, bill_id)
    assert record is not None, f"{bill} {bill_id} is not in the sandbox"
    return record[STATUS]


def close_mode_of(env: EngineEnv, bill: str, bill_id: str) -> Value:
    """How the bill ``bill_id`` was closed, from its internal column (requirement 7.8)."""
    record = env.record(bill, bill_id)
    assert record is not None, f"{bill} {bill_id} is not in the sandbox"
    return record[CLOSE_MODE_COLUMN]


def details(error: ErpError) -> list[dict[str, object]]:
    """The detail items of ``error`` as plain dicts, in report order (requirement 13.6)."""
    return [dict(item) for item in error.details]


def paths(error: ErpError) -> list[object]:
    """The field path of every detail item of ``error``, in report order (req 7.5)."""
    return [item["field"] for item in error.details]


def run_transition(env: EngineEnv, bill: str, bill_id: str, action: StateAction) -> ErpError:
    """Run ``action`` on ``bill_id`` and return the error it ended in."""
    with pytest.raises(ErpError) as raised:
        env.transition(bill, bill_id, action)
    return raised.value


# --- the transition table (requirements 7.1 and 7.2) -------------------------------------


@pytest.mark.req("7.1", "7.2")
def test_the_table_holds_the_transitions_of_the_two_preset_machines() -> None:
    assert {key: (row.pre, row.post) for key, row in TRANSITIONS.items()} == {
        ("standard", "submit"): (DRAFT, SUBMITTED),
        ("standard", "unsubmit"): (SUBMITTED, DRAFT),
        ("standard", "approve"): (SUBMITTED, APPROVED),
        ("standard", "unapprove"): (APPROVED, DRAFT),
        ("standard", "close"): (APPROVED, CLOSED),
        ("standard", "unclose"): (CLOSED, APPROVED),
        ("simple", "approve"): (DRAFT, APPROVED),
        ("simple", "unapprove"): (APPROVED, DRAFT),
    }


@pytest.mark.req("7.2")
def test_a_simple_machine_has_no_submit_and_no_close() -> None:
    # The four operations requirement 7.11 keeps a simple bill from declaring; the table
    # answers for none of them.
    for action in ("submit", "unsubmit", "close", "unclose"):
        assert transition("simple", action) is None


@pytest.mark.req("7.1")
def test_a_standard_bill_runs_through_its_four_states(env: EngineEnv) -> None:
    order = new_order(env)

    reached = [
        str(as_record(env.transition(ORDER, order, action).view).fields[STATUS])
        for action in ("submit", "approve", "close", "unclose", "unapprove", "submit")
    ]

    # Each operation returns the bill it left behind, so the result carries the new state
    # (requirement 13.2), and the sandbox holds the same one.
    assert reached == [SUBMITTED, APPROVED, CLOSED, APPROVED, DRAFT, SUBMITTED]
    assert status_of(env, ORDER, order) == SUBMITTED


@pytest.mark.req("7.2")
def test_a_simple_bill_approves_from_draft_and_goes_back(env: EngineEnv) -> None:
    note = new_note(env)

    assert status_of(env, NOTE, note) == DRAFT
    assert str(as_record(env.transition(NOTE, note, "approve").view).fields[STATUS]) == APPROVED
    assert str(as_record(env.transition(NOTE, note, "unapprove").view).fields[STATUS]) == DRAFT
    assert status_of(env, NOTE, note) == DRAFT


@pytest.mark.req("7.1")
def test_an_update_leaves_a_draft_in_draft(env: EngineEnv) -> None:
    order = new_order(env)

    view = as_record(env.update(ORDER, order, {"remark": "夹具改期"}).view)

    assert view.fields[STATUS] == DRAFT
    assert status_of(env, ORDER, order) == DRAFT


@pytest.mark.req("7.1", "13.2")
def test_a_state_operation_returns_what_a_get_returns_right_after_it(env: EngineEnv) -> None:
    order = new_order(env)

    result = env.transition(ORDER, order, "submit")

    # The object of requirement 13.2: the bill after the operation, every computed field
    # and every line included.
    assert as_record(result.view) == as_record(env.run(GetOp(ORDER, order)).view)


@pytest.mark.req("4.5", "4.11")
def test_a_state_operation_takes_an_operation_sequence_number(env: EngineEnv) -> None:
    order = new_order(env)
    created = env.record(ORDER, order)
    assert created is not None

    view = as_record(env.transition(ORDER, order, "submit").view)

    # The create of the bill was one write operation and the submit the next one, which
    # only the updated_seq of the bill follows (requirement 4.5).
    assert view.fields[CREATED_SEQ] == created[CREATED_SEQ]
    assert view.fields[UPDATED_SEQ] == env.operations + 2


# --- R-GEN-001: only a draft is changed or deleted (requirement 7.3) ---------------------


@pytest.mark.req("7.3")
def test_a_draft_is_updated_and_deleted(env: EngineEnv) -> None:
    order = new_order(env)

    env.update(ORDER, order, {"remark": "夹具备注"})
    env.delete(ORDER, order)

    assert env.record(ORDER, order) is None


@pytest.mark.req("7.3")
@pytest.mark.parametrize(
    ("actions", "status"),
    [
        (("submit",), SUBMITTED),
        (("submit", "approve"), APPROVED),
        (("submit", "approve", "close"), CLOSED),
    ],
)
def test_a_bill_that_is_no_draft_is_not_updated(
    env: EngineEnv, actions: tuple[StateAction, ...], status: str
) -> None:
    order = new_order(env)
    for action in actions:
        env.transition(ORDER, order, action)

    with pytest.raises(ErpError) as raised:
        env.update(ORDER, order, {"remark": "夹具改期"})

    error = raised.value
    assert error.code is ErrorCode.INVALID_STATE
    assert error.rule_id == DRAFT_RULE
    # The detail item names no input field and carries the state of the bill
    # (design.md data model 6).
    assert details(error) == [{"reason": INVALID_STATE_REASON, "status": status}]
    # The call wrote nothing: the state and the remark are what they were.
    record = env.record(ORDER, order)
    assert record is not None
    assert record[STATUS] == status
    assert record["remark"] is None


@pytest.mark.req("7.3")
def test_a_bill_that_is_no_draft_is_not_deleted(env: EngineEnv) -> None:
    order = new_order(env)
    env.transition(ORDER, order, "submit")

    with pytest.raises(ErpError) as raised:
        env.delete(ORDER, order)

    error = raised.value
    assert error.code is ErrorCode.INVALID_STATE
    assert error.rule_id == DRAFT_RULE
    assert details(error) == [{"reason": INVALID_STATE_REASON, "status": SUBMITTED}]
    assert status_of(env, ORDER, order) == SUBMITTED


@pytest.mark.req("7.3")
def test_an_approved_simple_bill_is_not_updated(env: EngineEnv) -> None:
    note = new_note(env)
    env.transition(NOTE, note, "approve")

    with pytest.raises(ErpError) as raised:
        env.update(NOTE, note, {"remark": "夹具改期"})

    # Approved is the one state besides draft a simple bill has (requirement 7.2).
    assert raised.value.rule_id == DRAFT_RULE
    assert details(raised.value) == [{"reason": INVALID_STATE_REASON, "status": APPROVED}]


@pytest.mark.req("7.3")
def test_the_record_of_basic_data_has_no_state_to_check(env: EngineEnv) -> None:
    # R-GEN-001 is about the state of a bill; an entity record is updated and deleted in
    # whatever state its own status field holds (requirement 5.2).
    vendor = as_record(env.create("vendor", {"name": "夹具新供货单位"}).view)
    vendor_id = str(vendor.fields["id"])
    env.update("vendor", vendor_id, {"status": "disabled"})

    env.delete("vendor", vendor_id)

    assert env.record("vendor", vendor_id) is None


# --- the state an operation starts from (requirement 7.4) --------------------------------


@pytest.mark.req("7.4")
@pytest.mark.parametrize(
    ("actions", "action", "status"),
    [
        # A draft: nothing but submit applies to it.
        ((), "approve", DRAFT),
        ((), "unsubmit", DRAFT),
        ((), "unapprove", DRAFT),
        ((), "close", DRAFT),
        ((), "unclose", DRAFT),
        # A submitted bill: approve and unsubmit apply, closing does not.
        (("submit",), "submit", SUBMITTED),
        (("submit",), "close", SUBMITTED),
        # An approved bill: it is not submitted again and not withdrawn from submission.
        (("submit", "approve"), "approve", APPROVED),
        (("submit", "approve"), "unsubmit", APPROVED),
        # A closed bill: only unclose applies.
        (("submit", "approve", "close"), "unapprove", CLOSED),
        (("submit", "approve", "close"), "close", CLOSED),
    ],
)
def test_an_operation_whose_state_does_not_fit_is_refused(
    env: EngineEnv, actions: tuple[StateAction, ...], action: StateAction, status: str
) -> None:
    order = new_order(env)
    for done in actions:
        env.transition(ORDER, order, done)

    error = run_transition(env, ORDER, order, action)

    # No rule decides this error, so its rule id is None (requirement 13.8), and the detail
    # item carries the state the bill is in.
    assert error.code is ErrorCode.INVALID_STATE
    assert error.rule_id is None
    assert details(error) == [{"reason": INVALID_STATE_REASON, "status": status}]
    # The bill stays where it was.
    assert status_of(env, ORDER, order) == status


@pytest.mark.req("7.4")
def test_an_approved_simple_bill_is_not_approved_again(env: EngineEnv) -> None:
    note = new_note(env)
    env.transition(NOTE, note, "approve")

    error = run_transition(env, NOTE, note, "approve")

    assert error.code is ErrorCode.INVALID_STATE
    assert error.rule_id is None
    assert details(error) == [{"reason": INVALID_STATE_REASON, "status": APPROVED}]
    assert status_of(env, NOTE, note) == APPROVED


@pytest.mark.req("7.4")
def test_a_simple_bill_has_no_submit_tool_behind_it(env: EngineEnv) -> None:
    note = new_note(env)

    # The submit of a simple bill is no operation of its machine, and requirement 7.11
    # keeps a pack from declaring one, so no tool can ask for it: the engine reports a
    # programming error rather than a business error.
    with pytest.raises(RuntimeError, match="simple"):
        env.transition(NOTE, note, "submit")


@pytest.mark.req("7.4", "12.1")
def test_the_state_is_reported_before_the_business_rules(env: EngineEnv) -> None:
    order = new_order(env, [])

    # The bill has no line, so R-GEN-002 would stop a submit; the approve of a draft is
    # refused by the state step, which runs before the rules (design.md 4.1).
    error = run_transition(env, ORDER, order, "approve")

    assert error.code is ErrorCode.INVALID_STATE
    assert error.rule_id is None


@pytest.mark.req("7.3", "7.4")
def test_the_slot_of_the_state_is_before_the_rules_and_the_write() -> None:
    slots = [step.name for step in PIPELINE]

    # Step 3 of design.md 4.1: the state of a bill, before the business rules of step 7 and
    # the write of step 9 (requirement 12.1).
    assert slots.index("bill_status") < slots.index("rules") < slots.index("write")


# --- R-GEN-002: what a bill needs to leave draft (requirement 7.5) ----------------------


@pytest.mark.req("7.5")
def test_which_fields_a_bill_needs_filled_comes_from_its_configuration() -> None:
    order = fixture_model().bills[ORDER]
    note = fixture_model().bills[NOTE]

    # The declared required fields of the header, and of the lines the declared ones, the
    # three pricing fields and the material, unit and quantity of the quantity semantics,
    # in field view order (requirement 7.5).
    assert required_header_fields(order) == ("vendor_id", "need_date")
    assert required_line_fields(order) == ("qty", "price", "tax_rate", "article_id", "measure_id")
    # The fee note prices its lines without measuring them, so no unit is asked for.
    assert required_header_fields(note) == ("vendor_id", "due_date")
    assert required_line_fields(note) == ("qty", "price", "tax_rate", "item_text")


@pytest.mark.req("7.5")
def test_the_rule_guards_the_operation_that_leaves_draft() -> None:
    order = fixture_model().bills[ORDER]
    note = fixture_model().bills[NOTE]

    # The submit of a standard bill and the approve of a simple one; no other operation
    # looks at the required values (requirement 7.5).
    assert leaves_draft(order, "submit")
    assert not any(
        leaves_draft(order, action)
        for action in ("unsubmit", "approve", "unapprove", "close", "unclose")
    )
    assert leaves_draft(note, "approve")
    assert not leaves_draft(note, "unapprove")


@pytest.mark.req("7.5")
def test_a_complete_bill_leaves_draft(env: EngineEnv) -> None:
    order = new_order(env)
    note = new_note(env)

    env.transition(ORDER, order, "submit")
    env.transition(NOTE, note, "approve")

    assert status_of(env, ORDER, order) == SUBMITTED
    assert status_of(env, NOTE, note) == APPROVED


@pytest.mark.req("7.5")
def test_a_bill_without_lines_does_not_leave_draft(env: EngineEnv) -> None:
    order = new_order(env, [])

    error = run_transition(env, ORDER, order, "submit")

    assert error.code is ErrorCode.VALIDATION_FAILED
    assert error.rule_id == REQUIRED_RULE
    # The path of the whole array, which is what a bill with no line is missing (7.5).
    assert details(error) == [{"field": LINES, "reason": REQUIRED_MISSING_REASON}]
    # The message carries that path as well (requirement 13.7).
    assert LINES in error.message
    assert status_of(env, ORDER, order) == DRAFT


@pytest.mark.req("7.5")
def test_a_simple_bill_without_lines_is_not_approved(env: EngineEnv) -> None:
    note = new_note(env, [])

    error = run_transition(env, NOTE, note, "approve")

    assert error.code is ErrorCode.VALIDATION_FAILED
    assert error.rule_id == REQUIRED_RULE
    assert details(error) == [{"field": LINES, "reason": REQUIRED_MISSING_REASON}]
    assert status_of(env, NOTE, note) == DRAFT


@pytest.mark.req("7.5")
def test_every_missing_value_is_reported_with_its_path(env: EngineEnv) -> None:
    # A header without its required date, a first line without a price and a second one
    # without a quantity; the unit and the tax rate of both lines come from the material
    # through default_from, so they are filled (requirement 5.1).
    order = new_order(
        env,
        [order_line(env, price=None), order_line(env, qty=None)],
        need_date=None,
    )

    error = run_transition(env, ORDER, order, "submit")

    assert error.rule_id == REQUIRED_RULE
    # The header field by its own name first, then the line fields by line order and in
    # field view order, the index counting from 0 (requirement 7.5).
    assert paths(error) == ["need_date", "lines[0].price", "lines[1].qty"]
    assert [item["reason"] for item in error.details] == [REQUIRED_MISSING_REASON] * 3
    # The message carries the path of a detail item (requirement 13.7).
    assert "need_date" in error.message


@pytest.mark.req("7.5")
def test_a_required_value_of_a_line_is_asked_for_in_every_line(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, article_id=None), order_line(env)])

    error = run_transition(env, ORDER, order, "submit")

    # The material of the first line is empty, and with it the unit and the tax rate its
    # defaults would have come from (requirement 5.1).
    assert paths(error) == ["lines[0].tax_rate", "lines[0].article_id", "lines[0].measure_id"]
    assert status_of(env, ORDER, order) == DRAFT


@pytest.mark.req("7.5")
def test_an_incomplete_bill_is_still_updated_and_deleted(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, price=None)])

    # A create and an update accept an empty required field; only the step out of draft
    # asks for it (requirements 5.1 and 7.5).
    env.update(ORDER, order, {"remark": "夹具待定价"})
    env.delete(ORDER, order)

    assert env.record(ORDER, order) is None


# --- how a bill was closed (requirements 7.8 and 7.9) ------------------------------------


@pytest.mark.req("7.8")
def test_the_close_operation_records_a_manual_close(env: EngineEnv) -> None:
    order = new_order(env)
    env.transition(ORDER, order, "submit")
    env.transition(ORDER, order, "approve")

    view = as_record(env.transition(ORDER, order, "close").view)

    assert view.fields[STATUS] == CLOSED
    assert close_mode_of(env, ORDER, order) == MANUAL_CLOSE
    # The column is internal, so no result shows it (requirement 5.12).
    assert CLOSE_MODE_COLUMN not in view.fields


@pytest.mark.req("7.8")
def test_a_bill_that_leaves_closed_keeps_no_close_mode(env: EngineEnv) -> None:
    order = new_order(env)
    for action in ("submit", "approve", "close"):
        env.transition(ORDER, order, action)

    env.transition(ORDER, order, "unclose")

    # The column is set exactly for the bills that are closed, so the next close decides
    # the way it was closed again (requirements 7.9 and 8.14).
    assert status_of(env, ORDER, order) == APPROVED
    assert close_mode_of(env, ORDER, order) is None
    assert close_mode_of(env, ORDER, new_order(env)) is None


@pytest.mark.req("7.9")
def test_a_bill_closed_automatically_is_not_unclosed(env: EngineEnv) -> None:
    order = new_order(env)
    env.transition(ORDER, order, "submit")
    env.transition(ORDER, order, "approve")
    # What the conversion primitive does when a pushed bill is approved and the source is
    # fully linked (requirement 8.13): the same write, through the same function.
    with env.store.transaction() as uow:
        write_status(uow, env.domain.bills[ORDER], order, CLOSED, mode=AUTO_CLOSE)

    error = run_transition(env, ORDER, order, "unclose")

    assert error.code is ErrorCode.INVALID_STATE
    # No rule decides it (requirement 13.8) and the message says why (requirement 7.9).
    assert error.rule_id is None
    assert details(error) == [{"reason": INVALID_STATE_REASON, "status": CLOSED}]
    assert "自动关闭" in error.message
    # The bill stays closed, the way it was closed with it.
    assert status_of(env, ORDER, order) == CLOSED
    assert close_mode_of(env, ORDER, order) == AUTO_CLOSE


@pytest.mark.req("7.9")
def test_a_bill_closed_by_hand_is_unclosed(env: EngineEnv) -> None:
    order = new_order(env)
    for action in ("submit", "approve", "close"):
        env.transition(ORDER, order, action)

    view = as_record(env.transition(ORDER, order, "unclose").view)

    assert view.fields[STATUS] == APPROVED


@pytest.mark.req("7.8")
def test_a_close_mode_belongs_to_the_closed_state_alone(env: EngineEnv) -> None:
    order = new_order(env)
    bill = env.domain.bills[ORDER]

    with env.store.transaction() as uow:
        # A close says how it was closed, and no other state carries one (requirement 7.8).
        with pytest.raises(ValueError, match="closed"):
            write_status(uow, bill, order, CLOSED)
        with pytest.raises(ValueError, match="close mode"):
            write_status(uow, bill, order, APPROVED, mode=MANUAL_CLOSE)


@pytest.mark.req("7.1")
def test_the_operation_of_a_bill_names_the_bill(env: EngineEnv) -> None:
    order = new_order(env)

    # A state operation runs on a bill; an entity record has no state machine (7.2).
    with pytest.raises(RuntimeError, match="no bill"):
        env.run(TransitionOp("vendor", env.id(MAIN_VENDOR), "approve"))
    assert status_of(env, ORDER, order) == DRAFT
