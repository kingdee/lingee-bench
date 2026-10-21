"""Reference model of the preset state machines (property 22, requirements 7.1 to 7.4).

Written independently of :mod:`erp_mock.domain.state_machine`, whose table is keyed by the
operation - ``(machine, action) -> (pre, post)``. Here each machine is written the other way
round, as what every one of its states allows: a mapping from a state to the operations that
run from it and the state each of them leads to (:data:`STANDARD`, :data:`SIMPLE`), which is
the state diagram of meta-model.md section 6 read state by state::

    standard   draft      submit -> submitted
               submitted  unsubmit -> draft,   approve -> approved
               approved   unapprove -> draft,  close -> closed
               closed     unclose -> approved
    simple     draft      approve -> approved
               approved   unapprove -> draft

An operation a state does not list does not run from it, and an operation no state of the
machine lists at all is one the machine does not have - the submit, unsubmit, close and
unclose of a simple bill (requirements 7.2 and 7.11).

The update and the delete of :data:`RECORD_ACTIONS` are in the model as well, because
requirement 7.3 states them in the same terms: both run from draft alone, an update leaves
the bill in draft and a delete removes it.

:func:`outcome` is the whole model: the state an operation leads to, or the error it ends
in - INVALID_STATE with the rule id :data:`DRAFT_RULE` for the update or the delete of a
bill that is not a draft (requirement 7.3), and INVALID_STATE that no rule decides for an
operation whose state does not fit (requirement 7.4). :func:`fitting` is the other side of
the same table, the operations that go through from a state, which a generator walks a
machine with.

What the model does not cover, because property 22 does not: the required values a bill
needs to leave draft (requirement 7.5), the linkages and the writebacks of an approve
(requirements 7.6 and 7.7), and the unclose of a bill the engine closed itself
(requirement 7.9) - a bill of the model is complete and is only ever closed by the close
operation.
"""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from erp_mock.config.model import StateMachine
from erp_mock.config.pack import BillStatus
from erp_mock.domain.state_machine import StateAction

__all__ = [
    "APPROVED",
    "CLOSED",
    "DELETE",
    "DRAFT",
    "DRAFT_RULE",
    "MACHINES",
    "RECORD_ACTIONS",
    "SIMPLE",
    "STANDARD",
    "STATE_ACTIONS",
    "SUBMITTED",
    "UPDATE",
    "Action",
    "Machine",
    "Outcome",
    "Refused",
    "Removed",
    "Written",
    "actions",
    "fitting",
    "machine_of",
    "outcome",
    "path_to",
    "state_action",
]

# The four states of meta-model.md section 6; a simple bill holds the first and the third.
DRAFT: Final[BillStatus] = "draft"
SUBMITTED: Final[BillStatus] = "submitted"
APPROVED: Final[BillStatus] = "approved"
CLOSED: Final[BillStatus] = "closed"

# The two write operations of a record that requirement 7.3 ties to the state of a bill.
type RecordAction = Literal["update", "delete"]
UPDATE: Final[RecordAction] = "update"
DELETE: Final[RecordAction] = "delete"
RECORD_ACTIONS: Final[tuple[RecordAction, ...]] = (UPDATE, DELETE)

# One operation of the model: a state operation or a write of the record itself.
type Action = StateAction | RecordAction

# The six state operations of meta-model.md section 6, forward and back; which of them a
# machine has follows from its own table (:func:`actions`).
STATE_ACTIONS: Final[tuple[StateAction, ...]] = (
    "submit",
    "unsubmit",
    "approve",
    "unapprove",
    "close",
    "unclose",
)

# The rule that lets a draft alone be changed or deleted (requirement 7.3).
DRAFT_RULE: Final = "R-GEN-001"


@dataclass(frozen=True, slots=True)
class Written:
    """The operation went through and left the bill in ``status``."""

    status: BillStatus


@dataclass(frozen=True, slots=True)
class Removed:
    """The delete went through and the bill is gone (requirement 7.1)."""


@dataclass(frozen=True, slots=True)
class Refused:
    """The operation ended in INVALID_STATE, with the bill left in ``status``.

    ``rule_id`` is :data:`DRAFT_RULE` where that rule decided - the update or the delete of
    a bill that is not a draft (requirement 7.3) - and None for an operation whose state
    does not fit, which no rule decides (requirement 7.4).
    """

    rule_id: str | None
    status: BillStatus


# What one operation leads to: a state, a bill that is gone, or an error.
type Outcome = Written | Removed | Refused


@dataclass(frozen=True, slots=True)
class Machine:
    """One preset state machine: what each of its states allows (requirements 7.1, 7.2).

    ``steps`` maps a state to the state operations that run from it and the state each of
    them leads to; a state without an operation of its own needs no entry. ``states`` is
    every state the machine holds, in the order a bill passes them.
    """

    name: StateMachine
    states: tuple[BillStatus, ...]
    steps: Mapping[BillStatus, Mapping[Action, BillStatus]]


# The standard machine of meta-model.md section 6, state by state. The way back is not the
# way forward: an unapprove returns an approved bill to draft and not to submitted.
STANDARD: Final = Machine(
    name="standard",
    states=(DRAFT, SUBMITTED, APPROVED, CLOSED),
    steps={
        DRAFT: {"submit": SUBMITTED},
        SUBMITTED: {"unsubmit": DRAFT, "approve": APPROVED},
        APPROVED: {"unapprove": DRAFT, "close": CLOSED},
        CLOSED: {"unclose": APPROVED},
    },
)

# The simple machine: draft and approved alone, with no submit and no close at all
# (requirements 7.2 and 7.11).
SIMPLE: Final = Machine(
    name="simple",
    states=(DRAFT, APPROVED),
    steps={
        DRAFT: {"approve": APPROVED},
        APPROVED: {"unapprove": DRAFT},
    },
)

MACHINES: Final[Mapping[StateMachine, Machine]] = {STANDARD.name: STANDARD, SIMPLE.name: SIMPLE}


def machine_of(name: StateMachine) -> Machine:
    """The preset machine ``name``; the two of meta-model.md section 6 are all there are."""
    return MACHINES[name]


def actions(machine: Machine) -> tuple[Action, ...]:
    """Every operation ``machine`` has, the update and the delete first.

    The state operations follow in the order of :data:`STATE_ACTIONS`, each one some state
    of the machine allows; one no state allows is not an operation of that machine at all
    (requirement 7.2), so a simple machine has four operations and a standard one eight.
    """
    allowed = {action for state in machine.states for action in machine.steps.get(state, {})}
    return (*RECORD_ACTIONS, *(action for action in STATE_ACTIONS if action in allowed))


def path_to(machine: Machine, target: BillStatus) -> tuple[Action, ...]:
    """The shortest run of operations that takes a new bill of ``machine`` to ``target``.

    Every bill is created in draft (requirements 7.1 and 7.2), so the run starts there and
    is found breadth first over the steps of the machine: the run is a shortest one and
    every operation of it goes through, which is how a walk reaches a state it means to
    start from. Empty for draft itself.

    Raises KeyError for a state the machine does not reach, the submitted and the closed of
    a simple bill (requirement 7.2).
    """
    runs: dict[BillStatus, tuple[Action, ...]] = {DRAFT: ()}
    queue = deque([DRAFT])
    while queue:
        status = queue.popleft()
        if status == target:
            return runs[status]
        for action, post in machine.steps.get(status, {}).items():
            if post not in runs:
                runs[post] = (*runs[status], action)
                queue.append(post)
    raise KeyError(f"the {machine.name} machine does not reach {target}")


def fitting(machine: Machine, status: BillStatus) -> tuple[Action, ...]:
    """The operations that go through from ``status``, the delete left out.

    The state operations ``status`` allows, and the update of a draft, which leaves the bill
    in draft (requirement 7.1). The delete is left out on purpose: it is the one operation
    that ends a bill, so a walk that is meant to reach every state does not draw it from
    here - :func:`actions` has it, and :func:`outcome` answers for it either way.
    """
    found: list[Action] = list(machine.steps.get(status, {}))
    if status == DRAFT:
        found.append(UPDATE)
    return tuple(found)


def outcome(machine: Machine, status: BillStatus, action: Action) -> Outcome:
    """What ``action`` does to a bill of ``machine`` that is in ``status``.

    The state operations of the machine lead to the state its own table names, an update
    leaves a draft in draft and a delete removes it (requirements 7.1 and 7.2); every other
    pair of a state and an operation is :class:`Refused`, with the rule id requirement 7.3
    or 7.4 gives it.

    Raises KeyError for an operation ``machine`` does not have, which no caller may ask
    for: no tool offers one and no valid pack declares one (requirement 7.11).
    """
    if action in RECORD_ACTIONS:
        if status != DRAFT:
            return Refused(DRAFT_RULE, status)
        return Removed() if action == DELETE else Written(DRAFT)
    if action not in actions(machine):
        raise KeyError(f"the {machine.name} machine has no {action} operation")
    post = machine.steps.get(status, {}).get(action)
    return Refused(None, status) if post is None else Written(post)


def state_action(action: Action) -> StateAction:
    """``action`` as the state operation it is; an update or a delete fails here.

    The six operations of :data:`STATE_ACTIONS` are the ones a state operation of the engine
    takes (:data:`~erp_mock.domain.state_machine.StateAction`).
    """
    for candidate in STATE_ACTIONS:
        if candidate == action:
            return candidate
    raise AssertionError(f"{action} is no state operation")
