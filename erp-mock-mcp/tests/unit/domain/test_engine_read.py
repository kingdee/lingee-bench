"""erp_mock.domain.engine and .views: the read operations of the engine session.

The tests run against a real sandbox database: the ``store`` fixture creates the tables of
the engine test fixture (tests.helpers.fixtures) and writes the records directly through a
UnitOfWork, so a get and a business_context_get are read from stored rows rather than from
records the engine wrote itself - the write primitives arrive with the later tasks of
design.md 4.2. What the tests pin:

- a get returns every non-internal field of the record, a field whose value is empty
  included, and a bill returns its lines by line number, whose code point order is not the
  order of the numbers (requirement 5.12);
- a reference comes back as the summary of the record it points at: ``id``, ``code`` and
  ``name`` for basic data, ``id`` and ``number`` for a bill, in that order
  (requirement 13.3);
- a get of a record that is not there ends in NOT_FOUND whose detail names the ``id``
  parameter (requirements 5.5 and 5.6);
- business_context_get returns the anchor date, the month it falls in, the default
  organization and the current user, in that order (requirement 16.10);
- a read-only operation makes no writes and records no effective call (design.md 4.1).
"""

from collections.abc import Iterator, Mapping
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.config.model import DomainModel
from erp_mock.domain.engine import (
    PIPELINE,
    BusinessContextOp,
    Engine,
    GetOp,
    OperationContext,
)
from erp_mock.domain.views import PageView, QueryView, RecordView, RefSummary
from erp_mock.state.schema import EFFECTIVE_WRITES_TABLE, StorageSchema, build_schema
from erp_mock.state.store import SandboxStore, Value
from tests.helpers.fixtures import ANCHOR_DATE, fixture_domain

# The sequence number of the traced call the operations of these tests run under; a
# read-only operation must leave the effective calls of the sandbox empty.
CALL_SEQ: Final = 7

# The fields every line of the seeded goods entry carries, next to its id and line number.
LINE_FIELDS: Final[Mapping[str, Value]] = {
    "qty_base": Decimal("2"),
    "article_id": "art_000001",
    "measure_id": "msr_000001",
    "depot_id": "dep_000001",
    "entry_qty": Decimal("2"),
    "_bill_id": "ge_000001",
}

# The records the sandbox starts from, in insertion order: the two organizations of which
# one is the default, the current user, the units, an article, a warehouse, a vendor, an
# approved supply order and the goods entry that was pushed from it, with three lines. A
# line id is built from the bill id and the line number (requirement 4.2), so the code
# point order of these three - -1, -10, -2 - is not the order of their numbers, and they
# are written in yet another order.
SEEDED: Final[tuple[tuple[str, Mapping[str, Value]], ...]] = (
    ("branch", {"id": "br_000001", "code": "B01", "name": "一号组织", "is_primary": True}),
    ("branch", {"id": "br_000002", "code": "B02", "name": "二号组织", "is_primary": False}),
    (
        "operator",
        {
            "id": "opr_000001",
            "code": "O01",
            "name": "张三",
            "branch_id": "br_000001",
            "duty": "buyer",
        },
    ),
    ("measure", {"id": "msr_000001", "code": "PCS", "name": "个"}),
    ("measure", {"id": "msr_000002", "code": "BOX", "name": "箱"}),
    (
        "article",
        {
            "id": "art_000001",
            "code": "A00001",
            "name": "螺栓",
            "spec": "M6",
            "grade": "plain",
            "base_measure_id": "msr_000001",
            "buy_measure_id": "msr_000002",
            "tax_rate": Decimal("0.13"),
        },
    ),
    ("depot", {"id": "dep_000001", "code": "D001", "name": "主仓", "branch_id": "br_000001"}),
    (
        "vendor",
        {
            "id": "ven_000001",
            "code": "V0001",
            "status": "enabled",
            "name": "一号供应商",
            "contact": "甲",
            "pay_days": 30,
            "default_tax_rate": Decimal("0.13"),
        },
    ),
    (
        "supply_order",
        {
            "id": "so_000001",
            "number": "SPO20261009001",
            "bill_type": "supply_order",
            "org_id": "br_000001",
            "biz_date": ANCHOR_DATE,
            "status": "approved",
            "price_includes_tax": False,
            "created_seq": 1,
            "updated_seq": 3,
            "vendor_id": "ven_000001",
            "need_date": date(2026, 10, 16),
        },
    ),
    (
        "goods_entry",
        {
            "id": "ge_000001",
            "number": "GRN20261009001",
            "bill_type": "goods_entry",
            "org_id": "br_000001",
            "biz_date": ANCHOR_DATE,
            "status": "draft",
            "source_bill_type": "supply_order",
            "source_bill_id": "so_000001",
            "created_seq": 4,
            "updated_seq": 4,
            "vendor_id": "ven_000001",
            "_conversion": "supply_order_to_entry",
        },
    ),
    ("goods_entry_line", {"id": "ge_000001-10", "line_no": 10, **LINE_FIELDS}),
    ("goods_entry_line", {"id": "ge_000001-1", "line_no": 1, **LINE_FIELDS}),
    ("goods_entry_line", {"id": "ge_000001-2", "line_no": 2, **LINE_FIELDS}),
)

# The result keys of the three objects the tests read, in the order the field view puts
# them in: the standard fields of the level, then the declared ones, every reference
# without its ``_id`` (requirements 5.12 and 13.3).
VENDOR_KEYS: Final[tuple[str, ...]] = (
    "id",
    "code",
    "status",
    "name",
    "short_name",
    "contact",
    "pay_days",
    "default_tax_rate",
)
ENTRY_KEYS: Final[tuple[str, ...]] = (
    "id",
    "number",
    "bill_type",
    "org",
    "biz_date",
    "status",
    "source_bill_type",
    "source_bill",
    "remark",
    "created_seq",
    "updated_seq",
    "vendor",
    "entry_ref",
    "lines",
)
ENTRY_LINE_KEYS: Final[tuple[str, ...]] = (
    "id",
    "line_no",
    "source_line_id",
    "qty_base",
    "article",
    "measure",
    "depot",
    "entry_qty",
)


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
    """An open store with the tables of the fixture and the seeded records."""
    store = SandboxStore(tmp_path / "sandbox", schema)
    store.open()
    with closing(store):
        store.create_tables()
        with store.transaction() as uow:
            for table, values in SEEDED:
                uow.insert(table, values)
        yield store


@pytest.fixture
def ctx() -> OperationContext:
    """The context of one traced call: the anchor date and the current user of the fixture."""
    return OperationContext(
        anchor_date=ANCHOR_DATE, current_user_id="opr_000001", call_seq=CALL_SEQ
    )


def as_record(view: RecordView | PageView | QueryView) -> RecordView:
    """The view as the record it is: a read of one record returns a RecordView."""
    assert isinstance(view, RecordView)
    return view


def as_summary(value: object) -> RefSummary:
    """The value as the reference summary it is (requirement 13.3)."""
    assert isinstance(value, RefSummary)
    return value


def as_lines(value: object) -> tuple[RecordView, ...]:
    """The value as the array of line objects it is (requirement 13.2)."""
    assert isinstance(value, tuple)
    for line in value:
        assert isinstance(line, RecordView)
    return value


def effective_calls(store: SandboxStore) -> list[int]:
    """The sequence numbers the sandbox recorded as effective calls (requirement 18.10)."""
    with store.transaction() as uow:
        return [int(str(row["seq"])) for row in uow.select(EFFECTIVE_WRITES_TABLE)]


# --- get (requirement 5.12) --------------------------------------------------------------


@pytest.mark.req("5.12")
def test_get_returns_every_non_internal_field_of_an_entity(
    engine: Engine, store: SandboxStore, ctx: OperationContext
) -> None:
    result = engine.run(store, ctx, GetOp("vendor", "ven_000001"))

    view = as_record(result.view)
    assert tuple(view.fields) == VENDOR_KEYS
    # An empty value keeps its key and is None (requirement 13.4).
    assert view.fields["short_name"] is None
    assert view.fields["code"] == "V0001"
    assert view.fields["status"] == "enabled"
    assert view.fields["pay_days"] == 30
    # Values are typed, not formatted: the tools layer writes them (design.md 5.4).
    assert view.fields["default_tax_rate"] == Decimal("0.13")
    # A read-only operation changes nothing and records no effective call (design.md 4.1).
    assert result.writes == ()
    assert effective_calls(store) == []


@pytest.mark.req("5.12")
def test_get_returns_a_reference_as_the_summary_of_basic_data(
    engine: Engine, store: SandboxStore, ctx: OperationContext
) -> None:
    view = as_record(engine.run(store, ctx, GetOp("article", "art_000001")).view)

    # The ref field base_measure_id is returned under base_measure (requirement 13.3).
    base = as_summary(view.fields["base_measure"])
    assert tuple(base.fields) == ("id", "code", "name")
    assert dict(base.fields) == {"id": "msr_000001", "code": "PCS", "name": "个"}
    assert dict(as_summary(view.fields["buy_measure"]).fields)["code"] == "BOX"
    assert "base_measure_id" not in view.fields


@pytest.mark.req("5.12")
def test_get_of_a_bill_returns_its_lines_by_line_number(
    engine: Engine, store: SandboxStore, ctx: OperationContext
) -> None:
    view = as_record(engine.run(store, ctx, GetOp("goods_entry", "ge_000001")).view)

    assert tuple(view.fields) == ENTRY_KEYS
    # The internal columns of the storage are in no result (requirement 5.12).
    assert [key for key in view.fields if key.startswith("_")] == []
    lines = as_lines(view.fields["lines"])
    assert tuple(lines[0].fields) == ENTRY_LINE_KEYS
    assert [line.fields["line_no"] for line in lines] == [1, 2, 10]
    assert [line.fields["id"] for line in lines] == [
        "ge_000001-1",
        "ge_000001-2",
        "ge_000001-10",
    ]
    assert [key for key in lines[0].fields if key.startswith("_")] == []
    assert dict(as_summary(lines[0].fields["depot"]).fields) == {
        "id": "dep_000001",
        "code": "D001",
        "name": "主仓",
    }


@pytest.mark.req("5.12")
def test_get_of_a_bill_returns_a_bill_reference_as_id_and_number(
    engine: Engine, store: SandboxStore, ctx: OperationContext
) -> None:
    view = as_record(engine.run(store, ctx, GetOp("goods_entry", "ge_000001")).view)

    # source_bill_id refers to the bill its own source_bill_type names (requirement 13.3).
    source = as_summary(view.fields["source_bill"])
    assert tuple(source.fields) == ("id", "number")
    assert dict(source.fields) == {"id": "so_000001", "number": "SPO20261009001"}
    # org_id and the declared vendor_id refer to basic data.
    assert dict(as_summary(view.fields["org"]).fields)["name"] == "一号组织"
    assert dict(as_summary(view.fields["vendor"]).fields)["code"] == "V0001"


@pytest.mark.req("5.12")
def test_get_of_a_bill_without_lines_returns_an_empty_array(
    engine: Engine, store: SandboxStore, ctx: OperationContext
) -> None:
    view = as_record(engine.run(store, ctx, GetOp("supply_order", "so_000001")).view)

    assert as_lines(view.fields["lines"]) == ()


@pytest.mark.req("5.5", "5.6")
def test_get_of_a_record_that_is_not_there_is_not_found(
    engine: Engine, store: SandboxStore, ctx: OperationContext
) -> None:
    with pytest.raises(ErpError) as raised:
        engine.run(store, ctx, GetOp("vendor", "ven_000009"))

    error = raised.value
    assert error.code is ErrorCode.NOT_FOUND
    assert error.rule_id is None
    assert [dict(item) for item in error.details] == [{"field": "id", "reason": "not_found"}]
    # The message names the field path of a detail item (requirement 13.7).
    assert "id" in error.message
    assert "ven_000009" in error.message


# --- business_context_get (requirement 16.10) --------------------------------------------


@pytest.mark.req("16.10")
def test_business_context_returns_date_period_org_and_user(
    engine: Engine, store: SandboxStore, ctx: OperationContext
) -> None:
    result = engine.run(store, ctx, BusinessContextOp())

    view = as_record(result.view)
    assert tuple(view.fields) == ("business_date", "current_period", "org", "user")
    assert view.fields["business_date"] == ANCHOR_DATE
    assert view.fields["current_period"] == "2026-10"
    # The default organization is the record whose default flag is true (requirement 1.8).
    org = as_summary(view.fields["org"])
    assert tuple(org.fields) == ("id", "code", "name")
    assert dict(org.fields) == {"id": "br_000001", "code": "B01", "name": "一号组织"}
    assert dict(as_summary(view.fields["user"]).fields) == {
        "id": "opr_000001",
        "code": "O01",
        "name": "张三",
    }
    assert result.writes == ()


@pytest.mark.req("16.10")
def test_business_context_takes_the_records_as_the_sandbox_holds_them_now(
    engine: Engine, store: SandboxStore, ctx: OperationContext
) -> None:
    with store.transaction() as uow:
        uow.update("branch", "br_000001", {"is_primary": False, "name": "改名组织"})
        uow.update("branch", "br_000002", {"is_primary": True})

    view = as_record(engine.run(store, ctx, BusinessContextOp()).view)

    assert dict(as_summary(view.fields["org"]).fields)["id"] == "br_000002"


@pytest.mark.req("16.10")
def test_business_context_without_a_default_organization_or_a_current_user(
    engine: Engine, store: SandboxStore
) -> None:
    with store.transaction() as uow:
        uow.update("branch", "br_000001", {"is_primary": False})

    view = as_record(
        engine.run(store, OperationContext(anchor_date=date(2027, 2, 28)), BusinessContextOp()).view
    )

    assert view.fields["current_period"] == "2027-02"
    # Null where the sandbox holds no default organization and no user is configured.
    assert view.fields["org"] is None
    assert view.fields["user"] is None


# --- the pipeline (design.md 4.1) --------------------------------------------------------


@pytest.mark.req("12.1", "12.10")
def test_the_pipeline_runs_the_ten_steps_in_order() -> None:
    """The slots of design.md 4.1, whose order fixes the order the errors are reported in.

    The checks themselves arrive with the primitives, each in the slot design.md 4.1
    assigns it; what this pins is that the slots are there and in this order.
    """
    assert [step.name for step in PIPELINE] == [
        "references",
        "ownership",
        "bill_status",
        "pushed_lines",
        "candidate",
        "unit_conversion",
        "rules",
        "uniqueness",
        "write",
        "view",
    ]
