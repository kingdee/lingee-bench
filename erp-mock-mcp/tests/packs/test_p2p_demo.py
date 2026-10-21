"""The configuration test of the p2p-demo instance (requirements 21.2, 21.8 and 21.12).

``instances/p2p-demo`` is the example instance configuration of the repository: the three
example business packs, one semantic profile and the one initial dataset ``purchase-basic``
(example-p2p.md section 8). It is the instance the contract test, the determinism tests and
the protocol integration test are served from, so what it has to be is settled here.

Unlike the three pack level tests, which write a temporary instance configuration over one
pack (:mod:`tests.helpers.pack_instance`), this module loads the real directory with a
:class:`~erp_mock.loader.loader.Loader`: all eight validation steps and the cross-check of
descriptions against Business_Data, which is exactly what the ``validate`` command will run
(task 24.2, requirement 22.5). The clock of the instance is in mode today, so the anchor
date of step 8 is passed in rather than read from a document (requirement 2.14) - a test
never reads the system clock (requirement 25.10).

What this module asserts:

- instance.yaml and the profile carry the values requirement 21.2 writes: three packs each
  pinned to a version, one profile and one dataset which are also the defaults, an employee
  as the current user and a clock in mode today over ``Asia/Shanghai``;
- the alias map of ``purchase-basic`` holds the five kinds of record requirement 21.8 lists,
  and the Snapshot backs each of them up: the statuses, the unit conversion of the material
  whose purchase unit is not its base unit, the ``linked_qty`` of the partly received order
  line and the stock the historical receipt fed;
- every anchor date of the range requirement 21.12 gives passes the whole validation, the
  build of ``purchase-basic`` included (:data:`ANCHOR_DATES`); task 19.9 turns that into the
  property test over the whole range.

The 22 tools the instance exposes are the contract snapshot of task 19.6 (requirement 21.3),
and the invariants of the Snapshot are checked by loading step 8 itself at every anchor date
this module loads, so a load that passed is a Snapshot that satisfies INV-1, INV-2, INV-3
and INV-6 (requirement 2.14).
"""

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.frozen import FrozenMap
from erp_mock.domain.inventory import stock_balance
from erp_mock.domain.numbering import line_id
from erp_mock.loader.instance import DatasetModel, LoadedInstance
from erp_mock.loader.loader import Loader
from erp_mock.seed.builder import (
    ALIASES_FILENAME,
    META_FILENAME,
    SNAPSHOT_FILENAME,
    SnapshotKey,
    SnapshotRef,
)
from erp_mock.state.schema import build_schema
from erp_mock.state.snapshot import copy_snapshot
from erp_mock.state.store import DB_FILENAME, Record, SandboxStore
from tests.helpers.pack_instance import read_aliases

# The example instance and the example business packs of the repository.
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INSTANCE_DIR: Final = REPO_ROOT / "instances" / "p2p-demo"
PACKS_DIR: Final = REPO_ROOT / "packs"

# What instance.yaml calls itself and names (requirement 21.2).
INSTANCE: Final = "p2p-demo"
PROFILE: Final = "semantic"
DATASET: Final = "purchase-basic"
PACKS: Final[tuple[str, ...]] = ("core", "purchase", "inventory")
CURRENT_USER: Final = "emp_000001"
TIMEZONE: Final = "Asia/Shanghai"

# The anchor date this module reads the dataset at. Any date of the range would do - that is
# what requirement 21.12 is about - so this one is picked for being nothing special: a
# Friday in the middle of a 31 day month of a non-leap year.
ANCHOR_DATE: Final = date(2026, 10, 9)

# The anchor dates the whole validation is run at (requirement 21.12). The three of them
# requirement 25.4 names - the first day of the range, the leap day of 2000 and the last day
# of the range - and dates that put the relative expressions of the dataset in awkward
# places: the first day of a month whose previous month has 29 days, a 29th whose previous
# month has 28, a Sunday, which is the last day of the week ``this_week`` counts from, and
# the last day of a year, where ``today+14d`` crosses into the next one.
ANCHOR_DATES: Final[tuple[date, ...]] = (
    date(2000, 1, 1),  # the first day of the range, a Saturday; month(-1) is 1999-12
    date(2000, 2, 29),  # the leap day of 2000
    date(2024, 3, 1),  # the first day of a month whose previous month has 29 days
    date(2025, 3, 29),  # a 29th whose previous month has 28 days
    date(2026, 10, 25),  # a Sunday, the last day of its week
    date(2031, 12, 31),  # the last day of a year; month(0).start is 2031-12-01
    date(2050, 7, 15),
    date(2099, 12, 31),  # the last day of the range; today+14d is 2100-01-14
)

# The aliases of ``purchase-basic``, which is every record of its ``entities``
# (requirement 20.10); the records of ``bulk`` carry none (requirement 20.4).
ALIASES: Final[frozenset[str]] = frozenset(
    {
        "supplier:main",
        "supplier:spare",
        "supplier:retired",
        "material:boxed",
        "material:plain",
        "material:retired",
        "unit_conversion:boxed_box",
        "purchase_requisition:open",
        "purchase_order:partly_received",
        "purchase_receipt:history",
        "purchase_order:closed_by_hand",
        "purchase_order:waiting_approval",
        "purchase_order:open_draft",
        "stock_adjustment:gain",
    }
)

# The five kinds of record requirement 21.8 asks the dataset to declare an alias for, and
# the alias each of them is declared under.
BOXED_MATERIAL: Final = "material:boxed"
BOXED_CONVERSION: Final = "unit_conversion:boxed_box"
DISABLED_SUPPLIER: Final = "supplier:retired"
DISABLED_MATERIAL: Final = "material:retired"
APPROVED_REQUISITION: Final = "purchase_requisition:open"
PARTLY_RECEIVED_ORDER: Final = "purchase_order:partly_received"
HISTORICAL_RECEIPT: Final = "purchase_receipt:history"

# What the dataset writes about the material whose purchase unit is not its base unit: one
# purchase unit is this many base units (requirement 6.6), so an order of ``ORDERED_UNITS``
# purchase units is ``ORDERED_BASE`` base units.
FACTOR: Final = Decimal("24")
ORDERED_UNITS: Final = Decimal("20")
ORDERED_BASE: Final = Decimal("480")

# What the historical receipt took in: part of the first line of that order, which is both
# the ``linked_qty`` the writeback left on the order line and the stock the linkage fed
# (requirements 21.11 and 21.7).
RECEIVED_UNITS: Final = Decimal("6")
RECEIVED_BASE: Final = Decimal("144")


# --- the loaded instance and its Snapshot ------------------------------------------------


def validate(anchor_date: date, data_dir: Path) -> LoadedInstance:
    """Run every validation step of p2p-demo at ``anchor_date``; return the configuration.

    The eight steps and the cross-check that follows them (requirement 2.1), with the
    Snapshot of ``purchase-basic`` cached below ``data_dir``. Raises
    :class:`~erp_mock.loader.errors.ConfigErrors` at the first step that finds an error and
    :class:`~erp_mock.seed.resolve.SeedError` where the dataset cannot be built at that
    date, which is what requirement 21.12 rules out.
    """
    return Loader(INSTANCE_DIR, PACKS_DIR, anchor_date, data_dir).load()


def snapshot_ref(dataset: DatasetModel, anchor_date: date) -> SnapshotRef:
    """The three files of the Snapshot step 8 took up for ``dataset`` (requirement 20.10).

    The step leaves the directory of the cache entry behind
    (:attr:`~erp_mock.loader.instance.DatasetModel.snapshot`); the three files have fixed
    names inside it (design.md data model 2).
    """
    directory = dataset.snapshot
    assert directory is not None, "第 8 步没有为数据集取用 Snapshot"
    return SnapshotRef(
        db=directory / SNAPSHOT_FILENAME,
        meta=directory / META_FILENAME,
        aliases=directory / ALIASES_FILENAME,
        key=SnapshotKey(f"{dataset.name}@{dataset.version}", anchor_date),
    )


# A stored value is of whatever type its field has (:class:`~erp_mock.state.store.Record`),
# so the three readers below narrow it and assert the type on the way, which is itself part
# of what a Snapshot has to hold: a reference is an internal ID, a quantity a decimal and a
# date a date.


def ref_id(record: Record, field: str) -> str:
    """The internal ID ``record`` holds for the reference field ``field``."""
    value = record[field]
    assert isinstance(value, str), f"{field} 的值 {value!r} 不是内部 ID"
    return value


def quantity(record: Record, field: str) -> Decimal:
    """The decimal ``record`` holds for the quantity or rate field ``field``."""
    value = record[field]
    assert isinstance(value, Decimal), f"{field} 的值 {value!r} 不是小数"
    return value


def day(record: Record, field: str) -> date:
    """The date ``record`` holds for the date field ``field``."""
    value = record[field]
    assert isinstance(value, date), f"{field} 的值 {value!r} 不是日期"
    return value


@dataclass(frozen=True, slots=True)
class Demo:
    """p2p-demo loaded, with a sandbox opened on the Snapshot of ``purchase-basic``.

    ``aliases`` is the alias map beside that Snapshot - the internal ID of every record of
    ``entities`` (requirement 20.10) - and ``store`` a copy of the Snapshot opened the way a
    new sandbox opens one, which is how this module reads what the build produced: the
    statuses, the quantities and the stock no document writes down.
    """

    loaded: LoadedInstance
    aliases: FrozenMap[str, str]
    store: SandboxStore

    @property
    def dataset(self) -> DatasetModel:
        """The one initial dataset of the instance (requirement 21.2)."""
        return self.loaded.default_dataset

    def id(self, alias: str) -> str:
        """The internal ID the record ``alias`` was created with; it has to be there."""
        assert alias in self.aliases, f"别名映射中没有 {alias}：已有 {sorted(self.aliases)}"
        return self.aliases[alias]

    def record(self, table: str, record_id: str) -> Record:
        """The record ``record_id`` of ``table``; it has to be in the Snapshot."""
        with self.store.transaction() as uow:
            found = uow.get(table, record_id)
        assert found is not None, f"{table} {record_id} 不在 Snapshot 中"
        return found

    def bill(self, alias: str) -> Record:
        """The bill the alias ``alias`` stands for, read from its own table."""
        kind, _, _ = alias.partition(":")
        return self.record(kind, self.id(alias))

    def line(self, alias: str, line_no: int) -> Record:
        """The line ``line_no`` of the bill ``alias`` (requirement 4.2)."""
        kind, _, _ = alias.partition(":")
        table = self.loaded.domain.bills[kind].line_name
        return self.record(table, line_id(self.id(alias), line_no))

    def balance(self, material_id: str, warehouse_id: str) -> Decimal:
        """The stock of (``material_id``, ``warehouse_id``) in base units (req 9.1)."""
        with self.store.transaction() as uow:
            return stock_balance(uow, material_id, warehouse_id)


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Demo]:
    """p2p-demo past every validation step, with its Snapshot open for reading.

    Built once for the module: the validation itself builds the Snapshot of
    ``purchase-basic`` at :data:`ANCHOR_DATE` and checks it (requirement 2.14), and the
    Snapshot is then copied into a sandbox directory, as creating a sandbox would do
    (requirement 15.1), so the files of the cache entry are only read.
    """
    workdir = tmp_path_factory.mktemp(INSTANCE)
    loaded = validate(ANCHOR_DATE, workdir / "data")
    ref = snapshot_ref(loaded.default_dataset, ANCHOR_DATE)
    sandbox = workdir / "sandbox"
    copy_snapshot(ref.db, sandbox / DB_FILENAME)
    store = SandboxStore(sandbox, build_schema(loaded.domain))
    store.open()
    with closing(store):
        yield Demo(loaded=loaded, aliases=read_aliases(ref), store=store)


# --- instance.yaml and the profile (requirement 21.2) ------------------------------------


@pytest.mark.req("21.2")
def test_the_instance_references_the_three_packs_each_pinned_to_a_version(
    demo: Demo,
) -> None:
    instance = demo.loaded.instance
    assert instance.instance == INSTANCE
    # The three example packs, in this order: core first, since the other two depend on it
    # (requirement 2.5), and that order is also the output order of tools/list
    # (requirement 11.9) and the order the Shared_Data is created in (requirement 20.17).
    assert tuple(instance.packs) == PACKS
    # Each pinned to a concrete version, never a range (requirement 2.3), and each equal to
    # the ``version`` of that pack's own pack.yaml, which step 1 checked.
    assert dict(instance.packs) == {"core": "1.0.0", "purchase": "1.0.0", "inventory": "1.0.0"}
    assert {pack.name: pack.manifest.version for pack in demo.loaded.packs} == dict(instance.packs)


@pytest.mark.req("21.2")
def test_the_one_profile_and_the_one_dataset_are_the_defaults(demo: Demo) -> None:
    instance = demo.loaded.instance
    assert tuple(instance.profiles) == (PROFILE,)
    assert tuple(instance.datasets) == (DATASET,)
    # A client that asks for neither gets these two (requirement 15.1).
    assert instance.default_profile == PROFILE
    assert instance.default_dataset == DATASET
    assert demo.loaded.default_profile.name == PROFILE
    assert demo.dataset.name == DATASET


@pytest.mark.req("21.2")
def test_the_profile_is_the_semantic_surface_over_the_three_packs(demo: Demo) -> None:
    profile = demo.loaded.default_profile.definition
    # The three values M0 fixes, which loading step 4 checks (requirement 2.13).
    assert (profile.surface, profile.description_variant) == ("semantic", "zh-detailed")
    assert not profile.include_deprecated
    # The packs in the order of instance.yaml, and nothing added or dropped on top of what
    # they expose: the M0 tool list is reached by each pack's own ``exclude``
    # (requirement 21.4), not by the profile.
    assert tuple(profile.packs) == PACKS
    assert tuple(profile.include_tools) == ()
    assert tuple(profile.exclude_tools) == ()


@pytest.mark.req("21.2")
def test_the_current_user_is_an_employee_of_the_snapshot(demo: Demo) -> None:
    instance = demo.loaded.instance
    assert instance.current_user == CURRENT_USER
    # The dataset declares no ``current_user`` of its own, so every operation of its build
    # and every call of a sandbox runs as the one of instance.yaml (requirement 20.5).
    assert demo.dataset.definition.current_user is None
    assert demo.dataset.current_user == CURRENT_USER

    # It is an internal ID of the user context entity, never an alias or a code
    # (requirement 20.16), and it is a record of the Snapshot, which step 8 checked
    # (requirement 2.10): the first employee of core's Shared_Data.
    user_entity = demo.loaded.domain.user_entity
    assert user_entity is not None
    assert CURRENT_USER.startswith(f"{user_entity.id_prefix}_")
    assert demo.record(user_entity.name, CURRENT_USER)["code"] == "E001"


@pytest.mark.req("21.2")
def test_the_clock_is_in_mode_today_over_the_shanghai_zone(demo: Demo) -> None:
    clock = demo.loaded.instance.clock
    # Mode today reads the system time once, when a sandbox is created, and takes the date
    # in this zone (requirement 16.1), so the business date the model is told matches the
    # client's own "today"; no ``anchor_date`` is written, a run that needs one byte for
    # byte reproducible passing it per sandbox.
    assert clock.mode == "today"
    assert clock.timezone == TIMEZONE
    assert clock.anchor_date is None
    # Which is why this module hands the anchor date to the loader (requirement 2.14).
    assert demo.loaded.step8_anchor == ANCHOR_DATE


# --- the records requirement 21.8 asks for -----------------------------------------------


@pytest.mark.req("21.8")
def test_the_alias_map_holds_every_record_of_the_dataset(demo: Demo) -> None:
    # One alias per record of ``entities``, each mapped to the internal ID it was created
    # with (requirement 20.10).
    assert frozenset(demo.aliases) == ALIASES
    assert frozenset(demo.dataset.definition.entities) == ALIASES
    # The five kinds of record of requirement 21.8 are among them.
    assert ALIASES >= frozenset(
        {
            BOXED_MATERIAL,
            DISABLED_SUPPLIER,
            DISABLED_MATERIAL,
            APPROVED_REQUISITION,
            PARTLY_RECEIVED_ORDER,
            HISTORICAL_RECEIPT,
        }
    )
    # An alias names the entity or bill of its record, so every ID carries the ID prefix of
    # that kind (requirement 20.6).
    domain = demo.loaded.domain
    for alias, record_id in demo.aliases.items():
        kind, _, _ = alias.partition(":")
        declared = domain.entities.get(kind) or domain.bills.get(kind)
        assert declared is not None, f"别名 {alias} 的 kind 不是实例中的实体或单据"
        assert record_id.startswith(f"{declared.id_prefix}_"), alias


@pytest.mark.req("21.8")
def test_the_boxed_material_is_purchased_in_a_unit_that_is_not_its_base_unit(
    demo: Demo,
) -> None:
    material_id = demo.id(BOXED_MATERIAL)
    material = demo.record("material", material_id)
    assert material["status"] == "enabled"
    base, purchase = material["base_unit_id"], material["purchase_unit_id"]
    assert base != purchase, "采购单位与基本单位必须不同（需求 21 第 8 条）"

    # And the conversion record that makes the two comparable: one purchase unit is FACTOR
    # base units (requirement 6.6), which is what the engine folds a quantity with.
    conversion = demo.record("unit_conversion", demo.id(BOXED_CONVERSION))
    assert conversion["material_id"] == material_id
    assert conversion["from_unit_id"] == purchase
    assert conversion["factor"] == FACTOR

    # It is the only material of the dataset with a conversion, and the others are measured
    # in their base unit alone, so a tool call that leaves ``unit_id`` out gets the base
    # unit there and the purchase unit here.
    with demo.store.transaction() as uow:
        assert [found["material_id"] for found in uow.select("unit_conversion")] == [material_id]
    plain = demo.record("material", demo.id("material:plain"))
    assert plain["base_unit_id"] == plain["purchase_unit_id"]


@pytest.mark.req("21.8")
def test_the_disabled_supplier_and_the_disabled_material_are_the_only_disabled_records(
    demo: Demo,
) -> None:
    # Both are there for R-GEN-006: a disabled record may not be written into a new bill
    # (requirement 5.7).
    assert demo.record("supplier", demo.id(DISABLED_SUPPLIER))["status"] == "disabled"
    assert demo.record("material", demo.id(DISABLED_MATERIAL))["status"] == "disabled"
    # Everything else the dataset writes is enabled, so a call that picks a record at random
    # out of a search is not refused for a reason the model cannot see. The one disabled
    # warehouse comes from core's Shared_Data and is no part of this dataset.
    with demo.store.transaction() as uow:
        disabled = {
            entity: [found["id"] for found in uow.select(entity, where={"status": "disabled"})]
            for entity in ("supplier", "material", "warehouse")
        }
    assert disabled["supplier"] == [demo.id(DISABLED_SUPPLIER)]
    assert disabled["material"] == [demo.id(DISABLED_MATERIAL)]
    assert len(disabled["warehouse"]) == 1


@pytest.mark.req("21.8")
def test_the_approved_requisition_can_still_be_pushed_at_the_anchor_date(demo: Demo) -> None:
    requisition = demo.bill(APPROVED_REQUISITION)
    # Approved is the state a push requires of its source (requirement 21.5); the build got
    # there by running create, submit and approve, the latter two being operations M0
    # exposes no tool for (requirement 20.2).
    assert requisition["status"] == "approved"

    # Its required date is on or after the anchor date. The push maps it onto the delivery
    # date of the order it generates and R-PUR-001 is evaluated at create, the push included
    # (requirement 10.9), so an earlier date would make the push of a client fail.
    assert day(requisition, "need_date") >= ANCHOR_DATE
    assert day(requisition, "biz_date") <= day(requisition, "need_date")

    # Nothing has been ordered from it yet, so both lines are pushable in full
    # (requirement 8.5) and the first one is measured in the purchase unit of the boxed
    # material, which is where the conversion shows up in a quantity.
    first = demo.line(APPROVED_REQUISITION, 1)
    assert first["linked_qty"] is None
    assert first["material_id"] == demo.id(BOXED_MATERIAL)
    assert quantity(first, "qty_base") == quantity(first, "qty") * FACTOR
    assert demo.line(APPROVED_REQUISITION, 2)["linked_qty"] is None


@pytest.mark.req("21.8")
def test_the_partly_received_order_has_a_line_between_zero_and_its_full_quantity(
    demo: Demo,
) -> None:
    order = demo.bill(PARTLY_RECEIVED_ORDER)
    # Still approved after the historical receipt: not all of its lines are linked in full,
    # so the writeback did not close it (requirement 8.13).
    assert order["status"] == "approved"

    # The first line is the one requirement 21.8 asks for: part of it has been received, so
    # a push of a client can take the rest and R-GEN-005 keeps it to that
    # (requirement 8.5).
    first = demo.line(PARTLY_RECEIVED_ORDER, 1)
    assert quantity(first, "qty") == ORDERED_UNITS
    assert quantity(first, "qty_base") == ORDERED_BASE == ORDERED_UNITS * FACTOR
    assert quantity(first, "linked_qty") == RECEIVED_BASE
    assert Decimal(0) < quantity(first, "linked_qty") < quantity(first, "qty_base")

    # The second line has not been received at all, which is what keeps the order open.
    second = demo.line(PARTLY_RECEIVED_ORDER, 2)
    assert second["linked_qty"] is None
    assert quantity(second, "qty_base") > 0


@pytest.mark.req("21.8")
def test_the_historical_receipt_is_what_put_the_stock_above_zero(demo: Demo) -> None:
    receipt = demo.bill(HISTORICAL_RECEIPT)
    # Approved, and generated by the push from the order it received against
    # (requirement 8.1); the build ran create through the push, then submit and approve.
    assert receipt["status"] == "approved"
    assert receipt["source_bill_type"] == "purchase_order"
    assert receipt["source_bill_id"] == demo.id(PARTLY_RECEIVED_ORDER)
    # The header fields the conversion maps are not written by the dataset but taken from
    # the order, so the receipt prices its lines exactly as the order did (req 20.6).
    order = demo.bill(PARTLY_RECEIVED_ORDER)
    assert receipt["supplier_id"] == order["supplier_id"]
    assert receipt["price_includes_tax"] == order["price_includes_tax"]

    # Its one line, which is both the writeback and the stock: the quantity in purchase
    # units, folded into base units, is the ``linked_qty`` of the source line and the stock
    # of (material, warehouse) the linkage fed on approve (requirements 21.7 and 21.11).
    line = demo.line(HISTORICAL_RECEIPT, 1)
    assert line["source_line_id"] == line_id(demo.id(PARTLY_RECEIVED_ORDER), 1)
    assert quantity(line, "qty") == RECEIVED_UNITS
    assert quantity(line, "qty_base") == RECEIVED_BASE == RECEIVED_UNITS * FACTOR
    balance = demo.balance(ref_id(line, "material_id"), ref_id(line, "warehouse_id"))
    assert balance == RECEIVED_BASE
    assert balance > 0

    # Every stock the dataset leaves behind is above zero, and it is spread over more than
    # one (material, warehouse), so a balance query returns several rows (requirement 9.12).
    with demo.store.transaction() as uow:
        balances = uow.select("stock_balance")
    assert len(balances) > 1
    assert all(quantity(found, "qty_base") > 0 for found in balances)


# --- every anchor date of the range (requirement 21.12) ----------------------------------


@pytest.mark.req("21.12")
@pytest.mark.parametrize("anchor_date", ANCHOR_DATES, ids=[str(day) for day in ANCHOR_DATES])
def test_the_instance_passes_the_whole_validation_at_any_anchor_date(
    anchor_date: date, tmp_path: Path
) -> None:
    # The dates of the dataset are relative expressions, so each of them lands inside the
    # calendar at every anchor date of 2000-01-01 to 2099-12-31 and the order between any
    # two of them does not change (requirement 16.5). Passing the validation at this date
    # therefore covers the build of purchase-basic, INV-1, INV-2, INV-3 and INV-6 over its
    # Snapshot, and the current user and default organization checks (requirement 2.14).
    loaded = validate(anchor_date, tmp_path)
    assert loaded.step8_anchor == anchor_date

    # The alias map is the same whatever the anchor date: an internal ID comes from the
    # creation order, which the dates do not touch (requirement 20.14).
    ref = snapshot_ref(loaded.default_dataset, anchor_date)
    assert frozenset(read_aliases(ref)) == ALIASES
