"""erp_mock.loader.errors: the report line, the error order and pydantic loc conversion."""

import pytest
from pydantic import ValidationError

from erp_mock.config.base import Loc
from erp_mock.config.fields import EnumField
from erp_mock.loader.errors import (
    CROSS,
    ConfigError,
    ConfigErrors,
    Location,
    Step,
    StepErrors,
    config_path,
    sort_errors,
)

# The pack names in the order of instance.yaml, deliberately not the alphabetical order.
PACKS = ("purchase", "core", "inventory")


def error(
    step: Step = 5,
    pack: str | None = "purchase",
    file: str = "entities/purchase_order.yaml",
    path: str | None = "header.delivery_date.default",
    reason: str = "缺省值与字段类型 date 不符",
) -> ConfigError:
    """An error at one place; every part has a default, so a test names only what it varies."""
    return ConfigError(step, pack, (Location(file, path),), reason)


# --- the report line ---------------------------------------------------------------------


@pytest.mark.req("2.2")
def test_line_names_step_pack_file_config_path_and_reason() -> None:
    assert error().line() == (
        "[step 5] purchase: entities/purchase_order.yaml:"
        " header.delivery_date.default: 缺省值与字段类型 date 不符"
    )
    assert str(error()) == error().line()


@pytest.mark.req("2.2")
def test_line_of_an_instance_config_error_omits_the_pack() -> None:
    found = error(step=1, pack=None, file="instance.yaml", path="packs[0].version", reason="原因")
    assert found.line() == "[step 1] instance.yaml: packs[0].version: 原因"


@pytest.mark.req("2.2")
def test_line_without_a_config_path_omits_it() -> None:
    found = error(step=8, file="datasets/purchase-basic.yaml", path=None, reason="INV-1 不成立")
    assert found.line() == "[step 8] purchase: datasets/purchase-basic.yaml: INV-1 不成立"


@pytest.mark.req("2.2")
def test_line_of_an_error_with_several_places_separates_them() -> None:
    found = ConfigError(
        5,
        "purchase",
        (
            Location("entities/supplier.yaml", "id_prefix"),
            Location("bills/purchase_order.yaml", "id_prefix"),
        ),
        "ID 前缀 po 同时用于两个对象",
    )
    assert found.line() == (
        "[step 5] purchase: entities/supplier.yaml: id_prefix"
        " | bills/purchase_order.yaml: id_prefix: ID 前缀 po 同时用于两个对象"
    )


@pytest.mark.req("2.1", "2.2")
def test_line_of_a_cross_check_error_names_the_cross_step() -> None:
    found = error(step=CROSS, file="descriptions/zh-detailed.yaml", path="tools.po_create")
    assert found.line().startswith("[step cross] purchase: descriptions/zh-detailed.yaml: ")


# --- the order of the errors -------------------------------------------------------------


@pytest.mark.req("2.2")
def test_errors_sort_by_step_then_pack_order_then_file_then_config_path() -> None:
    ordered = (
        # the step first: an earlier step before a later one, the cross-check last
        error(step=4, pack=None, file="instance.yaml", path="packs"),
        # then the pack: the instance config before every pack
        error(step=5, pack=None, file="instance.yaml", path="packs"),
        # then the packs in the order of instance.yaml, not in alphabetical order
        error(step=5, pack="purchase", file="entities/a.yaml", path=None),
        error(step=5, pack="purchase", file="entities/a.yaml", path="fields.a"),
        error(step=5, pack="purchase", file="entities/b.yaml", path="fields.a"),
        error(step=5, pack="core", file="entities/a.yaml", path="fields.a"),
        error(step=5, pack="inventory", file="entities/a.yaml", path="fields.a"),
        # a pack instance.yaml does not list sorts after the listed ones
        error(step=5, pack="gl", file="entities/a.yaml", path="fields.a"),
        error(step=CROSS, pack="purchase", file="entities/a.yaml", path="fields.a"),
    )
    assert sort_errors(reversed(ordered), PACKS) == ordered
    # any other input order gives the same result
    shuffled = (*ordered[4:], *ordered[:2], *ordered[2:4])
    assert sort_errors(shuffled, PACKS) == ordered


@pytest.mark.req("2.2")
def test_reason_decides_between_errors_that_are_alike_otherwise() -> None:
    first, second = error(reason="原因 1"), error(reason="原因 2")
    assert sort_errors((second, first), PACKS) == (first, second)


@pytest.mark.req("2.2")
def test_config_errors_sorts_its_errors_and_reports_one_line_each() -> None:
    first, second = error(reason="原因 1"), error(reason="原因 2")
    raised = ConfigErrors(5, (second, first), PACKS)
    assert raised.step == 5
    assert raised.errors == (first, second)
    assert raised.packs == PACKS
    assert raised.lines() == (first.line(), second.line())
    assert str(raised) == f"{first.line()}\n{second.line()}"


@pytest.mark.req("2.2")
def test_config_errors_needs_at_least_one_error() -> None:
    with pytest.raises(ValueError, match="at least one error"):
        ConfigErrors(5, ())


# --- pydantic locs as config paths -------------------------------------------------------


@pytest.mark.req("2.2")
@pytest.mark.parametrize(
    ("loc", "expected"),
    [
        (("header", "delivery_date", "default"), "header.delivery_date.default"),
        (("packs", 0, "version"), "packs[0].version"),
        (("lines", 2, "qty"), "lines[2].qty"),
        (("values",), "values"),
        (("values", 0), "values[0]"),
        # the marker of an error about a mapping key: the key is already the last part
        (("values", "Bad-Code", "[key]"), "values.Bad-Code"),
        (
            ("entities", "item", "fields", "code", "maxLength"),
            "entities.item.fields.code.maxLength",
        ),
        # an error about the document as a whole has no config path
        ((), None),
    ],
)
def test_config_path_of_a_pydantic_loc(loc: Loc, expected: str | None) -> None:
    assert config_path(loc) == expected


@pytest.mark.req("2.2")
def test_model_errors_are_recorded_one_per_line_error_at_its_config_path() -> None:
    with pytest.raises(ValidationError) as caught:
        EnumField.model_validate({"type": "enum", "values": {"Bad-Code": {"zh": "名称"}}})
    errors = StepErrors(4)
    errors.add_model_errors("purchase", "entities/purchase_order.yaml", caught.value)
    assert [found.locations for found in errors.errors] == [
        (Location("entities/purchase_order.yaml", "values.Bad-Code"),),
        (Location("entities/purchase_order.yaml", "values.Bad-Code.en"),),
    ]
    assert [found.pack for found in errors.errors] == ["purchase", "purchase"]
    assert errors.errors[1].reason == "Field required"


# --- the collector of one step -----------------------------------------------------------


@pytest.mark.req("2.2")
def test_collector_keeps_every_error_of_the_step_and_raises_them_sorted() -> None:
    errors = StepErrors(5)
    assert not errors
    errors.add_at("core", "pack.yaml", None, "原因")
    errors.add_at(None, "instance.yaml", "packs", "原因")
    errors.add(
        "inventory",
        (Location("entities/a.yaml", "fields.a"), Location("entities/b.yaml", "fields.b")),
        "原因",
    )
    errors.extend((error(pack="purchase"),))
    assert len(errors) == 4
    with pytest.raises(ConfigErrors) as caught:
        errors.raise_if_any(PACKS)
    assert [found.pack for found in caught.value.errors] == [
        None,
        "purchase",
        "core",
        "inventory",
    ]
    assert {found.step for found in caught.value.errors} == {5}


@pytest.mark.req("2.2")
def test_collector_of_a_step_without_errors_raises_nothing() -> None:
    StepErrors(1).raise_if_any(PACKS)
