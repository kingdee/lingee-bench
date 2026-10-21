"""The state_machine primitive: which state a bill operation starts from and leads to.

Every bill follows one of the two preset state machines of meta-model.md section 6, and
which one is the ``state_machine`` of its business pack (requirements 7.1 and 7.2). The
transitions are a constant table of ``(machine, action) -> (pre, post)``
(:data:`TRANSITIONS`, design.md 4.2), so a state operation is one lookup: the bill must be
in the ``pre`` state of its own machine, and a successful operation writes the ``post``
state::

    standard  draft --submit--> submitted --approve--> approved --close--> closed
              draft <-unsubmit- submitted <-unapprove- approved <-unclose- closed
    simple    draft --approve--> approved
              draft <-unapprove- approved

What this module contributes to the slots of the pipeline (design.md 4.1):

=====================  ==================================================================
slot                   what this module contributes
=====================  ==================================================================
``bill_status``        :func:`check_draft`: R-GEN-001, only a draft is changed or deleted
                       (requirement 7.3); :func:`check_transition`: the state the action
                       starts from (7.4) and the unclose of a bill the engine closed
                       itself (7.9)
``rules``              :func:`check_required`: R-GEN-002, the lines and the required
                       values a bill needs to leave draft (requirement 7.5)
``write``              :func:`apply_transition`: the new state of the bill, and the way it
                       was closed (requirements 7.1, 7.2 and 7.8)
=====================  ==================================================================

Three errors, each with the error code and the rule id requirement 13.8 gives it:

- a write to a bill that is not a draft - an update or a delete - is INVALID_STATE with
  the rule id ``R-GEN-001`` (requirement 7.3);
- a state operation whose bill is not in the state the table names, and the unclose of a
  bill that was closed automatically, are INVALID_STATE that no rule decides, so their
  rule id is None (requirements 7.4 and 7.9);
- a bill that leaves draft without lines or without a required value is
  VALIDATION_FAILED with the rule id ``R-GEN-002`` (requirement 7.5).

All three report the same way: the detail item of an INVALID_STATE carries the current
state of the bill under ``status`` and names no input field (design.md data model 6), and
R-GEN-002 carries one item per missing value, each with its field path - a field of the
header by its own name, a field of a line as ``lines[i].<name>`` with ``i`` the line number
minus one, and the whole ``lines`` array for a bill that has no line at all.
:func:`invalid_state` is the one place that error is built, so the conversion primitive
raises R-GEN-004 through it as well, naming the parameter its source bill came from
(requirement 8.3).

Which values R-GEN-002 asks for is requirement 7.5, and all of it comes from the
configuration (:func:`required_header_fields`, :func:`required_line_fields`): the header
and line fields the pack declared ``required``, the ``qty``, ``price`` and ``tax_rate`` of
the lines of a pricing bill, and the material reference, the unit reference and the
quantity of lines that declare quantity semantics. The check runs on the bill as the
sandbox holds it - a state operation writes no value of its own - and in the ``rules`` slot,
where requirement 10.8 puts the engine rules.

How a bill was closed lives in the internal column ``_close_mode`` (requirement 7.8,
design.md 4.2): :data:`MANUAL_CLOSE` for the close operation, which includes the close a
seed build runs, and :data:`AUTO_CLOSE` for the close the conversion primitive performs
through ``auto_close_source_when``. :func:`write_status` keeps the column and the state
together - a bill that leaves ``closed`` loses its close mode - so the column is set
exactly for the bills that are closed, and an unclose can tell the two kinds apart
(requirements 7.9 and 8.14).

What is not here: the linkages, the writebacks and the automatic close an approve sets off
and an unapprove reverses (requirements 7.6, 7.7 and 7.10). They run in the ``write`` slot
right after :func:`apply_transition`, in the same transaction: the stock of the inventory
primitive (:func:`~erp_mock.domain.inventory.run_linkages`), then the amounts and the
automatic close of the writeback primitive
(:func:`~erp_mock.domain.writeback.run_writebacks`), which changes the state of a source
bill through :func:`write_status` (design.md 4.2).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.config.model import BillModel, StateMachine
from erp_mock.config.pack import BillStatus
from erp_mock.domain.views import LINES
from erp_mock.state.schema import CLOSE_MODE_COLUMN, ID_COLUMN
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "APPROVED",
    "AUTO_CLOSE",
    "CLOSED",
    "DRAFT",
    "DRAFT_RULE",
    "INVALID_STATE_REASON",
    "MANUAL_CLOSE",
    "REQUIRED_MISSING_REASON",
    "REQUIRED_RULE",
    "STATUS",
    "SUBMITTED",
    "TRANSITIONS",
    "CloseMode",
    "StateAction",
    "Transition",
    "apply_transition",
    "bill_status",
    "check_draft",
    "check_required",
    "check_transition",
    "close_mode",
    "invalid_state",
    "leaves_draft",
    "missing_required",
    "required_header_fields",
    "required_line_fields",
    "status_text",
    "transition",
    "write_status",
]

# The state operations of a bill, the subset of the write operations that moves it from
# one state to the next (meta-model.md section 6, requirement 7.2).
type StateAction = Literal["submit", "unsubmit", "approve", "unapprove", "close", "unclose"]

# How a bill reached the closed state (requirement 7.8): the close operation, or the
# automatic close of a source bill the conversion primitive performs.
type CloseMode = Literal["manual", "auto"]

MANUAL_CLOSE: Final[CloseMode] = "manual"
AUTO_CLOSE: Final[CloseMode] = "auto"

# The state field of a bill header and the four states of meta-model.md section 6; a simple
# bill holds the first and the third of them (:data:`TRANSITIONS`).
STATUS: Final = "status"
DRAFT: Final[BillStatus] = "draft"
SUBMITTED: Final[BillStatus] = "submitted"
APPROVED: Final[BillStatus] = "approved"
CLOSED: Final[BillStatus] = "closed"

# The engine rules this module decides: only a draft is changed or deleted, and a bill
# leaves draft with its lines and its required values in place (requirements 7.3 and 7.5).
DRAFT_RULE: Final = "R-GEN-001"
REQUIRED_RULE: Final = "R-GEN-002"

# The reasons of design.md data model 6 this module reports: a state the operation does not
# allow, which carries the current state under ``status``, and a required value that is
# empty.
INVALID_STATE_REASON: Final = "invalid_state"
REQUIRED_MISSING_REASON: Final = "required_missing"

# The three fields of a pricing line a bill needs filled to leave draft (requirement 7.5);
# the amounts are computed from them (requirement 6.1).
_PRICING_FIELDS: Final[tuple[str, ...]] = ("qty", "price", "tax_rate")

# The standard field of a bill header a message names the bill by (meta-model.md 4).
_NUMBER: Final = "number"

# The keys of a detail item (design.md data model 6): the input field it is about, the
# reason, then the extra keys of that reason.
_FIELD_KEY: Final = "field"
_REASON_KEY: Final = "reason"
_STATUS_KEY: Final = "status"

# The Chinese name of each state operation, for the messages of this module alone; what a
# result returns is the code of a state and never a label (requirement 13.4).
_ACTION_LABELS: Final[Mapping[StateAction, str]] = {
    "submit": "提交",
    "unsubmit": "撤回提交",
    "approve": "审核",
    "unapprove": "反审核",
    "close": "关闭",
    "unclose": "反关闭",
}


@dataclass(frozen=True, slots=True)
class Transition:
    """One row of the transition table: the state before and the state after.

    ``pre`` is the state the bill must be in for the action to run (requirement 7.4) and
    ``post`` the state a successful action writes (requirements 7.1 and 7.2).
    """

    pre: BillStatus
    post: BillStatus


# The transition table of design.md 4.2, the one place the preset state machines are
# written down: ``(machine, action) -> (pre, post)``. A pair it does not hold is an
# operation that machine does not have - the submit, unsubmit, close and unclose of a
# simple bill, whose approve runs from draft instead (requirements 7.2 and 7.11).
TRANSITIONS: Final[Mapping[tuple[StateMachine, StateAction], Transition]] = {
    ("standard", "submit"): Transition(DRAFT, SUBMITTED),
    ("standard", "unsubmit"): Transition(SUBMITTED, DRAFT),
    ("standard", "approve"): Transition(SUBMITTED, APPROVED),
    ("standard", "unapprove"): Transition(APPROVED, DRAFT),
    ("standard", "close"): Transition(APPROVED, CLOSED),
    ("standard", "unclose"): Transition(CLOSED, APPROVED),
    ("simple", "approve"): Transition(DRAFT, APPROVED),
    ("simple", "unapprove"): Transition(APPROVED, DRAFT),
}


def transition(machine: StateMachine, action: StateAction) -> Transition | None:
    """The transition ``action`` makes in ``machine``, None when it has no such action.

    None for the submit, unsubmit, close and unclose of a simple bill, which no tool offers
    and no valid pack declares (requirement 7.11).
    """
    return TRANSITIONS.get((machine, action))


def leaves_draft(bill: BillModel, action: StateAction) -> bool:
    """Whether ``action`` takes ``bill`` out of draft, which R-GEN-002 guards (req 7.5).

    The submit of a standard bill and the approve of a simple one: the one transition of
    each machine whose ``pre`` is draft, and so the point a bill needs its lines and its
    required values (requirement 7.5).
    """
    found = transition(bill.state_machine, action)
    return found is not None and found.pre == DRAFT


def bill_status(record: Record) -> str:
    """The state the bill ``record`` is in, the empty string where it holds none.

    Every bill is created in draft (requirement 7.1), so a record without a state is one
    the engine never wrote.
    """
    status = record.get(STATUS)
    return status if isinstance(status, str) else ""


def close_mode(record: Record) -> str | None:
    """How the bill ``record`` was closed, None for a bill that is not closed (req 7.8).

    :data:`MANUAL_CLOSE` for a bill the close operation closed, :data:`AUTO_CLOSE` for one
    the conversion primitive closed through ``auto_close_source_when``
    (:func:`write_status`).
    """
    mode = record.get(CLOSE_MODE_COLUMN)
    return mode if isinstance(mode, str) else None


# --- the checks of the bill_status slot (requirements 7.3, 7.4 and 7.9) ------------------


def check_draft(bill: BillModel, record: Record) -> None:
    """Step 3 of an update or a delete: the bill is a draft (R-GEN-001, requirement 7.3).

    A bill in any other state of its machine - submitted, approved or closed for a standard
    bill, approved for a simple one - ends the call in INVALID_STATE with the rule id
    ``R-GEN-001``, whose one detail item carries the state the bill is in.
    """
    status = bill_status(record)
    if status == DRAFT:
        return
    raise invalid_state(
        bill,
        record,
        f"当前状态为{status_text(bill, status)}，只有草稿可以修改或删除",
        rule_id=DRAFT_RULE,
    )


def check_transition(bill: BillModel, record: Record, action: StateAction) -> Transition:
    """Step 3 of a state operation: the bill is in the state it starts from (req 7.4, 7.9).

    Returns the transition the action makes (:data:`TRANSITIONS`). A bill in another state
    ends the call in INVALID_STATE that no rule decides, so its rule id is None
    (requirement 13.8), and so does the unclose of a bill the engine closed itself, which
    stays closed (requirement 7.9); either error carries the state of the bill.

    Raises RuntimeError when the machine of the bill has no such action: no tool offers one
    and no valid pack declares one (requirement 7.11).
    """
    found = _require_transition(bill, action)
    status = bill_status(record)
    if status != found.pre:
        raise invalid_state(
            bill,
            record,
            f"当前状态为{status_text(bill, status)}，"
            f"{_action_text(action)}要求{status_text(bill, found.pre)}",
        )
    if action == "unclose" and close_mode(record) == AUTO_CLOSE:
        raise invalid_state(bill, record, "由下推自动关闭，不能反关闭，只能反审核下游单据")
    return found


def invalid_state(
    bill: BillModel,
    record: Record,
    reason: str,
    *,
    rule_id: str | None = None,
    field: str | None = None,
) -> ErpError:
    """The INVALID_STATE of a bill operation (requirements 7.3, 7.4, 7.9 and 8.3).

    One detail item, which carries the state the bill is in under ``status``; ``field`` is
    the input field the bill came from, which only a push has - the parameter that names
    its source bill (requirement 8.3) - and which every other operation leaves out, since
    the bill it works on comes from the ``id`` of the tool (design.md data model 6).
    ``rule_id`` is ``R-GEN-001`` or ``R-GEN-004`` where one of those rules decided and None
    everywhere else (requirement 13.8). The message names the bill and ``reason`` says what
    about its state does not fit.
    """
    status = bill_status(record)
    item: dict[str, str] = {} if field is None else {_FIELD_KEY: field}
    item.update({_REASON_KEY: INVALID_STATE_REASON, _STATUS_KEY: status})
    return ErpError(
        ErrorCode.INVALID_STATE,
        f"{_bill_text(bill, record)}{reason}",
        rule_id=rule_id,
        details=(item,),
    )


# --- R-GEN-002, in the rules slot (requirements 7.5 and 10.8) ----------------------------


def required_header_fields(bill: BillModel) -> tuple[str, ...]:
    """The header fields ``bill`` needs filled to leave draft (requirement 7.5).

    The fields the business pack declared ``required``, in field view order; no standard
    field of a header is required of itself.
    """
    return tuple(name for name, field in bill.header.items() if field.required)


def required_line_fields(bill: BillModel) -> tuple[str, ...]:
    """The line fields ``bill`` needs filled in every line to leave draft (req 7.5).

    In field view order: the fields the pack declared ``required``, the ``qty``, ``price``
    and ``tax_rate`` of a pricing bill, and the material reference, the unit reference and
    the quantity of lines that declare quantity semantics - the quantity being the standard
    ``qty`` of a pricing bill and the declared field of a bill without pricing
    (:attr:`~erp_mock.config.model.BillModel.qty_field`).
    """
    needed = {name for name, field in bill.lines.items() if field.required}
    if bill.pricing:
        needed.update(_PRICING_FIELDS)
    quantity = bill.line_quantity
    if quantity is not None:
        needed.update({quantity.material_field, quantity.unit_field})
        if bill.qty_field is not None:
            needed.add(bill.qty_field)
    return tuple(name for name in bill.lines if name in needed)


def missing_required(bill: BillModel, header: Record, lines: Sequence[Record]) -> tuple[str, ...]:
    """The field paths ``bill`` is missing to leave draft, in report order (req 7.5).

    The required header fields that are empty, by their own names and in field view order,
    then ``lines`` for a bill that has no line at all, or the required line fields that are
    empty as ``lines[i].<name>``, by line order and field view order. Empty when the bill
    may leave draft.
    """
    missing = [name for name in required_header_fields(bill) if header.get(name) is None]
    if not lines:
        missing.append(LINES)
        return tuple(missing)
    needed = required_line_fields(bill)
    missing.extend(
        _line_path(index, name)
        for index, line in enumerate(lines)
        for name in needed
        if line.get(name) is None
    )
    return tuple(missing)


def check_required(
    bill: BillModel, header: Record, lines: Sequence[Record], action: StateAction
) -> None:
    """Step 7 of a state operation: R-GEN-002 (requirements 7.5 and 10.8).

    Only for the action that takes the bill out of draft (:func:`leaves_draft`): the submit
    of a standard bill and the approve of a simple one. Raises VALIDATION_FAILED with the
    rule id ``R-GEN-002`` and one detail item per missing item, each naming its field path;
    the message ends in the path of the first of them, the ``lines`` of a bill without
    lines included, so it always carries the path of a detail item (requirement 13.7).
    """
    if not leaves_draft(bill, action):
        return
    missing = missing_required(bill, header, lines)
    if not missing:
        return
    first = missing[0]
    reason = "没有分录" if first == LINES else "有必填字段为空"
    message = (
        f"{_bill_text(bill, header)}{reason}，"
        f"{_action_text(action)}要求至少一行分录和必填字段齐全：{first}"
    )
    raise ErpError(
        ErrorCode.VALIDATION_FAILED,
        _with_rest(message, len(missing), "处缺失"),
        rule_id=REQUIRED_RULE,
        details=_details(missing, REQUIRED_MISSING_REASON),
    )


# --- the write of the state (requirements 7.1, 7.2 and 7.8) ------------------------------


def write_status(
    uow: UnitOfWork,
    bill: BillModel,
    bill_id: str,
    status: BillStatus,
    *,
    mode: CloseMode | None = None,
) -> None:
    """Write ``status`` into the bill ``bill_id``, with the way it was closed (req 7.8).

    ``mode`` is how the bill was closed and belongs to ``closed`` alone: the close
    operation passes :data:`MANUAL_CLOSE` and the automatic close of a source bill passes
    :data:`AUTO_CLOSE` (design.md 4.2). A bill that reaches any other state loses its close
    mode, so the internal column is set exactly for the bills that are closed and an
    unclose can tell a manual close from an automatic one (requirements 7.9 and 8.14).

    Raises ValueError for a ``mode`` on a state that is not closed and for a close without
    one: how a bill was closed is never left open.
    """
    closing = status == CLOSED
    if closing and mode is None:
        raise ValueError(f"closing {bill.name} {bill_id} needs the way it was closed")
    if not closing and mode is not None:
        raise ValueError(f"the state {status} of {bill.name} {bill_id} takes no close mode")
    uow.update(bill.name, bill_id, {STATUS: status, CLOSE_MODE_COLUMN: mode})


def apply_transition(
    uow: UnitOfWork, bill: BillModel, record: Record, action: StateAction
) -> Transition:
    """Step 9 of a state operation: the new state of the bill (requirements 7.1 and 7.2).

    ``record`` is the bill as the earlier slots read it, which :func:`check_transition` has
    already found in the state ``action`` starts from; the bill takes the state of the
    transition, and a close is recorded as a manual one (requirement 7.8). Returns the
    transition that ran, so the caller knows which state the bill left.

    The linkages and the writebacks of an approve, and their reversal on an unapprove, run
    after this in the same transaction (requirements 7.6 and 7.7); they are the inventory,
    conversion and writeback primitives, and both go through :func:`write_status` where
    they change the state of a source bill.
    """
    found = _require_transition(bill, action)
    mode = MANUAL_CLOSE if found.post == CLOSED else None
    write_status(uow, bill, _id_of(record), found.post, mode=mode)
    return found


# --- internals ---------------------------------------------------------------------------


def _require_transition(bill: BillModel, action: StateAction) -> Transition:
    """The transition ``action`` makes on ``bill``, whose machine has to have it.

    Raises RuntimeError otherwise: no tool offers such an operation and no valid pack
    declares one (requirement 7.11).
    """
    found = transition(bill.state_machine, action)
    if found is None:
        raise RuntimeError(
            f"the {bill.state_machine} state machine of {bill.name} has no {action} operation"
        )
    return found


def _line_path(index: int, name: str) -> str:
    """The path of ``name`` in the line at ``index``, counting from 0 (requirement 7.5)."""
    return f"{LINES}[{index}].{name}"


def _details(paths: Iterable[str], reason: str) -> tuple[Mapping[str, str], ...]:
    """One detail item per field path, the reason after it (design.md data model 6)."""
    return tuple({_FIELD_KEY: path, _REASON_KEY: reason} for path in paths)


def _bill_text(bill: BillModel, record: Record) -> str:
    """How a message names the bill: its label and its number, or its id (req 13.7)."""
    number = record.get(_NUMBER)
    name = number if isinstance(number, str) and number else _id_of(record)
    label = "" if bill.label is None else bill.label.zh
    return f"{label} {name} ".lstrip()


def status_text(bill: BillModel, status: str) -> str:
    """A state as a message writes it: its Chinese label and its code (requirement 13.7).

    For messages alone; what a result and a detail item carry is the code of the state and
    never a label (requirement 13.4).
    """
    field = bill.header.get(STATUS)
    label = None if field is None else field.enum_values.get(status)
    return status if label is None else f"{label.zh}（{status}）"


def _action_text(action: StateAction) -> str:
    """A state operation as a message writes it: its Chinese name (requirement 13.7)."""
    return _ACTION_LABELS[action]


def _with_rest(message: str, total: int, rest: str) -> str:
    """``message``, saying how many further items of the same kind there are."""
    return message if total < 2 else f"{message}；另有 {total - 1} {rest}"


def _id_of(record: Mapping[str, Value]) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""
