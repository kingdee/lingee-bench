"""Internal IDs, line IDs, codes, bill numbers and operation sequence numbers (req 4).

The numbering primitive hands out every identifier a record of a sandbox carries and owns
the counters they come from (design.md 4.2). Two slots of the pipeline use it: the
``candidate`` slot, where the record a write operation builds takes its internal ID, its
code, its bill number and the IDs of its lines, and the ``view`` slot, where a successful
write operation takes the next operation sequence number of the sandbox and writes it into
the bills it created and changed (design.md 4.1, steps 5 and 10)::

    bill_id = next_id(uow, bill)                    # so_000001
    number = next_number(uow, bill, biz_date)       # SPO202610090001
    ids = line_ids(bill_id, 3)                      # so_000001-1, -2, -3
    ...
    stamps.created_bill(bill.name, bill_id)
    seq = assign_operation_seq(uow, stamps)         # created_seq and updated_seq

The forms, each built by a ``format_`` function of its own so the engine and the tests read
the same rule:

- an internal ID is ``<id_prefix>_<sequence>``, the sequence padded with leading zeros to
  :data:`ID_DIGITS` digits and written in full where it outgrows them, as ``so_000001`` and
  ``so_1000000`` (requirement 4.1);
- a line ID is ``<bill ID><LINE_ID_SEPARATOR><line_no>``, the line number without leading
  zeros, so the twelfth line of ``so_000001`` is ``so_000001-12``; the line numbers of a
  bill run from :data:`FIRST_LINE_NO` in the order of its lines and need no counter
  (requirement 4.2);
- a code is ``<prefix><sequence>`` of the entity's code rule, the sequence padded to that
  rule's ``digits`` and written in full where it outgrows them, so with prefix ``V`` and 4
  digits the 107th code is ``V0107`` (requirement 4.3);
- a bill number is ``<prefix><YYYYMMDD><4 digits>``, the date being the business date the
  bill was created with and the sequence that of the (number prefix, date) counter, so the
  first bill of a prefix on a date ends in ``0001``; the number never changes afterwards, a
  later change of the business date included (requirement 4.4).

Counters. Every sequence comes from :meth:`~erp_mock.state.store.UnitOfWork.bump_counter`,
which raises it in the transaction of the operation, and the counters live in the sandbox
database (requirement 4.6): a sandbox created from a Snapshot continues the counters of
that Snapshot, and an operation that ends in an error rolls its counters back with
everything else, so the next successful operation gets exactly the IDs, numbers and
sequence number it would have got had the failed one never run (requirement 4.12). Nothing
here reads the records: a sequence is handed out once, so the ID and the number of a record
that was deleted are never assigned again (requirement 4.1 and 4.4, INV-6). Which counter
holds what (design.md 4.2): ``id`` and ``code`` are keyed by the name of the entity or the
bill, ``number`` by :func:`number_key` of its prefix and date, and ``op`` by the single
:data:`OP_COUNTER_KEY` of the sandbox.

Operation sequence numbers. Only a write operation takes one - a create of an entity record
or a bill, a push, an update, a delete and every state operation - so a read-only call and
a tools/list leave the ``op`` counter where it is (requirement 4.11); the engine decides
which operation that is (:func:`~erp_mock.domain.engine.is_write_operation`) and the slots
of the pipeline record on :class:`SequenceStamps` which bills the number is written into
(requirement 4.5).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Final

from erp_mock.config.model import BillModel, EntityModel
from erp_mock.state.store import UnitOfWork

__all__ = [
    "CREATED_SEQ",
    "FIRST_LINE_NO",
    "ID_DIGITS",
    "LINE_ID_SEPARATOR",
    "NUMBER_DIGITS",
    "NUMBER_KEY_SEPARATOR",
    "OP_COUNTER_KEY",
    "UPDATED_SEQ",
    "BillRef",
    "SequenceStamps",
    "assign_operation_seq",
    "format_code",
    "format_id",
    "format_number",
    "line_id",
    "line_ids",
    "line_numbers",
    "next_code",
    "next_id",
    "next_number",
    "next_operation_seq",
    "number_key",
    "stamp_bills",
]

# The sequence of an internal ID is padded to this many digits (requirement 4.1).
ID_DIGITS: Final = 6
# The sequence of a bill number is always four digits (requirement 4.4).
NUMBER_DIGITS: Final = 4

# What stands between a bill ID and the line number in a line ID (requirement 4.2); the
# underscore of an internal ID keeps the two forms apart.
LINE_ID_SEPARATOR: Final = "-"
# The line numbers of a bill start here and run on without a gap (requirement 4.2).
FIRST_LINE_NO: Final = 1

# The key of a bill number counter is the prefix and the date, held apart by a character
# that neither of them contains (design.md 4.2).
NUMBER_KEY_SEPARATOR: Final = "|"
# The operation sequence number is one counter of the whole sandbox, so its ``op`` kind has
# a single key (requirement 4.11, design.md 4.2).
OP_COUNTER_KEY: Final = ""

# The two standard fields of a bill header that carry the operation sequence numbers
# (meta-model.md section 4): the operation that created the bill and the one that changed
# it last (requirement 4.5). Both are read-only to every write tool (requirement 1.3).
CREATED_SEQ: Final = "created_seq"
UPDATED_SEQ: Final = "updated_seq"


# --- the forms (requirements 4.1 to 4.4) -------------------------------------------------


def format_id(id_prefix: str, seq: int) -> str:
    """The internal ID ``seq`` of ``id_prefix`` (requirement 4.1).

    Padded with leading zeros to :data:`ID_DIGITS` digits, and written with the digits it
    needs where it has more of them: ``so_000001``, ``so_999999``, ``so_1000000``.
    """
    return f"{id_prefix}_{seq:0{ID_DIGITS}d}"


def format_code(prefix: str, digits: int, seq: int) -> str:
    """The code ``seq`` of the code rule (``prefix``, ``digits``) (requirement 4.3).

    Padded with leading zeros to ``digits`` digits, and written with the digits it needs
    where it has more of them: with prefix ``S`` and 4 digits, ``S0107`` and ``S12345``.
    """
    return f"{prefix}{seq:0{digits}d}"


def format_number(prefix: str, biz_date: date, seq: int) -> str:
    """The bill number ``seq`` of ``prefix`` on ``biz_date`` (requirement 4.4).

    ``<prefix><YYYYMMDD><4 digits>``, as ``SPO202610090001``. A sequence that outgrows
    four digits keeps the digits it needs, as every other form does.
    """
    return f"{prefix}{_date_text(biz_date)}{seq:0{NUMBER_DIGITS}d}"


def line_id(bill_id: str, line_no: int) -> str:
    """The ID of the line ``line_no`` of the bill ``bill_id`` (requirement 4.2).

    The line number carries no leading zeros, so the lines of ``so_000001`` are
    ``so_000001-1`` to ``so_000001-12`` and beyond.
    """
    return f"{bill_id}{LINE_ID_SEPARATOR}{line_no:d}"


def line_ids(bill_id: str, count: int) -> tuple[str, ...]:
    """The IDs of the ``count`` lines of ``bill_id``, in the order of the lines (req 4.2).

    The line numbers run from :data:`FIRST_LINE_NO` without a gap, which is what a create,
    a push and an update that replaces the lines as a whole assign (requirement 5.3).
    """
    return tuple(line_id(bill_id, line_no) for line_no in line_numbers(count))


def line_numbers(count: int) -> range:
    """The line numbers of ``count`` lines, from :data:`FIRST_LINE_NO` on (req 4.2)."""
    return range(FIRST_LINE_NO, FIRST_LINE_NO + count)


def number_key(prefix: str, biz_date: date) -> str:
    """The key of the number counter of (``prefix``, ``biz_date``) (design.md 4.2)."""
    return f"{prefix}{NUMBER_KEY_SEPARATOR}{_date_text(biz_date)}"


# --- assigning (requirements 4.1, 4.3, 4.4, 4.6 and 4.11) --------------------------------


def next_id(uow: UnitOfWork, item: EntityModel | BillModel) -> str:
    """Assign the next internal ID of the entity or bill ``item`` (requirement 4.1).

    The ID counter of ``item`` is raised in the transaction of the operation, so two
    records of one type get consecutive sequences and a failed operation returns the
    counter to where it was (requirement 4.12).
    """
    return format_id(item.id_prefix, uow.bump_counter("id", item.name))


def next_code(uow: UnitOfWork, entity: EntityModel) -> str | None:
    """Assign the next code of ``entity``, None where it has no code rule (req 4.3).

    An entity that declares no ``code`` rule (:class:`~erp_mock.config.pack.CodeDef`)
    carries the code its shared data or its dataset writes, and no counter of it is
    raised.
    """
    code = entity.code
    if code is None:
        return None
    return format_code(code.prefix, code.digits, uow.bump_counter("code", entity.name))


def next_number(uow: UnitOfWork, bill: BillModel, biz_date: date) -> str:
    """Assign the next number of ``bill`` on the business date ``biz_date`` (req 4.4).

    The counter is the one of (number prefix, date), so the first bill of a prefix on a
    date ends in ``0001`` whatever other dates and prefixes the sandbox holds. The number
    is assigned with the business date the bill is created with and stays as it is
    afterwards: nothing here is called again for a bill that already has a number.
    """
    prefix = bill.number_prefix
    seq = uow.bump_counter("number", number_key(prefix, biz_date))
    return format_number(prefix, biz_date, seq)


def next_operation_seq(uow: UnitOfWork) -> int:
    """Assign the operation sequence number of this write operation (requirement 4.11).

    The ``op`` counter of the sandbox, raised once per write operation - one write tool
    call, and in a seed build every create and every bill operation. A read-only call and
    a tools/list do not call it, so the numbers count the write operations alone.
    """
    return uow.bump_counter("op", OP_COUNTER_KEY)


# --- writing the operation sequence number (requirement 4.5) -----------------------------


@dataclass(frozen=True, slots=True)
class BillRef:
    """One bill of the sandbox: the name of its type, which is its table, and its ID."""

    bill: str
    id: str


@dataclass(slots=True)
class SequenceStamps:
    """Which bills the operation sequence number is written into (requirement 4.5).

    The slots of the pipeline record here which bills their writes concern, and the
    ``view`` slot writes the sequence number of the operation into them
    (:func:`stamp_bills`):

    - :meth:`created_bill` for a bill the operation created or a push generated, whose
      ``created_seq`` and ``updated_seq`` both become that number;
    - :meth:`changed_bill` for the bill the operation was called on, and for a source bill
      a writeback, an automatic close or an automatic unclose changed, whose
      ``updated_seq`` becomes that number while its ``created_seq`` stays as it is.

    A bill named here neither way keeps both numbers, which is what the source bill of a
    push does (requirements 4.5 and 8.12). Each bill is stamped once, in the order it was
    recorded, and a bill the operation created is never stamped as a changed one as well.
    """

    created: list[BillRef] = field(default_factory=list)
    changed: list[BillRef] = field(default_factory=list)

    def created_bill(self, bill: str, bill_id: str) -> None:
        """Record that the operation created the bill ``bill_id`` of the type ``bill``."""
        ref = BillRef(bill, bill_id)
        if ref in self.created:
            return
        self.created.append(ref)
        if ref in self.changed:
            self.changed.remove(ref)

    def changed_bill(self, bill: str, bill_id: str) -> None:
        """Record that the operation changed the bill ``bill_id`` of the type ``bill``."""
        ref = BillRef(bill, bill_id)
        if ref not in self.created and ref not in self.changed:
            self.changed.append(ref)


def stamp_bills(uow: UnitOfWork, seq: int, stamps: SequenceStamps) -> None:
    """Write the operation sequence number ``seq`` into the bills of ``stamps`` (req 4.5).

    A created bill gets it in both ``created_seq`` and ``updated_seq``, a changed one in
    ``updated_seq`` alone; every other bill of the sandbox is left as it is.
    """
    for ref in stamps.created:
        uow.update(ref.bill, ref.id, {CREATED_SEQ: seq, UPDATED_SEQ: seq})
    for ref in stamps.changed:
        uow.update(ref.bill, ref.id, {UPDATED_SEQ: seq})


def assign_operation_seq(uow: UnitOfWork, stamps: SequenceStamps) -> int:
    """Take the next operation sequence number and stamp ``stamps`` with it (4.5, 4.11).

    What the ``view`` slot runs for a write operation (design.md 4.1 step 10): the number
    is taken even where no bill carries it - a create of an entity record, a delete - so
    the numbers of the write operations of a sandbox run on without a gap, and it is
    written before the result view is assembled, so a create returns the same object a get
    of the record returns right after it (requirement 5.16).
    """
    seq = next_operation_seq(uow)
    stamp_bills(uow, seq, stamps)
    return seq


def _date_text(biz_date: date) -> str:
    """``YYYYMMDD`` of ``biz_date``, the date part of a bill number (requirement 4.4)."""
    return f"{biz_date.year:04d}{biz_date.month:02d}{biz_date.day:02d}"
