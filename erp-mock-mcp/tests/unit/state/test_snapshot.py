"""erp_mock.state.snapshot: the Snapshot files and the database files of a sandbox.

Every test works on real files below ``tmp_path`` and on the schema of the engine test
fixture (tests.helpers.fixtures). What they pin:

- a build database is empty and asserts the pragmas the byte-deterministic output depends
  on, and two builds that reach the same content through different write histories - one
  transaction against dozens, forward against reverse order, records written straight
  against records updated into place, pages freed in between - produce Snapshots that are
  identical byte for byte (requirement 20.13);
- creating a sandbox copies the Snapshot into a database of its own and links it as
  ``base.sqlite``, and neither the copy, the writes that follow nor a reset change the
  Snapshot's bytes (requirements 15.1 and 15.13);
- a reset and a restore replace the whole database: the records, the counters and the
  effective calls all become the ones of the source (requirements 19.4 and 19.6), and a
  Named_Snapshot holds what the sandbox held when it was saved, without changing it
  (requirement 19.5).

The snapshot files are read through an immutable URI, the way the engine reads them, so a
test that compares content never writes to the file it reads.
"""

import errno
import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import DomainModel
from erp_mock.state.schema import (
    BILL_ID_COLUMN,
    COUNTERS_TABLE,
    EFFECTIVE_WRITES_TABLE,
    StorageSchema,
    build_schema,
)
from erp_mock.state.snapshot import (
    BASE_DB_FILENAME,
    PAGE_SIZE,
    STORAGE_FORMAT_VERSION,
    build_snapshot_db,
    copy_snapshot,
    finalize_snapshot,
    link_snapshot,
)
from erp_mock.state.store import (
    DB_FILENAME,
    DIRECT_CAUSE,
    SandboxStore,
    Value,
    Write,
    immutable_uri,
)
from tests.helpers.fixtures import fixture_domain

# The content every build of this test file reaches: three vendors, a supply order with
# two lines, the counters of those records and two effective calls.
VENDOR: Final = "vendor"
ORDER_TABLE: Final = "supply_order"
LINE_TABLE: Final = "supply_order_line"

VENDORS: Final[tuple[Mapping[str, Value], ...]] = (
    {
        "id": "ven_000001",
        "code": "V0001",
        "status": "enabled",
        "name": "一号供应商",
        "short_name": "一号",
        "pay_days": 30,
        "default_tax_rate": Decimal("0.13"),
    },
    {
        "id": "ven_000002",
        "code": "V0002",
        "status": "enabled",
        "name": "二号供应商",
        "contact": "乙",
        "pay_days": 60,
        "default_tax_rate": Decimal("0.06"),
    },
    {
        "id": "ven_000003",
        "code": "V0003",
        "status": "disabled",
        "name": "三号供应商",
        "pay_days": 0,
        "default_tax_rate": Decimal("0"),
    },
)
ORDER: Final[Mapping[str, Value]] = {
    "id": "so_000001",
    "number": "SUP202610090001",
    "bill_type": ORDER_TABLE,
    "org_id": "br_000001",
    "biz_date": date(2026, 10, 9),
    "status": "draft",
    "remark": "备注",
    "amount": Decimal("1700.00"),
    "vendor_id": "ven_000001",
}
LINES: Final[tuple[Mapping[str, Value], ...]] = (
    {
        "id": "so_000001-1",
        "line_no": 1,
        "qty": Decimal("2000"),
        "price": Decimal("0.85"),
        "amount": Decimal("1700.00"),
        "article_id": "art_000031",
        BILL_ID_COLUMN: "so_000001",
    },
    {
        "id": "so_000001-2",
        "line_no": 2,
        "qty": Decimal("0.5"),
        "price": Decimal("12.125"),
        "amount": Decimal("6.06"),
        "article_id": "art_000032",
        BILL_ID_COLUMN: "so_000001",
    },
)
# (kind, key, how often it was bumped), the counters a build of this content leaves behind.
COUNTERS: Final[tuple[tuple[str, str, int], ...]] = (
    ("id", VENDOR, 3),
    ("code", VENDOR, 3),
    ("id", ORDER_TABLE, 1),
    ("number", "SUP|20261009", 1),
    ("op", "", 2),
)
# The two effective calls of the build: one wrote the vendors, one the order and its lines.
EFFECTIVE_CALLS: Final[tuple[tuple[int, tuple[Write, ...]], ...]] = (
    (
        1,
        tuple(
            Write(entity=VENDOR, op="insert", id=str(record["id"]), cause=DIRECT_CAUSE)
            for record in VENDORS
        ),
    ),
    (
        2,
        (
            Write(entity=ORDER_TABLE, op="insert", id="so_000001", cause=DIRECT_CAUSE),
            Write(entity=LINE_TABLE, op="insert", id="so_000001-1", cause="lk_entry_in"),
            Write(entity=LINE_TABLE, op="insert", id="so_000001-2", cause="lk_entry_in"),
        ),
    ),
)

# Every table of a database, each row of it as SQLite holds it, in primary key order.
type Content = dict[str, list[tuple[object, ...]]]


@pytest.fixture(scope="module")
def domain() -> DomainModel:
    return fixture_domain()


@pytest.fixture(scope="module")
def schema(domain: DomainModel) -> StorageSchema:
    return build_schema(domain)


@pytest.fixture
def snapshot(tmp_path: Path, schema: StorageSchema) -> Path:
    """A finished Snapshot file, built the way the Snapshot cache lays it out."""
    return build_and_finalize(tmp_path / "snapshots", schema, write_in_one_transaction)


@pytest.fixture
def sandbox(tmp_path: Path, schema: StorageSchema, snapshot: Path) -> Iterator[SandboxStore]:
    """An open sandbox store created from ``snapshot``, with its ``base.sqlite`` link."""
    # A space in the directory name: the snapshot is read through a URI (design.md 3.3).
    sandbox_dir = tmp_path / "sandboxes" / "sb 1"
    copy_snapshot(snapshot, sandbox_dir / DB_FILENAME)
    link_snapshot(snapshot, sandbox_dir / BASE_DB_FILENAME)
    store = SandboxStore(sandbox_dir, schema)
    store.open()
    try:
        yield store
    finally:
        store.close()


# --- building the content of a Snapshot ----------------------------------------------------


def write_in_one_transaction(store: SandboxStore) -> None:
    """Write the content straight: one transaction, records and counters in order."""
    with store.transaction() as uow:
        for record in VENDORS:
            uow.insert(VENDOR, record)
        uow.insert(ORDER_TABLE, ORDER)
        for line in LINES:
            uow.insert(LINE_TABLE, line)
        for kind, key, times in COUNTERS:
            for _ in range(times):
                uow.bump_counter(kind, key)  # type: ignore[arg-type]
        for seq, writes in EFFECTIVE_CALLS:
            uow.record_effective_call(seq, writes)


def write_in_many_transactions(store: SandboxStore) -> None:
    """Reach the same content through dozens of transactions, in another order.

    A decoy record with an overflowing text is inserted and deleted again, so the database
    carries free pages; the vendors are inserted in reverse order as placeholders and
    updated into their final values; a line is deleted and written again; the counters are
    bumped one transaction at a time and the effective calls recorded back to front.
    """
    with store.transaction() as uow:
        uow.insert(VENDOR, {"id": "ven_000099", "name": "临时", "contact": "废" * 3000})
    for line in reversed(LINES):
        with store.transaction() as uow:
            uow.insert(LINE_TABLE, line)
    for record in reversed(VENDORS):
        with store.transaction() as uow:
            uow.insert(VENDOR, {"id": record["id"], "name": "占位"})
    with store.transaction() as uow:
        uow.delete(VENDOR, "ven_000099")
        uow.insert(ORDER_TABLE, {**ORDER, "status": "approved"})
    for seq, writes in reversed(EFFECTIVE_CALLS):
        with store.transaction() as uow:
            uow.record_effective_call(seq, writes)
    for kind, key, times in COUNTERS:
        for _ in range(times):
            with store.transaction() as uow:
                uow.bump_counter(kind, key)  # type: ignore[arg-type]
    with store.transaction() as uow:
        uow.delete(LINE_TABLE, "so_000001-1")
        uow.insert(LINE_TABLE, LINES[0])
        uow.update(ORDER_TABLE, "so_000001", {"status": str(ORDER["status"])})
    for record in VENDORS:
        with store.transaction() as uow:
            changes = {name: value for name, value in record.items() if name != "id"}
            uow.update(VENDOR, str(record["id"]), changes)


def build_and_finalize(
    work: Path, schema: StorageSchema, history: Callable[[SandboxStore], None]
) -> Path:
    """Run ``history`` on a build database below ``work`` and return the Snapshot it writes."""
    build_db = work / ".tmp" / "build.sqlite"
    store = build_snapshot_db(build_db, schema)
    try:
        history(store)
    finally:
        store.close()
    out = work / "fx-basic@1.0.0" / "2026-10-09" / "snapshot.sqlite"
    finalize_snapshot(build_db, out)
    return out


# --- reading a database without changing it ------------------------------------------------


def dump(connection: sqlite3.Connection) -> Content:
    """Every table of the connected database, each in primary key order."""
    names = [
        str(name)
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
        )
    ]
    return {name: rows_of(connection, name) for name in names}


def rows_of(connection: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    columns = len(connection.execute(f"SELECT * FROM {table} LIMIT 0").description)
    order = ", ".join(str(index) for index in range(1, columns + 1))
    return connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()


def snapshot_content(path: Path) -> Content:
    """The content of a snapshot file, read read-only and immutable."""
    with closing(sqlite3.connect(immutable_uri(path), uri=True)) as connection:
        return dump(connection)


def store_content(store: SandboxStore) -> Content:
    """The content of an open store, read through a second, plain connection."""
    with closing(sqlite3.connect(store.db_path)) as connection:
        return dump(connection)


def pragma(connection: sqlite3.Connection, name: str) -> object:
    return connection.execute(f"PRAGMA {name}").fetchone()[0]


def file_pragma(path: Path, name: str) -> object:
    with closing(sqlite3.connect(immutable_uri(path), uri=True)) as connection:
        return pragma(connection, name)


def neighbours(path: Path) -> list[str]:
    """The names next to ``path`` in its directory, the file itself left out."""
    return sorted(item.name for item in path.parent.iterdir() if item.name != path.name)


def change_the_sandbox(store: SandboxStore) -> None:
    """One call's worth of writes: a record changed, one deleted, one added."""
    with store.transaction() as uow:
        uow.update(VENDOR, "ven_000001", {"name": "改名", "pay_days": 7})
        uow.delete(VENDOR, "ven_000003")
        uow.insert(VENDOR, {"id": "ven_000004", "name": "四号供应商"})
        uow.bump_counter("id", VENDOR)
        uow.record_effective_call(3, uow.net_writes())


# --- the build database --------------------------------------------------------------------


@pytest.mark.req("20.13")
def test_a_build_database_is_empty_and_fixes_what_the_output_depends_on(
    tmp_path: Path, schema: StorageSchema
) -> None:
    build_db = tmp_path / "build" / "build.sqlite"

    store = build_snapshot_db(build_db, schema)
    try:
        assert store.is_open
        assert store.db_path == build_db
        # Every table of the schema is there and holds no record yet.
        tables = dump(store.driver_connection)
        assert sorted(tables) == sorted(schema.table_names)
        assert all(rows == [] for rows in tables.values())

        connection = store.driver_connection
        assert pragma(connection, "page_size") == PAGE_SIZE
        assert pragma(connection, "auto_vacuum") == 0  # NONE
        assert pragma(connection, "encoding") == "UTF-8"
        assert pragma(connection, "user_version") == STORAGE_FORMAT_VERSION
        # MEMORY keeps the rollback without a journal file next to the database.
        assert pragma(connection, "journal_mode") == "memory"
        assert pragma(connection, "synchronous") == 0  # OFF
        assert neighbours(build_db) == []

        # The pragmas only take hold on an empty file, so an existing one is refused.
        with pytest.raises(FileExistsError, match="already there"):
            build_snapshot_db(build_db, schema)

        # The Snapshot is written through a connection of its own, so the build store may
        # still be open when the build ends (design.md 6.1); a dataset without records
        # gives a Snapshot of empty tables.
        out = build_db.parent / "snapshot.sqlite"
        finalize_snapshot(build_db, out)
        assert snapshot_content(out) == tables
    finally:
        store.close()


@pytest.mark.req("20.13")
def test_different_write_histories_give_byte_identical_snapshots(
    tmp_path: Path, schema: StorageSchema
) -> None:
    histories = {"forward": write_in_one_transaction, "scrambled": write_in_many_transactions}
    snapshots: dict[str, Path] = {}
    builds: dict[str, bytes] = {}
    for name, history in histories.items():
        work = tmp_path / name
        snapshots[name] = build_and_finalize(work, schema, history)
        builds[name] = (work / ".tmp" / "build.sqlite").read_bytes()

    # The two build databases hold the same rows in different files ...
    assert builds["forward"] != builds["scrambled"]
    assert len(builds["scrambled"]) > len(builds["forward"])
    # ... and the Snapshots written out of them are identical byte for byte.
    assert snapshots["forward"].read_bytes() == snapshots["scrambled"].read_bytes()

    content = snapshot_content(snapshots["scrambled"])
    assert content == snapshot_content(snapshots["forward"])
    # The records, the counters and the effective calls are all in the file.
    assert [row[0] for row in content[VENDOR]] == ["ven_000001", "ven_000002", "ven_000003"]
    assert [row[0] for row in content[LINE_TABLE]] == ["so_000001-1", "so_000001-2"]
    assert content[COUNTERS_TABLE] == [
        ("code", VENDOR, 3),
        ("id", ORDER_TABLE, 1),
        ("id", VENDOR, 3),
        ("number", "SUP|20261009", 1),
        ("op", "", 2),
    ]
    assert content[EFFECTIVE_WRITES_TABLE] == [
        (1, VENDOR, "ven_000001"),
        (1, VENDOR, "ven_000002"),
        (1, VENDOR, "ven_000003"),
        (2, ORDER_TABLE, "so_000001"),
        (2, LINE_TABLE, "so_000001-1"),
        (2, LINE_TABLE, "so_000001-2"),
    ]

    out = snapshots["scrambled"]
    # The output keeps the pragmas of the build database and is in a rollback journal
    # mode, which is what an immutable reader needs; it has no sidecar file.
    assert file_pragma(out, "page_size") == PAGE_SIZE
    assert file_pragma(out, "user_version") == STORAGE_FORMAT_VERSION
    assert file_pragma(out, "journal_mode") == "delete"
    assert file_pragma(out, "integrity_check") == "ok"
    assert neighbours(out) == []
    # VACUUM INTO refuses to overwrite, so a finished Snapshot cannot be written over.
    with pytest.raises(sqlite3.OperationalError):
        finalize_snapshot(tmp_path / "forward" / ".tmp" / "build.sqlite", out)


# --- creating a sandbox from a Snapshot ----------------------------------------------------


@pytest.mark.req("15.1", "15.13")
def test_a_sandbox_is_created_from_a_snapshot_without_changing_its_bytes(
    snapshot: Path, sandbox: SandboxStore
) -> None:
    before = snapshot.read_bytes()
    base = sandbox.sandbox_dir / BASE_DB_FILENAME

    # The working database holds the content of the Snapshot, in its own file ...
    assert store_content(sandbox) == snapshot_content(snapshot)
    assert pragma(sandbox.driver_connection, "journal_mode") == "wal"
    assert sandbox.db_path != snapshot
    assert not sandbox.db_path.samefile(snapshot)
    # ... and base.sqlite is the Snapshot itself, one more link to the same file.
    assert base.samefile(snapshot)

    change_the_sandbox(sandbox)

    assert store_content(sandbox) != snapshot_content(snapshot)
    # Writing in the sandbox changes neither the Snapshot nor the basis it fixed.
    assert snapshot.read_bytes() == before
    assert base.read_bytes() == before
    assert neighbours(snapshot) == []


@pytest.mark.req("19.4", "15.13")
def test_a_reset_gives_back_the_content_and_the_counters_of_the_base(
    snapshot: Path, sandbox: SandboxStore
) -> None:
    initial = store_content(sandbox)
    before = snapshot.read_bytes()
    change_the_sandbox(sandbox)
    assert store_content(sandbox) != initial

    sandbox.restore_from(sandbox.sandbox_dir / BASE_DB_FILENAME)

    # Records, counters and effective calls are the ones of the Snapshot again.
    assert store_content(sandbox) == initial
    with sandbox.transaction() as uow:
        # The effective calls of the call that was reset away are gone with it.
        rows = [dict(row) for row in uow.select(EFFECTIVE_WRITES_TABLE)]
        assert sorted({int(str(row["seq"])) for row in rows}) == [1, 2]
        # The counters count on from the Snapshot's values, not from the ones the reset
        # discarded: the build bumped ("id", vendor) three times (requirement 4.6).
        assert uow.bump_counter("id", VENDOR) == 4
    # The database stays usable in WAL mode and the Snapshot is untouched.
    assert pragma(sandbox.driver_connection, "journal_mode") == "wal"
    assert snapshot.read_bytes() == before

    with pytest.raises(FileNotFoundError, match="no such snapshot"):
        sandbox.restore_from(sandbox.sandbox_dir / "named" / "missing.sqlite")


# --- Named_Snapshots -----------------------------------------------------------------------


@pytest.mark.req("19.5")
def test_a_named_snapshot_holds_the_content_of_the_sandbox_and_leaves_it_alone(
    sandbox: SandboxStore,
) -> None:
    change_the_sandbox(sandbox)
    saved_state = store_content(sandbox)
    named = sandbox.sandbox_dir / "named" / "demo.sqlite"

    sandbox.save_to(named)

    # The sandbox database is only read.
    assert store_content(sandbox) == saved_state
    assert pragma(sandbox.driver_connection, "journal_mode") == "wal"
    # The output is a database of its own, in DELETE journal mode, so it can be read
    # read-only and immutable; counters and effective calls came along with it.
    assert snapshot_content(named) == saved_state
    assert file_pragma(named, "journal_mode") == "delete"
    assert file_pragma(named, "integrity_check") == "ok"
    assert neighbours(named) == []

    # A name that is taken is refused, and the file that holds it stays as it is.
    with pytest.raises(FileExistsError, match="already there"):
        sandbox.save_to(named)
    assert snapshot_content(named) == saved_state


@pytest.mark.req("19.6")
def test_restoring_a_named_snapshot_gives_its_content_and_counters_back(
    sandbox: SandboxStore,
) -> None:
    change_the_sandbox(sandbox)
    named = sandbox.sandbox_dir / "named" / "demo.sqlite"
    sandbox.save_to(named)
    saved_state = store_content(sandbox)
    saved_bytes = named.read_bytes()
    # Carry on after the save: more records, more counters, another effective call.
    with sandbox.transaction() as uow:
        uow.insert(VENDOR, {"id": "ven_000005", "name": "五号供应商"})
        uow.delete(VENDOR, "ven_000002")
        uow.bump_counter("id", VENDOR)
        uow.record_effective_call(4, uow.net_writes())
    assert store_content(sandbox) != saved_state

    sandbox.restore_from(named)

    assert store_content(sandbox) == saved_state
    with sandbox.transaction() as uow:
        # The counter continues where the Named_Snapshot left it: it had been bumped four
        # times when it was saved (requirement 4.6).
        assert uow.bump_counter("id", VENDOR) == 5
    assert named.read_bytes() == saved_bytes
    assert neighbours(named) == []


# --- linking across file systems -----------------------------------------------------------


@pytest.mark.req("15.13")
def test_the_base_is_copied_when_the_snapshot_cannot_be_linked(
    tmp_path: Path, snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A destination on another file system cannot be arranged in a temporary directory, so
    # os.link is made to fail exactly as it would there (EXDEV).
    def refuse(source: object, target: object) -> None:
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(os, "link", refuse)
    base = tmp_path / "sandboxes" / "sb 2" / BASE_DB_FILENAME

    link_snapshot(snapshot, base)

    # The copy is the same file content, as its own file rather than a second link.
    assert base.read_bytes() == snapshot.read_bytes()
    assert not base.samefile(snapshot)
    assert snapshot_content(base) == snapshot_content(snapshot)

    # Any other error is the caller's to see: a copy would hide a real problem.
    def deny(source: object, target: object) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "link", deny)
    with pytest.raises(OSError, match="Permission denied"):
        link_snapshot(snapshot, tmp_path / "sandboxes" / "sb 3" / BASE_DB_FILENAME)


# --- refused calls -------------------------------------------------------------------------


@pytest.mark.req("15.1")
def test_a_missing_source_or_an_existing_target_is_refused(
    tmp_path: Path, snapshot: Path, schema: StorageSchema
) -> None:
    missing = tmp_path / "snapshots" / "missing.sqlite"
    dest = tmp_path / "sandboxes" / "sb 4" / DB_FILENAME

    with pytest.raises(FileNotFoundError, match="no such snapshot"):
        copy_snapshot(missing, dest)
    with pytest.raises(FileNotFoundError, match="no such snapshot"):
        link_snapshot(missing, dest)
    with pytest.raises(FileNotFoundError, match="no such build database"):
        finalize_snapshot(missing, tmp_path / "out.sqlite")
    # A refused call creates nothing.
    assert not dest.exists()
    assert not (tmp_path / "out.sqlite").exists()

    copy_snapshot(snapshot, dest)
    assert neighbours(dest) == []
    with pytest.raises(FileExistsError, match="already there"):
        copy_snapshot(snapshot, dest)
    with pytest.raises(FileExistsError, match="already there"):
        link_snapshot(snapshot, dest)


@pytest.mark.req("19.4", "19.5")
def test_a_whole_database_copy_needs_an_open_store_and_no_transaction(
    sandbox: SandboxStore,
) -> None:
    base = sandbox.sandbox_dir / BASE_DB_FILENAME
    named = sandbox.sandbox_dir / "named" / "demo.sqlite"

    with sandbox.transaction():
        with pytest.raises(RuntimeError, match="inside a transaction"):
            sandbox.restore_from(base)
        with pytest.raises(RuntimeError, match="inside a transaction"):
            sandbox.save_to(named)
    assert not named.exists()

    content = store_content(sandbox)
    sandbox.close()
    with pytest.raises(RuntimeError, match="not open"):
        sandbox.restore_from(base)
    with pytest.raises(RuntimeError, match="not open"):
        sandbox.save_to(named)

    # The refused calls left the database as it was.
    sandbox.open()
    assert store_content(sandbox) == content
    assert not named.exists()
