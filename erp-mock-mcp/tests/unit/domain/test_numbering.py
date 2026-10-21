"""erp_mock.domain.numbering: the identifiers and the counters of a sandbox (requirement 4).

The tests run against a real sandbox database over the schema of the engine test fixture
(tests.helpers.fixtures), so the counters are read and raised exactly as an operation does
it. The ``store`` fixture is an empty sandbox - every counter is 0, as at the start of a
seed build (requirement 4.6) - and the allocation functions are called directly in a
transaction of it: the write operations that call them from the ``candidate`` slot arrive
with the crud and conversion primitives (design.md 4.2). What the tests pin:

- the four forms: an internal ID padded to six digits, a line ID of bill ID and line
  number, a code padded to the digits of its rule, and a bill number of prefix, business
  date and four digits, each keeping the digits it needs where the sequence outgrows the
  padding (requirements 4.1 to 4.4);
- which counter a sequence comes from: one per entity and bill type for IDs and codes, one
  per (number prefix, date) for bill numbers, one for the whole sandbox for the operation
  sequence numbers (requirements 4.1, 4.3, 4.4, 4.6 and 4.11);
- a sequence is handed out once: the ID and the number of a bill that was deleted are not
  assigned again (requirement 4.1, 4.4, INV-6);
- an operation that ends in an error leaves every counter where it was, so the next
  successful one gets the IDs, the number and the sequence number it would have got had the
  failed one never run (requirement 4.12);
- the operation sequence number goes into ``created_seq`` and ``updated_seq`` of a created
  bill and into ``updated_seq`` of a changed one, and a read-only operation of the engine
  takes no such number at all (requirements 4.5 and 4.11).
"""

from collections.abc import Iterator, Mapping
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.config.model import BillModel, DomainModel, EntityModel
from erp_mock.domain.engine import (
    BusinessContextOp,
    CreateOp,
    DeleteOp,
    Engine,
    GetOp,
    Operation,
    OperationContext,
    PushOp,
    QueryOp,
    SearchOp,
    TransitionOp,
    UpdateOp,
    is_write_operation,
)
from erp_mock.domain.numbering import (
    CREATED_SEQ,
    OP_COUNTER_KEY,
    UPDATED_SEQ,
    SequenceStamps,
    assign_operation_seq,
    format_code,
    format_id,
    format_number,
    line_id,
    line_ids,
    line_numbers,
    next_code,
    next_id,
    next_number,
    next_operation_seq,
    number_key,
)
from erp_mock.state.schema import COUNTERS_TABLE, StorageSchema, build_schema
from erp_mock.state.store import Record, SandboxStore, Value
from tests.helpers.fixtures import ANCHOR_DATE, fixture_domain

# The three bills of the fixture these tests number: a pricing bill with a standard state
# machine, the bill a push generates from it, and a third one that no operation touches.
ORDER: Final = "supply_order"
ENTRY: Final = "goods_entry"
SHEET: Final = "count_sheet"

# The day after the anchor date, so a second (prefix, date) counter comes into play.
NEXT_DAY: Final = date(2026, 10, 10)

# The bills the ``seeded`` fixture starts from: an approved order that two earlier
# operations created and changed, a draft entry a write has just inserted without its
# sequence numbers, and a count sheet that stays as it is.
SEEDED: Final[tuple[tuple[str, Mapping[str, Value]], ...]] = (
    (
        ORDER,
        {
            "id": "so_000001",
            "number": "SPO202610090001",
            "bill_type": ORDER,
            "biz_date": ANCHOR_DATE,
            "status": "approved",
            CREATED_SEQ: 1,
            UPDATED_SEQ: 2,
        },
    ),
    (
        ENTRY,
        {
            "id": "ge_000001",
            "number": "GRN202610090001",
            "bill_type": ENTRY,
            "biz_date": ANCHOR_DATE,
            "status": "draft",
        },
    ),
    (
        SHEET,
        {
            "id": "cs_000001",
            "number": "CNT202610090001",
            "bill_type": SHEET,
            "biz_date": ANCHOR_DATE,
            "status": "draft",
            CREATED_SEQ: 3,
            UPDATED_SEQ: 3,
        },
    ),
)

# How many write operations the seeded sandbox has behind it: the three the sequence
# numbers of :data:`SEEDED` name, so the next one is number 4.
SEEDED_OPERATIONS: Final = 3


@pytest.fixture(scope="module")
def domain() -> DomainModel:
    return fixture_domain()


@pytest.fixture(scope="module")
def schema(domain: DomainModel) -> StorageSchema:
    return build_schema(domain)


@pytest.fixture(scope="module")
def engine(domain: DomainModel) -> Engine:
    return Engine(domain)


@pytest.fixture
def store(tmp_path: Path, schema: StorageSchema) -> Iterator[SandboxStore]:
    """An empty sandbox with the tables of the fixture: every counter is 0 (req 4.6)."""
    store = SandboxStore(tmp_path / "sandbox", schema)
    store.open()
    with closing(store):
        store.create_tables()
        yield store


@pytest.fixture
def seeded(store: SandboxStore) -> SandboxStore:
    """The sandbox with the three bills of :data:`SEEDED` and the counters they imply."""
    with store.transaction() as uow:
        for table, values in SEEDED:
            uow.insert(table, values)
        for _ in range(SEEDED_OPERATIONS):
            next_operation_seq(uow)
    return store


@pytest.fixture
def order(domain: DomainModel) -> BillModel:
    """The supply order: ID prefix ``so``, number prefix ``SPO`` (tests/fixtures)."""
    return domain.bills[ORDER]


@pytest.fixture
def entry(domain: DomainModel) -> BillModel:
    """The goods entry: ID prefix ``ge``, number prefix ``GRN``."""
    return domain.bills[ENTRY]


@pytest.fixture
def vendor(domain: DomainModel) -> EntityModel:
    """The partner entity: ID prefix ``ven``, code rule prefix ``V`` with 4 digits."""
    return domain.entities["vendor"]


@pytest.fixture
def article(domain: DomainModel) -> EntityModel:
    """The material entity: ID prefix ``art``, code rule prefix ``A`` with 5 digits."""
    return domain.entities["article"]


@pytest.fixture
def measure(domain: DomainModel) -> EntityModel:
    """The unit entity, whose codes come from its shared data rather than from a rule."""
    return domain.entities["measure"]


def counters(store: SandboxStore) -> dict[tuple[str, str], int]:
    """The counters the sandbox holds, keyed by (kind, key) (requirement 4.6)."""
    with store.transaction() as uow:
        rows = uow.select(COUNTERS_TABLE)
    return {(str(row["kind"]), str(row["key"])): int(str(row["value"])) for row in rows}


def bill_record(store: SandboxStore, table: str, bill_id: str) -> Record:
    """The record ``bill_id`` of ``table``; the tests only read bills they seeded."""
    with store.transaction() as uow:
        record = uow.get(table, bill_id)
    assert record is not None
    return record


def sequence_numbers(store: SandboxStore, table: str, bill_id: str) -> tuple[Value, Value]:
    """The ``created_seq`` and ``updated_seq`` of one bill (requirement 4.5)."""
    record = bill_record(store, table, bill_id)
    return (record[CREATED_SEQ], record[UPDATED_SEQ])


# --- internal IDs (requirement 4.1) ------------------------------------------------------


@pytest.mark.req("4.1", "4.6")
def test_an_internal_id_is_the_prefix_and_a_six_digit_sequence(
    store: SandboxStore, order: BillModel, vendor: EntityModel
) -> None:
    assert counters(store) == {}

    with store.transaction() as uow:
        ids = [next_id(uow, order) for _ in range(3)]
        first_vendor = next_id(uow, vendor)

    # The counter of a type starts at 0, so its first record is number 1, and two records
    # of one type differ by 1 (requirement 4.1).
    assert ids == ["so_000001", "so_000002", "so_000003"]
    # Every entity and bill type counts on its own (design.md 4.2).
    assert first_vendor == "ven_000001"
    assert counters(store) == {("id", ORDER): 3, ("id", "vendor"): 1}


@pytest.mark.req("4.1")
def test_an_internal_id_keeps_the_digits_it_needs_beyond_six() -> None:
    assert format_id("so", 1) == "so_000001"
    assert format_id("so", 999999) == "so_999999"
    # Padding is a minimum, not a limit (erp_mock.config.instance.INTERNAL_ID_PATTERN).
    assert format_id("so", 1000000) == "so_1000000"


# --- line IDs (requirement 4.2) ----------------------------------------------------------


@pytest.mark.req("4.2")
def test_a_line_id_is_the_bill_id_and_the_line_number() -> None:
    assert line_id("so_000001", 1) == "so_000001-1"
    # The line number carries no leading zeros, the sequence of the bill ID does.
    assert line_id("so_000001", 12) == "so_000001-12"


@pytest.mark.req("4.2")
def test_the_line_numbers_of_a_bill_run_from_one_without_a_gap() -> None:
    assert list(line_numbers(3)) == [1, 2, 3]
    assert line_ids("so_000001", 3) == ("so_000001-1", "so_000001-2", "so_000001-3")
    # A bill without lines gets no line ID, and no counter is involved either way.
    assert line_ids("so_000001", 0) == ()


# --- codes (requirement 4.3) -------------------------------------------------------------


@pytest.mark.req("4.3")
def test_a_code_is_the_prefix_and_the_sequence_padded_to_its_digits(
    store: SandboxStore, vendor: EntityModel, article: EntityModel
) -> None:
    with store.transaction() as uow:
        codes = [next_code(uow, vendor) for _ in range(107)]
        first_article = next_code(uow, article)

    assert codes[:2] == ["V0001", "V0002"]
    # Requirement 4.3's example with the fixture's own prefix: with 4 digits, the 107th
    # code of the rule is <prefix>0107.
    assert codes[-1] == "V0107"
    # Another entity has a code counter and a rule of its own, here 5 digits.
    assert first_article == "A00001"
    assert counters(store) == {("code", "vendor"): 107, ("code", "article"): 1}


@pytest.mark.req("4.3")
def test_a_code_keeps_the_digits_it_needs_beyond_its_rule() -> None:
    assert format_code("S", 4, 107) == "S0107"
    assert format_code("S", 4, 9999) == "S9999"
    # Beyond the declared digits the sequence is written as it is (requirement 4.3).
    assert format_code("S", 4, 12345) == "S12345"


@pytest.mark.req("4.3")
def test_an_entity_without_a_code_rule_gets_no_code(
    store: SandboxStore, measure: EntityModel
) -> None:
    with store.transaction() as uow:
        code = next_code(uow, measure)

    assert code is None
    # No code was assigned, so no counter of that entity was raised either.
    assert counters(store) == {}


# --- bill numbers (requirement 4.4) ------------------------------------------------------


@pytest.mark.req("4.4")
def test_a_bill_number_is_the_prefix_the_business_date_and_four_digits(
    store: SandboxStore, order: BillModel
) -> None:
    with store.transaction() as uow:
        numbers = [next_number(uow, order, ANCHOR_DATE) for _ in range(2)]

    assert numbers == ["SPO202610090001", "SPO202610090002"]
    assert format_number("CGDD", date(2026, 10, 9), 1) == "CGDD202610090001"
    # A sequence beyond four digits keeps the digits it needs.
    assert format_number("SPO", ANCHOR_DATE, 12345) == "SPO2026100912345"


@pytest.mark.req("4.4", "4.6")
def test_a_bill_number_counts_per_prefix_and_date(
    store: SandboxStore, order: BillModel, entry: BillModel
) -> None:
    with store.transaction() as uow:
        first = next_number(uow, order, ANCHOR_DATE)
        same_day = next_number(uow, order, ANCHOR_DATE)
        other_day = next_number(uow, order, NEXT_DAY)
        other_prefix = next_number(uow, entry, ANCHOR_DATE)

    assert (first, same_day) == ("SPO202610090001", "SPO202610090002")
    # The first bill of each (prefix, date) is 0001 (requirement 4.4).
    assert other_day == "SPO202610100001"
    assert other_prefix == "GRN202610090001"
    assert counters(store) == {
        ("number", number_key("SPO", ANCHOR_DATE)): 2,
        ("number", number_key("SPO", NEXT_DAY)): 1,
        ("number", number_key("GRN", ANCHOR_DATE)): 1,
    }
    # The key of a number counter is the prefix and the date, not the bill type.
    assert number_key("SPO", ANCHOR_DATE) == "SPO|20261009"


@pytest.mark.req("4.1", "4.4")
def test_the_id_and_the_number_of_a_deleted_bill_are_not_assigned_again(
    store: SandboxStore, entry: BillModel
) -> None:
    with store.transaction() as uow:
        bill_id = next_id(uow, entry)
        number = next_number(uow, entry, ANCHOR_DATE)
        uow.insert(ENTRY, {"id": bill_id, "number": number})
        uow.delete(ENTRY, bill_id)

        # The counters do not look at the records, so the sequences the deleted bill used
        # are gone for good (INV-6).
        assert next_id(uow, entry) == "ge_000002"
        assert next_number(uow, entry, ANCHOR_DATE) == "GRN202610090002"

    assert (bill_id, number) == ("ge_000001", "GRN202610090001")


# --- failed operations (requirement 4.12) ------------------------------------------------


@pytest.mark.req("4.12")
def test_a_failed_operation_leaves_every_counter_as_it_was(
    store: SandboxStore, order: BillModel, vendor: EntityModel
) -> None:
    with pytest.raises(ErpError), store.transaction() as uow:
        next_id(uow, order)
        next_code(uow, vendor)
        next_number(uow, order, ANCHOR_DATE)
        next_operation_seq(uow)
        raise ErpError(ErrorCode.VALIDATION_FAILED, "本次操作失败")

    # The transaction rolled back, counters included (requirement 4.12).
    assert counters(store) == {}
    with store.transaction() as uow:
        # The next successful operation gets what it would have got had the failed one
        # never run.
        assert next_id(uow, order) == "so_000001"
        assert next_code(uow, vendor) == "V0001"
        assert next_number(uow, order, ANCHOR_DATE) == "SPO202610090001"
        assert next_operation_seq(uow) == 1


# --- operation sequence numbers (requirements 4.5 and 4.11) ------------------------------


@pytest.mark.req("4.11")
def test_every_write_operation_takes_the_next_operation_sequence_number(
    store: SandboxStore,
) -> None:
    with store.transaction() as uow:
        seqs = [next_operation_seq(uow) for _ in range(3)]

    assert seqs == [1, 2, 3]
    # One counter for the whole sandbox, under a single key (design.md 4.2).
    assert counters(store) == {("op", OP_COUNTER_KEY): 3}


@pytest.mark.req("4.11")
def test_the_write_operations_are_the_ones_that_take_a_sequence_number() -> None:
    writes: tuple[Operation, ...] = (
        CreateOp("vendor"),
        UpdateOp(ORDER, "so_000001"),
        DeleteOp(ORDER, "so_000001"),
        TransitionOp(ORDER, "so_000001", "approve"),
        PushOp("supply_order_to_entry", "so_000001"),
    )
    reads: tuple[Operation, ...] = (
        GetOp(ORDER, "so_000001"),
        SearchOp(ORDER),
        QueryOp("stock_balance_query"),
        BusinessContextOp(),
    )

    assert [is_write_operation(op) for op in writes] == [True] * len(writes)
    assert [is_write_operation(op) for op in reads] == [False] * len(reads)


@pytest.mark.req("4.5", "4.11")
def test_the_operation_sequence_number_is_written_to_the_bills_it_concerns(
    seeded: SandboxStore,
) -> None:
    stamps = SequenceStamps()
    stamps.created_bill(ENTRY, "ge_000001")
    stamps.changed_bill(ORDER, "so_000001")

    with seeded.transaction() as uow:
        seq = assign_operation_seq(uow, stamps)

    assert seq == SEEDED_OPERATIONS + 1
    # A bill the operation created carries the number in both fields.
    assert sequence_numbers(seeded, ENTRY, "ge_000001") == (4, 4)
    # A bill it changed keeps the number of the operation that created it (req 4.5).
    assert sequence_numbers(seeded, ORDER, "so_000001") == (1, 4)
    # A bill it does not touch keeps both numbers (requirements 4.5 and 8.12).
    assert sequence_numbers(seeded, SHEET, "cs_000001") == (3, 3)


@pytest.mark.req("4.5")
def test_a_created_bill_is_not_stamped_as_a_changed_one_as_well(seeded: SandboxStore) -> None:
    stamps = SequenceStamps()
    # A pushed bill is created by the operation and is also the bill it works on; both
    # fields carry the number of this operation (requirement 4.5).
    stamps.changed_bill(ENTRY, "ge_000001")
    stamps.created_bill(ENTRY, "ge_000001")

    with seeded.transaction() as uow:
        assign_operation_seq(uow, stamps)

    assert stamps.changed == []
    assert sequence_numbers(seeded, ENTRY, "ge_000001") == (4, 4)


@pytest.mark.req("4.11")
def test_a_read_only_operation_takes_no_operation_sequence_number(
    engine: Engine, seeded: SandboxStore
) -> None:
    ctx = OperationContext(anchor_date=ANCHOR_DATE, call_seq=7)

    engine.run(seeded, ctx, GetOp(ORDER, "so_000001"))
    engine.run(seeded, ctx, BusinessContextOp())

    # The reads raised no counter, so the next write operation is still number 4
    # (requirement 4.11), and no bill was stamped.
    assert counters(seeded) == {("op", OP_COUNTER_KEY): SEEDED_OPERATIONS}
    assert sequence_numbers(seeded, ORDER, "so_000001") == (1, 2)
    with seeded.transaction() as uow:
        assert next_operation_seq(uow) == SEEDED_OPERATIONS + 1
