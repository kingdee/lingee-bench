"""The requirement tracing check: tests/req_index.json, the marker export and the check.

Three things are checked here (requirements 25.9 and 25.10):

- ``tests/req_index.json`` is the list of criteria of the spec. Where the spec is there -
  a working copy of the workspace - it has to be exactly what the generator writes right
  now, so the committed file cannot drift from requirements.md; where it is not - a fresh
  checkout of the repository alone - the committed file has to be enough on its own, which
  is what the check runs on then;
- ``--export-req-markers`` (tests/conftest.py) writes the criteria of every collected test,
  markers of the module, of the class and of one ``pytest.param`` included;
- the check itself passes only when every criterion of requirements 1 to 24 has a test, and
  reports what is missing criterion by criterion.

The check is run here on synthetic marker exports rather than on a collection of this
suite: the real collection is what the last step of ``scripts/check.py`` does, and running
it from inside the suite would collect the suite a second time for every case.
"""

import json
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.jsonio import dumps_pretty
from scripts.req_trace import (
    FIRST_TRACED,
    INDEX,
    INDEX_COMMAND,
    LAST_TRACED,
    SOURCE,
    SUMMARY_LIMIT,
    Index,
    TraceFailed,
    check_index,
    parse_requirements,
    read_index,
    read_source,
    summarize,
    trace,
    write_index,
)
from tests.helpers.req_marker import LAST_REQUIREMENT

# A requirements document of the shape the generator parses, with everything it insists on:
# level 3 headings with the requirement number and title, and an ordered list below the
# "Acceptance Criteria" heading. The user story paragraph between the two is skipped, and so
# is a list that is not below that heading (the one under Introduction).
SAMPLE: Final = """# Requirements Document

## Introduction

1. 不是验收标准，不在 Acceptance Criteria 之下。

## Requirements

### Requirement 1: 示例需求甲

**User Story:** 作为示例，我希望有一条标准。

#### Acceptance Criteria

1. THE Sample SHALL 做第一件事。
2. THE Sample SHALL 做第二件事，这条很长，长到需要被截断：\
0123456789012345678901234567890123456789012345678901234567890123456789。

### Requirement 2: 示例需求乙

#### Acceptance Criteria

1. THE Sample SHALL 做另一件事。
"""

# The option tests/conftest.py registers, as a pytester sample run passes it.
EXPORT_OPTION: Final = "--export-req-markers"

# A conftest.py for a pytester project that registers that option: the two hooks of
# tests/conftest.py, imported from it, so the sample run writes an export the same way a
# real run does. Taking the hooks rather than copying them is what makes this a test of the
# option and not of a second implementation of it.
SAMPLE_CONFTEST: Final = "from tests.conftest import pytest_addoption, pytest_collection_finish"

# A sample test module whose markers sit everywhere a marker can sit.
SAMPLE_TESTS: Final = """
import pytest

pytestmark = pytest.mark.req("5.1")


@pytest.mark.req("5.2", "5.1")
def test_marked():
    pass


def test_inherits_only_the_module_marker():
    pass


@pytest.mark.req("9.9")
class TestClass:
    @pytest.mark.parametrize("x", [1, pytest.param(2, marks=pytest.mark.req("9.10"))])
    def test_method(self, x):
        pass
"""


def write_source(tmp_path: Path, text: str = SAMPLE) -> Path:
    """Write ``text`` as a requirements document below ``tmp_path`` and return its path."""
    path = tmp_path / "requirements.md"
    path.write_text(text, encoding="utf-8")
    return path


def write_markers(tmp_path: Path, covered: dict[str, list[str]], collected: int = 0) -> Path:
    """Write a marker export that covers ``covered``: one test per criterion it names."""
    tests = [
        {"nodeid": nodeid, "criteria": [criterion]}
        for criterion, nodeids in covered.items()
        for nodeid in nodeids
    ]
    path = tmp_path / "req-markers.json"
    path.write_text(
        dumps_pretty({"collected": max(collected, len(tests)), "tests": tests}), encoding="utf-8"
    )
    return path


def covering(index: Index, without: str | None = None) -> dict[str, list[str]]:
    """One made-up test per traced criterion of ``index``, except ``without``."""
    return {
        criterion.id: [f"tests/unit/test_sample.py::test_{criterion.id.replace('.', '_')}"]
        for criterion in index.traced()
        if criterion.id != without
    }


# --- tests/req_index.json ----------------------------------------------------------------


@pytest.mark.req("25.9")
def test_the_committed_index_is_what_the_spec_generates_right_now() -> None:
    # The consistency check of the generated file, which needs the spec: in a working copy
    # of the workspace it keeps tests/req_index.json from drifting from requirements.md,
    # and in a checkout of the repository alone there is nothing to compare with.
    if not SOURCE.is_file():
        pytest.skip(f"没有 {SOURCE}，跳过与需求文档的一致性校验")
    check_index(SOURCE, INDEX)


@pytest.mark.req("25.9", "25.10")
def test_the_committed_index_lists_every_requirement_without_the_spec() -> None:
    # What the check works off in a fresh checkout: the committed file alone, read without
    # looking at the spec at all (requirement 25.10).
    index = read_index(INDEX)
    numbers = sorted({criterion.requirement for criterion in index.criteria})

    # Requirement numbers run from 1 to the last requirement the marker check accepts, so
    # the two cannot disagree about how many requirements there are.
    assert numbers == list(range(1, LAST_REQUIREMENT + 1))
    assert index.spec == "erp-mock-mcp-m0"
    assert len(index.source_sha256) == 64

    # The traced range is requirements 1 to 24, and every one of them has criteria.
    traced = index.traced()
    assert {criterion.requirement for criterion in traced} == set(
        range(FIRST_TRACED, LAST_TRACED + 1)
    )
    assert len(traced) < len(index.criteria), "需求 25 的条目不在追踪范围内"

    # Every criterion carries the title of its requirement and a summary of its text, which
    # is what a report of a missing criterion shows beside the number.
    first = traced[0]
    assert (first.id, first.requirement, first.item) == ("1.1", 1, 1)
    assert first.title and first.summary
    assert all(len(criterion.summary) <= SUMMARY_LIMIT + 1 for criterion in index.criteria)


@pytest.mark.req("25.9")
def test_the_generator_parses_requirements_and_criteria_in_document_order(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    index = read_source(source)

    assert [(criterion.id, criterion.title) for criterion in index.criteria] == [
        ("1.1", "示例需求甲"),
        ("1.2", "示例需求甲"),
        ("2.1", "示例需求乙"),
    ]
    assert index.criteria[0].summary == "THE Sample SHALL 做第一件事。"
    # A long criterion is cut to SUMMARY_LIMIT code points plus the ellipsis.
    assert len(index.criteria[1].summary) == SUMMARY_LIMIT + 1
    assert index.criteria[1].summary.startswith("THE Sample SHALL 做第二件事")
    # The list under Introduction is not below an "Acceptance Criteria" heading.
    assert len(index.criteria) == 3


@pytest.mark.req("25.9")
def test_the_generated_file_is_deterministic_and_keeps_the_text_as_it_is(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    path = tmp_path / "req_index.json"

    write_index(source, path)
    first = path.read_bytes()
    write_index(source, path)
    assert path.read_bytes() == first

    text = first.decode("utf-8")
    assert text.endswith("}\n")
    # Non-ASCII characters are written as they are, not as \\uXXXX escapes, and the sha256
    # of the source bytes is what tells one revision of the document from another.
    assert "示例需求甲" in text
    document = json.loads(text)
    assert document["source_sha256"] == read_source(source).source_sha256
    assert document["generated_by"] == INDEX_COMMAND
    assert [group["requirement"] for group in document["requirements"]] == [1, 2]


@pytest.mark.req("25.9")
def test_a_stale_index_is_reported_with_the_command_that_rewrites_it(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    path = tmp_path / "req_index.json"
    write_index(source, path)

    # What an edited requirements.md leaves behind: an index of the previous revision.
    path.write_text(path.read_text(encoding="utf-8").replace("2.1", "2.2"), encoding="utf-8")
    with pytest.raises(TraceFailed) as failure:
        check_index(source, path)
    assert INDEX_COMMAND in str(failure.value)
    assert "2.2" in str(failure.value)


@pytest.mark.req("25.9")
@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        ("### Requirement 2: 示例需求乙", "### Requirement 3: 示例需求乙"),  # a gap
        ("1. THE Sample SHALL 做另一件事。", "2. THE Sample SHALL 做另一件事。"),  # item 1 missing
        ("1. THE Sample SHALL 做另一件事。", "没有条目的需求。"),
    ],
)
def test_a_document_that_is_not_numbered_from_one_is_rejected(
    tmp_path: Path, broken: str, reason: str
) -> None:
    # A renumbered or reformatted document is reported rather than silently indexed as
    # fewer criteria, which would make the tracing check pass for a criterion that is gone.
    source = write_source(tmp_path, SAMPLE.replace(broken, reason))
    with pytest.raises(TraceFailed):
        read_source(source)


@pytest.mark.req("25.9")
def test_a_missing_or_malformed_index_is_reported(tmp_path: Path) -> None:
    with pytest.raises(TraceFailed, match=INDEX_COMMAND):
        read_index(tmp_path / "nothing.json")
    (tmp_path / "req_index.json").write_text('{"spec": "x"}', encoding="utf-8")
    with pytest.raises(TraceFailed, match=INDEX_COMMAND):
        read_index(tmp_path / "req_index.json")


@pytest.mark.req("25.9")
def test_summarize_collapses_whitespace() -> None:
    assert summarize("  THE  Sample\tSHALL\n做事。 ") == "THE Sample SHALL 做事。"


@pytest.mark.req("25.9")
def test_the_parser_reports_a_document_with_no_requirement(tmp_path: Path) -> None:
    with pytest.raises(TraceFailed):
        parse_requirements("# 没有需求的文档\n", "sample.md")


# --- the check ---------------------------------------------------------------------------


@pytest.mark.req("25.9", "25.10")
def test_the_check_passes_on_the_committed_index_without_the_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fresh checkout case: no requirements.md anywhere, the committed index and an
    # export that covers every traced criterion (requirement 25.10).
    index = read_index(INDEX)
    markers = write_markers(tmp_path, covering(index))

    trace(markers=markers, source=tmp_path / "no-spec.md", path=INDEX)

    said = capsys.readouterr().err
    assert "没有 ../.kiro/specs/erp-mock-mcp-m0/requirements.md" in said
    assert f"需求 {FIRST_TRACED} 到 {LAST_TRACED} 的 {len(index.traced())} 条验收标准" in said


@pytest.mark.req("25.9")
def test_a_criterion_without_a_test_fails_the_check(tmp_path: Path) -> None:
    index = read_index(INDEX)
    missing = next(criterion for criterion in index.traced() if criterion.id == "24.9")
    markers = write_markers(tmp_path, covering(index, without=missing.id))

    with pytest.raises(TraceFailed) as failure:
        trace(markers=markers, source=tmp_path / "no-spec.md", path=INDEX)

    report = str(failure.value)
    assert "有 1 条验收标准没有对应测试" in report
    # The report names the criterion and quotes its text, so it says which requirement is
    # untested rather than only how many are.
    assert missing.id in report
    assert missing.summary in report


@pytest.mark.req("25.9")
def test_a_marker_naming_an_item_the_index_does_not_have_fails_the_check(tmp_path: Path) -> None:
    # req("24.99") is well formed and inside the range of requirement numbers, so the
    # collection check accepts it; requirement 24 has no item 99, so nothing it was meant
    # to cover would ever be covered.
    index = read_index(INDEX)
    covered = covering(index)
    covered["24.99"] = ["tests/unit/test_sample.py::test_typo"]
    markers = write_markers(tmp_path, covered)

    with pytest.raises(TraceFailed) as failure:
        trace(markers=markers, source=tmp_path / "no-spec.md", path=INDEX)

    report = str(failure.value)
    assert "清单中不存在的条目" in report
    assert "24.99：tests/unit/test_sample.py::test_typo" in report


@pytest.mark.req("25.9")
def test_an_export_that_is_no_marker_list_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "req-markers.json"
    path.write_text('{"tests": []}', encoding="utf-8")
    with pytest.raises(TraceFailed, match="标记清单"):
        trace(markers=path, source=tmp_path / "no-spec.md", path=INDEX)


# --- the export option -------------------------------------------------------------------


@pytest.mark.req("25.9")
def test_the_export_option_writes_the_criteria_of_every_collected_test(
    pytester: pytest.Pytester, tmp_path: Path
) -> None:
    pytester.makeini(
        """
        [pytest]
        addopts = --strict-markers
        markers =
            req(*criteria): acceptance criteria covered by the test
        """
    )
    pytester.makeconftest(SAMPLE_CONFTEST)
    pytester.makepyfile(test_sample=SAMPLE_TESTS)
    export = tmp_path / "markers" / "req-markers.json"

    result = pytester.runpytest("--collect-only", f"{EXPORT_OPTION}={export}")

    assert result.ret == pytest.ExitCode.OK
    # The parent directory was created and the document holds every marked test in
    # collection order, each with its criteria deduplicated and in criterion order:
    # "9.9" before "9.10", a criterion named twice once, and the marker of the module, of
    # the class and of one pytest.param each where it applies.
    document = json.loads(export.read_text(encoding="utf-8"))
    assert document == {
        "collected": 4,
        "tests": [
            {"nodeid": "test_sample.py::test_marked", "criteria": ["5.1", "5.2"]},
            {
                "nodeid": "test_sample.py::test_inherits_only_the_module_marker",
                "criteria": ["5.1"],
            },
            {"nodeid": "test_sample.py::TestClass::test_method[1]", "criteria": ["5.1", "9.9"]},
            {
                "nodeid": "test_sample.py::TestClass::test_method[2]",
                "criteria": ["5.1", "9.9", "9.10"],
            },
        ],
    }


@pytest.mark.req("25.9")
def test_a_run_without_the_option_writes_nothing(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(SAMPLE_CONFTEST)
    pytester.makepyfile(test_sample="def test_nothing():\n    pass\n")

    result = pytester.runpytest("--collect-only")

    assert result.ret == pytest.ExitCode.OK
    assert list(pytester.path.glob("*.json")) == []
