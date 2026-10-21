"""erp_mock.config.fields: field declarations and the locations of their errors.

Declarations are written in YAML and read with loader.yaml_io, as the loader reads them, so
``0.13`` is a float and ``2026-10-09`` a string. Every error is asserted with its pydantic
``loc``, the config path the loader reports (``("header", "reason", "default")`` is
``header.reason.default``).
"""

import textwrap
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.base import FrozenModel, Label, Loc, validation_error
from erp_mock.config.fields import (
    DEFAULT_MAX_LENGTH,
    FIELD_TYPES,
    BoolField,
    DateField,
    DecimalField,
    EnumField,
    FieldMap,
    IntField,
    PeriodField,
    RefField,
    StandardOverride,
    StringField,
    TypedField,
    TypedFieldMap,
    field_name_errors,
)
from erp_mock.loader.yaml_io import YamlLoadError, parse_yaml

FILE = Path("entities/sample.yaml")


class Entity(FrozenModel):
    """The fields of an entity, declared as the entity model declares them."""

    fields: TypedFieldMap


class Bill(FrozenModel):
    """The header and lines of a bill, declared as the bill model declares them."""

    header: FieldMap = FrozenMap()
    lines: FieldMap = FrozenMap()


def parse(text: str) -> Any:
    return parse_yaml(textwrap.dedent(text), FILE)


def entity(text: str) -> Entity:
    return Entity.model_validate(parse(text))


def bill(text: str) -> Bill:
    return Bill.model_validate(parse(text))


def errors(model: type[FrozenModel], text: str) -> list[tuple[Loc, str]]:
    """The ``(loc, type)`` of every error of the YAML document ``text`` as a ``model``."""
    with pytest.raises(ValidationError) as caught:
        model.model_validate(parse(text))
    return [(error["loc"], error["type"]) for error in caught.value.errors()]


def field_errors(decl: str, name: str = "x") -> list[tuple[Loc, str]]:
    """Errors of an entity whose only field ``name`` is declared as ``decl`` (flow YAML)."""
    return errors(Entity, f"fields:\n  {name}: {decl}\n")


def field(decl: str, name: str = "x") -> TypedField:
    return entity(f"fields:\n  {name}: {decl}\n").fields[name]


# --- accepted declarations ---------------------------------------------------------------


@pytest.mark.req("1.2")
def test_field_types_are_the_twelve_of_the_meta_model() -> None:
    assert FIELD_TYPES == (
        "string",
        "text",
        "int",
        "bool",
        "enum",
        "date",
        "period",
        "qty",
        "price",
        "money",
        "rate",
        "ref",
    )


MINIMAL_DECLARATIONS = {
    "string": ("{ type: string }", StringField),
    "text": ("{ type: text }", StringField),
    "int": ("{ type: int }", IntField),
    "bool": ("{ type: bool }", BoolField),
    "enum": ("{ type: enum, values: { a: { zh: 甲, en: A } } }", EnumField),
    "date": ("{ type: date }", DateField),
    "period": ("{ type: period }", PeriodField),
    "qty": ("{ type: qty }", DecimalField),
    "price": ("{ type: price }", DecimalField),
    "money": ("{ type: money }", DecimalField),
    "rate": ("{ type: rate }", DecimalField),
    "ref": ("{ type: ref, ref: supplier }", RefField),
}


@pytest.mark.req("1.2")
@pytest.mark.parametrize("field_type", FIELD_TYPES)
def test_type_selects_the_model(field_type: str) -> None:
    decl, model = MINIMAL_DECLARATIONS[field_type]
    parsed = field(decl, name="x_id")
    assert type(parsed) is model
    assert parsed.type == field_type
    assert (parsed.required, parsed.readonly, parsed.unique) == (False, False, False)
    assert parsed.label is None
    assert parsed.default is None


@pytest.mark.req("1.2")
def test_meta_model_entity_example() -> None:
    supplier = entity(
        """
        fields:
          name:              { type: string, label: { zh: 名称 }, required: true }
          short_name:        { type: string, label: { zh: 简称 } }
          payment_term_days: { type: int, label: { zh: 账期天数 }, default: 30 }
          default_tax_rate:  { type: rate, label: { zh: 默认税率 }, default: "0.13" }
        """
    )
    assert list(supplier.fields) == ["name", "short_name", "payment_term_days", "default_tax_rate"]
    name = supplier.fields["name"]
    assert isinstance(name, StringField)
    assert name.label == Label(zh="名称")
    assert name.required
    term = supplier.fields["payment_term_days"]
    assert isinstance(term, IntField)
    assert term.default == 30
    rate = supplier.fields["default_tax_rate"]
    assert isinstance(rate, DecimalField)
    assert (rate.type, rate.default, rate.places) == ("rate", "0.13", 4)


@pytest.mark.req("1.2", "1.7")
def test_design_entity_examples() -> None:
    org = entity(
        """
        fields:
          code:       { type: string, required: true, unique: true }
          name:       { type: string, required: true }
          is_default: { type: bool, default: false }
        """
    )
    assert org.fields["code"].unique
    is_default = org.fields["is_default"]
    assert isinstance(is_default, BoolField)
    assert is_default.default is False
    material = entity(
        """
        fields:
          name:             { type: string, required: true }
          base_unit_id:     { type: ref, ref: unit, required: true }
          purchase_unit_id: { type: ref, ref: unit, default_from: base_unit.id }
          tax_rate:         { type: rate, values: ["0", "0.06", "0.09", "0.13"], default: "0.13" }
        """
    )
    purchase_unit = material.fields["purchase_unit_id"]
    assert isinstance(purchase_unit, RefField)
    assert (purchase_unit.ref, purchase_unit.default_from) == ("unit", "base_unit.id")
    tax_rate = material.fields["tax_rate"]
    assert isinstance(tax_rate, DecimalField)
    assert tax_rate.values == ("0", "0.06", "0.09", "0.13")  # the YAML list, as a tuple


@pytest.mark.req("1.2", "1.5", "1.13")
def test_bill_examples() -> None:
    purchase_order = bill(
        """
        header:
          supplier_id:   { type: ref, ref: supplier, required: true }
          delivery_date: { type: date, label: { zh: 交货日期 }, required: true }
        lines:
          material_id: { type: ref, ref: material, required: true }
          unit_id:     { type: ref, ref: unit, default_from: material.purchase_unit_id }
          tax_rate:    { default_from: material.tax_rate }
        """
    )
    assert isinstance(purchase_order.header["delivery_date"], DateField)
    assert list(purchase_order.lines) == ["material_id", "unit_id", "tax_rate"]
    tax_rate = purchase_order.lines["tax_rate"]
    assert isinstance(tax_rate, StandardOverride)
    assert tax_rate.default_from == "material.tax_rate"
    stock_adjustment = bill(
        """
        header:
          warehouse_id: { type: ref, ref: warehouse, required: true }
          reason:
            type: enum
            required: true
            values: { gain: { zh: 盘盈, en: Gain }, loss: { zh: 盘亏, en: Loss } }
        lines:
          qty: { type: qty, required: true }
        """
    )
    reason = stock_adjustment.header["reason"]
    assert isinstance(reason, EnumField)
    assert isinstance(reason.values, FrozenMap)
    assert list(reason.values) == ["gain", "loss"]
    assert (reason.values["loss"].zh, reason.values["loss"].en) == ("盘亏", "Loss")


@pytest.mark.req("1.2")
def test_string_max_length() -> None:
    assert DEFAULT_MAX_LENGTH == {"string": 100, "text": 500}
    string = field("{ type: string }")
    text = field("{ type: text }")
    short = field("{ type: string, maxLength: 20, default: '12345678901234567890' }")
    assert isinstance(string, StringField)
    assert isinstance(text, StringField)
    assert isinstance(short, StringField)
    assert (string.max_length, string.effective_max_length) == (None, 100)
    assert (text.max_length, text.effective_max_length) == (None, 500)
    assert (short.max_length, short.effective_max_length) == (20, 20)
    assert short.model_dump(by_alias=True)["maxLength"] == 20


@pytest.mark.req("1.2", "1.11")
@pytest.mark.parametrize(
    ("decl", "expected"),
    [
        ("{ type: date, default: '2028-02-29', default_context: today }", "2028-02-29"),
        ("{ type: period, default: '2026-12' }", "2026-12"),
        ("{ type: enum, values: { a: { zh: 甲, en: A } }, default: a }", "a"),
        ("{ type: money, values: ['0.5', '1.00'], default: '1' }", "1"),
        ("{ type: rate, values: ['0.13'], default: '0.1300' }", "0.1300"),
        ("{ type: price, default: '-0.000001' }", "-0.000001"),
        ("{ type: text, default: '' }", ""),
        ("{ type: bool, default: true }", True),
        ("{ type: int, default: -5 }", -5),
    ],
)
def test_defaults_of_the_field_type_are_accepted(decl: str, expected: object) -> None:
    parsed = field(decl)
    assert parsed.default == expected
    assert type(parsed.default) is type(expected)


@pytest.mark.req("1.2")
def test_ref_default_context() -> None:
    org = field("{ type: ref, ref: org, default_context: org }", name="org_id")
    user = field("{ type: ref, ref: employee, default_context: user }", name="buyer_id")
    assert isinstance(org, RefField)
    assert isinstance(user, RefField)
    assert (org.default_context, user.default_context) == ("org", "user")


def override(decl: object) -> StandardOverride:
    assert type(decl) is StandardOverride
    return decl


@pytest.mark.req("1.5")
def test_declaration_without_type_in_a_bill_is_a_standard_override() -> None:
    parsed = bill(
        """
        header:
          biz_date:           { default_context: today }
          price_includes_tax: { label: { zh: 含税, en: Tax included }, default: false }
          remark:             { default: "" }
        lines:
          qty:      { default: "1" }
          price:    { default_from: material.purchase_price }
          tax_rate: { default_from: material.tax_rate }
        """
    )
    assert list(parsed.header) == ["biz_date", "price_includes_tax", "remark"]
    assert override(parsed.header["biz_date"]).default_context == "today"
    price_includes_tax = override(parsed.header["price_includes_tax"])
    assert price_includes_tax.default is False
    assert price_includes_tax.label == Label(zh="含税", en="Tax included")
    assert override(parsed.header["remark"]).default == ""
    assert override(parsed.lines["qty"]).default == "1"
    assert override(parsed.lines["price"]).default_from == "material.purchase_price"
    assert override(parsed.lines["tax_rate"]).default_from == "material.tax_rate"
    # Step 5 sees which attributes a same-name declaration states.
    assert price_includes_tax.model_fields_set == {"label", "default"}


@pytest.mark.req("1.5")
def test_standard_override_keeps_other_field_attributes_for_step_5() -> None:
    qty = override(bill("lines:\n  qty: { required: true, readonly: false }\n").lines["qty"])
    assert qty.model_fields_set == {"required", "readonly"}
    assert (qty.required, qty.readonly, qty.unique) == (True, False, False)


# --- type and keys -----------------------------------------------------------------------


@pytest.mark.req("1.2", "1.11")
@pytest.mark.parametrize(
    "decl", ["{ type: float }", "{ type: String }", "{ type: 1 }", "{ type: }"]
)
def test_unknown_type_is_located_at_type(decl: str) -> None:
    assert field_errors(decl) == [(("fields", "x", "type"), "literal_error")]


@pytest.mark.req("1.11")
def test_entity_field_requires_type() -> None:
    assert field_errors("{ label: { zh: 名称 } }") == [(("fields", "x", "type"), "missing")]
    assert field_errors("{ default_from: material.tax_rate }") == [
        (("fields", "x", "type"), "missing")
    ]


@pytest.mark.req("1.11")
@pytest.mark.parametrize("decl", ["string", "[type, string]", "~"])
def test_declaration_must_be_a_mapping(decl: str) -> None:
    assert field_errors(decl) == [(("fields", "x"), "dict_type")]


@pytest.mark.req("1.2", "1.11")
@pytest.mark.parametrize(
    ("decl", "key"),
    [
        ("{ type: string, size: 10 }", "size"),
        ("{ type: string, max_length: 10 }", "max_length"),  # the key is maxLength
        ("{ type: int, maxLength: 10 }", "maxLength"),
        ("{ type: qty, maxLength: 10 }", "maxLength"),
        ("{ type: date, values: ['2026-01-01'] }", "values"),
        ("{ type: string, ref: supplier }", "ref"),
        ("{ type: int, default_context: today }", "default_context"),
        ("{ type: period, default_context: today }", "default_context"),
        ("{ type: bool, nullable: true }", "nullable"),
    ],
)
def test_undefined_key_is_located_at_the_key(decl: str, key: str) -> None:
    assert field_errors(decl) == [(("fields", "x", key), "extra_forbidden")]


@pytest.mark.req("1.11")
@pytest.mark.parametrize("name", ["Name", "1st", "short-name", "_x", "名称"])
def test_field_name_must_match_the_name_pattern(name: str) -> None:
    assert field_errors("{ type: string }", name=name) == [
        (("fields", name, "[key]"), "string_pattern_mismatch")
    ]


@pytest.mark.req("1.11")
@pytest.mark.parametrize(
    ("decl", "key", "error_type"),
    [
        ("{ type: string, required: 'yes' }", "required", "bool_type"),
        ("{ type: string, readonly: 1 }", "readonly", "bool_type"),
        ("{ type: string, unique: null }", "unique", "bool_type"),
        ("{ type: string, label: 名称 }", "label", "model_type"),
        ("{ type: string, maxLength: 0 }", "maxLength", "greater_than_equal"),
        ("{ type: string, maxLength: '20' }", "maxLength", "int_type"),
        ("{ type: string, default_from: material }", "default_from", "string_pattern_mismatch"),
        ("{ type: string, default_from: a.b.c }", "default_from", "string_pattern_mismatch"),
        (
            "{ type: string, default_from: Material.name }",
            "default_from",
            "string_pattern_mismatch",
        ),
    ],
)
def test_attribute_value_is_located_at_the_attribute(decl: str, key: str, error_type: str) -> None:
    assert field_errors(decl) == [(("fields", "x", key), error_type)]


@pytest.mark.req("1.11", "1.13")
def test_label_errors_are_located_inside_the_label() -> None:
    assert field_errors("{ type: string, label: { en: Name } }") == [
        (("fields", "x", "label", "zh"), "missing")
    ]
    assert field_errors("{ type: string, label: { zh: '', fr: Nom } }") == [
        (("fields", "x", "label", "zh"), "string_too_short"),
        (("fields", "x", "label", "fr"), "extra_forbidden"),
    ]


@pytest.mark.req("1.11")
def test_every_error_of_every_field_is_reported() -> None:
    assert errors(
        Entity,
        """
        fields:
          a: { type: int, default: "1", extra: 0 }
          b: { type: date, default: "2026-02-30" }
          c: { type: ref }
        """,
    ) == [
        (("fields", "a", "default"), "int_type"),
        (("fields", "a", "extra"), "extra_forbidden"),
        (("fields", "b", "default"), "date_invalid"),
        (("fields", "c", "ref"), "missing"),
    ]


# --- default ------------------------------------------------------------------------------


@pytest.mark.req("1.11")
@pytest.mark.parametrize(
    ("decl", "error_type"),
    [
        ("{ type: string, default: 1 }", "string_type"),
        ("{ type: string, maxLength: 3, default: abcd }", "string_too_long"),
        ("{ type: text, maxLength: 2, default: 中文字 }", "string_too_long"),
        ("{ type: int, default: '30' }", "int_type"),
        ("{ type: int, default: true }", "int_type"),
        ("{ type: int, default: 1.5 }", "int_type"),
        ("{ type: bool, default: 'true' }", "bool_type"),
        ("{ type: bool, default: yes }", "bool_type"),  # YAML 1.2: yes is a string
        ("{ type: bool, default: 1 }", "bool_type"),
        ("{ type: enum, values: { a: { zh: 甲, en: A } }, default: b }", "default_not_in_values"),
        ("{ type: enum, values: { a: { zh: 甲, en: A } }, default: A }", "string_pattern_mismatch"),
        ("{ type: date, default: 2026/10/09 }", "string_pattern_mismatch"),
        ("{ type: date, default: 20261009 }", "string_type"),
        ("{ type: date, default: today }", "string_pattern_mismatch"),
        ("{ type: date, default: today+3d }", "string_pattern_mismatch"),
        ("{ type: date, default: '2026-02-29' }", "date_invalid"),
        ("{ type: date, default: '2026-13-01' }", "date_invalid"),
        ("{ type: date, default: '0000-01-01' }", "date_invalid"),
        ("{ type: period, default: '2026-13' }", "string_pattern_mismatch"),
        ("{ type: period, default: '2026-1' }", "string_pattern_mismatch"),
        ("{ type: period, default: '2026-10-01' }", "string_pattern_mismatch"),
        ("{ type: qty, default: 1 }", "string_type"),
        ("{ type: rate, default: 0.13 }", "string_type"),  # a YAML float
        ("{ type: price, default: 1e3 }", "string_type"),
        ("{ type: price, default: '1e3' }", "string_pattern_mismatch"),
        ("{ type: money, default: '1,000' }", "string_pattern_mismatch"),
        ("{ type: money, default: ' 5' }", "string_pattern_mismatch"),
        ("{ type: money, default: '1.005' }", "decimal_places"),
        ("{ type: rate, default: '0.13001' }", "decimal_places"),
        ("{ type: rate, values: ['0', '0.13'], default: '0.06' }", "default_not_in_values"),
        ("{ type: ref, ref: org, default: 1 }", "string_type"),
    ],
)
def test_default_must_fit_the_field_type(decl: str, error_type: str) -> None:
    assert field_errors(decl, name="x_id") == [(("fields", "x_id", "default"), error_type)]


@pytest.mark.req("1.11")
@pytest.mark.parametrize(
    "decl",
    [
        "{ type: date, default_context: org }",
        "{ type: date, default_context: now }",
        "{ type: ref, ref: org, default_context: today }",
        "{ type: ref, ref: org, default_context: company }",
    ],
)
def test_default_context_must_fit_the_field_type(decl: str) -> None:
    assert field_errors(decl, name="x_id") == [
        (("fields", "x_id", "default_context"), "literal_error")
    ]


@pytest.mark.req("1.11")
def test_default_errors_name_the_limit() -> None:
    with pytest.raises(ValidationError) as caught:
        entity("fields:\n  x: { type: money, default: '1.005' }\n")
    (error,) = caught.value.errors()
    assert error["ctx"] == {"value": "1.005", "type": "money", "places": 2}
    assert error["msg"] == "1.005 的小数位数超过 money 字段的精度 2 位"
    with pytest.raises(ValidationError) as caught:
        entity("fields:\n  x: { type: string, default: '" + "长" * 101 + "' }\n")
    (error,) = caught.value.errors()
    assert (error["type"], error["ctx"]) == ("string_too_long", {"max_length": 100})


# --- allowed values of qty, price, money and rate (requirement 1.7) -----------------------


@pytest.mark.req("1.7")
@pytest.mark.parametrize(
    ("field_type", "longest", "too_long"),
    [
        ("qty", "1.0001", "1.00001"),
        ("price", "0.000001", "0.0000001"),
        ("money", "-12.34", "12.345"),
        ("rate", "0.1234", "0.12345"),
    ],
)
def test_allowed_values_have_at_most_the_decimals_of_the_type(
    field_type: str, longest: str, too_long: str
) -> None:
    parsed = field(f"{{ type: {field_type}, values: ['0', '{longest}'] }}")
    assert isinstance(parsed, DecimalField)
    assert parsed.values == ("0", longest)
    assert field_errors(f"{{ type: {field_type}, values: ['0', '{longest}', '{too_long}'] }}") == [
        (("fields", "x", "values", 2), "decimal_places")
    ]


@pytest.mark.req("1.7")
@pytest.mark.parametrize(
    ("decl", "loc", "error_type"),
    [
        ("{ type: rate, values: [] }", ("values",), "too_short"),
        ("{ type: rate, values: '0.13' }", ("values",), "tuple_type"),
        ("{ type: rate, values: { a: '0.13' } }", ("values",), "tuple_type"),
        ("{ type: rate, values: ['0', 0.13] }", ("values", 1), "string_type"),
        ("{ type: rate, values: ['0', 13] }", ("values", 1), "string_type"),
        ("{ type: rate, values: ['0', '.13'] }", ("values", 1), "string_pattern_mismatch"),
        ("{ type: rate, values: [x] }", ("values", 0), "string_pattern_mismatch"),
    ],
)
def test_allowed_values_errors(decl: str, loc: Loc, error_type: str) -> None:
    assert field_errors(decl) == [(("fields", "x", *loc), error_type)]


@pytest.mark.req("1.7")
def test_allowed_values_are_optional() -> None:
    for decl in ("{ type: rate }", "{ type: rate, values: null }"):
        parsed = field(decl)
        assert isinstance(parsed, DecimalField)
        assert parsed.values is None


@pytest.mark.req("1.7")
def test_default_is_compared_with_allowed_values_by_value() -> None:
    parsed = field("{ type: rate, values: ['0', '0.13'], default: '0.130' }")
    assert parsed.default == "0.130"
    # Both the decimals and the membership of the default are checked.
    assert field_errors("{ type: money, values: ['0.001', '1'], default: '0.001' }") == [
        (("fields", "x", "values", 0), "decimal_places"),
        (("fields", "x", "default"), "decimal_places"),
    ]


# --- enum values (requirement 1.13) ------------------------------------------------------


@pytest.mark.req("1.13")
@pytest.mark.parametrize(
    ("decl", "loc", "error_type"),
    [
        ("{ type: enum }", ("values",), "missing"),
        ("{ type: enum, values: {} }", ("values",), "too_short"),
        ("{ type: enum, values: [gain, loss] }", ("values",), "dict_type"),
        ("{ type: enum, values: { Gain: { zh: 盘盈, en: Gain } } }", ("values", "Gain", "[key]"), "string_pattern_mismatch"),
        ("{ type: enum, values: { 1st: { zh: 一, en: One } } }", ("values", "1st", "[key]"), "string_pattern_mismatch"),
        ("{ type: enum, values: { 1: { zh: 一, en: One } } }", ("values", 1, "[key]"), "string_type"),
        ("{ type: enum, values: { gain: { zh: 盘盈 } } }", ("values", "gain", "en"), "missing"),
        ("{ type: enum, values: { gain: { en: Gain } } }", ("values", "gain", "zh"), "missing"),
        ("{ type: enum, values: { gain: { zh: 盘盈, en: '' } } }", ("values", "gain", "en"), "string_too_short"),
        ("{ type: enum, values: { gain: { zh: 盘盈, en: Gain, ja: x } } }", ("values", "gain", "ja"), "extra_forbidden"),
        ("{ type: enum, values: { gain: 盘盈 } }", ("values", "gain"), "model_type"),
    ],
)  # fmt: skip
def test_enum_values_errors(decl: str, loc: Loc, error_type: str) -> None:
    assert field_errors(decl) == [(("fields", "x", *loc), error_type)]


@pytest.mark.req("1.13")
def test_repeated_enum_code_is_a_yaml_error_at_its_line() -> None:
    text = textwrap.dedent(
        """\
        fields:
          reason:
            type: enum
            values:
              gain: { zh: 盘盈, en: Gain }
              loss: { zh: 盘亏, en: Loss }
              gain: { zh: 盘盈, en: Gain }
        """
    )
    with pytest.raises(YamlLoadError) as caught:
        parse_yaml(text, FILE)
    assert caught.value.config_path == "line 7"


# --- names of ref fields (requirement 1.12) ----------------------------------------------


@pytest.mark.req("1.12")
def test_ref_requires_its_target() -> None:
    assert field_errors("{ type: ref }", name="supplier_id") == [
        (("fields", "supplier_id", "ref"), "missing")
    ]
    assert field_errors("{ type: ref, ref: Supplier }", name="supplier_id") == [
        (("fields", "supplier_id", "ref"), "string_pattern_mismatch")
    ]


@pytest.mark.req("1.12")
@pytest.mark.parametrize("name", ["supplier", "supplier_ref", "supplierid", "id", "supplier_id_x"])
def test_ref_name_must_end_with_id(name: str) -> None:
    assert field_errors("{ type: ref, ref: supplier }", name=name) == [
        (("fields", name), "ref_name_suffix")
    ]


@pytest.mark.req("1.12")
def test_ref_name_without_id_must_not_name_another_field() -> None:
    assert errors(
        Entity,
        """
        fields:
          supplier:      { type: string }
          supplier_id:   { type: ref, ref: supplier }
          material_id:   { type: ref, ref: material }
          unit_id_id:    { type: ref, ref: unit }
          unit_id:       { type: ref, ref: unit }
        """,
    ) == [
        (("fields", "supplier_id"), "ref_name_conflict"),
        (("fields", "unit_id_id"), "ref_name_conflict"),
    ]
    assert errors(
        Bill,
        """
        header:
          warehouse_id: { type: ref, ref: warehouse }
        lines:
          material_id: { type: ref, ref: material }
          material:    { type: string }
          warehouse:   { type: string }
        """,
    ) == [(("lines", "material_id"), "ref_name_conflict")]


@pytest.mark.req("1.12")
def test_fields_ending_with_id_that_are_not_refs_are_not_checked() -> None:
    parsed = entity(
        """
        fields:
          tax_id:        { type: string }
          tax:           { type: money }
          supplier_id:   { type: ref, ref: supplier }
          supplier_name: { type: string }
        """
    )
    assert list(parsed.fields) == ["tax_id", "tax", "supplier_id", "supplier_name"]


@pytest.mark.req("1.12")
def test_ref_name_conflict_message_names_the_key() -> None:
    with pytest.raises(ValidationError) as caught:
        entity("fields:\n  unit: { type: string }\n  unit_id: { type: ref, ref: unit }\n")
    (error,) = caught.value.errors()
    assert error["loc"] == ("fields", "unit_id")
    assert error["ctx"] == {"key": "unit"}
    assert "unit" in error["msg"]


@pytest.mark.req("1.12")
def test_name_check_with_standard_fields() -> None:
    parsed = bill(
        """
        header:
          status_id:      { type: ref, ref: bill_status }
          org:            { type: string }
          source_bill_id: { label: { zh: 来源单据 } }
          source_bill:    { type: string }
          number:         { type: string }
          supplier_id:    { type: ref, ref: supplier }
        """
    )
    # The bill model knows the standard fields of its header: True marks those that
    # results return without _id.
    standard = {"number": False, "status": False, "org_id": True, "source_bill_id": True}
    found = field_name_errors(parsed.header, loc=("header",), standard=standard)
    assert [(error["loc"], error["type"]) for error in validation_error(found).errors()] == [
        (("header", "status_id"), "ref_name_conflict"),
        (("header", "source_bill_id"), "ref_name_conflict"),
        (("header", "org"), "ref_name_conflict"),
    ]
    assert field_name_errors(parsed.header) == []


@pytest.mark.req("1.12")
def test_name_errors_are_located_at_the_fields_in_declaration_order() -> None:
    assert errors(
        Bill,
        """
        lines:
          material_id: { type: ref, ref: material }
          material:    { type: string }
          unit:        { type: ref, ref: unit }
        """,
    ) == [
        (("lines", "material_id"), "ref_name_conflict"),
        (("lines", "unit"), "ref_name_suffix"),
    ]


# --- declarations without type (requirement 1.5) -----------------------------------------


@pytest.mark.req("1.5", "1.11")
@pytest.mark.parametrize(
    ("decl", "key", "error_type"),
    [
        ("{ default: 0.13 }", "default", "scalar_type"),
        ("{ default: [a] }", "default", "scalar_type"),
        ("{ default_context: now }", "default_context", "literal_error"),
        ("{ default_from: tax_rate }", "default_from", "string_pattern_mismatch"),
        ("{ values: ['0.13'] }", "values", "extra_forbidden"),
        ("{ ref: unit }", "ref", "extra_forbidden"),
        ("{ maxLength: 300 }", "maxLength", "extra_forbidden"),
    ],
)
def test_standard_override_errors(decl: str, key: str, error_type: str) -> None:
    assert errors(Bill, f"lines:\n  tax_rate: {decl}\n") == [
        (("lines", "tax_rate", key), error_type)
    ]


@pytest.mark.req("1.5", "1.11")
def test_typed_declarations_in_a_bill_still_require_a_known_type() -> None:
    assert errors(Bill, "header:\n  remark: { type: memo }\n") == [
        (("header", "remark", "type"), "literal_error")
    ]
    assert errors(Bill, "header:\n  remark: string\n") == [(("header", "remark"), "dict_type")]


# --- JSON Schema -------------------------------------------------------------------------


@pytest.mark.req("1.2", "1.7", "1.13")
def test_json_schema_matches_the_models() -> None:
    schema = Bill.model_json_schema(mode="validation")
    header = schema["properties"]["header"]
    assert header["additionalProperties"] is False  # field names other than Name
    choices = header["patternProperties"]["^[a-z][a-z0-9_]*$"]["anyOf"]
    assert [choice["$ref"].removeprefix("#/$defs/") for choice in choices] == [
        "StringField",
        "IntField",
        "BoolField",
        "EnumField",
        "DateField",
        "PeriodField",
        "DecimalField",
        "RefField",
        "StandardOverride",
    ]
    defs = schema["$defs"]
    assert "type" not in defs["StandardOverride"]["properties"]
    assert defs["StringField"]["properties"]["maxLength"]["anyOf"][0] == {
        "minimum": 1,
        "type": "integer",
    }
    enum_values = defs["EnumField"]["properties"]["values"]
    assert (enum_values["minProperties"], enum_values["additionalProperties"]) == (1, False)
    assert defs["DecimalField"]["properties"]["values"]["anyOf"][0]["minItems"] == 1
    assert defs["DateField"]["properties"]["default"]["anyOf"][0]["format"] == "date"
    entity_choices = Entity.model_json_schema()["properties"]["fields"]["patternProperties"]
    assert len(entity_choices["^[a-z][a-z0-9_]*$"]["anyOf"]) == 8  # no StandardOverride


@pytest.mark.req("1.11")
def test_validation_error_needs_errors() -> None:
    with pytest.raises(ValueError, match="at least one error"):
        validation_error([])
