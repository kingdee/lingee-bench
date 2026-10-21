"""Building purchase-basic across processes: the same three files, whoever builds them.

tests/property/test_p31_seed_determinism.py compares two builds inside one process and
tests/property/test_p32_anchor_validation.py loads p2p-demo at any anchor date of the range;
this file puts the same dataset through what only separate processes can show (task 26.5,
requirement 25.4):

- two processes, two ``--data-dir`` below ``tmp_path``, two working directories, two
  ``PYTHONHASHSEED`` values and two ``TZ`` values each build ``purchase-basic`` at
  :data:`ANCHORS` - the three anchor dates requirement 25.4 names and
  :data:`RANDOM_ANCHORS` drawn ones - and the Snapshot, the metadata and the alias map of
  every one of those dates are byte for byte the same in both (requirement 20.13);
- every one of those dates passes the whole validation, in both processes, which is the
  ``validate`` of requirement 22.5 over the instance of requirement 21.12;
- the alias map is the same file at every anchor date: an internal id comes from the
  creation order and from nothing a date decides (requirement 20.14);
- a build that differs only in ``random_seed`` keeps the written field values, every
  internal id and the alias map, so the seed moves drawn values and nothing else
  (requirement 20.3);
- the example data of a business pack takes no part in a build: changing it and deleting it
  leave the three files as they were (requirement 20.8);
- a drawn value that contains an entry of the company name blacklist is discarded and the
  next value comes from the same sequence; a field whose every value is blacklisted ends
  the build at the discard limit (requirement 20.7).

What runs where. A build is some tenths of a second and a whole validation about a third of
one, so 23 anchor dates in two processes is the one thing here that costs real time
(:data:`PROCESS_TIMEOUT_S` bounds it) and it is run once for the module
(:func:`processes`). Everything after that is in-process: the clauses about the seed, the
example data and the blacklist compare builds of their own, which
:func:`~tests.property.test_p31_seed_determinism.build_at` carries out in a temporary
directory - the fixtures of property 31 are shared rather than written again, as design.md
asks of this test.

How the two processes run the command. The worker of :data:`WORKER_SOURCE` calls
``erp_mock.cli.main:main``, which is the entry point ``[project.scripts]`` installs, once
per anchor date in a process of its own, rather than spawning the console script 23 times:
the console script is spawned once, at the leap day, in a third process and a third data
directory (:data:`INSTALLED_ANCHOR`), and its three files are compared against the workers'
along with everything else. Each process is given the environment it needs and nothing of
the machine's ``ERP_MOCK_*`` (:data:`PASSED_THROUGH`, requirement 23.9), and every wait is a
poll against a deadline.

No test here reads the system clock: every run passes ``--anchor-date`` or hands the date to
the builder, which is what lets the module pass on any day (requirement 25.10). The two
``TZ`` values the workers run under are part of that - p2p-demo's clock is in mode today, so
a date read from the clock rather than from the option would differ between them.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from random import Random
from typing import Final

import pytest

from erp_mock.cli.options import EXIT_OK
from erp_mock.config.pack import GeneratorObject
from erp_mock.loader.step4 import DATASETS_DIR
from erp_mock.seed.builder import ALIASES_FILENAME, META_FILENAME, SNAPSHOT_FILENAME
from erp_mock.seed.cache import SNAPSHOTS_DIR
from erp_mock.seed.generators import DISCARD_LIMIT, ValueGenerators, blacklist_hits
from erp_mock.seed.resolve import Drawn, SeedError
from tests.helpers.tree import (
    CONTEXT_FILES,
    dataset_doc,
    entity_doc,
    profile_doc,
    write_instance,
    write_pack,
)
from tests.property.test_p31_seed_determinism import (
    P2P_CASE,
    Built,
    Case,
    build_at,
    difference,
    target,
    written_values,
)

# --- what is built (requirement 21.2) ----------------------------------------------------

# The example instance of the repository, its business packs and its one initial dataset,
# taken from the case property 31 builds, so both work on the same three (:data:`P2P_CASE`).
INSTANCE_DIR: Final = P2P_CASE.instance_dir
PACKS_DIR: Final = P2P_CASE.packs_dir
DATASET: Final = P2P_CASE.dataset
DATASET_VERSION: Final = "1.0.0"
DATASET_KEY: Final = f"{DATASET}@{DATASET_VERSION}"

# The three files of one Snapshot, in the order the assertions read them, inside the
# directory of its cache entry (requirement 20.10, design.md data model 2).
ENTRY_FILES: Final = (SNAPSHOT_FILENAME, META_FILENAME, ALIASES_FILENAME)

# The business pack whose example data the clause of requirement 20.8 changes and deletes,
# and the directory of that data below it (business-pack.md section 3).
EXAMPLES_PACK: Final = "core"
EXAMPLES_DIR: Final = "seed/examples"

# --- the anchor dates (requirement 25.4) -------------------------------------------------

# The three dates requirement 25.4 names: the first day of the range requirement 21.12
# gives, the leap day of a century year that is a leap year, and the last day of the range.
FIRST_DAY: Final = date(2000, 1, 1)
LEAP_DAY: Final = date(2000, 2, 29)
LAST_DAY: Final = date(2099, 12, 31)
NAMED_ANCHORS: Final[tuple[date, ...]] = (FIRST_DAY, LEAP_DAY, LAST_DAY)

# The random anchor dates, their count the 20 requirement 25.4 asks for and their draw
# reproducible: one sequence of its own seed, so a failure is the same failure on the next
# run and the dates it names can be built again by hand. Property 31 and property 32 draw
# from the whole range with Hypothesis; what is drawn here goes through two processes, which
# is why the count is fixed.
RANDOM_ANCHORS: Final = 20
ANCHOR_SEED: Final = 20260305


def anchor_dates() -> tuple[date, ...]:
    """The anchor dates the two processes build at: the named ones and the drawn ones.

    In date order and without repetitions, so the cache entries of a process are one per
    date; the drawn dates are from the range of requirement 21.12 and come from
    :data:`ANCHOR_SEED` alone.
    """
    rng = Random(ANCHOR_SEED)
    first, last = FIRST_DAY.toordinal(), LAST_DAY.toordinal()
    drawn: set[date] = set()
    while len(drawn) < RANDOM_ANCHORS:
        drawn.add(date.fromordinal(rng.randint(first, last)))
    return tuple(sorted(drawn | set(NAMED_ANCHORS)))


# The dates of this run, worked out once: 23 of them, which is what makes the two processes
# the long-running part of the module (module docstring).
ANCHORS: Final[tuple[date, ...]] = anchor_dates()

# The anchor date the console script is spawned at, one of the three named ones, and the one
# the in-process clauses build at: a date in the middle of the range and none of the named
# ones, so nothing those clauses compare rests on a date the others already use.
INSTALLED_ANCHOR: Final = LEAP_DAY
IN_PROCESS_ANCHOR: Final = date(2050, 7, 15)

# --- the processes -----------------------------------------------------------------------

# The installed command, beside the interpreter that runs the tests, as the CLI integration
# tests spawn it (tests/integration/test_cli_serve.py).
COMMAND: Final = Path(sys.executable).parent / "erp-mock-mcp"

# The variables a subprocess is given beyond what this module sets: enough to run, and not
# one of the ``ERP_MOCK_*`` the environment of the test process may carry (req 23.9).
PASSED_THROUGH: Final = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")

# What the three processes differ in besides their data directory and their working
# directory: the hash seed of the interpreter and the time zone of the machine they believe
# they are on (requirements 20.13 and 24.9).
HASH_SEED_VAR: Final = "PYTHONHASHSEED"
TZ_VAR: Final = "TZ"

# The deadline of the three processes together. A whole validation of p2p-demo - the seven
# configuration steps, the build of purchase-basic and the checks over its Snapshot - is
# about a third of a second, so the 23 dates of a worker are some ten seconds; a run that
# reaches this deadline has stopped making progress rather than being slow.
PROCESS_TIMEOUT_S: Final = 180.0
POLL_SECONDS: Final = 0.1

# The worker of a build process (module docstring): ``erp-mock-mcp validate`` once per
# anchor date, in one process, with the exit code of each one written into a report. The
# arguments are the instance directory, the packs directory, the data directory, the report
# file and the anchor dates; the process ends with 0 only if every date validated.
WORKER_SOURCE: Final = '''"""Validate and build one dataset at every anchor date of argv."""

import json
import sys
from pathlib import Path

from erp_mock.cli.main import main


def validate(argv):
    """Run one command of the group and return the exit code it ended with."""
    try:
        main.main(args=argv, prog_name="erp-mock-mcp")
    except SystemExit as ended:
        code = ended.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


def run():
    instance_dir, packs_dir, data_dir, report = sys.argv[1:5]
    codes = {}
    for anchor in sys.argv[5:]:
        codes[anchor] = validate(
            [
                "validate",
                "--instance", instance_dir,
                "--packs-dir", packs_dir,
                "--data-dir", data_dir,
                "--anchor-date", anchor,
            ]
        )
    report_json = json.dumps({"codes": codes, "cwd": str(Path.cwd())}, ensure_ascii=False)
    Path(report).write_text(report_json, encoding="utf-8")
    return 0 if set(codes.values()) == {0} else 1


sys.exit(run())
'''


@dataclass(frozen=True, slots=True)
class Started:
    """One process of this module while it runs: what it is and where it writes."""

    name: str
    process: subprocess.Popen[bytes]
    data_dir: Path
    cwd: Path
    report: Path | None
    err: Path

    @property
    def stderr(self) -> str:
        """What the process has written to stderr: its log (design.md 7.3)."""
        return self.err.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Finished:
    """One process of this module after it ended, and what it left behind.

    ``codes`` is the exit code of each anchor date it validated, by ISO date, which is
    empty for the console script run - that one validated the single date it was given and
    its own ``code`` is that result.
    """

    name: str
    code: int
    data_dir: Path
    stderr: str
    codes: Mapping[str, int]

    def files(self, anchor: date) -> Mapping[str, bytes]:
        """The three files of the cache entry of ``anchor`` in this process' data directory."""
        return entry_files(self.data_dir, anchor, self.name)


@dataclass(frozen=True, slots=True)
class Processes:
    """The three processes of this module, all of them finished (module docstring)."""

    workers: tuple[Finished, Finished]
    installed: Finished

    @property
    def every(self) -> tuple[Finished, ...]:
        """The three of them, the console script run last."""
        return (*self.workers, self.installed)


def process_env(**extra: str) -> dict[str, str]:
    """The environment of one process: those of :data:`PASSED_THROUGH` and ``extra``."""
    inherited = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    return {**inherited, **extra}


def entry_dir(data_dir: Path, anchor: date) -> Path:
    """The cache entry of :data:`DATASET` at ``anchor`` below ``data_dir`` (req 20.11)."""
    return data_dir / SNAPSHOTS_DIR / DATASET_KEY / anchor.isoformat()


def entry_files(data_dir: Path, anchor: date, name: str = "") -> Mapping[str, bytes]:
    """The bytes of the three files of the cache entry of ``anchor``, by file name.

    Fails the test where the entry is not there or a file of it is empty: a process that
    validated that date built the Snapshot of it and left all three behind
    (requirements 20.10 and 20.11).
    """
    directory = entry_dir(data_dir, anchor)
    missing = [
        file
        for file in ENTRY_FILES
        if not (directory / file).is_file() or (directory / file).stat().st_size == 0
    ]
    assert not missing, f"{name} 在锚点日期 {anchor} 下的缓存目录 {directory} 缺少文件：{missing}"
    return {file: (directory / file).read_bytes() for file in ENTRY_FILES}


def alias_map(aliases: bytes) -> Mapping[str, str]:
    """The ``aliases`` of an alias map file: the internal id of each alias (req 20.10)."""
    payload = json.loads(aliases)["aliases"]
    return {str(alias): str(record_id) for alias, record_id in payload.items()}


# --- running the three processes ---------------------------------------------------------


def worker_args(worker: Path, data_dir: str, report: Path) -> list[str]:
    """The command line of one worker process: every anchor date of :data:`ANCHORS`."""
    return [
        sys.executable,
        str(worker),
        str(INSTANCE_DIR),
        str(PACKS_DIR),
        data_dir,
        str(report),
        *(anchor.isoformat() for anchor in ANCHORS),
    ]


def installed_args(data_dir: Path) -> list[str]:
    """The command line of the console script run: one validate at :data:`INSTALLED_ANCHOR`."""
    return [
        str(COMMAND),
        "validate",
        "--instance",
        str(INSTANCE_DIR),
        "--packs-dir",
        str(PACKS_DIR),
        "--data-dir",
        str(data_dir),
        "--anchor-date",
        INSTALLED_ANCHOR.isoformat(),
    ]


@contextmanager
def started(
    name: str,
    args: Sequence[str],
    *,
    root: Path,
    data_dir: Path,
    report: Path | None = None,
    env: Mapping[str, str],
) -> Iterator[Started]:
    """Start one process below ``root`` and yield it; it is gone after the block.

    ``root`` is both the working directory and where stderr is collected, so each process
    writes where the test can read it and pytest removes it. Whatever is still running when
    the block ends is killed, so no test leaves a process behind.
    """
    root.mkdir(parents=True, exist_ok=True)
    err = root / "stderr.txt"
    with err.open("wb") as err_file:
        process = subprocess.Popen(
            list(args),
            cwd=root,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err_file,
        )
    try:
        yield Started(
            name=name,
            process=process,
            data_dir=data_dir,
            cwd=root,
            report=report,
            err=err,
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=PROCESS_TIMEOUT_S)


def wait_for(running: Sequence[Started]) -> None:
    """Poll until every process of ``running`` has ended, or fail the test at the deadline.

    No fixed sleep: the processes run side by side and the poll is what the module waits
    on; a deadline that is reached names each process that is still going and what it has
    written so far (module docstring).
    """
    deadline = time.monotonic() + PROCESS_TIMEOUT_S
    while time.monotonic() < deadline:
        if all(started.process.poll() is not None for started in running):
            return
        time.sleep(POLL_SECONDS)
    stuck = [started for started in running if started.process.poll() is None]
    report = "；".join(f"{started.name} 的 stderr：{started.stderr}" for started in stuck)
    pytest.fail(
        f"等待构建进程结束超时（{PROCESS_TIMEOUT_S} 秒）："
        f"{[started.name for started in stuck]} 仍在运行；{report}"
    )


def finish(started: Started) -> Finished:
    """What the ended process ``started`` did: its exit code, its stderr and its report."""
    code = started.process.poll()
    assert code is not None, f"{started.name} 还没有结束"
    codes: Mapping[str, int] = {}
    if started.report is not None:
        assert started.report.is_file(), (
            f"{started.name} 没有写出报告文件 {started.report}；stderr：{started.stderr}"
        )
        payload = json.loads(started.report.read_text(encoding="utf-8"))
        codes = {str(anchor): int(value) for anchor, value in payload["codes"].items()}
        assert payload["cwd"] == str(started.cwd.resolve()), (
            f"{started.name} 的工作目录是 {payload['cwd']}，不是 {started.cwd}"
        )
    return Finished(
        name=started.name,
        code=code,
        data_dir=started.data_dir,
        stderr=started.stderr,
        codes=codes,
    )


@pytest.fixture(scope="module")
def processes(tmp_path_factory: pytest.TempPathFactory) -> Processes:
    """Build purchase-basic at every anchor date in three processes, run side by side.

    Two workers over :data:`ANCHORS`, each in a data directory, a working directory, a hash
    seed and a time zone of its own - the first one naming its data directory relative to
    its working directory, as requirement 22.1 resolves a relative path, and the second one
    absolutely - and the console script at :data:`INSTALLED_ANCHOR` in a third of each. Run
    once for the module: it is the part of this file that costs real time (module docstring).
    """
    assert COMMAND.is_file() and os.access(COMMAND, os.X_OK), f"没有找到安装好的命令：{COMMAND}"
    root = tmp_path_factory.mktemp("seed-build")
    worker = root / "worker.py"
    worker.write_text(WORKER_SOURCE, encoding="utf-8")

    first_root, second_root, third_root = root / "first", root / "second", root / "third"
    first_data, second_data = first_root / "var", second_root / "var"
    third_data = third_root / "var"
    with (
        started(
            "进程 1",
            # A relative --data-dir, resolved against the working directory of the process.
            worker_args(worker, "var", first_root / "report.json"),
            root=first_root,
            data_dir=first_data,
            report=first_root / "report.json",
            env=process_env(**{HASH_SEED_VAR: "0", TZ_VAR: "UTC"}),
        ) as first,
        started(
            "进程 2",
            worker_args(worker, str(second_data), second_root / "report.json"),
            root=second_root,
            data_dir=second_data,
            report=second_root / "report.json",
            env=process_env(**{HASH_SEED_VAR: "12345", TZ_VAR: "America/Los_Angeles"}),
        ) as second,
        started(
            "安装后的命令",
            installed_args(third_data),
            root=third_root,
            data_dir=third_data,
            env=process_env(**{HASH_SEED_VAR: "1", TZ_VAR: "Pacific/Auckland"}),
        ) as third,
    ):
        wait_for((first, second, third))
        return Processes(
            workers=(finish(first), finish(second)),
            installed=finish(third),
        )


# --- the three files of two processes (requirements 20.13 and 25.4) ----------------------


@pytest.mark.req("20.13", "25.4")
def test_two_processes_in_two_data_dirs_build_the_same_files_at_every_anchor_date(
    processes: Processes,
) -> None:
    first, second = processes.workers
    for finished in processes.every:
        assert finished.code == EXIT_OK, (
            f"{finished.name} 以 {finished.code} 退出；锚点日期 {list(ANCHORS)}；"
            f"stderr：{finished.stderr}"
        )

    # Each process built one Snapshot per anchor date and nothing else: the cache is keyed
    # by (dataset@version, anchor date) and the data directories started out empty
    # (requirement 20.11).
    wanted = [anchor.isoformat() for anchor in ANCHORS]
    for finished in processes.workers:
        entries = finished.data_dir / SNAPSHOTS_DIR / DATASET_KEY
        built = sorted(path.name for path in entries.iterdir() if path.is_dir())
        assert built == wanted, f"{finished.name} 构建出的锚点日期是 {built}，不是 {wanted}"

    # Two processes, two data directories, two working directories, two hash seeds and two
    # time zones: the same bytes in all three files, at every anchor date (requirement
    # 20.13). The dates are named in the message, drawn ones included (module docstring).
    for anchor in ANCHORS:
        mine, theirs = first.files(anchor), second.files(anchor)
        for file in ENTRY_FILES:
            assert mine[file] == theirs[file], (
                f"锚点日期 {anchor} 下两个进程的 {file} 不同："
                f"{difference(file, mine[file], theirs[file])}"
            )

    # The console script, in a third process and a third data directory, built the same
    # three files at the date it was given.
    installed, worker = processes.installed.files(INSTALLED_ANCHOR), first.files(INSTALLED_ANCHOR)
    for file in ENTRY_FILES:
        assert installed[file] == worker[file], (
            f"锚点日期 {INSTALLED_ANCHOR} 下安装后的命令写出的 {file} 与进程 1 的不同："
            f"{difference(file, worker[file], installed[file])}"
        )

    # What is being compared is one entry per date and not one file over and over: the
    # metadata of the three named dates differs, each carrying its own anchor date
    # (requirement 20.10).
    metas = {anchor: first.files(anchor)[META_FILENAME] for anchor in NAMED_ANCHORS}
    assert len(set(metas.values())) == len(NAMED_ANCHORS), f"这些日期的元数据相同：{list(metas)}"


# --- the validation at every one of those dates (requirement 21.12) ----------------------


@pytest.mark.req("21.12", "25.4")
def test_the_validate_command_passes_at_every_anchor_date(processes: Processes) -> None:
    expected = [anchor.isoformat() for anchor in ANCHORS]
    for finished in processes.workers:
        assert sorted(finished.codes) == sorted(expected), (
            f"{finished.name} 没有逐一校验这些锚点日期：{finished.codes}"
        )
        # p2p-demo passes the whole validation at every date of the range, the build of
        # purchase-basic, its invariants and the current user check included (req 21.12).
        failed = {anchor: code for anchor, code in finished.codes.items() if code != EXIT_OK}
        assert not failed, (
            f"{finished.name} 在这些锚点日期下 validate 没有通过：{failed}；"
            f"stderr：{finished.stderr}"
        )

    # The installed command itself, at the leap day of a century leap year (req 22.5).
    assert processes.installed.code == EXIT_OK, (
        f"锚点日期 {INSTALLED_ANCHOR} 下安装后的命令以 {processes.installed.code} 退出："
        f"{processes.installed.stderr}"
    )


# --- the alias map and the anchor date (requirement 20.14) -------------------------------


@pytest.mark.req("20.14")
def test_the_alias_map_is_the_same_file_at_every_anchor_date(processes: Processes) -> None:
    reference = processes.workers[0].files(ANCHORS[0])[ALIASES_FILENAME]

    # It is the alias map of this dataset and it has the aliases purchase-basic declares in
    # it (requirements 20.10 and 21.8): an empty file would be the same bytes at every
    # anchor date without that saying anything.
    aliases = alias_map(reference)
    assert json.loads(reference)["dataset"] == DATASET_KEY, f"别名映射写的不是 {DATASET_KEY}"
    assert len(aliases) > 10, f"别名映射只有 {len(aliases)} 个别名：{sorted(aliases)}"

    # An internal id comes from the creation order, which no anchor date decides, so the
    # file is the same bytes at every one of them and in both processes (requirement 20.14).
    for finished in processes.workers:
        for anchor in ANCHORS:
            theirs = finished.files(anchor)[ALIASES_FILENAME]
            assert theirs == reference, (
                f"{finished.name} 在锚点日期 {anchor} 下的别名映射与 {ANCHORS[0]} 的不同："
                f"{difference(ALIASES_FILENAME, reference, theirs)}"
            )


# --- the seed moves drawn values and nothing else (requirement 20.3) ---------------------

# The second ``random_seed`` of the clause of requirement 20.3, as the distance from the
# one the dataset declares, so it is never that seed again; any non-negative integer is a
# seed a dataset could declare.
SEED_OFFSET: Final = 7919


@pytest.mark.req("20.3")
def test_only_the_random_seed_moves_the_values_a_generator_draws() -> None:
    at = target(P2P_CASE)
    first = build_at(P2P_CASE, IN_PROCESS_ANCHOR)
    other = build_at(P2P_CASE, IN_PROCESS_ANCHOR, at.definition.random_seed + SEED_OFFSET)

    # Only ``random_seed`` differs: the alias map, every internal id and the field values
    # the dataset writes itself stay as they were (requirement 20.3).
    assert other.aliases == first.aliases, difference(
        ALIASES_FILENAME, first.aliases, other.aliases
    )
    assert other.ids == first.ids, f"内部 ID 变了：{first.ids} 与 {other.ids}"
    written, other_written = written_values(at, first), written_values(at, other)
    assert other_written == written, (
        f"写死的字段值变了：{[cell for cell in written if written[cell] != other_written.get(cell)]}"
    )

    # And the seed does move the drawn values: the two Snapshots are not the same file, so
    # what the clause above asserts is not simply two identical builds.
    assert other.db != first.db, "换了 random_seed 的构建与原来逐字节相同，生成值没有变"


# --- the example data takes no part in a build (requirement 20.8) ------------------------

# What the modified example data of the pack copy says: records written the way the example
# file writes them, with names and aliases of their own, so a build that read this file
# would create other records and hand out other internal ids than the dataset's own.
CHANGED_EXAMPLES: Final = """entities:
  supplier:changed_example:
    kind: supplier
    name: 改过的示例供货商
    short_name: 改过示例
  material:changed_example:
    kind: material
    name: 改过的示例物料
    material_kind: raw
    base_unit: PCS
"""
ADDED_EXAMPLES: Final = """entities:
  supplier:added_example:
    kind: supplier
    name: 新增的示例供货商
"""
ADDED_EXAMPLE_FILE: Final = "added-data.yaml"


def packs_with_changed_examples(root: Path) -> Path:
    """A copy of the business packs whose example data was rewritten and added to.

    The packs of the repository are copied rather than touched, and the copy is below
    ``tmp_path``; the example file that is rewritten has to be there to begin with, or the
    clause of requirement 20.8 would be about a file that no longer exists.
    """
    packs = _copied_packs(root)
    examples = packs / EXAMPLES_PACK / EXAMPLES_DIR
    files = sorted(path for path in examples.iterdir() if path.is_file())
    assert files, f"{EXAMPLES_PACK} 的示例数据目录 {examples} 是空的"
    files[0].write_text(CHANGED_EXAMPLES, encoding="utf-8")
    (examples / ADDED_EXAMPLE_FILE).write_text(ADDED_EXAMPLES, encoding="utf-8")
    return packs


def packs_without_examples(root: Path) -> Path:
    """A copy of the business packs with the example data of :data:`EXAMPLES_PACK` deleted."""
    packs = _copied_packs(root)
    examples = packs / EXAMPLES_PACK / EXAMPLES_DIR
    assert examples.is_dir(), f"{EXAMPLES_PACK} 没有示例数据目录 {examples}"
    shutil.rmtree(examples)
    return packs


def _copied_packs(root: Path) -> Path:
    """The business packs of the repository, copied below ``root``."""
    packs = root / "packs"
    shutil.copytree(PACKS_DIR, packs)
    return packs


@pytest.mark.req("20.8")
def test_changed_or_deleted_example_data_leaves_the_three_files_unchanged(
    tmp_path: Path,
) -> None:
    baseline = build_at(P2P_CASE, IN_PROCESS_ANCHOR)
    builds = {
        "改过示例数据": packs_with_changed_examples(tmp_path / "changed"),
        "删掉示例数据": packs_without_examples(tmp_path / "removed"),
    }

    # Only the Shared_Data, the generators and the dataset definition produce a Snapshot, so
    # the example data of a pack can be rewritten, added to or deleted and the three files
    # are the ones they were (requirement 20.8).
    for what, packs in builds.items():
        case = Case(dataset=DATASET, instance_dir=INSTANCE_DIR, packs_dir=packs)
        rebuilt = build_at(case, IN_PROCESS_ANCHOR)
        for file, mine, theirs in (
            (SNAPSHOT_FILENAME, baseline.db, rebuilt.db),
            (META_FILENAME, baseline.meta, rebuilt.meta),
            (ALIASES_FILENAME, baseline.aliases, rebuilt.aliases),
        ):
            assert theirs == mine, f"{what}后的 {file} 变了：{difference(file, mine, theirs)}"


# --- the company name blacklist (requirement 20.7) ---------------------------------------

# The one-pack instance of the blacklist clause: an entity with a string field other than
# ``name``, so that the blacklist is the only thing that discards a value drawn for it - a
# drawn ``name`` is also discarded for repeating a name of the same entity.
BL_DATASET: Final = "bl-basic"
BL_PACK: Final = "core"
BL_ENTITY: Final = "supplier"
BL_FIELD: Final = "note"
BL_SEED: Final = 20260305
BL_COUNT: Final = 8

# The values the generator of that field draws from: one that contains an entry of the
# shipped blacklist and two that do not. Both of the latter show up among the records, so
# the draws went on through the discarded ones rather than stopping at the first value.
BL_ENTRY: Final = "华为"
BLOCKED_VALUE: Final = f"{BL_ENTRY}演示机械有限公司"
KEPT_VALUES: Final[tuple[str, ...]] = ("演示甲机械有限公司", "演示乙机械有限公司")


def write_choice_instance(root: Path, values: Sequence[str]) -> Case:
    """An instance whose ``bulk`` records draw :data:`BL_FIELD` from ``values``.

    One pack with the two context entities an instance declares one of each of
    (requirement 1.14) and the entity the records are of, and one dataset of
    :data:`BL_COUNT` ``bulk`` records and nothing else, so the build draws
    :data:`BL_FIELD` and draws nothing before it.
    """
    instance_dir = write_instance(
        root / "instance",
        {
            "profiles/semantic.yaml": profile_doc(),
            f"{DATASETS_DIR}/{BL_DATASET}.yaml": dataset_doc(
                dataset=BL_DATASET,
                random_seed=str(BL_SEED),
                bulk=f"[{{ kind: {BL_ENTITY}, count: {BL_COUNT} }}]",
            ),
        },
        datasets=f"[{BL_DATASET}]",
        default_dataset=BL_DATASET,
    )
    drawn = json.dumps(list(values), ensure_ascii=False)
    packs_dir = root / "packs"
    write_pack(
        packs_dir,
        BL_PACK,
        files={
            **CONTEXT_FILES,
            f"entities/{BL_ENTITY}.yaml": entity_doc(
                entity=BL_ENTITY,
                fields=f"{{ name: {{ type: string }}, {BL_FIELD}: {{ type: string }} }}",
            ),
            "seed/generators.yaml": (
                f"generators: {{ {BL_ENTITY}.{BL_FIELD}: {{ gen: choice, values: {drawn} }} }}\n"
            ),
        },
    )
    return Case(dataset=BL_DATASET, instance_dir=instance_dir, packs_dir=packs_dir)


@dataclass(frozen=True, slots=True)
class DrawnField:
    """The one drawn field of the one ``bulk`` item of a blacklist instance.

    ``decl`` is the generator the build draws it with, which the test draws from itself to
    see what the same sequence gives without the blacklist, and ``column`` the column of
    the Snapshot that holds the value.
    """

    decl: GeneratorObject
    column: str


def drawn_field(case: Case) -> DrawnField:
    """The generator and the column of the one drawn field of ``case`` (req 20.4)."""
    field = target(case).plan.bulk[0].fields[0]
    assert isinstance(field.value, Drawn), f"{BL_FIELD} 不是生成值：{field.value}"
    return DrawnField(decl=field.value.decl, column=field.field.column)


def notes_of(built: Built, column: str) -> tuple[str, ...]:
    """The value of ``column`` of every record of :data:`BL_ENTITY`, in internal id order."""
    return tuple(str(row[column]) for row in built.rows[BL_ENTITY].values())


@pytest.mark.req("20.7")
def test_a_drawn_value_that_hits_the_blacklist_is_discarded(tmp_path: Path) -> None:
    assert BL_ENTRY in blacklist_hits(BLOCKED_VALUE), (
        f"{BLOCKED_VALUE} 不含框架黑名单中的词条，这个用例就不是在测丢弃"
    )
    case = write_choice_instance(tmp_path / "kept", (BLOCKED_VALUE, *KEPT_VALUES))
    field = drawn_field(case)

    built = build_at(case, IN_PROCESS_ANCHOR)
    notes = notes_of(built, field.column)

    # The blacklisted value is in none of the records: every one of them has one of the
    # other two values, and both of those show up (requirement 20.7).
    assert len(notes) == BL_COUNT, f"{BL_ENTITY} 的记录数是 {len(notes)}，不是 {BL_COUNT}"
    assert [value for value in notes if blacklist_hits(value)] == [], (
        f"快照里有命中黑名单的生成值：{notes}"
    )
    assert set(notes) == set(KEPT_VALUES), f"生成值不止没被丢弃的那些：{notes}"

    # The same sequence drawn without a blacklist, which the build drew from: the values it
    # wrote are that sequence with the blacklisted draws taken out, in the same order, so a
    # hit was discarded and the next value came from the same sequence (requirement 20.7).
    raw = ValueGenerators(BL_SEED, blacklist=())
    unfiltered = [
        str(raw.value(field.decl, entity=BL_ENTITY, field=BL_FIELD)) for _ in range(BL_COUNT)
    ]
    kept = tuple(value for value in unfiltered if not blacklist_hits(value))
    assert len(kept) < BL_COUNT, (
        f"随机种子 {BL_SEED} 的前 {BL_COUNT} 个取值都没命中黑名单，这个用例就不是在测丢弃：{unfiltered}"
    )
    assert notes[: len(kept)] == kept, f"丢弃后没有从同一序列继续取值：{notes} 与 {kept}"


@pytest.mark.req("20.7")
def test_a_field_whose_every_value_is_blacklisted_ends_the_build(tmp_path: Path) -> None:
    case = write_choice_instance(tmp_path / "exhausted", (BLOCKED_VALUE,))

    # Every value of the generator is blacklisted, so one field is discarded over and over
    # and the build stops at the discard limit (requirement 20.7).
    with pytest.raises(SeedError) as raised:
        build_at(case, IN_PROCESS_ANCHOR)

    reason = str(raised.value)
    assert f"{DATASETS_DIR}/{BL_DATASET}.yaml" in reason, f"错误没有指出数据集定义文件：{reason}"
    assert "bulk[0]" in reason, f"错误没有指出记录的配置路径：{reason}"
    assert f"{BL_ENTITY}.{BL_FIELD}" in reason, f"错误没有指出被丢弃的字段：{reason}"
    assert str(DISCARD_LIMIT) in reason and BL_ENTRY in reason, (
        f"错误没有说明连续丢弃次数与命中的词条：{reason}"
    )
