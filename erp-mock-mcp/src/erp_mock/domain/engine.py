"""The engine session: one operation, one transaction, ten named steps (design.md 4.1).

:class:`Engine` is what the tools layer and the seed builder perform business operations
through. One call of :meth:`Engine.run` is one :class:`Operation` against the database of
one sandbox: it opens a transaction, moves the operation through the steps of
:data:`PIPELINE` in order, records which call wrote what, and returns the view of the
result together with the net changes it made (requirement 17.4)::

    engine = Engine(domain)
    result = engine.run(store, OperationContext(anchor_date=date(2026, 10, 9)), GetOp(...))

The transaction is the one of design.md 5.3: every check and every write of an operation
runs inside it, so an error at any step rolls back the records, the counters and the
operation sequence number together and leaves the sandbox exactly as the call found it
(requirements 12.7 and 4.12). A read-only operation runs in a transaction of its own as
well, takes no operation sequence number and makes no writes.

The pipeline is the table of design.md 4.1: ten named slots in a fixed order, so the
order the errors of one call are reported in is the order of the slots and nothing else
(requirement 25.6). Each slot carries the checks of the primitives that belong to it:

=====================  ==================================================================
slot                   what runs in it
=====================  ==================================================================
``references``         every reference the operation names exists, the reference
                       parameters of a search and of a query and the source bill and
                       source lines of a push included (requirement 5.6)
``ownership``          a pushed line's source line belongs to the source bill (8.4)
``bill_status``        R-GEN-001, the state an operation requires, R-GEN-004 (7.3, 7.4)
``pushed_lines``       the lines of a pushed bill are not replaced (5.4)
``candidate``          the record the operation writes: input, mapping, defaults, ids and
                       numbers, amounts and base unit quantities (5.1 to 5.3, 6)
``unit_conversion``    the conversion record a line's unit needs exists (6.7), and what a
                       conversion record a write stores may carry (6.11)
``rules``              R-GEN-002, R-GEN-003, R-GEN-005, R-GEN-006, then the pack rules by
                       id (10.8)
``uniqueness``         no other record holds the same unique value (5.8)
``write``              the records, then the linkages, the writebacks and the automatic
                       close of an approve (7.6, 8.8, 8.13, 9)
``view``               the operation sequence numbers and the view of the result, which
                       for a read is the record, the page or the query result it returns
                       (4.5, 4.11, 5.10 to 5.13, 5.16)
=====================  ==================================================================

A slot decides for itself which operations it concerns; one that concerns none of them
does nothing. A slot holds no logic of its own: it reads what it needs from the session and
calls the primitive the table above names, so the primitives stay testable on their own and
the order of the checks stays here (design.md 4.2).

What the engine performs so far: the read operations of requirements 5.9 to 5.13 and 16.10,
the numbering primitive of requirement 4 - the ``view`` slot gives a write operation its
operation sequence number and writes it into the bills the earlier slots recorded on the
session (:mod:`erp_mock.domain.numbering`) - the create, the update and the delete of the
crud primitive, which fill the ``references``, ``pushed_lines``, ``candidate``, ``rules``,
``uniqueness``, ``write`` and ``view`` slots (:mod:`erp_mock.domain.crud`), the
unit_conversion primitive, which fills the ``unit_conversion`` slot and contributes the base
unit quantity of a measured line to the ``candidate`` one
(:mod:`erp_mock.domain.unit_conversion`), the search of an entity or a bill and the
declarative queries of the exposures, which fill the ``references`` and ``view`` slots
(:mod:`erp_mock.domain.search`, :mod:`erp_mock.domain.query`), the state operations of
the state_machine primitive, which fill the ``bill_status``, ``rules``, ``write`` and
``view`` slots and bring R-GEN-001 to the update and the delete of a bill
(:mod:`erp_mock.domain.state_machine`), the inventory primitive, which moves the stock
of an approve in the ``write`` slot and answers the stock balance query in the ``view``
one (:mod:`erp_mock.domain.inventory`), and the push of the conversion primitive, which
fills the ``references``, ``ownership``, ``bill_status``, ``candidate`` and ``rules`` slots
and whose draft the ``write`` and ``view`` slots store and return like any other new
record (:mod:`erp_mock.domain.conversion`), and the writeback primitive, which brings
R-GEN-003 to the ``rules`` slot and the writebacks and the automatic close and unclose of a
source bill to the ``write`` one (:mod:`erp_mock.domain.writeback`). What an approve and an
unapprove set off beyond the state itself runs in the ``write`` slot with those primitives,
in the order requirements 7.6 and 7.7 give them (:func:`_write_transition`).

The ``rules`` slot is the one slot of the table that no single primitive fills: the four
engine rules of requirement 10.8 belong to three different primitives and the enforced
rules of the business packs to none of them, so the order of all of them, and what each
kind of operation is measured on, live in the rules step
(:mod:`erp_mock.domain.rules`). The slot reads the operation from the session, says which
operation it is (:func:`_rule_step`) and leaves the rest to that step.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Final, Literal, assert_never

from erp_mock.common.errors import ErpError
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import (
    BillModel,
    ConversionModel,
    DomainModel,
    EntityModel,
    QueryModel,
)
from erp_mock.domain.conversion import (
    PushLine,
    build_push_draft,
    check_push_references,
    check_source_lines,
    check_source_status,
    push_lines,
    source_bill,
)
from erp_mock.domain.crud import (
    Candidate,
    build_create_candidate,
    build_update_candidate,
    candidate_view,
    check_input_references,
    check_lines_editable,
    check_uniqueness,
    delete_record,
    not_found,
    target_reference,
    write_candidate,
)
from erp_mock.domain.inventory import reverse_linkages, run_balance_query, run_linkages
from erp_mock.domain.numbering import SequenceStamps, assign_operation_seq
from erp_mock.domain.query import check_query_references, query_filters, run_query
from erp_mock.domain.rule_expr.nodes import Expr
from erp_mock.domain.rules import RuleStep, check_rules, rule_checks
from erp_mock.domain.search import check_search_references, run_search
from erp_mock.domain.state_machine import (
    StateAction,
    apply_transition,
    check_draft,
    check_transition,
)
from erp_mock.domain.unit_conversion import check_unit_conversion
from erp_mock.domain.views import (
    PageView,
    QueryView,
    RecordView,
    business_context_view,
    record_view,
    referenced,
)
from erp_mock.domain.writeback import (
    reverse_writebacks,
    run_writebacks,
    writeback_values,
)
from erp_mock.state.store import Record, SandboxStore, UnitOfWork, Value, Write

__all__ = [
    "PIPELINE",
    "Arguments",
    "BusinessContextOp",
    "CreateOp",
    "DeleteOp",
    "Engine",
    "GetOp",
    "Lines",
    "OpResult",
    "Operation",
    "OperationContext",
    "OperationSession",
    "PipelineStep",
    "PushOp",
    "QueryOp",
    "SearchOp",
    "SlotName",
    "StateAction",
    "TransitionOp",
    "UpdateOp",
    "View",
    "is_write_operation",
]

# The arguments of one level of an operation: the normalized values of the tool's
# parameters, which the tools layer has already typed and rounded (design.md 5.2), and the
# lines array as a sequence of such maps.
type Arguments = FrozenMap[str, Value]
type Lines = tuple[Arguments, ...]

# What one operation returns: one record, one page of records or one query result.
type View = RecordView | PageView | QueryView

# The arguments an operation that writes no values is built with.
_NO_ARGUMENTS: Final[Arguments] = FrozenMap()


@dataclass(frozen=True, slots=True)
class OperationContext:
    """What the engine needs to know about the call it runs (design.md 4.1).

    ``anchor_date`` is the anchor date of the sandbox, which the business date and every
    ``today`` default come from (requirement 16.2); ``current_user_id`` the id of the
    current user, None where the instance configures none (requirement 1.8); ``call_seq``
    the trace sequence number reserved for this tools/call, under which the records it
    writes are recorded as one effective call (requirement 18.10) - None in a seed build
    and for every call that is not traced.
    """

    anchor_date: date
    current_user_id: str | None = None
    call_seq: int | None = None


# --- the operations (design.md 4.1) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateOp:
    """Create one record of the entity or bill ``target`` (requirement 5.1).

    ``values`` are the arguments of the record itself - of the header for a bill - and
    ``lines`` those of its lines, None where the call carried no lines at all.
    """

    target: str
    values: Arguments = _NO_ARGUMENTS
    lines: Lines | None = None


@dataclass(frozen=True, slots=True)
class UpdateOp:
    """Change the record ``id`` of ``target`` (requirements 5.2 and 5.3).

    ``lines`` is None where the call carried no lines, which leaves the lines of the bill
    as they are, and otherwise replaces them as a whole.
    """

    target: str
    id: str
    values: Arguments = _NO_ARGUMENTS
    lines: Lines | None = None


@dataclass(frozen=True, slots=True)
class DeleteOp:
    """Delete the record ``id`` of ``target``, a bill with its lines (requirement 5.5)."""

    target: str
    id: str


@dataclass(frozen=True, slots=True)
class TransitionOp:
    """Run the state operation ``action`` on the bill ``id`` (requirement 7.2).

    ``action`` is one of the six state operations of meta-model.md section 6
    (:data:`~erp_mock.domain.state_machine.StateAction`), which the preset state machine of
    the bill decides the states of (:data:`~erp_mock.domain.state_machine.TRANSITIONS`).
    """

    target: str
    id: str
    action: StateAction


@dataclass(frozen=True, slots=True)
class PushOp:
    """Generate the target bill of the conversion ``conversion`` (requirement 8.1).

    ``source_id`` is the source bill the push reads and ``lines`` the arguments of the
    lines to generate, each one naming the source line it comes from under
    ``source_line_id``. ``source_name`` is the short name the push tool names the source
    bill under, which the field path of that parameter is built from
    (:func:`~erp_mock.domain.conversion.source_path`, requirement 11.13); None takes the
    name of the source bill itself, the short name requirement 11.13 falls back to.

    ``values`` are arguments of the header of the draft, which the mapping of the
    conversion and the defaults of the target bill do not cover
    (:func:`~erp_mock.domain.conversion.push_header`): empty for a push tool, whose
    parameters are the source bill and the line inputs alone (requirement 11.4), and the
    header fields of a pushed record for a seed build, which writes them in this one
    operation so that the bill number follows the business date it gives
    (requirement 20.2).
    """

    conversion: str
    source_id: str
    lines: Lines = ()
    source_name: str | None = None
    values: Arguments = _NO_ARGUMENTS


@dataclass(frozen=True, slots=True)
class GetOp:
    """Read the record ``id`` of ``target``, with its lines (requirement 5.12)."""

    target: str
    id: str


@dataclass(frozen=True, slots=True)
class SearchOp:
    """Search the records of ``target`` (requirements 5.9 to 5.11, 5.14).

    ``args`` are the arguments of the search tool: its keyword, its filters and the page
    to return, under the names requirement 11.5 gives them.
    """

    target: str
    args: Arguments = _NO_ARGUMENTS


@dataclass(frozen=True, slots=True)
class QueryOp:
    """Run the query tool ``tool`` with ``args`` (requirements 5.13, 9.7)."""

    tool: str
    args: Arguments = _NO_ARGUMENTS


@dataclass(frozen=True, slots=True)
class BusinessContextOp:
    """Read the business context of the sandbox (requirement 16.10)."""


Operation = (
    CreateOp
    | UpdateOp
    | DeleteOp
    | TransitionOp
    | PushOp
    | GetOp
    | SearchOp
    | QueryOp
    | BusinessContextOp
)

# The write operations of requirement 4.11: a create of an entity record or a bill, a push,
# an update, a delete and every state operation. Every other operation is read-only.
_WRITE_OPERATIONS: Final = (CreateOp, UpdateOp, DeleteOp, TransitionOp, PushOp)


def is_write_operation(op: Operation) -> bool:
    """Whether ``op`` writes, and so takes an operation sequence number (req 4.11).

    A read-only operation - a get, a search, a query, the business context - changes no
    counter of the sandbox, so the operation sequence numbers count the write operations
    alone.
    """
    return isinstance(op, _WRITE_OPERATIONS)


@dataclass(frozen=True, slots=True)
class OpResult:
    """What one operation produced (design.md 4.1).

    ``view`` is the result of the operation, as typed values with resolved references
    (:mod:`erp_mock.domain.views`), and ``writes`` the net change it made to each
    (record, cause), empty for a read-only operation (requirement 17.4).
    """

    view: View
    writes: tuple[Write, ...] = ()


# --- the pipeline (design.md 4.1) --------------------------------------------------------

# The slots of the pipeline, in the order they run; see the module docstring for what
# belongs in each.
type SlotName = Literal[
    "references",
    "ownership",
    "bill_status",
    "pushed_lines",
    "candidate",
    "unit_conversion",
    "rules",
    "uniqueness",
    "write",
    "view",
]


@dataclass(slots=True)
class OperationSession:
    """The state of one operation while it moves through the pipeline (design.md 4.1).

    One instance per :meth:`Engine.run`: the slots read the operation and the context from
    it and leave what they produced on it.

    - ``record`` is the stored record the operation works on - the one an update changes, a
      delete removes or a push reads as its source - which the ``references`` slot read;
      None for an operation that works on no existing record.
    - ``candidate`` is the record the ``candidate`` slot built, which the slots after it
      check, write and return, None for an operation that writes no record of its own
      (design.md 4.1 step 5).
    - ``removed`` is the object a deleted record had before the ``write`` slot removed it,
      which a delete returns (requirement 13.9).
    - ``stamps`` collects the bills the operation creates and changes, which the ``view``
      slot writes the operation sequence number into (requirement 4.5), and ``op_seq`` is
      that number, None for a read-only operation.
    - ``view`` is the result the ``view`` slot assembles, None until then.
    """

    domain: DomainModel
    uow: UnitOfWork
    ctx: OperationContext
    op: Operation
    record: Record | None = None
    candidate: Candidate | None = None
    removed: RecordView | None = None
    stamps: SequenceStamps = field(default_factory=SequenceStamps)
    op_seq: int | None = None
    view: View | None = None


@dataclass(frozen=True, slots=True)
class PipelineStep:
    """One slot of the pipeline: its name and what runs in it."""

    name: SlotName
    run: Callable[["Engine", OperationSession], None]


def _check_references(engine: "Engine", session: OperationSession) -> None:
    """Step 1: every reference the operation names exists (requirement 5.6).

    The record a read or a write operates on, the references written into a header or a
    line, the reference parameters of a search and of a query, and the source bill and the
    source lines of a push; all of them are collected and reported in one NOT_FOUND whose
    details carry one field path each.

    The engine checks the record a get, an update, a delete or a state operation works on,
    the references a create or an update writes
    (:func:`~erp_mock.domain.crud.check_input_references`), the reference parameters of
    a search and of a query (:func:`~erp_mock.domain.search.check_search_references`,
    :func:`~erp_mock.domain.query.check_query_references`) and the source bill, source lines
    and line inputs of a push
    (:func:`~erp_mock.domain.conversion.check_push_references`); the record an update, a
    delete or a state operation found is left on the session, as is the source bill header
    of a push, so the slots after this one read it rather than the sandbox.
    """
    match session.op:
        case GetOp(target=target, id=record_id):
            _require_record(session, _require_object(session, target), record_id)
        case CreateOp(target=target, values=values, lines=lines):
            check_input_references(
                session.uow, session.domain, _require_object(session, target), values, lines
            )
        case UpdateOp(target=target, id=record_id, values=values, lines=lines):
            item = _require_object(session, target)
            check_input_references(
                session.uow, session.domain, item, values, lines, record_id=record_id
            )
            session.record = _require_record(session, item, record_id)
        case DeleteOp(target=target, id=record_id) | TransitionOp(target=target, id=record_id):
            item = _require_object(session, target)
            session.record = _require_record(session, item, record_id)
        case SearchOp(target=target, args=args):
            check_search_references(
                session.uow, session.domain, _require_object(session, target), args
            )
        case QueryOp(tool=tool, args=args):
            check_query_references(session.uow, session.domain, _require_query(session, tool), args)
        case PushOp(source_id=source_id, lines=lines, source_name=source_name):
            conversion = _require_conversion(session)
            session.record = check_push_references(
                session.uow,
                session.domain,
                conversion,
                source_id,
                _source_lines(session, conversion, lines),
                source_name=source_name,
            )


def _check_ownership(engine: "Engine", session: OperationSession) -> None:
    """Step 2: every source line of a push belongs to the source bill (requirement 8.4).

    Checked before the state of the source bill, so a line of another bill is reported as
    VALIDATION_FAILED even where the source bill is in the wrong state
    (:func:`~erp_mock.domain.conversion.check_source_lines`). No other operation names a
    source line, so every one of them passes through.
    """
    match session.op:
        case PushOp(source_id=source_id, lines=lines, source_name=source_name):
            conversion = _require_conversion(session)
            check_source_lines(
                conversion,
                source_bill(session.domain, conversion),
                source_id,
                _source_lines(session, conversion, lines),
                source_name=source_name,
            )


def _check_bill_status(engine: "Engine", session: OperationSession) -> None:
    """Step 3: the state a bill operation requires (requirements 7.3, 7.4, 7.9 and 8.3).

    R-GEN-001 for a write to a bill that is not a draft, the state a state operation
    starts from, the unclose of a bill the engine closed itself, and R-GEN-004 for the
    source bill of a push. The state_machine primitive decides the first three
    (:func:`~erp_mock.domain.state_machine.check_draft` for the update and the delete of a
    bill, :func:`~erp_mock.domain.state_machine.check_transition` for a state operation)
    and the conversion primitive R-GEN-004
    (:func:`~erp_mock.domain.conversion.check_source_status`), over the source bill header
    the ``references`` slot read.

    An update or a delete of an entity record passes through: R-GEN-001 is about the state
    of a bill, and the records of basic data have none of it.
    """
    match session.op:
        case UpdateOp(target=target) | DeleteOp(target=target):
            bill = _bill_of(session, target)
            if bill is not None:
                check_draft(bill, _require_stored(session))
        case TransitionOp(target=target, action=action):
            check_transition(_require_bill(session, target), _require_stored(session), action)
        case PushOp(source_name=source_name):
            conversion = _require_conversion(session)
            check_source_status(
                conversion,
                source_bill(session.domain, conversion),
                _require_stored(session),
                source_name=source_name,
            )


def _check_pushed_lines(engine: "Engine", session: OperationSession) -> None:
    """Step 4: the lines of a bill a push generated are not replaced (requirement 5.4).

    Only an update carries lines of an existing bill, and only one whose
    ``source_bill_id`` is empty may replace them
    (:func:`~erp_mock.domain.crud.check_lines_editable`). A push passes through: the lines
    it generates are new ones, and it is the ``source_bill_id`` it writes into the draft
    that makes a later update of those lines refuse.
    """
    match session.op:
        case UpdateOp(target=target, lines=lines) if lines is not None:
            bill = _require_object(session, target)
            if isinstance(bill, BillModel):
                check_lines_editable(bill, _require_stored(session))


def _build_candidate(engine: "Engine", session: OperationSession) -> None:
    """Step 5: the record the operation writes (requirements 5.1 to 5.3, 6 and 4.1 to 4.4).

    The arguments and, for a push, the mapping of the conversion are merged with the
    defaults of the level; the internal id, the code, the bill number and the line ids come
    from :mod:`erp_mock.domain.numbering` (:func:`~erp_mock.domain.numbering.next_id`,
    :func:`~erp_mock.domain.numbering.next_code`,
    :func:`~erp_mock.domain.numbering.next_number` and
    :func:`~erp_mock.domain.numbering.line_ids`); the amounts and the base unit quantities
    are computed. The counters are raised inside the transaction, so a later step that
    fails returns them to where they were and the next successful operation gets the same
    id (requirement 4.12). Filled by the crud, numbering, pricing, unit_conversion and
    conversion primitives: the create, the update
    (:func:`~erp_mock.domain.crud.build_create_candidate`,
    :func:`~erp_mock.domain.crud.build_update_candidate`) and the push
    (:func:`~erp_mock.domain.conversion.build_push_draft`), whose draft takes the mapping
    of the conversion and the caller's header values and line inputs first and a default
    only for the fields none of them covers (requirement 8.2). A delete builds no
    candidate: it stores no values.
    """
    ctx = session.ctx
    match session.op:
        case CreateOp(target=target, values=values, lines=lines):
            session.candidate = build_create_candidate(
                session.uow,
                session.domain,
                _require_object(session, target),
                values,
                lines,
                anchor_date=ctx.anchor_date,
                current_user_id=ctx.current_user_id,
            )
        case UpdateOp(target=target, values=values, lines=lines):
            session.candidate = build_update_candidate(
                session.uow,
                session.domain,
                _require_object(session, target),
                _require_stored(session),
                values,
                lines,
                anchor_date=ctx.anchor_date,
                current_user_id=ctx.current_user_id,
            )
        case PushOp(source_id=source_id, lines=lines, values=values):
            conversion = _require_conversion(session)
            session.candidate = build_push_draft(
                session.uow,
                session.domain,
                conversion,
                source_id,
                _require_stored(session),
                _source_lines(session, conversion, lines),
                values=values,
                anchor_date=ctx.anchor_date,
                current_user_id=ctx.current_user_id,
            )


def _check_unit_conversion(engine: "Engine", session: OperationSession) -> None:
    """Step 6: what the unit conversion of the record requires (requirements 6.7 and 6.11).

    For a bill, every line whose unit is not the base unit of its material needs a
    conversion record to read the factor from (requirement 6.7); the check runs here, after
    the state of the bill and before the business rules (requirement 12.10). For the entity
    that holds those conversion records, the unit and the factor the write stores
    (requirement 6.11, design.md 4.2). Both are the unit_conversion primitive's
    (:func:`~erp_mock.domain.unit_conversion.check_unit_conversion`), over the candidate the
    slot before this one built; an operation that writes no record of its own has nothing
    to check.
    """
    candidate = session.candidate
    if candidate is not None:
        check_unit_conversion(
            session.uow, session.domain, candidate.item, candidate.values, candidate.lines
        )


def _check_rules(engine: "Engine", session: OperationSession) -> None:
    """Step 7: the business rules, the engine's own first (requirements 10.7 and 10.8).

    R-GEN-002, R-GEN-003, R-GEN-005 and R-GEN-006, then the enforced rules of the business
    packs that apply to this operation, by rule id in code point order, stopping at the
    first one that does not hold. All of that is the rules step
    (:func:`~erp_mock.domain.rules.check_rules`), which holds the order of the checks and
    knows what each kind of operation is measured on (requirement 10.5); this slot reads
    the operation from the session and tells it which one it is
    (:func:`_rule_step`). The checks of the pack rules are the trees the engine holds
    (:attr:`Engine.checks`), so no call parses one of those texts again.

    A read-only operation and the business context have no rules step: they write nothing
    and no rule is about them.
    """
    step = _rule_step(session)
    if step is None:
        return
    ctx = session.ctx
    check_rules(
        session.uow,
        session.domain,
        step,
        checks=engine.checks,
        anchor_date=ctx.anchor_date,
        current_user_id=ctx.current_user_id,
    )


def _rule_step(session: OperationSession) -> RuleStep | None:
    """The operation the ``rules`` slot checks, None for one no rule is about.

    What :class:`~erp_mock.domain.rules.RuleStep` needs from the session: the operation
    itself, the candidate the ``candidate`` slot built, the record the ``references`` slot
    read - the bill a state operation moves, the bill a delete removes, the source bill of
    a push - and the conversion of a push, whose operation is the ``create`` of its target
    bill (requirement 10.9).
    """
    match session.op:
        case CreateOp():
            return RuleStep(operation="create", candidate=_require_candidate(session))
        case UpdateOp():
            return RuleStep(
                operation="update",
                candidate=_require_candidate(session),
                record=_require_stored(session),
            )
        case PushOp():
            return RuleStep(
                operation="create",
                candidate=_require_candidate(session),
                record=_require_stored(session),
                conversion=_require_conversion(session),
            )
        case TransitionOp(target=target, action=action):
            return RuleStep(
                operation=action,
                record=_require_stored(session),
                bill=_require_bill(session, target),
            )
        case DeleteOp(target=target):
            return RuleStep(
                operation="delete",
                record=_require_stored(session),
                bill=_bill_of(session, target),
            )
        case _:
            return None


def _check_uniqueness(engine: "Engine", session: OperationSession) -> None:
    """Step 8: no other record holds the same unique value (requirements 5.8 and 12.11).

    After the business rules, so a call that breaks a rule and a unique constraint at once
    reports the rule. The crud primitive compares the unique values the candidate writes
    (:func:`~erp_mock.domain.crud.check_uniqueness`); an operation that writes no record of
    its own has none.
    """
    if session.candidate is not None:
        check_uniqueness(session.uow, session.candidate)


def _write(engine: "Engine", session: OperationSession) -> None:
    """Step 9: the records, then what their change sets off (requirements 7.6, 8.8 and 9).

    An approve writes the state first, then the inventory linkages, then the writebacks
    and the automatic close of the source bill; an unapprove reverses the linkages and the
    writebacks in the opposite order (:func:`_write_transition`). Everything happens in the
    transaction of this operation. Filled by the crud, state_machine, inventory, conversion
    and writeback primitives.

    A candidate the ``candidate`` slot built is stored here
    (:func:`~erp_mock.domain.crud.write_candidate`): a new record is inserted and a record
    the sandbox already holds takes the fields the call changes. A bill it created is
    recorded on the session as created and one it changed as changed, so the ``view`` slot
    writes this operation's sequence number into both sequence number fields of a new bill
    and into the ``updated_seq`` of a changed one (requirement 4.5).

    A delete removes the record and, for a bill, its lines
    (:func:`~erp_mock.domain.crud.delete_record`); the object the record had before it is
    removed stays on the session, since that is what a delete returns (requirement 13.9).
    """
    candidate = session.candidate
    if candidate is not None:
        write_candidate(session.uow, candidate)
        if candidate.bill is not None:
            if candidate.is_new:
                session.stamps.created_bill(candidate.item.name, candidate.id)
            else:
                session.stamps.changed_bill(candidate.item.name, candidate.id)
        return
    match session.op:
        case TransitionOp():
            _write_transition(engine, session)
        case DeleteOp(target=target):
            item = _require_object(session, target)
            record = _require_stored(session)
            session.removed = record_view(session.uow, session.domain, item, record)
            delete_record(session.uow, session.domain, item, record)


def _write_transition(engine: "Engine", session: OperationSession) -> None:
    """The write of one state operation: the state, then what it sets off (7.6, 7.7).

    The order of requirements 7.6 and 7.7, all of it in the transaction of this operation:

    1. the bill takes the state of the transition
       (:func:`~erp_mock.domain.state_machine.apply_transition`), a close recording that it
       was a manual one (requirement 7.8);
    2. an approve runs the linkages of the bill type and then the writebacks of the
       conversions whose target this bill is, the automatic close of their source bills
       last (requirements 8.8 and 8.13);
    3. an unapprove reverses the two in the opposite order: the writebacks and the close of
       a source bill first (requirements 8.9 and 8.14), the linkages after them.

    The linkages are the inventory primitive, where R-GEN-007 is decided
    (:func:`~erp_mock.domain.inventory.run_linkages`,
    :func:`~erp_mock.domain.inventory.reverse_linkages`), and the writebacks and the
    automatic close and unclose of a source bill the writeback primitive
    (:func:`~erp_mock.domain.writeback.run_writebacks`,
    :func:`~erp_mock.domain.writeback.reverse_writebacks`), which reads the values of the
    writebacks from the trees the engine holds (:attr:`Engine.values`); an error of either
    rolls back the state this function just wrote together with everything else of the call
    (requirement 7.10). The bill is recorded as changed, so the ``view`` slot writes this
    operation's sequence number into its ``updated_seq`` (requirement 4.5); a source bill
    one of those steps changes is recorded there by the step that changes it.
    """
    op = session.op
    assert isinstance(op, TransitionOp)
    ctx = session.ctx
    bill = _require_bill(session, op.target)
    header = _require_stored(session)
    apply_transition(session.uow, bill, header, op.action)
    session.stamps.changed_bill(bill.name, op.id)
    if op.action == "approve":
        run_linkages(session.uow, session.domain, bill, header)
        run_writebacks(
            session.uow,
            session.domain,
            bill,
            header,
            session.stamps,
            values=engine.values,
            anchor_date=ctx.anchor_date,
            current_user_id=ctx.current_user_id,
        )
    elif op.action == "unapprove":
        reverse_writebacks(session.uow, session.domain, bill, header, session.stamps)
        reverse_linkages(session.uow, session.domain, bill, header)


def _assemble_view(engine: "Engine", session: OperationSession) -> None:
    """Step 10: the operation sequence numbers, then the view of the result.

    A write operation takes the next operation sequence number of the sandbox and the
    numbering primitive writes it into ``created_seq`` and ``updated_seq`` of the bills the
    earlier slots recorded on the session (requirements 4.5 and 4.11); a read-only
    operation takes none, so the counter counts the write operations alone. The view is
    assembled afterwards and is the one a get of the record returns, the new sequence
    numbers included (requirements 5.16 and 13.2).
    """
    if is_write_operation(session.op):
        session.op_seq = assign_operation_seq(session.uow, session.stamps)
    session.view = _result_view(engine, session)


# The ten steps of design.md 4.1, in the order Engine.run runs them.
PIPELINE: Final[tuple[PipelineStep, ...]] = (
    PipelineStep("references", _check_references),
    PipelineStep("ownership", _check_ownership),
    PipelineStep("bill_status", _check_bill_status),
    PipelineStep("pushed_lines", _check_pushed_lines),
    PipelineStep("candidate", _build_candidate),
    PipelineStep("unit_conversion", _check_unit_conversion),
    PipelineStep("rules", _check_rules),
    PipelineStep("uniqueness", _check_uniqueness),
    PipelineStep("write", _write),
    PipelineStep("view", _assemble_view),
)


class Engine:
    """Performs the operations of the engine against the database of one sandbox.

    One instance per loaded instance configuration, shared by every sandbox of it: the
    engine holds no state of a call, which lives on the :class:`OperationSession` of that
    call, and reads the business configuration from the resolved domain model alone
    (requirement 1.10).

    ``filters`` are the syntax trees of the filters of the declarative queries, by tool
    name, ``values`` those of the values of the writebacks, by writeback id, and ``checks``
    those of the checks of the enforced pack rules, by rule id: the caller that holds them -
    the loader, which parsed and checked every rule expression in step 5 - passes them in,
    and an engine built from a domain model alone parses them itself
    (:func:`~erp_mock.domain.query.query_filters`,
    :func:`~erp_mock.domain.writeback.writeback_values`,
    :func:`~erp_mock.domain.rules.rule_checks`), which is the same tree for the same text.
    Either way no call parses one of those texts again (requirement 10.2).
    """

    __slots__ = ("_checks", "_domain", "_filters", "_values")

    def __init__(
        self,
        domain: DomainModel,
        filters: Mapping[str, Expr] | None = None,
        values: Mapping[str, Expr] | None = None,
        checks: Mapping[str, Expr] | None = None,
    ) -> None:
        self._domain = domain
        self._filters = FrozenMap(filters) if filters is not None else query_filters(domain)
        self._values = FrozenMap(values) if values is not None else writeback_values(domain)
        self._checks = FrozenMap(checks) if checks is not None else rule_checks(domain)

    @property
    def domain(self) -> DomainModel:
        """The resolved configuration of the instance the engine serves."""
        return self._domain

    @property
    def filters(self) -> FrozenMap[str, Expr]:
        """The parsed filter of each declarative query that declares one (req 10.10)."""
        return self._filters

    @property
    def values(self) -> FrozenMap[str, Expr]:
        """The parsed value of each writeback of the instance (requirements 8.8, 10.2)."""
        return self._values

    @property
    def checks(self) -> FrozenMap[str, Expr]:
        """The parsed check of each enforced pack rule (requirements 10.2 and 10.8)."""
        return self._checks

    def run(self, store: SandboxStore, ctx: OperationContext, op: Operation) -> OpResult:
        """Run ``op`` against ``store`` in one transaction and return its result.

        The steps of :data:`PIPELINE` run in order; the first one that fails raises
        :class:`~erp_mock.common.errors.ErpError` and the transaction rolls back, so the
        sandbox is left as the call found it (requirement 12.7). A successful operation
        that wrote something records those records as the effective call ``ctx.call_seq``,
        in the same transaction (requirement 18.10).

        Raises ErpError for every business error of the operation, and NotImplementedError
        for an operation whose primitive the engine does not have yet.
        """
        with store.transaction() as uow:
            session = OperationSession(domain=self._domain, uow=uow, ctx=ctx, op=op)
            for step in PIPELINE:
                step.run(self, session)
            writes = uow.net_writes()
            if writes and ctx.call_seq is not None:
                uow.record_effective_call(ctx.call_seq, writes)
            return OpResult(view=_require_view(session), writes=writes)


def _result_view(engine: "Engine", session: OperationSession) -> View:
    """The view of the result of ``session``'s operation (design.md 4.1 step 10)."""
    op = session.op
    match op:
        case GetOp(target=target, id=record_id):
            item = _require_object(session, target)
            record = _require_record(session, item, record_id)
            return record_view(session.uow, session.domain, item, record)
        case BusinessContextOp():
            ctx = session.ctx
            return business_context_view(
                session.uow, session.domain, ctx.anchor_date, ctx.current_user_id
            )
        case CreateOp() | UpdateOp() | PushOp():
            # The record the ``write`` slot stored, read back with the sequence numbers the
            # ``view`` slot has just written into it: for a push the draft it generated,
            # which is what a get of it returns (requirements 5.16 and 13.2).
            return candidate_view(session.uow, session.domain, _require_candidate(session))
        case DeleteOp():
            # The object the record had before the ``write`` slot removed it (13.9).
            return _require_removed(session)
        case SearchOp(target=target, args=args):
            return run_search(session.uow, session.domain, _require_object(session, target), args)
        case QueryOp(tool=tool, args=args):
            ctx = session.ctx
            query = _require_query(session, tool)
            if query.primitive is not None:
                # The one query a primitive provides rather than an exposure: the stock
                # balance of the inventory primitive, whose parameters and result the
                # primitive defines (requirements 9.7 and 9.12).
                return run_balance_query(session.uow, session.domain, query, args)
            return run_query(
                session.uow,
                session.domain,
                query,
                args,
                filter_tree=engine.filters.get(tool),
                anchor_date=ctx.anchor_date,
                current_user_id=ctx.current_user_id,
            )
        case TransitionOp(target=target, id=record_id):
            # The bill as it stands after the write, read back with the state and the
            # sequence number this operation gave it: the object a get of it returns right
            # afterwards (requirement 13.2).
            item = _require_object(session, target)
            record = _require_record(session, item, record_id)
            return record_view(session.uow, session.domain, item, record)
        case _:
            assert_never(op)


def _require_view(session: OperationSession) -> View:
    """The view the ``view`` slot assembled; a missing one is a programming error."""
    if session.view is None:
        raise RuntimeError(f"no slot assembled the view of {type(session.op).__name__}")
    return session.view


def _require_candidate(session: OperationSession) -> Candidate:
    """The candidate the ``candidate`` slot built; a missing one is a programming error."""
    if session.candidate is None:
        raise RuntimeError(f"no slot built the candidate of {type(session.op).__name__}")
    return session.candidate


def _require_stored(session: OperationSession) -> Record:
    """The record the ``references`` slot read; a missing one is a programming error.

    For a push that record is the header of its source bill, which every slot after the
    first one reads from here rather than from the sandbox (requirement 8.12).
    """
    if session.record is None:
        raise RuntimeError(f"no slot read the record of {type(session.op).__name__}")
    return session.record


def _require_removed(session: OperationSession) -> RecordView:
    """The object of the deleted record; a missing one is a programming error."""
    if session.removed is None:
        raise RuntimeError(f"no slot kept the object {type(session.op).__name__} removed")
    return session.removed


def _require_conversion(session: OperationSession) -> ConversionModel:
    """The conversion a push runs; an unknown id is a programming error.

    Only the push tools of the loaded instance reach the engine, and each of them names a
    conversion of its configuration (requirement 11.1).
    """
    op = session.op
    if not isinstance(op, PushOp):
        raise RuntimeError(f"{type(op).__name__} is no push and names no conversion")
    conversion = session.domain.conversions.get(op.conversion)
    if conversion is None:
        raise RuntimeError(f"the instance has no conversion named {op.conversion!r}")
    return conversion


def _source_lines(
    session: OperationSession, conversion: ConversionModel, lines: Lines
) -> tuple[PushLine, ...]:
    """The lines of a push with the source line each of them names (requirement 8.1).

    Read from the sandbox in the slot that needs them, so every slot works on the lines as
    the transaction of this operation sees them
    (:func:`~erp_mock.domain.conversion.push_lines`).
    """
    return push_lines(session.uow, source_bill(session.domain, conversion), lines)


def _require_object(session: OperationSession, target: str) -> EntityModel | BillModel:
    """The entity or bill ``target`` names; an unknown name is a programming error.

    Only the tools of the loaded instance reach the engine, and each of them names an
    object of its configuration (requirement 11.1).
    """
    item = referenced(session.domain, target)
    if item is None:
        raise RuntimeError(f"the instance has no entity or bill named {target!r}")
    return item


def _bill_of(session: OperationSession, target: str) -> BillModel | None:
    """The bill ``target`` names, None when it names an entity (requirement 7.3)."""
    item = _require_object(session, target)
    return item if isinstance(item, BillModel) else None


def _require_bill(session: OperationSession, target: str) -> BillModel:
    """The bill ``target`` names; an entity is a programming error.

    A state operation runs on a bill alone: the records of basic data have no state machine
    and no tool offers them one (requirement 7.2).
    """
    bill = _bill_of(session, target)
    if bill is None:
        raise RuntimeError(f"{target} is no bill and has no state operations")
    return bill


def _require_query(session: OperationSession, tool: str) -> QueryModel:
    """The query the tool ``tool`` runs; an unknown tool name is a programming error.

    The queries of the instance are keyed by the name of the tool that runs them
    (requirement 11.1), and only the tools of the instance reach the engine.
    """
    query = session.domain.queries.get(tool)
    if query is None:
        raise RuntimeError(f"the instance has no query tool named {tool!r}")
    return query


def _require_record(
    session: OperationSession, item: EntityModel | BillModel, record_id: str
) -> Record:
    """The record ``record_id`` of ``item``, or NOT_FOUND (requirements 5.5 and 5.6).

    The detail item names the parameter the id came from, which for the record an
    operation works on is ``id`` (requirement 11.4). The error itself is built by
    :func:`~erp_mock.domain.crud.not_found`, which every reference of a call is reported
    through.
    """
    record = session.uow.get(item.name, record_id)
    if record is None:
        raise _not_found(session, item, record_id)
    return record


def _not_found(
    session: OperationSession, item: EntityModel | BillModel, record_id: str
) -> ErpError:
    """The NOT_FOUND of the record an operation works on (design.md data model 6)."""
    return not_found(session.domain, (target_reference(item, record_id),))
