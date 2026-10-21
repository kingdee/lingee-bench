"""The invariants of a sandbox: what every state of it satisfies, whatever got it there.

The engine rules decide one operation; an invariant is about the state itself and is
checked against the records alone, with no operation in sight (design.md 4.4). M0 keeps
four of the seven invariants of meta-model.md section 9, each the responsibility of one
primitive:

=========  ============================================================  ================
invariant  what it says                                                  primitive
=========  ============================================================  ================
``INV-1``  the quantity on hand of each (material, warehouse) is the     inventory
           sum of the ``qty_delta`` of its stock transactions, and is
           not below 0 (requirement 9.8)
``INV-2``  the link field of each source line - of the source header     writeback,
           for a push limited by amount - is the sum of what the         conversion
           approve of every downstream bill that is approved or closed
           added to it; and for a push limited by quantity, no source
           line is occupied beyond its own ``qty_base`` (req 8.11)
``INV-3``  the three amounts of a bill header are the sums of the        pricing
           amounts of its lines, and the total of a line is its net
           amount plus its tax (requirements 6.4 and 6.5)
``INV-6``  the internal ids, the line ids and the bill numbers of the    numbering
           sandbox are each pairwise distinct, and no id or number
           that was handed out will be handed out again (req 4.10)
=========  ============================================================  ================

:func:`check_invariants` checks the ones a caller asks for and returns what did not hold
rather than raising: one :class:`InvariantViolation` per place, naming the invariant, what
about it failed, the records involved and the two values (requirement 20.9). An empty list
is a sandbox in which all four hold. The callers are the seed builder, which refuses to
write a Snapshot whose records break one (requirement 20.9), loading step 8, which checks
every Snapshot it takes up (requirement 2.14), the pack test runner, which checks after
every step (requirement 21.9), and the property test of the random operation sequences
(property 17).

How each one is decided (design.md 4.4):

- ``INV-1``: the transactions are grouped by (material, warehouse) and summed, and the sum
  is compared with the quantity the balance record of that pair holds - 0 for a pair that
  has none, which is what the primitive reads as well
  (:func:`~erp_mock.domain.inventory.stock_balance`). Whether a pair that has transactions
  has a balance record at all is requirement 9.10 and not this invariant: a pair whose
  transactions cancel out satisfies INV-1 with or without a record.
- ``INV-2``: the writeback ledger is what the approve of a downstream bill added
  (:func:`~erp_mock.domain.writeback.ledger_entries`), so the sum the invariant asks for is
  the sum of the ledger rows of the downstream bills that are now approved or closed; the
  rows of a bill in any other state are left out, which is how a bill whose state was
  tampered with is caught - its amounts are still in the link field but no longer counted.
  Re-evaluating the ``value`` expression of a writeback instead would need the business
  context of a call, which a state has none of, and would answer a different question: the
  ledger is there so that the invariant does not depend on what a value expression would
  evaluate to now (design.md 4.2). The occupancy part reuses
  :func:`~erp_mock.domain.conversion.occupied_qty`, the same function the quota of a push
  is measured against, for every source line a downstream line names; a source line no
  downstream line names is occupied by 0.
- ``INV-3``: the lines are summed again, non-empty values only, and compared with the three
  header fields, which is requirement 6.4; the total of a line is checked on the lines whose
  three amounts are all there, which is requirement 6.5. A line with no amounts at all is
  what an incomplete line gets (requirement 6.10) and breaks neither.
- ``INV-6``: the ids, the line ids and the numbers are collected and compared for
  duplicates, and the sequence each id and number carries is compared with the counter it
  came from (:mod:`erp_mock.domain.numbering`): a counter only ever rises, so a sequence no
  higher than its counter is one that will not be handed out again - deleted records
  included, since nothing here reads the records to decide what to hand out next
  (requirement 4.12). An id or a number that is not of the form its counter produces is
  left out of that comparison: no allocation can produce it, so it collides with nothing.

What is not here: INV-4, INV-5 and INV-7, which belong to the primitives of M1
(requirements, scope), and the uniqueness of a ``code`` or of a field a pack declared
unique, which is a check of one call rather than an invariant of the state
(requirement 5.8).
"""

import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from erp_mock.common.decimals import PLACES, WIDE, DecimalKind, format_decimal, round_half_up
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import BillModel, ConversionModel, DomainModel
from erp_mock.domain.conversion import (
    SOURCE_LINE_ID,
    occupied_qty,
    source_bill,
    target_bill,
)
from erp_mock.domain.inventory import balance_id
from erp_mock.domain.numbering import format_id, format_number, number_key
from erp_mock.domain.pricing import AMOUNT_FIELDS
from erp_mock.domain.state_machine import APPROVED, CLOSED, bill_status
from erp_mock.domain.unit_conversion import QTY_BASE
from erp_mock.domain.views import bill_lines
from erp_mock.domain.writeback import ledger_entries, link_kind
from erp_mock.state.schema import (
    COUNTERS_TABLE,
    ID_COLUMN,
    MATERIAL_COLUMN,
    QTY_BASE_COLUMN,
    QTY_DELTA_COLUMN,
    STOCK_BALANCE_TABLE,
    STOCK_TXN_TABLE,
    WAREHOUSE_COLUMN,
    WRITEBACK_LEDGER_TABLE,
    WRITEBACK_TARGET_COLUMN,
)
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "BALANCE_MISMATCH",
    "DUPLICATE_ID",
    "DUPLICATE_LINE_ID",
    "DUPLICATE_NUMBER",
    "HEADER_AMOUNT_MISMATCH",
    "ID_ABOVE_COUNTER",
    "INV_1",
    "INV_2",
    "INV_3",
    "INV_6",
    "INVARIANTS",
    "LINE_AMOUNT_MISMATCH",
    "LINKED_STATUSES",
    "LINK_FIELD_MISMATCH",
    "NEGATIVE_BALANCE",
    "NUMBER_ABOVE_COUNTER",
    "OCCUPIED_ABOVE_QTY_BASE",
    "BillRecord",
    "InvariantId",
    "InvariantViolation",
    "RecordRef",
    "bills_by_id",
    "check_bill_amounts",
    "check_identifiers",
    "check_invariants",
    "check_link_fields",
    "check_stock_balances",
    "counter_values",
    "id_sequence",
    "linked_totals",
    "number_parts",
]

# The invariants of M0, in the order :func:`check_invariants` reports them (requirements,
# scope); the three of meta-model.md section 9 that M1 adds are not among them.
type InvariantId = Literal["INV-1", "INV-2", "INV-3", "INV-6"]

INV_1: Final[InvariantId] = "INV-1"
INV_2: Final[InvariantId] = "INV-2"
INV_3: Final[InvariantId] = "INV-3"
INV_6: Final[InvariantId] = "INV-6"

INVARIANTS: Final[tuple[InvariantId, ...]] = (INV_1, INV_2, INV_3, INV_6)

# What did not hold, one engine word per way an invariant can fail; the ``reason`` of a
# violation, in the spirit of a detail item of design.md data model 6.
BALANCE_MISMATCH: Final = "balance_not_sum_of_txn"
NEGATIVE_BALANCE: Final = "negative_balance"
LINK_FIELD_MISMATCH: Final = "link_field_not_sum_of_writebacks"
OCCUPIED_ABOVE_QTY_BASE: Final = "occupied_above_qty_base"
HEADER_AMOUNT_MISMATCH: Final = "header_not_sum_of_lines"
LINE_AMOUNT_MISMATCH: Final = "line_total_not_amount_plus_tax"
DUPLICATE_ID: Final = "duplicate_id"
DUPLICATE_LINE_ID: Final = "duplicate_line_id"
DUPLICATE_NUMBER: Final = "duplicate_number"
ID_ABOVE_COUNTER: Final = "id_above_counter"
NUMBER_ABOVE_COUNTER: Final = "number_above_counter"

# The states of a downstream bill whose amounts a link field holds (requirement 8.11): the
# approve added them and only an unapprove takes them off, so a closed bill still counts.
LINKED_STATUSES: Final[tuple[str, str]] = (APPROVED, CLOSED)

# The standard field a bill number is kept in (meta-model.md section 4).
_NUMBER: Final = "number"

# What stands between the prefix and the sequence of an internal id (requirement 4.1,
# :func:`~erp_mock.domain.numbering.format_id`).
_ID_SEPARATOR: Final = "_"

# The ``YYYYMMDD`` of a bill number, in code points (requirement 4.4).
_DATE_WIDTH: Final = 8

# ASCII digits alone: ``str.isdigit`` also accepts digits ``int`` does not read.
_DIGITS: Final = re.compile(r"[0-9]+")

# The precision of the two quantities and of an amount this module compares (req 4.7).
_QTY: Final[DecimalKind] = "qty"
_MONEY: Final[DecimalKind] = "money"

# One table as INV-6 reads it: its name and the records it holds, read once and used by
# both the duplicates and the sequences (:func:`check_identifiers`).
type _Scan = tuple[str, Sequence[Record]]

_ZERO: Final = Decimal(0)


@dataclass(frozen=True, slots=True)
class RecordRef:
    """One record an invariant is about: the table it is in and its id (req 20.9).

    A bill, a bill line, an entity record or a stock balance; the balance of a (material,
    warehouse) is named by the id its pair gives it, whether the sandbox holds the record
    or not (:func:`~erp_mock.domain.inventory.balance_id`).
    """

    table: str
    id: str

    @property
    def text(self) -> str:
        """How a message names the record: its table and its id."""
        return f"{self.table} {self.id}"


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    """One place an invariant does not hold (requirement 20.9).

    ``invariant`` is the number of the invariant and ``reason`` the engine word for what
    about it failed, one of the module's constants. ``records`` are the records involved,
    in report order: the two or more records that share an id for a duplicate, and the one
    record the values are about otherwise. ``field`` is the field of that record the two
    values belong to, None where the violation is about the record as a whole, and
    ``cause`` the conversion, writeback or linkage the invariant is kept by, None where it
    names none.

    ``expected`` is what the invariant asks the sandbox to hold and ``found`` what it
    holds, both as the canonical text of their field type (requirement 4.8); a sequence
    above its counter reports the counter as ``expected`` and the sequence as ``found``,
    and either may be None where the invariant names no such value.
    """

    invariant: InvariantId
    reason: str
    records: tuple[RecordRef, ...] = ()
    field: str | None = None
    cause: str | None = None
    expected: str | None = None
    found: str | None = None

    @property
    def place(self) -> str:
        """How a message names what the violation is about: the records and the field."""
        where = "、".join(ref.text for ref in self.records) or "沙箱"
        return where if self.field is None else f"{where} 的 {self.field}"

    @property
    def text(self) -> str:
        """One line: the invariant, what did not hold, the records and the two values.

        What the seed builder puts in its error and what a failing property test reports
        (requirements 20.9 and 21.9).
        """
        said = [f"{self.invariant} 不成立（{self.reason}）：{self.place}"]
        if self.cause is not None:
            said.append(f"由 {self.cause} 维护")
        if self.expected is not None:
            said.append(f"应为 {self.expected}")
        if self.found is not None:
            said.append(f"实际为 {self.found}")
        return "，".join(said)


@dataclass(frozen=True, slots=True)
class BillRecord:
    """One bill of the sandbox: its type and its header as storage holds it.

    What :func:`bills_by_id` answers with, so that a ledger row naming a bill finds the
    state of that bill without a scan of its own.
    """

    bill: BillModel
    header: Record


# --- the entry point (design.md 4.4) -----------------------------------------------------


def check_invariants(
    uow: UnitOfWork, domain: DomainModel, which: Collection[InvariantId] = INVARIANTS
) -> list[InvariantViolation]:
    """Every place one of ``which`` does not hold, in invariant order (requirement 20.9).

    ``which`` is the invariants to check, all four of M0 by default; they are reported in
    the order of :data:`INVARIANTS` whatever order ``which`` names them in, and the
    violations of one invariant come in the order of the records they are about, so what a
    caller reports does not depend on the iteration order of anything. An empty list is a
    sandbox in which every invariant of ``which`` holds.

    Reads the sandbox and writes nothing; a caller runs it in the transaction of a write it
    is about to commit (the seed builder, the pack test runner) or in one of its own.
    """
    return [
        violation
        for invariant in INVARIANTS
        if invariant in which
        for violation in _CHECKS[invariant](uow, domain)
    ]


# --- INV-1: the stock on hand of each (material, warehouse) (requirement 9.8) ------------


def check_stock_balances(uow: UnitOfWork, domain: DomainModel) -> tuple[InvariantViolation, ...]:
    """INV-1: every balance is the sum of its transactions and is not below 0 (req 9.8).

    The transactions are grouped by the (material, warehouse) of their own columns and
    summed; a pair the sandbox holds no balance record for counts as 0, the way the
    primitive reads it (:func:`~erp_mock.domain.inventory.stock_balance`). One violation
    per pair whose balance is not that sum and one per pair whose balance is below 0, by
    material id and then warehouse id. ``domain`` is not read: both tables and all their
    columns are the engine's own (design.md data model 3).
    """
    moved = _stock_totals(uow, STOCK_TXN_TABLE, QTY_DELTA_COLUMN)
    on_hand = _stock_totals(uow, STOCK_BALANCE_TABLE, QTY_BASE_COLUMN)
    found: list[InvariantViolation] = []
    for place in sorted(set(moved) | set(on_hand)):
        balance = on_hand.get(place, _ZERO)
        total = moved.get(place, _ZERO)
        ref = RecordRef(STOCK_BALANCE_TABLE, balance_id(*place))
        if balance != total:
            found.append(
                InvariantViolation(
                    INV_1,
                    BALANCE_MISMATCH,
                    (ref,),
                    field=QTY_BASE_COLUMN,
                    expected=_qty_text(total),
                    found=_qty_text(balance),
                )
            )
        if balance < _ZERO:
            found.append(
                InvariantViolation(
                    INV_1,
                    NEGATIVE_BALANCE,
                    (ref,),
                    field=QTY_BASE_COLUMN,
                    found=_qty_text(balance),
                )
            )
    return tuple(found)


# --- INV-2: the link field and the occupancy of a source (requirement 8.11) --------------


def bills_by_id(uow: UnitOfWork, domain: DomainModel) -> FrozenMap[str, BillRecord]:
    """Every bill of the sandbox by its id, in model order of the types and then by id.

    An id two bills share keeps the first of them, which INV-6 reports as a duplicate
    (:func:`check_identifiers`).
    """
    found: dict[str, BillRecord] = {}
    for bill in domain.bills.values():
        for header in uow.select(bill.name):
            found.setdefault(_id_of(header), BillRecord(bill, header))
    return FrozenMap(found)


def linked_totals(uow: UnitOfWork, domain: DomainModel) -> FrozenMap[tuple[str, str, str], Decimal]:
    """What every link field should hold, by (table, record, field) (requirement 8.11).

    The sum of the amounts the writeback ledger holds for each place
    (:func:`~erp_mock.domain.writeback.ledger_entries`), counting the rows of the
    downstream bills that are now approved or closed alone: those are the bills whose
    approve added an amount and whose unapprove has not taken it off again. A place no row
    counts for is absent, which is a link field that should be empty or 0.

    Raises RuntimeError for a ledger row that names a field no bill of the instance has,
    which no write of the engine produces
    (:func:`~erp_mock.domain.writeback.link_kind`).
    """
    headers = bills_by_id(uow, domain)
    totals: dict[tuple[str, str, str], Decimal] = {}
    for target_id in _ledger_targets(uow):
        found = headers.get(target_id)
        if found is None or bill_status(found.header) not in LINKED_STATUSES:
            continue
        for entry in ledger_entries(uow, domain, target_id):
            total = WIDE.add(totals.get(entry.place, _ZERO), entry.amount)
            totals[entry.place] = round_half_up(total, PLACES[entry.kind])
    return FrozenMap(totals)


def check_link_fields(uow: UnitOfWork, domain: DomainModel) -> tuple[InvariantViolation, ...]:
    """INV-2: the link field and the occupancy of every source (requirement 8.11).

    Two parts, per conversion in model order and per record of its source by id:

    - the link field of each source record - each source line, or the source header for a
      push limited by amount - is the sum of what the approved and closed bills of every
      conversion added to it (:func:`linked_totals`); an empty field counts as 0, and two
      conversions that keep the same field are one check rather than two;
    - for a conversion limited by quantity, no source line is occupied beyond its own
      ``qty_base``: the occupancy is the one the quota of a push is measured against
      (:func:`~erp_mock.domain.conversion.occupied_qty`), and a source line no downstream
      line names is occupied by 0.

    Raises RuntimeError for an instance whose conversions do not resolve, which loading
    step 5 reports (requirement 2.9).
    """
    totals = linked_totals(uow, domain)
    found: list[InvariantViolation] = []
    checked: set[tuple[str, str, str]] = set()
    for conversion in domain.conversions.values():
        source = source_bill(domain, conversion)
        found.extend(_link_field_violations(uow, domain, conversion, source, totals, checked))
        if conversion.limit_by == "qty":
            found.extend(_occupancy_violations(uow, domain, conversion, source))
    return tuple(found)


# --- INV-3: the amounts of a bill and of its lines (requirements 6.4 and 6.5) ------------


def check_bill_amounts(uow: UnitOfWork, domain: DomainModel) -> tuple[InvariantViolation, ...]:
    """INV-3: a header is the sum of its lines, a line's total its amount plus its tax.

    Per bill type that has ``pricing``, in model order, and per bill by id: the three
    amounts of the header against the sum of the non-empty values of its lines, 0 where
    there is none and where the bill has no line (requirement 6.4), then the lines whose
    three amounts are all there, each of which holds ``amount_with_tax`` equal to
    ``amount`` plus ``tax_amount`` (requirement 6.5). A line with no amounts at all - one
    whose quantity, price or tax rate is empty - breaks neither (requirement 6.10).
    """
    found: list[InvariantViolation] = []
    for bill in domain.bills.values():
        if not bill.pricing:
            continue
        for header in uow.select(bill.name):
            lines = bill_lines(uow, bill, _id_of(header))
            found.extend(_header_amount_violations(bill, header, lines))
            found.extend(_line_amount_violations(bill, lines))
    return tuple(found)


# --- INV-6: the ids, the line ids and the numbers (requirement 4.10) ---------------------


def counter_values(uow: UnitOfWork) -> FrozenMap[tuple[str, str], int]:
    """The counters the sandbox holds, by (kind, key) (requirement 4.6).

    What the next allocation of each kind continues from; a counter no record of its key
    came from is absent, which counts as 0.
    """
    rows = uow.select(COUNTERS_TABLE)
    found: dict[tuple[str, str], int] = {}
    for row in rows:
        value = row.get(_COUNTER_VALUE)
        found[(_text(row.get(_COUNTER_KIND)), _text(row.get(_COUNTER_KEY)))] = (
            value if isinstance(value, int) else 0
        )
    return FrozenMap(found)


def id_sequence(prefix: str, text: str) -> int | None:
    """The sequence of the internal id ``text`` of ``prefix``, None for no such id (4.1).

    ``<prefix>_<sequence>`` as :func:`~erp_mock.domain.numbering.format_id` writes it, the
    sequence read back as the integer it is; None for a text that function produces for no
    sequence - another prefix, a sequence that is no ASCII decimal, or padding it does not
    write - which no allocation can hand out and which therefore collides with nothing.
    """
    head = f"{prefix}{_ID_SEPARATOR}"
    if not text.startswith(head):
        return None
    digits = text[len(head) :]
    if _DIGITS.fullmatch(digits) is None:
        return None
    sequence = int(digits)
    return sequence if format_id(prefix, sequence) == text else None


def number_parts(prefix: str, text: str) -> tuple[date, int] | None:
    """The date and the sequence of the bill number ``text`` of ``prefix`` (req 4.4).

    ``<prefix><YYYYMMDD><4 digits>`` as
    :func:`~erp_mock.domain.numbering.format_number` writes it, which also fixes the
    counter the sequence came from (:func:`~erp_mock.domain.numbering.number_key`); None
    for a text that function produces for no (date, sequence), a date that is no date
    included, on the same grounds as :func:`id_sequence`.
    """
    if not text.startswith(prefix):
        return None
    digits = text[len(prefix) :]
    if _DIGITS.fullmatch(digits) is None or len(digits) <= _DATE_WIDTH:
        return None
    try:
        day = date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None
    sequence = int(digits[_DATE_WIDTH:])
    return (day, sequence) if format_number(prefix, day, sequence) == text else None


def check_identifiers(uow: UnitOfWork, domain: DomainModel) -> tuple[InvariantViolation, ...]:
    """INV-6: the ids, the line ids and the numbers of the sandbox (requirement 4.10).

    The internal ids of every entity record and bill are pairwise distinct, the ids of
    every bill line are, and so are the bill numbers; each of the three is its own
    namespace, and an empty number is no duplicate of another empty one. Then the sequence
    every id and every number carries is at most the counter it came from, which is what
    says that the next id and the next number of that counter are ones no record ever had -
    the records of the sandbox do not take part in an allocation, so a deleted record's id
    and number are gone for good (requirements 4.1, 4.4 and 4.12).

    The duplicates come first, by the id or the number they are about, then the sequences,
    per type in model order and per record by id, the id of a bill before its number. Each
    table is read once, and neither stock table takes part: the id of a transaction and of
    a balance is built from the records it is about and comes from no counter
    (requirement 18.8).
    """
    counters = counter_values(uow)
    ids: list[_Scan] = []
    lines: list[_Scan] = []
    numbers: list[_Scan] = []
    sequences: list[InvariantViolation] = []
    for entity in domain.entities.values():
        records = uow.select(entity.name)
        ids.append((entity.name, records))
        sequences.extend(_id_violations(entity.name, entity.id_prefix, records, counters))
    for bill in domain.bills.values():
        records = uow.select(bill.name)
        ids.append((bill.name, records))
        numbers.append((bill.name, records))
        lines.append((bill.line_name, uow.select(bill.line_name)))
        sequences.extend(_id_violations(bill.name, bill.id_prefix, records, counters))
        sequences.extend(_number_violations(bill.name, bill.number_prefix, records, counters))
    found: list[InvariantViolation] = []
    found.extend(_duplicates(_values(ids, ID_COLUMN), DUPLICATE_ID, ID_COLUMN))
    found.extend(_duplicates(_values(lines, ID_COLUMN), DUPLICATE_LINE_ID, ID_COLUMN))
    found.extend(_duplicates(_values(numbers, _NUMBER), DUPLICATE_NUMBER, _NUMBER))
    found.extend(sequences)
    return tuple(found)


# --- internals ---------------------------------------------------------------------------

# The columns of a counter (design.md data model 3); the table itself is
# :data:`~erp_mock.state.schema.COUNTERS_TABLE`, which the unit of work reads through
# :meth:`~erp_mock.state.store.UnitOfWork.select` like any other table and writes through
# :meth:`~erp_mock.state.store.UnitOfWork.bump_counter` alone.
_COUNTER_KIND: Final = "kind"
_COUNTER_KEY: Final = "key"
_COUNTER_VALUE: Final = "value"

# The two kinds of counter INV-6 reads (requirement 4.6).
_ID_COUNTER: Final = "id"
_NUMBER_COUNTER: Final = "number"


def _stock_totals(uow: UnitOfWork, table: str, column: str) -> dict[tuple[str, str], Decimal]:
    """The sum of ``column`` of ``table`` per (material, warehouse) (requirement 9.8).

    The two columns are the engine's own on both stock tables, so one function reads the
    transactions and the balances; the sum is kept at quantity precision, as every write of
    the primitive is (requirement 4.7).
    """
    totals: dict[tuple[str, str], Decimal] = {}
    for row in uow.select(table):
        place = (_text(row.get(MATERIAL_COLUMN)), _text(row.get(WAREHOUSE_COLUMN)))
        totals[place] = _qty(WIDE.add(totals.get(place, _ZERO), _decimal(row.get(column))))
    return totals


def _ledger_targets(uow: UnitOfWork) -> tuple[str, ...]:
    """The bills the writeback ledger holds rows for, by id (design.md data model 3)."""
    rows = uow.select(WRITEBACK_LEDGER_TABLE)
    return tuple(dict.fromkeys(_text(row.get(WRITEBACK_TARGET_COLUMN)) for row in rows))


def _link_field_violations(
    uow: UnitOfWork,
    domain: DomainModel,
    conversion: ConversionModel,
    source: BillModel,
    totals: Mapping[tuple[str, str, str], Decimal],
    checked: set[tuple[str, str, str]],
) -> Iterator[InvariantViolation]:
    """The source records of ``conversion`` whose link field is not the sum (req 8.11).

    The link field lives on the lines of the source for a push limited by quantity and on
    its header for one limited by amount (:attr:`ConversionModel.link_field`); ``checked``
    carries the places already compared, so a field two conversions keep is reported once.
    """
    ref = conversion.link_field
    table = source.line_name if ref.level == "line" else source.name
    kind = link_kind(domain, table, ref.name)
    for record in uow.select(table):
        place = (table, _id_of(record), ref.name)
        if place in checked:
            continue
        checked.add(place)
        expected = totals.get(place, _ZERO)
        held = _decimal(record.get(ref.name))
        if held != expected:
            yield InvariantViolation(
                INV_2,
                LINK_FIELD_MISMATCH,
                (RecordRef(table, place[1]),),
                field=ref.name,
                cause=conversion.id,
                expected=format_decimal(kind, expected),
                found=format_decimal(kind, held),
            )


def _occupancy_violations(
    uow: UnitOfWork, domain: DomainModel, conversion: ConversionModel, source: BillModel
) -> Iterator[InvariantViolation]:
    """The source lines of ``conversion`` occupied beyond their ``qty_base`` (req 8.11).

    Every line of the source bill type, by id; the occupancy of a line a downstream line of
    this conversion names is :func:`~erp_mock.domain.conversion.occupied_qty`, and the
    occupancy of every other line is 0, which that function returns for it as well.
    """
    target = target_bill(domain, conversion)
    named = {_text(line.get(SOURCE_LINE_ID)) for line in uow.select(target.line_name)}
    for record in uow.select(source.line_name):
        line_id = _id_of(record)
        if line_id not in named:
            occupied = _ZERO
        else:
            occupied = occupied_qty(uow, conversion, target, line_id)
        own = _decimal(record.get(QTY_BASE))
        if occupied > own:
            yield InvariantViolation(
                INV_2,
                OCCUPIED_ABOVE_QTY_BASE,
                (RecordRef(source.line_name, line_id),),
                field=QTY_BASE,
                cause=conversion.id,
                expected=_qty_text(own),
                found=_qty_text(occupied),
            )


def _header_amount_violations(
    bill: BillModel, header: Record, lines: Sequence[Record]
) -> Iterator[InvariantViolation]:
    """The three amounts of ``header`` that are not the sum of the lines (req 6.4)."""
    ref = RecordRef(bill.name, _id_of(header))
    for field in AMOUNT_FIELDS:
        total = _sum_of(line.get(field) for line in lines)
        held = _decimal(header.get(field))
        if held != total:
            yield InvariantViolation(
                INV_3,
                HEADER_AMOUNT_MISMATCH,
                (ref,),
                field=field,
                expected=_money_text(total),
                found=_money_text(held),
            )


def _line_amount_violations(
    bill: BillModel, lines: Sequence[Record]
) -> Iterator[InvariantViolation]:
    """The lines whose total is not its net amount plus its tax (requirement 6.5)."""
    net_field, tax_field, total_field = AMOUNT_FIELDS
    for line in lines:
        net = line.get(net_field)
        tax = line.get(tax_field)
        total = line.get(total_field)
        if not (
            isinstance(net, Decimal) and isinstance(tax, Decimal) and isinstance(total, Decimal)
        ):
            continue
        expected = _money(WIDE.add(net, tax))
        if total != expected:
            yield InvariantViolation(
                INV_3,
                LINE_AMOUNT_MISMATCH,
                (RecordRef(bill.line_name, _id_of(line)),),
                field=total_field,
                expected=_money_text(expected),
                found=_money_text(total),
            )


def _values(tables: Sequence[_Scan], field: str) -> list[tuple[RecordRef, str]]:
    """The value of ``field`` of every record of ``tables``, with the record it is of.

    In the order of ``tables`` and by record id within each; a record whose value is empty
    is left out, so two bills without a number are no duplicate of each other.
    """
    found: list[tuple[RecordRef, str]] = []
    for table, records in tables:
        for record in records:
            value = _text(record.get(field))
            if value:
                found.append((RecordRef(table, _id_of(record)), value))
    return found


def _duplicates(
    values: Sequence[tuple[RecordRef, str]], reason: str, field: str
) -> Iterator[InvariantViolation]:
    """One violation per value more than one record of ``values`` holds (requirement 4.10).

    By the value, so the report does not depend on which table came first; the records of
    one violation are in the order they were read.
    """
    places: dict[str, list[RecordRef]] = {}
    for ref, value in values:
        places.setdefault(value, []).append(ref)
    for value in sorted(places):
        refs = places[value]
        if len(refs) > 1:
            yield InvariantViolation(INV_6, reason, tuple(refs), field=field, found=value)


def _id_violations(
    table: str,
    id_prefix: str,
    records: Sequence[Record],
    counters: Mapping[tuple[str, str], int],
) -> Iterator[InvariantViolation]:
    """The records of ``table`` whose id sequence is above its counter (req 4.1, 4.10).

    The id counter of an entity or a bill is keyed by its own name, which is its table
    (design.md 4.2); a counter no allocation ever raised counts as 0, which every id of
    that type is above - a type with no record has none.
    """
    counter = counters.get((_ID_COUNTER, table), 0)
    for record in records:
        record_id = _id_of(record)
        sequence = id_sequence(id_prefix, record_id)
        if sequence is not None and sequence > counter:
            yield InvariantViolation(
                INV_6,
                ID_ABOVE_COUNTER,
                (RecordRef(table, record_id),),
                field=ID_COLUMN,
                expected=str(counter),
                found=str(sequence),
            )


def _number_violations(
    table: str,
    prefix: str,
    records: Sequence[Record],
    counters: Mapping[tuple[str, str], int],
) -> Iterator[InvariantViolation]:
    """The bills of ``table`` whose number sequence is above its counter (req 4.4, 4.10).

    The counter of a bill number is keyed by its (number prefix, date), which the number
    itself names (:func:`~erp_mock.domain.numbering.number_key`), so two bill types that
    share a prefix share the counter their numbers are measured against.
    """
    for record in records:
        number = _text(record.get(_NUMBER))
        parts = number_parts(prefix, number)
        if parts is None:
            continue
        day, sequence = parts
        counter = counters.get((_NUMBER_COUNTER, number_key(prefix, day)), 0)
        if sequence > counter:
            yield InvariantViolation(
                INV_6,
                NUMBER_ABOVE_COUNTER,
                (RecordRef(table, _id_of(record)),),
                field=_NUMBER,
                expected=str(counter),
                found=str(sequence),
            )


def _sum_of(values: Iterable[Value]) -> Decimal:
    """The sum of the non-empty ``values`` at money precision, 0 for none (req 6.4)."""
    total = _ZERO
    for value in values:
        if isinstance(value, Decimal):
            total = WIDE.add(total, value)
    return _money(total)


def _qty(value: Decimal) -> Decimal:
    """``value`` at quantity precision, which every stored quantity is (requirement 4.7)."""
    return round_half_up(value, PLACES[_QTY])


def _money(value: Decimal) -> Decimal:
    """``value`` at money precision, which every stored amount is (requirement 4.7)."""
    return round_half_up(value, PLACES[_MONEY])


def _qty_text(value: Decimal) -> str:
    """``value`` as a quantity reads (requirement 4.8)."""
    return format_decimal(_QTY, value)


def _money_text(value: Decimal) -> str:
    """``value`` as an amount reads (requirement 4.8)."""
    return format_decimal(_MONEY, value)


def _decimal(value: Value) -> Decimal:
    """``value`` as the number it holds, 0 where the field is empty (requirement 6.10)."""
    return value if isinstance(value, Decimal) else _ZERO


def _text(value: Value) -> str:
    """``value`` as the text it is, the empty string for anything else."""
    return value if isinstance(value, str) else ""


def _id_of(record: Record) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""


# The check of each invariant, read by :func:`check_invariants` in the order of
# :data:`INVARIANTS`.
_CHECKS: Final[
    Mapping[InvariantId, Callable[[UnitOfWork, DomainModel], tuple[InvariantViolation, ...]]]
] = {
    INV_1: check_stock_balances,
    INV_2: check_link_fields,
    INV_3: check_bill_amounts,
    INV_6: check_identifiers,
}
