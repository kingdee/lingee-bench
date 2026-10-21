"""Collection-time validation of ``req`` markers, and the export of what they say (25.9).

Every test names the acceptance criteria it covers with
``@pytest.mark.req("<requirement>.<item>", ...)``, e.g. ``req("23.7", "23.8")``.
tests/conftest.py loads this module as a pytest plugin. When a test collected from a
module or class carries a malformed ``req`` marker, the collection of that module or
class fails: pytest reports "ERROR collecting <node>" listing the offending arguments and
stops with "Interrupted: N errors during collection" (exit code 2) before any test runs,
the same way it handles a test module that cannot be imported.

:func:`marker_export` is the other half: the document ``--export-req-markers`` writes
(tests/conftest.py), which is what the requirement tracing check of scripts/req_trace.py
compares with tests/req_index.json.
"""

import re
from collections.abc import Generator, Sequence

import pytest

from erp_mock.common.jsonio import JsonValue

MARKER = "req"
FIRST_REQUIREMENT = 1
LAST_REQUIREMENT = 25

# ASCII digits only (``\d`` would also accept e.g. full-width digits), no leading zeros.
_CRITERION = re.compile(r"([1-9][0-9]*)\.([1-9][0-9]*)")

# The keys of the exported document (:func:`marker_export`).
COLLECTED_KEY = "collected"
TESTS_KEY = "tests"
NODEID_KEY = "nodeid"
CRITERIA_KEY = "criteria"


def criterion_problem(value: object) -> str | None:
    """Return why ``value`` is not a criterion such as "23.7", or None if it is one."""
    if not isinstance(value, str):
        return f"{value!r} is not a string"
    match = _CRITERION.fullmatch(value)
    if match is None:
        return (
            f"{value!r} is not of the form '<requirement>.<item>'"
            " (positive integers without leading zeros)"
        )
    requirement = int(match.group(1))
    if not FIRST_REQUIREMENT <= requirement <= LAST_REQUIREMENT:
        return (
            f"{value!r} names requirement {requirement},"
            f" outside {FIRST_REQUIREMENT}-{LAST_REQUIREMENT}"
        )
    return None


def criterion_order(value: str) -> tuple[int, int, str]:
    """Sort key of a criterion: by requirement, then by item, both as numbers.

    "9.10" therefore sorts after "9.9" and before "10.1". A value that is no criterion -
    which the collection check rejects, so it cannot reach an export - sorts after every
    one that is, by its text.
    """
    match = _CRITERION.fullmatch(value)
    if match is None:
        return (LAST_REQUIREMENT + 1, 0, value)
    return (int(match.group(1)), int(match.group(2)), "")


def mark_problems(mark: pytest.Mark) -> list[str]:
    """Return the problems of one ``req`` mark; an empty list means the mark is valid."""
    problems: list[str] = []
    if not mark.args:
        problems.append("no criteria given")
    if mark.kwargs:
        problems.append(f"keyword arguments are not allowed: {', '.join(sorted(mark.kwargs))}")
    for value in mark.args:
        problem = criterion_problem(value)
        if problem is not None:
            problems.append(problem)
    return problems


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    """Fail the collection of ``collector`` if a test it yields has an invalid ``req`` mark.

    Markers inherited from the class or module (``pytestmark``) are checked on each test.
    """
    report = yield
    if not report.passed:
        return report
    problems = [
        f"{node.nodeid}: {problem}"
        for node in report.result
        if isinstance(node, pytest.Item)
        for mark in node.iter_markers(name=MARKER)
        for problem in mark_problems(mark)
    ]
    if not problems:
        return report
    header = (
        'invalid req marker; write @pytest.mark.req("<requirement>.<item>", ...)'
        f' with requirement {FIRST_REQUIREMENT}-{LAST_REQUIREMENT}, e.g. req("23.7"):'
    )
    longrepr = "\n".join([header, *(f"  {line}" for line in problems)])
    return pytest.CollectReport(report.nodeid, "failed", longrepr, [], report.sections)


def criteria_of(item: pytest.Item) -> tuple[str, ...]:
    """The criteria ``item`` is marked with, deduplicated and in criterion order.

    Every ``req`` marker that applies to the test counts, whether it sits on the test, on
    its class, on its module (``pytestmark``) or on one ``pytest.param``, which is what
    ``iter_markers`` walks. The same criterion named twice is one criterion.
    """
    found = {
        value
        for mark in item.iter_markers(name=MARKER)
        for value in mark.args
        if isinstance(value, str)
    }
    return tuple(sorted(found, key=criterion_order))


def marker_export(items: Sequence[pytest.Item]) -> dict[str, JsonValue]:
    """The document ``--export-req-markers`` writes for the tests ``items``.

    ``collected`` is how many tests the run collected and ``tests`` the marked ones among
    them, in collection order, each with its nodeid and its criteria::

        {"collected": 4321,
         "tests": [{"nodeid": "tests/unit/domain/test_crud.py::test_create",
                    "criteria": ["5.1", "5.2"]}]}

    A test without a ``req`` marker contributes nothing but is counted in ``collected``,
    so a reader of the file can tell an export of the whole suite from one of a selection.
    """
    return {
        COLLECTED_KEY: len(items),
        TESTS_KEY: [
            {NODEID_KEY: item.nodeid, CRITERIA_KEY: list(criteria)}
            for item in items
            if (criteria := criteria_of(item))
        ],
    }
