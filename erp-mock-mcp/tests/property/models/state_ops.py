"""Random record-level writes on the fixture tables, and the state they leave behind.

The diff properties all work the same way: a basis built once from :data:`BASE_CONTENT`, a
sandbox created from that Snapshot per example, and a drawn plan of record-level inserts,
updates and deletes run through the unit of work - one transaction and one trace seq per
call, as a tools/call runs (requirement 12.7). This module draws such plans, runs them and
reads the content of either side back, so a test is left with the assertions of its
property::

    basis = build_basis(workdir, schema)            # once per module
    with sandbox_from(basis, parent, schema) as store:
        run_plan(store, data.draw(plans(schema)))
        current = current_state(store)

Which tables the plans write to is :data:`OP_TABLES`: an entity, a bill, its lines and the
two inventory tables of the fixture, which together carry every field type the fixture
declares - string, text, int, bool, enum, date, ref and the four decimal kinds (period is
the one type no fixture column has). Five tables keep an example small; the diff still
compares every table of the instance, so the ones no plan writes to must stay out of its
output.

Ids come from :data:`ID_POOL`, a handful per table: two the basis holds and one or two it
does not. The pool is small on purpose, so a drawn plan deletes and re-inserts a record,
changes one and changes it back, or writes a value the basis already has - the cases
requirement 18.2 asks to report as no change at all. The values of :data:`VALUES` are
small pools of their field type for the same reason, with decimals that are equal but
written differently ("1700" and "1700.00") and decimals that round to the precision of
their field (money 0.005 is stored as 0.01).

Reading back. :func:`current_state` reads the sandbox through the unit of work, which
decodes stored values into typed ones, and :func:`snapshot_state` reads a Snapshot file
read-only and immutable, so its bytes stay as they are (requirement 15.13); both then
write every value the way requirement 18.5 does, through :func:`formatted`. A state is
therefore what a diff of it against an empty basis would report, keyed by table and id.
"""

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, assert_never

from hypothesis import strategies as st

from erp_mock.common.decimals import format_decimal
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.fields import FIELD_TYPES, FieldType
from erp_mock.state.schema import (
    ID_COLUMN,
    STOCK_BALANCE_TABLE,
    STOCK_TXN_TABLE,
    ColumnSchema,
    StorageSchema,
    TableSchema,
)
from erp_mock.state.snapshot import (
    BASE_DB_FILENAME,
    build_snapshot_db,
    copy_snapshot,
    finalize_snapshot,
    link_snapshot,
)
from erp_mock.state.store import (
    DB_FILENAME,
    SandboxStore,
    UnitOfWork,
    Value,
    from_stored,
    immutable_uri,
)

__all__ = [
    "BASE_CONTENT",
    "ID_POOL",
    "JSON_TYPES",
    "OP_TABLES",
    "VALUES",
    "Delete",
    "Fields",
    "Insert",
    "Op",
    "Plan",
    "State",
    "Update",
    "build_basis",
    "current_state",
    "fields_of",
    "formatted",
    "plans",
    "run_plan",
    "sandbox_from",
    "snapshot_state",
]

# The tables of the fixture the drawn plans write to (see the module docstring).
VENDOR: Final = "vendor"
ORDER_TABLE: Final = "supply_order"
LINE_TABLE: Final = "supply_order_line"
OP_TABLES: Final[tuple[str, ...]] = (
    VENDOR,
    ORDER_TABLE,
    LINE_TABLE,
    STOCK_TXN_TABLE,
    STOCK_BALANCE_TABLE,
)

# The ids a plan may write, per table: the two of :data:`BASE_CONTENT` first, then the ones
# the basis does not hold. ``ven_甲`` is there for the order of requirement 18.6: an id
# outside ASCII sorts by code point, which SQLite's BINARY collation on UTF-8 gives.
ID_POOL: Final[Mapping[str, tuple[str, ...]]] = {
    VENDOR: ("ven_000001", "ven_000002", "ven_000003", "ven_甲"),
    ORDER_TABLE: ("so_000001", "so_000002", "so_000003"),
    LINE_TABLE: ("so_000001-1", "so_000001-2", "so_000002-1"),
    STOCK_TXN_TABLE: (
        "ge_000001-1:dep_000001:in",
        "ge_000001-1:dep_000002:out",
        "ge_000002-1:dep_000001:in",
    ),
    STOCK_BALANCE_TABLE: (
        "art_000031:dep_000001",
        "art_000032:dep_000001",
        "art_000031:dep_000002",
    ),
}

# What the basis holds: two records per table of :data:`OP_TABLES`, written through the unit
# of work when the Snapshot is built. An initial dataset has no effective call
# (requirement 18.10), so the build records none.
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
            "status": "disabled",
            "name": "二号供应商",
            "contact": "乙",
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
        ORDER_TABLE,
        {
            "id": "so_000002",
            "number": "SUP202610090002",
            "bill_type": ORDER_TABLE,
            "org_id": "br_000001",
            "biz_date": date(2026, 10, 9),
            "status": "draft",
            "created_seq": 2,
            "updated_seq": 2,
            "price_includes_tax": True,
            "remark": "备注",
            "vendor_id": "ven_000002",
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
            "_bill_id": "so_000001",
        },
    ),
    (
        LINE_TABLE,
        {
            "id": "so_000001-2",
            "line_no": 2,
            "qty": Decimal("0.5"),
            "tax_rate": Decimal("0.13"),
            "article_id": "art_000032",
            "_bill_id": "so_000001",
        },
    ),
    (
        STOCK_TXN_TABLE,
        {
            "id": "ge_000001-1:dep_000001:in",
            "material_id": "art_000031",
            "warehouse_id": "dep_000001",
            "qty_delta": Decimal("300"),
            "source_bill_type": "goods_entry",
            "source_line_id": "ge_000001-1",
        },
    ),
    (
        STOCK_TXN_TABLE,
        {
            "id": "ge_000001-1:dep_000002:out",
            "material_id": "art_000031",
            "warehouse_id": "dep_000002",
            "qty_delta": Decimal("-300"),
            "source_bill_type": "goods_entry",
            "source_line_id": "ge_000001-1",
        },
    ),
    (
        STOCK_BALANCE_TABLE,
        {
            "id": "art_000031:dep_000001",
            "material_id": "art_000031",
            "warehouse_id": "dep_000001",
            "qty_base": Decimal("300"),
        },
    ),
    (
        STOCK_BALANCE_TABLE,
        {
            "id": "art_000032:dep_000001",
            "material_id": "art_000032",
            "warehouse_id": "dep_000001",
            "qty_base": Decimal("0"),
        },
    ),
)

# --- drawn values ------------------------------------------------------------------------

# The values a column of each field type may get, every type with an absent value among
# them (requirement 18.5 writes it as null). The pools are small, so a plan writes a value
# the record already has often enough; the decimals cover values that are equal but written
# differently and values that round to the precision of their field type (requirement 4.7).
VALUES: Final[Mapping[FieldType, st.SearchStrategy[Value]]] = {
    "string": st.sampled_from([None, "", "V0001", "甲"]),
    "text": st.sampled_from([None, "", "备注", "两行\n备注"]),
    "int": st.sampled_from([None, 0, 1, 30]),
    "bool": st.sampled_from([None, True, False]),
    "enum": st.sampled_from([None, "enabled", "disabled", "approved"]),
    "date": st.sampled_from([None, date(2026, 10, 9), date(2026, 10, 16)]),
    "period": st.sampled_from([None, "2026-10", "2026-11"]),
    "qty": st.sampled_from([None, Decimal("0"), Decimal("2000"), Decimal("0.5")]),
    "price": st.sampled_from([None, Decimal("0.85"), Decimal("0.850000"), Decimal("1.0000005")]),
    "money": st.sampled_from([None, Decimal("1700"), Decimal("1700.00"), Decimal("0.005")]),
    "rate": st.sampled_from([None, Decimal("0.13"), Decimal("0.130"), Decimal("0")]),
    "ref": st.sampled_from([None, "ven_000001", "art_000031", "dep_000001"]),
}
# Every field type has a pool, so a table of any instance can be written to.
assert VALUES.keys() == set(FIELD_TYPES)

# How many columns one update writes. Few columns per update, so two updates of one record
# often meet in the same column and the second may write the value the first replaced.
MAX_CHANGED_COLUMNS: Final = 3

# --- plans -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Insert:
    """Write every field of the record ``id``, whether it is there or not.

    A record the table does not hold is inserted; one it holds has every field written
    again, which is the record-level write an insert of a taken id amounts to (the unit of
    work refuses the insert itself).
    """

    table: str
    id: str
    fields: Mapping[str, Value]


@dataclass(frozen=True, slots=True)
class Update:
    """Write the drawn fields of the record ``id``, leaving its other fields.

    An id the table does not hold changes nothing, as the unit of work has it.
    """

    table: str
    id: str
    fields: Mapping[str, Value]


@dataclass(frozen=True, slots=True)
class Delete:
    """Delete the record ``id``; an id the table does not hold changes nothing."""

    table: str
    id: str


# One record-level write, what a call is made of.
type Op = Insert | Update | Delete
# The writes of one call, which run in one transaction under one trace seq.
type Call = tuple[Op, ...]
# A sequence of calls, run one after another on a fresh sandbox.
type Plan = tuple[Call, ...]


def plans(
    schema: StorageSchema, *, max_calls: int = 4, max_ops: int = 4
) -> st.SearchStrategy[Plan]:
    """Plans of at most ``max_calls`` calls, each of at most ``max_ops`` record writes.

    Both bounds are small, because every example builds a sandbox of its own on disk. An
    empty call and an empty plan are drawn as well: a call that writes nothing is no
    effective call (requirement 18.10) and an empty plan leaves the sandbox at the basis.
    """
    calls = st.lists(ops(schema), max_size=max_ops).map(tuple)
    return st.lists(calls, max_size=max_calls).map(tuple)


def ops(schema: StorageSchema) -> st.SearchStrategy[Op]:
    """One record write on one table of :data:`OP_TABLES`."""
    return st.one_of([table_ops(schema.tables[name]) for name in OP_TABLES])


def table_ops(table: TableSchema) -> st.SearchStrategy[Op]:
    """An insert, an update or a delete of one of the ids ``table`` has a pool for."""
    ids = st.sampled_from(ID_POOL[table.name])
    return st.one_of(
        st.builds(Insert, st.just(table.name), ids, records(table)),
        st.builds(Update, st.just(table.name), ids, changes(table)),
        st.builds(Delete, st.just(table.name), ids),
    )


def records(table: TableSchema) -> st.SearchStrategy[Mapping[str, Value]]:
    """A value for every column of ``table`` but ``id``, the internal ones included."""
    return st.fixed_dictionaries(
        {column.name: VALUES[column.type] for column in _written_columns(table)}
    )


@st.composite
def changes(draw: st.DrawFn, table: TableSchema) -> Mapping[str, Value]:
    """Draw the fields of one update: a few columns of ``table``, or none at all.

    The internal columns are drawn like the others, so a plan writes updates that change an
    internal column alone, which is no change of the record (requirement 18.4).
    """
    names = [column.name for column in _written_columns(table)]
    chosen = draw(st.lists(st.sampled_from(names), unique=True, max_size=MAX_CHANGED_COLUMNS))
    return {name: draw(VALUES[table.columns[name].type]) for name in chosen}


def _written_columns(table: TableSchema) -> tuple[ColumnSchema, ...]:
    """The columns a write may set: every column but the primary key."""
    return tuple(column for column in table.columns.values() if column.name != ID_COLUMN)


# --- building the basis and running a plan -----------------------------------------------


def build_basis(workdir: Path, schema: StorageSchema) -> Path:
    """Build the Snapshot of :data:`BASE_CONTENT` below ``workdir`` and return its path.

    The way the Snapshot cache builds one (design.md 6.1): the records are written through
    the unit of work into a build database and ``finalize_snapshot`` writes the file the
    sandboxes are created from. Call once per module and create a sandbox per example with
    :func:`sandbox_from`.
    """
    build_db = workdir / "build" / "build.sqlite"
    store = build_snapshot_db(build_db, schema)
    try:
        with store.transaction() as uow:
            for table, record in BASE_CONTENT:
                uow.insert(table, record)
    finally:
        store.close()
    snapshot = workdir / "snapshots" / "basis.sqlite"
    finalize_snapshot(build_db, snapshot)
    return snapshot


@contextmanager
def sandbox_from(snapshot: Path, parent: Path, schema: StorageSchema) -> Iterator[SandboxStore]:
    """Yield an open sandbox created from ``snapshot``, in a directory of its own below ``parent``.

    What requirement 15.1 gives a new sandbox: ``db.sqlite`` copied from the Snapshot and
    ``base.sqlite`` linked beside it, the basis of an ``initial`` diff. The directory is
    removed when the block leaves, so a run of many examples does not keep them all. Its
    name holds a space, because both databases are read through a URI (design.md 3.4).
    """
    with TemporaryDirectory(dir=parent, prefix="sb ") as made:
        sandbox_dir = Path(made)
        copy_snapshot(snapshot, sandbox_dir / DB_FILENAME)
        link_snapshot(snapshot, sandbox_dir / BASE_DB_FILENAME)
        store = SandboxStore(sandbox_dir, schema)
        store.open()
        try:
            yield store
        finally:
            store.close()


def run_plan(store: SandboxStore, plan: Plan, first_seq: int = 1) -> int:
    """Run ``plan`` on ``store``, one transaction per call, and return the next trace seq.

    Each call runs its writes in one transaction and records itself as the effective call of
    its seq, which is what the Sandbox_Manager does for a tools_call event with a non-empty
    ``writes`` (requirement 18.10); a call whose net writes are empty records nothing.
    """
    seq = first_seq
    for call in plan:
        with store.transaction() as uow:
            for op in call:
                apply_op(uow, op)
            uow.record_effective_call(seq, uow.net_writes())
        seq += 1
    return seq


def apply_op(uow: UnitOfWork, op: Op) -> None:
    """Run one record write of a call through ``uow``."""
    match op:
        case Insert(table=table, id=record_id, fields=fields):
            if uow.get(table, record_id) is None:
                uow.insert(table, {ID_COLUMN: record_id, **fields})
            else:
                uow.update(table, record_id, fields)
        case Update(table=table, id=record_id, fields=fields):
            uow.update(table, record_id, fields)
        case Delete(table=table, id=record_id):
            uow.delete(table, record_id)
        case _:
            assert_never(op)


# --- reading a state back ----------------------------------------------------------------

# The non-internal fields of one record but ``id``, as requirement 18.5 writes them.
type Fields = dict[str, JsonValue]
# The content of every table the diff covers, by table name and record id.
type State = dict[str, dict[str, Fields]]

# The JSON type of a value of each field type (requirement 18.5). bool is a subclass of
# int, so a check of the type must be ``type(value) is JSON_TYPES[...]``.
JSON_TYPES: Final[Mapping[FieldType, type]] = {
    field_type: bool if field_type == "bool" else int if field_type == "int" else str
    for field_type in FIELD_TYPES
}


def formatted(column: ColumnSchema, value: Value) -> JsonValue:
    """The typed ``value`` of ``column`` as requirement 18.5 writes it.

    A decimal is the canonical text of its field type, a date ``YYYY-MM-DD``, an int a JSON
    integer, a bool a JSON boolean, and an enum code, a reference, a period and a string
    themselves; an absent value is null.
    """
    field_type = column.type
    if value is None:
        return None
    match field_type:
        case "qty" | "price" | "money" | "rate":
            assert isinstance(value, Decimal)
            return format_decimal(field_type, value)
        case "date":
            assert isinstance(value, date)
            return value.isoformat()
        case "bool":
            assert isinstance(value, bool)
            return value
        case "int":
            assert isinstance(value, int)
            return value
        case _:
            assert isinstance(value, str)
            return value


def fields_of(table: TableSchema, record: Mapping[str, Value]) -> Fields:
    """The non-internal fields of ``record`` but ``id``, keyed and written as a diff has them."""
    return {
        column.name: formatted(column, record[column.name])
        for column in table.public_columns
        if column.name != ID_COLUMN
    }


def current_state(store: SandboxStore) -> State:
    """The content of every table the diff covers, read through the unit of work.

    The records come back as typed values, which :func:`formatted` writes the way
    requirement 18.5 does, so the result is comparable with what a diff reports.
    """
    with store.transaction() as uow:
        return {
            table.name: {
                str(record[ID_COLUMN]): fields_of(table, record)
                for record in uow.select(table.name)
            }
            for table in store.schema.diffable_tables
        }


def snapshot_state(snapshot: Path, schema: StorageSchema) -> State:
    """The content of every table the diff covers, read from the Snapshot file ``snapshot``.

    The file is opened read-only and immutable, so its bytes stay as they are and it gets no
    sidecar file (requirement 15.13); the stored values are decoded like the unit of work
    decodes them and then written as requirement 18.5 writes them.
    """
    with closing(sqlite3.connect(immutable_uri(snapshot), uri=True)) as connection:
        return {table.name: _stored_rows(connection, table) for table in schema.diffable_tables}


def _stored_rows(connection: sqlite3.Connection, table: TableSchema) -> dict[str, Fields]:
    """The non-internal columns of every record of ``table``, by id."""
    columns = table.public_columns
    projection = ", ".join(_quoted(column.name) for column in columns)
    found: dict[str, Fields] = {}
    for row in connection.execute(f"SELECT {projection} FROM {_quoted(table.name)}"):
        record = _decoded(columns, row)
        found[str(record[ID_COLUMN])] = fields_of(table, record)
    return found


def _decoded(columns: Sequence[ColumnSchema], row: Sequence[str | int | None]) -> dict[str, Value]:
    """The stored ``row`` as typed values, keyed by column name."""
    return {
        column.name: from_stored(column, stored)
        for column, stored in zip(columns, row, strict=True)
    }


def _quoted(identifier: str) -> str:
    """``identifier`` as a quoted SQL name, so a table or column may be any word."""
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'
