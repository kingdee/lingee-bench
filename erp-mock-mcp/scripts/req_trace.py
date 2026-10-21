"""Requirement tracing check, and the generator of tests/req_index.json (25.9, 25.10).

Run it from the repository root::

    uv run --locked python scripts/req_trace.py            # the tracing check
    uv run --locked python scripts/req_trace.py index      # rewrite tests/req_index.json
    uv run --locked python scripts/req_trace.py index --check   # only compare, write nothing

The check (the last step of ``scripts/check.py``) answers one question: does every
acceptance criterion of requirements 1 to 24 have at least one automated test
(requirement 25.9)? It needs two lists for that.

The criteria come from ``tests/req_index.json``, a file of this repository. The spec lives
outside the repository, in ``.kiro/specs/erp-mock-mcp-m0/requirements.md``, and a fresh
checkout may not have it (requirement 25.10), so the criteria are not parsed out of it at
check time: the ``index`` subcommand parses it and writes the index, and the index is what
the check reads. Where the spec *is* there, the check compares the two and fails on a
stale index, so the committed file cannot drift from the requirements it was generated
from; ``index --check`` is that comparison on its own, and tests/static/test_req_trace.py
runs it as part of the suite.

The tests come from the suite itself: one ``pytest --collect-only`` with
``--export-req-markers`` (tests/conftest.py), which writes the ``@pytest.mark.req(...)``
criteria of every collected test. Collection alone runs no test, so this step costs a few
seconds whatever the suite does; ``--markers`` takes an export that was already made
instead of collecting again.

What makes the check fail, each reported criterion by criterion:

- a criterion of requirements 1 to 24 that no collected test is marked with. The message
  lists every one of them with its number and the first characters of its text, so the
  report says which requirements are untested rather than how many;
- a criterion a test is marked with that the index does not have, e.g. ``3.10`` where
  requirement 3 has nine items. The collection check of tests/helpers/req_marker.py
  catches a malformed marker and the range of the requirement number, but it cannot know
  how many items a requirement has, so that is caught here;
- a stale ``tests/req_index.json``, where the spec is available (above);
- a collection that failed, which is reported with the output of the pytest run.

Requirement 25 is deliberately outside the traced range, as requirement 25.9 writes it:
its own criteria are the checks of ``scripts/check.py`` - the coverage gate (25.1), the
steps of the local command (25.7, 25.8), this check (25.9) and a run of the whole command
in a fresh checkout (25.10) - which no single test can stand in for.

This script writes its own lines to stderr, as ``scripts/check.py`` does, and leaves stdout
empty. Exit code 0 means the check passed (or the index was written), 1 that it did not,
and the reason is on stderr.
"""

import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# The spec this repository implements, and where its requirements document is: beside the
# repository, in the .kiro directory of the workspace. A fresh checkout of the repository
# alone does not have it (requirement 25.10), which is what the index file is for.
SPEC: Final = "erp-mock-mcp-m0"
SOURCE: Final = REPO_ROOT.parent / ".kiro" / "specs" / SPEC / "requirements.md"

# The generated list of criteria, committed to the repository.
INDEX: Final = REPO_ROOT / "tests" / "req_index.json"

# The requirements the check traces, both ends included (requirement 25.9).
FIRST_TRACED: Final = 1
LAST_TRACED: Final = 24

# How many code points of a criterion's text the index keeps, as the summary a report
# shows beside the number; the sha256 of the source is what tells the two apart exactly.
SUMMARY_LIMIT: Final = 80
ELLIPSIS: Final = "…"

# How the sections of requirements.md are recognised. A requirement is a level 3 heading
# with its number and title, its criteria are the ordered list below the level 4 heading
# of :data:`CRITERIA_HEADING`, and any further heading ends that list.
_REQUIREMENT: Final = re.compile(r"### Requirement ([1-9][0-9]*): (.+)")
_ITEM: Final = re.compile(r"([1-9][0-9]*)\. (.+)")
CRITERIA_HEADING: Final = "#### Acceptance Criteria"

# How a criterion is identified, in the index file and in a ``req`` marker: two positive
# integers without leading zeros (tests/helpers/req_marker.py writes the same pattern).
_ID: Final = re.compile(r"([1-9][0-9]*)\.([1-9][0-9]*)")

# The keys of tests/req_index.json (:func:`index_document`).
SPEC_KEY: Final = "spec"
SOURCE_KEY: Final = "source"
SHA256_KEY: Final = "source_sha256"
GENERATED_BY_KEY: Final = "generated_by"
REQUIREMENTS_KEY: Final = "requirements"
NUMBER_KEY: Final = "requirement"
TITLE_KEY: Final = "title"
CRITERIA_KEY: Final = "criteria"
ID_KEY: Final = "id"
SUMMARY_KEY: Final = "summary"

# The keys of the marker export (tests.helpers.req_marker.marker_export).
COLLECTED_KEY: Final = "collected"
TESTS_KEY: Final = "tests"
NODEID_KEY: Final = "nodeid"
EXPORT_CRITERIA_KEY: Final = "criteria"

# How this file is regenerated and how the suite is collected, as the messages quote them.
INDEX_COMMAND: Final = "uv run --locked python scripts/req_trace.py index"
EXPORT_OPTION: Final = "--export-req-markers"

# How many lines of a stale-index report are shown before it is cut off.
DIFF_LIMIT: Final = 40

# How long one collection of the suite may take. Collecting the whole suite is seconds;
# this deadline is reached only by a run that stopped making progress.
COLLECT_TIMEOUT_S: Final = 900.0


def _say(message: str) -> None:
    """Write one line of this script's own output to stderr."""
    sys.stderr.write(f"==> {message}\n")
    sys.stderr.flush()


class TraceFailed(Exception):
    """What made a run fail; :func:`main` reports it and exits with 1."""


# --- the criteria of the spec ------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class Criterion:
    """One acceptance criterion, as the index lists it.

    ``requirement`` and ``item`` are its two numbers and ``id`` is how a marker writes
    them (``"23.7"``); ``title`` is the title of the requirement it belongs to and
    ``summary`` the first characters of its text (:data:`SUMMARY_LIMIT`). Criteria sort by
    requirement and then by item, so ``9.10`` comes after ``9.9``.
    """

    requirement: int
    item: int
    title: str
    summary: str

    @property
    def id(self) -> str:
        """``"<requirement>.<item>"``, which is what ``@pytest.mark.req`` names."""
        return f"{self.requirement}.{self.item}"

    @property
    def line(self) -> str:
        """The one line a report shows this criterion on."""
        return f"{self.id:<7} 需求 {self.requirement}（{self.title}）：{self.summary}"


@dataclass(frozen=True, slots=True)
class Requirement:
    """One requirement of the document: its number, its title and its criteria texts."""

    number: int
    title: str
    items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Index:
    """The criteria of one requirements document, in document order.

    ``source`` is the path the document was read from, as the index records it, and
    ``source_sha256`` the hash of its bytes, which is what tells an index of one revision
    of the document from an index of another.
    """

    spec: str
    source: str
    source_sha256: str
    criteria: tuple[Criterion, ...]

    def traced(self) -> tuple[Criterion, ...]:
        """The criteria the check traces: those of requirements 1 to 24 (req 25.9)."""
        return tuple(
            criterion
            for criterion in self.criteria
            if FIRST_TRACED <= criterion.requirement <= LAST_TRACED
        )

    @property
    def ids(self) -> frozenset[str]:
        """The identifiers of every criterion, traced or not."""
        return frozenset(criterion.id for criterion in self.criteria)


def summarize(text: str) -> str:
    """The summary the index keeps of a criterion's text.

    Whitespace is collapsed to single spaces and the result is cut to
    :data:`SUMMARY_LIMIT` code points, with an ellipsis where anything was cut, so every
    summary is one line of a report whatever the document does with line breaks.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= SUMMARY_LIMIT:
        return collapsed
    return collapsed[:SUMMARY_LIMIT] + ELLIPSIS


def parse_requirements(text: str, label: str = "requirements.md") -> tuple[Requirement, ...]:
    """Parse ``text`` into one :class:`Requirement` per requirement, in document order.

    ``label`` is how a message names the document. The structure the parse insists on, so
    that a renumbered or reformatted document is reported rather than silently indexed as
    fewer criteria: requirement numbers are 1, 2, 3, ... without a gap; every requirement
    has an "Acceptance Criteria" heading with at least one item below it; and the items of
    a requirement are numbered 1, 2, 3, ... as well.

    Raises TraceFailed naming the line where the structure breaks.
    """
    lines = text.splitlines()
    found: list[Requirement] = []
    number: int | None = None
    title = ""
    items: list[str] = []
    in_criteria = False

    def close(at: int) -> None:
        if number is None:
            return
        if not items:
            raise TraceFailed(f"{label} 第 {at} 行：需求 {number} 没有验收标准条目")
        found.append(Requirement(number=number, title=title, items=tuple(items)))

    for lineno, line in enumerate(lines, start=1):
        heading = _REQUIREMENT.fullmatch(line)
        if heading is not None:
            close(lineno)
            number, title, items, in_criteria = int(heading.group(1)), heading.group(2), [], False
            if number != len(found) + 1:
                raise TraceFailed(
                    f"{label} 第 {lineno} 行：需求编号为 {number}，"
                    f"期望 {len(found) + 1}（需求编号必须从 1 起连续）"
                )
            continue
        if line.startswith("#"):
            in_criteria = line == CRITERIA_HEADING
            continue
        if not in_criteria:
            continue
        item = _ITEM.fullmatch(line)
        if item is None:
            continue
        if int(item.group(1)) != len(items) + 1:
            raise TraceFailed(
                f"{label} 第 {lineno} 行：需求 {number} 的条目编号为 {item.group(1)}，"
                f"期望 {len(items) + 1}（条目编号必须从 1 起连续）"
            )
        items.append(item.group(2))
    close(len(lines) + 1)
    if not found:
        raise TraceFailed(f"{label} 中没有解析到任何需求")
    return tuple(found)


def read_source(path: Path = SOURCE) -> Index:
    """Parse the requirements document ``path`` into an :class:`Index`.

    Raises TraceFailed where the document is not there - which is a fresh checkout without
    the .kiro directory, where only the committed index can be used (requirement 25.10) -
    or where its structure is not the one :func:`parse_requirements` insists on.
    """
    if not path.is_file():
        raise TraceFailed(f"没有找到需求文档 {path}")
    content = path.read_bytes()
    criteria = tuple(
        Criterion(
            requirement=requirement.number,
            item=item,
            title=requirement.title,
            summary=summarize(body),
        )
        for requirement in parse_requirements(content.decode("utf-8"), path.name)
        for item, body in enumerate(requirement.items, start=1)
    )
    return Index(
        spec=SPEC,
        source=source_label(path),
        source_sha256=hashlib.sha256(content).hexdigest(),
        criteria=criteria,
    )


def source_label(path: Path) -> str:
    """How the index records where it was generated from: a path relative to the repository.

    ``../.kiro/specs/erp-mock-mcp-m0/requirements.md`` for the spec of this repository,
    which is beside it; a path that is nowhere below the parent of the repository is
    recorded as it is.
    """
    base = REPO_ROOT.parent
    resolved = path.resolve()
    if resolved.is_relative_to(base):
        return "/".join(("..", *resolved.relative_to(base).parts))
    return resolved.as_posix()


# --- tests/req_index.json ----------------------------------------------------------------


def index_document(index: Index) -> dict[str, Any]:
    """``tests/req_index.json`` as a JSON object, grouped by requirement.

    The provenance comes first - the spec, where its requirements document is, the sha256
    of that document's bytes and the command that writes this file - and then one entry
    per requirement with its title and its criteria::

        {"spec": "erp-mock-mcp-m0",
         "source": "../.kiro/specs/erp-mock-mcp-m0/requirements.md",
         "source_sha256": "...",
         "generated_by": "uv run --locked python scripts/req_trace.py index",
         "requirements": [{"requirement": 1, "title": "元模型 v0",
                           "criteria": [{"id": "1.1", "summary": "THE Meta_Model SHALL ..."}]}]}

    Everything is in document order, so two runs over the same document produce the same
    bytes (:func:`index_text`).
    """
    by_requirement: dict[int, list[Criterion]] = {}
    for criterion in index.criteria:
        by_requirement.setdefault(criterion.requirement, []).append(criterion)
    return {
        SPEC_KEY: index.spec,
        SOURCE_KEY: index.source,
        SHA256_KEY: index.source_sha256,
        GENERATED_BY_KEY: INDEX_COMMAND,
        REQUIREMENTS_KEY: [
            {
                NUMBER_KEY: number,
                TITLE_KEY: criteria[0].title,
                CRITERIA_KEY: [
                    {ID_KEY: criterion.id, SUMMARY_KEY: criterion.summary} for criterion in criteria
                ],
            }
            for number, criteria in sorted(by_requirement.items())
        ],
    }


def index_text(index: Index) -> str:
    """The bytes of ``tests/req_index.json`` as text: UTF-8, two spaces, final newline.

    The same formatting every other JSON file of this repository is written with
    (:func:`~erp_mock.common.jsonio.dumps_pretty`), spelled out here so that the generator
    needs nothing but the standard library and keeps working in a checkout where the
    package is not installed.
    """
    return (
        json.dumps(index_document(index), ensure_ascii=False, indent=2, separators=(",", ": "))
        + "\n"
    )


def write_index(source: Path = SOURCE, path: Path = INDEX) -> Index:
    """Generate ``path`` from ``source`` and report what was written on stderr."""
    index = read_source(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(index_text(index), encoding="utf-8")
    requirements = len({criterion.requirement for criterion in index.criteria})
    _say(
        f"已写入 {_where(path)}：{requirements} 个需求，"
        f"{len(index.criteria)} 条验收标准，来源 {index.source}"
    )
    return index


def _where(path: Path) -> str:
    """How a message names ``path``: relative to the repository root where it is below it."""
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def check_index(source: Path = SOURCE, path: Path = INDEX) -> None:
    """Fail where ``path`` is not what ``source`` generates right now.

    Raises TraceFailed with the first lines that differ and the command that rewrites the
    file. Used by the ``index --check`` subcommand, by the tracing check where the source
    is available, and by tests/static/test_req_trace.py.
    """
    expected = index_text(read_source(source))
    found = path.read_text(encoding="utf-8") if path.is_file() else None
    if found == expected:
        return
    where = _where(path)
    head = f"{where} 不存在" if found is None else f"{where} 与 {source_label(source)} 不一致"
    lines = [f"{head}，请运行 `{INDEX_COMMAND}` 重新生成。"]
    if found is not None:
        lines += ["", *_differing_lines(found, expected)]
    raise TraceFailed("\n".join(lines))


def _differing_lines(found: str, expected: str) -> list[str]:
    """The lines where the two texts differ, at most :data:`DIFF_LIMIT` of them."""
    diff = [
        line.rstrip("\n")
        for line in difflib.unified_diff(
            found.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile="仓库中的 req_index.json",
            tofile="requirements.md 当前内容生成的结果",
        )
    ]
    if len(diff) > DIFF_LIMIT:
        return [*diff[:DIFF_LIMIT], f"... diff 还有 {len(diff) - DIFF_LIMIT} 行，未全部列出"]
    return diff


def read_index(path: Path = INDEX) -> Index:
    """Read ``tests/req_index.json`` back into an :class:`Index`.

    Raises TraceFailed where the file is not there or is not the document
    :func:`index_document` writes, which is what a hand-edited or truncated index looks
    like; the message names the command that regenerates it.
    """
    if not path.is_file():
        raise TraceFailed(f"没有找到需求条目清单 {path}，请运行 `{INDEX_COMMAND}` 生成")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        criteria = tuple(
            _criterion_of(group, entry)
            for group in document[REQUIREMENTS_KEY]
            for entry in group[CRITERIA_KEY]
        )
        index = Index(
            spec=str(document[SPEC_KEY]),
            source=str(document[SOURCE_KEY]),
            source_sha256=str(document[SHA256_KEY]),
            criteria=criteria,
        )
    except (ValueError, LookupError, TypeError) as error:
        raise TraceFailed(
            f"{path} 不是需求条目清单（{type(error).__name__}: {error}），"
            f"请运行 `{INDEX_COMMAND}` 重新生成"
        ) from None
    if not index.criteria:
        raise TraceFailed(f"{path} 中没有任何验收标准条目，请运行 `{INDEX_COMMAND}` 重新生成")
    return index


def _criterion_of(group: Mapping[str, Any], entry: Mapping[str, Any]) -> Criterion:
    """One criterion of the index file, read back from its requirement entry.

    Raises ValueError where the ``id`` is not ``"<requirement>.<item>"`` with the number of
    the requirement it is listed under, which :func:`read_index` reports as a file that is
    no index.
    """
    number = int(group[NUMBER_KEY])
    found = _ID.fullmatch(str(entry[ID_KEY]))
    if found is None or int(found.group(1)) != number:
        raise ValueError(f"需求 {number} 的条目编号 {entry[ID_KEY]!r} 不合法")
    return Criterion(
        requirement=number,
        item=int(found.group(2)),
        title=str(group[TITLE_KEY]),
        summary=str(entry[SUMMARY_KEY]),
    )


# --- the markers of the collected tests --------------------------------------------------


def read_markers(path: Path) -> tuple[int, dict[str, list[str]]]:
    """Read a marker export: how many tests were collected, and who covers each criterion.

    The mapping is from criterion to the nodeids of the tests marked with it, in collection
    order (tests.helpers.req_marker.marker_export writes the file).

    Raises TraceFailed where the file is not the document that option writes.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        collected = int(document[COLLECTED_KEY])
        covered: dict[str, list[str]] = {}
        for entry in document[TESTS_KEY]:
            for criterion in entry[EXPORT_CRITERIA_KEY]:
                covered.setdefault(str(criterion), []).append(str(entry[NODEID_KEY]))
    except (OSError, ValueError, LookupError, TypeError) as error:
        raise TraceFailed(
            f"{path} 不是 {EXPORT_OPTION} 导出的标记清单（{type(error).__name__}: {error}）"
        ) from None
    return collected, covered


def collect_markers(directory: Path) -> Path:
    """Collect the suite into a marker export below ``directory`` and return its path.

    One ``pytest --collect-only`` over ``tests/`` with the export option of
    tests/conftest.py. The output of that run is kept and only reported when it fails -
    which is a collection error, e.g. a test module that cannot be imported or a malformed
    ``req`` marker (tests/helpers/req_marker.py) - because a successful collection prints
    one line per collected test and says nothing this check needs.

    Raises TraceFailed where the run failed, timed out or wrote no export.
    """
    export = directory / "req-markers.json"
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "--collect-only",
        "-q",
        f"{EXPORT_OPTION}={export}",
    ]
    _say("收集全部用例的 req 标记：pytest tests/ --collect-only")
    try:
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=COLLECT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TraceFailed(f"收集用例超时（{COLLECT_TIMEOUT_S} 秒）") from None
    if completed.returncode != 0:
        raise TraceFailed(
            f"收集用例失败（退出码 {completed.returncode}）：\n"
            f"{completed.stdout.strip()}\n{completed.stderr.strip()}"
        )
    if not export.is_file():
        raise TraceFailed(f"收集用例没有写出标记清单 {export}")
    return export


# --- the check ---------------------------------------------------------------------------


def missing_criteria(index: Index, covered: Mapping[str, Sequence[str]]) -> tuple[Criterion, ...]:
    """The traced criteria no collected test is marked with, in document order."""
    return tuple(criterion for criterion in index.traced() if not covered.get(criterion.id))


def unknown_criteria(index: Index, covered: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """The criteria a marker names that the index does not have, sorted by number.

    A marker such as ``req("3.10")`` where requirement 3 has nine items: the number is
    well formed and inside the range of requirement numbers, so the collection check
    accepts it, and nothing would ever cover the criterion it was meant for.
    """
    known = index.ids
    return tuple(
        sorted((criterion for criterion in covered if criterion not in known), key=_id_order)
    )


def _id_order(value: str) -> tuple[int, int, str]:
    """Sort key of a criterion identifier: by requirement, then by item, both as numbers."""
    found = _ID.fullmatch(value)
    if found is None:
        return (sys.maxsize, 0, value)
    return (int(found.group(1)), int(found.group(2)), "")


def trace(markers: Path | None = None, source: Path = SOURCE, path: Path = INDEX) -> None:
    """Run the tracing check (module docstring).

    ``markers`` is an export made earlier; None collects the suite into a temporary one.
    Raises TraceFailed with the full report where the check does not pass.
    """
    index = read_index(path)
    if source.is_file():
        check_index(source, path)
        _say(f"{_where(path)} 与 {index.source} 一致")
    else:
        _say(f"没有 {index.source}，按仓库中的 {_where(path)} 检查")

    if markers is not None:
        collected, covered = read_markers(markers)
    else:
        with tempfile.TemporaryDirectory(prefix="erp-mock-req-trace-") as directory:
            collected, covered = read_markers(collect_markers(Path(directory)))

    problems: list[str] = []
    missing = missing_criteria(index, covered)
    if missing:
        problems.append(
            f"需求 {FIRST_TRACED} 到 {LAST_TRACED} 中有 {len(missing)} 条验收标准没有对应测试："
        )
        problems += [f"  {criterion.line}" for criterion in missing]
    unknown = unknown_criteria(index, covered)
    if unknown:
        problems.append(f"有 {len(unknown)} 个 req 标记指向清单中不存在的条目：")
        problems += [f"  {criterion}：{', '.join(covered[criterion][:3])}" for criterion in unknown]
    if problems:
        problems.append(
            '为每条没有对应测试的条目补写测试并标记 @pytest.mark.req("<需求号>.<条目号>")；'
            "条目号写错时改正标记。"
        )
        raise TraceFailed("\n".join(problems))

    traced = index.traced()
    _say(
        f"需求追踪检查通过：需求 {FIRST_TRACED} 到 {LAST_TRACED} 的 {len(traced)} 条验收标准"
        f"各有至少一个测试（共收集 {collected} 个用例，{len(covered)} 个条目被标记）"
    )


# --- the command line --------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The options of one run; a usage error ends the process with exit code 2."""
    parser = argparse.ArgumentParser(
        prog="scripts/req_trace.py",
        description=(
            "需求追踪检查：需求 1 到 24 的每条验收标准都要有至少一个带 req 标记的测试"
            "（需求 25.9）。不带子命令时执行该检查。"
        ),
    )
    subcommands = parser.add_subparsers(dest="command")
    tracing = subcommands.add_parser("trace", help="执行需求追踪检查（缺省子命令）。")
    tracing.add_argument(
        "--markers",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            f"使用已有的标记清单（pytest {EXPORT_OPTION}=PATH 的输出），"
            "不再收集一次用例；缺省自行收集。"
        ),
    )
    generating = subcommands.add_parser(
        "index", help="由 requirements.md 生成 tests/req_index.json。"
    )
    generating.add_argument(
        "--check",
        action="store_true",
        help="只校验仓库中的 tests/req_index.json 与 requirements.md 一致，不写入文件。",
    )
    args = parser.parse_args(argv)
    if args.command is None:
        args.command, args.markers = "trace", None
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run what the command line asked for and return the exit code of the process."""
    args = parse_args(argv)
    generating = args.command == "index"
    try:
        if generating and args.check:
            check_index()
            _say(f"{_where(INDEX)} 与 {source_label(SOURCE)} 一致")
        elif generating:
            write_index()
        else:
            trace(markers=args.markers)
    except TraceFailed as failure:
        _say(f"{'生成需求条目清单失败' if generating else '需求追踪检查失败'}：{failure}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
