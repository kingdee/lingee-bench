"""erp_mock.loader.yaml_io: YAML 1.2 core scalars, errors located by line, file reading."""

import math
import sys
from pathlib import Path

import pytest

from erp_mock.loader.yaml_io import (
    YamlLoadError,
    YamlValue,
    list_files,
    load_yaml,
    parse_yaml,
    read_text,
)

FILE = Path("entities/purchase_order.yaml")


def scalar(text: str) -> YamlValue:
    """Parse ``text`` as the value of a one-key mapping."""
    data = parse_yaml(f"v: {text}\n", FILE)
    assert isinstance(data, dict)
    return data["v"]


def parse_error(text: str) -> YamlLoadError:
    with pytest.raises(YamlLoadError) as caught:
        parse_yaml(text, FILE)
    return caught.value


# --- scalar resolution -------------------------------------------------------------------


@pytest.mark.req("2.2")
def test_on_key_stays_a_string() -> None:
    text = "rule:\n  on: approve\nwriteback:\n  on: [approve, unapprove]\n"
    assert parse_yaml(text, FILE) == {
        "rule": {"on": "approve"},
        "writeback": {"on": ["approve", "unapprove"]},
    }
    assert parse_yaml("yes: 1\nno: 2\non: 3\noff: 4\n", FILE) == {
        "yes": 1,
        "no": 2,
        "on": 3,
        "off": 4,
    }


@pytest.mark.req("2.2")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # bool: true and false only, lower-case, capitalised or upper-case
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("tRUE", "tRUE"),
        ("yes", "yes"),
        ("no", "no"),
        ("on", "on"),
        ("off", "off"),
        ("Yes", "Yes"),
        ("OFF", "OFF"),
        ("y", "y"),
        ("n", "n"),
        # null
        ("~", None),
        ("null", None),
        ("Null", None),
        ("NULL", None),
        ("", None),
        ("nULL", "nULL"),
        # int: decimal only, no leading zero
        ("0", 0),
        ("12", 12),
        ("-12", -12),
        ("+12", 12),
        ("12345678901234567890", 12345678901234567890),
        ("012", "012"),
        ("-012", "-012"),
        ("0o17", "0o17"),
        ("0x1F", "0x1F"),
        ("0b101", "0b101"),
        ("1_000", "1_000"),
        ("1:30", "1:30"),
        # float: decimal notation of the core schema, .inf
        ("0.13", 0.13),
        (".5", 0.5),
        ("5.", 5.0),
        ("1e3", 1000.0),
        ("-1.5E-3", -0.0015),
        ("012.5", 12.5),
        (".inf", math.inf),
        ("-.inf", -math.inf),
        ("+.INF", math.inf),
        ("inf", "inf"),
        ("NaN", "NaN"),
        ("1e", "1e"),
        ("1,000", "1,000"),
        # no merge or value keys
        ("<<", "<<"),
        ("=", "="),
        # explicit core tags
        ("!!str 012", "012"),
        ("!!str true", "true"),
        ("!!int '12'", 12),
        ("!!float 12", 12.0),
    ],
)
def test_plain_scalars(text: str, expected: YamlValue) -> None:
    value = scalar(text)
    assert type(value) is type(expected)
    assert value == expected


@pytest.mark.req("2.2")
@pytest.mark.parametrize("text", [".nan", ".NaN", ".NAN"])
def test_nan(text: str) -> None:
    value = scalar(text)
    assert isinstance(value, float)
    assert math.isnan(value)


@pytest.mark.req("16.3")
def test_dates_stay_strings() -> None:
    # yaml.safe_load returns date(2026, 10, 9) and raises ValueError for 2026-02-30. Here
    # the config models get the text, so they can report an invalid date at its config path.
    text = "clock:\n  anchor_date: 2026-10-09\n  a: 2026-02-30\n  b: 2026-2-3\n  c: 2026-10-09T08:00:00Z\n"
    assert parse_yaml(text, FILE) == {
        "clock": {
            "anchor_date": "2026-10-09",
            "a": "2026-02-30",
            "b": "2026-2-3",
            "c": "2026-10-09T08:00:00Z",
        }
    }


@pytest.mark.req("2.2")
def test_empty_document_is_none() -> None:
    assert parse_yaml("", FILE) is None
    assert parse_yaml("# comment only\n", FILE) is None


# --- errors ------------------------------------------------------------------------------


@pytest.mark.req("2.2")
@pytest.mark.parametrize(
    ("text", "line", "column", "reason"),
    [
        ("a: 1\nb: 2\na: 3\n", 3, 1, '重复的键 "a"（第 1 行已出现）'),
        (
            'rule:\n  on: approve\n  check: x\n  "on": submit\n',
            4,
            3,
            '重复的键 "on"（第 2 行已出现）',
        ),
        ("entities:\n  - alias: s1\nentities: []\n", 3, 1, '重复的键 "entities"（第 1 行已出现）'),
        ("true: 1\nTrue: 2\n", 2, 1, '重复的键 "True"（第 1 行已出现）'),
        ("a: {x: 1, x: 2}\n", 1, 11, '重复的键 "x"（第 1 行已出现）'),
        ("a: 1\r\nb: 2\r\na: 3\r\n", 3, 1, '重复的键 "a"（第 1 行已出现）'),
    ],
)
def test_repeated_key_is_reported_at_its_line(
    text: str, line: int, column: int, reason: str
) -> None:
    error = parse_error(text)
    assert (error.file, error.line, error.column) == (FILE, line, column)
    assert error.config_path == f"line {line}"
    assert error.reason == f"YAML 无法解析：{reason}"


@pytest.mark.req("2.2")
@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("a: 1\n  b: 2\n", 2),  # mapping value in a scalar
        ("a:\n  b: 1\n c: 2\n", 3),  # indentation
        ("a: [1, 2\nb: 3\n", 2),  # unclosed flow sequence
        ('a: 1\nb: "abc\nc: 2\n', 4),  # unclosed quote, found at the end of the stream
        ("a:\n\tb: 1\n", 2),  # tab indentation
        ("a: 1\n---\nb: 2\n", 2),  # a second document
        ("a: 1\nb: *missing\n", 2),  # undefined alias
        ("a: 1\nb: 2\nc: x\x07y\n", 3),  # control character
        ("a: 1\nb: x\ud800y\n", 2),  # lone surrogate
    ],
)
def test_syntax_error_is_reported_at_its_line(text: str, line: int) -> None:
    error = parse_error(text)
    assert error.file == FILE
    assert error.line == line
    assert error.config_path == f"line {line}"
    assert error.reason.startswith("YAML 无法解析：")


@pytest.mark.req("2.2")
def test_error_reason_names_where_the_construct_started() -> None:
    error = parse_error('a: 1\nb: "abc\nc: 2\n')
    assert error.reason == (
        "YAML 无法解析：while scanning a quoted scalar (line 2), found unexpected end of stream"
    )
    assert str(error) == f"{FILE}:4:1: {error.reason}"


@pytest.mark.req("2.2")
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("d: !!timestamp 2026-10-09\n", "不支持的标签 !!timestamp"),
        ("a: !!binary aGk=\n", "不支持的标签 !!binary"),
        ("a: !!set {x: null}\n", "不支持的标签 !!set"),
        ("a: !!python/name:os.system\n", "不支持的标签 !!python/name:os.system"),
        ("a: !local x\n", "不支持的标签 !local"),
        ("base: {x: 1}\nd:\n  !!merge <<: {x: 2}\n", "不支持的标签 !!merge"),
        ("a: !!int 0x1F\n", '!!int 不接受 "0x1F"'),
        ("a: !!bool yes\n", '!!bool 不接受 "yes"'),
        ("a: !!null x\n", '!!null 不接受 "x"'),
        ("n: " + "1" * 5000 + "\n", f"整数的位数超过上限 {sys.get_int_max_str_digits()}"),
    ],
)
def test_values_outside_the_core_schema_are_errors(text: str, reason: str) -> None:
    error = parse_error(text)
    assert error.line == text.count("\n")  # each problem is on the last line
    assert error.reason == f"YAML 无法解析：{reason}"


# --- files and directories ---------------------------------------------------------------


@pytest.mark.req("2.2")
def test_read_text_decodes_utf8(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_bytes("\ufeffname: 采购订单\r\n".encode())
    assert read_text(path) == "name: 采购订单\r\n"  # without the BOM, line breaks as written


@pytest.mark.req("2.2")
def test_invalid_utf8_is_reported_at_its_line(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_bytes("a: 1\nname: 采购订单\n".encode("gbk"))
    with pytest.raises(YamlLoadError) as caught:
        read_text(path)
    error = caught.value
    assert (error.file, error.line, error.column) == (path, 2, 7)
    assert error.config_path == "line 2"
    assert error.reason.startswith("不是有效的 UTF-8 文本")


@pytest.mark.req("2.2")
def test_unreadable_file_has_no_line(tmp_path: Path) -> None:
    for path in (tmp_path / "missing.yaml", tmp_path):
        with pytest.raises(YamlLoadError) as caught:
            read_text(path)
        error = caught.value
        assert (error.file, error.line, error.column, error.config_path) == (path, None, None, None)
        assert error.reason.startswith("无法读取文件：")


@pytest.mark.req("2.2")
def test_load_yaml_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "rules" / "r1.yaml"
    path.parent.mkdir()
    path.write_text("id: R-PUR-001\non: approve\n", encoding="utf-8")
    assert load_yaml(path) == {"id": "R-PUR-001", "on": "approve"}
    path.write_text("id: R-PUR-001\non: approve\non: submit\n", encoding="utf-8")
    with pytest.raises(YamlLoadError) as caught:
        load_yaml(path)
    assert (caught.value.file, caught.value.line) == (path, 3)


@pytest.mark.req("2.2")
def test_list_files_sorts_by_path(tmp_path: Path) -> None:
    # Created out of order. Names differ in more than case, for case-insensitive file systems.
    for relative in [
        "b.yaml",
        "a/x.yaml",
        "a.yaml",
        "Z.yaml",
        "a-b.yaml",
        "a/c/d.yaml",
        "dir.yaml/e.yaml",
        "notes.txt",
        "f.yml",
        ".hidden.yaml",
        ".git/config.yaml",
    ]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a: 1\n", encoding="utf-8")

    def files(*relative: str) -> tuple[Path, ...]:
        return tuple(tmp_path / p for p in relative)

    # Code point order of the relative path: "Z" < "a", "-" < "." < "/".
    assert list_files(tmp_path) == files(
        "Z.yaml", "a-b.yaml", "a.yaml", "a/c/d.yaml", "a/x.yaml", "b.yaml", "dir.yaml/e.yaml"
    )
    assert list_files(tmp_path, recursive=False) == files("Z.yaml", "a-b.yaml", "a.yaml", "b.yaml")
    assert list_files(tmp_path, ".txt") == files("notes.txt")
    assert list_files(tmp_path / "missing") == ()
    with pytest.raises(YamlLoadError) as caught:
        list_files(tmp_path / "b.yaml")
    assert (caught.value.file, caught.value.line) == (tmp_path / "b.yaml", None)
