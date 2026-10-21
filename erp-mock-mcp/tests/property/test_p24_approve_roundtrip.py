"""Property 24: the round trip of an approve and an unapprove.

**Validates: Requirements 7.6, 7.7, 8.8, 8.9, 8.13, 8.14, 9.6, 9.10**

For any state an approvable bill has been brought to, an approve followed immediately by
the unapprove of the same bill leaves the sandbox as it found it: the stock transactions and
the stock on hand, a balance record the approve created included (requirements 9.6 and
9.10), the link fields of the source bill the writebacks of the approve wrote to and the
state and the close mode of that source bill, an automatic close taken back again
(requirements 8.8, 8.9, 8.13 and 8.14); only the ``updated_seq`` of the bill itself and the
counters of the sandbox are different (requirements 7.6 and 7.7).

The reference model is the sandbox itself as it stood before the approve: every table of
the instance, every row of it and every column of those rows, read off storage and compared
whole. The model is therefore neither a second implementation of the writebacks nor one of
the linkages - properties 23 and 25 carry those - but the identity the two operations have
to compose to, and the comparison covers far more than the property names: the writeback
ledger, the lines of both bills, the records of the basic data and every bill the example
built along the way all have to come back unchanged as well.

What the comparison leaves out, which is the ``只有`` of the property:

- the ``_counters`` table: a write operation takes the next operation sequence number
  (requirement 4.5), so the ``op`` counter is two higher after the two operations. It is
  compared on its own: every other counter is where it was - the approve and the unapprove
  create no record, so no id, code or number counter moves - and the ``op`` counter advanced
  by exactly 2;
- the ``status`` of the bill under test, and only where the approve runs from a state it
  does not come back to: the standard machine approves a submitted bill and unapproves it to
  draft (requirement 7.2). A simple bill is approved out of draft and unapproved back to it,
  so its state is compared like every other column and the property then holds to the letter
  - which is what :class:`Fee` is in the example set for. The state is asserted on its own
  either way, before the approve and after the unapprove;
- the ``updated_seq`` of the bill under test and of its source bill: both carry the sequence
  number of the operation that last changed them, and a source bill a writeback changed is
  one of them (requirements 4.5 and 8.8). Every other record keeps its ``updated_seq`` in
  the comparison, so an operation that stamps a bill it had no business touching fails here.

That the approve did something at all is pinned before the unapprove runs, so the round trip
cannot hold by both halves doing nothing: the bill is approved, it has exactly the stock
transactions its linkage calls for - one per line for ``in``, ``out`` and ``adjust``, two
for a ``transfer``, none for a bill whose type declares no linkage (requirement 9.1) - and
exactly the ledger rows its writebacks call for - one per line for a writeback on the source
lines, one for a writeback on the source header, none for a bill no push generated
(requirement 8.8); and every (material, warehouse) that has a transaction has a balance
record (requirement 9.10).

What is drawn, per example: which of the four (material, warehouse) pairs of the fixture
start with stock on hand (:data:`SEEDINGS`), and one of six scenarios, which together cover
every linkage and every writeback the fixture declares
(tests/fixtures/packs/fx_trade) -

- :class:`Entry`, a goods entry pushed from a supply order: the ``in`` linkage and the
  writeback on the source lines at once, with the automatic close of the source order and
  its reopening (requirements 8.13 and 8.14). The source order gets 1 or 2 lines, a second
  entry may be pushed from it and approved first, and how much of each source line each of
  the two entries takes is drawn in quarters that add up to no more than the whole line - so
  a line linked in full, a partly linked one and a source order closed by the other entry
  all come up, and the push itself always stays within the quota of requirement 8.5. The
  source order may also be closed by the close operation beforehand, which an unapprove
  leaves closed (requirement 8.14);
- :class:`Slip`, a pay slip pushed from a fee note: the writeback on the source header, of a
  conversion limited by amount and of a source bill with a simple state machine, whose
  writeback declares no automatic close (requirement 7.11);
- :class:`Fee`, a fee note of its own: the bill with nothing to do - no push generated it
  and no linkage follows its type - and the one with a simple state machine, which approves
  out of draft and unapproves back to it (requirement 7.2);
- :class:`Issue`, :class:`Move` and :class:`Count`: the ``out``, ``transfer`` and ``adjust``
  linkages, which no writeback follows. A transfer out of a warehouse that holds stock into
  one that holds none is where requirement 9.10 bites hardest: the one approve creates a
  balance record on one pair and takes stock off the record of another, and the unapprove
  has to delete the first and restore the second.

A bill whose approve would leave a (material, warehouse) below 0 is not approvable at all,
and the property says nothing about it: the approve ends in R-GEN-007 (property 25 decides
when), the whole operation rolls back and the example asserts that instead - nothing moved,
the state of the bill and the counters included (requirement 9.4) - and ends there.

Every example gets an empty sandbox of its own with the standard basic data, because the
balance records of requirement 9.10 are the point: a pair that got a record from an earlier
example would never be a pair the approve of this one creates one for.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from typing import Final, assert_never

import pytest
from hypothesis import event, example, given, note, settings
from hypothesis import strategies as st

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.domain.inventory import NEGATIVE_STOCK_RULE
from erp_mock.domain.numbering import LINE_ID_SEPARATOR, OP_COUNTER_KEY, UPDATED_SEQ
from erp_mock.domain.state_machine import APPROVED, DRAFT, STATUS, SUBMITTED
from erp_mock.state.schema import (
    COUNTERS_TABLE,
    ID_COLUMN,
    MATERIAL_COLUMN,
    SOURCE_LINE_COLUMN,
    STOCK_BALANCE_TABLE,
    STOCK_TXN_TABLE,
    WAREHOUSE_COLUMN,
    WRITEBACK_LEDGER_TABLE,
    WRITEBACK_TARGET_COLUMN,
)
from erp_mock.state.store import Record, Value
from tests.helpers.engine_env import (
    BOLT,
    BOX,
    MAIN_DEPOT,
    MAIN_VENDOR,
    SPARE_DEPOT,
    WASHER,
    EngineEnv,
    as_record,
    engine_env,
)

# The bills of the engine test fixture an example works with: the two sources of a push, the
# two bills a push generates and the three other bills that move stock
# (tests/fixtures/packs/fx_trade/entities).
ORDER: Final = "supply_order"
ENTRY: Final = "goods_entry"
NOTE: Final = "fee_note"
SLIP: Final = "pay_slip"
ISSUE: Final = "issue_note"
MOVE: Final = "stock_move"
COUNT: Final = "count_sheet"

# The two conversions of the fixture and the short name the push tool of each names its
# source bill under, which the field path of that parameter is built from (req 11.13).
ORDER_TO_ENTRY: Final = "supply_order_to_entry"
FEE_TO_SLIP: Final = "fee_note_to_pay_slip"
ORDER_SHORT: Final = "order"
FEE_SHORT: Final = "fee"

# The fields a bill of the fixture takes from a caller, by the name each bill gives them.
ARTICLE: Final = "article_id"
MEASURE: Final = "measure_id"
DEPOT: Final = "depot_id"
FROM_DEPOT: Final = "from_depot_id"
TO_DEPOT: Final = "to_depot_id"
VENDOR: Final = "vendor_id"
NEED_DATE: Final = "need_date"
DUE_DATE: Final = "due_date"
ITEM_TEXT: Final = "item_text"
COUNT_KIND: Final = "count_kind"
QTY: Final = "qty"
PRICE: Final = "price"
SOURCE_LINE_ID: Final = "source_line_id"
ENTRY_QTY: Final = "entry_qty"
ISSUE_QTY: Final = "issue_qty"
MOVE_QTY: Final = "move_qty"
DIFF_QTY: Final = "diff_qty"

# The two values of the enum field the adjust direction of a count sheet reads: a gain adds
# the quantity to the stock on hand and a loss takes it off (requirement 9.2).
GAIN: Final = "gain"
LOSS: Final = "loss"

# The (material, warehouse) pairs a bill of the fixture may touch: the two materials of the
# basic data in its two warehouses, in the order a pattern of :data:`SEEDINGS` lists them.
PLACES: Final[tuple[tuple[str, str], ...]] = (
    (BOLT, MAIN_DEPOT),
    (BOLT, SPARE_DEPOT),
    (WASHER, MAIN_DEPOT),
    (WASHER, SPARE_DEPOT),
)

# Which pairs start with stock on hand, which decides what the approve of a bill does to a
# balance record: a pair that has none gets one, which the unapprove then has to delete
# (requirement 9.10), and a pair that has one keeps it at the quantity it held. The patterns
# cover all four pairs, one warehouse, one material and no stock at all.
SEEDINGS: Final[tuple[tuple[bool, ...], ...]] = (
    (True, True, True, True),
    (True, False, True, False),
    (False, True, False, True),
    (True, True, False, False),
    (False, False, False, False),
)
EVERY_PLACE: Final = SEEDINGS[0]
NO_PLACE: Final = SEEDINGS[-1]

# How much a seeded pair starts with, in base units. Well above anything a drawn bill takes
# off a pair, so a bill all of whose pairs are seeded is approvable (requirement 9.5).
SEED_QTY: Final = Decimal("100")

# The quantities a line of a bill that only moves stock carries. A line of the bolt may be
# written in boxes, which are 12 base units each, so the most one such bill takes off a pair
# is 3 lines of 2 boxes, well within :data:`SEED_QTY` (requirements 6.6 and 9.5).
STOCK_QUANTITIES: Final[tuple[Decimal, ...]] = tuple(
    Decimal(text) for text in ("0", "0.5", "1", "2")
)

# The quantities a line of the source order is ordered with: values a quarter of which is
# still a quantity of four decimals, in the unit the line is ordered in (requirement 4.7).
ORDER_QUANTITIES: Final[tuple[Decimal, ...]] = tuple(Decimal(text) for text in ("1", "2", "4"))

# The quantity and the price of the line of the source fee note: values a quarter of which
# gives an amount of exactly two decimals, so four quarters of it are the whole amount of
# the note and the quota of requirement 8.6 is met exactly rather than by a rounded value.
NOTE_QUANTITIES: Final[tuple[Decimal, ...]] = tuple(Decimal(text) for text in ("40", "100"))
NOTE_PRICES: Final[tuple[Decimal, ...]] = tuple(Decimal(text) for text in ("1.5", "2"))

# The price of a line of the source order: a pricing bill needs one to leave draft
# (requirement 7.5) and the quota of that conversion is counted in quantities alone.
ORDER_PRICE: Final = Decimal("1")

# How much of a source line a push may take, in quarters of it: 0 takes nothing and 4 takes
# the whole line and links it in full, which is what the automatic close asks of every
# source line (requirement 8.13). The two pushes of one example take no more than 4 between
# them, so both stay within the quota of requirement 8.5.
QUARTERS: Final = 4

# How many lines a drawn bill gets. A bill always has one, since a standard bill without
# lines cannot be submitted (R-GEN-002, requirement 7.5).
MAX_STOCK_LINES: Final = 3
MAX_ORDER_LINES: Final = 2

# The materials a line may name and the warehouses a bill may name. Both pools are small on
# purpose, so two lines of one bill land on the same (material, warehouse) often.
MATERIALS: Final[tuple[str, ...]] = (BOLT, WASHER)
DEPOTS: Final[tuple[str, ...]] = (MAIN_DEPOT, SPARE_DEPOT)

# How many stock transactions one line of a bill writes, by the direction of its linkage
# (requirement 9.1): one for in, out and adjust, and two for a transfer - one in the
# warehouse the stock leaves and one in the warehouse it arrives at.
ONE_MOVE: Final = 1
TWO_MOVES: Final = 2

# The kind of counter the two operations do move: the operation sequence number of the
# sandbox, which every write operation takes the next of (requirements 4.5 and 4.6).
OP_COUNTER: Final = "op"

# The columns the comparison leaves out of one record (see the module docstring): both of
# them for a bill under test whose approve runs from a state it does not come back to, and
# the sequence number alone for every other record a column of which legitimately moves -
# the source bill a writeback changed, and a simple bill under test.
STATE_AND_SEQ: Final[frozenset[str]] = frozenset({STATUS, UPDATED_SEQ})
SEQ_ALONE: Final[frozenset[str]] = frozenset({UPDATED_SEQ})

# One record, named by its table and its internal id; what a column is exempted of.
type Place = tuple[str, str]
# One row of a table, as storage holds it: every column, the internal ones included.
type Row = dict[str, Value]
# The content of every table of the instance but ``_counters``, by table name, each table's
# rows in primary key order (:meth:`~erp_mock.state.store.UnitOfWork.select`).
type Content = dict[str, list[Row]]
# What a record has no column exempted of, which keeps :func:`without` free of special cases.
_KEEP: Final[frozenset[str]] = frozenset()


# --- what one example works on -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Moved:
    """One line of a bill that only moves stock: what it measures and how much.

    ``in_boxes`` writes the line in the alternative unit of the bolt, the one material of
    the fixture that has a conversion record, so its base unit quantity is the drawn
    quantity times 12 (requirement 6.6). The warehouse is not on the line: every bill that
    uses this reads its warehouses off the header (requirement 9.11).
    """

    material: str
    qty: Decimal
    in_boxes: bool


@dataclass(frozen=True, slots=True)
class Ordered:
    """One line of the source order of an :class:`Entry`: what it orders and how much.

    ``in_boxes`` orders the line in the alternative unit of the bolt, so the quota it gives
    and the quantity a push of it writes back are both counted in base units
    (requirements 6.6 and 8.8).
    """

    material: str
    qty: Decimal
    in_boxes: bool


@dataclass(frozen=True, slots=True)
class Entry:
    """A goods entry pushed from a supply order: the ``in`` linkage and a writeback.

    ``order`` are the lines of the source order. ``sibling`` is how many quarters of each
    source line a second entry takes, which is pushed and approved before the one under
    test, or None for an example with one entry alone; ``take`` is how many quarters the
    entry under test takes of each source line, and the two add up to no more than
    :data:`QUARTERS`, so both pushes stay within the quota (requirement 8.5). ``depots`` is
    the warehouse each line of the entry under test receives into, the warehouse field of
    this bill being on the line (requirement 9.11). ``closed`` closes the source order
    through the close operation before either entry is approved, which an unapprove leaves
    closed however little is linked (requirement 8.14).
    """

    order: tuple[Ordered, ...]
    sibling: tuple[int, ...] | None
    take: tuple[int, ...]
    depots: tuple[str, ...]
    closed: bool


@dataclass(frozen=True, slots=True)
class Slip:
    """A pay slip pushed from a fee note: a writeback on the source header (req 8.8).

    ``qty`` and ``price`` are the one line of the source note, whose amount is the quota of
    this conversion (requirement 8.6); ``sibling`` is how many quarters of that amount a
    second slip takes, approved before the one under test, or None for an example with one
    slip alone, and ``take`` how many quarters the slip under test takes.
    """

    qty: Decimal
    price: Decimal
    sibling: int | None
    take: int


@dataclass(frozen=True, slots=True)
class Fee:
    """A fee note of its own: no linkage follows its approve and it writes nothing back.

    The one scenario whose bill under test has a simple state machine, which approves a
    draft and unapproves it back to draft (requirement 7.2), so its state comes back with
    everything else and the ``only the updated_seq of the bill and the counters are
    different`` of the property holds to the letter - which is what the comparison asks of
    it, the state of a simple bill being exempted from nothing (:func:`exempted`).
    """

    qty: Decimal
    price: Decimal


@dataclass(frozen=True, slots=True)
class Issue:
    """An issue note: the ``out`` linkage, which takes stock off one warehouse (req 9.2)."""

    depot: str
    lines: tuple[Moved, ...]


@dataclass(frozen=True, slots=True)
class Move:
    """A stock move: the ``transfer`` linkage, which writes two transactions a line (9.2).

    ``depot`` is the warehouse the stock leaves and ``target`` the one it arrives at; the two
    differ, which R-FXT-001 of the fixture asks of every stock move.
    """

    depot: str
    target: str
    lines: tuple[Moved, ...]


@dataclass(frozen=True, slots=True)
class Count:
    """A count sheet: the ``adjust`` linkage, whose sign its enum field decides (req 9.2)."""

    depot: str
    gain: bool
    lines: tuple[Moved, ...]


# The bill under test and how an example gets it into the state the approve runs from.
type Scenario = Entry | Slip | Fee | Issue | Move | Count


@dataclass(frozen=True, slots=True)
class Case:
    """One example: which pairs start with stock on hand, and the bill under test."""

    seeded: tuple[bool, ...]
    scenario: Scenario


@dataclass(frozen=True, slots=True)
class Prepared:
    """The bill the approve runs on, once the example has built everything around it.

    ``pre`` is the state the approve runs from: submitted for a standard bill and draft for
    a simple one (requirement 7.2). ``source`` is the source bill the writebacks of this
    bill write to, as a table and an id, and None for a bill no push generated; ``moves``
    and ``amounts`` are how many stock transactions and how many ledger rows its approve
    writes (requirements 9.1 and 8.8), which is what pins that the approve did anything at
    all.
    """

    bill: str
    bill_id: str
    pre: str
    source: Place | None
    moves: int
    amounts: int


# --- reading the sandbox, which is the reference model -----------------------------------


def content(env: EngineEnv) -> Content:
    """Every table of the instance but ``_counters``, every row and every column.

    Read in one transaction, each table in primary key order, with the internal columns
    among them: the close mode and the conversion of a bill header, the bill a line belongs
    to, the rows of the writeback ledger and the effective calls of the sandbox. The
    counters are read by :meth:`~tests.helpers.engine_env.EngineEnv.counters` instead, since
    the operation sequence counter is the one thing the two operations do move
    (requirement 4.5).
    """
    with env.store.transaction() as uow:
        return {
            name: [dict(record) for record in uow.select(name)]
            for name in env.schema.table_names
            if name != COUNTERS_TABLE
        }


def row_id(row: Mapping[str, Value]) -> str:
    """The internal id of ``row``, or "" for a row of a table that has no ``id`` column.

    The writeback ledger and the counters are keyed by several columns rather than by an id
    (design.md data model 3), and "" is no id a record can have, so such a row never matches
    a record the comparison exempts a column of.
    """
    value = row.get(ID_COLUMN)
    return value if isinstance(value, str) else ""


def without(state: Content, exempt: Mapping[Place, frozenset[str]]) -> Content:
    """``state`` with the exempted columns dropped from the records they are exempted of.

    Everything else stays, the internal columns included, so the comparison is of the whole
    sandbox but for the handful of columns the module docstring names.
    """
    return {
        name: [
            {
                column: value
                for column, value in row.items()
                if column not in exempt.get((name, row_id(row)), _KEEP)
            }
            for row in rows
        ]
        for name, rows in state.items()
    }


def exempted(prepared: Prepared) -> dict[Place, frozenset[str]]:
    """The columns the comparison leaves out, by record (see the module docstring).

    The state of the bill under test is exempted only where the approve runs from a state
    it does not come back to, which is the submitted of a standard bill (requirement 7.2);
    a simple bill is approved from draft and unapproved back to it, so its state is
    compared like every other column.
    """
    own = STATE_AND_SEQ if prepared.pre != DRAFT else SEQ_ALONE
    exempt = {(prepared.bill, prepared.bill_id): own}
    if prepared.source is not None:
        exempt[prepared.source] = SEQ_ALONE
    return exempt


def moves_of(env: EngineEnv, bill_id: str) -> list[Record]:
    """The stock transactions the approve of the bill ``bill_id`` wrote (requirement 9.1).

    Every transaction names the line it came from, and a line id starts with the id of its
    bill and the line separator, which no other bill's id is a prefix of (requirement 4.2).
    """
    prefix = f"{bill_id}{LINE_ID_SEPARATOR}"
    return [
        row
        for row in env.records(STOCK_TXN_TABLE)
        if str(row[SOURCE_LINE_COLUMN]).startswith(prefix)
    ]


def amounts_of(env: EngineEnv, bill_id: str) -> list[Record]:
    """The ledger rows the approve of the bill ``bill_id`` wrote (requirement 8.8)."""
    return [
        row
        for row in env.records(WRITEBACK_LEDGER_TABLE)
        if row[WRITEBACK_TARGET_COLUMN] == bill_id
    ]


def places_of(rows: Sequence[Mapping[str, Value]]) -> set[tuple[str, str]]:
    """The (material, warehouse) pairs ``rows`` name, rows of either stock table."""
    return {(str(row[MATERIAL_COLUMN]), str(row[WAREHOUSE_COLUMN])) for row in rows}


def status_of(env: EngineEnv, prepared: Prepared) -> Value:
    """The state of the bill under test as storage holds it (requirement 7.1)."""
    header = env.record(prepared.bill, prepared.bill_id)
    assert header is not None, f"{prepared.bill} {prepared.bill_id} is not in the sandbox"
    return header[STATUS]


def operations(counters: Mapping[tuple[str, str], int]) -> int:
    """How many write operations the sandbox has run (requirements 4.5 and 4.6)."""
    return counters[(OP_COUNTER, OP_COUNTER_KEY)]


def but_operations(counters: Mapping[tuple[str, str], int]) -> dict[tuple[str, str], int]:
    """``counters`` without the operation sequence number (requirements 4.5 and 4.6)."""
    return {key: value for key, value in counters.items() if key[0] != OP_COUNTER}


# --- building what an example starts from ------------------------------------------------


def bill_id_of(result: object) -> str:
    """The internal id of the record a write operation returned (requirement 5.16)."""
    assert hasattr(result, "view")
    value = as_record(result.view).fields[ID_COLUMN]
    assert isinstance(value, str), "a write operation returned no id"
    return value


def created(env: EngineEnv, bill: str, header: Mapping[str, Value], lines: Sequence[Row]) -> str:
    """Create the bill ``bill`` and return its internal id (requirements 5.1 and 5.16)."""
    return bill_id_of(env.create(bill, dict(header), [dict(line) for line in lines]))


def approve_bill(env: EngineEnv, bill: str, bill_id: str) -> None:
    """Submit and approve the standard bill ``bill_id`` (requirements 7.1 and 7.6)."""
    env.transition(bill, bill_id, "submit")
    env.transition(bill, bill_id, "approve")


def measured(env: EngineEnv, material: str, in_boxes: bool) -> dict[str, Value]:
    """The material of a line and, for a line written in boxes, its unit (req 6.6).

    Only the bolt has a conversion record, so only its lines may be written in boxes; the
    unit of every other line defaults to the base unit of its material (requirement 5.1).
    """
    values: dict[str, Value] = {ARTICLE: env.id(material)}
    if in_boxes:
        values[MEASURE] = env.id(BOX)
    return values


def seed_stock(env: EngineEnv, seeded: Sequence[bool]) -> None:
    """Put :data:`SEED_QTY` base units on every seeded pair, through one goods entry.

    A goods entry only adds stock, so this approve never breaks R-GEN-007; a pattern that
    seeds nothing writes no bill at all and leaves every pair without a balance record
    (requirement 9.10).
    """
    lines = [
        {**measured(env, material, False), DEPOT: env.id(depot), ENTRY_QTY: SEED_QTY}
        for (material, depot), wanted in zip(PLACES, seeded, strict=True)
        if wanted
    ]
    if not lines:
        return
    approve_bill(env, ENTRY, created(env, ENTRY, {VENDOR: env.id(MAIN_VENDOR)}, lines))


def quarters_of(whole: Decimal, taken: int) -> Decimal:
    """``taken`` quarters of ``whole``, which is a quantity of four decimals (req 4.7).

    The pools of :data:`ORDER_QUANTITIES` and :data:`NOTE_QUANTITIES` are values a quarter
    of which still fits a quantity field, so the division is exact and a push of
    :data:`QUARTERS` quarters asks for the whole line rather than a rounded part of it.
    """
    return whole * taken / QUARTERS


def source_line_of(bill_id: str, index: int) -> str:
    """The id of the line at ``index`` of the bill ``bill_id``, which a push names (4.2)."""
    return f"{bill_id}{LINE_ID_SEPARATOR}{index + 1}"


def push_entry(env: EngineEnv, scenario: Entry, order: str, take: Sequence[int]) -> str:
    """Push one goods entry draft off the source order and return its id (requirement 8.1).

    One line per source line, each receiving ``take`` quarters of it into the warehouse the
    scenario names for that position; a line of 0 is written too, so an entry that takes
    nothing is still a bill with lines (requirement 7.5).
    """
    lines: list[Row] = [
        {
            SOURCE_LINE_ID: source_line_of(order, index),
            ENTRY_QTY: quarters_of(item.qty, taken),
            DEPOT: env.id(scenario.depots[index]),
        }
        for index, (item, taken) in enumerate(zip(scenario.order, take, strict=True))
    ]
    return bill_id_of(env.push(ORDER_TO_ENTRY, order, lines, source_name=ORDER_SHORT))


def build_entry(env: EngineEnv, scenario: Entry) -> Prepared:
    """Build the source order and the goods entry under test (requirements 8.1 and 8.3).

    The order is approved first, since a push needs an approved source bill; both entry
    drafts are pushed while it still is one, so a source order the other entry closes
    automatically does not leave this example without a bill to approve (requirements 8.3
    and 8.13). The close operation runs before the other entry is approved, so a manually
    closed source order is not closed again by a writeback (requirement 8.13), and the entry
    under test is left submitted, ready for its approve.
    """
    header: dict[str, Value] = {VENDOR: env.id(MAIN_VENDOR), NEED_DATE: env.anchor_date}
    lines = [
        {**measured(env, item.material, item.in_boxes), QTY: item.qty, PRICE: ORDER_PRICE}
        for item in scenario.order
    ]
    order = created(env, ORDER, header, lines)
    approve_bill(env, ORDER, order)
    sibling = (
        None if scenario.sibling is None else push_entry(env, scenario, order, scenario.sibling)
    )
    entry = push_entry(env, scenario, order, scenario.take)
    if scenario.closed:
        env.transition(ORDER, order, "close")
    if sibling is not None:
        approve_bill(env, ENTRY, sibling)
    env.transition(ENTRY, entry, "submit")
    return Prepared(
        bill=ENTRY,
        bill_id=entry,
        pre=SUBMITTED,
        source=(ORDER, order),
        # The ``in`` linkage writes one transaction per line and the writeback one amount
        # per line, whatever that amount is (requirements 9.1 and 8.8).
        moves=len(scenario.order) * ONE_MOVE,
        amounts=len(scenario.order),
    )


def push_slip(env: EngineEnv, scenario: Slip, note_id: str, taken: int) -> str:
    """Push one pay slip draft off the source fee note and return its id (requirement 8.1).

    One line, which takes ``taken`` quarters of the quantity of the note's line at the same
    price, so the amount of the slip is that many quarters of the amount the quota of this
    conversion is counted against (requirement 8.6).
    """
    line: dict[str, Value] = {
        SOURCE_LINE_ID: source_line_of(note_id, 0),
        QTY: quarters_of(scenario.qty, taken),
        PRICE: scenario.price,
    }
    return bill_id_of(env.push(FEE_TO_SLIP, note_id, [line], source_name=FEE_SHORT))


def build_slip(env: EngineEnv, scenario: Slip) -> Prepared:
    """Build the source fee note and the pay slip under test (requirements 8.1 and 8.6).

    The note has a simple state machine, so it is approved straight out of draft
    (requirement 7.2); both slip drafts are pushed off it, the other one is approved first
    and the slip under test is left submitted.
    """
    header: dict[str, Value] = {VENDOR: env.id(MAIN_VENDOR), DUE_DATE: env.anchor_date}
    line: dict[str, Value] = {ITEM_TEXT: "夹具运费", QTY: scenario.qty, PRICE: scenario.price}
    note_id = created(env, NOTE, header, [line])
    env.transition(NOTE, note_id, "approve")
    sibling = (
        None if scenario.sibling is None else push_slip(env, scenario, note_id, scenario.sibling)
    )
    slip = push_slip(env, scenario, note_id, scenario.take)
    if sibling is not None:
        approve_bill(env, SLIP, sibling)
    env.transition(SLIP, slip, "submit")
    # No linkage follows a pay slip, and its writeback adds one amount to the source header.
    return Prepared(
        bill=SLIP, bill_id=slip, pre=SUBMITTED, source=(NOTE, note_id), moves=0, amounts=1
    )


def build_fee(env: EngineEnv, scenario: Fee) -> Prepared:
    """Create the fee note under test and leave it in draft, ready for its approve (7.2).

    A simple bill is approved straight out of draft and has no submit at all
    (requirement 7.11); no conversion generated this one and no linkage follows its type, so
    its approve writes neither a stock transaction nor a ledger row.
    """
    header: dict[str, Value] = {VENDOR: env.id(MAIN_VENDOR), DUE_DATE: env.anchor_date}
    line: dict[str, Value] = {ITEM_TEXT: "夹具仓储费", QTY: scenario.qty, PRICE: scenario.price}
    note_id = created(env, NOTE, header, [line])
    return Prepared(bill=NOTE, bill_id=note_id, pre=DRAFT, source=None, moves=0, amounts=0)


def line_values(env: EngineEnv, qty_field: str, lines: Sequence[Moved]) -> list[Row]:
    """The lines of a bill that only moves stock, as a create carries them (req 5.1, 9.2)."""
    return [{**measured(env, line.material, line.in_boxes), qty_field: line.qty} for line in lines]


def build_moving(env: EngineEnv, scenario: Issue | Move | Count) -> Prepared:
    """Build the bill under test of a scenario that only moves stock (requirement 9.2).

    The header carries the warehouses the linkage of the bill reads and, for a count sheet,
    the enum field its adjust direction reads; the bill is created and submitted, ready for
    its approve. No conversion generated it, so its approve writes nothing back (req 8.8).
    """
    header: dict[str, Value]
    match scenario:
        case Issue(depot=depot, lines=lines):
            bill, qty_field, per_line = ISSUE, ISSUE_QTY, ONE_MOVE
            header = {DEPOT: env.id(depot)}
        case Move(depot=depot, target=target, lines=lines):
            bill, qty_field, per_line = MOVE, MOVE_QTY, TWO_MOVES
            header = {FROM_DEPOT: env.id(depot), TO_DEPOT: env.id(target)}
        case Count(depot=depot, gain=gain, lines=lines):
            bill, qty_field, per_line = COUNT, DIFF_QTY, ONE_MOVE
            header = {DEPOT: env.id(depot), COUNT_KIND: GAIN if gain else LOSS}
        case _:
            assert_never(scenario)
    bill_id = created(env, bill, header, line_values(env, qty_field, lines))
    env.transition(bill, bill_id, "submit")
    return Prepared(
        bill=bill,
        bill_id=bill_id,
        pre=SUBMITTED,
        source=None,
        moves=len(lines) * per_line,
        amounts=0,
    )


def prepare(env: EngineEnv, case: Case) -> Prepared:
    """Put the stock of ``case`` on hand and build its bill under test, ready to approve."""
    seed_stock(env, case.seeded)
    match case.scenario:
        case Entry():
            return build_entry(env, case.scenario)
        case Slip():
            return build_slip(env, case.scenario)
        case Fee():
            return build_fee(env, case.scenario)
        case Issue() | Move() | Count():
            return build_moving(env, case.scenario)
        case _:
            assert_never(case.scenario)


# --- what is drawn -----------------------------------------------------------------------


@st.composite
def stock_lines(draw: st.DrawFn) -> Moved:
    """Draw one line of a bill that only moves stock: a material, a quantity and a unit."""
    material = draw(st.sampled_from(MATERIALS))
    boxes = draw(st.booleans())
    return Moved(
        material=material,
        qty=draw(st.sampled_from(STOCK_QUANTITIES)),
        # Only the bolt has a conversion record, so only its lines may be written in boxes.
        in_boxes=boxes and material == BOLT,
    )


def line_sets() -> st.SearchStrategy[tuple[Moved, ...]]:
    """1 to :data:`MAX_STOCK_LINES` lines of a bill that only moves stock."""
    return st.lists(stock_lines(), min_size=1, max_size=MAX_STOCK_LINES).map(tuple)


@st.composite
def ordered_lines(draw: st.DrawFn) -> Ordered:
    """Draw one line of the source order: a material, a quantity and a unit."""
    material = draw(st.sampled_from(MATERIALS))
    boxes = draw(st.booleans())
    return Ordered(
        material=material,
        qty=draw(st.sampled_from(ORDER_QUANTITIES)),
        in_boxes=boxes and material == BOLT,
    )


@st.composite
def shares(draw: st.DrawFn, lines: int) -> tuple[tuple[int, ...] | None, tuple[int, ...]]:
    """Draw how many quarters of each source line the two pushes of an example take.

    The two add up to no more than :data:`QUARTERS` per source line, so both pushes stay
    within the quota of requirement 8.5. The entry under test is weighted towards taking all
    that is left, so a source line linked in full - which is what the automatic close asks of
    every line (requirement 8.13) - comes up about as often as a partly linked one, whether
    there is a second entry or not.
    """
    other = draw(st.booleans())
    first = tuple(
        draw(st.integers(min_value=0, max_value=QUARTERS)) if other else 0 for _ in range(lines)
    )
    second = tuple(
        draw(st.sampled_from([QUARTERS - taken, *range(QUARTERS - taken + 1)])) for taken in first
    )
    return (first if other else None, second)


@st.composite
def entries(draw: st.DrawFn) -> Entry:
    """Draw a goods entry pushed from a supply order, with the order it comes from."""
    # The count is drawn rather than left to the shape of a list strategy, so an order of two
    # lines - the shape a source bill only partly linked needs - comes up as often as one of
    # a single line (requirement 8.13).
    count = draw(st.integers(min_value=1, max_value=MAX_ORDER_LINES))
    order = tuple(draw(ordered_lines()) for _ in range(count))
    sibling, take = draw(shares(count))
    return Entry(
        order=order,
        sibling=sibling,
        take=take,
        depots=tuple(draw(st.sampled_from(DEPOTS)) for _ in range(count)),
        closed=draw(st.booleans()),
    )


@st.composite
def slips(draw: st.DrawFn) -> Slip:
    """Draw a pay slip pushed from a fee note, with the note it comes from."""
    other = draw(st.booleans())
    sibling = draw(st.integers(min_value=0, max_value=QUARTERS)) if other else 0
    return Slip(
        qty=draw(st.sampled_from(NOTE_QUANTITIES)),
        price=draw(st.sampled_from(NOTE_PRICES)),
        sibling=sibling if other else None,
        take=draw(st.integers(min_value=0, max_value=QUARTERS - sibling)),
    )


@st.composite
def fees(draw: st.DrawFn) -> Fee:
    """Draw a fee note of its own: the quantity and the price of its one line."""
    return Fee(qty=draw(st.sampled_from(NOTE_QUANTITIES)), price=draw(st.sampled_from(NOTE_PRICES)))


@st.composite
def issues(draw: st.DrawFn) -> Issue:
    """Draw an issue note: the warehouse it takes stock off and its lines."""
    return Issue(depot=draw(st.sampled_from(DEPOTS)), lines=draw(line_sets()))


@st.composite
def moves(draw: st.DrawFn) -> Move:
    """Draw a stock move: the two warehouses, which differ (R-FXT-001), and its lines."""
    depot = draw(st.sampled_from(DEPOTS))
    return Move(
        depot=depot,
        target=draw(st.sampled_from([other for other in DEPOTS if other != depot])),
        lines=draw(line_sets()),
    )


@st.composite
def counts(draw: st.DrawFn) -> Count:
    """Draw a count sheet: its warehouse, whether it is a gain or a loss, and its lines."""
    return Count(
        depot=draw(st.sampled_from(DEPOTS)),
        gain=draw(st.booleans()),
        lines=draw(line_sets()),
    )


@st.composite
def cases(draw: st.DrawFn) -> Case:
    """Draw one example: the stock to start from and the bill under test.

    The scenario is drawn by picking one of the six strategies and then drawing from it,
    rather than with ``one_of``, so each of them - each linkage and each writeback of the
    fixture - comes up about as often as the others.
    """
    scenarios: tuple[st.SearchStrategy[Scenario], ...] = (
        entries(),
        slips(),
        fees(),
        issues(),
        moves(),
        counts(),
    )
    return Case(
        seeded=draw(st.sampled_from(SEEDINGS)),
        scenario=draw(draw(st.sampled_from(scenarios))),
    )


# --- examples written out ----------------------------------------------------------------


def stock_line(material: str, qty: str, *, boxes: bool = False) -> Moved:
    """One line of a stock bill written out, in the base unit unless ``boxes`` says so."""
    assert not boxes or material == BOLT, "only the bolt has a conversion record"
    return Moved(material=material, qty=Decimal(qty), in_boxes=boxes)


def order_line(material: str, qty: str, *, boxes: bool = False) -> Ordered:
    """One line of the source order written out, in the base unit unless ``boxes`` says so."""
    assert not boxes or material == BOLT, "only the bolt has a conversion record"
    return Ordered(material=material, qty=Decimal(qty), in_boxes=boxes)


def entry_case(
    order: Sequence[Ordered],
    take: Sequence[int],
    *,
    sibling: Sequence[int] | None = None,
    depots: Sequence[str] | None = None,
    closed: bool = False,
    seeded: tuple[bool, ...] = NO_PLACE,
) -> Case:
    """One pushed goods entry written out; every line receives into the main depot."""
    return Case(
        seeded=seeded,
        scenario=Entry(
            order=tuple(order),
            sibling=None if sibling is None else tuple(sibling),
            take=tuple(take),
            depots=tuple(depots) if depots is not None else (MAIN_DEPOT,) * len(order),
            closed=closed,
        ),
    )


def slip_case(take: int, *, sibling: int | None = None) -> Case:
    """One pushed pay slip written out, off a fee note of 40 at 1.5 (requirement 8.6)."""
    return Case(
        seeded=NO_PLACE,
        scenario=Slip(qty=Decimal("40"), price=Decimal("1.5"), sibling=sibling, take=take),
    )


def fee_case() -> Case:
    """One fee note of its own written out, a simple bill of 40 at 1.5 (requirement 7.2)."""
    return Case(seeded=NO_PLACE, scenario=Fee(qty=Decimal("40"), price=Decimal("1.5")))


def stock_case(scenario: Issue | Move | Count, seeded: tuple[bool, ...]) -> Case:
    """One bill that only moves stock, written out over the stock pattern ``seeded``."""
    return Case(seeded=seeded, scenario=scenario)


# --- running one example -----------------------------------------------------------------


@pytest.fixture(scope="module")
def sandbox_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandbox of each example is created below."""
    return tmp_path_factory.mktemp("p24-sandboxes")


@contextmanager
def sandbox(root: Path) -> Iterator[EngineEnv]:
    """An empty sandbox with the standard basic data, removed again when the block returns.

    One per example, because the balance records of requirement 9.10 are the point: a pair
    that got a record from an earlier example would never be a pair the approve of this one
    creates one for.
    """
    # A space in the directory name: the sandbox database is read through a URI (design 3.4).
    sandbox_dir = Path(mkdtemp(dir=root, prefix="sb "))
    try:
        with engine_env(sandbox_dir) as env:
            yield env
    finally:
        rmtree(sandbox_dir, ignore_errors=True)


def refused(env: EngineEnv, prepared: Prepared) -> bool:
    """Approve the bill under test; True when R-GEN-007 refused it (requirement 9.4).

    A bill that would leave a (material, warehouse) below 0 is not approvable, and the
    property says nothing about it; any other error ends the example, since the drawn bills
    break no other rule - every reference is there and enabled, the quota of both pushes is
    met and every required field is written.
    """
    try:
        env.transition(prepared.bill, prepared.bill_id, "approve")
    except ErpError as error:
        assert (error.code, error.rule_id) == (
            ErrorCode.BUSINESS_RULE_VIOLATION,
            NEGATIVE_STOCK_RULE,
        ), f"the approve ended in {error.code} {error.rule_id}: {error.message}"
        return True
    return False


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 24: 审核与反审核往返
@pytest.mark.req("7.6", "7.7", "8.8", "8.9", "8.13", "8.14", "9.6", "9.10")
@settings(max_examples=100)
@given(case=cases())
# A source line linked in full by the entry under test: the source order is closed
# automatically and the unapprove reopens it, and the link field goes back to empty
# (requirements 8.13 and 8.14).
@example(case=entry_case([order_line(WASHER, "4")], [QUARTERS]))
# The other entry links the source line in full and is approved first, so the order is
# already closed when the entry under test is approved and stays closed afterwards: another
# bill still links it in full (requirement 8.14).
@example(case=entry_case([order_line(WASHER, "4")], [0], sibling=[QUARTERS]))
# Half each: the entry under test completes the line, so its approve closes the order and its
# unapprove reopens it with the other entry's half still linked (requirements 8.9 and 8.14).
@example(case=entry_case([order_line(WASHER, "4")], [2], sibling=[2]))
# The close operation closed the source order first, so no writeback closes it again and the
# unapprove leaves it closed, the close mode manual (requirements 8.13 and 8.14).
@example(case=entry_case([order_line(WASHER, "4")], [QUARTERS], closed=True))
# One source line linked in full and one only partly, so the order stays approved (8.13).
@example(case=entry_case([order_line(WASHER, "4"), order_line(WASHER, "2")], [QUARTERS, 2]))
# A source line ordered in boxes: the amount written back is the base unit quantity of the
# boxes received, and the two warehouses each get a balance record of their own (6.6, 9.10).
@example(
    case=entry_case(
        [order_line(BOLT, "2", boxes=True), order_line(BOLT, "1", boxes=True)],
        [QUARTERS, 1],
        depots=[MAIN_DEPOT, SPARE_DEPOT],
    )
)
# The same entry over stock that is already on hand: every balance record it touches is there
# before the approve and has to be back at its own quantity afterwards (requirement 9.6).
@example(case=entry_case([order_line(WASHER, "4")], [QUARTERS], seeded=EVERY_PLACE))
# A pay slip for the whole amount of its fee note: the link field of the source header goes
# from empty to that amount and back to empty (requirements 8.8 and 8.9).
@example(case=slip_case(QUARTERS))
# A pay slip for part of it, with another slip approved first: the unapprove takes off its
# own amount alone (requirement 8.9).
@example(case=slip_case(2, sibling=2))
# A pay slip of nothing at all: the writeback adds an amount of 0, which the unapprove takes
# off again and leaves the link field empty (requirement 8.9).
@example(case=slip_case(0))
# A fee note of its own, the one simple bill under test: no linkage and no writeback, so the
# round trip leaves its state behind as well and only its ``updated_seq`` and the counters
# are different (requirements 7.2, 7.6 and 7.7).
@example(case=fee_case())
# An issue note out of stock that is on hand, two lines of one material: the balance record
# survives the round trip at the quantity it held (requirements 9.6 and 9.10).
@example(
    case=stock_case(
        Issue(depot=MAIN_DEPOT, lines=(stock_line(BOLT, "2"), stock_line(BOLT, "1"))),
        EVERY_PLACE,
    )
)
# The same issue note with nothing on hand: the approve is refused over R-GEN-007 and
# nothing moved at all (requirement 9.4).
@example(case=stock_case(Issue(depot=MAIN_DEPOT, lines=(stock_line(BOLT, "2"),)), NO_PLACE))
# A transfer out of a warehouse that holds stock into one that holds none: the approve adds a
# balance record on the arriving pair and takes stock off the record of the leaving one, and
# the unapprove has to delete the first and restore the second (requirements 9.6 and 9.10).
@example(
    case=stock_case(
        Move(
            depot=MAIN_DEPOT,
            target=SPARE_DEPOT,
            lines=(stock_line(BOLT, "1", boxes=True), stock_line(WASHER, "2")),
        ),
        SEEDINGS[1],
    )
)
# A count sheet writing a gain onto pairs that have no balance record: both records are
# created by the approve and deleted by the unapprove (requirements 9.2 and 9.10).
@example(
    case=stock_case(
        Count(
            depot=SPARE_DEPOT,
            gain=True,
            lines=(stock_line(BOLT, "0.5"), stock_line(WASHER, "0")),
        ),
        NO_PLACE,
    )
)
# A count sheet writing a loss off stock that is on hand (requirement 9.2).
@example(
    case=stock_case(
        Count(depot=MAIN_DEPOT, gain=False, lines=(stock_line(WASHER, "2"),)), EVERY_PLACE
    )
)
def test_an_approve_and_an_unapprove_leave_the_sandbox_as_they_found_it(
    sandbox_root: Path, case: Case
) -> None:
    with sandbox(sandbox_root) as env:
        prepared = prepare(env, case)
        note(f"the bill under test: {prepared}")
        before, counters = content(env), env.counters()
        assert status_of(env, prepared) == prepared.pre

        if refused(env, prepared):
            event("the bill is not approvable")
            # Not an approvable bill after all: R-GEN-007 rolls the whole operation back, so
            # nothing moved and no counter did either (requirement 9.4).
            assert content(env) == before
            assert env.counters() == counters
            return
        event(f"the round trip of a {prepared.bill}")

        # The approve wrote what its linkage and its writebacks call for, so the round trip
        # below cannot hold by both halves of it doing nothing (requirements 9.1 and 8.8).
        assert status_of(env, prepared) == APPROVED
        assert len(moves_of(env, prepared.bill_id)) == prepared.moves
        assert len(amounts_of(env, prepared.bill_id)) == prepared.amounts
        # Every (material, warehouse) that has a transaction has a balance record (9.10).
        balanced = places_of(env.records(STOCK_BALANCE_TABLE))
        assert places_of(env.records(STOCK_TXN_TABLE)) <= balanced
        note(
            f"balance records the approve created: {balanced - places_of(before[STOCK_BALANCE_TABLE])}"
        )

        env.transition(prepared.bill, prepared.bill_id, "unapprove")

        # The whole sandbox is back where it was: the stock transactions and the stock on
        # hand, a balance record the approve created included, the link fields of the source
        # bill and its state and close mode, the writeback ledger and every record the two
        # operations had no business with (requirements 7.6, 7.7, 8.9, 8.14, 9.6 and 9.10).
        exempt = exempted(prepared)
        assert without(content(env), exempt) == without(before, exempt)
        # The columns that do not come back: the state of the bill, which the standard
        # machine leaves in draft (requirement 7.2), and the operation sequence counter,
        # which the two write operations each took the next of (requirement 4.5).
        after = env.counters()
        assert status_of(env, prepared) == DRAFT
        assert but_operations(after) == but_operations(counters)
        assert operations(after) == operations(counters) + 2
