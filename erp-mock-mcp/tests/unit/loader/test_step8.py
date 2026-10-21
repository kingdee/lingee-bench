"""Loading step 8 and the cross-check after it (requirements 2.10, 2.14 and 3.5).

Three parts, as the step itself has three:

- the engine test fixture passes every step on the real directories, so its dataset is a
  worked example of what step 8 accepts: a Snapshot in which the four invariants hold, whose
  current user is a record of the user context entity and which holds exactly one default
  organization, and descriptions that name none of its records. What the step leaves behind
  is asserted there too - one :class:`~erp_mock.loader.instance.DatasetModel` per dataset,
  with the cache entry it took up, and the anchor date it was taken under;
- the invariants are checked over a Snapshot that came out of the cache as well
  (requirement 2.14), which is asserted by changing a cached file behind the cache's back:
  the Seed_Builder never writes a Snapshot that breaks one (requirement 20.9), so that is
  the only way such a Snapshot comes about;
- one example per kind of error, each on a tree written below ``tmp_path`` that holds
  exactly the one thing that is wrong, asserted by the places and the reasons the loader
  reports.

:data:`BASE_FILES` is the pack every tree is written against: the two context entities and
nothing else, so the tree generates no tool and needs no description file, and its dataset
is two records - the default organization and the record the current user names. The
cross-check needs a tool to put text on, so :func:`user_get_files` gives the user entity a
``get`` operation and writes the descriptions of the tool that comes out of it.
"""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Final

import pytest

from erp_mock.loader.errors import CROSS, ConfigError, ConfigErrors, Step
from erp_mock.loader.loader import STEPS, Loader
from erp_mock.loader.step1 import INSTANCE_FILE
from erp_mock.loader.step4 import DATASETS_DIR, DESCRIPTION_VARIANT
from erp_mock.loader.step7 import description_file
from erp_mock.loader.step8 import CURRENT_USER, anchor_of
from erp_mock.seed.builder import ALIASES_FILENAME, META_FILENAME, SNAPSHOT_FILENAME
from erp_mock.seed.cache import SNAPSHOTS_DIR
from erp_mock.state.schema import QTY_BASE_COLUMN, STOCK_BALANCE_TABLE
from tests.helpers.fixtures import ANCHOR_DATE, DATASET, INSTANCE_DIR, PACKS_DIR, load_fixture
from tests.helpers.tree import (
    CONTEXT_FILES,
    dataset_doc,
    entity_doc,
    profile_doc,
    write_instance,
    write_pack,
)

# The two directories of a tree below ``tmp_path`` and the data directory of its loader.
INSTANCE: Final = "instance"
PACKS: Final = "packs"
DATA: Final = "data"

# The pack the default instance.yaml of tests.helpers.tree references, and its one dataset.
PACK: Final = "core"
DATASET_NAME: Final = "purchase-basic"
DATASET_FILE: Final = f"{DATASETS_DIR}/{DATASET_NAME}.yaml"

# The two context entities of :data:`CONTEXT_FILES`, the bool field that marks the default
# organization, and the internal id the first record of the user entity gets (req 4.1).
ORG_ENTITY: Final = "org_unit"
USER_ENTITY: Final = "employee"
DEFAULT_FLAG: Final = "is_default"
FIRST_USER: Final = "emp_000001"
MISSING_USER: Final = "emp_000009"

# The anchor date every tree is loaded under, passed as the CLI passes ``--anchor-date``:
# the trees write no clock, whose default mode is today (requirement 16.3).
ANCHOR: Final = date(2026, 10, 9)

# The records of the dataset of a tree, each written as one flow mapping: the default
# organization, a second one, an organization that is no default, and the user record.
ORG_RECORD: Final = f"'{ORG_ENTITY}:main': {{ kind: {ORG_ENTITY}, name: 甲组织, is_default: true }}"
SPARE_ORG: Final = f"'{ORG_ENTITY}:spare': {{ kind: {ORG_ENTITY}, name: 乙组织, is_default: true }}"
PLAIN_ORG: Final = f"'{ORG_ENTITY}:plain': {{ kind: {ORG_ENTITY}, name: 甲组织 }}"
USER_RECORD: Final = f"'{USER_ENTITY}:clerk': {{ kind: {USER_ENTITY}, name: 甲员工 }}"

# The names of those records, which the cross-check looks for in the descriptions.
ORG_NAME: Final = "甲组织"
USER_NAME: Final = "甲员工"

# The pack of every tree: the two context entities, which declare no operation and so
# generate no tool (requirement 11.1) and need no description file.
BASE_FILES: Final[Mapping[str, str]] = dict(CONTEXT_FILES)

# The tool of the cross-check tree, its one parameter and the file its descriptions are in.
USER_TOOL: Final = f"{USER_ENTITY}_get"
TOOL_PARAM: Final = "id"
VARIANT_FILE: Final = description_file(DESCRIPTION_VARIANT)

# A tool description in the parts of tool-design.md section 3 and a parameter description,
# neither naming a guiding phrase nor a record of the dataset.
TOOL_TEXT: Final = (
    "按内部 ID 获取一条记录的全部字段。\n"
    "前置条件：id 由 search 工具返回。\n"
    "副作用：无，只读。\n"
    "返回：记录的全部字段，值为空的字段返回 null。"
)
PARAM_TEXT: Final = "参数的含义、格式与取值来源；不填时的缺省行为见工具描述"

# One place of a reported error: the pack, the file and the config path.
type Place = tuple[str | None, str, str | None]


# --- the engine test fixture -------------------------------------------------------------


@pytest.mark.req("2.10", "2.14", "3.5")
def test_the_fixture_passes_step_8_and_the_cross_check(tmp_path: Path) -> None:
    loader = load_fixture(CROSS, data_dir=tmp_path)

    # Both raise ConfigErrors when they find anything, so a fixture whose Snapshot breaks an
    # invariant, whose current user is no record of the user entity, which holds no single
    # default organization, or whose descriptions name one of its records fails here.
    result = loader.step8
    assert result is not None
    assert [model.name for model in result.datasets] == [DATASET]
    model = result.datasets[0]
    assert model.file == f"{DATASETS_DIR}/{DATASET}.yaml"
    # The current user of the dataset, which it and instance.yaml agree on here.
    assert model.current_user == "opr_000001"
    # The entry the step took up, with the three files of a Snapshot in it.
    entry = tmp_path / SNAPSHOTS_DIR / f"{DATASET}@1.0.0" / ANCHOR_DATE.isoformat()
    assert model.snapshot == entry
    assert sorted(path.name for path in entry.iterdir()) == [
        ALIASES_FILENAME,
        META_FILENAME,
        SNAPSHOT_FILENAME,
    ]


@pytest.mark.req("2.14")
def test_the_loaded_instance_carries_the_datasets_and_the_anchor_date(tmp_path: Path) -> None:
    loaded = load_fixture(CROSS, data_dir=tmp_path).assemble()

    assert loaded.step8_anchor == ANCHOR_DATE
    assert list(loaded.datasets) == [DATASET]
    assert loaded.default_dataset is loaded.datasets[DATASET]
    assert loaded.default_dataset.version == loaded.versions.datasets[DATASET]
    assert loaded.default_dataset.definition.dataset == DATASET


@pytest.mark.req("3.5")
def test_the_business_data_holds_the_names_codes_and_numbers_of_the_snapshot(
    tmp_path: Path,
) -> None:
    result = load_fixture(CROSS, data_dir=tmp_path).step8
    assert result is not None
    texts = result.data.texts

    # Every text comes from the one dataset of the fixture; the names and the codes of its
    # Shared_Data are among them, as is the number of a bill its history created.
    assert set(texts.values()) == {DATASET}
    assert {"夹具核算本部", "BR01", "夹具主料仓", "D001", "PCS", "夹具主力供货单位"} <= set(texts)
    assert any(text.startswith("SPO") for text in texts), "单据编号也参与比对"
    # An empty value and an empty string do not take part in the comparison.
    assert "" not in texts


@pytest.mark.req("2.14")
def test_a_cached_snapshot_is_checked_again_after_it_was_changed(tmp_path: Path) -> None:
    # The first load builds the Snapshot and caches it; the Seed_Builder checks the
    # invariants itself, so what the cache holds satisfies them (requirement 20.9).
    first = load_fixture(CROSS, data_dir=tmp_path)
    assert first.step8 is not None
    entry = first.step8.datasets[0].snapshot
    assert entry is not None

    # The stock on hand of every pair is set to 0 while its transactions stay as they are,
    # which breaks INV-1. ``meta.json`` is untouched, so the entry still fits and the second
    # load takes it up rather than building it again (requirement 20.12).
    with closing(sqlite3.connect(entry / SNAPSHOT_FILENAME)) as connection:
        connection.execute(f"UPDATE {STOCK_BALANCE_TABLE} SET {QTY_BASE_COLUMN} = '0'")
        connection.commit()

    with pytest.raises(ConfigErrors) as caught:
        load_fixture(CROSS, data_dir=tmp_path)

    found = caught.value.errors
    assert caught.value.step == 8
    # One error per place the invariant does not hold, each at the dataset definition and
    # with no config path: no configuration item corresponds to it (requirement 2.2).
    assert set(places(found)) == {(None, f"{DATASETS_DIR}/{DATASET}.yaml", None)}
    assert all("INV-1 不成立" in reason for reason in reasons(found)), reasons(found)
    assert any("balance_not_sum_of_txn" in reason for reason in reasons(found)), reasons(found)


# --- the anchor date of the step (requirement 2.14) --------------------------------------


@pytest.mark.req("2.14")
def test_the_anchor_date_is_the_given_one_or_the_one_of_a_fixed_clock(tmp_path: Path) -> None:
    given = date(2031, 2, 28)
    loader = Loader(INSTANCE_DIR, PACKS_DIR, given, tmp_path)
    loader.run_step(1)
    assert loader.instance is not None

    # The date of the command line wins over the clock configuration, which the fixture
    # fixes to ANCHOR_DATE (requirement 16.3).
    assert loader.anchor() == given
    assert anchor_of(loader.instance, given) == given
    assert anchor_of(loader.instance, None) == ANCHOR_DATE


@pytest.mark.req("2.14")
def test_a_clock_in_mode_today_takes_the_date_from_the_caller(tmp_path: Path) -> None:
    # Only the gateway's SystemClock reads the system time (requirement 24.7), so the loader
    # is given the date of a clock in mode today instead of working it out.
    target = write_core(tmp_path, anchor_date=None)
    target.run_step(1)
    assert target.instance is not None
    assert target.instance.clock.mode == "today"

    with pytest.raises(AssertionError, match="第 8 步锚点日期"):
        anchor_of(target.instance, None)
    with pytest.raises(AssertionError, match="第 8 步锚点日期"):
        target.anchor()
    assert anchor_of(target.instance, ANCHOR) == ANCHOR


@pytest.mark.req("2.14")
def test_step_8_needs_a_data_directory(tmp_path: Path) -> None:
    written = write_core(tmp_path)
    target = Loader(written.instance_dir, written.packs_dir, ANCHOR)
    for step in (1, 2, 3, 4, 5, 6, 7):
        target.run_step(step)

    with pytest.raises(AssertionError, match="数据目录"):
        target.run_step(8)


# --- the trees of the error cases --------------------------------------------------------


def records(*written: str) -> str:
    """The ``entities`` of a dataset: the records ``written``, as one flow mapping."""
    return "{ " + ", ".join(written) + " }"


def write_core(
    tmp_path: Path,
    *,
    entities: str | None = None,
    current_user: str | None = FIRST_USER,
    instance_user: str | None = None,
    files: Mapping[str, str] | None = None,
    anchor_date: date | None = ANCHOR,
    **manifest: str,
) -> Loader:
    """A loader for an instance of the one pack ``core``, written below ``tmp_path``.

    ``entities`` are the records of its dataset, the default organization and the user
    record by default; ``current_user`` is the ``current_user`` of the dataset and
    ``instance_user`` that of instance.yaml, either left out when it is None. ``files``
    replaces the files of the pack (:data:`BASE_FILES`) and ``manifest`` overrides keys of
    its pack.yaml. ``anchor_date`` is passed as ``--anchor-date`` and the data directory is
    ``tmp_path/data``.
    """
    write_instance(
        tmp_path / INSTANCE,
        {
            "profiles/semantic.yaml": profile_doc(),
            DATASET_FILE: dataset_doc(
                dataset=DATASET_NAME,
                current_user=current_user,
                entities=entities if entities is not None else records(ORG_RECORD, USER_RECORD),
            ),
        },
        current_user=instance_user,
    )
    write_pack(tmp_path / PACKS, PACK, files=dict(files or BASE_FILES), **manifest)
    return Loader(tmp_path / INSTANCE, tmp_path / PACKS, anchor_date, tmp_path / DATA)


def user_get_files(description: str = TOOL_TEXT, param: str = PARAM_TEXT) -> Mapping[str, str]:
    """The pack files of the cross-check tree: a ``get`` tool on the user entity.

    ``description`` is the tool description of that tool and ``param`` the description of
    the one parameter it takes; both are needed for step 7 to pass (requirement 3.2), and
    either is where a test puts a text of the Business_Data.
    """
    return {
        **BASE_FILES,
        f"entities/{USER_ENTITY}.yaml": entity_doc(
            entity=USER_ENTITY,
            id_prefix="emp",
            context="{ role: user }",
            fields="{ name: { type: string } }",
            operations="[get]",
        ),
        VARIANT_FILE: (
            f"variant: {DESCRIPTION_VARIANT}\n"
            "tools:\n"
            f"  {USER_TOOL}:\n"
            f"    description: {_scalar(description)}\n"
            "    params:\n"
            f"      {TOOL_PARAM}: {_scalar(param)}\n"
        ),
    }


def _scalar(text: str) -> str:
    """``text`` as a double-quoted YAML scalar; JSON string syntax is a subset of it."""
    return json.dumps(text, ensure_ascii=False)


def failures(target: Loader, step: Step = 8) -> tuple[ConfigError, ...]:
    """The errors ``step`` reports for ``target``, in report order; it must find some.

    The steps before ``step`` run first and must pass, so the tree holds exactly the one
    thing the test is about.
    """
    for earlier in STEPS:
        if earlier == step:
            break
        target.run_step(earlier)
    with pytest.raises(ConfigErrors) as caught:
        target.run_step(step)
    assert caught.value.step == step
    return caught.value.errors


def places(errors: Sequence[ConfigError]) -> list[Place]:
    """The places of ``errors``, in report order."""
    return [
        (error.pack, location.file, location.config_path)
        for error in errors
        for location in error.locations
    ]


def reasons(errors: Sequence[ConfigError]) -> list[str]:
    """The reasons of ``errors``, in report order."""
    return [error.reason for error in errors]


@pytest.mark.req("2.10", "2.14")
def test_a_tree_whose_snapshot_holds_the_user_and_one_default_organization_passes(
    tmp_path: Path,
) -> None:
    target = write_core(tmp_path)
    loaded = target.load()

    assert list(loaded.datasets) == [DATASET_NAME]
    model = loaded.datasets[DATASET_NAME]
    assert model.current_user == FIRST_USER
    assert model.snapshot is not None and model.snapshot.is_dir()
    assert loaded.step8_anchor == ANCHOR
    # The names of the two records are the Business_Data the cross-check compares against;
    # neither entity has a code rule and the pack declares no bill, so there is nothing else.
    assert target.step8 is not None
    assert dict(target.step8.data.texts) == {
        ORG_NAME: DATASET_NAME,
        USER_NAME: DATASET_NAME,
    }


# --- the current user (requirement 2.10) -------------------------------------------------


@pytest.mark.req("2.10")
def test_a_current_user_that_is_no_record_of_the_user_entity(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, current_user=MISSING_USER))

    assert places(found) == [(None, DATASET_FILE, CURRENT_USER)]
    assert reasons(found) == [
        f"初始数据集 {DATASET_NAME} 的当前用户 {MISSING_USER} 不是 Snapshot 中 {USER_ENTITY} 的记录"
    ]


@pytest.mark.req("2.10")
def test_a_current_user_of_instance_yaml_that_is_no_record_either(tmp_path: Path) -> None:
    # The dataset sets none, so the one of instance.yaml is the current user of the build
    # and the error is reported at that file (requirement 20.5).
    found = failures(write_core(tmp_path, current_user=None, instance_user=MISSING_USER))

    assert places(found) == [(None, INSTANCE_FILE, CURRENT_USER)]
    assert reasons(found) == [
        f"初始数据集 {DATASET_NAME} 的当前用户 {MISSING_USER} 不是 Snapshot 中 {USER_ENTITY} 的记录"
    ]


@pytest.mark.req("2.10")
def test_a_current_user_that_neither_the_dataset_nor_the_instance_sets(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, current_user=None, instance_user=None))

    # Either file could carry it, so the error names both places (requirement 2.2).
    assert places(found) == [
        (None, DATASET_FILE, CURRENT_USER),
        (None, INSTANCE_FILE, CURRENT_USER),
    ]
    assert reasons(found) == [
        f"初始数据集 {DATASET_NAME} 没有当前用户：数据集与 instance.yaml 的 {CURRENT_USER} 都没有设置"
    ]


# --- the default organization (requirements 1.8 and 2.10) --------------------------------


@pytest.mark.req("2.10")
def test_two_records_of_the_org_entity_carry_the_default_flag(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, entities=records(ORG_RECORD, SPARE_ORG, USER_RECORD)))

    assert places(found) == [(None, DATASET_FILE, None)]
    assert reasons(found) == [
        f"初始数据集 {DATASET_NAME} 的 Snapshot 中 {ORG_ENTITY} 的 {DEFAULT_FLAG} 为 true 的记录"
        "有 2 条，必须恰好 1 条"
    ]


@pytest.mark.req("2.10")
def test_no_record_of_the_org_entity_carries_the_default_flag(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, entities=records(PLAIN_ORG, USER_RECORD)))

    assert places(found) == [(None, DATASET_FILE, None)]
    assert reasons(found) == [
        f"初始数据集 {DATASET_NAME} 的 Snapshot 中 {ORG_ENTITY} 的 {DEFAULT_FLAG} 为 true 的记录"
        "有 0 条，必须恰好 1 条"
    ]


# --- the cross-check (requirement 3.5) ---------------------------------------------------


@pytest.mark.req("3.5")
def test_a_tool_description_that_contains_a_name_of_the_business_data(tmp_path: Path) -> None:
    description = f"按内部 ID 获取一条记录，如 {USER_NAME} 那一条。\n副作用：无，只读。"
    target = write_core(
        tmp_path, files=user_get_files(description), tool_prefixes=f"[{USER_ENTITY}_]"
    )
    found = failures(target, CROSS)

    assert places(found) == [(PACK, VARIANT_FILE, f"tools.{USER_TOOL}.description")]
    assert reasons(found) == [
        f"{DESCRIPTION_VARIANT} 变体中工具 {USER_TOOL} 的工具描述含初始数据集 "
        f"{DATASET_NAME} 的业务数据文本 {USER_NAME}"
    ]


@pytest.mark.req("3.5")
def test_a_parameter_description_that_contains_a_name_of_the_business_data(
    tmp_path: Path,
) -> None:
    files = user_get_files(param=f"记录的内部 ID，如 {ORG_NAME} 那一条的 ID")
    found = failures(write_core(tmp_path, files=files, tool_prefixes=f"[{USER_ENTITY}_]"), CROSS)

    assert places(found) == [(PACK, VARIANT_FILE, f"tools.{USER_TOOL}.params.{TOOL_PARAM}")]
    assert reasons(found) == [
        f"{DESCRIPTION_VARIANT} 变体中工具 {USER_TOOL} 的参数 {TOOL_PARAM} 的描述含初始数据集 "
        f"{DATASET_NAME} 的业务数据文本 {ORG_NAME}"
    ]


@pytest.mark.req("3.5")
def test_descriptions_that_name_no_record_of_the_business_data_pass(tmp_path: Path) -> None:
    target = write_core(tmp_path, files=user_get_files(), tool_prefixes=f"[{USER_ENTITY}_]")
    loaded = target.load()

    # The tool is generated and described, and its text names neither record of the dataset.
    assert loaded.default_profile.tool_names[-1] == USER_TOOL
    assert target.step8 is not None
    assert set(target.step8.data.texts) == {ORG_NAME, USER_NAME}
