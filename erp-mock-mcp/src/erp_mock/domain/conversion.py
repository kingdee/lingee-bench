"""The conversion primitive: the target bill draft a push generates from a source bill.

A push reads one source bill and writes one draft of the conversion's target bill: the
header takes the ``header_mapping`` of the conversion, every line takes the
``line_mapping`` of the source line it names and the ``required_line_inputs`` the caller
sent, and the draft records where it came from (requirement 8.1). What this module
contributes to the slots of the pipeline (design.md 4.1):

=====================  ==================================================================
slot                   what this module contributes
=====================  ==================================================================
``references``         :func:`check_push_references`: the source bill, the source line of
                       every line and the references the caller sent exist, all of them
                       collected into one NOT_FOUND (requirement 5.6)
``ownership``          :func:`check_source_lines`: every source line belongs to the source
                       bill, checked before its state (requirements 8.4 and 12.10)
``bill_status``        :func:`check_source_status`: R-GEN-004, the source bill is in the
                       state the conversion requires (requirement 8.3)
``candidate``          :func:`build_push_draft`: the draft - the mapping, the caller's
                       inputs, the fields the push writes itself, then the defaults of the
                       fields none of them cover (requirements 8.1 and 8.2)
``rules``              :func:`check_push_quota`: R-GEN-005, the quota of each source line
                       or of the source bill (requirements 8.5, 8.6 and 8.7)
=====================  ==================================================================

The source bill is left exactly as it was (requirement 8.12): this module reads it and
writes nothing into it, so its state, its link field and its ``updated_seq`` are the ones
the push found. What the target draft gets instead: ``source_bill_type`` and
``source_bill_id`` on the header, the ``source_line_id`` of every line, and the internal
column ``_conversion``, which names the conversion that generated the draft and is what the
quota of the next push is counted per (design.md 4.2). The draft is a draft like any other
(requirement 7.1) and is numbered, priced and measured by the same code a create goes
through (:func:`~erp_mock.domain.crud.build_push_candidate`).

Defaults (requirement 8.2). A field the ``header_mapping``, the ``line_mapping`` or the
``required_line_inputs`` name is covered: it keeps what the mapping or the caller gave it,
an empty value included, and takes no ``default``, ``default_from`` or ``default_context``.
Every other field takes its default the way a create fills it, which is how a pushed draft
gets its business date, its organization and the constants of its own bill. A required
field that stays empty does not stop the push; R-GEN-002 asks for it at submit
(requirement 7.5).

The quota, R-GEN-005 (requirements 8.5 to 8.7, glossary Pushable_Qty). What is left of a
source depends on the ``limit_by`` of the conversion:

- ``qty``: per source line, its own ``qty_base`` minus the ``qty_base`` of every line of
  every bill this conversion generated and the sandbox still holds - a draft among them.
  The lines of one push that name the same source line are checked together, by the sum of
  their ``qty_base`` (requirement 8.7), so one source line never yields more than one
  detail item; the items are ordered by the index of the line that names the source line
  first, and each carries the quantity field path of that line, the source line and what
  was left (requirement 8.5);
- ``amount``: per source bill, its ``amount_with_tax`` minus that of every bill this
  conversion generated from it, against the ``amount_with_tax`` of the draft itself, which
  the pricing primitive has computed by the time this check runs (requirement 8.6).

A quota that is used up exactly is not exceeded: the check is on *greater than*
(requirements 8.5 and 8.6).

What is not here: the writebacks the approve of a pushed bill runs, the automatic close and
unclose of a source bill, and R-GEN-003 (requirements 8.8 to 8.10, 8.13 and 8.14). They
belong to the ``rules`` and ``write`` slots of a state operation and are the writeback
primitive (:mod:`erp_mock.domain.writeback`), which reads the ``_conversion`` column and the
link field this module's drafts carry.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from erp_mock.common.decimals import PLACES, WIDE, format_decimal, round_half_up
from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import BillModel, ConversionModel, DomainModel
from erp_mock.domain.crud import (
    Arguments,
    Candidate,
    Reference,
    build_push_candidate,
    field_path,
    level_references,
    line_path,
    missing_references,
    not_found,
)
from erp_mock.domain.pricing import AMOUNT_FIELDS
from erp_mock.domain.state_machine import bill_status, invalid_state, status_text
from erp_mock.domain.unit_conversion import QTY_BASE
from erp_mock.state.schema import BILL_ID_COLUMN, CONVERSION_COLUMN, ID_COLUMN
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "EXCEEDS_AMOUNT_REASON",
    "EXCEEDS_QTY_REASON",
    "NOT_IN_SOURCE_REASON",
    "QUOTA_RULE",
    "SOURCE_BILL_ID",
    "SOURCE_BILL_TYPE",
    "SOURCE_LINE_ID",
    "SOURCE_STATUS_RULE",
    "LineQuota",
    "PushLine",
    "build_push_draft",
    "check_push_quota",
    "check_push_references",
    "check_source_lines",
    "check_source_status",
    "foreign_lines",
    "line_quotas",
    "missing_push_references",
    "occupied_amount",
    "occupied_qty",
    "push_header",
    "push_line",
    "push_lines",
    "pushable_amount",
    "pushable_qty",
    "source_bill",
    "source_path",
    "target_bill",
]

# The two engine rules this module decides: the state the source bill of a push must be in,
# and the quota a push may not exceed (requirements 8.3, 8.5 and 8.6).
SOURCE_STATUS_RULE: Final = "R-GEN-004"
QUOTA_RULE: Final = "R-GEN-005"

# The reasons of design.md data model 6 this module reports: a source line of another bill
# (requirement 8.4) and the two kinds of quota, whose items carry the source line and what
# was left (requirements 8.5 and 8.6).
NOT_IN_SOURCE_REASON: Final = "not_in_source_bill"
EXCEEDS_QTY_REASON: Final = "exceeds_pushable_qty"
EXCEEDS_AMOUNT_REASON: Final = "exceeds_pushable_amount"

# The standard fields a push writes to say where the draft came from (meta-model.md 4): the
# bill type and the id of the source on the header, the source line on every line.
SOURCE_BILL_TYPE: Final = "source_bill_type"
SOURCE_BILL_ID: Final = "source_bill_id"
SOURCE_LINE_ID: Final = "source_line_id"

# The header field the quota of a push limited by amount is counted in (requirement 8.6).
_AMOUNT_WITH_TAX: Final = AMOUNT_FIELDS[2]

# The suffix the parameter that names the source bill ends with (requirement 11.4).
_ID_SUFFIX: Final = "_id"

# The keys of a detail item and the extra keys of the two quota reasons (design.md data
# model 6).
_FIELD_KEY: Final = "field"
_REASON_KEY: Final = "reason"
_PUSHABLE_QTY_KEY: Final = "pushable_qty"
_PUSHABLE_AMOUNT_KEY: Final = "pushable_amount"

# The precision of the two kinds of quota (requirement 4.7).
_QTY_PLACES: Final = PLACES["qty"]
_MONEY_PLACES: Final = PLACES["money"]
_ZERO: Final = Decimal(0)

# The header values of a push whose caller sent none, which is every push tool (req 11.4).
_NO_VALUES: Final[Arguments] = FrozenMap()


@dataclass(frozen=True, slots=True)
class PushLine:
    """One line of a push: what the caller sent and the source line it names (req 8.1).

    ``index`` is the position of the line in the ``lines`` array, which its field paths
    count from 0 (requirement 5.6); ``args`` the ``required_line_inputs`` the caller sent
    for it; ``source_line_id`` the value of its ``source_line_id``, the empty string where
    it carries none; and ``record`` the source line that value names, None where the
    sandbox has no such line (requirement 5.6).
    """

    index: int
    args: Arguments
    source_line_id: str
    record: Record | None = None

    @property
    def path(self) -> str:
        """The field path of the ``source_line_id`` of this line (requirements 5.6, 8.4)."""
        return line_path(self.index, SOURCE_LINE_ID)

    @property
    def bill_id(self) -> str:
        """The bill the source line belongs to, the empty string without a source line."""
        return "" if self.record is None else _text(self.record.get(BILL_ID_COLUMN))


@dataclass(frozen=True, slots=True)
class LineQuota:
    """What one source line has left against the lines of one push (requirements 8.5, 8.7).

    ``pushed`` is the sum of the ``qty_base`` of every line of this push that names the
    source line and ``pushable`` what the source line had left before it; ``path`` is the
    quantity field path of the line that names the source line first, which the detail item
    of a violation reports (requirement 8.5).
    """

    source_line_id: str
    path: str
    pushed: Decimal
    pushable: Decimal

    @property
    def exceeded(self) -> bool:
        """Whether the push takes more than was left; an exact fit is allowed (req 8.5)."""
        return self.pushed > self.pushable


# --- what a conversion names (meta-model.md section 7) -----------------------------------


def source_bill(domain: DomainModel, conversion: ConversionModel) -> BillModel:
    """The bill a push of ``conversion`` reads (requirement 8.1).

    Raises RuntimeError when the instance has no such bill: loading step 5 resolves the
    source of every conversion (requirement 2.9).
    """
    return _require_bill(domain, conversion, conversion.source)


def target_bill(domain: DomainModel, conversion: ConversionModel) -> BillModel:
    """The bill a push of ``conversion`` generates (requirement 8.1).

    Raises RuntimeError when the instance has no such bill, as :func:`source_bill` does.
    """
    return _require_bill(domain, conversion, conversion.target)


def source_path(conversion: ConversionModel, source_name: str | None = None) -> str:
    """The field path of the parameter that names the source bill (requirement 11.13).

    ``<short name>_id``, the short name being the one the push tool is named after;
    ``source_name`` is None where the caller knows none, which takes the name of the source
    bill itself - the short name requirement 11.13 falls back to.
    """
    short = conversion.source if source_name is None else source_name
    return field_path(f"{short}{_ID_SUFFIX}")


def push_lines(
    uow: UnitOfWork, source: BillModel, lines: Sequence[Arguments]
) -> tuple[PushLine, ...]:
    """The lines of a push with the source line each of them names (requirement 8.1).

    One :class:`PushLine` per element of the array, in the order the caller sent them: a
    line whose ``source_line_id`` is empty or names no line of ``source`` carries no record,
    which the ``references`` slot reports (requirement 5.6).
    """
    found: list[PushLine] = []
    for index, args in enumerate(lines):
        source_line_id = _text(args.get(SOURCE_LINE_ID))
        found.append(
            PushLine(
                index=index,
                args=args,
                source_line_id=source_line_id,
                record=uow.get(source.line_name, source_line_id),
            )
        )
    return tuple(found)


# --- step 1: the references of a push (requirement 5.6) ----------------------------------


def missing_push_references(
    uow: UnitOfWork,
    domain: DomainModel,
    conversion: ConversionModel,
    source_id: str,
    lines: Sequence[PushLine],
    *,
    source_name: str | None = None,
) -> tuple[Reference, ...]:
    """The references of a push that no record answers, in report order (requirement 5.6).

    The source bill first, under the path of the parameter that names it
    (:func:`source_path`), then the references of each line in line order: its
    ``source_line_id``, which comes first in the field view of a line, and then the
    references the caller sent among its ``required_line_inputs``, in field view order. A
    deleted record and a record of another type count as absent.
    """
    source = source_bill(domain, conversion)
    target = target_bill(domain, conversion)
    missing: list[Reference] = []
    if uow.get(source.name, source_id) is None:
        missing.append(
            Reference(path=source_path(conversion, source_name), target=source.name, id=source_id)
        )
    for line in lines:
        if line.record is None:
            missing.append(
                Reference(path=line.path, target=source.line_name, id=line.source_line_id)
            )
        named = tuple(level_references(target.lines, line.args, _line_paths(line.index)))
        missing.extend(missing_references(uow, domain, named))
    return tuple(missing)


def check_push_references(
    uow: UnitOfWork,
    domain: DomainModel,
    conversion: ConversionModel,
    source_id: str,
    lines: Sequence[PushLine],
    *,
    source_name: str | None = None,
) -> Record:
    """Step 1 of a push: everything it names is there (requirements 5.6 and 12.1).

    Returns the source bill header, which the slots after this one read rather than the
    sandbox. Raises one NOT_FOUND for every reference that is not there at once
    (:func:`~erp_mock.domain.crud.not_found`), the source lines among them: a
    ``source_line_id`` that names an existing line of another bill is not a missing
    reference but a line of the wrong bill, which the ``ownership`` slot reports
    (requirement 8.4).
    """
    missing = missing_push_references(
        uow, domain, conversion, source_id, lines, source_name=source_name
    )
    if missing:
        raise not_found(domain, missing)
    header = uow.get(source_bill(domain, conversion).name, source_id)
    if header is None:
        raise RuntimeError(f"the source bill {source_id} of {conversion.id} disappeared")
    return header


# --- step 2: the source lines belong to the source bill (requirement 8.4) ----------------


def foreign_lines(source_id: str, lines: Sequence[PushLine]) -> tuple[PushLine, ...]:
    """The lines whose source line belongs to another bill, in line order (req 8.4).

    A line without a source line is not among them: that one is a missing reference, which
    the ``references`` slot has already reported (requirement 5.6).
    """
    return tuple(line for line in lines if line.record is not None and line.bill_id != source_id)


def check_source_lines(
    conversion: ConversionModel,
    source: BillModel,
    source_id: str,
    lines: Sequence[PushLine],
    *,
    source_name: str | None = None,
) -> None:
    """Step 2 of a push: every source line belongs to the source bill (req 8.4, 12.10).

    Runs before the state of the source bill is looked at, so a line of another bill is
    reported even where that bill is in the wrong state. Raises VALIDATION_FAILED, which no
    rule decides (requirement 13.8), with one detail item per such line, each naming its
    ``source_line_id`` path; the message names the path of the first of them and the
    parameter the source bill came from (requirement 13.7).
    """
    foreign = foreign_lines(source_id, lines)
    if not foreign:
        return
    first = foreign[0]
    message = (
        f"{first.path} 的分录 {first.source_line_id} 不属于"
        f" {source_path(conversion, source_name)} 指定的{_label(source)} {source_id}，"
        "下推只能引用该单据的分录"
    )
    raise ErpError(
        ErrorCode.VALIDATION_FAILED,
        _with_rest(message, len(foreign), "处分录不属于该单据"),
        details=_details((line.path for line in foreign), NOT_IN_SOURCE_REASON),
    )


# --- step 3: R-GEN-004, the state of the source bill (requirement 8.3) -------------------


def check_source_status(
    conversion: ConversionModel,
    source: BillModel,
    header: Record,
    *,
    source_name: str | None = None,
) -> None:
    """Step 3 of a push: the source bill is in the state it requires (R-GEN-004, req 8.3).

    Raises INVALID_STATE with the rule id ``R-GEN-004``, whose one detail item names the
    parameter the source bill came from and carries the state the bill is in.
    """
    status = bill_status(header)
    if status == conversion.require_source_status:
        return
    raise invalid_state(
        source,
        header,
        f"当前状态为{status_text(source, status)}，"
        f"下推要求{status_text(source, conversion.require_source_status)}",
        rule_id=SOURCE_STATUS_RULE,
        field=source_path(conversion, source_name),
    )


# --- step 5: the draft a push generates (requirements 8.1 and 8.2) -----------------------


def push_header(
    conversion: ConversionModel,
    source_id: str,
    header: Record,
    values: Arguments = _NO_VALUES,
) -> dict[str, Value]:
    """The header values of a pushed draft before its defaults (requirements 8.1, 8.2).

    The fields of the ``header_mapping``, each taking the value of the source header field
    it names - empty where the source has none - then the header values ``values`` the
    caller sent, then the three the push writes itself: the bill type and the id of the
    source, and the internal column that names the conversion. Every key of the result is
    covered, so none of them takes a default (requirement 8.2).

    ``values`` is empty for a push tool, whose parameters are the source bill and the line
    inputs and nothing else (requirement 11.4); a seed build writes the header fields of a
    pushed record here, so they are part of the same operation as the push and the bill
    number is allocated from the business date the build gives it (requirement 20.2). A
    caller's value wins over the mapping, as it does on a line (:func:`push_line`); the
    three fields the push writes itself win over both.
    """
    merged: dict[str, Value] = {
        name: header.get(field) for name, field in conversion.header_mapping.items()
    }
    merged.update(values)
    merged[SOURCE_BILL_TYPE] = conversion.source
    merged[SOURCE_BILL_ID] = source_id
    merged[CONVERSION_COLUMN] = conversion.id
    return merged


def push_line(conversion: ConversionModel, line: PushLine) -> dict[str, Value]:
    """The values of one pushed line before its defaults (requirements 8.1 and 8.2).

    The fields of the ``line_mapping``, each taking the value of the source line field it
    names, then the ``required_line_inputs`` the caller sent - a field that is both takes
    the caller's value - and the ``source_line_id`` of the line. A field the caller left out
    is present and empty, so it is covered and takes no default (requirement 8.2).

    Raises KeyError for an argument that names none of the ``required_line_inputs``: the
    lines of a push tool offer a parameter for those and for nothing else
    (requirement 11.4), so such a value would otherwise be dropped without a word.
    """
    unknown = sorted(set(line.args) - set(conversion.required_line_inputs) - {SOURCE_LINE_ID})
    if unknown:
        raise KeyError(f"the push {conversion.id} has no line input named {', '.join(unknown)}")
    source = line.record
    values: dict[str, Value] = {
        name: None if source is None else source.get(field)
        for name, field in conversion.line_mapping.items()
    }
    for name in conversion.required_line_inputs:
        values[name] = line.args.get(name)
    values[SOURCE_LINE_ID] = line.source_line_id
    return values


def build_push_draft(
    uow: UnitOfWork,
    domain: DomainModel,
    conversion: ConversionModel,
    source_id: str,
    header: Record,
    lines: Sequence[PushLine],
    *,
    values: Arguments = _NO_VALUES,
    anchor_date: date,
    current_user_id: str | None = None,
) -> Candidate:
    """Step 5 of a push: the target bill draft to store (requirements 8.1 and 8.2).

    The mapping and the caller's inputs of each level (:func:`push_header`,
    :func:`push_line`), then the defaults of the fields they do not cover and everything the
    engine writes itself (:func:`~erp_mock.domain.crud.build_push_candidate`). The lines of
    the draft answer to the lines of the call one by one and in the same order
    (requirement 8.1). ``values`` are header values of the caller, which only a seed build
    sends (:func:`push_header`).
    """
    return build_push_candidate(
        uow,
        domain,
        target_bill(domain, conversion),
        FrozenMap(push_header(conversion, source_id, header, values)),
        [FrozenMap(push_line(conversion, line)) for line in lines],
        anchor_date=anchor_date,
        current_user_id=current_user_id,
    )


# --- step 7: R-GEN-005, the quota of the source (requirements 8.5 to 8.7) ----------------


def occupied_qty(
    uow: UnitOfWork, conversion: ConversionModel, target: BillModel, source_line_id: str
) -> Decimal:
    """The base unit quantity the bills of ``conversion`` already take from one source line.

    Every line of every bill this conversion generated that names the source line and the
    sandbox still holds, a draft among them (requirement 8.5); a line of a bill another
    conversion generated, or of one a caller created itself, is not counted.
    """
    total = _ZERO
    for line in uow.select(target.line_name, where={SOURCE_LINE_ID: source_line_id}):
        if _generated_by(uow, conversion, target, _text(line.get(BILL_ID_COLUMN))):
            total = _qty(WIDE.add(total, _decimal(line.get(QTY_BASE))))
    return total


def pushable_qty(
    uow: UnitOfWork, conversion: ConversionModel, target: BillModel, source_line: Record
) -> Decimal:
    """What is left of one source line, in its base unit (requirement 8.5, glossary).

    Its own ``qty_base`` minus :func:`occupied_qty`; a source line that measures nothing
    counts as 0.
    """
    own = _decimal(source_line.get(QTY_BASE))
    taken = occupied_qty(uow, conversion, target, _id_of(source_line))
    return _qty(WIDE.subtract(own, taken))


def occupied_amount(
    uow: UnitOfWork, conversion: ConversionModel, target: BillModel, source_id: str
) -> Decimal:
    """The amount the bills of ``conversion`` already take from one source bill (req 8.6).

    The ``amount_with_tax`` of every bill this conversion generated from that source and
    the sandbox still holds, a draft among them.
    """
    stored = uow.select(
        target.name, where={SOURCE_BILL_ID: source_id, CONVERSION_COLUMN: conversion.id}
    )
    total = _ZERO
    for header in stored:
        total = _money(WIDE.add(total, _decimal(header.get(_AMOUNT_WITH_TAX))))
    return total


def pushable_amount(
    uow: UnitOfWork, conversion: ConversionModel, target: BillModel, header: Record
) -> Decimal:
    """What is left of one source bill, as an amount (requirement 8.6, glossary).

    Its ``amount_with_tax`` minus :func:`occupied_amount`; a source bill without pricing
    counts as 0.
    """
    own = _decimal(header.get(_AMOUNT_WITH_TAX))
    taken = occupied_amount(uow, conversion, target, _id_of(header))
    return _money(WIDE.subtract(own, taken))


def line_quotas(
    uow: UnitOfWork,
    conversion: ConversionModel,
    source: BillModel,
    target: BillModel,
    lines: Sequence[Mapping[str, Value]],
) -> tuple[LineQuota, ...]:
    """One quota per source line the ``lines`` of a push name (requirements 8.5 and 8.7).

    ``lines`` are the lines of the candidate, each carrying its ``source_line_id`` and the
    ``qty_base`` the unit conversion computed for it. Lines that name the same source line
    are one quota, whose ``pushed`` is the sum of their quantities and whose ``path`` names
    the first of them, and the quotas are ordered by that first line (requirement 8.5).
    """
    quantity = target.qty_field if target.qty_field is not None else QTY_BASE
    pushed: dict[str, Decimal] = {}
    paths: dict[str, str] = {}
    for index, line in enumerate(lines):
        source_line_id = _text(line.get(SOURCE_LINE_ID))
        pushed[source_line_id] = _qty(
            WIDE.add(pushed.get(source_line_id, _ZERO), _decimal(line.get(QTY_BASE)))
        )
        paths.setdefault(source_line_id, line_path(index, quantity))
    return tuple(
        LineQuota(
            source_line_id=source_line_id,
            path=paths[source_line_id],
            pushed=total,
            pushable=_line_pushable(uow, conversion, source, target, source_line_id),
        )
        for source_line_id, total in pushed.items()
    )


def check_push_quota(
    uow: UnitOfWork,
    domain: DomainModel,
    conversion: ConversionModel,
    header: Record,
    candidate: Candidate,
) -> None:
    """Step 7 of a push: R-GEN-005 (requirements 8.5, 8.6, 8.7 and 10.8).

    ``header`` is the source bill and ``candidate`` the draft the ``candidate`` slot built,
    whose base unit quantities and amounts the check reads. Raises
    BUSINESS_RULE_VIOLATION with the rule id ``R-GEN-005``: for a conversion limited by qty
    one detail item per source line the push takes too much of, in the order those lines
    appear in the call, each carrying the quantity field path of its first line, the source
    line and what was left; for one limited by amount a single item carrying the amount that
    was left, which names no input field (design.md data model 6).
    """
    source = source_bill(domain, conversion)
    target = target_bill(domain, conversion)
    if conversion.limit_by == "amount":
        _check_amount_quota(uow, conversion, source, target, header, candidate)
        return
    exceeded = tuple(
        quota
        for quota in line_quotas(uow, conversion, source, target, candidate.lines)
        if quota.exceeded
    )
    if not exceeded:
        return
    first = exceeded[0]
    message = (
        f"{first.path} 的数量超过{_label(source)}分录 {first.source_line_id} 的可下推数量："
        f"本次合计 {_qty_text(first.pushed)}，可下推 {_qty_text(first.pushable)}"
    )
    raise ErpError(
        ErrorCode.BUSINESS_RULE_VIOLATION,
        _with_rest(message, len(exceeded), "个源行同样超额"),
        rule_id=QUOTA_RULE,
        details=tuple(_qty_detail(quota) for quota in exceeded),
    )


def _check_amount_quota(
    uow: UnitOfWork,
    conversion: ConversionModel,
    source: BillModel,
    target: BillModel,
    header: Record,
    candidate: Candidate,
) -> None:
    """R-GEN-005 for a conversion limited by amount (requirement 8.6).

    The ``amount_with_tax`` of the draft against what the source bill has left; an exact
    fit is allowed.
    """
    pushed = _decimal(candidate.values.get(_AMOUNT_WITH_TAX))
    pushable = pushable_amount(uow, conversion, target, header)
    if pushed <= pushable:
        return
    raise ErpError(
        ErrorCode.BUSINESS_RULE_VIOLATION,
        f"本次下推金额 {_money_text(pushed)} 超过{_label(source)} {_id_of(header)}"
        f" 的可下推金额 {_money_text(pushable)}",
        rule_id=QUOTA_RULE,
        details=(
            FrozenMap(
                (
                    (_REASON_KEY, EXCEEDS_AMOUNT_REASON),
                    (_PUSHABLE_AMOUNT_KEY, _money_text(pushable)),
                )
            ),
        ),
    )


# --- internals ---------------------------------------------------------------------------


def _line_pushable(
    uow: UnitOfWork,
    conversion: ConversionModel,
    source: BillModel,
    target: BillModel,
    source_line_id: str,
) -> Decimal:
    """What the source line ``source_line_id`` has left, 0 when it is not there (8.5).

    A line that is not there was reported by the ``references`` slot, so this only keeps the
    quota defined for every line the check sees.
    """
    record = uow.get(source.line_name, source_line_id)
    return _ZERO if record is None else pushable_qty(uow, conversion, target, record)


def _generated_by(
    uow: UnitOfWork, conversion: ConversionModel, target: BillModel, bill_id: str
) -> bool:
    """Whether the bill ``bill_id`` is one ``conversion`` generated (design.md 4.2)."""
    header = uow.get(target.name, bill_id)
    return header is not None and header.get(CONVERSION_COLUMN) == conversion.id


def _require_bill(domain: DomainModel, conversion: ConversionModel, name: str) -> BillModel:
    """The bill ``name``; an unresolved one is a programming error (requirement 2.9)."""
    bill = domain.bills.get(name)
    if bill is None:
        raise RuntimeError(f"the source or target {name} of {conversion.id} is no bill")
    return bill


def _line_paths(index: int) -> Callable[[str], str]:
    """The path builder of the line at ``index`` (requirement 5.6)."""
    return lambda name: line_path(index, name)


def _qty_detail(quota: LineQuota) -> FrozenMap[str, str]:
    """One detail item of R-GEN-005 by qty, in key order (design.md data model 6)."""
    items: tuple[tuple[str, str], ...] = (
        (_FIELD_KEY, quota.path),
        (_REASON_KEY, EXCEEDS_QTY_REASON),
        (SOURCE_LINE_ID, quota.source_line_id),
        (_PUSHABLE_QTY_KEY, _qty_text(quota.pushable)),
    )
    return FrozenMap(items)


def _details(paths: Iterable[str], reason: str) -> tuple[Mapping[str, str], ...]:
    """One detail item per field path, the reason after it (design.md data model 6)."""
    return tuple({_FIELD_KEY: path, _REASON_KEY: reason} for path in paths)


def _label(bill: BillModel) -> str:
    """The Chinese label of ``bill``, empty where it has none (requirement 13.7)."""
    return "" if bill.label is None else bill.label.zh


def _with_rest(message: str, total: int, rest: str) -> str:
    """``message``, saying how many further items of the same kind there are."""
    return message if total < 2 else f"{message}；另有 {total - 1} {rest}"


def _qty(value: Decimal) -> Decimal:
    """``value`` at the precision of a quantity, never -0 (requirements 4.7 and 4.8)."""
    return round_half_up(value, _QTY_PLACES)


def _money(value: Decimal) -> Decimal:
    """``value`` at the precision of an amount, never -0 (requirements 4.7 and 4.8)."""
    return round_half_up(value, _MONEY_PLACES)


def _qty_text(value: Decimal) -> str:
    """A quantity as a detail item and a message write it (requirements 4.8 and 13.4)."""
    return format_decimal("qty", value)


def _money_text(value: Decimal) -> str:
    """An amount as a detail item and a message write it (requirements 4.8 and 13.4)."""
    return format_decimal("money", value)


def _decimal(value: Value) -> Decimal:
    """``value`` as the decimal it holds, 0 when the field is empty (requirement 6.10)."""
    return value if isinstance(value, Decimal) else _ZERO


def _text(value: Value) -> str:
    """``value`` as the text it is, the empty string for anything else."""
    return value if isinstance(value, str) else ""


def _id_of(record: Mapping[str, Value]) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""
