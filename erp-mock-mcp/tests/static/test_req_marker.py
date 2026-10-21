"""The req marker check: which arguments it accepts and how a violation fails collection."""

import pytest

from tests.helpers.req_marker import criterion_order, criterion_problem, mark_problems

PLUGIN = "tests.helpers.req_marker"


@pytest.mark.req("25.9")
@pytest.mark.parametrize("criterion", ["1.1", "9.10", "23.7", "24.11", "25.1", "25.11", "1.123"])
def test_well_formed_criteria_are_accepted(criterion: str) -> None:
    assert criterion_problem(criterion) is None


@pytest.mark.req("25.9")
@pytest.mark.parametrize(
    "criterion",
    [
        "0.1",  # requirement numbers start at 1
        "26.1",  # past the last requirement
        "100.1",
        "1.0",  # item numbers start at 1
        "01.1",  # no leading zeros
        "1.01",
        "1",
        "1.",
        ".1",
        "1.1.1",
        "R1.1",
        "1,1",
        "1-1",
        " 1.1",
        "1.1 ",
        "1.1\n",
        "\uff11.\uff11",  # full-width digits
        "",
    ],
)
def test_malformed_or_out_of_range_criteria_are_rejected(criterion: str) -> None:
    assert criterion_problem(criterion) is not None


@pytest.mark.req("25.9")
@pytest.mark.parametrize("value", [1.1, 25, True, None, b"1.1", ("23", "7")])
def test_non_string_arguments_are_rejected(value: object) -> None:
    assert criterion_problem(value) is not None


@pytest.mark.req("25.9")
def test_mark_with_several_valid_criteria_is_accepted() -> None:
    assert mark_problems(pytest.mark.req("23.7", "23.8", "25.9").mark) == []


@pytest.mark.req("25.9")
def test_mark_reports_every_invalid_argument() -> None:
    problems = mark_problems(pytest.mark.req("23.7", "26.1", "1.0").mark)
    assert len(problems) == 2
    assert "'26.1'" in problems[0]
    assert "'1.0'" in problems[1]


@pytest.mark.req("25.9")
def test_criteria_sort_by_requirement_and_item_as_numbers() -> None:
    # The order an export and a report list criteria in: "9.10" is item ten of requirement
    # nine, so it comes after "9.9" and before "10.1", which sorting as text would not do.
    ordered = sorted(["10.1", "9.10", "9.9", "1.2"], key=criterion_order)
    assert ordered == ["1.2", "9.9", "9.10", "10.1"]
    # A value that is no criterion - which the collection check rejects, so it reaches no
    # export - sorts after every one that is, rather than raising.
    assert sorted(["9.9", "oops"], key=criterion_order) == ["9.9", "oops"]


@pytest.mark.req("25.9")
def test_mark_without_criteria_is_rejected() -> None:
    assert mark_problems(pytest.mark.req.mark) == ["no criteria given"]


@pytest.mark.req("25.9")
def test_mark_with_keyword_arguments_is_rejected() -> None:
    assert mark_problems(pytest.mark.req("23.7", item="23.8").mark) == [
        "keyword arguments are not allowed: item"
    ]


def _run_sample(pytester: pytest.Pytester, source: str) -> pytest.RunResult:
    """Run ``source`` as test_sample.py in a scratch project with the req marker check."""
    pytester.makeini(
        """
        [pytest]
        addopts = --strict-markers
        markers =
            req(*criteria): acceptance criteria covered by the test
        """
    )
    pytester.makepyfile(test_sample=source)
    return pytester.runpytest("-p", PLUGIN)


@pytest.mark.req("25.9")
def test_valid_markers_are_collected_and_run(pytester: pytest.Pytester) -> None:
    result = _run_sample(
        pytester,
        """
        import pytest

        pytestmark = pytest.mark.req("25.9")

        @pytest.mark.req("23.7", "23.8")
        def test_function():
            pass

        @pytest.mark.req("1.1")
        class TestClass:
            @pytest.mark.parametrize("x", [1, pytest.param(2, marks=pytest.mark.req("24.11"))])
            def test_method(self, x):
                pass
        """,
    )
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=3)


@pytest.mark.req("25.9")
def test_invalid_marker_fails_collection(pytester: pytest.Pytester) -> None:
    result = _run_sample(
        pytester,
        """
        import pytest

        @pytest.mark.req("23.7")
        def test_valid():
            pass

        @pytest.mark.req("23.7", "26.1")
        def test_out_of_range():
            pass
        """,
    )
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR collecting test_sample.py*",
            "invalid req marker;*",
            "  test_sample.py::test_out_of_range: '26.1' names requirement 26, outside 1-25",
            "*Interrupted: 1 error during collection*",
        ]
    )
    result.stdout.no_fnmatch_line("*test_valid*")


@pytest.mark.req("25.9")
def test_invalid_marker_in_class_fails_collection_of_the_class(pytester: pytest.Pytester) -> None:
    result = _run_sample(
        pytester,
        """
        import pytest

        class TestClass:
            @pytest.mark.req()
            def test_method(self):
                pass
        """,
    )
    assert result.ret == pytest.ExitCode.INTERRUPTED
    # The section heading names the file; the short summary names the failed collector.
    result.stdout.fnmatch_lines(
        [
            "*ERROR collecting test_sample.py*",
            "  test_sample.py::TestClass::test_method: no criteria given",
            "ERROR test_sample.py::TestClass - invalid req marker;*",
        ]
    )
