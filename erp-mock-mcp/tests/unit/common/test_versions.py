"""erp_mock.common.versions: package versions and version ranges (requirements 2.3, 2.4)."""

from collections.abc import Callable

import pytest

from erp_mock.common.versions import Comparator, SemVer, VersionError, VersionRange


def rejection(parse: Callable[[str], object], text: str) -> VersionError:
    """Parse ``text``, which must fail, and return the error."""
    with pytest.raises(VersionError) as info:
        parse(text)
    assert info.value.text == text
    assert info.value.reason
    return info.value


# --- versions (requirement 2.3) ----------------------------------------------------------


@pytest.mark.req("2.3")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.0.0", SemVer(0, 0, 0)),
        ("0.1.0", SemVer(0, 1, 0)),
        ("1.2.3", SemVer(1, 2, 3)),
        ("10.20.30", SemVer(10, 20, 30)),
    ],
)
def test_parse_version(text: str, expected: SemVer) -> None:
    version = SemVer.parse(text)
    assert version == expected
    assert str(version) == text


@pytest.mark.req("2.3")
@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("01.0.0", "前导零"),
        ("1.00.0", "前导零"),
        ("1.0.01", "前导零"),
        ("1.0.0-rc1", "后缀"),
        ("1.0.0+build.5", "后缀"),
        (">=1.0.0", "范围"),  # a range is not a version
        ("=1.0.0", "范围"),
        ("~1.0.0", "范围"),
        (">=1.0", "范围"),
        ("1.0", "三段"),
        ("1.0.0.0", "三段"),
        ("", "为空"),
        ("1..0", "次版本号为空"),
        (" 1.0.0", "空白"),
        ("1.0.0\n", "空白"),
        ("v1.0.0", "非负整数"),
        ("-1.0.0", "非负整数"),
        ("1.x.0", "非负整数"),
        ("\uff11.0.0", "非负整数"),  # full-width digit one
    ],
)
def test_parse_version_rejects(text: str, fragment: str) -> None:
    assert fragment in rejection(SemVer.parse, text).reason


@pytest.mark.req("2.4")
def test_versions_compare_numerically_segment_by_segment() -> None:
    texts = ["1.10.0", "0.9.9", "1.9.10", "2.0.0", "1.9.9", "0.10.0"]
    ordered = sorted(SemVer.parse(text) for text in texts)
    assert [str(v) for v in ordered] == ["0.9.9", "0.10.0", "1.9.9", "1.9.10", "1.10.0", "2.0.0"]
    assert SemVer.parse("1.10.0") > SemVer.parse("1.9.0")


@pytest.mark.req("2.3")
def test_segments_of_any_length() -> None:
    # More digits than int() and str() convert by default (sys.get_int_max_str_digits()).
    text = "1" + "0" * 5000 + ".0.0"
    version = SemVer.parse(text)
    assert version.major == 10**5000
    assert str(version) == text
    assert version > SemVer.parse("9.0.0")


# --- version ranges (requirement 2.4) ----------------------------------------------------


@pytest.mark.req("2.4")
@pytest.mark.parametrize(
    ("text", "comparators", "canonical"),
    [
        ("1.0.0", (("=", SemVer(1, 0, 0)),), "=1.0.0"),  # no operator means =
        ("=1.0.0", (("=", SemVer(1, 0, 0)),), "=1.0.0"),
        (
            ">=0.1.0 <1.0.0",
            ((">=", SemVer(0, 1, 0)), ("<", SemVer(1, 0, 0))),
            ">=0.1.0 <1.0.0",
        ),
        (
            ">0.1.0 <=0.9.9",
            ((">", SemVer(0, 1, 0)), ("<=", SemVer(0, 9, 9))),
            ">0.1.0 <=0.9.9",
        ),
        (
            ">=0.1.0   <1.0.0",  # several spaces separate comparators too
            ((">=", SemVer(0, 1, 0)), ("<", SemVer(1, 0, 0))),
            ">=0.1.0 <1.0.0",
        ),
        ("1.0.0 2.0.0", (("=", SemVer(1, 0, 0)), ("=", SemVer(2, 0, 0))), "=1.0.0 =2.0.0"),
    ],
)
def test_parse_range(text: str, comparators: tuple[Comparator, ...], canonical: str) -> None:
    version_range = VersionRange.parse(text)
    assert version_range.comparators == comparators
    assert str(version_range) == canonical
    assert VersionRange.parse(canonical) == version_range


@pytest.mark.req("2.4")
@pytest.mark.parametrize(
    ("text", "version", "expected"),
    [
        (">=0.1.0 <1.0.0", "0.3.2", True),
        (">=0.1.0 <1.0.0", "1.0.0", False),
        (">=0.1.0 <1.0.0", "0.1.0", True),
        (">=0.1.0 <1.0.0", "0.0.9", False),
        (">=0.1.0 <1.0.0", "0.99.99", True),
        (">1.2.3", "1.2.3", False),
        (">1.2.3", "1.2.4", True),
        ("<=1.2.3", "1.2.3", True),
        ("<=1.2.3", "1.3.0", False),
        ("<1.2.3", "1.2.2", True),
        ("<1.2.3", "1.2.3", False),
        ("=1.2.3", "1.2.3", True),
        ("=1.2.3", "1.2.4", False),
        ("1.2.3", "1.2.3", True),
        ("1.2.3", "1.2.30", False),
        (">=1.9.0", "1.10.0", True),  # numbers, not texts, compare
        (">=2.0.0 <1.0.0", "1.5.0", False),  # no version satisfies both
    ],
)
def test_matches(text: str, version: str, expected: bool) -> None:
    assert VersionRange.parse(text).matches(SemVer.parse(version)) is expected


@pytest.mark.req("2.4")
@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("~1.0.0", "不支持的运算符"),
        ("^1.0.0", "不支持的运算符"),
        ("==1.0.0", "不支持的运算符"),
        ("!=1.0.0", "不支持的运算符"),
        ("=>1.0.0", "不支持的运算符"),
        (">=0.1.0 ~1.0.0", '比较式 "~1.0.0"'),  # names the offending comparator
        (">=1.0", "三段"),
        (">=0.1.0 <1.0", "三段"),
        (">=1.0.0,<2.0.0", "三段"),
        ("01.0.0", "前导零"),
        (">=1.0.01", "前导零"),
        ("1.0.0-rc1", "后缀"),
        ("<1.0.0+build", "后缀"),
        ("", "没有比较式"),
        ("   ", "没有比较式"),
        (" >=1.0.0", "首尾"),
        (">=1.0.0 ", "首尾"),
        (">= 1.0.0", "缺少版本"),
        (">=1.0.0\t<2.0.0", "空白"),
        (">=1.0.0\u3000<2.0.0", "空白"),  # ideographic space
    ],
)
def test_parse_range_rejects(text: str, fragment: str) -> None:
    assert fragment in rejection(VersionRange.parse, text).reason


# --- errors and constructors -------------------------------------------------------------


@pytest.mark.req("2.3", "2.4")
def test_version_error() -> None:
    assert issubclass(VersionError, ValueError)
    error = rejection(VersionRange.parse, ">=0.1.0 01.0.0")
    assert error.reason == '比较式 "01.0.0" 不合法：主版本号 "01" 有前导零'
    assert str(error) == '">=0.1.0 01.0.0"：' + error.reason


@pytest.mark.req("2.3", "2.4")
def test_constructors_reject_values_without_a_text() -> None:
    with pytest.raises(ValueError):
        SemVer(-1, 0, 0)
    with pytest.raises(TypeError):
        SemVer(True, 0, 0)
    with pytest.raises(TypeError):
        SemVer(1, 0.0, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        VersionRange(())
    with pytest.raises(ValueError):
        VersionRange((("~", SemVer(1, 0, 0)),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        VersionRange([("=", SemVer(1, 0, 0))])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SemVer.parse(100)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        VersionRange.parse(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        VersionRange.parse("1.0.0").matches("1.0.0")  # type: ignore[arg-type]
