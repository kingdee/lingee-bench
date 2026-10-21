"""The working database of one sandbox and the unit of work of one call (design.md 3.2).

:class:`SandboxStore` owns the SQLite file a sandbox keeps its records in;
:class:`UnitOfWork` is the repository the engine reads and writes through, one instance
per database transaction. A tools/call runs every write of its pipeline - records,
linkages, writebacks and counters - in a single transaction, so an error at any step
leaves the database exactly as the call found it (requirements 12.7 and 4.12)::

    with store.transaction() as uow:
        number = uow.bump_counter("id", "supply_order")
        uow.insert("supply_order", {"id": f"so_{number:06d}", ...})
        with uow.cause("lk_entry_in"):
            uow.insert("stock_txn", {...})
        writes = uow.net_writes()
        uow.record_effective_call(seq, writes)

Transactions. The pysqlite recipe of V9, confirmed by tests/probes/test_sqlite.py: a
``connect`` listener sets the driver's ``isolation_level`` to None, so the driver emits no
BEGIN of its own, and a ``begin`` listener issues ``BEGIN IMMEDIATE``, so the write lock is
taken before the first statement rather than at the first write. Every transaction of this
engine takes that lock, reads included; the diff reads through its own connection
(design.md 3.4) and never waits for it. The connection is opened with
``check_same_thread=False`` and the store keeps exactly one of them (a ``StaticPool``):
every call of one sandbox runs in a worker thread, serialized by the sandbox lock
(design.md 7.5), so one connection is enough and the pragmas of :meth:`SandboxStore.open`
are asserted once. Nesting transactions of one store is a programming error and raises.

Values. The engine works in typed values - str, int, bool, Decimal,
``datetime.date``, None - and the database holds the two SQLite types of design.md data
model 3: decimals as the canonical text of
:func:`~erp_mock.common.decimals.format_decimal`, dates as ``YYYY-MM-DD``, bools as 0 or 1,
ints as INTEGER, everything else as TEXT, an absent value as NULL. :func:`to_stored` and
:func:`from_stored` convert between the two and are the only place that does;
:meth:`UnitOfWork.get` and :meth:`UnitOfWork.select` return typed values again. Writing a
decimal rounds it to the precision of its field type (requirement 4.7), so a stored value
never has more decimals than the field allows, and equal values have equal text.

Net writes. Requirement 17.4 reports what a call changed per (record, cause), not per
statement: the unit of work saves a record's pre-transaction row the first time the call
touches it and, per cause, which columns were written. :meth:`UnitOfWork.net_writes` then
compares that row with the row the transaction now holds, so a record inserted and deleted
again, or changed back to its original value, does not appear. The cause is ``direct`` for
the records the called tool operates on and the id of the linkage or writeback that
changed any other record (:meth:`UnitOfWork.cause`).

Whole-database copies. :meth:`SandboxStore.restore_from` and :meth:`SandboxStore.save_to`
move the whole database at once through SQLite's backup API, which is what a reset, a
restore and a Named_Snapshot are (requirements 19.4, 19.5 and 19.6): everything a sandbox
remembers is in the file, so copying pages gives the right semantics for the records, the
counters and the effective calls without naming any of them. The snapshot files these two
work on are the business of :mod:`erp_mock.state.snapshot` (design.md 3.3).
"""

import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    Executable,
    Table,
    create_engine,
    event,
    or_,
)
from sqlalchemy import delete as sql_delete
from sqlalchemy import insert as sql_insert
from sqlalchemy import select as sql_select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import URL
from sqlalchemy.pool import ConnectionPoolEntry, StaticPool

from erp_mock.common.decimals import format_decimal
from erp_mock.common.frozen import FrozenMap
from erp_mock.state.schema import (
    COUNTERS_TABLE,
    EFFECTIVE_WRITES_TABLE,
    ID_COLUMN,
    WRITEBACK_LEDGER_TABLE,
    WRITEBACK_TARGET_COLUMN,
    ColumnSchema,
    CounterKind,
    StorageSchema,
    TableSchema,
)

__all__ = [
    "CONNECTION_PRAGMAS",
    "DB_FILENAME",
    "DIRECT_CAUSE",
    "Record",
    "SandboxStore",
    "StoredValue",
    "UnitOfWork",
    "Value",
    "Write",
    "WriteOp",
    "from_stored",
    "immutable_uri",
    "to_stored",
]

# The working database of a sandbox, below its directory (design.md data model 2); the
# ``-wal`` and ``-shm`` files sit next to it while the store is open.
DB_FILENAME: Final = "db.sqlite"

# The cause of a change to the records the called tool operates on, their lines and the
# bills a push generates; every other change names the linkage or writeback that made it
# (requirement 17.4).
DIRECT_CAUSE: Final = "direct"

# One typed value, as the engine works with it: the Python type of the field type of its
# column (see the module docstring). A bool is an int to Python but not to a column: an
# int column refuses a bool and a bool column refuses an int.
type Value = str | int | bool | Decimal | date | None
# One value as SQLite holds it: TEXT, INTEGER or NULL (design.md data model 3).
type StoredValue = str | int | None
# One record: every column of its table in DDL order, with typed values.
type Record = FrozenMap[str, Value]
# The net change one cause made to one record (requirement 17.4).
type WriteOp = Literal["insert", "update", "delete"]

# Pragmas every connection of a sandbox database asserts (design.md 3.2). WAL lets the
# diff read a committed state without waiting for the write lock (requirement 19.8);
# synchronous=NORMAL does not fsync on commit, which a mock sandbox does not need;
# foreign keys stay off because references are checked by the pipeline, which decides the
# order of the errors a call reports (requirement 5.6). The build database of a snapshot
# asserts its own set instead (:data:`~erp_mock.state.snapshot.BUILD_PRAGMAS`).
CONNECTION_PRAGMAS: Final = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=OFF",
)

# The escape character of every LIKE the store emits. SQLite's LIKE ignores case for ASCII
# letters only, which is what a keyword search compares by (requirement 5.9).
_LIKE_ESCAPE: Final = "\\"

# Immutable defaults for the filters of :meth:`UnitOfWork.select`.
_NO_WHERE: Final[Mapping[str, Value]] = FrozenMap()
_NO_LIKE: Final[Mapping[str, str]] = FrozenMap()
_NO_DATES: Final[Mapping[str, date]] = FrozenMap()


@dataclass(frozen=True, slots=True)
class Write:
    """The net change one cause made to one record, an item of requirement 17.4.

    The keys of a trace event's ``writes`` item in order: ``entity`` is the name of the
    record's table - the entity name, the bill name, ``<bill>_line``, stock_txn or
    stock_balance (requirement 18.4) - ``op`` the net change, ``id`` the record's id and
    ``cause`` :data:`DIRECT_CAUSE` or the id of a linkage or writeback.
    """

    entity: str
    op: WriteOp
    id: str
    cause: str

    @property
    def sort_key(self) -> tuple[str, str, str]:
        """The order of requirement 17.4: entity, id and cause by code point, not op."""
        return (self.entity, self.id, self.cause)


def to_stored(column: ColumnSchema, value: Value) -> StoredValue:
    """Return ``value`` as ``column`` stores it (design.md data model 3).

    None stays NULL. A decimal becomes the canonical text of its field type, rounded to
    that precision (requirements 4.7 and 4.8), a date its ``YYYY-MM-DD`` text, a bool 0 or
    1, an int itself, and every other type its string.

    Raises TypeError when ``value`` is not of the field type's Python type, so a float, a
    ``datetime`` or a bool in an int column never reaches storage.
    """
    if value is None:
        return None
    field_type = column.type
    match field_type:
        case "qty" | "price" | "money" | "rate":
            if not isinstance(value, Decimal):
                raise TypeError(_wrong_type(column, value, "Decimal"))
            return format_decimal(field_type, value)
        case "date":
            # datetime is a date subclass; its isoformat() is no YYYY-MM-DD.
            if not isinstance(value, date) or isinstance(value, datetime):
                raise TypeError(_wrong_type(column, value, "date"))
            return value.isoformat()
        case "bool":
            if not isinstance(value, bool):
                raise TypeError(_wrong_type(column, value, "bool"))
            return int(value)
        case "int":
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(_wrong_type(column, value, "int"))
            return value
        case _:
            if not isinstance(value, str):
                raise TypeError(_wrong_type(column, value, "str"))
            return value


def from_stored(column: ColumnSchema, stored: StoredValue) -> Value:
    """Return the typed value ``stored`` holds in ``column``, the inverse of :func:`to_stored`.

    NULL stays None. ``to_stored`` of the result gives ``stored`` back, and a value within
    the precision of its field type survives a write and a read unchanged
    (requirement 4.9).
    """
    if stored is None:
        return None
    match column.type:
        case "qty" | "price" | "money" | "rate":
            return Decimal(str(stored))
        case "date":
            return date.fromisoformat(str(stored))
        case "bool":
            return bool(stored)
        case "int":
            return int(stored)
        case _:
            return str(stored)


def _wrong_type(column: ColumnSchema, value: Value, expected: str) -> str:
    return (
        f"{column.name} is a {column.type} field and takes {expected},"
        f" got {type(value).__name__}: {value!r}"
    )


class SandboxStore:
    """Owns the working database of one sandbox. All methods run in a worker thread.

    The database is ``db.sqlite`` below ``sandbox_dir``; :meth:`open` creates the directory
    and the file when they do not exist yet, so an empty store is a database with no
    tables until :meth:`create_tables` runs. The store is closed when the sandbox becomes
    idle (requirement 15.5) and opened again on its next call (requirement 15.6), which
    keeps no state of its own: everything a sandbox remembers is in the file, its counters
    and its effective calls included (design.md data model 2).

    ``db_name`` and ``pragmas`` are the two things the build database of a snapshot does
    differently (design.md 3.3): it is a file of its own in a temporary directory and its
    connection asserts the pragmas that make the ``VACUUM INTO`` output byte-deterministic
    instead of the WAL set of a sandbox. :func:`~erp_mock.state.snapshot.build_snapshot_db`
    is the one caller that passes them.
    """

    __slots__ = (
        "_sandbox_dir",
        "_db_name",
        "_pragmas",
        "_schema",
        "_engine",
        "_driver",
        "_in_transaction",
    )

    def __init__(
        self,
        sandbox_dir: Path,
        schema: StorageSchema,
        *,
        db_name: str = DB_FILENAME,
        pragmas: Sequence[str] = CONNECTION_PRAGMAS,
    ) -> None:
        self._sandbox_dir = sandbox_dir
        self._db_name = db_name
        self._pragmas = tuple(pragmas)
        self._schema = schema
        self._engine: Engine | None = None
        self._driver: sqlite3.Connection | None = None
        self._in_transaction = False

    @property
    def sandbox_dir(self) -> Path:
        """The directory the database and its sidecar files live in."""
        return self._sandbox_dir

    @property
    def db_path(self) -> Path:
        """The working database file, ``<sandbox_dir>/db.sqlite`` by default."""
        return self._sandbox_dir / self._db_name

    @property
    def schema(self) -> StorageSchema:
        """The tables of the instance this sandbox belongs to."""
        return self._schema

    @property
    def is_open(self) -> bool:
        """Whether the database is open."""
        return self._engine is not None

    @property
    def driver_connection(self) -> sqlite3.Connection:
        """The one sqlite3 connection of the store (design.md 3.2).

        Statements run on it are in autocommit, because the driver's ``isolation_level``
        is None: a pragma or a backup takes no transaction and no write lock of its own.
        The snapshot functions copy pages through it (design.md 3.3). Raises RuntimeError
        when the database is not open.
        """
        return self._require_driver()

    def open(self) -> None:
        """Open the working database, asserting the pragmas of design.md 3.2.

        Connects right away, so the file exists, WAL mode is in effect and a later
        transaction cannot fail on the pragmas. Opening an open store does nothing.
        """
        if self._engine is not None:
            return
        self._sandbox_dir.mkdir(parents=True, exist_ok=True)
        # URL.create takes the path verbatim; a URL string would parse '?' and '#' in it.
        engine = create_engine(
            URL.create("sqlite+pysqlite", database=str(self.db_path)),
            # One connection per store, shared by the worker threads the sandbox lock
            # serializes; a pool would open a second one and assert the pragmas again.
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        event.listen(engine, "connect", self._on_connect)
        event.listen(engine, "begin", _begin_immediate)
        self._engine = engine
        try:
            # Forces the connect listener, which creates the file and sets the pragmas.
            engine.connect().close()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close the database; the WAL is checkpointed and its sidecar files removed.

        Closing a closed store does nothing. Raises RuntimeError inside a transaction.
        """
        if self._in_transaction:
            raise RuntimeError(f"cannot close {self.db_path} inside a transaction")
        engine, self._engine, self._driver = self._engine, None, None
        if engine is not None:
            engine.dispose()

    def db_bytes(self) -> int:
        """Return the size of the database: page count times page size (requirement 23.12).

        Read on the driver connection, which is in autocommit, so the call takes no write
        lock and does not disturb a transaction of this store.
        """
        driver = self._require_driver()
        return _pragma_int(driver, "page_count") * _pragma_int(driver, "page_size")

    def create_tables(self) -> None:
        """Create every table of the schema, in the schema's order (design.md 3.1).

        One transaction, so a half-created database cannot stay behind. Raises when a
        table already exists: the DDL sequence of an instance is fixed, which the
        byte-deterministic snapshot depends on (requirement 20.13).
        """
        with self._begin() as connection:
            self._schema.metadata.create_all(connection, checkfirst=False)

    def restore_from(self, source: Path) -> None:
        """Replace the whole database with the content of ``source`` (requirements 19.4, 19.6).

        ``source`` is the sandbox's ``base.sqlite`` for a reset and
        ``named/<name>.sqlite`` for a restore. It is opened read-only and immutable, so
        the file it links to is not touched (requirement 15.13), and the backup API
        replaces every page of this database in place: the records, the counters and the
        effective calls all become the ones of ``source`` (design.md data model 2). A
        connection another thread has open sees the restored content on its next statement.
        The pragmas of this connection are asserted again afterwards, the journal mode
        above all (design.md 3.3).

        Raises RuntimeError when the database is not open or a transaction of it is, and
        FileNotFoundError when ``source`` is not a file.
        """
        driver = self._require_driver()
        if self._in_transaction:
            raise RuntimeError(f"cannot restore {self.db_path} inside a transaction")
        if not source.is_file():
            raise FileNotFoundError(f"no such snapshot: {source}")
        with closing(sqlite3.connect(immutable_uri(source), uri=True)) as origin:
            origin.backup(driver)
        for pragma in self._pragmas:
            driver.execute(pragma)

    def save_to(self, dest: Path) -> None:
        """Save the current content as the new database ``dest`` (requirement 19.5).

        ``dest`` is ``named/<name>.sqlite``, the file of one Named_Snapshot; its directory
        is created when it does not exist yet. The backup API copies every page, so the
        counters and the effective calls of the sandbox are part of it, and the output is
        then switched to the DELETE journal mode, which lets a reader open it read-only
        and immutable (design.md 3.3). This database is only read and stays as it was.

        Raises RuntimeError when the database is not open or a transaction of it is, and
        FileExistsError when ``dest`` is there already; a save that fails leaves no file
        behind.
        """
        driver = self._require_driver()
        if self._in_transaction:
            raise RuntimeError(f"cannot save {self.db_path} inside a transaction")
        if dest.exists():
            raise FileExistsError(f"the snapshot is already there: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(dest, isolation_level=None)) as target:
                driver.backup(target)
                # The copy inherits the journal mode of its pages; DELETE makes it
                # readable through an immutable URI, whatever the source was in.
                target.execute("PRAGMA journal_mode=DELETE")
        except Exception:
            dest.unlink(missing_ok=True)
            raise

    @contextmanager
    def transaction(self) -> Iterator["UnitOfWork"]:
        """Run a transaction and yield the unit of work of it (requirement 12.7).

        ``BEGIN IMMEDIATE`` on entry, COMMIT when the block returns, ROLLBACK when it
        raises, so a failed call leaves the database and its counters as they were
        (requirement 4.12). Raises RuntimeError when a transaction of this store is
        already open.
        """
        with self._begin() as connection:
            yield UnitOfWork(connection, self._schema)

    @contextmanager
    def _begin(self) -> Iterator[Connection]:
        """Begin a transaction on the one connection, refusing to nest."""
        engine = self._require_engine()
        if self._in_transaction:
            raise RuntimeError(f"a transaction of {self.db_path} is already open")
        self._in_transaction = True
        try:
            with engine.begin() as connection:
                yield connection
        finally:
            self._in_transaction = False

    def _on_connect(
        self, dbapi_connection: sqlite3.Connection, _record: ConnectionPoolEntry
    ) -> None:
        """Prepare a new connection: no implicit BEGIN, then the pragmas (V9)."""
        dbapi_connection.isolation_level = None
        for pragma in self._pragmas:
            dbapi_connection.execute(pragma)
        self._driver = dbapi_connection

    def _require_engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError(f"the sandbox database is not open: {self.db_path}")
        return self._engine

    def _require_driver(self) -> sqlite3.Connection:
        if self._driver is None:
            raise RuntimeError(f"the sandbox database is not open: {self.db_path}")
        return self._driver


def immutable_uri(path: Path) -> str:
    """The URI that opens ``path`` read-only and immutable (design.md 3.3).

    An immutable database is read without locks and without change detection, so opening
    it creates no journal, ``-wal`` or ``-shm`` file next to it and leaves its bytes
    exactly as they are (requirement 15.13). Used by every reader of a snapshot file: the
    copy a sandbox is created from, the source of a reset or a restore, and the base of a
    diff. ``Path.as_uri`` percent-encodes the characters a URI cannot carry, so a data
    directory whose name holds a space, a ``#`` or a ``%`` works; the connection must be
    opened with ``uri=True``.
    """
    return f"{path.resolve().as_uri()}?mode=ro&immutable=1"


def _begin_immediate(connection: Connection) -> None:
    """Take the write lock before the first statement of the transaction (V9)."""
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def _pragma_int(driver: sqlite3.Connection, pragma: str) -> int:
    row = driver.execute(f"PRAGMA {pragma}").fetchone()
    return int(row[0])


class UnitOfWork:
    """Repository interface used by domain; values are typed (design.md 3.2).

    One instance per transaction, created by :meth:`SandboxStore.transaction`. The record
    methods address a record table - an entity, a bill, a bill's lines, stock_txn or
    stock_balance - by its ``id`` and see every column of it, the internal ones included
    (``_bill_id``, ``_close_mode``, ``_conversion``), which the engine needs and no result
    shows (requirement 5.12). The engine's own tables have methods of their own:
    :meth:`bump_counter` for ``_counters`` and :meth:`record_effective_call` for
    ``_effective_writes``.
    """

    __slots__ = ("_connection", "_schema", "_cause", "_before", "_written")

    def __init__(self, connection: Connection, schema: StorageSchema) -> None:
        self._connection = connection
        self._schema = schema
        self._cause = DIRECT_CAUSE
        # The row every touched record held before the transaction, None when it did not
        # exist, keyed by (table, id).
        self._before: dict[tuple[str, str], Mapping[str, StoredValue] | None] = {}
        # Which columns each cause wrote, per touched record.
        self._written: dict[tuple[str, str], dict[str, set[str]]] = {}

    # --- reading --------------------------------------------------------------------

    def get(self, table: str, id: str) -> Record | None:
        """Return the record ``id`` of ``table``, or None when there is none."""
        record_table = self._record_table(table)
        row = self._row(record_table, id)
        return None if row is None else _decode(record_table, row)

    def select(
        self,
        table: str,
        where: Mapping[str, Value] = _NO_WHERE,
        like: Mapping[str, str] = _NO_LIKE,
        at_least: Mapping[str, date] = _NO_DATES,
        at_most: Mapping[str, date] = _NO_DATES,
    ) -> list[Record]:
        """Return the records of ``table`` that match, ordered by primary key.

        ``where`` compares columns for equality, a None value matching NULL, and every
        entry must match. ``like`` matches a SQL pattern with ``ESCAPE '\\'`` and at least
        one entry must match, which is how a keyword search compares against each of its
        keyword fields (requirement 5.9); an empty ``like`` does not filter. ``at_least``
        and ``at_most`` are the two ends of a date range, both included, and every entry
        must match: the date filter of a search, whose two parameters may be given one at a
        time (requirement 11.5). Ordering by the primary key makes the result of a read
        deterministic; the sort and the page a search reports are decided on it
        (requirement 5.14).

        Only a date column takes a range, since a date is stored as ``YYYY-MM-DD`` text,
        which SQLite orders chronologically; a decimal column holds canonical text, whose
        order is not the order of the numbers (design.md data model 3).

        Raises KeyError for an unknown table or column and ValueError for a range on a
        column that is no date.
        """
        schema = self._table(table)
        sa_table = self._schema.table(table)
        conditions = [_equals(sa_table, schema, name, value) for name, value in where.items()]
        conditions += [_from_day(sa_table, schema, name, day) for name, day in at_least.items()]
        conditions += [_until_day(sa_table, schema, name, day) for name, day in at_most.items()]
        if like:
            patterns = [_matches(sa_table, schema, name, pattern) for name, pattern in like.items()]
            conditions.append(or_(*patterns))
        statement = (
            sql_select(sa_table).where(*conditions).order_by(*_key_columns(sa_table, schema))
        )
        return [_decode(schema, row) for row in self._fetch(statement)]

    # --- writing --------------------------------------------------------------------

    def insert(self, table: str, record: Mapping[str, Value]) -> None:
        """Insert ``record`` into ``table``; columns it omits become NULL.

        Raises KeyError for an unknown table or column, ValueError when ``record`` has no
        ``id``, and sqlalchemy.exc.IntegrityError when that id is taken.
        """
        schema = self._record_table(table)
        record_id = _record_id(schema, record)
        values = {name: to_stored(_column(schema, name), value) for name, value in record.items()}
        # An insert writes the whole row, the columns it leaves NULL included.
        self._touch(schema, record_id, schema.columns.keys())
        self._connection.execute(sql_insert(self._schema.table(table)).values(values))

    def update(self, table: str, id: str, changes: Mapping[str, Value]) -> None:
        """Write ``changes`` to the record ``id`` of ``table``, leaving its other columns.

        An empty ``changes`` and an id that is not there change nothing. Raises KeyError
        for an unknown table or column.
        """
        schema = self._record_table(table)
        values = {name: to_stored(_column(schema, name), value) for name, value in changes.items()}
        self._touch(schema, id, values)
        if not values:
            return
        sa_table = self._schema.table(table)
        self._connection.execute(
            sql_update(sa_table).where(sa_table.c[ID_COLUMN] == id).values(values)
        )

    def delete(self, table: str, id: str) -> None:
        """Delete the record ``id`` of ``table``; an id that is not there changes nothing."""
        schema = self._record_table(table)
        self._touch(schema, id, schema.columns.keys())
        sa_table = self._schema.table(table)
        self._connection.execute(sql_delete(sa_table).where(sa_table.c[ID_COLUMN] == id))

    def bump_counter(self, kind: CounterKind, key: str) -> int:
        """Raise the counter (``kind``, ``key``) by one and return its new value (req 4.6).

        A counter that was never bumped counts as 0, so the first call returns 1: the
        sequence number of the first internal ID, code or bill number of its key, and of
        the first write operation of the sandbox (requirements 4.1, 4.3, 4.4 and 4.11).
        The counters live in the sandbox database, so a sandbox created from a Snapshot
        continues its counters and a failed call rolls them back with everything else
        (requirement 4.12).
        """
        counters = self._schema.table(COUNTERS_TABLE)
        statement = (
            sqlite_insert(counters)
            .values(kind=kind, key=key, value=1)
            .on_conflict_do_update(
                index_elements=["kind", "key"],
                # Unqualified in SQLite's upsert: the value the existing row holds.
                set_={"value": counters.c.value + 1},
            )
            .returning(counters.c.value)
        )
        return int(self._connection.execute(statement).scalar_one())

    # --- what the call changed ------------------------------------------------------

    @contextmanager
    def cause(self, cause: str) -> Iterator[None]:
        """Attribute the writes of the block to ``cause`` (requirement 17.4).

        ``cause`` is :data:`DIRECT_CAUSE` or the id of the linkage or writeback that is
        running. Blocks nest: the previous cause is restored on exit, also when the block
        raises.
        """
        previous = self._cause
        self._cause = cause
        try:
            yield
        finally:
            self._cause = previous

    def net_writes(self) -> tuple[Write, ...]:
        """Return the net change of each (record, cause) of this transaction (req 17.4).

        A record that did not exist before the transaction and does now is an insert, one
        that existed and does not any more a delete, and a record on both sides an update
        when a column that cause wrote differs. Everything else is left out: a record
        inserted and deleted again, one whose value was changed back, and a cause that
        only read. The items are ordered by entity, id and cause.
        """
        writes: list[Write] = []
        for (table, record_id), causes in self._written.items():
            before = self._before[(table, record_id)]
            after = self._row(self._table(table), record_id)
            for cause, columns in causes.items():
                op = _net_op(before, after, columns)
                if op is not None:
                    writes.append(Write(entity=table, op=op, id=record_id, cause=cause))
        return tuple(sorted(writes, key=lambda write: write.sort_key))

    def record_effective_call(self, seq: int, writes: Sequence[Write]) -> None:
        """Record that the call ``seq`` wrote the records of ``writes`` (requirement 18.10).

        One row per (seq, entity, id), so a record several causes changed is listed once.
        The rows are written in this transaction and roll back with it, which keeps a
        failed call out of the effective calls the diff reads its ``cause_seqs`` from
        (requirement 18.3). Nothing is written for empty ``writes``.
        """
        records = sorted({(write.entity, write.id) for write in writes})
        if not records:
            return
        rows = [
            {"seq": seq, "entity": entity, ID_COLUMN: record_id} for entity, record_id in records
        ]
        self._connection.execute(sql_insert(self._schema.table(EFFECTIVE_WRITES_TABLE)), rows)

    # --- the writeback ledger (design.md 4.2) ---------------------------------------

    def record_writebacks(self, rows: Sequence[Mapping[str, Value]]) -> None:
        """Add ``rows`` to the writeback ledger, one per amount a writeback added.

        The ledger says what the approve of one pushed bill added to which field of which
        source record, so the unapprove of that bill takes exactly those amounts off again
        (design.md 4.2); every row carries the keys of
        :data:`~erp_mock.state.schema.WRITEBACK_LEDGER_TABLE`. The rows are written in the
        transaction of the operation and roll back with it. Nothing is written for empty
        ``rows``.

        Raises KeyError for a key that is no column of the ledger and
        sqlalchemy.exc.IntegrityError when a (target bill, writeback, position) is taken.
        """
        if not rows:
            return
        schema = self._table(WRITEBACK_LEDGER_TABLE)
        stored = [
            {name: to_stored(_column(schema, name), value) for name, value in row.items()}
            for row in rows
        ]
        self._connection.execute(sql_insert(self._schema.table(WRITEBACK_LEDGER_TABLE)), stored)

    def clear_writebacks(self, target_bill_id: str) -> None:
        """Remove every ledger row of the bill ``target_bill_id`` (design.md 4.2).

        What an unapprove runs after it has taken the amounts of those rows off their
        source records; a bill with no row changes nothing.
        """
        ledger = self._schema.table(WRITEBACK_LEDGER_TABLE)
        self._connection.execute(
            sql_delete(ledger).where(ledger.c[WRITEBACK_TARGET_COLUMN] == target_bill_id)
        )

    # --- internals ------------------------------------------------------------------

    def _table(self, table: str) -> TableSchema:
        """The schema of ``table``; raises KeyError when the instance has no such table."""
        try:
            return self._schema.tables[table]
        except KeyError:
            raise KeyError(f"no such table: {table}") from None

    def _record_table(self, table: str) -> TableSchema:
        """The schema of the record table ``table``, the only kind addressed by id."""
        schema = self._table(table)
        if schema.primary_key != (ID_COLUMN,):
            raise KeyError(f"{table} is not a record table keyed by {ID_COLUMN}")
        return schema

    def _row(self, table: TableSchema, record_id: str) -> Mapping[str, StoredValue] | None:
        """The stored row ``record_id`` of ``table``, or None when there is none."""
        sa_table = self._schema.table(table.name)
        statement = sql_select(sa_table).where(sa_table.c[ID_COLUMN] == record_id)
        rows = self._fetch(statement)
        return rows[0] if rows else None

    def _fetch(self, statement: Executable) -> list[Mapping[str, StoredValue]]:
        """Run ``statement`` and return its rows as stored values keyed by column name."""
        result = self._connection.execute(statement)
        columns = [str(name) for name in result.keys()]
        return [dict(zip(columns, row, strict=True)) for row in result.all()]

    def _touch(self, table: TableSchema, record_id: str, columns: Iterable[str]) -> None:
        """Save the record's pre-transaction row on first touch and mark ``columns``."""
        key = (table.name, record_id)
        if key not in self._before:
            self._before[key] = self._row(table, record_id)
        causes = self._written.setdefault(key, {})
        causes.setdefault(self._cause, set()).update(columns)


def _net_op(
    before: Mapping[str, StoredValue] | None,
    after: Mapping[str, StoredValue] | None,
    columns: Iterable[str],
) -> WriteOp | None:
    """The net change of one (record, cause), None when there is none (requirement 17.4).

    Stored values are compared, which is comparing the values themselves: one number has
    one canonical text (design.md 3.1).
    """
    if before is None:
        return "insert" if after is not None else None
    if after is None:
        return "delete"
    if any(before.get(column) != after.get(column) for column in columns):
        return "update"
    return None


def _decode(table: TableSchema, row: Mapping[str, StoredValue]) -> Record:
    """The stored ``row`` of ``table`` as typed values, columns in DDL order."""
    return FrozenMap(
        (name, from_stored(column, row[name])) for name, column in table.columns.items()
    )


def _column(table: TableSchema, name: str) -> ColumnSchema:
    try:
        return table.columns[name]
    except KeyError:
        raise KeyError(f"no such column: {table.name}.{name}") from None


def _record_id(table: TableSchema, record: Mapping[str, Value]) -> str:
    record_id = record.get(ID_COLUMN)
    if not isinstance(record_id, str):
        raise ValueError(f"a record of {table.name} needs a str {ID_COLUMN}, got {record_id!r}")
    return record_id


def _equals(sa_table: Table, table: TableSchema, name: str, value: Value) -> ColumnElement[bool]:
    """``name`` equals ``value``, or is NULL when ``value`` is None."""
    stored = to_stored(_column(table, name), value)
    column = sa_table.c[name]
    return column.is_(None) if stored is None else column == stored


def _matches(sa_table: Table, table: TableSchema, name: str, pattern: str) -> ColumnElement[bool]:
    """``name`` matches the SQL pattern ``pattern``, with a backslash as escape."""
    _column(table, name)
    return sa_table.c[name].like(pattern, escape=_LIKE_ESCAPE)


def _from_day(sa_table: Table, table: TableSchema, name: str, day: date) -> ColumnElement[bool]:
    """``name`` is on or after ``day``, the lower end of a date range (requirement 11.5)."""
    return sa_table.c[name] >= _day_text(table, name, day)


def _until_day(sa_table: Table, table: TableSchema, name: str, day: date) -> ColumnElement[bool]:
    """``name`` is on or before ``day``, the upper end of a date range (requirement 11.5)."""
    return sa_table.c[name] <= _day_text(table, name, day)


def _day_text(table: TableSchema, name: str, day: date) -> str:
    """``day`` as the date column ``name`` stores it; raises for a column of another type."""
    column = _column(table, name)
    if column.type != "date":
        raise ValueError(
            f"{table.name}.{name} is a {column.type} field; only a date field takes a range"
        )
    stored = to_stored(column, day)
    if not isinstance(stored, str):
        raise TypeError(_wrong_type(column, day, "date"))
    return stored


def _key_columns(sa_table: Table, table: TableSchema) -> list[ColumnElement[object]]:
    """The primary key columns of ``table``, in key order."""
    return [sa_table.c[name] for name in table.primary_key]
