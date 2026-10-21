"""erp_mock.state.diff: the net change of a sandbox against a basis.

Every test works on real files below ``tmp_path`` and on the schema of the engine test
fixture (tests.helpers.fixtures). The basis is a finished Snapshot, built the way the
Snapshot cache lays one out, and the sandbox is a copy of it with ``base.sqlite`` linked
beside it, so the diff reads what it reads in production: an open WAL database against an
immutable snapshot file. The content of both sides is written by hand through the unit of
work, one transaction per call, so each test decides exactly what changed.

What the tests pin:

- a sandbox that holds what the basis holds gives a diff whose ``changes`` is empty and
  whose other keys are there as usual (requirements 18.1 and 18.7);
- a record only the sandbox has is an insert and one only the basis has a delete, each
  listing every non-internal field but ``id``; a record both sides hold is an update of
  the fields whose values differ, with the same keys in ``before`` and ``after``
  (requirements 18.2 and 18.9);
- values are written as their field type: a decimal as its canonical text, an int as a
  JSON integer, a bool as a JSON boolean, a date as ``YYYY-MM-DD``, an enum as its code,
  a reference as the internal ID and an absent value as null (requirement 18.5);
- the diff covers the entity, bill, line and inventory tables, keyed by the ids the
  engine builds for stock_txn and stock_balance, and leaves the internal columns and the
  internal tables out (requirement 18.4); ``changes`` is ordered by entity and then by id
  (requirement 18.6);
- a record created and deleted again, one changed back to its original value and one
  whose decimal was rewritten with other text are no changes at all (requirement 18.2);
- ``cause_seqs`` holds the calls that belong to only one of the two sides, in ascending
  order (requirement 18.3).
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import DomainModel
from erp_mock.state.diff import INITIAL_BASE, DiffBody, compute_diff
from erp_mock.state.schema import (
    BILL_ID_COLUMN,
    CLOSE_MODE_COLUMN,
    STOCK_BALANCE_TABLE,
    STOCK_TXN_TABLE,
    StorageSchema,
    build_schema,
)
from erp_mock.state.snapshot import (
    BASE_DB_FILENAME,
    build_snapshot_db,
    copy_snapshot,
    finalize_snapshot,
    link_snapshot,
)
from erp_mock.state.store import DB_FILENAME, SandboxStore, UnitOfWork, Value
from tests.helpers.fixtures import ANCHOR_DATE, fixture_domain

# The tables of the fixture the tests write to: an entity, a bill and its lines.
VENDOR: Final = "vendor"
ORDER_TABLE: Final = "supply_order"
LINE_TABLE: Final = "supply_order_line"

# The non-internal fields of a vendor record without ``id``, by name in code point order:
# what an insert or a delete of one lists (requirements 18.6 and 18.9).
VENDOR_FIELDS: Final = (
    "code",
    "contact",
    "default_tax_rate",
    "name",
    "pay_days",
    "short_name",
    "status",
)

# The id of a stock movement, <source line id>:<warehouse id>:<direction>, and of an
# inventory balance, <material id>:<warehouse id> (requirements 18.8 and 18.4).
ENTRY_TXN_ID: Final = "ge_000001-1:dep_000001:in"
BALANCE_ID: Final = "art_000031:dep_000001"

# What the basis holds: three vendors, an approved order with one line, and the inventory
# of the article that line received. An initial dataset has no effective call
# (requirement 18.10), so the build below records none.
BASE_CONTENT: Final[tuple[tuple[str, Mapping[str, Value]], ...]] = (
    (
        VENDOR,
        {
            "id": "ven_000001",
            "code": "V0001",
            "status": "enabled",
            "name": "一号供应商",
            "short_name": "一号",
            "pay_days": 30,
            "default_tax_rate": Decimal("0.13"),
        },
    ),
    (
        VENDOR,
        {
            "id": "ven_000002",
            "code": "V0002",
            "status": "enabled",
            "name": "二号供应商",
            "contact": "乙",
            "pay_days": 60,
            "default_tax_rate": Decimal("0.06"),
        },
    ),
    (
        VENDOR,
        {
            "id": "ven_000003",
            "code": "V0003",
            "status": "disabled",
            "name": "三号供应商",
            "pay_days": 0,
            "default_tax_rate": Decimal("0"),
        },
    ),
    (
        ORDER_TABLE,
        {
            "id": "so_000001",
            "number": "SUP202610090001",
            "bill_type": ORDER_TABLE,
            "org_id": "br_000001",
            "biz_date": date(2026, 10, 9),
            "status": "approved",
            "created_seq": 1,
            "updated_seq": 1,
            "price_includes_tax": False,
            "amount": Decimal("1700.00"),
            "vendor_id": "ven_000001",
        },
    ),
    (
        LINE_TABLE,
        {
            "id": "so_000001-1",
            "line_no": 1,
            "qty": Decimal("2000"),
            "price": Decimal("0.85"),
            "amount": Decimal("1700.00"),
            "article_id": "art_000031",
            BILL_ID_COLUMN: "so_000001",
        },
    ),
    (
        STOCK_BALANCE_TABLE,
        {
            "id": BALANCE_ID,
            "material_id": "art_000031",
            "warehouse_id": "dep_000001",
            "qty_base": Decimal("300"),
        },
    ),
    (
        STOCK_TXN_TABLE,
        {
            "id": ENTRY_TXN_ID,
            "material_id": "art_000031",
            "warehouse_id": "dep_000001",
            "qty_delta": Decimal("300"),
            "source_bill_type": "goods_entry",
            "source_line_id": "ge_000001-1",
        },
    ),
)


@pytest.fixture(scope="module")
def domain() -> DomainModel:
    return fixture_domain()


@pytest.fixture(scope="module")
def schema(domain: DomainModel) -> StorageSchema:
    return build_schema(domain)


@pytest.fixture
def snapshot(tmp_path: Path, schema: StorageSchema) -> Path:
    """The Snapshot the sandbox is created from: the basis of an ``initial`` diff."""
    build_db = tmp_path / "build" / "build.sqlite"
    store = build_snapshot_db(build_db, schema)
    try:
        with store.transaction() as uow:
            for table, record in BASE_CONTENT:
                uow.insert(table, record)
    finally:
        store.close()
    out = tmp_path / "snapshots" / "fx-basic@1.0.0" / "2026-10-09" / "snapshot.sqlite"
    finalize_snapshot(build_db, out)
    return out


@pytest.fixture
def sandbox(tmp_path: Path, schema: StorageSchema, snapshot: Path) -> Iterator[SandboxStore]:
    """An open sandbox store created from ``snapshot``, with its ``base.sqlite`` link."""
    # A space in the directory name: both databases are read through a URI (design.md 3.4).
    sandbox_dir = tmp_path / "sandboxes" / "sb 1"
    copy_snapshot(snapshot, sandbox_dir / DB_FILENAME)
    link_snapshot(snapshot, sandbox_dir / BASE_DB_FILENAME)
    store = SandboxStore(sandbox_dir, schema)
    store.open()
    try:
        yield store
    finally:
        store.close()


# --- helpers -------------------------------------------------------------------------------


def call(store: SandboxStore, seq: int, writes: Callable[[UnitOfWork], None]) -> None:
    """Run ``writes`` as one call and record it as the effective call ``seq`` (req 18.10)."""
    with store.transaction() as uow:
        writes(uow)
        uow.record_effective_call(seq, uow.net_writes())


def diff_of(sandbox: SandboxStore, base: Path | None = None, label: str = INITIAL_BASE) -> DiffBody:
    """The diff of ``sandbox`` against ``base``, its own ``base.sqlite`` by default."""
    basis = sandbox.sandbox_dir / BASE_DB_FILENAME if base is None else base
    return compute_diff(sandbox.db_path, basis, sandbox.schema, label)


def listed(body: DiffBody) -> list[tuple[str, str, str]]:
    """Every change as (entity, id, op), in the order the diff reports them."""
    return [(change.entity, change.id, change.op) for change in body.changes]


def causes(body: DiffBody) -> list[tuple[str, Sequence[int]]]:
    """Every change as (id, cause_seqs), in the order the diff reports them."""
    return [(change.id, change.cause_seqs) for change in body.changes]


# --- no net change -------------------------------------------------------------------------


@pytest.mark.req("18.1", "18.7")
def test_a_sandbox_that_holds_what_the_basis_holds_has_an_empty_diff(
    sandbox: SandboxStore,
) -> None:
    body = diff_of(sandbox)

    assert body == DiffBody(base=INITIAL_BASE, changes=())
    document = body.as_json(
        sandbox_id="sb_3f9c1e7a0b2d4c6e",
        instance="fx-demo@1.0.0",
        dataset="fx-basic@1.0.0",
        anchor_date=ANCHOR_DATE,
    )
    # The keys of a diff, in order, with changes as an empty array (requirement 18.7).
    assert list(document) == [
        "sandbox_id",
        "instance",
        "dataset",
        "anchor_date",
        "base",
        "changes",
    ]
    assert document["anchor_date"] == "2026-10-09"
    assert document["base"] == INITIAL_BASE
    assert document["changes"] == []

    # An idle sandbox, whose store is closed and whose WAL files are gone, reads the same:
    # the reader is a plain connection and creates them again (V8).
    basis = sandbox.sandbox_dir / BASE_DB_FILENAME
    before = basis.read_bytes()
    sandbox.close()
    assert diff_of(sandbox) == body
    # The basis is read through an immutable URI, so its bytes stay and it gets no
    # sidecar file (requirement 15.13).
    assert basis.read_bytes() == before
    assert sorted(path.name for path in basis.parent.iterdir()) == [
        BASE_DB_FILENAME,
        DB_FILENAME,
    ]


@pytest.mark.req("18.2")
def test_records_created_and_deleted_again_or_changed_back_are_no_changes(
    sandbox: SandboxStore,
) -> None:
    call(sandbox, 1, lambda uow: uow.insert(VENDOR, {"id": "ven_000009", "name": "临时"}))
    call(sandbox, 2, lambda uow: uow.delete(VENDOR, "ven_000009"))
    call(sandbox, 3, lambda uow: uow.update(VENDOR, "ven_000002", {"contact": "丙"}))
    call(sandbox, 4, lambda uow: uow.update(VENDOR, "ven_000002", {"contact": "乙"}))
    # The same amount written with other text: equal decimals have equal canonical text,
    # so this is no change either (design.md 3.1).
    call(sandbox, 5, lambda uow: uow.update(ORDER_TABLE, "so_000001", {"amount": Decimal("1700")}))

    assert diff_of(sandbox).changes == ()


@pytest.mark.req("18.4")
def test_internal_columns_and_internal_tables_stay_out_of_the_diff(
    sandbox: SandboxStore,
) -> None:
    def internal_writes(uow: UnitOfWork) -> None:
        uow.update(ORDER_TABLE, "so_000001", {CLOSE_MODE_COLUMN: "auto"})
        uow.update(LINE_TABLE, "so_000001-1", {BILL_ID_COLUMN: "so_000002"})
        uow.bump_counter("id", VENDOR)

    call(sandbox, 1, internal_writes)

    # The internal columns, the counters and the effective calls themselves are no changes.
    assert diff_of(sandbox).changes == ()

    call(sandbox, 2, lambda uow: uow.update(ORDER_TABLE, "so_000001", {"remark": "补充说明"}))

    # A field of the same record does show, the internal columns still left out.
    (change,) = diff_of(sandbox).changes
    assert (change.entity, change.id, change.op) == (ORDER_TABLE, "so_000001", "update")
    assert change.before == {"remark": None}
    assert change.after == {"remark": "补充说明"}


# --- insert, delete and update -------------------------------------------------------------


@pytest.mark.req("18.2", "18.5", "18.9")
def test_a_record_only_the_sandbox_holds_is_an_insert_with_all_its_fields(
    sandbox: SandboxStore,
) -> None:
    def add_vendor(uow: UnitOfWork) -> None:
        uow.insert(
            VENDOR,
            {
                "id": "ven_000004",
                "code": "V0004",
                "status": "enabled",
                "name": "四号供应商",
                "pay_days": 7,
                "default_tax_rate": Decimal("0.0600"),
            },
        )

    call(sandbox, 1, add_vendor)

    (change,) = diff_of(sandbox).changes
    assert (change.entity, change.id, change.op) == (VENDOR, "ven_000004", "insert")
    assert change.before is None
    assert list(change.as_json()) == ["entity", "op", "id", "cause_seqs", "after"]
    # Every non-internal field but id, an absent one as null, each value written as its
    # field type: a rate as its canonical text, pay_days as a JSON integer, the status as
    # its enum code (requirements 18.5 and 18.9).
    assert change.after == {
        "code": "V0004",
        "contact": None,
        "default_tax_rate": "0.06",
        "name": "四号供应商",
        "pay_days": 7,
        "short_name": None,
        "status": "enabled",
    }
    assert list(change.after or {}) == list(VENDOR_FIELDS)


@pytest.mark.req("18.2", "18.5", "18.9")
def test_a_record_only_the_basis_holds_is_a_delete_with_the_fields_it_had(
    sandbox: SandboxStore,
) -> None:
    call(sandbox, 1, lambda uow: uow.delete(VENDOR, "ven_000003"))

    (change,) = diff_of(sandbox).changes
    assert (change.entity, change.id, change.op) == (VENDOR, "ven_000003", "delete")
    assert change.after is None
    assert list(change.as_json()) == ["entity", "op", "id", "cause_seqs", "before"]
    assert change.before == {
        "code": "V0003",
        "contact": None,
        "default_tax_rate": "0",
        "name": "三号供应商",
        "pay_days": 0,
        "short_name": None,
        "status": "disabled",
    }
    assert list(change.before or {}) == list(VENDOR_FIELDS)


@pytest.mark.req("18.2", "18.5", "18.9")
def test_an_update_lists_only_the_fields_whose_values_differ(sandbox: SandboxStore) -> None:
    def close_order(uow: UnitOfWork) -> None:
        uow.update(
            ORDER_TABLE,
            "so_000001",
            {
                "status": "closed",
                "updated_seq": 2,
                "price_includes_tax": True,
                "need_date": date(2026, 10, 16),
                # Unchanged values, written again: neither shows up in the change.
                "amount": Decimal("1700.000"),
                "vendor_id": "ven_000001",
            },
        )

    call(sandbox, 1, close_order)

    (change,) = diff_of(sandbox).changes
    assert (change.entity, change.id, change.op) == (ORDER_TABLE, "so_000001", "update")
    assert list(change.as_json()) == ["entity", "op", "id", "cause_seqs", "before", "after"]
    # before and after have the same keys, in field name order (requirements 18.6, 18.9);
    # a bool is a JSON boolean, a date YYYY-MM-DD and a field the basis left empty null.
    assert change.before == {
        "need_date": None,
        "price_includes_tax": False,
        "status": "approved",
        "updated_seq": 1,
    }
    assert change.after == {
        "need_date": "2026-10-16",
        "price_includes_tax": True,
        "status": "closed",
        "updated_seq": 2,
    }
    assert list(change.before or {}) == list(change.after or {})


# --- which records the diff covers, and in which order --------------------------------------


@pytest.mark.req("18.4", "18.6")
def test_the_diff_covers_every_record_table_ordered_by_entity_and_id(
    sandbox: SandboxStore,
) -> None:
    def receive_goods(uow: UnitOfWork) -> None:
        # Two vendors in reverse id order, to show the order of the output is not the
        # order of the writes.
        uow.insert(VENDOR, {"id": "ven_000006", "name": "六号供应商"})
        uow.insert(VENDOR, {"id": "ven_000005", "name": "五号供应商"})
        uow.insert(
            LINE_TABLE,
            {
                "id": "so_000001-2",
                "line_no": 2,
                "qty": Decimal("0.5"),
                "article_id": "art_000032",
                BILL_ID_COLUMN: "so_000001",
            },
        )
        uow.insert(
            STOCK_TXN_TABLE,
            {
                "id": "ge_000002-1:dep_000001:in",
                "material_id": "art_000031",
                "warehouse_id": "dep_000001",
                "qty_delta": Decimal("200"),
                "source_bill_type": "goods_entry",
                "source_line_id": "ge_000002-1",
            },
        )
        uow.update(STOCK_BALANCE_TABLE, BALANCE_ID, {"qty_base": Decimal("500")})
        uow.delete(STOCK_TXN_TABLE, ENTRY_TXN_ID)

    call(sandbox, 1, receive_goods)

    body = diff_of(sandbox)

    # The entities sort stock_balance, stock_txn, supply_order_line, vendor, and the two
    # stock movements by their id (requirement 18.6).
    assert listed(body) == [
        (STOCK_BALANCE_TABLE, BALANCE_ID, "update"),
        (STOCK_TXN_TABLE, "ge_000001-1:dep_000001:in", "delete"),
        (STOCK_TXN_TABLE, "ge_000002-1:dep_000001:in", "insert"),
        (LINE_TABLE, "so_000001-2", "insert"),
        (VENDOR, "ven_000005", "insert"),
        (VENDOR, "ven_000006", "insert"),
    ]
    keys = [change.sort_key for change in body.changes]
    assert keys == sorted(keys)
    balance, _deleted, received, line, *_vendors = body.changes
    # The inventory tables are keyed by the ids the engine builds for them, and their
    # columns are the engine's own (requirement 18.4).
    assert balance.before == {"qty_base": "300"}
    assert balance.after == {"qty_base": "500"}
    assert received.after == {
        "material_id": "art_000031",
        "qty_delta": "200",
        "source_bill_type": "goods_entry",
        "source_line_id": "ge_000002-1",
        "warehouse_id": "dep_000001",
    }
    # A line is its own entity, <bill>_line, keyed by the line id; the internal _bill_id
    # is not part of it (requirement 18.4).
    assert line.after == {
        "amount": None,
        "amount_with_tax": None,
        "article_id": "art_000032",
        "line_no": 2,
        "linked_qty": None,
        "measure_id": None,
        "price": None,
        "qty": "0.5",
        "qty_base": None,
        "source_line_id": None,
        "tax_amount": None,
        "tax_rate": None,
    }


# --- cause_seqs -----------------------------------------------------------------------------


@pytest.mark.req("18.3")
def test_cause_seqs_are_the_calls_that_belong_to_only_one_of_the_two_sides(
    sandbox: SandboxStore,
) -> None:
    def raise_term(uow: UnitOfWork) -> None:
        uow.update(VENDOR, "ven_000001", {"pay_days": 60})
        uow.insert(VENDOR, {"id": "ven_000004", "name": "四号供应商"})

    call(sandbox, 1, lambda uow: uow.update(VENDOR, "ven_000001", {"contact": "甲"}))
    named = sandbox.sandbox_dir / "named" / "demo.sqlite"
    sandbox.save_to(named)
    call(sandbox, 2, lambda uow: uow.update(VENDOR, "ven_000001", {"pay_days": 45}))
    call(sandbox, 3, raise_term)

    # Against the initial basis, which has no effective call, every call is on one side
    # only, so each change names the calls that wrote its record, ascending and once.
    initial = diff_of(sandbox)
    assert causes(initial) == [("ven_000001", (1, 2, 3)), ("ven_000004", (3,))]

    # Against the Named_Snapshot, call 1 is on both sides: what it wrote is part of the
    # basis, so it is not a cause and the contact it set is not a change either.
    demo = diff_of(sandbox, named, "demo")
    assert demo.base == "demo"
    assert causes(demo) == [("ven_000001", (2, 3)), ("ven_000004", (3,))]
    changed, _added = demo.changes
    assert changed.before == {"pay_days": 30}
    assert changed.after == {"pay_days": 60}


# --- refused calls --------------------------------------------------------------------------


@pytest.mark.req("18.1")
def test_a_missing_database_or_basis_is_refused(sandbox: SandboxStore, tmp_path: Path) -> None:
    missing = tmp_path / "snapshots" / "missing.sqlite"

    with pytest.raises(FileNotFoundError, match="no such sandbox database"):
        compute_diff(missing, sandbox.sandbox_dir / BASE_DB_FILENAME, sandbox.schema)
    with pytest.raises(FileNotFoundError, match="no such snapshot"):
        compute_diff(sandbox.db_path, missing, sandbox.schema)
    # A refused call creates no database of its own.
    assert not missing.exists()
