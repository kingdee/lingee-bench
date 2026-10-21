"""erp-mock-mcp validate and seed on the example instance (req 2.14, 22.5, 22.6, 22.9, 22.10).

Both commands are run through the installed command group with :class:`CliRunner`, so what
is asserted is what a deployer sees: the exit code, stdout and stderr, and what the two
commands left in the data directory. ``instances/p2p-demo`` and ``packs/`` of the repository
are what they are run on, which is also what ``scripts/check.py`` runs validate on, so a
document of the example instance or of the three example packs that no longer passes the
8 steps fails here.

The cost of that: loading p2p-demo and building the Snapshot of ``purchase-basic`` takes
some tenths of a second, so the two runs over the real instance happen once per module, in
:func:`validated` and :func:`seeded`, and the tests read their results. Both run in the same
data directory and in this order, which is what lets :func:`seeded` compare the cache entry
validate left with the one seed put in its place (requirement 22.6).

The failing cases need no build of p2p-demo: a ``--packs-dir`` without the packs fails at
step 1 and an unknown ``--dataset`` before any step but the first, while a build that fails
and the step 8 checks of seed are asserted on trees of one and two records written below
``tmp_path``.

p2p-demo has ``clock.mode`` today, so every run here passes ``--anchor-date``: no test reads
the system clock or the current date (requirement 25.10), and the anchor date is also what
names the cache entry both commands work on.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

import pytest
from click.testing import CliRunner, Result

from erp_mock.cli.main import main
from erp_mock.cli.options import EXIT_FAILED, EXIT_OK, EXIT_USAGE, SERVICE_LOGGER
from erp_mock.cli.seed import DATASET_OPTION
from erp_mock.loader.step4 import DATASETS_DIR
from erp_mock.loader.step8 import CURRENT_USER
from erp_mock.seed.builder import ALIASES_FILENAME, META_FILENAME, SNAPSHOT_FILENAME
from erp_mock.seed.cache import SNAPSHOTS_DIR
from tests.helpers.tree import (
    CONTEXT_FILES,
    dataset_doc,
    profile_doc,
    write_instance,
    write_pack,
)

# The example instance and the example business packs of the repository.
REPO_ROOT: Final = Path(__file__).resolve().parents[3]
INSTANCE_DIR: Final = REPO_ROOT / "instances" / "p2p-demo"
PACKS_DIR: Final = REPO_ROOT / "packs"

# The packs instance.yaml references, in its order, and its one initial dataset, as
# instance.yaml and the definition name them.
PACK_NAMES: Final = ("core", "purchase", "inventory")
DATASET: Final = "purchase-basic"
DATASET_VERSION: Final = "1.0.0"
DATASET_KEY: Final = f"{DATASET}@{DATASET_VERSION}"
DATASET_FILE: Final = f"{DATASETS_DIR}/{DATASET}.yaml"

# The anchor date every run is given: p2p-demo's clock is in mode today, which would read the
# system clock (requirement 25.10).
ANCHOR: Final = date(2026, 10, 9)

# The three files of a cache entry, in the order the assertions read them.
ENTRY_FILES: Final = (SNAPSHOT_FILENAME, META_FILENAME, ALIASES_FILENAME)

# The tree of the step 8 test: the two context entities of :data:`CONTEXT_FILES` as the one
# pack, and a dataset of the default organization and one employee. The current user names an
# internal id no record of that dataset has, which is what step 8 reports (requirement 2.10).
TREE_PACK: Final = "core"
TREE_ORG: Final = "'org_unit:main': { kind: org_unit, name: 甲组织, is_default: true }"
TREE_USER: Final = "'employee:clerk': { kind: employee, name: 甲员工 }"
MISSING_USER: Final = "emp_000009"
USER_ENTITY: Final = "employee"

# The ``kind`` of the record the build refuses, which the tree declares no entity for
# (requirement 20.6).
UNKNOWN_KIND: Final = "nothing"


# --- running a command -------------------------------------------------------------------


def run(*args: str) -> Result:
    """Invoke the installed command group with ``args``; restore the service log after.

    The group callback points the ``erp_mock`` logger at ``sys.stderr``, which is the
    runner's stream while a command runs (:func:`~erp_mock.cli.options.setup_logging`), so
    the handler is removed again afterwards rather than left pointing at a closed stream.
    """
    logger = logging.getLogger(SERVICE_LOGGER)
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    try:
        result = CliRunner().invoke(main, list(args))
    finally:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"命令以未预期的异常结束：{result.exception!r}"
    )
    return result


def validate_args(instance_dir: Path, packs_dir: Path, data_dir: Path) -> tuple[str, ...]:
    """A validate run over ``instance_dir`` at :data:`ANCHOR`."""
    return ("validate", *_common(instance_dir, packs_dir, data_dir))


def seed_args(instance_dir: Path, packs_dir: Path, data_dir: Path, *extra: str) -> tuple[str, ...]:
    """A seed run over ``instance_dir`` at :data:`ANCHOR`, with ``extra`` appended."""
    return ("seed", *_common(instance_dir, packs_dir, data_dir), *extra)


def _common(instance_dir: Path, packs_dir: Path, data_dir: Path) -> tuple[str, ...]:
    """The four options both commands are given, the anchor date among them (req 25.10)."""
    return (
        "--instance",
        str(instance_dir),
        "--packs-dir",
        str(packs_dir),
        "--data-dir",
        str(data_dir),
        "--anchor-date",
        ANCHOR.isoformat(),
    )


def entry_of(data_dir: Path) -> Path:
    """The cache entry of :data:`DATASET` at :data:`ANCHOR` below ``data_dir``."""
    return data_dir / SNAPSHOTS_DIR / DATASET_KEY / ANCHOR.isoformat()


def files_of(entry: Path) -> Mapping[str, tuple[int, bytes]]:
    """The inode and the content of each file of the cache entry ``entry``.

    The inode tells a file that was written again from one that was taken up: the cache
    replaces an entry by renaming a directory into place, so a rebuilt entry holds other
    files (design.md 6.2), while the content of those files is the same whenever the build
    is (requirement 20.13).
    """
    return {
        name: ((entry / name).stat().st_ino, (entry / name).read_bytes()) for name in ENTRY_FILES
    }


@dataclass(frozen=True, slots=True)
class Seeded:
    """What the one seed run over p2p-demo did: its result and the entry before and after."""

    result: Result
    before: Mapping[str, tuple[int, bytes]]
    after: Mapping[str, tuple[int, bytes]]


# --- the two runs over the example instance ----------------------------------------------


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The ``--data-dir`` the validate and the seed run of this module share."""
    return tmp_path_factory.mktemp("cli-validate-seed")


@pytest.fixture(scope="module")
def validated(data_dir: Path) -> Result:
    """``validate`` over p2p-demo at :data:`ANCHOR`, run once for the module."""
    return run(*validate_args(INSTANCE_DIR, PACKS_DIR, data_dir))


@pytest.fixture(scope="module")
def seeded(data_dir: Path, validated: Result) -> Seeded:
    """``seed`` over p2p-demo in the data directory the validate run filled.

    ``validated`` is a fixture of this one, so the cache holds the entry that run took up
    before this one replaces it.
    """
    assert validated.exit_code == EXIT_OK, f"validate 必须先通过：{validated.stderr}"
    before = files_of(entry_of(data_dir))
    result = run(*seed_args(INSTANCE_DIR, PACKS_DIR, data_dir))
    after = files_of(entry_of(data_dir)) if result.exit_code == EXIT_OK else {}
    return Seeded(result=result, before=before, after=after)


# --- validate (requirements 22.5 and 2.14) -----------------------------------------------


@pytest.mark.req("22.5")
def test_validate_passes_on_the_example_instance(validated: Result) -> None:
    assert validated.exit_code == EXIT_OK, f"p2p-demo 的全部校验应通过：{validated.stderr}"
    # Requirement 22.5 asks for the exit code alone, so stdout stays empty and what was
    # validated is a line of the log, on stderr.
    assert validated.stdout == "", "validate 不向 stdout 输出内容"
    assert f"业务包 {len(PACK_NAMES)} 个，初始数据集 1 个" in validated.stderr
    assert ANCHOR.isoformat() in validated.stderr, "stderr 说明本次校验的锚点日期"


@pytest.mark.req("2.14")
def test_validate_takes_up_the_snapshot_of_every_dataset_at_the_anchor_date(
    validated: Result, data_dir: Path
) -> None:
    assert validated.exit_code == EXIT_OK, f"p2p-demo 的全部校验应通过：{validated.stderr}"
    entry = entry_of(data_dir)

    # Step 8 took up the Snapshot of the one dataset, which it had to build: the three files
    # of requirement 20.10 are in the entry of (dataset@version, anchor date).
    assert sorted(path.name for path in entry.iterdir()) == sorted(ENTRY_FILES)
    meta = json.loads((entry / META_FILENAME).read_text(encoding="utf-8"))
    assert meta["dataset"] == DATASET
    assert meta["version"] == DATASET_VERSION
    # The anchor date of the build is the ``--anchor-date`` of the command, not a date of
    # the clock configuration, whose mode is today (requirement 2.14).
    assert meta["anchor_date"] == ANCHOR.isoformat()


@pytest.mark.req("22.5")
def test_validate_writes_the_error_list_to_stderr_and_exits_1(tmp_path: Path) -> None:
    # The packs the instance references are not below --packs-dir, which step 1 reports for
    # each of them (requirement 2.3); nothing after step 1 runs.
    result = run(*validate_args(INSTANCE_DIR, tmp_path / "packs", tmp_path / "var"))

    assert result.exit_code == EXIT_FAILED, "校验失败的退出码必须是 1"
    assert result.stdout == "", "校验失败时 stdout 不输出任何内容"
    lines = result.stderr.splitlines()
    assert len(lines) == len(PACK_NAMES), f"每个业务包一行：{lines}"
    assert all(line.startswith("[step 1] instance.yaml: packs.") for line in lines), lines
    assert sorted(PACK_NAMES) == [line.split("packs.")[1].split(":")[0] for line in lines]
    assert "Error:" not in result.stderr, "错误清单不带 click 自己的前缀"
    assert not (tmp_path / "var" / SNAPSHOTS_DIR).exists(), "校验失败时不构建任何 Snapshot"


# --- seed (requirements 22.6 and 22.10) --------------------------------------------------


@pytest.mark.req("22.10")
def test_seed_writes_one_line_per_dataset_on_stdout(seeded: Seeded, data_dir: Path) -> None:
    result = seeded.result

    assert result.exit_code == EXIT_OK, f"p2p-demo 的 seed 应成功：{result.stderr}"
    lines = result.stdout.splitlines()
    assert len(lines) == 1, f"按 instance.yaml 的 datasets 顺序每个数据集一行：{lines}"
    entry = entry_of(data_dir)
    assert lines[0].split("\t") == [
        DATASET_KEY,
        ANCHOR.isoformat(),
        str(entry / SNAPSHOT_FILENAME),
        str(entry / ALIASES_FILENAME),
    ], "一行含名称@版本、锚点日期、Snapshot 与别名映射文件的路径"
    assert all(Path(field).is_file() for field in lines[0].split("\t")[2:]), "两个路径都存在"


@pytest.mark.req("22.6")
def test_seed_rebuilds_the_snapshot_instead_of_taking_the_cached_one(seeded: Seeded) -> None:
    assert seeded.result.exit_code == EXIT_OK, f"p2p-demo 的 seed 应成功：{seeded.result.stderr}"

    for name in ENTRY_FILES:
        inode_before, content_before = seeded.before[name]
        inode_after, content_after = seeded.after[name]
        assert inode_after != inode_before, f"{name} 是重建写出的新文件，不是缓存里那个"
        # Two builds of one dataset at one anchor date write the same bytes, so a rebuild
        # that was really carried out is still invisible in the content (requirement 20.13).
        assert content_after == content_before, f"{name} 的内容与上次构建逐字节一致"


@pytest.mark.req("22.6")
def test_seed_builds_the_one_dataset_the_option_names(data_dir: Path, seeded: Seeded) -> None:
    assert seeded.result.exit_code == EXIT_OK, f"p2p-demo 的 seed 应成功：{seeded.result.stderr}"

    result = run(*seed_args(INSTANCE_DIR, PACKS_DIR, data_dir, DATASET_OPTION, DATASET))

    assert result.exit_code == EXIT_OK, f"--dataset 指定实例中的数据集应成功：{result.stderr}"
    assert result.stdout == seeded.result.stdout, "所选数据集就是缺省构建的那一个"


@pytest.mark.req("22.9")
def test_an_unknown_dataset_is_a_usage_error_and_builds_nothing(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    # The name differs from the one of instance.yaml in case alone (requirement 22.9).
    asked = DATASET.replace("purchase", "Purchase")

    result = run(*seed_args(INSTANCE_DIR, PACKS_DIR, data_dir, DATASET_OPTION, asked))

    assert result.exit_code == EXIT_USAGE, "实例中没有的 --dataset 是用法错误，退出码 2"
    assert result.stdout == "", "stdout 不输出任何内容"
    assert DATASET_OPTION in result.stderr and asked in result.stderr, (
        f"stderr 指出该选项及其值：{result.stderr}"
    )
    assert not data_dir.exists(), "不构建任何 Snapshot"


@pytest.mark.req("22.6", "22.8")
def test_a_build_that_failed_ends_the_command_with_exit_code_1(tmp_path: Path) -> None:
    # A dataset whose one record names a kind the instance does not have, which the static
    # pass of the build refuses (requirement 20.6); no step reports it, since what a record
    # may say is not a matter of the JSON Schema of the definition.
    instance_dir = write_instance(
        tmp_path / "instance",
        {
            "profiles/semantic.yaml": profile_doc(),
            DATASET_FILE: dataset_doc(
                dataset=DATASET, entities=f"{{ 'nothing:one': {{ kind: {UNKNOWN_KIND} }} }}"
            ),
        },
        current_user=None,
    )
    packs_dir = tmp_path / "packs"
    write_pack(packs_dir, TREE_PACK, files=dict(CONTEXT_FILES))
    data_dir = tmp_path / "var"

    result = run(*seed_args(instance_dir, packs_dir, data_dir))

    assert result.exit_code == EXIT_FAILED, "Snapshot 构建失败的退出码必须是 1"
    assert result.stdout == "", "构建失败时 stdout 不输出任何内容"
    assert "Snapshot 构建失败" in result.stderr, result.stderr
    assert DATASET_FILE in result.stderr and UNKNOWN_KIND in result.stderr, (
        f"stderr 指出数据集定义文件与原因：{result.stderr}"
    )
    assert not entry_of(data_dir).exists(), "构建失败时缓存保持不变"


@pytest.mark.req("22.6")
def test_seed_runs_the_step_8_checks_over_what_it_built(tmp_path: Path) -> None:
    # A tree whose build gets through and whose Snapshot then fails a check of step 8: the
    # current user names an internal id no record of the dataset has (requirement 2.10).
    instance_dir = write_instance(
        tmp_path / "instance",
        {
            "profiles/semantic.yaml": profile_doc(),
            DATASET_FILE: dataset_doc(
                dataset=DATASET,
                current_user=MISSING_USER,
                entities="{ " + TREE_ORG + ", " + TREE_USER + " }",
            ),
        },
    )
    packs_dir = tmp_path / "packs"
    write_pack(packs_dir, TREE_PACK, files=dict(CONTEXT_FILES))
    data_dir = tmp_path / "var"

    result = run(*seed_args(instance_dir, packs_dir, data_dir))

    assert result.exit_code == EXIT_FAILED, "第 8 步的检查不通过时退出码是 1"
    assert result.stdout == "", "检查不通过时 stdout 不输出任何内容"
    assert result.stderr.splitlines() == [
        f"[step 8] {DATASET_FILE}: {CURRENT_USER}: 初始数据集 {DATASET} 的当前用户"
        f" {MISSING_USER} 不是 Snapshot 中 {USER_ENTITY} 的记录"
    ], result.stderr
    # The checks ran over the Snapshot this command built, which is in the cache either way:
    # a build that got through replaces the entry, and only the checks after it failed.
    assert (entry_of(data_dir) / SNAPSHOT_FILENAME).is_file(), "检查前确实重建了 Snapshot"
