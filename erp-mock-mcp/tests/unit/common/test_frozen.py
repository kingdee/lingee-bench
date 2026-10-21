"""erp_mock.common.frozen: FrozenMap as a read-only mapping and as a pydantic field type."""

import copy
import pickle
from collections.abc import Callable
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from erp_mock.common.frozen import FrozenMap

# --- mapping behaviour -------------------------------------------------------------------


@pytest.mark.req("2.11")
def test_keeps_insertion_order() -> None:
    m = FrozenMap({"b": 1, "a": 2, "c": 3})
    assert list(m) == ["b", "a", "c"]
    assert list(m.keys()) == ["b", "a", "c"]
    assert list(m.values()) == [1, 2, 3]
    assert list(m.items()) == [("b", 1), ("a", 2), ("c", 3)]
    assert list(FrozenMap([("y", 1), ("x", 2)]).items()) == [("y", 1), ("x", 2)]


@pytest.mark.req("2.11")
def test_reads_like_a_mapping() -> None:
    m = FrozenMap({"a": 1})
    assert m["a"] == 1
    assert m.get("a") == 1
    assert m.get("z") is None
    assert "a" in m
    assert "z" not in m
    assert len(m) == 1
    with pytest.raises(KeyError):
        m["z"]
    assert len(FrozenMap()) == 0
    assert repr(m) == "FrozenMap({'a': 1})"


@pytest.mark.req("2.11")
def test_copies_its_source() -> None:
    source = {"a": 1}
    m = FrozenMap(source)
    source["b"] = 2
    del source["a"]
    assert dict(m) == {"a": 1}


@pytest.mark.req("2.11")
def test_equality_and_hash() -> None:
    m = FrozenMap({"a": 1, "b": 2})
    reordered = FrozenMap({"b": 2, "a": 1})
    # Equality ignores order, as for dict, and the hash agrees with it.
    assert m == reordered
    assert m == {"b": 2, "a": 1}
    assert hash(m) == hash(reordered)
    assert m != FrozenMap({"a": 1})
    assert m != {"a": 1, "b": 3}
    assert {m: "x"}[reordered] == "x"
    with pytest.raises(TypeError):
        hash(FrozenMap({"a": [1]}))


@pytest.mark.req("2.11")
def test_values_are_covariant() -> None:
    flags: FrozenMap[str, bool] = FrozenMap({"a": True})
    numbers: FrozenMap[str, int] = flags  # checked by mypy
    assert numbers["a"] == 1


@pytest.mark.req("2.11")
@pytest.mark.parametrize(
    "clone",
    [copy.copy, copy.deepcopy, lambda m: pickle.loads(pickle.dumps(m))],
    ids=["copy", "deepcopy", "pickle"],
)
def test_copies_are_equal_frozen_maps(clone: Callable[[Any], Any]) -> None:
    m = FrozenMap({"b": (1, 2), "a": FrozenMap({"x": "y"})})
    c = clone(m)
    assert type(c) is FrozenMap
    assert c == m
    assert list(c) == ["b", "a"]


# --- modification attempts (requirement 2.11) --------------------------------------------


def _assign_item(m: Any) -> None:
    m["a"] = 9


def _delete_item(m: Any) -> None:
    del m["a"]


def _merge_in_place(m: Any) -> None:
    m |= {"z": 0}


def _add_attribute(m: Any) -> None:
    m.extra = 1


def _replace_storage(m: Any) -> None:
    m._data = {"z": 0}


def _delete_storage(m: Any) -> None:
    del m._data


def _assign_storage_item(m: Any) -> None:
    m._data["a"] = 9


@pytest.mark.req("2.11")
@pytest.mark.parametrize(
    ("attempt", "error"),
    [
        pytest.param(_assign_item, TypeError, id="item-assignment"),
        pytest.param(_delete_item, TypeError, id="item-deletion"),
        pytest.param(_merge_in_place, TypeError, id="merge-in-place"),
        pytest.param(_add_attribute, AttributeError, id="new-attribute"),
        pytest.param(_replace_storage, AttributeError, id="attribute-assignment"),
        pytest.param(_delete_storage, AttributeError, id="attribute-deletion"),
        pytest.param(_assign_storage_item, TypeError, id="storage-item-assignment"),
    ],
)
def test_modification_attempts_raise(
    attempt: Callable[[Any], None], error: type[Exception]
) -> None:
    m = FrozenMap({"a": 1, "b": 2})
    with pytest.raises(error):
        attempt(m)
    assert list(m.items()) == [("a", 1), ("b", 2)]


@pytest.mark.req("2.11")
@pytest.mark.parametrize(
    "method", ["update", "pop", "popitem", "clear", "setdefault", "__setitem__", "__delitem__"]
)
def test_has_no_mutating_methods(method: str) -> None:
    assert not hasattr(FrozenMap({"a": 1}), method)


# --- pydantic field type -----------------------------------------------------------------

Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]


class Item(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    qty: int


class Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    limits: FrozenMap[Name, int]
    items: FrozenMap[str, Item] = FrozenMap()


@pytest.mark.req("2.11")
def test_model_validates_a_dict_into_a_frozen_map() -> None:
    data = {"limits": {"b": 1, "a": 2}, "items": {"x": {"qty": 3}}}
    config = Config.model_validate(data)
    assert isinstance(config.limits, FrozenMap)
    assert list(config.limits.items()) == [("b", 1), ("a", 2)]
    assert config.items["x"] == Item(qty=3)
    assert hash(config) == hash(Config.model_validate(data))
    with pytest.raises(TypeError):
        _assign_item(config.limits)


@pytest.mark.req("2.11")
def test_model_validates_the_items_of_a_frozen_map() -> None:
    assert Config.model_validate({"limits": FrozenMap({"a": 1})}).limits == {"a": 1}
    with pytest.raises(ValidationError) as info:
        Config.model_validate({"limits": FrozenMap({"a": "1"})})
    assert [error["loc"] for error in info.value.errors()] == [("limits", "a")]


@pytest.mark.req("2.11")
def test_model_errors_locate_keys_and_values_as_for_dict() -> None:
    with pytest.raises(ValidationError) as info:
        Config.model_validate(
            {"limits": {"ok": "1", "Bad": 2}, "items": {"x": {"qty": 1, "extra": 0}}}
        )
    assert [(error["loc"], error["type"]) for error in info.value.errors()] == [
        (("limits", "ok"), "int_type"),  # strict: a string is not an int
        (("limits", "Bad", "[key]"), "string_pattern_mismatch"),
        (("items", "x", "extra"), "extra_forbidden"),
    ]
    with pytest.raises(ValidationError) as info:
        Config.model_validate({"limits": [("a", 1)]})
    assert [error["type"] for error in info.value.errors()] == ["dict_type"]


@pytest.mark.req("2.11")
def test_model_json_input_dump_and_schema() -> None:
    config = Config.model_validate_json('{"limits": {"b": 1, "a": 2}}')
    assert isinstance(config.limits, FrozenMap)
    assert list(config.limits) == ["b", "a"]
    dumped = config.model_dump()
    assert dumped == {"limits": {"b": 1, "a": 2}, "items": {}}
    assert type(dumped["limits"]) is dict
    assert config.model_dump_json() == '{"limits":{"b":1,"a":2},"items":{}}'
    properties = Config.model_json_schema()["properties"]
    assert properties["limits"] == {
        "patternProperties": {"^[a-z][a-z0-9_]*$": {"type": "integer"}},
        "title": "Limits",
        "type": "object",
    }
    assert properties["items"] == {
        "additionalProperties": {"$ref": "#/$defs/Item"},
        "default": {},
        "title": "Items",
        "type": "object",
    }
