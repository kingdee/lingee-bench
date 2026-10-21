"""Probes for the SQLite and SQLAlchemy behaviour the state layer relies on (design V8, V9).

Checked with SQLite 3.53.1 (``sqlite3.sqlite_version`` of the uv-managed CPython 3.12.14)
and SQLAlchemy 2.1.4 on the pysqlite driver. Both items hold there; the tests below are
the evidence. If a test fails after a version change, switch to the fallback named here.

V8 holds.

* VACUUM INTO is byte-deterministic. Build databases that reach the same rows through
  different insert, update and delete orders and 1 versus more than 100 transactions
  differ on disk, but VACUUM INTO writes identical files, also from two interpreters with
  different data dirs and PYTHONHASHSEED values. The output uses the rollback journal
  format (header bytes 18-19 are 1), has no freelist or trailing pages, keeps page_size,
  encoding, auto_vacuum=NONE and user_version, and holds the values of a new file where
  the build history could show: change counter and version-valid-for are 1. The schema
  cookie is the build database's plus one, so every build must run the same DDL sequence
  (an extra CREATE and DROP changes the output). SQLite does not fsync the output; the
  caller must. The header also records the SQLite version number.
* Backup from an immutable snapshot into a WAL database works. ``immutable_uri()``
  (``Path.as_uri()`` plus ``?mode=ro&immutable=1``, opened with ``uri=True``) handles paths
  with spaces, '#', '%', '&', '=' and CJK characters and creates no sidecar file.
  ``Connection.backup`` into a new file gives a rollback-mode copy that ``PRAGMA
  journal_mode=WAL`` switches to WAL. Backup into a database already in WAL mode leaves it
  in WAL mode, the re-assert returns 'wal', the content and integrity_check match the
  source, and an open second connection reads the restored content on its next statement.
  Once all connections close, no -wal or -shm file remains.
* A plain connection with ``PRAGMA query_only=1`` reads an idle WAL database: with no
  connection open and no -wal or -shm file it opens the database (creating both files),
  can ATTACH the immutable base by URI (the main connection needs ``uri=True``), and inside
  a deferred BEGIN sees exactly the committed state, also while another connection holds
  an open transaction whose pages have already spilled into the -wal file. Writes fail
  with SQLITE_READONLY, and so would BEGIN IMMEDIATE, so the reader uses plain BEGIN. Closing
  as the last connection, it checkpoints and removes the -wal and -shm files.

V9 holds. A ``connect`` listener sets the driver's ``isolation_level`` to None and a
``begin`` listener runs ``conn.exec_driver_sql("BEGIN IMMEDIATE")``. begin() then takes the
write lock before any statement; a second connection's begin() raises
``sqlalchemy.exc.OperationalError`` wrapping SQLITE_BUSY ("database is locked") once the
busy timeout from ``connect_args={"timeout": ...}`` expires; rollback restores the
content; the driver never emits a BEGIN of its own (the control test shows the "BEGIN "
it emits before the first DML without the recipe). DDL from ``create_all`` runs inside the
transaction too. Constraints: keep sqlite3's default ``autocommit`` of
LEGACY_TRANSACTION_CONTROL, and leave ``skip_autocommit_rollback`` off, because the dialect
reports ``isolation_level is None`` as autocommit and would then skip ROLLBACK. Every
transaction of this engine takes the write lock, reads included, so the diff reader uses
its own sqlite3 connection.

Fallbacks: for V8, write the snapshot by copying each table in primary key order into a
new database, and read the diff under the sandbox lock; for V9, issue BEGIN IMMEDIATE,
COMMIT and ROLLBACK on a sqlite3 connection inside the state layer.
"""

import os
import random
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    Connection,
    Engine,
    MetaData,
    Table,
    Text,
    create_engine,
    delete,
    event,
    insert,
    select,
    update,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import ConnectionPoolEntry

REPO_ROOT = Path(__file__).resolve().parents[2]

# Busy timeout of every connection that may wait for the write lock, in seconds. Small, so
# the busy-error probes fail fast instead of waiting for sqlite3's default of 5 seconds.
BUSY_TIMEOUT = 0.1

STORAGE_FORMAT_VERSION = 7
PAGE_SIZE = 4096

# Pragmas of the snapshot build database, in the order design component 3.3 lists them.
# page_size, auto_vacuum and encoding only take effect before the file is initialised;
# writing user_version is the first write and initialises it.
BUILD_PRAGMAS = (
    f"PRAGMA page_size={PAGE_SIZE}",
    "PRAGMA auto_vacuum=NONE",
    "PRAGMA encoding='UTF-8'",
    f"PRAGMA user_version={STORAGE_FORMAT_VERSION}",
    "PRAGMA journal_mode=MEMORY",
    "PRAGMA synchronous=OFF",
)

# Record tables as in data model 3: WITHOUT ROWID, TEXT primary key, decimals stored as
# canonical TEXT, plus the internal counter and effective-call tables. Created in code
# point order of the table names.
SCHEMA = (
    "CREATE TABLE _counters (kind TEXT NOT NULL, key TEXT NOT NULL, value INTEGER NOT NULL,"
    " PRIMARY KEY (kind, key)) WITHOUT ROWID",
    "CREATE TABLE _effective_writes (seq INTEGER NOT NULL, entity TEXT NOT NULL,"
    " id TEXT NOT NULL, PRIMARY KEY (seq, entity, id)) WITHOUT ROWID",
    "CREATE TABLE customer (id TEXT NOT NULL PRIMARY KEY, name TEXT NOT NULL,"
    " credit_limit TEXT, active INTEGER NOT NULL, note TEXT) WITHOUT ROWID",
    "CREATE TABLE sales_order (id TEXT NOT NULL PRIMARY KEY, customer TEXT NOT NULL,"
    " order_date TEXT NOT NULL, total TEXT NOT NULL) WITHOUT ROWID",
    "CREATE TABLE sales_order_line (id TEXT NOT NULL PRIMARY KEY, bill_id TEXT NOT NULL,"
    " line_no INTEGER NOT NULL, qty TEXT NOT NULL, price TEXT NOT NULL,"
    " amount TEXT NOT NULL) WITHOUT ROWID",
)
PRIMARY_KEY_WIDTH = {
    "_counters": 2,
    "_effective_writes": 3,
    "customer": 1,
    "sales_order": 1,
    "sales_order_line": 1,
}

Row = tuple[object, ...]
Dataset = dict[str, list[Row]]


def dataset() -> Dataset:
    """Return the probe content: every table's rows in primary key order."""
    customers: list[Row] = []
    for i in range(1, 361):
        note: str | None
        if i % 90 == 0:
            note = "长" * 3000  # 9000 bytes of UTF-8: stored on overflow pages
        elif i % 4 == 0:
            note = None
        else:
            note = f"备注 {i}"
        credit = str(Decimal(i * 125) / 4) if i % 3 else None
        customers.append((f"C{i:05d}", f"客户 {i:03d}", credit, i % 2, note))
    orders: list[Row] = []
    lines: list[Row] = []
    for n in range(1, 241):
        bill_id = f"SO-20240115-{n:04d}"
        total = Decimal(0)
        for line_no in range(1, n % 4 + 2):
            qty = Decimal(n % 7 + 1)
            price = Decimal(f"{n}.{line_no:02d}")
            total += qty * price
            row = (f"{bill_id}#{line_no:03d}", bill_id, line_no, str(qty), str(price))
            lines.append((*row, str(qty * price)))
        orders.append((bill_id, f"C{n % 360 + 1:05d}", "2024-01-15", str(total)))
    counters: list[Row] = [
        ("code", "customer", 360),
        ("id", "customer", 360),
        ("id", "sales_order", 240),
        ("number", "SO|20240115", 240),
        ("op", "", 25),
    ]
    effective: list[Row] = [(seq, "customer", f"C{seq:05d}") for seq in range(1, 26)]
    effective += [(seq, "sales_order", f"SO-20240115-{seq:04d}") for seq in range(5, 26, 5)]
    data: Dataset = {
        "_counters": counters,
        "_effective_writes": effective,
        "customer": customers,
        "sales_order": orders,
        "sales_order_line": lines,
    }
    return {table: by_primary_key(table, rows) for table, rows in data.items()}


def by_primary_key(table: str, rows: list[Row]) -> list[Row]:
    # Python's str order is code point order, the same as SQLite's BINARY collation on UTF-8.
    width = PRIMARY_KEY_WIDTH[table]
    return sorted(rows, key=lambda row: row[:width])


def insert_sql(table: str, width: int) -> str:
    return f"INSERT INTO {table} VALUES ({', '.join('?' * width)})"


def load_in_one_transaction(conn: sqlite3.Connection, data: Dataset) -> None:
    """Insert every row in primary key order, table by table, in a single transaction."""
    conn.execute("BEGIN")
    for table, rows in data.items():
        conn.executemany(insert_sql(table, len(rows[0])), rows)
    conn.execute("COMMIT")


class ScrambledLoader:
    """Reach the same content through shuffled inserts, deletes, updates and many commits."""

    def __init__(self, conn: sqlite3.Connection, seed: int) -> None:
        self.conn = conn
        self.rng = random.Random(seed)
        self.pending = 0
        self.batch = 1
        self.transactions = 0

    def run(self, sql: str, params: Sequence[object] = ()) -> None:
        if self.pending == 0:
            self.conn.execute("BEGIN")
            self.batch = self.rng.randint(1, 25)
        self.conn.execute(sql, params)
        self.pending += 1
        if self.pending >= self.batch:
            self.commit()

    def commit(self) -> None:
        if self.pending:
            self.conn.execute("COMMIT")
            self.transactions += 1
            self.pending = 0

    def load(self, data: Dataset) -> None:
        rng = self.rng
        decoys: list[Row] = [(f"D{i:05d}", f"临时 {i}", "0", 0, "废" * 700) for i in range(300)]
        inserts: list[tuple[str, Row]] = [("customer", row) for row in decoys]
        bumps: list[tuple[object, object]] = []
        for table, rows in data.items():
            for row in rows:
                if table == "customer":
                    # Placeholder values with an overflowing note; the final values come later.
                    inserts.append((table, (row[0], "占位", "1", 1, "x" * 5000)))
                elif table == "_counters":
                    kind, key, value = row
                    assert isinstance(value, int)
                    inserts.append((table, (kind, key, 0)))
                    bumps += [(kind, key)] * value
                else:
                    inserts.append((table, row))
        rng.shuffle(inserts)
        for table, row in inserts:
            self.run(insert_sql(table, len(row)), row)
        rng.shuffle(decoys)
        for row in decoys:
            self.run("DELETE FROM customer WHERE id = ?", (row[0],))
        customers = list(data["customer"])
        rng.shuffle(customers)
        for cid, name, credit, active, note in customers:
            self.run(
                "UPDATE customer SET name = ?, credit_limit = ?, active = ?, note = ? WHERE id = ?",
                (name, credit, active, note, cid),
            )
        rng.shuffle(bumps)
        for kind, key in bumps:
            self.run(
                "UPDATE _counters SET value = value + 1 WHERE kind = ? AND key = ?", (kind, key)
            )
        for row in rng.sample(data["sales_order_line"], 50):
            self.run("DELETE FROM sales_order_line WHERE id = ?", (row[0],))
            self.run(insert_sql("sales_order_line", len(row)), row)
        self.commit()


def build_snapshot(build_db: Path, out: Path, variant: str) -> None:
    """Build the probe content in ``build_db`` and write it out with VACUUM INTO.

    The subprocess probe imports and runs this function in a fresh interpreter.
    """
    data = dataset()
    with closing(sqlite3.connect(build_db, isolation_level=None)) as conn:
        for pragma in BUILD_PRAGMAS:
            conn.execute(pragma)
        for ddl in SCHEMA:
            conn.execute(ddl)
        if variant == "one-transaction":
            load_in_one_transaction(conn, data)
        elif variant == "scrambled":
            loader = ScrambledLoader(conn, seed=20240115)
            loader.load(data)
            assert loader.transactions > 100
        else:
            raise ValueError(variant)
        conn.execute("VACUUM INTO ?", (str(out),))


def immutable_uri(path: Path) -> str:
    """URI opening ``path`` read-only, without locks or change detection."""
    return f"{path.resolve().as_uri()}?mode=ro&immutable=1"


def table_rows(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[Row]:
    columns = len(conn.execute(f"SELECT * FROM {schema}.{table} LIMIT 0").description)
    order = ", ".join(str(i) for i in range(1, columns + 1))
    return conn.execute(f"SELECT * FROM {schema}.{table} ORDER BY {order}").fetchall()


def dump(conn: sqlite3.Connection, schema: str = "main") -> Dataset:
    names = conn.execute(
        f"SELECT name FROM {schema}.sqlite_schema WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return {name: table_rows(conn, name, schema) for (name,) in names}


def dump_file(path: Path) -> Dataset:
    with closing(sqlite3.connect(immutable_uri(path), uri=True)) as conn:
        return dump(conn)


def header_u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def sidecars(db: Path) -> list[str]:
    return sorted(p.name for p in db.parent.iterdir() if p.name != db.name)


# ---------------------------------------------------------------------------
# V8: VACUUM INTO determinism
# ---------------------------------------------------------------------------


@pytest.mark.req("20.13")
def test_vacuum_into_output_depends_only_on_content(tmp_path: Path) -> None:
    outputs: dict[str, bytes] = {}
    sources: dict[str, bytes] = {}
    cookies: dict[str, int] = {}
    for variant in ("one-transaction", "scrambled"):
        work = tmp_path / variant
        work.mkdir()
        build_snapshot(work / "build.sqlite", work / "snapshot.sqlite", variant)
        assert sidecars(work / "snapshot.sqlite") == ["build.sqlite"]
        sources[variant] = (work / "build.sqlite").read_bytes()
        outputs[variant] = (work / "snapshot.sqlite").read_bytes()
        with closing(sqlite3.connect(work / "build.sqlite")) as conn:
            assert dump(conn) == dataset()
            cookies[variant] = conn.execute("PRAGMA schema_version").fetchone()[0]

    # The build databases hold the same rows in different page layouts ...
    assert sources["one-transaction"] != sources["scrambled"]
    assert len(sources["scrambled"]) > len(sources["one-transaction"])
    # ... and VACUUM INTO writes byte-identical files.
    assert outputs["one-transaction"] == outputs["scrambled"]

    out = outputs["scrambled"]
    assert out[:16] == b"SQLite format 3\x00"
    assert int.from_bytes(out[16:18], "big") == PAGE_SIZE
    assert out[18:20] == b"\x01\x01"  # rollback journal format, not WAL
    assert len(out) == header_u32(out, 28) * PAGE_SIZE  # no trailing pages
    assert header_u32(out, 24) == header_u32(out, 92) == 1  # change counter of a new file
    assert header_u32(out, 32) == header_u32(out, 36) == 0  # empty freelist
    # The schema cookie is the build database's plus one: the DDL sequence must be fixed.
    assert cookies["one-transaction"] == cookies["scrambled"]
    assert header_u32(out, 40) == cookies["scrambled"] + 1
    assert header_u32(out, 52) == 0  # auto_vacuum=NONE
    assert header_u32(out, 56) == 1  # UTF-8
    assert header_u32(out, 60) == STORAGE_FORMAT_VERSION
    assert header_u32(out, 96) == sqlite3.sqlite_version_info[0] * 1_000_000 + (
        sqlite3.sqlite_version_info[1] * 1000 + sqlite3.sqlite_version_info[2]
    )

    snapshot = tmp_path / "scrambled" / "snapshot.sqlite"
    assert dump_file(snapshot) == dataset()
    with closing(sqlite3.connect(immutable_uri(snapshot), uri=True)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)


# Runs build_snapshot in a fresh interpreter: argv[1] is the data dir, argv[2] the variant.
SUBPROCESS_BUILD = (
    "import sys\n"
    "from pathlib import Path\n"
    "from tests.probes.test_sqlite import build_snapshot\n"
    "data_dir = Path(sys.argv[1])\n"
    "build_snapshot(data_dir / 'build.sqlite', data_dir / 'snapshot.sqlite', sys.argv[2])\n"
)


@pytest.mark.req("20.13")
def test_vacuum_into_output_is_identical_across_processes(tmp_path: Path) -> None:
    outputs: list[bytes] = []
    for name, variant, hash_seed in (
        ("process-a", "one-transaction", "1"),
        ("process-b", "scrambled", "2"),
    ):
        data_dir = tmp_path / name
        data_dir.mkdir()
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(REPO_ROOT)}
        subprocess.run(
            [sys.executable, "-c", SUBPROCESS_BUILD, str(data_dir), variant],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            timeout=120,
        )
        outputs.append((data_dir / "snapshot.sqlite").read_bytes())
    in_process = tmp_path / "in-process"
    in_process.mkdir()
    build_snapshot(in_process / "build.sqlite", in_process / "snapshot.sqlite", "scrambled")
    assert outputs[0] == outputs[1] == (in_process / "snapshot.sqlite").read_bytes()


# ---------------------------------------------------------------------------
# V8: backup from an immutable snapshot into a WAL database
# ---------------------------------------------------------------------------


def change_some_rows(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE customer SET name = '已改名' WHERE id = 'C00001'")
    conn.execute("DELETE FROM sales_order_line WHERE bill_id = 'SO-20240115-0001'")
    conn.execute(
        "INSERT OR REPLACE INTO customer VALUES ('C99999', '新客户', '10', 1, ?)", ("新" * 4000,)
    )
    conn.execute("UPDATE _counters SET value = value + 1 WHERE kind = 'op'")
    conn.execute("COMMIT")


@pytest.mark.req("15.13")
def test_backup_from_immutable_snapshot_into_wal_database(tmp_path: Path) -> None:
    # Characters that must be percent-encoded in a URI path, so the probe also pins
    # immutable_uri() against a data-dir with such a name.
    data_dir = tmp_path / "data dir #1 %41&x=1 快照"
    data_dir.mkdir()
    snapshot = data_dir / "snapshot.sqlite"
    build_snapshot(tmp_path / "build.sqlite", snapshot, "one-transaction")
    snapshot_bytes = snapshot.read_bytes()
    expected = dataset()

    sandbox = data_dir / "sandboxes" / "sb 1"
    sandbox.mkdir(parents=True)
    db = sandbox / "db.sqlite"
    base = sandbox / "base.sqlite"
    os.link(snapshot, base)

    with (
        closing(sqlite3.connect(immutable_uri(snapshot), uri=True)) as source,
        closing(sqlite3.connect(db, isolation_level=None, timeout=BUSY_TIMEOUT)) as work,
    ):
        # Create: copy into a new file, then switch it to WAL.
        source.backup(work)
        assert work.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        assert dump(work) == expected
        change_some_rows(work)
        changed = dump(work)
        assert changed != expected

    with (
        closing(sqlite3.connect(immutable_uri(base), uri=True)) as source,
        closing(sqlite3.connect(db, isolation_level=None, timeout=BUSY_TIMEOUT)) as work,
        closing(sqlite3.connect(db, isolation_level=None, timeout=BUSY_TIMEOUT)) as other,
    ):
        assert dump(other) == changed  # an open connection that has read the old state
        change_some_rows(work)  # leave committed frames in the -wal file
        # Reset: the backup replaces every page of the WAL database in place.
        source.backup(work)
        assert work.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert work.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        assert dump(work) == expected
        assert dump(other) == expected
        assert work.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        # The restored database stays writable in WAL mode.
        change_some_rows(work)
        assert dump(other) == changed
        source.backup(work)

    # After every connection is closed the file alone holds the restored content.
    assert sidecars(db) == ["base.sqlite"]
    with closing(sqlite3.connect(db)) as reopened:
        assert reopened.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert dump(reopened) == expected
        assert reopened.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    # The immutable source was never modified and got no -journal, -wal or -shm file.
    assert snapshot.read_bytes() == snapshot_bytes
    assert sidecars(snapshot) == ["sandboxes"]


# ---------------------------------------------------------------------------
# V8: query_only reader of an idle WAL database
# ---------------------------------------------------------------------------


@pytest.mark.req("19.8")
def test_query_only_reader_of_idle_wal_database(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.sqlite"
    build_snapshot(tmp_path / "build.sqlite", snapshot, "one-transaction")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    db = sandbox / "db.sqlite"
    with (
        closing(sqlite3.connect(immutable_uri(snapshot), uri=True)) as source,
        closing(sqlite3.connect(db, isolation_level=None)) as work,
    ):
        source.backup(work)
        assert work.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        change_some_rows(work)
        committed = dump(work)

    # Idle: no connection is open; the -wal and -shm files are gone.
    assert sidecars(db) == []

    with closing(sqlite3.connect(db, uri=True, isolation_level=None)) as reader:
        reader.execute("PRAGMA query_only=1")
        reader.execute("ATTACH ? AS base", (immutable_uri(snapshot),))
        assert reader.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert sidecars(db) == ["db.sqlite-shm", "db.sqlite-wal"]

        with closing(sqlite3.connect(db, isolation_level=None, timeout=BUSY_TIMEOUT)) as writer:
            # A small page cache makes the open transaction spill its pages into the -wal file.
            writer.execute("PRAGMA cache_size=10")
            writer.execute("BEGIN IMMEDIATE")
            writer.executemany(
                "INSERT INTO customer VALUES (?, '未提交', NULL, 0, ?)",
                [(f"U{i:05d}", "未" * 200) for i in range(3000)],
            )
            writer.execute("UPDATE sales_order SET total = '0'")
            assert (sandbox / "db.sqlite-wal").stat().st_size > 50 * PAGE_SIZE

            with pytest.raises(sqlite3.OperationalError) as begin_error:
                reader.execute("BEGIN IMMEDIATE")
            assert begin_error.value.sqlite_errorname == "SQLITE_READONLY"
            assert not reader.in_transaction

            reader.execute("BEGIN")
            assert dump(reader) == committed
            changed_ids = reader.execute(
                "SELECT id FROM (SELECT * FROM main.customer EXCEPT SELECT * FROM base.customer)"
                " UNION SELECT id FROM (SELECT * FROM base.customer EXCEPT SELECT * FROM main.customer)"
                " ORDER BY id"
            ).fetchall()
            assert changed_ids == [("C00001",), ("C99999",)]
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                reader.execute("DELETE FROM customer")
            assert excinfo.value.sqlite_errorname == "SQLITE_READONLY"
            reader.execute("COMMIT")

            writer.execute("COMMIT")
            assert reader.execute("SELECT count(*) FROM customer").fetchone() == (
                len(committed["customer"]) + 3000,
            )

    # The reader closed last: it checkpointed and removed both files, leaving the sandbox idle.
    assert sidecars(db) == []


# ---------------------------------------------------------------------------
# V9: SQLAlchemy emitting BEGIN IMMEDIATE on pysqlite
# ---------------------------------------------------------------------------

metadata = MetaData()
customer = Table(
    "customer",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("credit_limit", Text),
    sqlite_with_rowid=False,
)

Trace = list[tuple[int, str]]


def engine_url(path: Path) -> URL:
    # URL.create takes the path verbatim; a URL string would parse '?' and '#' in it.
    return URL.create("sqlite+pysqlite", database=str(path))


def trace_statements(dbapi_connection: sqlite3.Connection, trace: Trace) -> None:
    key = id(dbapi_connection)

    def record(sql: str) -> None:
        trace.append((key, sql))

    dbapi_connection.set_trace_callback(record)


def begin_immediate_engine(path: Path, trace: Trace) -> Engine:
    """The recipe of design component 3.2 / V9."""
    engine = create_engine(
        engine_url(path),
        connect_args={"timeout": BUSY_TIMEOUT, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def on_connect(dbapi_connection: sqlite3.Connection, _record: ConnectionPoolEntry) -> None:
        # Stop the driver from emitting BEGIN itself; it still emits COMMIT and ROLLBACK.
        dbapi_connection.isolation_level = None
        trace_statements(dbapi_connection, trace)
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    @event.listens_for(engine, "begin")
    def on_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def driver_connection(conn: Connection) -> sqlite3.Connection:
    raw = conn.connection.driver_connection
    assert isinstance(raw, sqlite3.Connection)
    return raw


def customers(engine: Engine) -> list[Row]:
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(select(customer).order_by(customer.c.id))]


def begins(trace: Trace) -> list[str]:
    return [sql for _, sql in trace if sql.split(maxsplit=1)[0].upper() == "BEGIN"]


@contextmanager
def disposing(engine: Engine) -> Iterator[Engine]:
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.req("12.7")
def test_default_pysqlite_transaction_begins_lazily_and_implicitly(tmp_path: Path) -> None:
    """Control: without the recipe, begin() emits nothing and the driver emits 'BEGIN '."""
    trace: Trace = []
    engine = create_engine(engine_url(tmp_path / "default.sqlite"))
    event.listen(
        engine, "connect", lambda dbapi_connection, _: trace_statements(dbapi_connection, trace)
    )
    with disposing(engine):
        metadata.create_all(engine)
        with engine.connect() as conn:
            assert driver_connection(conn).autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
            trans = conn.begin()
            del trace[:]
            assert not driver_connection(conn).in_transaction  # no lock taken yet
            conn.execute(insert(customer).values(id="C1", name="a"))
            assert begins(trace) == ["BEGIN "]  # emitted by the driver, deferred
            trans.rollback()


@pytest.mark.req("12.7")
def test_begin_hook_emits_begin_immediate(tmp_path: Path) -> None:
    trace: Trace = []
    with disposing(begin_immediate_engine(tmp_path / "work.sqlite", trace)) as engine:
        metadata.create_all(engine)  # DDL runs inside BEGIN IMMEDIATE ... COMMIT as well
        with engine.begin() as conn:
            conn.execute(
                insert(customer),
                [{"id": f"C{i}", "name": f"客户 {i}", "credit_limit": f"{i}.50"} for i in range(5)],
            )
        before = customers(engine)

        with engine.connect() as first:
            raw = driver_connection(first)
            assert raw.isolation_level is None
            assert raw.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
            trans = first.begin()
            # The write lock is taken by begin() itself, before any statement.
            assert trace[-1] == (id(raw), "BEGIN IMMEDIATE")
            assert raw.in_transaction

            with engine.connect() as second:
                started = time.monotonic()
                with pytest.raises(OperationalError) as excinfo:
                    second.begin()
                waited = time.monotonic() - started
            orig = excinfo.value.orig
            assert isinstance(orig, sqlite3.OperationalError)
            assert orig.sqlite_errorname == "SQLITE_BUSY"
            # The busy handler waited for the configured timeout, not sqlite3's default 5 s.
            assert BUSY_TIMEOUT * 0.9 <= waited < 2

            first.execute(insert(customer).values(id="C9", name="新"))
            first.execute(update(customer).where(customer.c.id == "C1").values(name="改"))
            first.execute(delete(customer).where(customer.c.id == "C2"))
            assert len(first.execute(select(customer)).all()) == 5
            trans.rollback()
            assert trace[-1] == (id(raw), "ROLLBACK")

        assert customers(engine) == before
        # Every transaction, including the failed one and the reads, began with BEGIN
        # IMMEDIATE; the driver never emitted a BEGIN of its own.
        assert begins(trace)
        assert set(begins(trace)) == {"BEGIN IMMEDIATE"}
        assert "COMMIT" in {sql for _, sql in trace}
