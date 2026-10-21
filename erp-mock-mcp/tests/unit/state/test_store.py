"""erp_mock.state.store: the working database of a sandbox and the unit of work of a call.

Every test runs against a real SQLite file: :func:`open_store` creates a store over the
schema of the engine test fixture (tests.helpers.fixtures), whose ``vendor`` entity and
``supply_order`` bill between them carry 11 of the 12 field types, so one table shows how
each type is stored. What the tests pin:

- the pragmas :meth:`SandboxStore.open` asserts and that the database can be used from
  another thread, which ``check_same_thread=False`` allows (design.md 3.2);
- ``BEGIN IMMEDIATE``, and that an error anywhere in the block leaves the records and the
  counters as the transaction found them (requirements 12.7 and 4.12);
- the conversion between typed and stored values in both directions, read back as the
  bytes SQLite holds through a second, plain connection (design.md data model 3);
- the net writes of each (record, cause), which leave out a record that was inserted and
  deleted again or changed back to its original value (requirement 17.4);
- the effective calls, which roll back with the call that wrote them (requirement 18.10).
"""

import sqlite3
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import DomainModel
from erp_mock.state.schema import (
    BILL_ID_COLUMN,
    CLOSE_MODE_COLUMN,
    CONVERSION_COLUMN,
    COUNTERS_TABLE,
    EFFECTIVE_WRITES_TABLE,
    ColumnSchema,
    StorageSchema,
    build_schema,
)
from erp_mock.state.store import (
    DB_FILENAME,
    DIRECT_CAUSE,
    Record,
    SandboxStore,
    StoredValue,
    UnitOfWork,
    Value,
    Write,
    from_stored,
    to_stored,
)
from tests.helpers.fixtures import fixture_domain

# Three vendor records every test starts from, and the counters of the seeded sandbox.
VENDOR: Final = "vendor"
# The bill of the fixture, whose header carries the two date columns a range is read over.
ORDER: Final = "supply_order"
SEEDED: Final[tuple[Mapping[str, Value], ...]] = (
    {
        "id": "ven_000001",
        "code": "V0001",
        "status": "enabled",
        "name": "一号供应商",
        "short_name": "一号",
        "contact": "甲",
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


@pytest.fixture(scope="module")
def domain() -> DomainModel:
    return fixture_domain()


@pytest.fixture(scope="module")
def schema(domain: DomainModel) -> StorageSchema:
    return build_schema(domain)


@pytest.fixture
def store(tmp_path: Path, schema: StorageSchema) -> Iterator[SandboxStore]:
    """An open store with the fixture's tables and the three seeded vendors."""
    with open_store(tmp_path / "sandbox", schema) as store:
        store.create_tables()
        with store.transaction() as uow:
            for record in SEEDED:
                uow.insert(VENDOR, record)
            uow.bump_counter("id", VENDOR)
            uow.bump_counter("id", VENDOR)
            uow.bump_counter("id", VENDOR)
            uow.bump_counter("code", VENDOR)
            uow.bump_counter("op", "")
        yield store


def open_store(sandbox_dir: Path, schema: StorageSchema) -> closing[SandboxStore]:
    """An open store over ``sandbox_dir``, closed when the block ends."""
    store = SandboxStore(sandbox_dir, schema)
    store.open()
    return closing(store)


def pragma(connection: sqlite3.Connection, name: str) -> object:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    return row[0]


def stored_row(store: SandboxStore, table: str, record_id: str) -> dict[str, StoredValue]:
    """The row as SQLite holds it, read through a second, plain connection."""
    with closing(sqlite3.connect(store.db_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    return dict(row)


def content(store: SandboxStore) -> dict[str, list[Record]]:
    """Every table of the store, each in primary key order."""
    with store.transaction() as uow:
        return {name: uow.select(name) for name in store.schema.table_names}


def counters(uow: UnitOfWork) -> dict[tuple[str, str], int]:
    rows = uow.select(COUNTERS_TABLE)
    return {(str(row["kind"]), str(row["key"])): int(str(row["value"])) for row in rows}


def ids(records: list[Record]) -> list[str]:
    return [str(record["id"]) for record in records]


class Boom(Exception):
    """Raised inside a transaction block to end the call with an error."""


# --- opening, closing and the size of the database ----------------------------------------


@pytest.mark.req("12.7")
def test_open_creates_the_database_and_asserts_its_pragmas(
    tmp_path: Path, schema: StorageSchema
) -> None:
    sandbox_dir = tmp_path / "sandboxes" / "sb_1"

    with open_store(sandbox_dir, schema) as store:
        assert store.is_open
        assert store.db_path == sandbox_dir / DB_FILENAME
        assert store.db_path.is_file()
        # synchronous and foreign_keys are per connection, so they are read off the
        # store's own connection; journal_mode is a property of the file.
        connection = store.driver_connection
        assert pragma(connection, "journal_mode") == "wal"
        assert pragma(connection, "synchronous") == 1  # NORMAL
        assert pragma(connection, "foreign_keys") == 0
        # The driver must not begin a transaction of its own (V9).
        assert connection.isolation_level is None
        # Opening an open store does nothing.
        store.open()
        assert store.driver_connection is connection

    assert not store.is_open
    # The WAL was checkpointed into the file and its sidecar files are gone.
    assert sorted(path.name for path in sandbox_dir.iterdir()) == [DB_FILENAME]
    with closing(sqlite3.connect(store.db_path)) as plain:
        assert pragma(plain, "journal_mode") == "wal"


@pytest.mark.req("12.7")
def test_a_closed_store_refuses_to_work_and_reopens_with_its_content(store: SandboxStore) -> None:
    before = content(store)
    store.close()

    assert not store.is_open
    store.close()  # closing a closed store does nothing
    with pytest.raises(RuntimeError, match="not open"):
        store.db_bytes()
    with pytest.raises(RuntimeError, match="not open"):
        store.driver_connection.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="not open"):
        with store.transaction():
            pass

    store.open()
    assert content(store) == before


@pytest.mark.req("23.12")
def test_db_bytes_is_the_page_count_times_the_page_size(store: SandboxStore) -> None:
    with closing(sqlite3.connect(store.db_path)) as plain:
        page_size = pragma(plain, "page_size")
        page_count = pragma(plain, "page_count")
    assert isinstance(page_size, int)
    assert isinstance(page_count, int)

    assert store.db_bytes() == page_count * page_size

    before = store.db_bytes()
    with store.transaction() as uow:
        for index in range(400):
            uow.insert(
                VENDOR,
                {"id": f"ven_{index + 100:06d}", "name": "名称" * 200, "short_name": f"{index}"},
            )
    grown = store.db_bytes()

    assert grown > before
    assert grown % page_size == 0


@pytest.mark.req("12.7")
def test_the_database_can_be_used_from_another_thread(store: SandboxStore) -> None:
    # A sandbox runs its calls in a worker thread (design.md 7.5), which the connection
    # allows because check_same_thread is False.
    def insert() -> tuple[Record | None, int]:
        with store.transaction() as uow:
            uow.insert(VENDOR, {"id": "ven_000009", "name": "线程"})
            return uow.get(VENDOR, "ven_000009"), uow.bump_counter("op", "")

    with ThreadPoolExecutor(max_workers=1) as pool:
        record, operation = pool.submit(insert).result(timeout=30)

    assert record is not None
    assert record["name"] == "线程"
    assert operation == 2
    with store.transaction() as uow:
        assert uow.get(VENDOR, "ven_000009") is not None


# --- the transaction of one call -----------------------------------------------------------


@pytest.mark.req("12.7")
def test_a_transaction_takes_the_write_lock_before_its_first_statement(
    store: SandboxStore,
) -> None:
    statements: list[str] = []
    store.driver_connection.set_trace_callback(statements.append)
    try:
        with store.transaction() as uow:
            uow.update(VENDOR, "ven_000001", {"contact": "丙"})
    finally:
        store.driver_connection.set_trace_callback(None)

    # BEGIN IMMEDIATE is emitted by the begin hook, before any statement of the block (V9).
    assert statements[0] == "BEGIN IMMEDIATE"
    assert statements[-1] == "COMMIT"
    assert "BEGIN" not in statements


@pytest.mark.req("12.7")
def test_a_transaction_that_returns_commits(store: SandboxStore) -> None:
    with store.transaction() as uow:
        uow.insert(VENDOR, {"id": "ven_000004", "name": "四号供应商"})
        uow.update(VENDOR, "ven_000001", {"pay_days": 45})
        uow.delete(VENDOR, "ven_000003")

    with store.transaction() as uow:
        assert ids(uow.select(VENDOR)) == ["ven_000001", "ven_000002", "ven_000004"]
        record = uow.get(VENDOR, "ven_000001")
        assert record is not None
        assert record["pay_days"] == 45


@pytest.mark.req("4.12", "12.7")
def test_an_error_in_the_block_rolls_back_the_records_and_the_counters(
    store: SandboxStore,
) -> None:
    before = content(store)
    with store.transaction() as uow:
        before_counters = counters(uow)
    failed_id = 0

    with pytest.raises(Boom):
        with store.transaction() as uow:
            failed_id = uow.bump_counter("id", VENDOR)
            uow.bump_counter("op", "")
            uow.insert(VENDOR, {"id": f"ven_{failed_id:06d}", "name": "失败的供应商"})
            uow.update(VENDOR, "ven_000001", {"name": "改名"})
            uow.delete(VENDOR, "ven_000002")
            raise Boom

    assert content(store) == before
    with store.transaction() as uow:
        assert counters(uow) == before_counters
        # The next successful write operation gets what the failed one would have got
        # (requirement 4.12).
        assert uow.bump_counter("id", VENDOR) == failed_id == 4


@pytest.mark.req("12.7")
def test_transactions_of_one_store_do_not_nest(store: SandboxStore) -> None:
    with store.transaction():
        with pytest.raises(RuntimeError, match="already open"):
            with store.transaction():
                pass
        with pytest.raises(RuntimeError, match="inside a transaction"):
            store.close()

    # The refused attempts left the store usable.
    with store.transaction() as uow:
        assert len(uow.select(VENDOR)) == len(SEEDED)


# --- typed values and stored values --------------------------------------------------------

# A supply_order header and line with every column set: between them the 11 field types
# the fixture declares (design.md data model 3).
HEADER: Final[Mapping[str, Value]] = {
    "id": "so_000001",
    "number": "SUP202610090001",
    "bill_type": "supply_order",
    "org_id": "br_000001",
    "biz_date": date(2026, 10, 9),
    "status": "approved",
    "source_bill_type": "fee_note",
    "source_bill_id": "fn_000007",
    "remark": "备注\n第二行",
    "created_seq": 12,
    "updated_seq": 57,
    "price_includes_tax": False,
    "amount": Decimal("1700.00"),
    "tax_amount": Decimal("221"),
    "amount_with_tax": Decimal("1921.005"),
    "vendor_id": "ven_000001",
    "need_date": date(2026, 11, 1),
    CLOSE_MODE_COLUMN: "auto",
    CONVERSION_COLUMN: "cv_fee_to_pay",
}
LINE: Final[Mapping[str, Value]] = {
    "id": "so_000001-1",
    "line_no": 1,
    "source_line_id": "fn_000007-2",
    "qty": Decimal("2000"),
    "qty_base": Decimal("24000"),
    "price": Decimal("0.85"),
    "tax_rate": Decimal("0.13"),
    "amount": Decimal("1700.00"),
    "tax_amount": Decimal("221.00"),
    "amount_with_tax": Decimal("1921.00"),
    "linked_qty": Decimal("0"),
    "article_id": "art_000031",
    "measure_id": "mea_000002",
    BILL_ID_COLUMN: "so_000001",
}


@pytest.mark.req("4.7", "18.5")
def test_every_field_type_survives_a_write_and_a_read(store: SandboxStore) -> None:
    with store.transaction() as uow:
        uow.insert("supply_order", HEADER)
        uow.insert("supply_order_line", LINE)

        header = uow.get("supply_order", "so_000001")
        line = uow.get("supply_order_line", "so_000001-1")

    assert header is not None
    assert line is not None
    # get returns every column of the record, the internal ones included.
    assert dict(header) == {**HEADER, "amount_with_tax": Decimal("1921.01")}
    assert dict(line) == dict(LINE)
    # The columns come back in DDL order, so a read is deterministic.
    assert list(header) == list(store.schema.tables["supply_order"].columns)


@pytest.mark.req("4.7", "18.5")
def test_a_value_is_stored_as_the_canonical_text_of_its_field_type(store: SandboxStore) -> None:
    with store.transaction() as uow:
        uow.insert("supply_order", HEADER)
        uow.insert(
            "supply_order_line",
            {**LINE, "qty": Decimal("2000.00000"), "price": Decimal("0.1234565")},
        )

    header = stored_row(store, "supply_order", "so_000001")
    line = stored_row(store, "supply_order_line", "so_000001-1")

    # money keeps two decimals, qty, price and rate drop trailing zeros (requirement 4.8).
    assert header["amount"] == "1700.00"
    assert header["tax_amount"] == "221.00"
    assert line["qty"] == "2000"
    assert line["tax_rate"] == "0.13"
    assert line["linked_qty"] == "0"
    # A value with more decimals than the field type is rounded, ROUND_HALF_UP
    # (requirement 4.7): money 1921.005 to 1921.01, price to six decimals.
    assert header["amount_with_tax"] == "1921.01"
    assert line["price"] == "0.123457"
    # Dates are YYYY-MM-DD text, bools 0 or 1, ints INTEGER.
    assert header["biz_date"] == "2026-10-09"
    assert header["need_date"] == "2026-11-01"
    assert header["price_includes_tax"] == 0
    assert header["created_seq"] == 12
    assert line["line_no"] == 1
    assert header["remark"] == "备注\n第二行"


@pytest.mark.req("18.5")
def test_a_column_a_record_omits_is_null_and_comes_back_as_none(store: SandboxStore) -> None:
    with store.transaction() as uow:
        uow.insert(VENDOR, {"id": "ven_000004", "name": "四号供应商"})
        record = uow.get(VENDOR, "ven_000004")

    assert record is not None
    assert record["contact"] is None
    assert record["pay_days"] is None
    assert record["default_tax_rate"] is None
    assert stored_row(store, VENDOR, "ven_000004")["contact"] is None

    # Writing None explicitly stores NULL as well.
    with store.transaction() as uow:
        uow.update(VENDOR, "ven_000001", {"short_name": None, "default_tax_rate": None})
        updated = uow.get(VENDOR, "ven_000001")
    assert updated is not None
    assert updated["short_name"] is None
    assert updated["default_tax_rate"] is None


@pytest.mark.req("4.9", "18.5")
@pytest.mark.parametrize(
    ("field_type", "value", "stored"),
    [
        ("string", "V0001", "V0001"),
        ("text", "备注", "备注"),
        ("enum", "draft", "draft"),
        ("ref", "ven_000001", "ven_000001"),
        ("period", "2026-10", "2026-10"),
        ("date", date(2026, 10, 9), "2026-10-09"),
        ("int", 0, 0),
        ("int", -7, -7),
        ("bool", True, 1),
        ("bool", False, 0),
        ("qty", Decimal("2000"), "2000"),
        ("qty", Decimal("-12.5"), "-12.5"),
        ("price", Decimal("0.000001"), "0.000001"),
        ("money", Decimal("0"), "0.00"),
        ("rate", Decimal("0.13"), "0.13"),
    ],
)
def test_to_stored_and_from_stored_are_inverse_for_every_field_type(
    field_type: str, value: Value, stored: StoredValue
) -> None:
    column = ColumnSchema("value", field_type)  # type: ignore[arg-type]

    assert to_stored(column, value) == stored
    assert from_stored(column, stored) == value
    # None is NULL in both directions, whatever the type.
    assert to_stored(column, None) is None
    assert from_stored(column, None) is None


@pytest.mark.req("4.9")
def test_equal_decimals_are_stored_as_the_same_text() -> None:
    column = ColumnSchema("price", "price")

    assert to_stored(column, Decimal("0.85")) == to_stored(column, Decimal("0.850000")) == "0.85"
    # Zero is never "-0" (requirement 4.8).
    assert to_stored(column, Decimal("-0")) == to_stored(column, Decimal("0")) == "0"


@pytest.mark.req("18.5")
@pytest.mark.parametrize(
    ("field_type", "value"),
    [
        ("qty", 2000),
        ("qty", 0.85),
        ("qty", "2000"),
        ("date", "2026-10-09"),
        ("date", datetime(2026, 10, 9, 8, 0)),
        ("int", True),
        ("int", Decimal("1")),
        ("bool", 1),
        ("string", 5),
        ("ref", 5),
    ],
)
def test_a_value_of_the_wrong_python_type_never_reaches_storage(
    field_type: str, value: object
) -> None:
    column = ColumnSchema("value", field_type)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match=field_type):
        to_stored(column, value)  # type: ignore[arg-type]


# --- reading records -----------------------------------------------------------------------


@pytest.mark.req("5.9")
def test_select_matches_every_where_entry_and_at_least_one_like_pattern(
    store: SandboxStore,
) -> None:
    with store.transaction() as uow:
        # No filter: every record, ordered by id.
        assert ids(uow.select(VENDOR)) == ["ven_000001", "ven_000002", "ven_000003"]
        # Every where entry must match; None matches NULL.
        assert ids(uow.select(VENDOR, where={"status": "enabled"})) == [
            "ven_000001",
            "ven_000002",
        ]
        assert ids(uow.select(VENDOR, where={"status": "enabled", "pay_days": 60})) == [
            "ven_000002"
        ]
        assert ids(uow.select(VENDOR, where={"short_name": None})) == [
            "ven_000002",
            "ven_000003",
        ]
        assert uow.select(VENDOR, where={"default_tax_rate": Decimal("0.130000")}) != []
        # At least one like pattern must match: the keyword fields of a search.
        assert ids(uow.select(VENDOR, like={"name": "%二号%", "short_name": "%一号%"})) == [
            "ven_000001",
            "ven_000002",
        ]
        # where and like together.
        assert ids(
            uow.select(VENDOR, where={"status": "disabled"}, like={"name": "%号供应商%"})
        ) == ["ven_000003"]
        # The escape character is a backslash, so a literal % can be matched.
        assert uow.select(VENDOR, like={"code": r"%\%%"}) == []


@pytest.mark.req("5.9", "11.5")
def test_select_includes_both_ends_of_a_date_range(store: SandboxStore) -> None:
    days = (date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 11))
    with store.transaction() as uow:
        for number, day in enumerate(days, start=1):
            uow.insert(ORDER, {"id": f"so_00000{number}", "biz_date": day})

        # Both ends are included and each end can be given alone (requirement 11.5).
        assert ids(uow.select(ORDER, at_least={"biz_date": days[1]})) == [
            "so_000002",
            "so_000003",
        ]
        assert ids(uow.select(ORDER, at_most={"biz_date": days[1]})) == [
            "so_000001",
            "so_000002",
        ]
        both = uow.select(ORDER, at_least={"biz_date": days[1]}, at_most={"biz_date": days[1]})
        assert ids(both) == ["so_000002"]
        assert (
            uow.select(ORDER, at_least={"biz_date": days[2]}, at_most={"biz_date": days[1]}) == []
        )
        # Only a date column orders chronologically as stored text (design.md data model 3).
        with pytest.raises(ValueError, match="only a date field"):
            uow.select(ORDER, at_least={"amount": days[0]})  # type: ignore[dict-item]


@pytest.mark.req("5.12")
def test_get_returns_none_for_a_record_that_is_not_there(store: SandboxStore) -> None:
    with store.transaction() as uow:
        assert uow.get(VENDOR, "ven_999999") is None
        uow.delete(VENDOR, "ven_000001")

        assert uow.get(VENDOR, "ven_000001") is None
        assert "ven_000001" not in ids(uow.select(VENDOR))
        # Deleting a record that is not there changes nothing, and so does an empty update.
        uow.delete(VENDOR, "ven_000001")
        uow.update(VENDOR, "ven_999999", {"name": "无"})
        uow.update(VENDOR, "ven_000002", {})
        assert len(uow.select(VENDOR)) == len(SEEDED) - 1


@pytest.mark.req("5.12")
def test_an_unknown_table_or_column_is_refused(store: SandboxStore) -> None:
    with store.transaction() as uow:
        with pytest.raises(KeyError, match="no such table"):
            uow.get("no_such_table", "x")
        with pytest.raises(KeyError, match="no such column"):
            uow.select(VENDOR, where={"no_such_column": "x"})
        with pytest.raises(KeyError, match="no such column"):
            uow.insert(VENDOR, {"id": "ven_000004", "no_such_column": "x"})
        with pytest.raises(KeyError, match="no such column"):
            uow.update(VENDOR, "ven_000001", {"no_such_column": "x"})
        # The engine's internal tables are not addressed by id; they have own methods.
        with pytest.raises(KeyError, match="not a record table"):
            uow.insert(COUNTERS_TABLE, {"id": "x"})
        with pytest.raises(ValueError, match="needs a str id"):
            uow.insert(VENDOR, {"name": "没有 ID"})


# --- the counters of the sandbox -----------------------------------------------------------


@pytest.mark.req("4.6")
def test_bump_counter_counts_from_zero_per_kind_and_key(store: SandboxStore) -> None:
    with store.transaction() as uow:
        # The seeded sandbox bumped ("id", vendor) three times and ("code", vendor) once.
        assert uow.bump_counter("id", VENDOR) == 4
        assert uow.bump_counter("code", VENDOR) == 2
        # Every (kind, key) counts on its own, and a key that was never bumped starts at 1.
        assert uow.bump_counter("id", "supply_order") == 1
        assert uow.bump_counter("id", "supply_order") == 2
        assert uow.bump_counter("number", "SUP|20261009") == 1
        assert uow.bump_counter("op", "") == 2

    with store.transaction() as uow:
        assert counters(uow) == {
            ("code", VENDOR): 2,
            ("id", VENDOR): 4,
            ("id", "supply_order"): 2,
            ("number", "SUP|20261009"): 1,
            ("op", ""): 2,
        }


# --- what a call changed -------------------------------------------------------------------


@pytest.mark.req("17.4")
def test_net_writes_lists_one_item_per_record_and_cause_in_order(store: SandboxStore) -> None:
    with store.transaction() as uow:
        uow.insert(VENDOR, {"id": "ven_000004", "name": "四号供应商"})
        uow.delete(VENDOR, "ven_000003")
        with uow.cause("wb_fee_to_pay"):
            uow.update(VENDOR, "ven_000002", {"contact": "改"})
        with uow.cause("lk_entry_in"):
            uow.update(VENDOR, "ven_000002", {"short_name": "二号"})
            uow.insert("depot", {"id": "dep_000001", "name": "仓库"})

        writes = uow.net_writes()

    assert writes == (
        Write(entity="depot", op="insert", id="dep_000001", cause="lk_entry_in"),
        Write(entity=VENDOR, op="update", id="ven_000002", cause="lk_entry_in"),
        Write(entity=VENDOR, op="update", id="ven_000002", cause="wb_fee_to_pay"),
        Write(entity=VENDOR, op="delete", id="ven_000003", cause=DIRECT_CAUSE),
        Write(entity=VENDOR, op="insert", id="ven_000004", cause=DIRECT_CAUSE),
    )
    # entity, id and cause by code point; op is not a sort key (requirement 17.4).
    assert list(writes) == sorted(writes, key=lambda write: (write.entity, write.id, write.cause))


@pytest.mark.req("17.4")
def test_a_record_inserted_and_deleted_again_is_not_a_net_write(store: SandboxStore) -> None:
    with store.transaction() as uow:
        uow.insert(VENDOR, {"id": "ven_000004", "name": "四号供应商"})
        uow.update(VENDOR, "ven_000004", {"contact": "甲"})
        uow.delete(VENDOR, "ven_000004")

        assert uow.net_writes() == ()


@pytest.mark.req("17.4")
def test_a_value_changed_back_to_its_original_is_not_a_net_write(store: SandboxStore) -> None:
    with store.transaction() as uow:
        uow.update(VENDOR, "ven_000001", {"name": "改名", "pay_days": 7})
        uow.update(VENDOR, "ven_000001", {"name": "一号供应商", "pay_days": 30})
        # A decimal written as another text of the same value does not change the record.
        uow.update(VENDOR, "ven_000002", {"default_tax_rate": Decimal("0.060000")})
        # Deleting a record and inserting it again unchanged leaves no net change, which
        # is how reapproving writes the same stock movements back (requirement 18.8).
        uow.delete(VENDOR, "ven_000003")
        uow.insert(VENDOR, SEEDED[2])

        assert uow.net_writes() == ()

    assert ids(content(store)[VENDOR]) == ["ven_000001", "ven_000002", "ven_000003"]


@pytest.mark.req("17.4")
def test_a_cause_is_reported_only_for_the_columns_it_wrote(store: SandboxStore) -> None:
    with store.transaction() as uow:
        with uow.cause("lk_entry_in"):
            # Writes the value the record already holds: no net change for this cause.
            uow.update(VENDOR, "ven_000001", {"name": "一号供应商"})
        with uow.cause("wb_fee_to_pay"):
            uow.update(VENDOR, "ven_000001", {"contact": "改"})

        assert uow.net_writes() == (
            Write(entity=VENDOR, op="update", id="ven_000001", cause="wb_fee_to_pay"),
        )


@pytest.mark.req("17.4")
def test_a_call_that_only_read_has_no_net_writes(store: SandboxStore) -> None:
    with store.transaction() as uow:
        with uow.cause("lk_entry_in"):
            assert uow.get(VENDOR, "ven_000001") is not None
            assert uow.select(VENDOR, where={"status": "enabled"}) != []

        assert uow.net_writes() == ()
    # A read-only call allocates no operation sequence number either (requirement 4.11).
    with store.transaction() as uow:
        assert counters(uow)[("op", "")] == 1


@pytest.mark.req("17.4")
def test_the_cause_of_a_block_is_restored_even_when_the_block_raises(
    store: SandboxStore,
) -> None:
    with store.transaction() as uow:
        with pytest.raises(Boom):
            with uow.cause("lk_entry_in"):
                with uow.cause("wb_fee_to_pay"):
                    raise Boom
        uow.update(VENDOR, "ven_000001", {"contact": "改"})

        assert uow.net_writes() == (
            Write(entity=VENDOR, op="update", id="ven_000001", cause=DIRECT_CAUSE),
        )


# --- the effective calls of the sandbox ----------------------------------------------------


@pytest.mark.req("18.10")
def test_record_effective_call_lists_each_record_of_the_call_once(store: SandboxStore) -> None:
    with store.transaction() as uow:
        uow.insert("depot", {"id": "dep_000001", "name": "仓库"})
        with uow.cause("lk_entry_in"):
            uow.update(VENDOR, "ven_000001", {"contact": "改"})
        with uow.cause("wb_fee_to_pay"):
            uow.update(VENDOR, "ven_000001", {"short_name": "壹号"})
        writes = uow.net_writes()
        uow.record_effective_call(7, writes)

    assert len(writes) == 3  # the same record under two causes
    with store.transaction() as uow:
        assert [dict(row) for row in uow.select(EFFECTIVE_WRITES_TABLE)] == [
            {"seq": 7, "entity": "depot", "id": "dep_000001"},
            {"seq": 7, "entity": VENDOR, "id": "ven_000001"},
        ]
        # A call without writes records nothing (requirement 18.10).
        uow.record_effective_call(8, ())
        assert len(uow.select(EFFECTIVE_WRITES_TABLE)) == 2


@pytest.mark.req("18.10")
def test_the_effective_calls_of_a_failed_call_roll_back_with_it(store: SandboxStore) -> None:
    with pytest.raises(Boom):
        with store.transaction() as uow:
            uow.insert(VENDOR, {"id": "ven_000004", "name": "四号供应商"})
            uow.record_effective_call(7, uow.net_writes())
            raise Boom

    with store.transaction() as uow:
        assert uow.select(EFFECTIVE_WRITES_TABLE) == []
