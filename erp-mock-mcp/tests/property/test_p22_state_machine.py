"""Property 22: the model of the preset state machines.

**Validates: Requirements 7.1, 7.2, 7.3, 7.4**

For any state machine type m, a bill of that machine in a state x and an operation o out of
update, delete and the state operations m has, what the engine does agrees with the
transition table: an operation that goes through leaves the bill in the state the table
names; the update or the delete of a bill that is not a draft ends in INVALID_STATE with
the rule id R-GEN-001; an operation whose state does not fit ends in INVALID_STATE that no
rule decides, so with rule id None; either error carries the state the bill is in and
leaves it there.

The reference model is :mod:`tests.property.models.state_table`, which writes the two
machines of meta-model.md section 6 down state by state - the other way round from the
table of :mod:`erp_mock.domain.state_machine`, which is keyed by the operation - and answers
what one operation does to a bill in one state. The test walks a bill through a drawn
sequence of operations and compares every step with that answer, so each example covers a
whole run of states and not a single pair.

What is drawn, per example: one of the two bills of the engine test fixture, which is the
state machine type m - the standard ``supply_order`` and the simple ``fee_note``, whose
machine the test reads from the resolved configuration rather than assuming it - a state x
of that machine, and a walk of 1 to 12 operations over the operations the machine has. The
walk opens with the shortest run that takes the new bill to the drawn state, so x is drawn
as evenly as o is and the states a standard bill reaches late, approved and closed, come up
as often as draft. Two steps in three are drawn from the operations that go through from
the state the bill is in, which carries the walk on, and the rest from every operation of
the machine, which is what ends a step in a refusal; a walk stops at the operation that
deletes the bill, as every later operation would be about a bill that is gone
(requirement 5.6) and not about its state.

The examples written out below run every pair of a state and an operation of either
machine - 32 for the standard machine and 8 for the simple one - so the whole table is
covered whatever the drawn walks turn out to be.

The bill of an example is complete - a header with its required values and one priced,
measured line - so nothing but the state decides an operation: a bill that is complete
leaves draft whenever the table allows it, and R-GEN-002 (requirement 7.5) never comes up.
Neither bill has a linkage, an enforced pack rule or a writeback of its own, and the walk
pushes nothing, so no approve or unapprove carries work beyond the state (requirements 7.6
and 7.7), and a closed bill of the walk was always closed by the close operation, never
automatically (requirement 7.9).

Every example runs on the same sandbox, built once for the module with the standard basic
data, and creates a bill of its own in it: a bill is only ever read and written through its
own id, so the examples do not see each other, and one create per example keeps a hundred
walks quick where a sandbox per example would not.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, assert_never

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.config.model import StateMachine
from erp_mock.config.pack import BillStatus
from erp_mock.domain.engine import OpResult
from erp_mock.domain.state_machine import INVALID_STATE_REASON, STATUS
from erp_mock.state.store import Value
from tests.helpers.engine_env import (
    BOLT,
    MAIN_VENDOR,
    EngineEnv,
    as_record,
    engine_env,
    fixture_model,
)
from tests.property.models.state_table import (
    DELETE,
    DRAFT,
    MACHINES,
    UPDATE,
    Action,
    Machine,
    Outcome,
    Refused,
    Removed,
    Written,
    actions,
    fitting,
    machine_of,
    outcome,
    path_to,
    state_action,
)

# The two bills of the engine test fixture, one of each preset machine: the standard
# ``supply_order``, which prices and measures its lines, and the simple ``fee_note``, which
# declares neither submit nor close (requirement 7.11, tests/fixtures).
ORDER: Final = "supply_order"
NOTE: Final = "fee_note"
BILLS: Final[tuple[str, ...]] = (ORDER, NOTE)

# The most operations one walk holds; a walk of one operation is drawn as well.
MAX_ACTIONS: Final = 12

# How often a step is drawn from the operations that go through from the state the bill is
# in: two draws in three, which carries a walk through the states of its machine and still
# ends a step in a refusal often enough.
FITS: Final = st.sampled_from([True, True, False])

# What an update of the walk writes: a field no rule and no state looks at.
REMARK: Final = "夹具状态走查"


@dataclass(frozen=True, slots=True)
class Case:
    """One example: the bill a walk runs on and the operations it runs, in order."""

    bill: str
    actions: tuple[Action, ...]


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data, one for the whole module."""
    with engine_env(tmp_path_factory.mktemp("p22") / "sandbox") as environment:
        yield environment


# --- the machine of a bill and a complete bill of it -------------------------------------


def machine_name(bill: str) -> StateMachine:
    """The preset machine the business pack declared for ``bill`` (requirement 7.2).

    Read from the resolved configuration, so the test names the bill and the configuration
    decides which machine that is.
    """
    return fixture_model().bills[bill].state_machine


def machine_for(bill: str) -> Machine:
    """The reference model of the machine of ``bill`` (tests.property.models.state_table)."""
    return machine_of(machine_name(bill))


# The fixture declares one bill of each preset machine, so the drawn bill is the state
# machine type m of the property (requirements 7.1 and 7.2).
assert {machine_name(bill) for bill in BILLS} == set(MACHINES)


def complete_header(env: EngineEnv, bill: str) -> dict[str, Value]:
    """The required header values of ``bill``: its partner and its date (requirement 7.5)."""
    date_field = "need_date" if bill == ORDER else "due_date"
    return {"vendor_id": env.id(MAIN_VENDOR), date_field: env.anchor_date}


def complete_lines(env: EngineEnv, bill: str) -> list[dict[str, Value]]:
    """One complete line of ``bill``: a priced line, measured where the bill measures.

    The order takes an article, whose purchase unit and tax rate its line defaults from,
    and the fee note a text of its own; the quantity and the price are filled in both, as
    a priced line needs them (requirement 7.5).
    """
    line: dict[str, Value] = {"qty": Decimal("12"), "price": Decimal("0.85")}
    if bill == ORDER:
        line["article_id"] = env.id(BOLT)
    else:
        line["item_text"] = "夹具运费"
    return [line]


def new_bill(env: EngineEnv, bill: str) -> str:
    """Create a complete bill of ``bill`` and return its id; a create leaves it in draft."""
    header = complete_header(env, bill)
    view = as_record(env.create(bill, header, complete_lines(env, bill)).view)
    bill_id = view.fields["id"]
    assert isinstance(bill_id, str), f"the create of {bill} returned no id"
    return bill_id


# --- running one operation and comparing it with the model -------------------------------


def run_action(env: EngineEnv, bill: str, bill_id: str, action: Action) -> OpResult:
    """Run ``action`` on the bill ``bill_id``, the way a tools/call runs it.

    The update writes :data:`REMARK` and carries no lines, so it leaves the lines of the
    bill as they are (requirement 5.3); a state operation goes through the engine's
    transition operation (requirement 7.2).
    """
    if action == UPDATE:
        return env.update(bill, bill_id, {"remark": REMARK})
    if action == DELETE:
        return env.delete(bill, bill_id)
    return env.transition(bill, bill_id, state_action(action))


def stored_status(env: EngineEnv, bill: str, bill_id: str) -> Value:
    """The state the bill ``bill_id`` is in, as the sandbox holds it (requirement 7.1)."""
    record = env.record(bill, bill_id)
    assert record is not None, f"{bill} {bill_id} is not in the sandbox"
    return record[STATUS]


def details(error: ErpError) -> list[dict[str, object]]:
    """The detail items of ``error`` as plain dicts, in report order (requirement 13.6)."""
    return [dict(item) for item in error.details]


def run_step(
    env: EngineEnv, bill: str, bill_id: str, action: Action, expected: Outcome
) -> BillStatus | None:
    """Run ``action`` and pin what it did against ``expected``; None once the bill is gone.

    The three outcomes of the model, which are the three clauses of the property: an
    operation that goes through returns the bill in the state the table names and leaves
    the sandbox holding that state, a delete leaves no record at all, and a refused
    operation raises INVALID_STATE with the rule id of its clause and one detail item
    carrying the state the bill is in, which it stays in.
    """
    match expected:
        case Written(status=status):
            view = as_record(run_action(env, bill, bill_id, action).view)
            # The operation returns the bill it left behind (requirement 13.2), and the
            # sandbox holds the same state (requirements 7.1 and 7.2).
            assert view.fields[STATUS] == status
            assert stored_status(env, bill, bill_id) == status
            return status
        case Removed():
            view = as_record(run_action(env, bill, bill_id, action).view)
            # A delete runs on a draft and returns the record as it was (requirement 7.1).
            assert view.fields[STATUS] == DRAFT
            assert env.record(bill, bill_id) is None
            return None
        case Refused(rule_id=rule_id, status=status):
            with pytest.raises(ErpError) as raised:
                run_action(env, bill, bill_id, action)
            error = raised.value
            assert error.code is ErrorCode.INVALID_STATE
            # R-GEN-001 where that rule decided, None where no rule did (requirements 7.3,
            # 7.4 and 13.8).
            assert error.rule_id == rule_id
            # One detail item, which names no input field and gives the state of the bill.
            assert details(error) == [{"reason": INVALID_STATE_REASON, "status": status}]
            assert stored_status(env, bill, bill_id) == status
            return status
        case _:
            assert_never(expected)


# --- the walk ----------------------------------------------------------------------------


@st.composite
def walks(draw: st.DrawFn, machine: Machine) -> tuple[Action, ...]:
    """Draw a walk of operations over ``machine``, from a drawn state of it.

    The walk opens with the shortest run of operations that takes the new bill to a drawn
    state of the machine (:func:`~tests.property.models.state_table.path_to`), so the state
    x of the property is drawn as evenly as the operation o is: every state of the machine
    comes up as the state a drawn operation runs from.

    Each step after that is drawn from the operations that go through from the state the
    bill has reached, or from every operation of the machine (:data:`FITS`), and the walk is
    followed through the reference model as it is drawn, so a later step knows which state
    it runs from. The walk stops at the operation that deletes the bill: a later operation
    would be about a record that is gone (requirement 5.6) and not about a state.
    """
    status: BillStatus = draw(st.sampled_from(machine.states))
    walk: list[Action] = list(path_to(machine, status))
    length = draw(st.integers(min_value=1, max_value=MAX_ACTIONS))
    for _ in range(length):
        pool = fitting(machine, status) if draw(FITS) else actions(machine)
        action = draw(st.sampled_from(pool))
        walk.append(action)
        reached = outcome(machine, status, action)
        if isinstance(reached, Removed):
            break
        if isinstance(reached, Written):
            status = reached.status
    return tuple(walk)


@st.composite
def cases(draw: st.DrawFn) -> Case:
    """Draw one example: a bill of the fixture and a walk over the machine it declares."""
    bill = draw(st.sampled_from(BILLS))
    return Case(bill=bill, actions=draw(walks(machine_for(bill))))


def case(bill: str, *walk: Action) -> Case:
    """One example written out: the bill and the operations to run on it, in order."""
    return Case(bill=bill, actions=walk)


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 22: 状态机模型
@pytest.mark.req("7.1", "7.2", "7.3", "7.4")
@settings(max_examples=100)
@given(case=cases())
# The standard machine forward and back, every one of its six state operations once
# (requirement 7.1).
@example(
    case=case(
        ORDER,
        "submit",
        "unsubmit",
        "submit",
        "approve",
        "unapprove",
        "submit",
        "approve",
        "close",
        "unclose",
    )
)
# A draft: every state operation but submit is refused, an update goes through and a delete
# removes it (requirements 7.3 and 7.4).
@example(case=case(ORDER, "approve", "unsubmit", "unapprove", "close", "unclose", UPDATE, DELETE))
# R-GEN-001 in each of the three states a standard bill has besides draft (requirement 7.3).
@example(
    case=case(ORDER, "submit", UPDATE, DELETE, "approve", UPDATE, DELETE, "close", UPDATE, DELETE)
)
# Every state operation a submitted, an approved and a closed bill does not allow
# (requirement 7.4); with the two examples above, the examples run every pair of a state
# and an operation of the standard machine.
@example(
    case=case(
        ORDER,
        "submit",
        "submit",
        "unclose",
        "unapprove",
        "close",
        "approve",
        "approve",
        "submit",
        "unsubmit",
        "unclose",
        "close",
        "close",
        "submit",
        "unsubmit",
        "approve",
        "unapprove",
        "unclose",
    )
)
# The simple machine, every pair of its two states and its four operations: no unapprove
# from draft, approve from draft, no approve again, R-GEN-001 while approved, and back to
# draft (requirements 7.2, 7.3 and 7.4).
@example(
    case=case(NOTE, "unapprove", "approve", "approve", UPDATE, DELETE, "unapprove", UPDATE, DELETE)
)
def test_every_operation_agrees_with_the_transition_table(env: EngineEnv, case: Case) -> None:
    machine = machine_for(case.bill)

    bill_id = new_bill(env, case.bill)

    # A create leaves the bill in draft, whichever machine it has (requirements 7.1, 7.2).
    assert stored_status(env, case.bill, bill_id) == DRAFT
    status: BillStatus | None = DRAFT
    for number, action in enumerate(case.actions, start=1):
        assert status is not None, f"step {number} of {case} runs on a deleted bill"
        status = run_step(env, case.bill, bill_id, action, outcome(machine, status, action))
