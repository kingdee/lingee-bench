"""The business rules step: the engine's own rules by id, then the pack rules (req 10.8).

Step 7 of the pipeline is the one place a write operation is measured against the rules of
the instance (design.md 4.1). Four of them belong to the engine and are decided by the
primitives that own them; the rest are the ``enforced`` rules the business packs declare,
whose ``check`` this module evaluates. :func:`check_rules` runs all of them in the one
order requirement 10.8 fixes and stops at the first rule that does not hold:

======================  =================================================================
rule                    what decides it
======================  =================================================================
``R-GEN-002``           :func:`~erp_mock.domain.state_machine.check_required`: a bill
                        leaves draft with its lines and its required values in place
                        (requirement 7.5)
``R-GEN-003``           :func:`~erp_mock.domain.writeback.check_no_downstream`: an
                        approved bill a push has generated a bill from is not unapproved
                        (requirement 8.10)
``R-GEN-005``           :func:`~erp_mock.domain.conversion.check_push_quota`: a push stays
                        within the quota of its source (requirements 8.5 to 8.7)
``R-GEN-006``           :func:`~erp_mock.domain.crud.check_disabled_references`: no
                        reference this call writes points at disabled basic data (5.7)
pack ``enforced``       :func:`check_pack_rules`: the ``check`` of every rule that applies
rules                   to this operation, by rule id in code point order
======================  =================================================================

R-GEN-001 and R-GEN-004 are decided earlier, in the ``bill_status`` slot, and R-GEN-007
later, when an approve moves the stock, so neither takes part in this order
(requirement 10.8). The ``uniqueness`` slot runs after this step, which is why a call that
breaks a rule and a unique constraint at once reports the rule (requirements 5.8 and
12.11).

What the step is told about the operation is :class:`RuleStep`: which operation it is, the
record it writes (the ``candidate`` slot's :class:`~erp_mock.domain.crud.Candidate`), the
record the sandbox holds, and the conversion of a push. The engine's ``rules`` slot builds
one from its session and this module derives the rest, so the order of the checks and the
object they are evaluated on live here rather than in the pipeline (design.md 4.2).

Which pack rules apply (requirement 10.9). An ``enforced`` rule applies when the bill the
operation changed is the rule's ``bill`` and the operation is listed in its ``on``
(:func:`rule_applies`). The ``create`` of an ``on`` covers both a direct create and the
push that generates the bill, so a draft a conversion produced is measured against the same
rules as one a caller wrote (:class:`RuleStep` of a push carries the target draft). What an
approve sets off on *another* bill is no operation on that bill: the amounts a writeback
adds to a source bill and the automatic close of it are writes of the approve of the
downstream bill, so a rule on the source bill with ``update`` or ``close`` in its ``on`` is
not evaluated for them - this step runs once per call, on the bill the call names. A
``described`` rule is never evaluated and never stops a call; it is text the model reads in
the description of a tool (requirement 3.6), and the configuration gives it no ``check``
to evaluate in the first place.

What a rule is evaluated on (requirement 10.5). The bill *after* the operation, which each
kind of operation reaches differently (:func:`evaluated_bill`):

=========================  ==============================================================
operation                  the bill the check reads
=========================  ==============================================================
``create``, a push         the candidate: the caller's input, the mapping of the
                           conversion and the defaults, with the computed fields of
                           requirement 6 already in it
``update``                 the candidate: the stored record with the fields of the call
                           merged in and the computed fields recomputed
``submit`` ... ``unclose`` the stored record, with ``status`` the state the operation
                           writes (:data:`~erp_mock.domain.state_machine.TRANSITIONS`),
                           since a state operation writes no value of its own
``delete``                 the stored record, as it stands before it is removed
=========================  ==============================================================

``header.<field>`` reads that record and ``line.<field>`` each of its lines - the lines of
the candidate for a create, a push and an update, the stored lines for a state operation
and a delete. The rest of the environment is the one every rule expression is evaluated in
(:class:`~erp_mock.domain.query.RecordEnv`): ``context.today`` and ``context.period`` from
the anchor date, ``context.user.<field>`` from the current user's record, a ref path
expanded through the sandbox, and ``exists`` and ``sum_of`` reading a whole table once per
call - the records as the call found them, since this step runs before anything is written
(requirement 10.13).

The error of a rule that does not hold is the rule's own (requirement 10.7): its
``error_code``, its id as the ``rule_id`` and the text of its ``message.zh``, which is what
requirement 13.7 asks of a message a pack wrote. The details carry the field path of its
``field`` (:func:`rule_details`): one item for a check that names no ``line.`` path, none
at all when the rule declares no field, and one item per failing line, by line number
ascending, for a check that does - ``lines[i].<field>`` with ``i`` the line number minus
one, or ``lines[i]`` for a rule without a field.

The checks are evaluated on their syntax trees, never on the text again: :func:`rule_checks`
parses every enforced rule of an instance, which the engine does once per loaded instance
and hands to this step (:attr:`~erp_mock.domain.engine.Engine.checks`). Loading step 5 has
already parsed the same text and checked that it resolves against the bill, is well typed
and is boolean (``loader.step5_expr``, requirement 10.2); parsing is a pure function of the
text, so the tree here is the tree the loader checked, and domain must not import loader
(requirement 24.8), which is why this module can produce them on its own.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.model import (
    BillModel,
    ConversionModel,
    DomainModel,
    FieldModel,
    RuleModel,
)
from erp_mock.config.pack import WriteOperation
from erp_mock.domain.conversion import check_push_quota
from erp_mock.domain.crud import Candidate, check_disabled_references, line_path, own_fields
from erp_mock.domain.query import QueryTables, RecordEnv, StoredRecord, user_record
from erp_mock.domain.rule_expr.evaluator import CheckResult, eval_check
from erp_mock.domain.rule_expr.nodes import Expr
from erp_mock.domain.rule_expr.parser import parse_expr
from erp_mock.domain.state_machine import (
    STATUS,
    StateAction,
    check_required,
    transition,
)
from erp_mock.domain.views import LINES, bill_lines
from erp_mock.domain.writeback import check_no_downstream
from erp_mock.state.schema import ID_COLUMN
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "ENFORCED",
    "RULE_CHECK_FAILED_REASON",
    "EvaluatedBill",
    "RuleStep",
    "applicable_rules",
    "check_pack_rules",
    "check_rules",
    "evaluated_bill",
    "rule_applies",
    "rule_check",
    "rule_checks",
    "rule_details",
    "rule_env",
    "rule_error",
    "state_action",
]

# The enforcement the engine evaluates; a ``described`` rule is never evaluated
# (requirement 10.9).
ENFORCED: Final = "enforced"

# The reason of design.md data model 6 every pack rule reports: its check was false.
RULE_CHECK_FAILED_REASON: Final = "rule_check_failed"

# The keys of a detail item (design.md data model 6): the input field it is about, then the
# reason. A pack rule carries no extra key.
_FIELD_KEY: Final = "field"
_REASON_KEY: Final = "reason"

# The error code a rule that declares none would end in; every enforced rule declares one
# (``EnforcedRuleDef`` of :mod:`erp_mock.config.pack`).
_DEFAULT_CODE: Final = ErrorCode.BUSINESS_RULE_VIOLATION


@dataclass(frozen=True, slots=True)
class RuleStep:
    """One write operation as the business rules step sees it (design.md 4.1 step 7).

    - ``operation`` is the operation of the call, which a rule's ``on`` is matched against:
      ``create`` for a push as well, since the ``on`` of a rule covers the push that
      generates its bill (requirement 10.9).
    - ``candidate`` is the record the call writes, which a create, a push and an update
      have and which is what their rules are evaluated on (requirement 10.5); None for a
      state operation and a delete, neither of which writes a value of its own.
    - ``record`` is the record the sandbox holds: the bill a state operation moves or a
      delete removes, and the *source* bill of a push, which R-GEN-005 reads
      (requirement 8.12). None for a create.
    - ``bill`` is the bill of an operation that builds no candidate - a state operation, the
      delete of a bill - and None for the delete of an entity record; the bill of a create,
      a push or an update is the one its candidate carries (:attr:`target`).
    - ``conversion`` is the conversion a push runs, None for every other operation.
    """

    operation: WriteOperation
    candidate: Candidate | None = None
    record: Record | None = None
    bill: BillModel | None = None
    conversion: ConversionModel | None = None

    @property
    def target(self) -> BillModel | None:
        """The bill this operation changed, None for an operation on an entity record.

        The bill of the candidate where there is one - for a push the target bill of its
        conversion, which is the bill its rules apply to - and the bill the caller named
        otherwise.
        """
        candidate = self.candidate
        return self.bill if candidate is None else candidate.bill

    @property
    def action(self) -> StateAction | None:
        """The state operation this is, None for a create, a push, an update or a delete."""
        return state_action(self.operation)


@dataclass(frozen=True, slots=True)
class EvaluatedBill:
    """The bill the checks of one operation are evaluated on (requirement 10.5).

    ``header`` is what ``header.<field>`` reads and ``lines`` what ``line.<field>`` is
    evaluated over, by line number; a bill without lines satisfies every check that names a
    ``line.`` path (requirement 10.7).
    """

    bill: BillModel
    header: Record
    lines: tuple[Record, ...] = ()


def state_action(operation: WriteOperation) -> StateAction | None:
    """``operation`` as the state operation it is, None when it is not one (req 7.2).

    The six operations that move a bill from one state to the next; a create, a push, an
    update and a delete are not among them.
    """
    match operation:
        case "submit" | "unsubmit" | "approve" | "unapprove" | "close" | "unclose":
            return operation
        case _:
            return None


# --- the checks of the enforced rules (requirement 10.2) ---------------------------------


def rule_check(rule: RuleModel) -> Expr | None:
    """The syntax tree of the ``check`` of ``rule``, None where it declares none.

    A ``described`` rule declares no check and is never evaluated (requirement 10.9).
    Raises :class:`~erp_mock.domain.rule_expr.errors.ExprError` for a text that does not
    parse, which loading step 5 reported and refused to start on (requirement 10.2).
    """
    if rule.enforcement != ENFORCED or rule.check is None:
        return None
    return parse_expr(rule.check)


def rule_checks(domain: DomainModel) -> FrozenMap[str, Expr]:
    """The syntax tree of the ``check`` of every enforced rule of ``domain``, by rule id.

    A described rule is absent, as is an enforced rule without a check. Built once per
    loaded instance, so no call parses a check text again; a caller that holds the trees -
    the loader's ``ParsedDomain`` - hands them to the engine instead
    (:class:`~erp_mock.domain.engine.Engine`).
    """
    return FrozenMap(
        (rule.id, tree) for rule in domain.rules.values() if (tree := rule_check(rule)) is not None
    )


# --- which rules apply to one operation (requirement 10.9) -------------------------------


def rule_applies(rule: RuleModel, bill: str, operation: WriteOperation) -> bool:
    """Whether ``rule`` is evaluated on ``operation`` of the bill ``bill`` (req 10.9).

    An enforced rule whose ``bill`` is this bill and whose ``on`` lists this operation; the
    ``create`` of an ``on`` covers the push that generates the bill as well, which the
    caller passes as ``create``. A described rule never applies, whatever it names.
    """
    return (
        rule.enforcement == ENFORCED
        and rule.check is not None
        and rule.bill == bill
        and operation in rule.on
    )


def applicable_rules(
    domain: DomainModel, bill: str, operation: WriteOperation
) -> tuple[RuleModel, ...]:
    """The enforced rules of ``operation`` of ``bill``, by rule id in code point order.

    The order requirement 10.8 fixes for the pack rules - ``R-INV-001`` before
    ``R-PUR-001`` - whatever order the business packs and their files declared them in, so
    which rule a call that breaks several reports does not depend on the instance.
    """
    return tuple(
        sorted(
            (rule for rule in domain.rules.values() if rule_applies(rule, bill, operation)),
            key=_rule_key,
        )
    )


# --- what the checks of one operation are evaluated on (requirement 10.5) ----------------


def evaluated_bill(uow: UnitOfWork, step: RuleStep) -> EvaluatedBill | None:
    """The bill after ``step``'s operation, None for an operation on no bill (req 10.5).

    The candidate of a create, a push or an update, lines and all; the stored bill with the
    state the operation writes for a state operation; the stored bill as it stands for a
    delete (see the module docstring). None for an operation on an entity record, which no
    pack rule is about, and for one that carries neither a candidate nor a stored record.
    """
    bill = step.target
    if bill is None:
        return None
    candidate = step.candidate
    if candidate is not None:
        return EvaluatedBill(bill=bill, header=candidate.values, lines=candidate.lines)
    record = step.record
    if record is None:
        return None
    return EvaluatedBill(
        bill=bill,
        header=_after_transition(bill, record, step.action),
        lines=bill_lines(uow, bill, _id_of(record)),
    )


def rule_env(
    uow: UnitOfWork,
    domain: DomainModel,
    evaluated: EvaluatedBill,
    *,
    anchor_date: date,
    current_user_id: str | None = None,
) -> RecordEnv:
    """What the checks of ``evaluated`` are evaluated in (requirements 10.5 and 10.13).

    One environment per operation, shared by every rule of it: ``header.<field>`` reads the
    bill the operation produced, ``line.<field>`` the line the check is evaluated for,
    ``sum(lines.F)`` and ``count(lines)`` its lines, the context paths the business context
    of the call, and an ``exists`` or a ``sum_of`` a whole table once
    (:class:`~erp_mock.domain.query.QueryTables`).
    """
    bill = evaluated.bill
    return RecordEnv(
        anchor_date=anchor_date,
        header_record=_stored(uow, domain, own_fields(bill), evaluated.header),
        tables=QueryTables(uow=uow, domain=domain),
        line_records=tuple(_stored(uow, domain, bill.lines, line) for line in evaluated.lines),
        user_record=user_record(uow, domain, current_user_id),
    )


# --- the error of a rule that does not hold (requirement 10.7) ---------------------------


def rule_details(rule: RuleModel, result: CheckResult) -> tuple[Mapping[str, JsonValue], ...]:
    """The detail items of the error of ``rule`` (requirements 10.7, design data model 6).

    A check that names no ``line.`` path gets one item with the rule's ``field`` as its
    path, and none at all where the rule declares no field. A check that names one gets an
    item per failing line, by line number ascending, whose path is
    ``lines[<line number> - 1].<field>``, or ``lines[<line number> - 1]`` where the rule
    declares no field.
    """
    name = None if rule.field is None else rule.field.name
    if not result.per_line:
        return () if name is None else (_detail(name),)
    return tuple(_detail(_line_path(number - 1, name)) for number in result.failing_lines)


def rule_error(rule: RuleModel, result: CheckResult) -> ErpError:
    """The error of the rule ``rule`` on a bill its check is false for (req 10.7, 13.7).

    The ``error_code`` of the rule, its id as the ``rule_id`` and the text of its
    ``message.zh``, which the tools layer reports as it stands; the details are
    :func:`rule_details`. Raises ValueError for a satisfied result: an error says what did
    not hold.
    """
    if result.satisfied:
        raise ValueError(f"the check of {rule.id} holds, so it ends no call")
    code = _DEFAULT_CODE if rule.error_code is None else ErrorCode(rule.error_code)
    return ErpError(code, _message(rule), rule_id=rule.id, details=rule_details(rule, result))


# --- the step itself (requirement 10.8) --------------------------------------------------


def check_pack_rules(
    uow: UnitOfWork,
    domain: DomainModel,
    step: RuleStep,
    *,
    checks: Mapping[str, Expr] | None = None,
    anchor_date: date,
    current_user_id: str | None = None,
) -> None:
    """The enforced pack rules of ``step``, by rule id, stopping at the first one (10.8).

    ``checks`` are the syntax trees of the rule checks by rule id, which the engine holds
    (:func:`rule_checks`); the text of a rule that is not among them is parsed here. Raises
    the error of the first rule whose check is false (:func:`rule_error`); a rule whose
    check is unknown holds, as an SQL CHECK constraint does (requirement 10.6).

    An operation on an entity record and one that no enforced rule applies to evaluate
    nothing, which is also why the environment is built only where a rule needs it.
    """
    bill = step.target
    if bill is None:
        return
    rules = applicable_rules(domain, bill.name, step.operation)
    if not rules:
        return
    evaluated = evaluated_bill(uow, step)
    if evaluated is None:
        return
    trees: Mapping[str, Expr] = FrozenMap() if checks is None else checks
    env = rule_env(uow, domain, evaluated, anchor_date=anchor_date, current_user_id=current_user_id)
    for rule in rules:
        tree = trees.get(rule.id)
        if tree is None:
            tree = rule_check(rule)
        if tree is None:
            continue
        result = eval_check(tree, env)
        if not result.satisfied:
            raise rule_error(rule, result)


def check_rules(
    uow: UnitOfWork,
    domain: DomainModel,
    step: RuleStep,
    *,
    checks: Mapping[str, Expr] | None = None,
    anchor_date: date,
    current_user_id: str | None = None,
) -> None:
    """Step 7 of a write: the engine's rules by id, then the pack rules (req 10.8).

    R-GEN-002, R-GEN-003, R-GEN-005 and R-GEN-006 in that order, each decided by the
    primitive that owns it and each only for the operation it is about, then the enforced
    rules of the business packs that apply to this operation (:func:`check_pack_rules`).
    The first rule that does not hold raises its own
    :class:`~erp_mock.common.errors.ErpError` and nothing after it is evaluated.
    """
    bill, record, action = step.bill, step.record, step.action
    if action is not None and bill is not None and record is not None:
        # R-GEN-002 and R-GEN-003, over the bill as the sandbox holds it: a state operation
        # writes no value of its own (requirements 7.5 and 8.10).
        lines = bill_lines(uow, bill, _id_of(record))
        check_required(bill, record, lines, action)
        check_no_downstream(uow, domain, bill, record, action)
    conversion, candidate = step.conversion, step.candidate
    if conversion is not None and record is not None and candidate is not None:
        # R-GEN-005, over the draft the ``candidate`` slot built and the source bill the
        # ``references`` slot read (requirements 8.5 to 8.7).
        check_push_quota(uow, domain, conversion, record, candidate)
    if candidate is not None:
        # R-GEN-006, over the references this call writes (requirement 5.7).
        check_disabled_references(uow, domain, candidate)
    check_pack_rules(
        uow,
        domain,
        step,
        checks=checks,
        anchor_date=anchor_date,
        current_user_id=current_user_id,
    )


# --- internals ---------------------------------------------------------------------------


def _after_transition(bill: BillModel, record: Record, action: StateAction | None) -> Record:
    """``record`` with the state ``action`` writes, unchanged for a delete (req 10.5).

    A state operation writes no value of its own, so its check reads the stored fields with
    ``status`` taken from the transition of the bill's own state machine
    (:data:`~erp_mock.domain.state_machine.TRANSITIONS`); an action that machine does not
    have writes no state and leaves the record as it is.
    """
    found = None if action is None else transition(bill.state_machine, action)
    if found is None:
        return record
    values: dict[str, Value] = dict(record)
    values[STATUS] = found.post
    return FrozenMap(values)


def _stored(
    uow: UnitOfWork, domain: DomainModel, fields: Mapping[str, FieldModel], record: Record
) -> StoredRecord:
    """``record`` as an expression reads it, with the field view of its level."""
    return StoredRecord(uow=uow, domain=domain, fields=fields, record=record)


def _rule_key(rule: RuleModel) -> str:
    """The sort key of a pack rule: its id, which str compares by code point (req 10.8)."""
    return rule.id


def _detail(path: str) -> Mapping[str, JsonValue]:
    """One detail item of a pack rule: the field path, then the reason (data model 6)."""
    return FrozenMap[str, JsonValue](((_FIELD_KEY, path), (_REASON_KEY, RULE_CHECK_FAILED_REASON)))


def _line_path(index: int, name: str | None) -> str:
    """The path of a failing line: ``lines[i].<field>``, or ``lines[i]`` (req 10.7)."""
    return f"{LINES}[{index}]" if name is None else line_path(index, name)


def _message(rule: RuleModel) -> str:
    """The message of the error of ``rule``: its ``message.zh`` (requirement 13.7).

    Every enforced rule declares one (``EnforcedRuleDef`` of
    :mod:`erp_mock.config.pack`); a rule without one is named by its id alone.
    """
    return f"不满足业务规则 {rule.id}" if rule.message is None else rule.message.zh


def _id_of(record: Record) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""
