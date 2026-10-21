"""The SQLite tables of one instance, generated from the domain model (design.md 3.1).

:func:`build_schema` turns the resolved configuration into a :class:`StorageSchema`: the
SQLAlchemy ``MetaData`` the store creates the tables from, together with what every table
holds - the column names, their value types and which of them are internal, which the unit
of work needs to convert values and the diff to compare them. Which tables there are and
which columns each has is design.md data model 3:

- one table per entity and per bill, named after it, and one named ``<bill>_line`` per bill
  for its lines; every column is the storage name of one field of that level's field view
  (:attr:`~erp_mock.config.model.FieldModel.column`), so the engine takes them from the
  configuration alone (requirement 1.10);
- the internal columns the engine keeps on a bill (``_close_mode``, ``_conversion``) and on
  its lines (``_bill_id``, indexed), which no result and no diff shows (requirements 5.12
  and 18.4);
- the engine's own ``stock_txn`` and ``stock_balance``, whose column names are fixed names
  of the engine rather than of a business pack; requirement 2.7 keeps the configuration
  from declaring either table name;
- the internal tables ``_counters`` (requirement 4.6), ``_effective_writes``
  (requirement 18.10) and ``_writeback_ledger``, which the diff does not cover
  (requirement 18.4) and whose names start with the underscore no configured name may
  start with.

What this module settles, with the byte-deterministic snapshot of requirement 20.13 in
view - its ``VACUUM INTO`` output only stays identical while every build runs the same DDL
sequence (design.md 3.3):

- the tables are created in code point order of their names, which depends on the
  configuration alone: ``MetaData`` keeps its insertion order and no table has a foreign
  key, so ``create_all`` emits the DDL in exactly that order;
- a record table is ``WITHOUT ROWID`` with ``id`` as its primary key, so ``ORDER BY id``
  is the code point order of the ids (design.md 3.1);
- every value is stored in one of two SQLite types: INTEGER for int and for bool (0 or 1),
  TEXT for everything else, decimals as the canonical text of
  :func:`~erp_mock.common.decimals.format_decimal` (requirement 4.8), dates as
  ``YYYY-MM-DD`` and periods as ``YYYY-MM``. Equal decimals have equal text, so the diff
  compares stored values directly (design.md 3.1);
- of a record table only the primary key is NOT NULL, and no table has a UNIQUE or FOREIGN
  KEY constraint: required fields and uniqueness are checked by the call pipeline, which
  keeps the order of the errors a call reports with the pipeline (requirement 5.8), and an
  absent value is stored as NULL (requirement 18.5). The internal tables, whose rows the
  engine writes itself, keep the DDL of design.md data model 3: every column NOT NULL.
"""

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import Column, Index, Integer, MetaData, Table, Text
from sqlalchemy.types import TypeEngine

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.fields import FieldType
from erp_mock.config.model import (
    STOCK_BALANCE,
    STOCK_TXN,
    BillModel,
    DomainModel,
    EntityModel,
    FieldModel,
)

__all__ = [
    "BILL_ID_COLUMN",
    "CLOSE_MODE_COLUMN",
    "CONVERSION_COLUMN",
    "COUNTERS_TABLE",
    "EFFECTIVE_WRITES_TABLE",
    "ID_COLUMN",
    "INTERNAL_PREFIX",
    "MATERIAL_COLUMN",
    "QTY_BASE_COLUMN",
    "QTY_DELTA_COLUMN",
    "SOURCE_BILL_TYPE_COLUMN",
    "SOURCE_LINE_COLUMN",
    "STOCK_BALANCE_TABLE",
    "STOCK_TXN_TABLE",
    "WAREHOUSE_COLUMN",
    "WRITEBACK_AMOUNT_COLUMN",
    "WRITEBACK_COLUMN",
    "WRITEBACK_FIELD_COLUMN",
    "WRITEBACK_LEDGER_TABLE",
    "WRITEBACK_ORD_COLUMN",
    "WRITEBACK_SOURCE_ID_COLUMN",
    "WRITEBACK_SOURCE_TABLE_COLUMN",
    "WRITEBACK_TARGET_COLUMN",
    "ColumnSchema",
    "CounterKind",
    "StorageSchema",
    "TableKind",
    "TableSchema",
    "build_schema",
]

# The primary key of every record table (meta-model.md section 4).
ID_COLUMN: Final = "id"

# The engine's own record tables, named by the configuration layer, which keeps a
# configured name from colliding with either of them (requirement 2.7,
# :data:`~erp_mock.config.model.ENGINE_RECORDS`). Aliased here as table names, the role
# they have in this layer.
STOCK_TXN_TABLE: Final = STOCK_TXN
STOCK_BALANCE_TABLE: Final = STOCK_BALANCE

# The engine's internal tables. A name of the configuration matches ``^[a-z][a-z0-9_]*$``,
# so the prefix alone keeps these three and the internal columns out of its way.
INTERNAL_PREFIX: Final = "_"
COUNTERS_TABLE: Final = "_counters"
EFFECTIVE_WRITES_TABLE: Final = "_effective_writes"
WRITEBACK_LEDGER_TABLE: Final = "_writeback_ledger"

# The internal columns of a bill: how it was closed (manual or auto, design.md 4.2) and the
# conversion that generated it, which the push quota is counted per (design.md 4.2).
CLOSE_MODE_COLUMN: Final = "_close_mode"
CONVERSION_COLUMN: Final = "_conversion"
# The internal column of a bill's lines: the bill they belong to.
BILL_ID_COLUMN: Final = "_bill_id"

# The columns of the engine's two inventory tables, fixed names of the engine rather than
# of a business pack (requirement 9.1, design.md data model 3): the (material, warehouse) a
# transaction and a balance are kept per, the quantity a transaction moves, where it came
# from, and the quantity a balance holds, which is the ``qty_base`` of
# :mod:`erp_mock.domain.unit_conversion` - a balance is kept in the base unit of its
# material (requirement 9.7).
MATERIAL_COLUMN: Final = "material_id"
WAREHOUSE_COLUMN: Final = "warehouse_id"
QTY_DELTA_COLUMN: Final = "qty_delta"
SOURCE_BILL_TYPE_COLUMN: Final = "source_bill_type"
SOURCE_LINE_COLUMN: Final = "source_line_id"
QTY_BASE_COLUMN: Final = "qty_base"

# The columns of the writeback ledger (design.md data model 3): the bill whose approve
# wrote the row and the writeback that produced it, the position of the row within that
# writeback, the record and the field the amount was added to, and the amount itself as
# the canonical text of its field type (requirement 4.8).
WRITEBACK_TARGET_COLUMN: Final = "target_bill_id"
WRITEBACK_COLUMN: Final = "writeback"
WRITEBACK_ORD_COLUMN: Final = "ord"
WRITEBACK_SOURCE_TABLE_COLUMN: Final = "source_table"
WRITEBACK_SOURCE_ID_COLUMN: Final = "source_id"
WRITEBACK_FIELD_COLUMN: Final = "field"
WRITEBACK_AMOUNT_COLUMN: Final = "amount"

# What a counter counts (requirement 4.6): internal IDs and codes per entity or bill type,
# bill numbers per (number prefix, date), and the operation sequence number of the sandbox.
CounterKind = Literal["id", "code", "number", "op"]

# What a table holds: the records of an entity, the headers or the lines of a bill, one of
# the engine's two inventory tables, or the engine's internal bookkeeping.
type TableKind = Literal["entity", "bill", "line", "stock", "internal"]

# The field types stored as INTEGER; every other type is TEXT (design.md data model 3).
_INTEGER_TYPES: Final = frozenset({"int", "bool"})


@dataclass(frozen=True, slots=True)
class ColumnSchema:
    """One column: the name a value is stored under and the type it is stored as.

    ``type`` is the field type of the stored value, for a column of an internal table and
    for an internal column the type the engine keeps it in; it decides the SQLite type and
    how the unit of work and the diff read the value (requirement 18.5).
    """

    name: str
    type: FieldType
    primary_key: bool = False

    @property
    def internal(self) -> bool:
        """Whether the column is internal, which no result and no diff shows (req 18.4)."""
        return self.name.startswith(INTERNAL_PREFIX)


@dataclass(frozen=True, slots=True)
class TableSchema:
    """One table: its name, what it holds and its columns in DDL order."""

    name: str
    kind: TableKind
    columns: FrozenMap[str, ColumnSchema]

    @property
    def diffable(self) -> bool:
        """Whether the diff covers the table: every table but an internal one (req 18.4).

        The name of a diffable table is the ``entity`` of its changes: the entity name, the
        bill name, ``<bill>_line``, stock_txn or stock_balance.
        """
        return self.kind != "internal"

    @property
    def primary_key(self) -> tuple[str, ...]:
        """The primary key columns, in DDL order; ``id`` alone for a record table."""
        return tuple(name for name, column in self.columns.items() if column.primary_key)

    @property
    def public_columns(self) -> tuple[ColumnSchema, ...]:
        """The columns that are not internal, in DDL order (requirement 18.4)."""
        return tuple(column for column in self.columns.values() if not column.internal)


@dataclass(frozen=True, slots=True)
class StorageSchema:
    """The tables of one instance: the SQLAlchemy metadata and what each table holds.

    ``tables`` is keyed by table name in the order the tables are created, which is the
    code point order of those names and the order of ``metadata.sorted_tables``.
    """

    metadata: MetaData
    tables: FrozenMap[str, TableSchema]

    @property
    def table_names(self) -> tuple[str, ...]:
        """Every table name, in the order the tables are created."""
        return tuple(self.tables)

    @property
    def diffable_tables(self) -> tuple[TableSchema, ...]:
        """The tables the diff covers, in table name order (requirement 18.4)."""
        return tuple(table for table in self.tables.values() if table.diffable)

    def table(self, name: str) -> Table:
        """The SQLAlchemy table ``name``, to build a statement against it.

        Raises KeyError when the instance has no such table.
        """
        return self.metadata.tables[name]


def build_schema(domain: DomainModel) -> StorageSchema:
    """Return the storage schema of the instance ``domain`` describes (design.md 3.1).

    The tables are created in code point order of their names (see the module docstring).
    Raises ``sqlalchemy.exc.InvalidRequestError`` when two of them have the same name,
    which loading step 5 reports as a namespace conflict (requirement 2.7).
    """
    tables = sorted(_tables(domain), key=lambda table: table.name)
    metadata = MetaData()
    for table in tables:
        # Adding a name twice to one MetaData is what raises.
        _define(metadata, table)
    return StorageSchema(
        metadata=metadata, tables=FrozenMap((table.name, table) for table in tables)
    )


def _tables(domain: DomainModel) -> Iterator[TableSchema]:
    """Every table of the instance, in no particular order."""
    for entity in domain.entities.values():
        yield _entity_table(entity)
    for bill in domain.bills.values():
        yield from _bill_tables(bill)
    yield from _STOCK_TABLES
    yield from _INTERNAL_TABLES


def _entity_table(entity: EntityModel) -> TableSchema:
    """The table of an entity: one column per field of its record (design.md data model 3)."""
    return _record_table(entity.name, "entity", entity.fields.values())


def _bill_tables(bill: BillModel) -> Iterator[TableSchema]:
    """The two tables of a bill: its header and its lines (design.md data model 3)."""
    yield _record_table(
        bill.name, "bill", bill.header.values(), internal=(CLOSE_MODE_COLUMN, CONVERSION_COLUMN)
    )
    yield _record_table(bill.line_name, "line", bill.lines.values(), internal=(BILL_ID_COLUMN,))


def _record_table(
    name: str, kind: TableKind, fields: Iterable[FieldModel], internal: Sequence[str] = ()
) -> TableSchema:
    """A table of configured records: the fields in field view order, then ``internal``.

    An internal column holds an internal ID or one of the engine's own words, so all of
    them are TEXT.
    """
    columns = [
        ColumnSchema(field.column, field.type, primary_key=field.column == ID_COLUMN)
        for field in fields
    ]
    columns += [ColumnSchema(column, "string") for column in internal]
    return TableSchema(name=name, kind=kind, columns=_by_name(columns))


def _fixed_table(
    name: str, kind: TableKind, columns: Sequence[tuple[str, FieldType]], key_width: int = 1
) -> TableSchema:
    """A table of the engine, its ``key_width`` first columns forming the primary key."""
    specs = [
        ColumnSchema(column, type_, primary_key=index < key_width)
        for index, (column, type_) in enumerate(columns)
    ]
    return TableSchema(name=name, kind=kind, columns=_by_name(specs))


def _by_name(columns: Sequence[ColumnSchema]) -> FrozenMap[str, ColumnSchema]:
    """The columns keyed by name, keeping their DDL order."""
    return FrozenMap((column.name, column) for column in columns)


# The engine's inventory tables (requirement 9.1): one row per stock movement and one per
# (material, warehouse) that has movements. Their ids are built by the engine rather than
# by a counter (requirement 18.8).
_STOCK_TABLES: Final[tuple[TableSchema, ...]] = (
    _fixed_table(
        STOCK_TXN_TABLE,
        "stock",
        (
            (ID_COLUMN, "string"),
            (MATERIAL_COLUMN, "ref"),
            (WAREHOUSE_COLUMN, "ref"),
            (QTY_DELTA_COLUMN, "qty"),
            (SOURCE_BILL_TYPE_COLUMN, "string"),
            (SOURCE_LINE_COLUMN, "string"),
        ),
    ),
    _fixed_table(
        STOCK_BALANCE_TABLE,
        "stock",
        (
            (ID_COLUMN, "string"),
            (MATERIAL_COLUMN, "ref"),
            (WAREHOUSE_COLUMN, "ref"),
            (QTY_BASE_COLUMN, "qty"),
        ),
    ),
)

# The internal tables, the DDL of design.md data model 3. ``_counters`` holds the ID, code,
# number and operation sequence counters of the sandbox (requirement 4.6),
# ``_effective_writes`` the records each effective call wrote (requirement 18.10), and
# ``_writeback_ledger`` what every writeback added to its source, so unapprove reverses
# exactly that (design.md 4.2); the kind of a ledger amount follows the push it belongs to,
# qty or money, so the ledger keeps the canonical text itself.
_INTERNAL_TABLES: Final[tuple[TableSchema, ...]] = (
    _fixed_table(
        COUNTERS_TABLE,
        "internal",
        (("kind", "string"), ("key", "string"), ("value", "int")),
        key_width=2,
    ),
    _fixed_table(
        EFFECTIVE_WRITES_TABLE,
        "internal",
        (("seq", "int"), ("entity", "string"), (ID_COLUMN, "string")),
        key_width=3,
    ),
    _fixed_table(
        WRITEBACK_LEDGER_TABLE,
        "internal",
        (
            (WRITEBACK_TARGET_COLUMN, "string"),
            (WRITEBACK_COLUMN, "string"),
            (WRITEBACK_ORD_COLUMN, "int"),
            (WRITEBACK_SOURCE_TABLE_COLUMN, "string"),
            (WRITEBACK_SOURCE_ID_COLUMN, "string"),
            (WRITEBACK_FIELD_COLUMN, "string"),
            (WRITEBACK_AMOUNT_COLUMN, "string"),
        ),
        key_width=3,
    ),
)


def _define(metadata: MetaData, table: TableSchema) -> Table:
    """Add ``table`` to ``metadata`` as a WITHOUT ROWID table, with its index.

    Nullability follows the module docstring: a column of an internal table and the primary
    key of any table are NOT NULL, every other column accepts NULL.
    """
    internal_table = table.kind == "internal"
    columns = [
        Column(
            column.name,
            _sql_type(column.type),
            primary_key=column.primary_key,
            nullable=not (internal_table or column.primary_key),
        )
        for column in table.columns.values()
    ]
    # sqlite_with_rowid=False appends WITHOUT ROWID, which needs the primary key every
    # table here has.
    defined = Table(table.name, metadata, *columns, sqlite_with_rowid=False)
    if BILL_ID_COLUMN in table.columns:
        # The lines of one bill are read together on every bill operation.
        Index(f"ix_{table.name}{BILL_ID_COLUMN}", defined.c[BILL_ID_COLUMN])
    return defined


def _sql_type(field_type: FieldType) -> TypeEngine[Any]:
    """The SQLite type ``field_type`` is stored as (design.md data model 3)."""
    return Integer() if field_type in _INTEGER_TYPES else Text()
