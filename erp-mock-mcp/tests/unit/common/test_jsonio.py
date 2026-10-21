"""erp_mock.common.jsonio: canonical JSON text and the UTF-8 bytes written to files."""

import json
import math
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final

import pytest

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_compact, dumps_pretty, encode_utf8

DUMPS: Final[list[Callable[[JsonValue], str]]] = [dumps_compact, dumps_pretty]
DUMPS_IDS: Final = ["compact", "pretty"]

# --- layout and key order (requirements 13.1, 24.2) --------------------------------------


@pytest.mark.req("13.1", "24.2")
def test_keys_keep_insertion_order() -> None:
    obj: JsonValue = {"z": 1, "a": {"y": None, "b": [True, False]}, "m": "x"}
    assert dumps_compact(obj) == '{"z":1,"a":{"y":null,"b":[true,false]},"m":"x"}'
    assert list(json.loads(dumps_pretty(obj))["a"]) == ["y", "b"]


@pytest.mark.req("24.2")
def test_pretty_layout() -> None:
    obj: JsonValue = {
        "base": "initial",
        "changes": [{"op": "update", "before": {}, "ids": []}],
        "n": 2,
    }
    assert dumps_pretty(obj) == (
        "{\n"
        '  "base": "initial",\n'
        '  "changes": [\n'
        "    {\n"
        '      "op": "update",\n'
        '      "before": {},\n'
        '      "ids": []\n'
        "    }\n"
        "  ],\n"
        '  "n": 2\n'
        "}\n"
    )


@pytest.mark.req("13.1", "24.2")
def test_tuples_and_frozen_maps_serialize_as_arrays_and_objects() -> None:
    obj = FrozenMap({"b": (1, FrozenMap({"y": None, "x": "1.00"})), "a": ()})
    assert dumps_compact(obj) == '{"b":[1,{"y":null,"x":"1.00"}],"a":[]}'
    assert dumps_pretty(obj) == dumps_pretty({"b": [1, {"y": None, "x": "1.00"}], "a": []})


# --- characters (requirements 13.1, 17.8) ------------------------------------------------


@pytest.mark.req("13.1", "17.8")
@pytest.mark.parametrize("dumps", DUMPS, ids=DUMPS_IDS)
def test_non_ascii_characters_are_not_escaped(dumps: Callable[[JsonValue], str]) -> None:
    # CJK, Latin-1, an emoji and a CJK Extension B character (the last two outside the BMP)
    text = "示例供应商甲 Ünïcödé \U0001f600 \U00020000"
    out = dumps({"名称": text})
    assert "名称" in out
    assert text in out
    assert "\\u" not in out
    assert json.loads(out) == {"名称": text}


@pytest.mark.req("17.8")
def test_compact_text_is_one_line() -> None:
    # Every character that str.splitlines() treats as a line break, plus quotes,
    # backslashes and other control characters.
    value = 'a\nb\rc\r\nd\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029e\t"f\\g\x00\x1f'
    obj = {"remark": value, value: [value]}
    out = dumps_compact(obj)
    assert out.splitlines() == [out]
    assert json.loads(out) == obj
    for escape in ("\\n", "\\r", '\\"', "\\\\", "\\u0085", "\\u2028", "\\u2029"):
        assert escape in out


@pytest.mark.req("17.8")
@pytest.mark.parametrize(
    "value",
    [
        "\ud800",
        "a\udfffb",
        "\ude00\ud83d",  # a low surrogate before a high one is not a pair
        "x\\\ud800",  # a backslash right before the surrogate
        "\U0001f600\udbff",
        "\udc00\U0001f600",
    ],
)
@pytest.mark.parametrize("dumps", DUMPS, ids=DUMPS_IDS)
def test_lone_surrogates_read_back(dumps: Callable[[JsonValue], str], value: str) -> None:
    obj: JsonValue = {"args": {"remark": value}, value: [value]}
    data = encode_utf8(dumps(obj))
    text = data.decode("utf-8")  # strict: the bytes are valid UTF-8
    assert json.loads(text) == obj


@pytest.mark.req("17.8")
def test_encode_utf8() -> None:
    assert encode_utf8("a\ud800") == b"a\\ud800"
    assert encode_utf8("\udfff") == b"\\udfff"
    assert encode_utf8("供应商\U0001f600") == "供应商\U0001f600".encode()


@pytest.mark.req("17.8")
def test_surrogate_pair_reads_back_as_one_character() -> None:
    # JSON cannot spell a high and a low surrogate as two code points (documented).
    data = encode_utf8(dumps_compact("\ud83d\ude00"))
    assert data == b'"\\ud83d\\ude00"'
    assert json.loads(data) == "\U0001f600"


# --- values that are not JSON ------------------------------------------------------------


@pytest.mark.req("13.1")
@pytest.mark.parametrize("number", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("dumps", DUMPS, ids=DUMPS_IDS)
def test_nan_and_infinities_are_rejected(dumps: Callable[[JsonValue], str], number: float) -> None:
    with pytest.raises(ValueError):
        dumps({"x": [number]})


@pytest.mark.req("13.1")
@pytest.mark.parametrize("value", [Decimal("1.5"), {1, 2}, b"x", object()])
@pytest.mark.parametrize("dumps", DUMPS, ids=DUMPS_IDS)
def test_other_values_are_rejected(dumps: Callable[[JsonValue], str], value: Any) -> None:
    with pytest.raises(TypeError):
        dumps({"x": value})
