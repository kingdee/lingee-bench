"""erp_mock.loader.step5_fields: the fields, the linkages and the order of the defaults.

One example per kind of error the module reports, each on a tree written below ``tmp_path``
by tests.helpers.tree that holds exactly the one thing that is wrong, asserted by what the
loader reports: the places - the (pack, file, config path) of every location of the error -
and the reason. An error that involves two declarations, as a base unit of another entity
than the lines' unit does, lists both places in one error (requirement 2.2).

The trees run through :class:`~erp_mock.loader.loader.Loader`, the way loading runs step 5,
so steps 1 to 4 pass first and the names of the documents resolve
(``loader.step5_model``); :data:`BASE_FILES` holds what a sound quantity semantics needs -
the measured entity with its base unit and its conversion records, the unit entity and a
warehouse - together with the two context entities, and :func:`measured_bill` and
:func:`linkage_doc` write a bill and a linkage that fit them. A test replaces the one item
it is about, so what it writes is the only error that comes out.

The order the ``default_from`` defaults are applied in is read off the model the step
returns: the engine test fixture for what a real configuration produces, and trees for a
chain and for the cycles the step rejects.
"""

import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from erp_mock.loader.errors import ConfigError, ConfigErrors
from erp_mock.loader.loader import Loader
from tests.helpers.fixtures import fixture_domain
from tests.helpers.tree import (
    CONTEXT_FILES,
    bill_doc,
    dataset_doc,
    document,
    entity_doc,
    profile_doc,
    write_instance,
    write_pack,
)

INSTANCE_DIR = "instance"
PACKS_DIR = "packs"

# The pack the default instance.yaml of tests.helpers.tree references.
PACK = "core"

# One place of a reported error: the pack, the file and the config path.
type Place = tuple[str | None, str, str | None]

# The definitions the default instance.yaml names, both valid.
DEFINITIONS: Final[Mapping[str, str]] = {
    "profiles/semantic.yaml": profile_doc(),
    "datasets/purchase-basic.yaml": dataset_doc(),
}


def doc(text: str) -> str:
    """A YAML document written indented in the test source."""
    return textwrap.dedent(text).lstrip("\n")


def block(text: str) -> str:
    """A mapping written in the test source, as the value of a key of a document.

    ``entity_doc(fields=block("a: {...}\\nb: {...}"))`` writes the fields as an indented
    block below ``fields:``, which is how a document with more than one field reads.
    """
    return "\n" + textwrap.indent(doc(text), "  ").rstrip("\n")


# --- the tree a sound declaration needs --------------------------------------------------

# The measured entity, the unit entity, the conversion records and the warehouse of the
# linkages (requirements 6.8 and 9.1). The measured entity declares its base unit and the
# entity that holds one record per alternative unit (requirement 6.12).
MATERIAL_FIELDS: Final = (
    "{ name: { type: string, required: true }, unit_id: { type: ref, ref: unit, required: true } }"
)
UNIT_CONVERSION: Final = (
    "{ base_unit_field: unit_id, entity: material_unit, material_field: material_id,"
    " unit_field: unit_id, factor_field: factor }"
)
CONVERSION_FIELDS: Final = (
    "{ material_id: { type: ref, ref: material, required: true },"
    " unit_id: { type: ref, ref: unit, required: true },"
    " factor: { type: qty, required: true } }"
)
BASE_FILES: Final[Mapping[str, str]] = {
    **CONTEXT_FILES,
    "entities/material.yaml": entity_doc(
        entity="material", id_prefix="mat", fields=MATERIAL_FIELDS, unit_conversion=UNIT_CONVERSION
    ),
    "entities/material_unit.yaml": entity_doc(
        entity="material_unit", id_prefix="mu", fields=CONVERSION_FIELDS
    ),
    "entities/unit.yaml": entity_doc(entity="unit", id_prefix="unt"),
    "entities/warehouse.yaml": entity_doc(entity="warehouse", id_prefix="wh"),
}

# The ID prefixes of BASE_FILES, of the bills of this module and of the context entities.
IDS: Final = "[mat, mu, unt, wh, po, orgu, emp]"

# The lines of a bill that declares quantity semantics: the material, the unit it is
# measured in and the quantity (requirement 1.4).
MEASURED_LINES: Final = (
    "{ material_id: { type: ref, ref: material, required: true },"
    " unit_id: { type: ref, ref: unit },"
    " move_qty: { type: qty, required: true } }"
)
MEASURED_QUANTITY: Final = (
    "{ material_field: material_id, unit_field: unit_id, qty_field: move_qty }"
)

# The required keys of an inventory linkage document (meta-model.md section 8), written for
# the bill :func:`measured_bill` produces.
LINKAGE_KEYS: Final[Mapping[str, str]] = {
    "id": "order_stock",
    "bill": "purchase_order",
    "primitive": "inventory",
    "on": "approve",
    "direction": "in",
    "warehouse_field": "header.depot_id",
    "qty_field": "line.qty_base",
}


def measured_bill(**overrides: str | None) -> str:
    """A bill whose lines declare quantity semantics, with ``overrides`` applied.

    Its header holds the warehouse field of a linkage and its lines the material, the unit
    and the quantity; without pricing, so the quantity is the declared field.
    """
    keys = {
        "header": "{ depot_id: { type: ref, ref: warehouse, required: true } }",
        "lines": MEASURED_LINES,
        "line_quantity": MEASURED_QUANTITY,
    }
    return bill_doc(**{**keys, **overrides})


def linkage_doc(**overrides: str | None) -> str:
    """An inventory linkage document with the required keys, with ``overrides`` applied."""
    return "linkage:\n" + textwrap.indent(document(LINKAGE_KEYS, overrides), "  ")


def write_core(
    tmp_path: Path,
    files: Mapping[str, str] | None = None,
    *,
    id_prefixes: str = IDS,
    number_prefixes: str = "[CGDD]",
) -> Loader:
    """An instance of the one pack ``core``, holding :data:`BASE_FILES` and ``files``."""
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={**BASE_FILES, **(files or {})},
        id_prefixes=id_prefixes,
        number_prefixes=number_prefixes,
    )
    return loaded(tmp_path)


def loaded(tmp_path: Path) -> Loader:
    """A loader for the tree below ``tmp_path`` with steps 1 to 4 run; they must pass."""
    target = Loader(tmp_path / INSTANCE_DIR, tmp_path / PACKS_DIR)
    for step in (1, 2, 3, 4):
        target.run_step(step)
    return target


def failures(target: Loader) -> tuple[ConfigError, ...]:
    """The errors step 5 reports for ``target``, in report order; it must find some."""
    with pytest.raises(ConfigErrors) as caught:
        target.run_step(5)
    assert caught.value.step == 5
    return caught.value.errors


def places(errors: tuple[ConfigError, ...]) -> list[Place]:
    """The places of ``errors``, in report order."""
    return [
        (error.pack, location.file, location.config_path)
        for error in errors
        for location in error.locations
    ]


def reasons(errors: tuple[ConfigError, ...]) -> list[str]:
    """The reasons of ``errors``, in report order."""
    return [error.reason for error in errors]


# --- the same-name declarations of the standard fields (requirement 1.6) -----------------


@pytest.mark.req("1.5")
def test_a_sound_tree_with_same_name_declarations_passes(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            header=block("""
                depot_id: { type: ref, ref: warehouse, required: true }
                biz_date: { label: { zh: 业务日期 }, default_context: today }
                remark: { type: text, default: "草稿" }
                """),
        ),
        "linkages/stock.yaml": linkage_doc(),
    }

    # A same-name declaration may give a writable standard field its label and its
    # defaults, with or without writing the type of that field (requirement 1.5).
    write_core(tmp_path, files).run_step(5)


@pytest.mark.req("1.6")
def test_a_same_name_declaration_with_another_type(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            header="{ biz_date: { type: string, default: x } }"
        )
    }
    found = failures(write_core(tmp_path, files))

    assert places(found) == [(PACK, "entities/purchase_order.yaml", "header.biz_date.type")]
    assert reasons(found) == [
        "单据 purchase_order 的表头字段 biz_date 是引擎标准字段，同名声明的类型 string 与它的类型 date 不同"
    ]


@pytest.mark.req("1.6")
def test_a_default_on_a_read_only_standard_field(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            header="{ number: { default: CGDD0001 }, org_id: { default_context: org } }"
        )
    }
    found = failures(write_core(tmp_path, files))

    # Only the six writable standard fields take a default; the engine fills the others
    # itself (requirements 1.3 and 1.5).
    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "header.number"),
        (PACK, "entities/purchase_order.yaml", "header.org_id"),
    ]
    assert reasons(found) == [
        "单据 purchase_order 的表头字段 number 是只读的引擎标准字段，不能用同名声明声明缺省值（default）",
        "单据 purchase_order 的表头字段 org_id 是只读的引擎标准字段，不能用同名声明声明缺省值（default_context）",
    ]


@pytest.mark.req("1.6")
def test_a_same_name_declaration_with_an_attribute_it_may_not_write(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            header="{ remark: { type: text, maxLength: 200, required: true } }"
        )
    }
    found = failures(write_core(tmp_path, files))

    # The length of a standard field and its necessity are the engine's (requirement 1.3),
    # so a same-name declaration writes neither; the error names every item at once.
    assert places(found) == [(PACK, "entities/purchase_order.yaml", "header.remark")]
    assert reasons(found) == [
        "单据 purchase_order 的表头字段 remark 是引擎标准字段，同名声明只能写"
        " type、label、default、default_from、default_context，不能写 maxLength、required"
    ]


@pytest.mark.req("1.6")
def test_a_default_that_does_not_fit_the_type_of_the_standard_field(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            pricing="true",
            lines=block("""
                material_id: { type: ref, ref: material, required: true }
                unit_id: { type: ref, ref: unit }
                tax_rate: { default: "0.13000" }
                """),
            line_quantity="{ material_field: material_id, unit_field: unit_id }",
            header=block("""
                depot_id: { type: ref, ref: warehouse, required: true }
                price_includes_tax: { default: "true" }
                biz_date: { default: 2026-02-30 }
                """),
        )
    }
    found = failures(write_core(tmp_path, files))

    # A declaration without ``type`` writes a string, an integer or a boolean, and whether
    # that value is a constant of the standard field's type is settled here.
    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "header.biz_date.default"),
        (PACK, "entities/purchase_order.yaml", "header.price_includes_tax.default"),
        (PACK, "entities/purchase_order.yaml", "lines.tax_rate.default"),
    ]
    assert reasons(found) == [
        "单据 purchase_order 的表头字段 biz_date 的缺省值与标准字段的类型 date 不符：必须是 YYYY-MM-DD 形式的有效日期",
        "单据 purchase_order 的表头字段 price_includes_tax 的缺省值与标准字段的类型 bool 不符：必须是布尔值",
        "单据 purchase_order 的分录字段 tax_rate 的缺省值与标准字段的类型 rate 不符：小数位数超过 rate 字段的精度 4 位",
    ]


@pytest.mark.req("1.6")
def test_a_default_context_the_type_of_the_standard_field_does_not_take(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            header=block("""
                depot_id: { type: ref, ref: warehouse, required: true }
                remark: { default_context: today }
                """),
        )
    }
    found = failures(write_core(tmp_path, files))

    # The anchor date is the default of a date field, not of a text field
    # (requirement 5.1).
    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "header.remark.default_context")
    ]
    assert reasons(found) == [
        "单据 purchase_order 的表头字段 remark 的类型 text 不接受 default_context today"
    ]


@pytest.mark.req("1.6")
def test_a_declaration_without_type_that_is_no_standard_field(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            header="{ depot_id: { type: ref, ref: warehouse, required: true }, note: { label: { zh: 备注 } } }"
        )
    }
    found = failures(write_core(tmp_path, files))

    # Such a declaration gives no standard field its defaults and declares no field of its
    # own, every field declaration stating one of the 12 field types (requirement 1.2).
    assert places(found) == [(PACK, "entities/purchase_order.yaml", "header.note")]
    assert reasons(found) == [
        "单据 purchase_order 的表头字段 note 不是引擎标准字段，字段声明必须写 type"
    ]


@pytest.mark.req("1.6")
def test_an_entity_that_declares_a_field_of_its_own_standard_fields(tmp_path: Path) -> None:
    files = {
        "entities/vendor.yaml": entity_doc(
            entity="vendor",
            id_prefix="ven",
            has_status="true",
            fields=block("""
                id: { type: string, unique: true }
                status:
                  type: enum
                  values: { active: { zh: 启用, en: Active } }
                  default: active
                """),
        )
    }
    found = failures(write_core(tmp_path, files, id_prefixes="[mat, mu, unt, wh, ven, orgu, emp]"))

    # An entity has standard fields too (requirement 1.3): its generated ID is read-only,
    # and the values of its status are the engine's enabled and disabled, which a
    # declaration neither restates nor defaults outside of.
    assert places(found) == [
        (PACK, "entities/vendor.yaml", "fields.id"),
        (PACK, "entities/vendor.yaml", "fields.status"),
        (PACK, "entities/vendor.yaml", "fields.status.default"),
    ]
    assert reasons(found) == [
        "实体 vendor 的字段 id 是引擎标准字段，同名声明只能写"
        " type、label、default、default_from、default_context，不能写 unique",
        "实体 vendor 的字段 status 是引擎标准字段，同名声明只能写"
        " type、label、default、default_from、default_context，不能写 values",
        "实体 vendor 的字段 status 的缺省值与标准字段的类型 enum 不符：必须是 enabled、disabled 之一",
    ]


# --- the quantity semantics of a bill's lines (requirements 1.4 and 6.12) ----------------


@pytest.mark.req("1.4")
def test_a_pricing_bill_that_declares_a_quantity_field(tmp_path: Path) -> None:
    files = {"entities/purchase_order.yaml": measured_bill(pricing="true")}
    found = failures(write_core(tmp_path, files))

    # A bill with pricing measures its standard ``qty`` (requirement 1.4).
    assert places(found) == [(PACK, "entities/purchase_order.yaml", "line_quantity.qty_field")]
    assert reasons(found) == [
        "单据 purchase_order 启用了 pricing，分录数量语义不声明数量字段（数量取标准字段 qty）"
    ]


@pytest.mark.req("1.4")
def test_a_bill_without_pricing_whose_quantity_field_is_missing_or_no_qty_field(
    tmp_path: Path,
) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            line_quantity="{ material_field: material_id, unit_field: unit_id }"
        ),
        "entities/second_order.yaml": measured_bill(
            bill="second_order",
            id_prefix="so",
            number="{ prefix: SECOND }",
            line_quantity=("{ material_field: material_id, unit_field: unit_id, qty_field: name }"),
            lines=block("""
                material_id: { type: ref, ref: material, required: true }
                unit_id: { type: ref, ref: unit }
                name: { type: string }
                """),
        ),
    }
    target = write_core(
        tmp_path,
        files,
        id_prefixes="[mat, mu, unt, wh, po, so, orgu, emp]",
        number_prefixes="[CGDD, SECOND]",
    )
    found = failures(target)

    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "line_quantity.qty_field"),
        (PACK, "entities/second_order.yaml", "line_quantity.qty_field"),
    ]
    assert reasons(found) == [
        "单据 purchase_order 没有启用 pricing，分录数量语义必须声明 qty 类型的数量字段",
        "单据 second_order 的分录数量语义的数量字段 name 的类型是 string，必须是 qty",
    ]


@pytest.mark.req("6.12")
def test_a_material_field_that_is_no_ref_to_an_entity(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            lines=block("""
                material_id: { type: ref, ref: purchase_order, required: true }
                unit_id: { type: string }
                move_qty: { type: qty, required: true }
                """),
        )
    }
    found = failures(write_core(tmp_path, files))

    # The material is a record of an entity, which declares the base unit the factor of a
    # line is read from; the unit is a reference too (requirement 6.12).
    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "line_quantity.material_field"),
        (PACK, "entities/purchase_order.yaml", "line_quantity.unit_field"),
    ]
    assert reasons(found) == [
        "单据 purchase_order 的分录数量语义的字段 material_id 引用的 purchase_order 不是实体",
        "单据 purchase_order 的分录数量语义的字段 unit_id 的类型是 string，必须是 ref",
    ]


@pytest.mark.req("6.12")
def test_a_measured_entity_without_a_base_unit(tmp_path: Path) -> None:
    files = {
        "entities/material.yaml": entity_doc(
            entity="material", id_prefix="mat", fields=MATERIAL_FIELDS
        ),
        "entities/purchase_order.yaml": measured_bill(),
    }
    found = failures(write_core(tmp_path, files))

    # Without a base unit no factor applies to the unit of a line (requirement 6.6).
    assert places(found) == [(PACK, "entities/purchase_order.yaml", "line_quantity.material_field")]
    assert reasons(found) == [
        "单据 purchase_order 的分录数量语义的物料引用字段 material_id 指向的实体 material"
        " 没有声明基本单位字段（unit_conversion）"
    ]


@pytest.mark.req("6.12")
def test_a_base_unit_field_that_is_not_a_required_ref_field(tmp_path: Path) -> None:
    files = {
        "entities/material.yaml": entity_doc(
            entity="material",
            id_prefix="mat",
            fields="{ name: { type: string }, unit_id: { type: ref, ref: unit } }",
            unit_conversion=UNIT_CONVERSION,
        ),
        "entities/purchase_order.yaml": measured_bill(),
    }
    found = failures(write_core(tmp_path, files))

    # Every record must have a base unit, so the field is required (requirement 6.12).
    assert places(found) == [(PACK, "entities/material.yaml", "unit_conversion.base_unit_field")]
    assert reasons(found) == [
        "实体 material 的基本单位字段 unit_id 必须是必填的 ref 字段，当前为非必填的 ref 字段"
    ]


@pytest.mark.req("6.12")
def test_a_base_unit_of_another_entity_than_the_unit_of_the_lines(tmp_path: Path) -> None:
    files = {
        "entities/measure.yaml": entity_doc(entity="measure", id_prefix="msr"),
        "entities/material.yaml": entity_doc(
            entity="material",
            id_prefix="mat",
            fields=(
                "{ name: { type: string }, unit_id: { type: ref, ref: measure, required: true } }"
            ),
            unit_conversion=UNIT_CONVERSION,
        ),
        # The conversion records follow the base unit of the material, so the one thing
        # that does not fit is the unit the lines are written in.
        "entities/material_unit.yaml": entity_doc(
            entity="material_unit",
            id_prefix="mu",
            fields=(
                "{ material_id: { type: ref, ref: material, required: true },"
                " unit_id: { type: ref, ref: measure, required: true },"
                " factor: { type: qty, required: true } }"
            ),
        ),
        "entities/purchase_order.yaml": measured_bill(),
    }
    target = write_core(tmp_path, files, id_prefixes="[mat, mu, unt, msr, wh, po, orgu, emp]")
    found = failures(target)

    # One error for the whole mismatch, locating the field of the bill and the base unit
    # field of the entity (requirement 2.2).
    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "line_quantity.unit_field"),
        (PACK, "entities/material.yaml", "unit_conversion.base_unit_field"),
    ]
    assert reasons(found) == [
        "单据 purchase_order 的分录数量语义的计量单位引用字段 unit_id 指向实体 unit，"
        "而物料实体 material 的基本单位字段 unit_id 指向实体 measure"
    ]


@pytest.mark.req("6.12")
def test_conversion_records_whose_fields_do_not_fit(tmp_path: Path) -> None:
    files = {
        "entities/material_unit.yaml": entity_doc(
            entity="material_unit",
            id_prefix="mu",
            fields=block("""
                material_id: { type: ref, ref: material_unit, required: true }
                unit_id: { type: string }
                factor: { type: string }
                """),
        ),
        "entities/purchase_order.yaml": measured_bill(),
    }
    found = failures(write_core(tmp_path, files))

    # A conversion record names its material, its alternative unit and how many base units
    # one of them is, so the three fields are checked where they are named: at the
    # ``unit_conversion`` of the measured entity (requirement 6.12).
    assert places(found) == [
        (PACK, "entities/material.yaml", "unit_conversion.factor_field"),
        (PACK, "entities/material.yaml", "unit_conversion.material_field"),
        (PACK, "entities/material.yaml", "unit_conversion.unit_field"),
    ]
    assert reasons(found) == [
        "换算关系实体 material_unit 的换算系数字段 factor 的类型 string"
        " 不是 int、qty、price、money、rate 之一",
        "换算关系实体 material_unit 的字段 material_id 必须是指向实体 material 的 ref 字段，"
        "当前为 ref: material_unit",
        "换算关系实体 material_unit 的字段 unit_id 必须是指向实体 unit 的 ref 字段，当前为 string",
    ]


# --- the inventory linkages (requirements 9.3 and 9.11) ----------------------------------


@pytest.mark.req("9.11")
def test_a_warehouse_field_that_is_not_a_required_ref_field(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            header="{ depot_id: { type: ref, ref: warehouse } }"
        ),
        "linkages/stock.yaml": linkage_doc(),
    }
    found = failures(write_core(tmp_path, files))

    # Every stock transaction names its warehouse, so the field is required
    # (requirement 9.11).
    assert places(found) == [(PACK, "linkages/stock.yaml", "linkage.warehouse_field")]
    assert reasons(found) == [
        "库存联动的仓库字段 header.depot_id 必须是单据 purchase_order 中必填的 ref 字段，"
        "当前为非必填的 ref 字段"
    ]


@pytest.mark.req("9.3")
def test_a_transfer_linkage_without_both_warehouses(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(),
        "linkages/stock.yaml": linkage_doc(direction="transfer"),
    }
    found = failures(write_core(tmp_path, files))

    # A transfer writes one stock transaction in each of its two warehouses
    # (requirement 9.2), and the warehouse of the other directions does not apply to it.
    assert places(found) == [
        (PACK, "linkages/stock.yaml", "linkage"),
        (PACK, "linkages/stock.yaml", "linkage"),
        (PACK, "linkages/stock.yaml", "linkage.warehouse_field"),
    ]
    assert reasons(found) == [
        "direction 为 transfer 的库存联动必须声明 from_warehouse_field",
        "direction 为 transfer 的库存联动必须声明 to_warehouse_field",
        "direction 为 transfer 的库存联动不声明 warehouse_field",
    ]


@pytest.mark.req("9.3")
def test_an_adjust_linkage_without_the_direction_field(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(),
        "linkages/stock.yaml": linkage_doc(direction="adjust"),
    }
    found = failures(write_core(tmp_path, files))

    assert places(found) == [(PACK, "linkages/stock.yaml", "linkage")]
    assert reasons(found) == ["direction 为 adjust 的库存联动必须声明 adjust"]


@pytest.mark.req("9.3")
def test_a_direction_field_that_is_no_required_enum_field_of_the_header(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            header=block("""
                depot_id: { type: ref, ref: warehouse, required: true }
                count_kind: { type: enum, values: { gain: { zh: 盘盈, en: Gain } } }
                """),
        ),
        "linkages/stock.yaml": linkage_doc(
            direction="adjust",
            adjust="{ field: header.count_kind, increase: [gain], decrease: [loss] }",
        ),
    }
    found = failures(write_core(tmp_path, files))

    # The direction of every line is read off that one value of the header, so the field is
    # required (requirement 9.2).
    assert places(found) == [(PACK, "linkages/stock.yaml", "linkage.adjust.field")]
    assert reasons(found) == [
        "库存联动的增减方向字段 header.count_kind 必须是单据 purchase_order 表头中必填的"
        " enum 字段，当前为非必填的 enum 类型表头字段"
    ]


@pytest.mark.req("9.3")
def test_increase_and_decrease_that_do_not_divide_the_values(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            header=block("""
                depot_id: { type: ref, ref: warehouse, required: true }
                count_kind:
                  type: enum
                  required: true
                  values:
                    gain: { zh: 盘盈, en: Gain }
                    loss: { zh: 盘亏, en: Loss }
                    none: { zh: 无差异, en: None }
                """),
        ),
        "linkages/stock.yaml": linkage_doc(
            direction="adjust",
            adjust="{ field: header.count_kind, increase: [gain, loss], decrease: [loss, more] }",
        ),
    }
    found = failures(write_core(tmp_path, files))

    # Each value of the field says one direction: every value is listed, no value twice and
    # none that the field does not have (requirement 9.3).
    assert places(found) == [
        (PACK, "linkages/stock.yaml", "linkage.adjust"),
        (PACK, "linkages/stock.yaml", "linkage.adjust"),
        (PACK, "linkages/stock.yaml", "linkage.adjust"),
    ]
    assert reasons(found) == [
        "增减方向字段 count_kind 的取值与增减方向的对应关系不一致：loss 同时列在 increase 和 decrease 中",
        "增减方向字段 count_kind 的取值与增减方向的对应关系不一致：more 不是该字段的取值",
        "增减方向字段 count_kind 的取值与增减方向的对应关系不一致：none 没有列在 increase 或 decrease 中",
    ]


@pytest.mark.req("9.11")
def test_a_linkage_on_lines_without_quantity_semantics_and_another_quantity(
    tmp_path: Path,
) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            lines="{ move_qty: { type: qty, required: true } }", line_quantity=None
        ),
        "linkages/stock.yaml": linkage_doc(qty_field="line.move_qty"),
    }
    found = failures(write_core(tmp_path, files))

    # Stock is kept in base units, so a linkage reads the lines' qty_base and the lines
    # have to declare what they measure (requirement 9.11).
    assert places(found) == [
        (PACK, "linkages/stock.yaml", "linkage.bill"),
        (PACK, "linkages/stock.yaml", "linkage.qty_field"),
    ]
    assert reasons(found) == [
        "单据 purchase_order 的分录没有声明数量语义（line_quantity），库存联动取不到基本单位数量",
        "库存联动的数量字段必须是 line.qty_base，不能是 line.move_qty",
    ]


@pytest.mark.req("9.3")
def test_a_linkage_that_declares_what_its_direction_does_not_use(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(),
        "linkages/stock.yaml": linkage_doc(
            direction="out",
            to_warehouse_field="header.depot_id",
            adjust="{ field: header.depot_id, increase: [gain], decrease: [loss] }",
        ),
    }
    found = failures(write_core(tmp_path, files))

    assert places(found) == [
        (PACK, "linkages/stock.yaml", "linkage.adjust"),
        (PACK, "linkages/stock.yaml", "linkage.to_warehouse_field"),
    ]
    assert reasons(found) == [
        "direction 为 out 的库存联动不声明 adjust",
        "direction 为 out 的库存联动不声明 to_warehouse_field",
    ]


# --- the simple state machine (requirement 7.11) -----------------------------------------


@pytest.mark.req("7.11")
def test_a_simple_bill_that_declares_the_operations_it_does_not_have(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            state_machine="simple",
            operations="[create, submit, approve, close]",
        )
    }
    found = failures(write_core(tmp_path, files))

    # A simple bill has draft and approved alone (meta-model.md section 6), so each of the
    # four operations of the other states is reported at its own item of the list.
    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "operations[1]"),
        (PACK, "entities/purchase_order.yaml", "operations[3]"),
    ]
    assert reasons(found) == [
        "单据 purchase_order 的 state_machine 为 simple，"
        "没有 submit、unsubmit、close、unclose 操作，不能声明 submit",
        "单据 purchase_order 的 state_machine 为 simple，"
        "没有 submit、unsubmit、close、unclose 操作，不能声明 close",
    ]


@pytest.mark.req("7.11")
def test_a_writeback_that_closes_a_simple_source_automatically(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(state_machine="simple"),
        "entities/receipt.yaml": measured_bill(
            bill="receipt", id_prefix="rec", number="{ prefix: RECE }"
        ),
        "conversions/push.yaml": doc("""
            conversion:
              id: order_to_receipt
              source: purchase_order
              target: receipt
              require_source_status: approved
              limit_by: qty
              required_line_inputs: [source_line_id, move_qty]
            """),
        "writebacks/back.yaml": doc("""
            writeback:
              id: receipt_linked_qty
              conversion: order_to_receipt
              on: approve
              target: source_line.linked_qty
              op: add
              value: line.qty_base
              auto_close_source_when: all_lines_linked
            """),
    }
    target = write_core(
        tmp_path,
        files,
        id_prefixes="[mat, mu, unt, wh, po, rec, orgu, emp]",
        number_prefixes="[CGDD, RECE]",
    )
    found = failures(target)

    # A source that cannot be closed cannot be closed automatically (requirement 7.11).
    assert places(found) == [(PACK, "writebacks/back.yaml", "writeback.auto_close_source_when")]
    assert reasons(found) == [
        "单据 purchase_order 的 state_machine 为 simple，没有 closed 状态，"
        "writeback receipt_linked_qty 不能声明 auto_close_source_when"
    ]


# --- the order the defaults are applied in -----------------------------------------------


@pytest.mark.req("5.1")
def test_the_fixture_keeps_the_order_of_its_defaults() -> None:
    domain = fixture_domain()

    # Every field with a default_from, in the order they are applied; the fields the caller
    # or a constant fills take no part in that pass (design.md 4.2).
    assert domain.entities["article"].default_order == ("buy_measure_id",)
    assert domain.entities["measure"].default_order == ()
    # Standard fields come first in a field view, so the same-name declaration of
    # tax_rate is applied before the declared measure_id; neither reads through the other.
    order = domain.bills["supply_order"]
    assert order.line_default_order == ("tax_rate", "measure_id")
    assert order.header_default_order == ()
    assert domain.bills["goods_entry"].line_default_order == ("measure_id",)


@pytest.mark.req("5.1")
def test_a_default_read_through_a_field_with_a_default_comes_after_it(tmp_path: Path) -> None:
    files = {
        "entities/material.yaml": entity_doc(
            entity="material",
            id_prefix="mat",
            fields=block("""
                name: { type: string, required: true }
                sell_unit_id: { type: ref, ref: unit, default_from: alt_unit.id }
                alt_unit_id: { type: ref, ref: unit, default_from: unit.id }
                unit_id: { type: ref, ref: unit, required: true }
                """),
            unit_conversion=UNIT_CONVERSION,
        )
    }
    target = write_core(tmp_path, files)
    target.run_step(5)
    domain = target.domain

    # The declaration order is the reverse of the dependency order: the alternative unit
    # is read through the base unit, which the caller or the record fills, and the selling
    # unit through the alternative unit, so it comes last.
    assert domain is not None
    assert domain.entities["material"].default_order == ("alt_unit_id", "sell_unit_id")


@pytest.mark.req("2.2")
def test_defaults_that_depend_on_each_other_in_a_circle(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": measured_bill(
            header=block("""
                depot_id: { type: ref, ref: warehouse, required: true }
                first_unit_id: { type: ref, ref: unit, default_from: second_unit.id }
                second_unit_id: { type: ref, ref: unit, default_from: first_unit.id }
                third_unit_id: { type: ref, ref: unit, default_from: first_unit.id }
                """),
        )
    }
    found = failures(write_core(tmp_path, files))

    # One error for the whole cycle, locating every field of it (requirement 2.2); the
    # field that only reads through the cycle adds none of its own.
    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "header.first_unit_id.default_from"),
        (PACK, "entities/purchase_order.yaml", "header.second_unit_id.default_from"),
    ]
    assert reasons(found) == [
        "单据 purchase_order 的表头字段 first_unit_id、second_unit_id 的 default_from 构成循环依赖"
    ]


@pytest.mark.req("2.2")
def test_a_default_that_is_read_through_the_field_itself(tmp_path: Path) -> None:
    files = {
        "entities/material.yaml": entity_doc(
            entity="material",
            id_prefix="mat",
            fields=block("""
                name: { type: string, required: true }
                unit_id: { type: ref, ref: unit, required: true, default_from: unit.id }
                """),
            unit_conversion=UNIT_CONVERSION,
        )
    }
    found = failures(write_core(tmp_path, files))

    assert places(found) == [(PACK, "entities/material.yaml", "fields.unit_id.default_from")]
    assert reasons(found) == ["实体 material 的字段 unit_id 的 default_from 构成循环依赖"]
