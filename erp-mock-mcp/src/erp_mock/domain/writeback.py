"""The writeback primitive: what the approve of a pushed bill adds to its source bill.

A bill a push generated carries the conversion that generated it and the source bill it
came from (:mod:`erp_mock.domain.conversion`). When such a bill is approved, the writebacks
of that conversion add a value to the link field of the source - of each source line for a
``source_line.<field>`` target, of the source header for a ``source.<field>`` one - and the
source bill is closed where the writeback says so; the unapprove of the same bill takes
exactly those amounts off again and reopens a source bill the engine closed itself
(requirements 8.8, 8.9, 8.13 and 8.14). What this module contributes to the slots of the
pipeline (design.md 4.1):

=====================  ==================================================================
slot                   what this module contributes
=====================  ==================================================================
``rules``              :func:`check_no_downstream`: R-GEN-003, an approved bill a push has
                       generated a bill from is not unapproved (requirement 8.10)
``write``              :func:`run_writebacks`: the amounts of an approve and the automatic
                       close of its source bill, which run last (requirements 7.6, 8.8 and
                       8.13); :func:`reverse_writebacks`: the same amounts taken off again
                       and the automatic unclose of the source, which run first on an
                       unapprove (requirements 7.7, 8.9 and 8.14)
=====================  ==================================================================

Both writes run in the transaction of the operation, so an error of a later step - the
stock an unapprove moves back, for one - rolls back the state of the bill, the link fields
of its source and the state of that source together (requirement 7.10).

Which writebacks run (requirements 7.6 and 8.8). Only a bill a push generated writes back:
the internal column ``_conversion`` of its header names the conversion, and the writebacks
of that conversion whose ``on`` is approve run in model order (:func:`source_link`). A bill
of the same type a caller created itself carries no conversion and writes nothing back.

What one writeback adds (requirement 8.8). The ``value`` of the writeback is a rule
expression evaluated on the bill that was just approved: a ``source_line.<field>`` target
is evaluated once per target line, with ``line.`` the current line, and each result is added
to the link field of the source line that line was pushed from; a ``source.<field>`` target
is evaluated once on the header and added to the link field of the source bill. ``op`` is
``add``, the one operation of M0. The amount is rounded to the precision of the link field
it goes into before it is added (requirement 4.7), and a value that is null counts as 0
(requirement 10.6).

The ledger (``_writeback_ledger``, design.md 4.2). Every amount a writeback added is kept
as one row: which bill's approve added it, which writeback produced it, the position of the
amount within that writeback, and the record, the field and the amount itself. An unapprove
reads those rows and takes the amounts off in the reverse order, so what it removes is
exactly what the approve added whatever the ``value`` expression would evaluate to now -
INV-2 does not depend on a value that changed in between. A link field no row of any bill
contributes to any more goes back to empty, the way a (material, warehouse) with no stock
transaction left loses its balance record (requirement 9.10), so the approve and the
unapprove of one bill leave the source exactly as it was.

The automatic close (requirements 8.13 and 8.14). A writeback that declares
``auto_close_source_when`` closes its source bill after the amounts are written, where the
source is approved and the condition holds; ``all_lines_linked`` is every source line
holding a link field of at least its own ``qty_base`` (:func:`all_lines_linked`), and for a
writeback that keeps the header's link field it is the amount of the source bill being
linked in full, which is the quota a push limited by amount is counted against
(requirement 8.6). The close is recorded as an automatic one
(:func:`~erp_mock.domain.state_machine.write_status`), so the unclose operation refuses it
(requirement 7.9) and the unapprove of the downstream bill reopens it where the condition
no longer holds; a source bill the close operation closed stays closed, whatever the
amounts say (requirement 8.14).

R-GEN-003 (requirement 8.10). An approved bill a push has generated a bill from is not
unapproved: a bill of any state, a draft included, that names it in ``source_bill_id`` ends
the call in BUSINESS_RULE_VIOLATION with that rule id, checked in the ``rules`` slot between
R-GEN-002 and R-GEN-005 (requirement 10.8). The downstream bill has to be deleted or
unapproved first, and that is what keeps the ledger of this module meaningful: while a
downstream bill exists, the source it wrote back to cannot leave approved.

Attribution (requirement 17.4). Every write of this module names the writeback that made
it (:meth:`~erp_mock.state.store.UnitOfWork.cause`), the automatic close and unclose of a
source bill included; the ``updated_seq`` of a source bill a writeback changed is written by
the ``view`` slot, which this module records the bill as changed for
(:class:`~erp_mock.domain.numbering.SequenceStamps`, requirement 4.5).
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from erp_mock.common.decimals import PLACES, WIDE, DecimalKind, format_decimal, round_half_up
from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import (
    BillModel,
    ConversionModel,
    DomainModel,
    FieldModel,
    WritebackModel,
)
from erp_mock.domain.conversion import (
    SOURCE_BILL_ID,
    SOURCE_LINE_ID,
    source_bill,
    target_bill,
)
from erp_mock.domain.crud import own_fields
from erp_mock.domain.numbering import SequenceStamps
from erp_mock.domain.pricing import AMOUNT_FIELDS
from erp_mock.domain.query import QueryTables, RecordEnv, StoredRecord, user_record
from erp_mock.domain.rule_expr.evaluator import Value as ExprValue
from erp_mock.domain.rule_expr.evaluator import eval_expr
from erp_mock.domain.rule_expr.nodes import Expr
from erp_mock.domain.rule_expr.parser import parse_expr
from erp_mock.domain.state_machine import (
    APPROVED,
    AUTO_CLOSE,
    CLOSED,
    StateAction,
    bill_status,
    close_mode,
    write_status,
)
from erp_mock.domain.unit_conversion import QTY_BASE
from erp_mock.domain.views import bill_lines
from erp_mock.state.schema import (
    CONVERSION_COLUMN,
    ID_COLUMN,
    WRITEBACK_AMOUNT_COLUMN,
    WRITEBACK_COLUMN,
    WRITEBACK_FIELD_COLUMN,
    WRITEBACK_LEDGER_TABLE,
    WRITEBACK_ORD_COLUMN,
    WRITEBACK_SOURCE_ID_COLUMN,
    WRITEBACK_SOURCE_TABLE_COLUMN,
    WRITEBACK_TARGET_COLUMN,
)
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "ALL_LINES_LINKED",
    "APPROVE",
    "FIRST_ORD",
    "HAS_DOWNSTREAM_REASON",
    "PUSHED_RULE",
    "UNAPPROVE",
    "Downstream",
    "LedgerEntry",
    "SourceLink",
    "all_lines_linked",
    "apply_entries",
    "check_no_downstream",
    "close_source",
    "downstream_bills",
    "ledger_entries",
    "link_kind",
    "reverse_writebacks",
    "run_writebacks",
    "source_link",
    "unclose_source",
    "writeback_entries",
    "writeback_value",
    "writeback_values",
    "writebacks_of",
]

# The engine rule this module decides: an approved bill a push has generated a bill from
# stays approved (requirement 8.10).
PUSHED_RULE: Final = "R-GEN-003"

# The reason of design.md data model 6 this module reports; its item names no input field
# and carries the type and the id of the downstream bill that keeps the source approved.
HAS_DOWNSTREAM_REASON: Final = "has_downstream_bill"

# The two operations of a writeback (meta-model.md section 7): the one that writes the
# amounts and the one that takes them off again.
APPROVE: Final[StateAction] = "approve"
UNAPPROVE: Final[StateAction] = "unapprove"

# The one condition ``auto_close_source_when`` takes (requirement 8.13).
ALL_LINES_LINKED: Final = "all_lines_linked"

# The position of the first amount of one writeback in the ledger (design.md data model 3);
# a per-line writeback numbers its amounts by the line number of the target line, which
# starts at the same place (requirement 4.2).
FIRST_ORD: Final = 1

# The header field the link field of a push limited by amount is linked against
# (requirement 8.6): the amount of the source bill, as the quota of that push is.
_AMOUNT_WITH_TAX: Final = AMOUNT_FIELDS[2]

# The keys of a detail item (design.md data model 6): the reason, then the type and the id
# of the downstream bill.
_REASON_KEY: Final = "reason"
_BILL_TYPE_KEY: Final = "bill_type"
_BILL_ID_KEY: Final = "bill_id"

# The standard field of a bill header a message names the bill by (meta-model.md 4).
_NUMBER: Final = "number"

_ZERO: Final = Decimal(0)


@dataclass(frozen=True, slots=True)
class SourceLink:
    """Where one pushed bill came from, with the writebacks its approve runs (req 8.8).

    ``conversion`` is the conversion the internal column of the bill names, ``source`` the
    bill it reads and ``source_id`` the source bill itself; ``writebacks`` are the
    writebacks of that conversion whose ``on`` is approve, in model order, which may be
    empty for a conversion that declares none.
    """

    conversion: ConversionModel
    source: BillModel
    source_id: str
    writebacks: tuple[WritebackModel, ...]


@dataclass(frozen=True, slots=True)
class Downstream:
    """One bill a push generated from a source bill (requirement 8.10).

    ``conversion`` is the conversion that generated it, ``bill`` its type and ``id`` the
    bill itself; any state counts, a draft included.
    """

    conversion: str
    bill: BillModel
    id: str


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One amount a writeback added to one field of one source record (design.md 4.2).

    ``target_bill_id`` is the bill whose approve added it, ``writeback`` the writeback that
    produced it and ``ord`` the position of the amount within that writeback: the line
    number of the target line it came from, or :data:`FIRST_ORD` for a writeback that adds
    one amount to the source header. ``source_table``, ``source_id`` and ``field`` are the
    record and the link field it went into, ``amount`` the amount itself - already rounded
    to the precision of that field - and ``kind`` the field type it is kept at, so the
    ledger writes the canonical text of the amount (requirements 4.7 and 4.8).
    """

    target_bill_id: str
    writeback: str
    ord: int
    source_table: str
    source_id: str
    field: str
    amount: Decimal
    kind: DecimalKind

    @property
    def place(self) -> tuple[str, str, str]:
        """The link field the amount went into: its table, its record and its name."""
        return (self.source_table, self.source_id, self.field)

    @property
    def row(self) -> FrozenMap[str, Value]:
        """The entry as the ledger holds it (design.md data model 3)."""
        items: tuple[tuple[str, Value], ...] = (
            (WRITEBACK_TARGET_COLUMN, self.target_bill_id),
            (WRITEBACK_COLUMN, self.writeback),
            (WRITEBACK_ORD_COLUMN, self.ord),
            (WRITEBACK_SOURCE_TABLE_COLUMN, self.source_table),
            (WRITEBACK_SOURCE_ID_COLUMN, self.source_id),
            (WRITEBACK_FIELD_COLUMN, self.field),
            (WRITEBACK_AMOUNT_COLUMN, format_decimal(self.kind, self.amount)),
        )
        return FrozenMap(items)


# --- what a pushed bill writes back (requirements 7.6 and 8.8) ---------------------------


def writebacks_of(domain: DomainModel, conversion: str) -> tuple[WritebackModel, ...]:
    """The writebacks of the conversion ``conversion``, in model order (requirement 8.8).

    Every writeback of that conversion whose ``on`` is approve, which in M0 is every
    writeback there is; empty for a conversion whose packs declare none, which leaves its
    source bill untouched when a pushed bill is approved.
    """
    return tuple(
        writeback
        for writeback in domain.writebacks.values()
        if writeback.conversion == conversion and writeback.on == APPROVE
    )


def source_link(domain: DomainModel, header: Record) -> SourceLink | None:
    """Where the bill ``header`` was pushed from, None for a bill no push generated (8.8).

    The internal column ``_conversion`` of the header names the conversion and
    ``source_bill_id`` the source bill (:mod:`erp_mock.domain.conversion`); None where the
    bill carries neither, which is every bill a caller created itself, and where the
    instance no longer declares that conversion.
    """
    name = _text(header.get(CONVERSION_COLUMN))
    source_id = _text(header.get(SOURCE_BILL_ID))
    conversion = domain.conversions.get(name)
    if conversion is None or not source_id:
        return None
    return SourceLink(
        conversion=conversion,
        source=source_bill(domain, conversion),
        source_id=source_id,
        writebacks=writebacks_of(domain, conversion.id),
    )


def writeback_value(writeback: WritebackModel) -> Expr:
    """The syntax tree of the ``value`` of ``writeback`` (requirements 8.8 and 10.2).

    Raises :class:`~erp_mock.domain.rule_expr.errors.ExprError` for a text that does not
    parse, which loading step 5 reported and refused to start on.
    """
    return parse_expr(writeback.value)


def writeback_values(domain: DomainModel) -> FrozenMap[str, Expr]:
    """The syntax tree of the ``value`` of every writeback of ``domain``, by its id.

    Built once per loaded instance, so no call parses a value text again; parsing is a pure
    function of the text, so these are the trees loading step 5 checked (requirement 10.2),
    and a caller that holds them - the loader's ``ParsedDomain`` - hands them to the engine
    instead (:class:`~erp_mock.domain.engine.Engine`).
    """
    return FrozenMap(
        (writeback.id, writeback_value(writeback)) for writeback in domain.writebacks.values()
    )


def link_kind(domain: DomainModel, table: str, field: str) -> DecimalKind:
    """The field type the link field ``field`` of ``table`` is kept at (requirement 4.7).

    The link field of a push is a qty field on a source line and a money field on a source
    header (meta-model.md section 7), and the precision of the amounts of a writeback is
    the precision of that field.

    Raises RuntimeError when the instance has no such field or the field holds no number,
    both of which loading step 5 reports (requirements 2.9 and 10.2).
    """
    for bill in domain.bills.values():
        if table == bill.name:
            declared = bill.header.get(field)
        elif table == bill.line_name:
            declared = bill.lines.get(field)
        else:
            continue
        if declared is None:
            break
        match declared.type:
            case "qty" | "price" | "money" | "rate":
                return declared.type
            case _:
                raise RuntimeError(
                    f"the link field {table}.{field} is a {declared.type} field and holds no amount"
                )
    raise RuntimeError(f"the instance has no field {table}.{field} for a writeback to keep")


def writeback_entries(
    uow: UnitOfWork,
    domain: DomainModel,
    writeback: WritebackModel,
    link: SourceLink,
    target: BillModel,
    header: Record,
    *,
    tree: Expr | None = None,
    anchor_date: date,
    current_user_id: str | None = None,
) -> tuple[LedgerEntry, ...]:
    """The amounts ``writeback`` adds to the source of the bill ``header`` (req 8.8).

    ``target`` is the bill type that was approved and ``header`` its header; ``tree`` is the
    syntax tree of the writeback's ``value``, which the caller holds (:func:`writeback_values`)
    and which is parsed here where it is None.

    A ``source_line.<field>`` target gives one entry per line of the approved bill, in line
    number order, each carrying the source line that line was pushed from; a
    ``source.<field>`` target gives one entry for the source header. Every amount is the
    value of the expression rounded to the precision of the link field, 0 where the value is
    null (requirement 10.6), and an entry is produced even where it is 0, so the unapprove
    takes off exactly what the approve added.
    """
    ref = writeback.target
    source = link.source
    table = source.line_name if ref.level == "line" else source.name
    kind = link_kind(domain, table, ref.name)
    expr = writeback_value(writeback) if tree is None else tree
    bill_id = _id_of(header)
    lines = bill_lines(uow, target, bill_id)
    env = _target_env(
        uow, domain, target, header, lines, anchor_date=anchor_date, current_user_id=current_user_id
    )

    def entry(ord_: int, source_id: str, amount: Decimal) -> LedgerEntry:
        return LedgerEntry(
            target_bill_id=bill_id,
            writeback=writeback.id,
            ord=ord_,
            source_table=table,
            source_id=source_id,
            field=ref.name,
            amount=amount,
            kind=kind,
        )

    if ref.level != "line":
        return (entry(FIRST_ORD, link.source_id, _amount(eval_expr(expr, env), kind)),)
    return tuple(
        entry(ord_, _text(line.get(SOURCE_LINE_ID)), _amount(eval_expr(expr, env, stored), kind))
        for ord_, (line, stored) in enumerate(
            zip(lines, env.line_records, strict=True), start=FIRST_ORD
        )
    )


def ledger_entries(uow: UnitOfWork, domain: DomainModel, bill_id: str) -> tuple[LedgerEntry, ...]:
    """The amounts the approve of the bill ``bill_id`` added, in ledger order (req 8.9).

    The rows of the ledger of that bill, by writeback id and then by position, so the
    amounts of one writeback come in the order it added them; empty for a bill that added
    none.
    """
    rows = uow.select(WRITEBACK_LEDGER_TABLE, where={WRITEBACK_TARGET_COLUMN: bill_id})
    return tuple(_stored_entry(domain, row) for row in rows)


def apply_entries(
    uow: UnitOfWork, entries: Sequence[LedgerEntry], *, reverse: bool = False
) -> None:
    """Add the amount of every entry to its link field, or take it off (req 8.8, 8.9).

    One read and one write per entry, in the order of ``entries``, so two entries that name
    the same source record add up; ``reverse`` is what an unapprove passes, which subtracts
    instead. The new value is rounded to the precision of the link field and an empty field
    counts as 0. A source record the sandbox no longer holds is skipped: its amounts went
    with it.
    """
    for entry in entries:
        record = uow.get(entry.source_table, entry.source_id)
        if record is None:
            continue
        delta = -entry.amount if reverse else entry.amount
        after = round_half_up(
            WIDE.add(_decimal(record.get(entry.field)), delta), PLACES[entry.kind]
        )
        with uow.cause(entry.writeback):
            uow.update(entry.source_table, entry.source_id, {entry.field: after})


# --- the automatic close and unclose of a source bill (requirements 8.13 and 8.14) -------


def all_lines_linked(
    uow: UnitOfWork, writeback: WritebackModel, source: BillModel, header: Record
) -> bool:
    """Whether the source bill ``header`` is linked in full (requirement 8.13).

    For a writeback that keeps the link field of the source lines: every line of the source
    bill holds a link field of at least its own ``qty_base``. For one that keeps the link
    field of the source header: the header holds a link field of at least its
    ``amount_with_tax``, the amount a push limited by amount is counted against
    (requirement 8.6). An empty link field counts as 0.
    """
    ref = writeback.target
    if ref.level != "line":
        return _decimal(header.get(ref.name)) >= _decimal(header.get(_AMOUNT_WITH_TAX))
    return all(
        _decimal(line.get(ref.name)) >= _decimal(line.get(QTY_BASE))
        for line in bill_lines(uow, source, _id_of(header))
    )


def close_source(uow: UnitOfWork, link: SourceLink, header: Record, stamps: SequenceStamps) -> bool:
    """Close the source bill where a writeback says so (requirement 8.13).

    Runs after the amounts of the approve are written, on the source bill as they left it.
    The source is closed where it is approved and the condition of a writeback that declares
    ``auto_close_source_when`` holds; the close is recorded as an automatic one, so the
    unclose operation refuses it (requirement 7.9) and the unapprove of this bill reopens it
    (:func:`unclose_source`). Returns whether the source was closed. A writeback that
    declares no condition closes nothing, and a source bill in any other state - closed by
    the close operation, for one - is left as it is.
    """
    if bill_status(header) != APPROVED:
        return False
    source = link.source
    for writeback in _auto_closing(link):
        if not all_lines_linked(uow, writeback, source, header):
            continue
        with uow.cause(writeback.id):
            write_status(uow, source, link.source_id, CLOSED, mode=AUTO_CLOSE)
        stamps.changed_bill(source.name, link.source_id)
        return True
    return False


def unclose_source(
    uow: UnitOfWork, link: SourceLink, header: Record, stamps: SequenceStamps
) -> bool:
    """Reopen a source bill the engine closed itself (requirement 8.14).

    Runs after the amounts of the unapprove are taken off, on the source bill as they left
    it: a source that is closed, was closed automatically and no longer meets the condition
    of any writeback that declares one goes back to approved. Returns whether it was
    reopened. A source bill the close operation closed stays closed, whatever the amounts
    say, and so does one that still meets the condition.
    """
    if bill_status(header) != CLOSED or close_mode(header) != AUTO_CLOSE:
        return False
    source = link.source
    closing = _auto_closing(link)
    if not closing or any(
        all_lines_linked(uow, writeback, source, header) for writeback in closing
    ):
        return False
    with uow.cause(closing[0].id):
        write_status(uow, source, link.source_id, APPROVED)
    stamps.changed_bill(source.name, link.source_id)
    return True


# --- R-GEN-003, in the rules slot (requirements 8.10 and 10.8) ---------------------------


def downstream_bills(
    uow: UnitOfWork, domain: DomainModel, bill: BillModel, bill_id: str
) -> tuple[Downstream, ...]:
    """Every bill a push generated from the bill ``bill_id`` (requirement 8.10).

    By conversion in model order and by bill id within each: every undeleted bill that
    names ``bill_id`` in its ``source_bill_id``, in any state and a draft among them.
    """
    return tuple(_downstream(uow, domain, bill, bill_id))


def check_no_downstream(
    uow: UnitOfWork, domain: DomainModel, bill: BillModel, header: Record, action: StateAction
) -> None:
    """Step 7 of an unapprove: R-GEN-003 (requirements 8.10 and 10.8).

    Only for the unapprove of a bill: a bill a push has generated a bill from keeps its
    approve, since the link field and the state of a source bill are what the downstream
    bill was built on. Raises BUSINESS_RULE_VIOLATION with the rule id ``R-GEN-003``, whose
    one detail item carries the type and the id of the first such bill and names no input
    field; the message says which bill has to go first.
    """
    if action != UNAPPROVE:
        return
    found = next(_downstream(uow, domain, bill, _id_of(header)), None)
    if found is None:
        return
    raise ErpError(
        ErrorCode.BUSINESS_RULE_VIOLATION,
        f"{_bill_text(bill, header)}已下推生成{_label(found.bill)} {found.id}，"
        "不能反审核，请先反审核或删除下游单据",
        rule_id=PUSHED_RULE,
        details=(
            FrozenMap(
                (
                    (_REASON_KEY, HAS_DOWNSTREAM_REASON),
                    (_BILL_TYPE_KEY, found.bill.name),
                    (_BILL_ID_KEY, found.id),
                )
            ),
        ),
    )


# --- the writes of an approve and of an unapprove (requirements 7.6, 7.7, 8.8 and 8.9) ---


def run_writebacks(
    uow: UnitOfWork,
    domain: DomainModel,
    bill: BillModel,
    header: Record,
    stamps: SequenceStamps,
    *,
    values: Mapping[str, Expr] | None = None,
    anchor_date: date,
    current_user_id: str | None = None,
) -> None:
    """Step 9 of an approve: the writebacks and the automatic close (req 7.6, 8.8, 8.13).

    Runs last, after the state of the bill and the linkages of its type: the amounts of
    every writeback of the conversion that generated this bill are computed on the bill as
    it now stands, added to the link fields of its source and kept in the ledger, and the
    source bill is closed where a writeback says so (:func:`close_source`). ``values`` are
    the syntax trees of the writeback values by writeback id, which the engine holds; the
    text of a writeback that is not among them is parsed here.

    A bill no push generated writes nothing, and nothing is recorded on ``stamps``: a bill
    a caller created itself has no source bill (:func:`source_link`).
    """
    link = source_link(domain, header)
    if link is None:
        return
    stored = _stored_header(uow, bill, header)
    trees: Mapping[str, Expr] = FrozenMap() if values is None else values
    entries = tuple(
        entry
        for writeback in link.writebacks
        for entry in writeback_entries(
            uow,
            domain,
            writeback,
            link,
            bill,
            stored,
            tree=trees.get(writeback.id),
            anchor_date=anchor_date,
            current_user_id=current_user_id,
        )
    )
    apply_entries(uow, entries)
    uow.record_writebacks([entry.row for entry in entries])
    if entries:
        stamps.changed_bill(link.source.name, link.source_id)
    close_source(uow, link, _source_header(uow, link), stamps)


def reverse_writebacks(
    uow: UnitOfWork,
    domain: DomainModel,
    bill: BillModel,
    header: Record,
    stamps: SequenceStamps,
) -> None:
    """Step 9 of an unapprove: the writebacks reversed, the source reopened (7.7, 8.9, 8.14).

    Runs first, before the linkages of the bill type are reversed: the amounts the approve
    of this bill added are read from the ledger and taken off their link fields in the
    reverse order, the rows are removed, and a link field no row of any bill contributes to
    any more goes back to empty. Afterwards a source bill the engine closed itself is
    reopened where it no longer meets the condition (:func:`unclose_source`).

    A bill whose approve added nothing - one no push generated, or one whose conversion
    declares no writeback - changes nothing here.
    """
    link = source_link(domain, header)
    if link is None:
        return
    bill_id = _id_of(header)
    entries = ledger_entries(uow, domain, bill_id)
    apply_entries(uow, tuple(reversed(entries)), reverse=True)
    uow.clear_writebacks(bill_id)
    _empty_unlinked(uow, entries)
    if entries:
        stamps.changed_bill(link.source.name, link.source_id)
    unclose_source(uow, link, _source_header(uow, link), stamps)


# --- internals ---------------------------------------------------------------------------


def _downstream(
    uow: UnitOfWork, domain: DomainModel, bill: BillModel, bill_id: str
) -> Iterator[Downstream]:
    """The bills every conversion of ``bill`` generated from ``bill_id`` (req 8.10)."""
    for conversion in domain.conversions.values():
        if conversion.source != bill.name:
            continue
        target = target_bill(domain, conversion)
        for record in uow.select(target.name, where={SOURCE_BILL_ID: bill_id}):
            yield Downstream(conversion=conversion.id, bill=target, id=_id_of(record))


def _auto_closing(link: SourceLink) -> tuple[WritebackModel, ...]:
    """The writebacks of ``link`` that close the source bill (requirement 8.13)."""
    return tuple(
        writeback for writeback in link.writebacks if writeback.auto_close_source_when is not None
    )


def _empty_unlinked(uow: UnitOfWork, entries: Sequence[LedgerEntry]) -> None:
    """Empty every link field no ledger row contributes to any more (requirement 8.9).

    Read after the rows of this bill were removed: a link field another bill still wrote to
    keeps its value, a quantity of 0 included, and one nothing is linked to any more goes
    back to the empty value it had before the first push, the way a (material, warehouse)
    with no stock transaction left loses its balance record (requirement 9.10).
    """
    causes: dict[tuple[str, str, str], str] = {}
    for entry in entries:
        causes.setdefault(entry.place, entry.writeback)
    for (table, source_id, field), cause in causes.items():
        rows = uow.select(
            WRITEBACK_LEDGER_TABLE,
            where={
                WRITEBACK_SOURCE_TABLE_COLUMN: table,
                WRITEBACK_SOURCE_ID_COLUMN: source_id,
                WRITEBACK_FIELD_COLUMN: field,
            },
        )
        if rows or uow.get(table, source_id) is None:
            continue
        with uow.cause(cause):
            uow.update(table, source_id, {field: None})


def _stored_entry(domain: DomainModel, row: Record) -> LedgerEntry:
    """One row of the ledger as an entry (design.md data model 3).

    The amount is the canonical text of a value within the precision of its link field, so
    reading it back gives exactly the amount the approve added (requirement 4.9).
    """
    table = _text(row.get(WRITEBACK_SOURCE_TABLE_COLUMN))
    field = _text(row.get(WRITEBACK_FIELD_COLUMN))
    position = row.get(WRITEBACK_ORD_COLUMN)
    return LedgerEntry(
        target_bill_id=_text(row.get(WRITEBACK_TARGET_COLUMN)),
        writeback=_text(row.get(WRITEBACK_COLUMN)),
        ord=position if isinstance(position, int) else FIRST_ORD,
        source_table=table,
        source_id=_text(row.get(WRITEBACK_SOURCE_ID_COLUMN)),
        field=field,
        amount=Decimal(_text(row.get(WRITEBACK_AMOUNT_COLUMN)) or "0"),
        kind=link_kind(domain, table, field),
    )


def _target_env(
    uow: UnitOfWork,
    domain: DomainModel,
    target: BillModel,
    header: Record,
    lines: Sequence[Record],
    *,
    anchor_date: date,
    current_user_id: str | None = None,
) -> RecordEnv:
    """What the ``value`` of a writeback is evaluated on: the approved bill (req 8.8).

    ``header.<field>`` reads the header of the bill the push generated and ``line.<field>``
    the line the amount is computed for, which the caller passes to the evaluator;
    ``sum(lines.F)`` and ``count(lines)`` read its lines, the context paths the business
    context of the call, and an ``exists`` or a ``sum_of`` a whole table once
    (:class:`~erp_mock.domain.query.QueryTables`).
    """
    return RecordEnv(
        anchor_date=anchor_date,
        header_record=_stored(uow, domain, own_fields(target), header),
        tables=QueryTables(uow=uow, domain=domain),
        line_records=tuple(_stored(uow, domain, target.lines, line) for line in lines),
        user_record=user_record(uow, domain, current_user_id),
    )


def _stored(
    uow: UnitOfWork, domain: DomainModel, fields: Mapping[str, FieldModel], record: Record
) -> StoredRecord:
    """``record`` as an expression reads it, with the field view of its level."""
    return StoredRecord(uow=uow, domain=domain, fields=fields, record=record)


def _stored_header(uow: UnitOfWork, bill: BillModel, header: Record) -> Record:
    """The bill ``header`` as the sandbox holds it now (requirement 8.8).

    The writebacks run after the state of the bill was written, so the record the earlier
    slots read is one state behind: the value of a writeback reads the bill as the approve
    left it. A bill the sandbox no longer holds keeps the record that was read.
    """
    found = uow.get(bill.name, _id_of(header))
    return found if found is not None else header


def _source_header(uow: UnitOfWork, link: SourceLink) -> Record:
    """The source bill as the amounts of this operation left it (requirements 8.13, 8.14).

    An empty record for a source bill the sandbox no longer holds, which is in no state and
    is neither closed nor reopened.
    """
    found = uow.get(link.source.name, link.source_id)
    return found if found is not None else FrozenMap()


def _amount(value: ExprValue, kind: DecimalKind) -> Decimal:
    """The amount one evaluation of a ``value`` expression adds (requirements 8.8, 10.6).

    Rounded to the precision of the link field it goes into, so the ledger and the field
    hold the same number (requirement 4.7); a value that is null, which is what a null field
    or a division by zero gives, counts as 0.
    """
    number = value if isinstance(value, Decimal) else _ZERO
    return round_half_up(number, PLACES[kind])


def _decimal(value: Value) -> Decimal:
    """``value`` as the number it holds, 0 where the field is empty (requirement 6.10)."""
    return value if isinstance(value, Decimal) else _ZERO


def _text(value: Value) -> str:
    """``value`` as the text it is, the empty string for anything else."""
    return value if isinstance(value, str) else ""


def _id_of(record: Mapping[str, Value]) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""


def _label(bill: BillModel) -> str:
    """The Chinese label of ``bill``, empty where it has none (requirement 13.7)."""
    return "" if bill.label is None else bill.label.zh


def _bill_text(bill: BillModel, record: Record) -> str:
    """How a message names the bill: its label and its number, or its id (req 13.7)."""
    number = record.get(_NUMBER)
    name = number if isinstance(number, str) and number else _id_of(record)
    return f"{_label(bill)} {name} ".lstrip()
