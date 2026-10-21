"""erp_mock.state.schema: the tables an instance stores its records in.

The schema is read off the engine test fixture (tests.helpers.fixtures), whose seven
entities and seven bills cover pricing, quantity semantics without pricing and both kinds
of push, so one configuration shows every column design.md data model 3 lists.

What the tests assert comes from the database the schema creates, not from the SQLAlchemy
objects alone: :func:`create_in_memory` runs ``create_all`` against an in-memory SQLite
database and collects the DDL it emitted, the statements SQLite stored and the columns of
every table (``PRAGMA table_info``). So the tables, their creation order, the column order,
the SQLite types, the primary keys and NOT NULL are checked as SQLite ended up with them.
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Final, NamedTuple

import pytest
from sqlalchemy import Connection, create_engine, event
from sqlalchemy.exc import InvalidRequestError

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import BillModel, DomainModel, EntityModel
from erp_mock.state.schema import (
    BILL_ID_COLUMN,
    CLOSE_MODE_COLUMN,
    CONVERSION_COLUMN,
    COUNTERS_TABLE,
    EFFECTIVE_WRITES_TABLE,
    ID_COLUMN,
    STOCK_BALANCE_TABLE,
    STOCK_TXN_TABLE,
    WRITEBACK_LEDGER_TABLE,
    ColumnSchema,
    StorageSchema,
    TableSchema,
    build_schema,
)
from tests.helpers.fixtures import fixture_domain

# The internal tables of the engine, in the order their names sort (design.md data model 3).
INTERNAL_TABLES: Final = (COUNTERS_TABLE, EFFECTIVE_WRITES_TABLE, WRITEBACK_LEDGER_TABLE)

# The two SQLite types every value is stored in (design.md data model 3).
TEXT: Final = "TEXT"
INTEGER: Final = "INTEGER"


class ColumnInfo(NamedTuple):
    """One column as ``PRAGMA table_info`` reports it."""

    name: str
    type: str
    not_null: bool
    # 0 for a column outside the primary key, otherwise its 1-based position in the key.
    key_position: int


class Created(NamedTuple):
    """What creating a schema in an empty SQLite database produced."""

    # The DDL statements create_all emitted, in order, whitespace squeezed.
    ddl: tuple[str, ...]
    # The statement SQLite stored per table and index name.
    stored: Mapping[str, str]
    # The columns of each table, in storage order.
    columns: Mapping[str, tuple[ColumnInfo, ...]]


def squeeze(sql: str) -> str:
    """``sql`` with every run of whitespace replaced by one space."""
    return " ".join(sql.split())


def create_in_memory(schema: StorageSchema) -> Created:
    """Create ``schema`` in an empty in-memory database and read back what SQLite has."""
    statements: list[str] = []

    def record(
        _conn: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(squeeze(statement))

    engine = create_engine("sqlite+pysqlite://")
    event.listen(engine, "before_cursor_execute", record)
    try:
        with engine.connect() as conn:
            schema.metadata.create_all(conn, checkfirst=False)
            conn.commit()
            ddl = tuple(statements)
            stored = {
                name: squeeze(sql)
                for name, sql in conn.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_schema ORDER BY name"
                )
            }
            columns = {
                table: tuple(
                    ColumnInfo(name, type_, bool(not_null), key_position)
                    for _cid, name, type_, not_null, _default, key_position in conn.exec_driver_sql(
                        f"PRAGMA table_info({table})"
                    )
                )
                for table in schema.table_names
            }
    finally:
        engine.dispose()
    return Created(ddl=ddl, stored=stored, columns=columns)


@pytest.fixture(scope="module")
def domain() -> DomainModel:
    return fixture_domain()


@pytest.fixture(scope="module")
def schema(domain: DomainModel) -> StorageSchema:
    return build_schema(domain)


@pytest.fixture(scope="module")
def created(schema: StorageSchema) -> Created:
    return create_in_memory(schema)


def created_tables(created: Created) -> list[str]:
    """The tables in the order the DDL created them."""
    return [sql.split()[2] for sql in created.ddl if sql.startswith("CREATE TABLE ")]


def line_tables(domain: DomainModel) -> list[str]:
    return [bill.line_name for bill in domain.bills.values()]


def names(columns: Iterable[ColumnInfo | ColumnSchema]) -> list[str]:
    return [column.name for column in columns]


def types(columns: Iterable[ColumnInfo]) -> dict[str, str]:
    return {column.name: column.type for column in columns}


def key(columns: Sequence[ColumnInfo]) -> list[str]:
    """The primary key columns, in key order."""
    in_key = sorted((c.key_position, c.name) for c in columns if c.key_position)
    return [name for _position, name in in_key]


def record_tables(schema: StorageSchema) -> Iterator[TableSchema]:
    """Every table that holds records: the diff covers exactly these (requirement 18.4)."""
    return (table for table in schema.tables.values() if table.diffable)


# --- which tables there are ---------------------------------------------------------------


@pytest.mark.req("18.4")
def test_there_is_one_table_per_entity_per_bill_and_per_bills_lines(
    domain: DomainModel, schema: StorageSchema, created: Created
) -> None:
    expected = [
        *domain.entities,
        *domain.bills,
        *line_tables(domain),
        STOCK_TXN_TABLE,
        STOCK_BALANCE_TABLE,
        *INTERNAL_TABLES,
    ]

    assert sorted(schema.table_names) == sorted(expected)
    # The database holds those tables and nothing else.
    assert sorted(created.columns) == sorted(expected)
    assert created_tables(created) == sorted(expected)


@pytest.mark.req("18.4")
def test_the_tables_the_diff_covers_are_the_record_tables(
    domain: DomainModel, schema: StorageSchema
) -> None:
    diffable = [table.name for table in schema.diffable_tables]

    # An entity, a bill, <bill>_line, stock_txn and stock_balance are the diff's entities.
    assert diffable == sorted(
        [
            *domain.entities,
            *domain.bills,
            *line_tables(domain),
            STOCK_TXN_TABLE,
            STOCK_BALANCE_TABLE,
        ]
    )
    assert [table.kind for table in schema.tables.values() if not table.diffable] == [
        "internal"
    ] * len(INTERNAL_TABLES)
    kinds = {name: schema.tables[name].kind for name in diffable}
    assert {kinds[name] for name in domain.entities} == {"entity"}
    assert {kinds[name] for name in domain.bills} == {"bill"}
    assert {kinds[name] for name in line_tables(domain)} == {"line"}
    assert kinds[STOCK_TXN_TABLE] == kinds[STOCK_BALANCE_TABLE] == "stock"


@pytest.mark.req("2.7")
def test_two_tables_of_the_same_name_are_an_error_rather_than_one_merged_table() -> None:
    # Loading step 5 reports a configured name that collides with another one or with
    # stock_txn or stock_balance, so this cannot happen on a loaded instance; if it ever
    # did, the schema must not come back with one of the two tables silently dropped.
    colliding = EntityModel(
        name=STOCK_TXN_TABLE,
        pack="fx_base",
        file="entities/stock_txn.yaml",
        id_prefix="stx",
        fields=FrozenMap(),
    )

    with pytest.raises(InvalidRequestError, match=STOCK_TXN_TABLE):
        build_schema(DomainModel(entities=FrozenMap({colliding.name: colliding})))


@pytest.mark.req("4.6", "18.4")
def test_a_configuration_without_entities_and_bills_still_gets_the_engines_tables() -> None:
    schema = build_schema(DomainModel())

    assert list(schema.table_names) == [*INTERNAL_TABLES, STOCK_BALANCE_TABLE, STOCK_TXN_TABLE]
    assert sorted(create_in_memory(schema).columns) == sorted(schema.table_names)


# --- creation order and table shape -------------------------------------------------------


@pytest.mark.req("20.13")
def test_tables_are_created_in_code_point_order_of_their_names(
    schema: StorageSchema, created: Created
) -> None:
    created_in_order = created_tables(created)

    # The order the metadata is built in is the order the DDL runs in, so one instance
    # always runs the same DDL sequence (design.md 3.1 and 3.3).
    assert created_in_order == sorted(schema.table_names)
    assert created_in_order == list(schema.table_names)
    assert created_in_order == [table.name for table in schema.metadata.sorted_tables]
    # The underscore of an internal table sorts before every letter of a configured name.
    assert created_in_order[: len(INTERNAL_TABLES)] == list(INTERNAL_TABLES)
    # An index is created right after the table it belongs to.
    indexes = [
        (position, sql.split()[2], sql.split()[4])
        for position, sql in enumerate(created.ddl)
        if sql.startswith("CREATE INDEX ")
    ]
    assert indexes
    for position, _index, table in indexes:
        assert created.ddl[position - 1].startswith(f"CREATE TABLE {table} ")


@pytest.mark.req("20.13")
def test_every_table_is_without_rowid_and_a_record_table_is_keyed_by_id(
    schema: StorageSchema, created: Created
) -> None:
    for table in schema.tables.values():
        stored = created.stored[table.name]
        assert stored.endswith("WITHOUT ROWID"), stored
        assert key(created.columns[table.name]) == list(table.primary_key)

    # ORDER BY id on a record table is the code point order of the ids (design.md 3.1).
    for table in record_tables(schema):
        assert table.primary_key == (ID_COLUMN,)


@pytest.mark.req("18.4")
def test_a_record_table_has_no_constraint_but_its_primary_key(
    schema: StorageSchema, created: Created
) -> None:
    # Uniqueness and required fields are checked by the call pipeline (requirement 5.8),
    # and an absent value is stored as NULL (requirement 18.5).
    for table in record_tables(schema):
        stored = created.stored[table.name]
        assert "UNIQUE" not in stored
        assert "FOREIGN KEY" not in stored
        assert "REFERENCES" not in stored
        not_null = [column.name for column in created.columns[table.name] if column.not_null]
        assert not_null == [ID_COLUMN]


# --- the columns of the configured tables -------------------------------------------------


@pytest.mark.req("18.4")
def test_an_entity_table_holds_the_fields_of_its_record(
    domain: DomainModel, schema: StorageSchema, created: Created
) -> None:
    for entity in domain.entities.values():
        table = schema.tables[entity.name]
        stored = created.columns[entity.name]

        assert names(stored) == list(entity.fields)
        assert names(table.public_columns) == list(entity.fields)
        assert {name: column.type for name, column in table.columns.items()} == {
            name: field.type for name, field in entity.fields.items()
        }

    # The entity of the fixture that declares a code rule and a status (requirement 1.3).
    assert names(created.columns["vendor"]) == [
        ID_COLUMN,
        "code",
        "status",
        "name",
        "short_name",
        "contact",
        "pay_days",
        "default_tax_rate",
    ]


@pytest.mark.req("18.4")
def test_a_bill_holds_its_header_fields_and_its_lines_their_line_fields(
    domain: DomainModel, schema: StorageSchema, created: Created
) -> None:
    for bill in domain.bills.values():
        header = schema.tables[bill.name]
        lines = schema.tables[bill.line_name]

        assert names(header.public_columns) == list(bill.header)
        assert names(lines.public_columns) == list(bill.lines)
        assert names(created.columns[bill.name]) == [
            *bill.header,
            CLOSE_MODE_COLUMN,
            CONVERSION_COLUMN,
        ]
        assert names(created.columns[bill.line_name]) == [*bill.lines, BILL_ID_COLUMN]
        # Every field is stored under its own name, with its own type.
        for table, level in ((header, bill.header), (lines, bill.lines)):
            assert {name: table.columns[name].type for name in level} == {
                name: field.type for name, field in level.items()
            }


@pytest.mark.req("18.4")
def test_a_pricing_bill_that_is_a_push_source_holds_the_pricing_and_link_columns(
    created: Created,
) -> None:
    # supply_order has pricing and is the source of the push limited by qty, so its lines
    # carry linked_qty; fee_note is the source of the push limited by amount, so its header
    # carries linked_amount (meta-model.md section 7).
    assert names(created.columns["supply_order"]) == [
        ID_COLUMN,
        "number",
        "bill_type",
        "org_id",
        "biz_date",
        "status",
        "source_bill_type",
        "source_bill_id",
        "remark",
        "created_seq",
        "updated_seq",
        "price_includes_tax",
        "amount",
        "tax_amount",
        "amount_with_tax",
        "vendor_id",
        "need_date",
        CLOSE_MODE_COLUMN,
        CONVERSION_COLUMN,
    ]
    assert names(created.columns["supply_order_line"]) == [
        ID_COLUMN,
        "line_no",
        "source_line_id",
        "qty",
        "qty_base",
        "price",
        "tax_rate",
        "amount",
        "tax_amount",
        "amount_with_tax",
        "linked_qty",
        "article_id",
        "measure_id",
        BILL_ID_COLUMN,
    ]
    assert "linked_amount" in names(created.columns["fee_note"])
    # A bill without pricing that declares quantity semantics keeps qty_base alone
    # (requirement 1.4).
    assert names(created.columns["count_sheet_line"]) == [
        ID_COLUMN,
        "line_no",
        "source_line_id",
        "qty_base",
        "article_id",
        "measure_id",
        "diff_qty",
        BILL_ID_COLUMN,
    ]


@pytest.mark.req("18.4")
def test_the_internal_columns_of_a_bill_are_the_engines_own_and_no_field(
    domain: DomainModel, schema: StorageSchema, created: Created
) -> None:
    for bill in domain.bills.values():
        header = schema.tables[bill.name]
        lines = schema.tables[bill.line_name]

        assert internal(header) == [CLOSE_MODE_COLUMN, CONVERSION_COLUMN]
        assert internal(lines) == [BILL_ID_COLUMN]
        # No internal column is a field, so no result and no diff shows one.
        assert not {CLOSE_MODE_COLUMN, CONVERSION_COLUMN} & set(bill.header)
        assert BILL_ID_COLUMN not in bill.lines
        assert types(created.columns[bill.name])[CLOSE_MODE_COLUMN] == TEXT
        # The lines of one bill are read by their bill, so _bill_id is indexed.
        index = f"ix_{bill.line_name}{BILL_ID_COLUMN}"
        assert created.stored[index] == (
            f"CREATE INDEX {index} ON {bill.line_name} ({BILL_ID_COLUMN})"
        )

    for table in record_tables(schema):
        if table.kind not in ("bill", "line"):
            assert internal(table) == []


def internal(table: TableSchema) -> list[str]:
    """The internal columns of ``table``, in storage order (requirement 18.4)."""
    return [name for name, column in table.columns.items() if column.internal]


# --- the engine's own tables --------------------------------------------------------------


@pytest.mark.req("18.4")
def test_the_inventory_tables_have_the_engines_fixed_columns(created: Created) -> None:
    # The columns of stock_txn and stock_balance are names of the engine, not of a business
    # pack (design.md data model 3); the id of a stock_txn row is
    # <source line id>:<warehouse id>:<direction> (requirement 18.8).
    assert types(created.columns[STOCK_TXN_TABLE]) == {
        ID_COLUMN: TEXT,
        "material_id": TEXT,
        "warehouse_id": TEXT,
        "qty_delta": TEXT,
        "source_bill_type": TEXT,
        "source_line_id": TEXT,
    }
    assert types(created.columns[STOCK_BALANCE_TABLE]) == {
        ID_COLUMN: TEXT,
        "material_id": TEXT,
        "warehouse_id": TEXT,
        "qty_base": TEXT,
    }


@pytest.mark.req("4.6", "18.4")
def test_the_internal_tables_hold_the_engines_own_bookkeeping(
    schema: StorageSchema, created: Created
) -> None:
    # The counters of the sandbox (requirement 4.6), the records of each effective call
    # (requirement 18.10) and what every writeback added to its source (design.md 4.2).
    assert types(created.columns[COUNTERS_TABLE]) == {
        "kind": TEXT,
        "key": TEXT,
        "value": INTEGER,
    }
    assert types(created.columns[EFFECTIVE_WRITES_TABLE]) == {
        "seq": INTEGER,
        "entity": TEXT,
        ID_COLUMN: TEXT,
    }
    assert types(created.columns[WRITEBACK_LEDGER_TABLE]) == {
        "target_bill_id": TEXT,
        "writeback": TEXT,
        "ord": INTEGER,
        "source_table": TEXT,
        "source_id": TEXT,
        "field": TEXT,
        "amount": TEXT,
    }
    assert [key(created.columns[table]) for table in INTERNAL_TABLES] == [
        ["kind", "key"],
        ["seq", "entity", ID_COLUMN],
        ["target_bill_id", "writeback", "ord"],
    ]
    for table in INTERNAL_TABLES:
        # The engine writes these rows itself, so every column is NOT NULL.
        assert all(column.not_null for column in created.columns[table])
        # No internal table takes part in the diff (requirement 18.4).
        assert not schema.tables[table].diffable


# --- the SQLite type of every column ------------------------------------------------------


@pytest.mark.req("18.4")
def test_int_and_bool_are_stored_as_integer_and_every_other_type_as_text(
    schema: StorageSchema, created: Created
) -> None:
    for table in schema.tables.values():
        stored = types(created.columns[table.name])
        for name, column in table.columns.items():
            expected = INTEGER if column.type in ("int", "bool") else TEXT
            assert stored[name] == expected, f"{table.name}.{name} is {column.type}"

    # Decimals are stored as the canonical text of Decimal_Formatter (requirement 4.8), so
    # equal values have equal text and the diff compares stored values directly.
    lines = types(created.columns["supply_order_line"])
    assert [lines[name] for name in ("qty", "price", "tax_rate", "amount")] == [TEXT] * 4
    # line_no is an int, price_includes_tax a bool (requirement 18.5).
    assert lines["line_no"] == INTEGER
    assert types(created.columns["supply_order"])["price_includes_tax"] == INTEGER


@pytest.mark.req("18.4")
def test_a_column_knows_the_field_type_of_the_values_it_holds(
    domain: DomainModel, schema: StorageSchema
) -> None:
    bill: BillModel = domain.bills["supply_order"]
    lines = schema.tables[bill.line_name]

    assert lines.columns["qty"].type == "qty"
    assert lines.columns["line_no"].type == "int"
    assert lines.columns[BILL_ID_COLUMN].type == "string"
    assert schema.tables["vendor"].columns["default_tax_rate"].type == "rate"
    assert schema.tables[STOCK_TXN_TABLE].columns["qty_delta"].type == "qty"
    assert schema.tables[COUNTERS_TABLE].columns["value"].type == "int"


# --- the schema object --------------------------------------------------------------------


@pytest.mark.req("18.4")
def test_the_schema_hands_out_its_tables_by_name(schema: StorageSchema) -> None:
    table = schema.table("supply_order_line")

    assert table.name == "supply_order_line"
    assert [column.name for column in table.columns] == list(
        schema.tables["supply_order_line"].columns
    )
    assert isinstance(schema.tables, FrozenMap)
    with pytest.raises(KeyError):
        schema.table("no_such_table")
