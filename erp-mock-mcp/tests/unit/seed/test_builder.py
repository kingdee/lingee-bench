"""erp_mock.seed.builder: the build of one initial dataset (requirements 20.2 to 20.17).

The fixture's own dataset (``tests/fixtures/instances/fx-demo/datasets/fx-basic.yaml``) is
built once for this module and the first tests read what it produced: the three files, the
alias map, the Shared_Data in the order of the packs, the state and the numbers of the
historical bills, the stock on hand their approves left behind, and the background records
of ``bulk``. The dataset documents the state it is meant to leave in its own comments, so
the assertions here and that file have to be read together.

The tests after them build small datasets of their own: the business context with and
without the record of its current user, an engine error at a create and at an approve, and
a build whose data does not satisfy an invariant. The last one is the one case no dataset
can bring about - the engine is what keeps the invariants, so there is nothing to write
that would break one - and it is set up by putting a stock balance no transaction accounts
for into a build database and running the build over it.
"""

import hashlib
import json
import shutil
from collections.abc import Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.jsonio import JsonValue
from erp_mock.config.instance import DatasetDef
from erp_mock.domain.engine import Engine
from erp_mock.domain.inventory import balance_id
from erp_mock.loader.instance import LoadedInstance
from erp_mock.loader.loader import Loader
from erp_mock.loader.step4 import RawDoc
from erp_mock.seed.builder import (
    ALIASES_FILENAME,
    META_FILENAME,
    SNAPSHOT_FILENAME,
    SeedBuilder,
    SnapshotKey,
    SnapshotRef,
    _Build,
)
from erp_mock.seed.generators import BLACKLIST, blacklist_hits, short_name
from erp_mock.seed.resolve import SeedError, resolve
from erp_mock.state.schema import (
    MATERIAL_COLUMN,
    QTY_BASE_COLUMN,
    STOCK_BALANCE_TABLE,
    WAREHOUSE_COLUMN,
    build_schema,
)
from erp_mock.state.snapshot import build_snapshot_db, copy_snapshot
from erp_mock.state.store import Record, SandboxStore
from tests.helpers.fixtures import (
    ANCHOR_DATE,
    BASE_PACK,
    DATASET,
    IMPLEMENTED_STEPS,
    INSTANCE_DIR,
    PACKS_DIR,
    load_fixture,
)

# The dataset and the current user of the fixture (requirement 20.16).
KEY: Final = SnapshotKey(dataset=f"{DATASET}@1.0.0", anchor=ANCHOR_DATE)
USER: Final = "opr_000001"

# The state the fixture's dataset leaves its supply orders in, as its comments say: the
# status, how the bill was closed and the number its own business date gives it
# (requirements 20.2 and 4.4).
ORDER_STATES: Final = {
    "so_000001": ("closed", "auto", "SPO202609050001"),
    "so_000002": ("approved", None, "SPO202609120001"),
    "so_000003": ("closed", "manual", "SPO202609220001"),
    "so_000004": ("submitted", None, "SPO202610050001"),
    "so_000005": ("draft", None, "SPO202610090001"),
}

# The values the ``choice`` generator of ``vendor.default_tax_rate`` draws from, as the
# fixture pack writes them in seed/generators.yaml (requirement 20.4).
RATES: Final = (Decimal("0"), Decimal("0.06"), Decimal("0.09"), Decimal("0.13"))

# The stock on hand the history of the dataset leaves, by (article, depot) (req 9.8).
ON_HAND: Final = {
    ("art_000001", "dep_000001"): Decimal("360"),
    ("art_000001", "dep_000002"): Decimal("60"),
    ("art_000002", "dep_000002"): Decimal("40"),
}

# Records for the small datasets below; the article needs the unit ``PCS`` of the fixture's
# Shared_Data, and the issue note writes neither its taker nor its business date, so both
# come from the build context (requirement 20.5).
ARTICLE: Final[Mapping[str, object]] = {
    "kind": "article",
    "name": "夹具甲存货",
    "grade": "plain",
    "base_measure": "PCS",
}
ISSUE: Final[Mapping[str, object]] = {
    "kind": "issue_note",
    "depot": "D001",
    "lines": [{"article": "article:part", "measure": "PCS", "issue_qty": "1"}],
}
CONTEXT_RECORDS: Final = {"article:part": ARTICLE, "issue_note:draft": ISSUE}


@cache
def _fixture() -> tuple[LoadedInstance, RawDoc[DatasetDef]]:
    """The fixture's configuration and its dataset definition, loaded once."""
    loader = load_fixture()
    document = next(doc for doc in loader.raw_datasets if doc.document.dataset == DATASET)
    return loader.assemble(), document


def builder() -> SeedBuilder:
    """A builder over the fixture's instance configuration."""
    loaded, _ = _fixture()
    return SeedBuilder(loaded, INSTANCE_DIR)


def definition(**changes: object) -> DatasetDef:
    """A dataset definition like the fixture's, with ``changes`` written into it."""
    written = {
        "dataset": DATASET,
        "version": "1.0.0",
        "random_seed": 7,
        "include_shared": [BASE_PACK],
        "current_user": USER,
        **changes,
    }
    return DatasetDef.model_validate(written)


def build(out_dir: Path, **changes: object) -> SnapshotRef:
    """Build a dataset like the fixture's with ``changes`` written into it."""
    return builder().build(definition(**changes), ANCHOR_DATE, out_dir)


def failure(out_dir: Path, **changes: object) -> SeedError:
    """The error building a dataset with ``changes`` written into it raises."""
    with pytest.raises(SeedError) as caught:
        build(out_dir, **changes)
    return caught.value


@dataclass(frozen=True, slots=True)
class Built:
    """What one build produced: its three files and a sandbox copied from the Snapshot."""

    ref: SnapshotRef
    store: SandboxStore

    def rows(self, table: str) -> list[Record]:
        """Every record of ``table``, by id."""
        with self.store.transaction() as uow:
            return uow.select(table)

    def record(self, table: str, record_id: str) -> Record:
        """The record ``record_id`` of ``table``; it has to be there."""
        with self.store.transaction() as uow:
            found = uow.get(table, record_id)
        assert found is not None, f"{table} {record_id} 不在快照中"
        return found

    @property
    def meta(self) -> Mapping[str, JsonValue]:
        """The parsed ``meta.json`` of the build (requirement 20.10)."""
        return _read_json(self.ref.meta)

    @property
    def aliases(self) -> Mapping[str, str]:
        """The ``aliases`` of the parsed ``aliases.json`` (requirement 20.10)."""
        found = _read_json(self.ref.aliases)["aliases"]
        assert isinstance(found, Mapping)
        return {str(alias): str(found[alias]) for alias in found}


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Built]:
    """The fixture's own dataset, built once and opened as a sandbox would open it."""
    loaded, document = _fixture()
    workdir = tmp_path_factory.mktemp(DATASET)
    ref = SeedBuilder(loaded, INSTANCE_DIR).build(
        document.document, ANCHOR_DATE, workdir / "snapshot", file=document.file
    )
    sandbox = workdir / "sandbox"
    copy_snapshot(ref.db, sandbox / "db.sqlite")
    store = SandboxStore(sandbox, build_schema(loaded.domain))
    store.open()
    with closing(store):
        yield Built(ref=ref, store=store)


# --- the three files (requirement 20.10) -------------------------------------------------


@pytest.mark.req("20.10")
def test_the_build_writes_three_files_and_leaves_nothing_else_behind(built: Built) -> None:
    ref = built.ref
    assert ref.key == KEY
    assert [path.name for path in ref.files] == [
        SNAPSHOT_FILENAME,
        META_FILENAME,
        ALIASES_FILENAME,
    ]
    assert all(path.is_file() for path in ref.files)
    # The build database is gone: the directory holds the Snapshot and the two JSON files.
    assert sorted(path.name for path in ref.directory.iterdir()) == sorted(
        path.name for path in ref.files
    )


@pytest.mark.req("20.10")
def test_the_metadata_names_the_versions_and_no_build_time(built: Built) -> None:
    _, document = _fixture()
    meta = built.meta
    assert list(meta) == [
        "dataset",
        "version",
        "random_seed",
        "anchor_date",
        "closed_before",
        "current_user",
        "framework_version",
        "meta_model_version",
        "packs",
        "dataset_sha256",
    ]
    assert meta["dataset"] == DATASET
    assert meta["version"] == "1.0.0"
    assert meta["random_seed"] == 20261009
    assert meta["anchor_date"] == ANCHOR_DATE.isoformat()
    # periods.closed_before is month(-1), so the month before the anchor month is the
    # first open one (requirement 20.10).
    assert meta["closed_before"] == "2026-09"
    assert meta["current_user"] == USER
    assert meta["framework_version"] == "0.1.0"
    assert meta["meta_model_version"] == "0.1.0"
    assert meta["packs"] == {"fx_base": "1.0.0", "fx_trade": "1.0.0"}
    assert meta["dataset_sha256"] == _sha256(INSTANCE_DIR / document.file)


@pytest.mark.req("20.10")
def test_the_alias_map_holds_every_alias_by_code_point(built: Built) -> None:
    payload = _read_json(built.ref.aliases)
    assert list(payload) == ["dataset", "aliases"]
    assert payload["dataset"] == KEY.dataset
    aliases = built.aliases
    assert list(aliases) == sorted(aliases)
    # The ids of the records of ``entities``, in the order the dataset writes them
    # (requirement 20.17): three partners, two articles with one conversion record between
    # them, then the bills.
    assert aliases == {
        "vendor:main": "ven_000001",
        "vendor:spare": "ven_000002",
        "vendor:retired": "ven_000003",
        "article:bolt": "art_000001",
        "measure_link:bolt_box": "mlk_000001",
        "article:washer": "art_000002",
        "supply_order:fully_received": "so_000001",
        "goods_entry:full": "ge_000001",
        "supply_order:partly_received": "so_000002",
        "goods_entry:partial": "ge_000002",
        "supply_order:closed_by_hand": "so_000003",
        "fee_note:freight": "fn_000001",
        "pay_slip:freight_part": "ps_000001",
        "issue_note:workshop": "isu_000001",
        "stock_move:to_spare": "mv_000001",
        "count_sheet:gain": "cs_000001",
        "supply_order:waiting_approval": "so_000004",
        "supply_order:open_draft": "so_000005",
    }


# --- what the build created (requirements 20.2, 20.4 and 20.17) --------------------------


@pytest.mark.req("20.17")
def test_the_shared_data_comes_first_by_file_path_and_written_order(built: Built) -> None:
    # The ids and the generated codes follow the creation order, so they are what says in
    # which order the files of ``seed/shared/`` and their records were created: branch,
    # then depot, then measure, then operator.
    assert [(row["id"], row["code"], row["name"]) for row in built.rows("branch")] == [
        ("br_000001", "BR01", "夹具核算本部")
    ]
    assert [(row["id"], row["code"], row["branch_id"]) for row in built.rows("depot")] == [
        ("dep_000001", "D001", "br_000001"),
        ("dep_000002", "D002", "br_000001"),
    ]
    assert [(row["id"], row["code"]) for row in built.rows("measure")] == [
        ("msr_000001", "PCS"),
        ("msr_000002", "BOX"),
    ]
    assert [(row["id"], row["code"], row["duty"]) for row in built.rows("operator")] == [
        ("opr_000001", "OP01", "buyer"),
        ("opr_000002", "OP02", "keeper"),
    ]


@pytest.mark.req("20.2")
def test_a_declared_status_is_reached_by_the_operations_that_reach_it(built: Built) -> None:
    orders = {
        str(row["id"]): (row["status"], row["_close_mode"], row["number"])
        for row in built.rows("supply_order")
    }
    # The standard machine: submit, then approve, then the close that counts as a manual
    # one; the order the receipts closed is the automatic close of the conversion.
    assert orders == ORDER_STATES
    # The simple machine of the fee note reaches approved with an approve alone (7.11).
    fee = built.record("fee_note", "fn_000001")
    assert (fee["status"], fee["_close_mode"]) == ("approved", None)
    # Both receipts and the payment were approved, which is what moved the stock and wrote
    # the amounts back.
    assert [row["status"] for row in built.rows("goods_entry")] == ["approved", "approved"]
    assert [row["status"] for row in built.rows("pay_slip")] == ["approved"]


@pytest.mark.req("20.2")
def test_a_pushed_record_carries_its_own_header_and_writes_back(built: Built) -> None:
    entry = built.record("goods_entry", "ge_000002")
    # The header the dataset wrote is part of the push, so the number follows the business
    # date of the receipt and not the anchor date (requirements 20.2 and 4.4).
    assert (entry["biz_date"], entry["number"]) == (date(2026, 9, 18), "GRN202609180001")
    # The mapping of the conversion brought the partner, and the push recorded its source.
    assert (entry["vendor_id"], entry["source_bill_id"]) == ("ven_000001", "so_000002")
    payment = built.record("pay_slip", "ps_000001")
    assert (payment["biz_date"], payment["number"]) == (date(2026, 9, 20), "PAY202609200001")
    # A field neither the mapping nor the dataset covers takes the default of its field.
    assert payment["pay_mode"] == "transfer"
    # What the approves of the pushed bills wrote back (requirement 8.8).
    linked = {str(row["id"]): row["linked_qty"] for row in built.rows("supply_order_line")}
    assert linked["so_000001-1"] == Decimal("120")
    assert linked["so_000002-1"] == Decimal("400")
    assert linked["so_000002-2"] is None
    assert built.record("fee_note", "fn_000001")["linked_amount"] == Decimal("530")


@pytest.mark.req("20.2")
def test_the_history_leaves_the_stock_its_approves_moved(built: Built) -> None:
    balances = {
        (str(row[MATERIAL_COLUMN]), str(row[WAREHOUSE_COLUMN])): row[QTY_BASE_COLUMN]
        for row in built.rows(STOCK_BALANCE_TABLE)
    }
    assert balances == ON_HAND


@pytest.mark.req("20.4")
def test_bulk_generates_count_records_with_drawn_values_and_no_alias(built: Built) -> None:
    vendors = built.rows("vendor")
    # The three records of ``entities`` and the twelve of ``bulk[0]``.
    assert len(vendors) == 15
    background = vendors[3:]
    assert [str(row["code"]) for row in background] == [f"V{index:04d}" for index in range(4, 16)]
    assert not {str(row["id"]) for row in background} & set(built.aliases.values())
    for row in background:
        name = str(row["name"])
        # Every field the pack binds a generator to is drawn; the rest take their defaults.
        assert blacklist_hits(name, BLACKLIST) == ()
        assert row["short_name"] == short_name(name)
        assert isinstance(row["contact"], str)
        assert isinstance(row["pay_days"], int) and 15 <= row["pay_days"] <= 60
        assert row["default_tax_rate"] in RATES
        # A field no generator is bound to keeps the default of the configuration.
        assert row["status"] == "enabled"
    # The drawn names differ from one another, which is what the discard of a repeated name
    # keeps up (requirement 20.7).
    assert len({str(row["name"]) for row in vendors}) == len(vendors)


# --- the business context (requirement 20.5) ---------------------------------------------


@pytest.mark.req("20.5")
def test_the_business_context_is_the_anchor_date_and_the_current_user(tmp_path: Path) -> None:
    ref = build(tmp_path / "out", entities=dict(CONTEXT_RECORDS))
    note = _only(ref, "issue_note")
    # biz_date takes the anchor date and taker_id, whose default_context is user, the
    # current user of the dataset.
    assert note["biz_date"] == ANCHOR_DATE
    assert note["taker_id"] == USER


@pytest.mark.req("20.5")
def test_the_current_user_is_empty_until_its_record_is_created(tmp_path: Path) -> None:
    # A current user whose record the build never creates: the context has no user, so a
    # field that takes it stays empty. Whether that record exists at all is loading
    # step 8's to check (requirement 2.10), so the build itself gets through.
    ref = build(tmp_path / "out", current_user="opr_000009", entities=dict(CONTEXT_RECORDS))
    assert _only(ref, "issue_note")["taker_id"] is None
    assert _read_json(ref.meta)["current_user"] == "opr_000009"


# --- what a build fails over (requirements 20.9 and 20.15) -------------------------------


@pytest.mark.req("20.15")
def test_an_engine_error_at_a_create_names_the_operation_the_code_and_the_rule(
    tmp_path: Path,
) -> None:
    # A new order that references a disabled partner breaks R-GEN-006 (requirement 5.7).
    error = failure(
        tmp_path / "out",
        entities={
            "vendor:retired": {"kind": "vendor", "name": "夹具停用供货单位", "status": "disabled"},
            "article:part": ARTICLE,
            "supply_order:bad": {
                "kind": "supply_order",
                "vendor": "vendor:retired",
                "biz_date": "today",
                "need_date": "today+7d",
                "lines": [{"article": "article:part", "measure": "PCS", "qty": "1"}],
            },
        },
    )
    assert error.file == f"datasets/{DATASET}.yaml"
    assert error.config_path == "entities.supply_order:bad"
    assert error.reason.startswith(
        "create 操作返回错误 BUSINESS_RULE_VIOLATION，rule_id R-GEN-006："
    )


@pytest.mark.req("20.15")
def test_an_engine_error_at_a_state_operation_names_that_operation(tmp_path: Path) -> None:
    # Approving an issue note of an empty warehouse breaks R-GEN-007 (requirement 9.4).
    error = failure(
        tmp_path / "out",
        entities={
            "article:part": ARTICLE,
            "issue_note:bad": {**ISSUE, "taker": "OP01", "status": "approved"},
        },
    )
    assert error.config_path == "entities.issue_note:bad"
    assert error.reason.startswith(
        "approve 操作返回错误 BUSINESS_RULE_VIOLATION，rule_id R-GEN-007："
    )


@pytest.mark.req("20.6")
@pytest.mark.parametrize(
    ("record", "config_path", "reason"),
    [
        pytest.param(
            {**ARTICLE, "base_measure": "NOPE"},
            "entities.article:part.base_measure",
            "measure 中没有编码为 NOPE 的已创建记录",
            id="code-names-no-record",
        ),
        pytest.param(
            {**ARTICLE, "tax_rate": "abc"},
            "entities.article:part.tax_rate",
            "rate 字段 tax_rate 的值 'abc' 不是小数",
            id="value-is-no-decimal",
        ),
        pytest.param(
            {**ARTICLE, "spec": 12},
            "entities.article:part.spec",
            "string 字段 spec 不能写入 12",
            id="value-of-another-type",
        ),
    ],
)
def test_what_the_static_pass_leaves_to_the_build_is_reported_by_it(
    tmp_path: Path, record: Mapping[str, object], config_path: str, reason: str
) -> None:
    # The two things the static pass cannot settle: whether the code a reference names is
    # there among the records created so far, and whether a written value fits its field.
    error = failure(tmp_path / "out", entities={"article:part": record})
    assert (error.file, error.config_path) == (f"datasets/{DATASET}.yaml", config_path)
    assert error.reason == reason


@pytest.mark.req("20.9")
def test_a_broken_invariant_names_the_invariant_and_the_records(tmp_path: Path) -> None:
    loaded, _ = _fixture()
    plan = resolve(definition(entities={}), loaded.domain, loaded.packs, instance_user=USER)
    store = build_snapshot_db(tmp_path / "build.sqlite", build_schema(loaded.domain))
    with closing(store):
        with store.transaction() as uow:
            # Stock on hand that no transaction accounts for: INV-1 does not hold.
            uow.insert(
                STOCK_BALANCE_TABLE,
                {
                    "id": balance_id("art_000001", "dep_000001"),
                    MATERIAL_COLUMN: "art_000001",
                    WAREHOUSE_COLUMN: "dep_000001",
                    QTY_BASE_COLUMN: Decimal("7"),
                },
            )
        with pytest.raises(SeedError) as caught:
            _Build(Engine(loaded.domain), store, plan, ANCHOR_DATE, loaded.packs).run()
    error = caught.value
    assert (error.file, error.config_path) == (f"datasets/{DATASET}.yaml", None)
    assert error.reason.startswith("构建出的数据不满足不变量：")
    assert "INV-1 不成立（balance_not_sum_of_txn）" in error.reason
    assert f"{STOCK_BALANCE_TABLE} art_000001:dep_000001 的 {QTY_BASE_COLUMN}" in error.reason
    assert "实际为 7" in error.reason


# --- the example data of a business pack (requirement 20.8) ------------------------------


@pytest.mark.req("20.8")
def test_the_example_data_of_a_pack_has_no_part_in_a_snapshot(built: Built, tmp_path: Path) -> None:
    # A copy of the fixture packs with example data added to one of them; the Snapshot, the
    # metadata and the alias map have to come out byte for byte as they did without it.
    packs = tmp_path / "packs"
    shutil.copytree(PACKS_DIR, packs)
    examples = packs / BASE_PACK / "seed" / "examples"
    examples.mkdir(parents=True)
    (examples / "vendor.yaml").write_text(
        "records:\n  - { kind: vendor, name: 示例供货单位, short_name: 示例 }\n", "utf-8"
    )
    loader = Loader(INSTANCE_DIR, packs, ANCHOR_DATE)
    for step in IMPLEMENTED_STEPS:
        loader.run_step(step)
    document = next(doc for doc in loader.raw_datasets if doc.document.dataset == DATASET)
    ref = SeedBuilder(loader.assemble(), INSTANCE_DIR).build(
        document.document, ANCHOR_DATE, tmp_path / "out", file=document.file
    )
    for with_examples, without in zip(ref.files, built.ref.files, strict=True):
        assert with_examples.read_bytes() == without.read_bytes(), with_examples.name


# --- reading what a build wrote ----------------------------------------------------------


def _only(ref: SnapshotRef, table: str) -> Record:
    """The one record of ``table`` in the Snapshot ``ref``."""
    rows = _snapshot_rows(ref, table)
    assert len(rows) == 1, f"{table} 中有 {len(rows)} 条记录"
    return rows[0]


def _snapshot_rows(ref: SnapshotRef, table: str) -> list[Record]:
    """Every record of ``table`` in the Snapshot ``ref``, read through a copy of it."""
    loaded, _ = _fixture()
    sandbox = ref.directory / "sandbox"
    if not (sandbox / "db.sqlite").exists():
        copy_snapshot(ref.db, sandbox / "db.sqlite")
    store = SandboxStore(sandbox, build_schema(loaded.domain))
    store.open()
    with closing(store), store.transaction() as uow:
        return uow.select(table)


def _read_json(path: Path) -> Mapping[str, JsonValue]:
    """The JSON object ``path`` holds."""
    payload = json.loads(path.read_text("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _sha256(path: Path) -> str:
    """The SHA-256 of the bytes of ``path``, as the metadata writes it (req 20.12)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
