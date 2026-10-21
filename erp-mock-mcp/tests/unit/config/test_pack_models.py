"""erp_mock.config.pack: the configuration objects of a business pack.

Every example of meta-model.md, tool-design.md and design.md data model 1 is parsed here
with loader.yaml_io, the way loading step 4 reads a pack, and validated against its model.
Errors are asserted with their pydantic ``loc``, which is the config path the loader reports
(``("conversion", "limit_by")`` is ``conversion.limit_by``).
"""

import textwrap
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from erp_mock.common.errors import ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.versions import SemVer, VersionRange
from erp_mock.config.base import Label, Loc
from erp_mock.config.fields import (
    DecimalField,
    EnumField,
    RefField,
    StandardOverride,
    StringField,
)
from erp_mock.config.pack import (
    BILL_OPERATIONS,
    DESCRIPTION_VARIANTS,
    ENTITY_OPERATIONS,
    GENERATORS,
    PRIMITIVES,
    BillDef,
    ChoiceGenerator,
    CodeWordsGenerator,
    CompanyNameGenerator,
    ConversionFile,
    DecimalRangeGenerator,
    DeclarativeQueryDef,
    DescribedRuleDef,
    DescriptionFile,
    EnforcedRuleDef,
    EntityDef,
    ExposureDef,
    IntRangeGenerator,
    LinkageFile,
    PackManifest,
    PersonNameGenerator,
    PrimitiveQueryDef,
    RuleFile,
    SeedGeneratorsFile,
    SharedDataFile,
    ShortNameGenerator,
    WritebackFile,
    entity_standard_fields,
    header_standard_fields,
    line_standard_fields,
)
from erp_mock.loader.yaml_io import parse_yaml

FILE = Path("entities/sample.yaml")


def parse(text: str) -> Any:
    return parse_yaml(textwrap.dedent(text), FILE)


def load[M: BaseModel](model: type[M], text: str) -> M:
    """Validate the YAML document ``text`` as ``model``, as step 4 validates a file."""
    return model.model_validate(parse(text))


def errors(model: type[BaseModel], text: str) -> list[tuple[Loc, str]]:
    """The ``(loc, type)`` of every error of the YAML document ``text`` as a ``model``."""
    with pytest.raises(ValidationError) as caught:
        model.model_validate(parse(text))
    return [(error["loc"], error["type"]) for error in caught.value.errors()]


# --- manifest (business-pack.md section 4) -----------------------------------------------


@pytest.mark.req("1.1", "2.6")
def test_manifest_example() -> None:
    manifest = load(
        PackManifest,
        """
        pack: purchase
        version: 1.0.0
        title: { zh: 采购, en: Purchase }
        requires_meta_model: ">=0.1.0 <1.0.0"
        depends_on:
          core: ">=0.1.0 <1.0.0"
        primitives: [crud, numbering, state_machine, pricing, conversion, writeback, rule_expr]
        rule_prefix: PUR
        id_prefixes: [preq, po, prcv]
        number_prefixes: [CGSQ, CGDD, CGRK]
        tool_prefixes: [purchase_]
        description_variants: [zh-detailed]
        license: Apache-2.0
        maintainers: ["示例团队"]
        """,
    )
    assert (manifest.pack, manifest.version) == ("purchase", "1.0.0")
    assert manifest.title == Label(zh="采购", en="Purchase")
    assert manifest.primitives[:2] == ("crud", "numbering")
    assert manifest.id_prefixes == ("preq", "po", "prcv")
    assert manifest.number_prefixes == ("CGSQ", "CGDD", "CGRK")
    assert manifest.tool_prefixes == ("purchase_",)
    assert manifest.description_variants == ("zh-detailed",)
    # Steps 2 and 3 parse the ranges; they are text here (requirement 2.5).
    assert list(manifest.depends_on) == ["core"]
    assert VersionRange.parse(manifest.requires_meta_model).matches(SemVer(0, 1, 0))


@pytest.mark.req("1.1")
def test_manifest_defaults() -> None:
    manifest = load(
        PackManifest,
        """
        pack: core
        version: 0.1.0
        title: { zh: 基础 }
        requires_meta_model: 0.1.0
        primitives: []
        rule_prefix: CORE
        id_prefixes: []
        number_prefixes: []
        tool_prefixes: [core_]
        description_variants: []
        license: Apache-2.0
        maintainers: []
        """,
    )
    assert manifest.depends_on == FrozenMap()
    # An empty description_variants is reported at step 7, not here (requirement 3.1).
    assert manifest.description_variants == ()


@pytest.mark.req("1.1", "2.6")
def test_manifest_primitives_are_the_m0_primitives() -> None:
    assert PRIMITIVES == (
        "crud",
        "numbering",
        "pricing",
        "unit_conversion",
        "state_machine",
        "conversion",
        "writeback",
        "inventory",
        "rule_expr",
    )


@pytest.mark.req("2.6")
@pytest.mark.parametrize("primitive", ["partner_balance", "write_off", "voucher", "permission"])
def test_manifest_rejects_a_primitive_outside_m0(primitive: str) -> None:
    text = f"""
        pack: ap
        version: 1.0.0
        title: {{ zh: 应付, en: Accounts Payable }}
        requires_meta_model: ">=0.1.0 <1.0.0"
        primitives: [crud, numbering, {primitive}]
        rule_prefix: AP
        id_prefixes: [apb]
        number_prefixes: [YFD]
        tool_prefixes: [ap_]
        description_variants: [zh-detailed]
        license: Apache-2.0
        maintainers: ["示例团队"]
        """
    assert errors(PackManifest, text) == [(("primitives", 2), "literal_error")]


@pytest.mark.req("1.1")
@pytest.mark.parametrize("key", ["pack", "version", "title", "requires_meta_model", "primitives"])
def test_manifest_required_keys(key: str) -> None:
    lines = [
        "pack: core",
        "version: 0.1.0",
        "title: { zh: 基础 }",
        'requires_meta_model: ">=0.1.0 <1.0.0"',
        "primitives: [crud]",
        "rule_prefix: CORE",
        "id_prefixes: [org]",
        "number_prefixes: []",
        "tool_prefixes: [core_]",
        "description_variants: [zh-detailed]",
        "license: Apache-2.0",
        "maintainers: []",
    ]
    text = "\n".join(line for line in lines if not line.startswith(f"{key}:")) + "\n"
    assert errors(PackManifest, text) == [((key,), "missing")]


@pytest.mark.req("1.1", "2.3")
@pytest.mark.parametrize("version", ["1.0", "01.0.0", "1.0.0-rc1", ">=1.0.0", "v1.0.0", "1.0.0 "])
def test_manifest_version_must_be_a_concrete_version(version: str) -> None:
    text = f"""
        pack: core
        version: "{version}"
        title: {{ zh: 基础 }}
        requires_meta_model: 0.1.0
        primitives: [crud]
        rule_prefix: CORE
        id_prefixes: [org]
        number_prefixes: []
        tool_prefixes: [core_]
        description_variants: [zh-detailed]
        license: Apache-2.0
        maintainers: []
        """
    assert errors(PackManifest, text) == [(("version",), "version_invalid")]


# --- entity (meta-model.md section 3) ----------------------------------------------------


@pytest.mark.req("1.1")
def test_meta_model_entity_example() -> None:
    entity = load(
        EntityDef,
        """
        entity: supplier
        label: { zh: 供应商, en: Supplier }
        id_prefix: sup
        code: { prefix: S, digits: 4 }
        shared: false
        has_status: true
        search:
          keyword_fields: [code, name, short_name]
          filters: [status]
          default_sort: [code]
        fields:
          name:              { type: string, label: { zh: 名称 }, required: true }
          short_name:        { type: string, label: { zh: 简称 } }
          payment_term_days: { type: int, label: { zh: 账期天数 }, default: 30 }
          default_tax_rate:  { type: rate, label: { zh: 默认税率 }, default: "0.13" }
        """,
    )
    assert entity.entity == "supplier"
    assert entity.label == Label(zh="供应商", en="Supplier")
    assert entity.id_prefix == "sup"
    assert entity.code is not None
    assert (entity.code.prefix, entity.code.digits) == ("S", 4)
    assert (entity.shared, entity.has_status) == (False, True)
    assert entity.search is not None
    assert entity.search.keyword_fields == ("code", "name", "short_name")
    assert entity.search.filters == ("status",)
    assert entity.search.default_sort == ("code",)
    assert list(entity.fields) == ["name", "short_name", "payment_term_days", "default_tax_rate"]
    assert isinstance(entity.fields["default_tax_rate"], DecimalField)
    # Not declared: no context role, no unit conversion, no tools.
    assert (entity.context, entity.unit_conversion, entity.operations) == (None, None, ())


@pytest.mark.req("1.8")
def test_design_org_context_entity() -> None:
    entity = load(
        EntityDef,
        """
        entity: org
        label: { zh: 组织, en: Organization }
        id_prefix: org
        shared: true
        context: { role: org, default_flag: is_default }
        fields:
          code:       { type: string, required: true, unique: true }
          name:       { type: string, required: true }
          is_default: { type: bool, default: false }
        """,
    )
    assert entity.shared is True
    assert entity.context is not None
    assert (entity.context.role, entity.context.default_flag) == ("org", "is_default")
    # An entity without a code rule keeps the code it is given, so code is a declared field.
    assert entity.code is None
    assert isinstance(entity.fields["code"], StringField)


@pytest.mark.req("1.8")
def test_user_context_entity_has_no_default_flag() -> None:
    entity = load(
        EntityDef,
        """
        entity: employee
        id_prefix: emp
        code: { prefix: E, digits: 4 }
        shared: true
        context: { role: user }
        operations: [search]
        fields:
          name: { type: string, required: true }
          role: { type: enum, values: { buyer: { zh: 采购员, en: Buyer } } }
        """,
    )
    assert entity.context is not None
    assert (entity.context.role, entity.context.default_flag) == ("user", None)
    assert entity.operations == ("search",)


@pytest.mark.req("1.8")
@pytest.mark.parametrize(
    ("decl", "loc", "error_type"),
    [
        ("{ role: company, default_flag: is_default }", ("role",), "literal_error"),
        ("{ default_flag: is_default }", ("role",), "missing"),
        ("{ role: org, default: is_default }", ("default",), "extra_forbidden"),
        ("{ role: org, default_flag: Is_Default }", ("default_flag",), "string_pattern_mismatch"),
        ("{ role: org, default_flag: true }", ("default_flag",), "string_type"),
    ],
)
def test_context_errors(decl: str, loc: Loc, error_type: str) -> None:
    text = f"entity: org\nid_prefix: org\ncontext: {decl}\nfields: {{}}\n"
    assert errors(EntityDef, text) == [(("context", *loc), error_type)]


@pytest.mark.req("1.1")
def test_design_material_entity_with_unit_conversion() -> None:
    entity = load(
        EntityDef,
        """
        entity: material
        id_prefix: mat
        code: { prefix: M, digits: 5 }
        has_status: true
        operations: [search, get]
        unit_conversion:
          base_unit_field: base_unit_id
          entity: unit_conversion
          material_field: material_id
          unit_field: from_unit_id
          factor_field: factor
        fields:
          name:             { type: string, required: true }
          base_unit_id:     { type: ref, ref: unit, required: true }
          purchase_unit_id: { type: ref, ref: unit, default_from: base_unit.id }
          tax_rate:         { type: rate, values: ["0", "0.06", "0.09", "0.13"], default: "0.13" }
        """,
    )
    assert entity.operations == ("search", "get")
    declared = entity.unit_conversion
    assert declared is not None
    assert declared.base_unit_field == "base_unit_id"
    assert declared.entity == "unit_conversion"
    assert (declared.material_field, declared.unit_field) == ("material_id", "from_unit_id")
    assert declared.factor_field == "factor"
    assert isinstance(entity.fields["base_unit_id"], RefField)


@pytest.mark.req("1.1", "1.11")
@pytest.mark.parametrize(
    ("document", "loc", "error_type"),
    [
        ("entity: Supplier\nid_prefix: sup\nfields: {}\n", ("entity",), "string_pattern_mismatch"),
        ("entity: supplier\nfields: {}\n", ("id_prefix",), "missing"),
        ("entity: supplier\nid_prefix: sup\n", ("fields",), "missing"),
        ("id_prefix: sup\nfields: {}\n", ("entity",), "missing"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\nstate_machine: standard\n", ("state_machine",), "extra_forbidden"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\nbill: supplier\n", ("bill",), "extra_forbidden"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\nshared: 'true'\n", ("shared",), "bool_type"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\nhas_status: 1\n", ("has_status",), "bool_type"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\ncode: { prefix: S }\n", ("code", "digits"), "missing"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\ncode: { prefix: s, digits: 4 }\n", ("code", "prefix"), "string_pattern_mismatch"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\ncode: { prefix: S, digits: 0 }\n", ("code", "digits"), "greater_than_equal"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\ncode: { prefix: S, digits: '4' }\n", ("code", "digits"), "int_type"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\noperations: [submit]\n", ("operations", 0), "literal_error"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\noperations: search\n", ("operations",), "tuple_type"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\nsearch: { filters: [status], sort: [code] }\n", ("search", "sort"), "extra_forbidden"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\nsearch: { default_sort: [code up] }\n", ("search", "default_sort", 0), "string_pattern_mismatch"),
        ("entity: supplier\nid_prefix: sup\nfields: {}\nunit_conversion: { base_unit_field: base_unit_id }\n", ("unit_conversion", "entity"), "missing"),
    ],
)  # fmt: skip
def test_entity_errors(document: str, loc: Loc, error_type: str) -> None:
    found = errors(EntityDef, document)
    assert (loc, error_type) in found


@pytest.mark.req("1.1")
def test_entity_search_lists_default_to_empty() -> None:
    entity = load(EntityDef, "entity: unit\nid_prefix: unit\nsearch: {}\nfields: {}\n")
    assert entity.search is not None
    assert (entity.search.keyword_fields, entity.search.filters) == ((), ())
    assert entity.search.default_sort == ()


@pytest.mark.req("1.1")
def test_search_sort_accepts_a_direction() -> None:
    entity = load(
        EntityDef,
        """
        entity: unit
        id_prefix: unit
        search: { default_sort: [code asc, name desc, id] }
        fields: {}
        """,
    )
    assert entity.search is not None
    assert entity.search.default_sort == ("code asc", "name desc", "id")


# --- bill (meta-model.md section 4) ------------------------------------------------------


@pytest.mark.req("1.1", "1.9")
def test_meta_model_bill_example() -> None:
    bill = load(
        BillDef,
        """
        bill: purchase_order
        label: { zh: 采购订单, en: Purchase Order }
        id_prefix: po
        number: { prefix: CGDD }
        state_machine: standard
        pricing: true
        partner: { field: supplier_id }
        operations: [search, get, create, update, delete, submit, unsubmit, approve, unapprove, close, unclose]
        header:
          supplier_id:   { type: ref, ref: supplier, required: true }
          delivery_date: { type: date, label: { zh: 交货日期 }, required: true }
        lines:
          material_id: { type: ref, ref: material, required: true }
          unit_id:     { type: ref, ref: unit, default_from: material.purchase_unit_id }
        search:
          keyword_fields: [number]
          filters: [status, supplier_id, biz_date]
          default_sort: [biz_date desc, number desc]
        """,
    )
    assert bill.bill == "purchase_order"
    assert bill.number.prefix == "CGDD"
    assert bill.state_machine == "standard"
    assert bill.pricing is True
    assert bill.partner is not None
    assert bill.partner.field == "supplier_id"
    assert bill.operations == BILL_OPERATIONS
    assert list(bill.header) == ["supplier_id", "delivery_date"]
    assert list(bill.lines) == ["material_id", "unit_id"]
    assert bill.search is not None
    assert bill.search.default_sort == ("biz_date desc", "number desc")
    assert bill.line_quantity is None


@pytest.mark.req("1.4")
def test_design_bill_with_quantity_semantics_and_no_pricing() -> None:
    bill = load(
        BillDef,
        """
        bill: stock_adjustment
        id_prefix: sadj
        number: { prefix: KCTZ }
        state_machine: standard
        operations: [create, submit, approve]
        header:
          warehouse_id: { type: ref, ref: warehouse, required: true }
          reason:       { type: enum, required: true, values: { gain: { zh: 盘盈, en: Gain }, loss: { zh: 盘亏, en: Loss } } }
        lines:
          material_id: { type: ref, ref: material, required: true }
          unit_id:     { type: ref, ref: unit, default_from: material.base_unit_id }
          qty:         { type: qty, required: true }
        line_quantity: { material_field: material_id, unit_field: unit_id, qty_field: qty }
        """,
    )
    assert bill.pricing is False
    quantity = bill.line_quantity
    assert quantity is not None
    assert (quantity.material_field, quantity.unit_field) == ("material_id", "unit_id")
    assert quantity.qty_field == "qty"
    assert isinstance(bill.header["reason"], EnumField)
    assert isinstance(bill.lines["qty"], DecimalField)


@pytest.mark.req("1.4")
def test_quantity_semantics_of_a_pricing_bill_needs_no_qty_field() -> None:
    bill = load(
        BillDef,
        """
        bill: purchase_receipt
        id_prefix: prcv
        number: { prefix: CGRK }
        state_machine: standard
        pricing: true
        header:
          warehouse_id: { type: ref, ref: warehouse, required: true }
        lines:
          material_id: { type: ref, ref: material, required: true }
          unit_id:     { type: ref, ref: unit }
          tax_rate:    { default_from: material.tax_rate }
        line_quantity: { material_field: material_id, unit_field: unit_id }
        """,
    )
    quantity = bill.line_quantity
    assert quantity is not None
    assert quantity.qty_field is None
    # A same-name declaration of the standard tax_rate (requirement 1.5).
    assert isinstance(bill.lines["tax_rate"], StandardOverride)


@pytest.mark.req("1.9")
def test_state_machine_is_required_and_has_two_values() -> None:
    document = """
        bill: purchase_order
        id_prefix: po
        number: { prefix: CGDD }
        header: {}
        lines: {}
        """
    assert errors(BillDef, document) == [(("state_machine",), "missing")]
    assert load(BillDef, document + "state_machine: simple\n").state_machine == "simple"


@pytest.mark.req("1.9")
@pytest.mark.parametrize("value", ["Standard", "none", "approval", "1", "null", "[standard]"])
def test_state_machine_rejects_any_other_value(value: str) -> None:
    document = f"""
        bill: purchase_order
        id_prefix: po
        number: {{ prefix: CGDD }}
        state_machine: {value}
        header: {{}}
        lines: {{}}
        """
    assert errors(BillDef, document) == [(("state_machine",), "literal_error")]


@pytest.mark.req("1.1", "1.11")
@pytest.mark.parametrize(
    ("document", "loc", "error_type"),
    [
        ("id_prefix: po\nnumber: { prefix: CGDD }\nstate_machine: standard\nheader: {}\nlines: {}\n", ("bill",), "missing"),
        ("bill: purchase_order\nstate_machine: standard\nnumber: { prefix: CGDD }\nheader: {}\nlines: {}\n", ("id_prefix",), "missing"),
        ("bill: purchase_order\nid_prefix: po\nstate_machine: standard\nheader: {}\nlines: {}\n", ("number",), "missing"),
        ("bill: purchase_order\nid_prefix: po\nnumber: { prefix: CGDD }\nstate_machine: standard\nlines: {}\n", ("header",), "missing"),
        ("bill: purchase_order\nid_prefix: po\nnumber: { prefix: CGDD }\nstate_machine: standard\nheader: {}\n", ("lines",), "missing"),
        ("bill: purchase_order\nid_prefix: po\nnumber: { digits: 4, prefix: CGDD }\nstate_machine: standard\nheader: {}\nlines: {}\n", ("number", "digits"), "extra_forbidden"),
        ("bill: purchase_order\nid_prefix: po\nnumber: CGDD\nstate_machine: standard\nheader: {}\nlines: {}\n", ("number",), "model_type"),
        ("bill: purchase_order\nid_prefix: po\nnumber: { prefix: CGDD }\nstate_machine: standard\nheader: {}\nlines: {}\npricing: 'true'\n", ("pricing",), "bool_type"),
        ("bill: purchase_order\nid_prefix: po\nnumber: { prefix: CGDD }\nstate_machine: standard\nheader: {}\nlines: {}\npartner: supplier_id\n", ("partner",), "model_type"),
        ("bill: purchase_order\nid_prefix: po\nnumber: { prefix: CGDD }\nstate_machine: standard\nheader: {}\nlines: {}\noperations: [query]\n", ("operations", 0), "literal_error"),
        ("bill: purchase_order\nid_prefix: po\nnumber: { prefix: CGDD }\nstate_machine: standard\nheader: {}\nlines: {}\nline_quantity: { unit_field: unit_id }\n", ("line_quantity", "material_field"), "missing"),
        ("bill: purchase_order\nid_prefix: po\nnumber: { prefix: CGDD }\nstate_machine: standard\nheader: {}\nlines: {}\nentity: purchase_order\n", ("entity",), "extra_forbidden"),
    ],
)  # fmt: skip
def test_bill_errors(document: str, loc: Loc, error_type: str) -> None:
    assert errors(BillDef, document) == [(loc, error_type)]


@pytest.mark.req("1.1")
def test_bill_operations_cover_the_state_machine() -> None:
    assert ENTITY_OPERATIONS == ("search", "get", "create", "update", "delete")
    assert BILL_OPERATIONS == (
        "search",
        "get",
        "create",
        "update",
        "delete",
        "submit",
        "unsubmit",
        "approve",
        "unapprove",
        "close",
        "unclose",
    )


# --- standard fields and the names of ref fields (requirements 1.3 and 1.12) -------------


@pytest.mark.req("1.3")
def test_standard_fields_of_a_bill() -> None:
    header = header_standard_fields(pricing=False)
    assert list(header) == [
        "id",
        "number",
        "bill_type",
        "org_id",
        "biz_date",
        "status",
        "source_bill_type",
        "source_bill_id",
        "remark",
        "created_seq",
        "updated_seq",
    ]
    # Only the two refs are returned as a nested object under the name without _id.
    assert [name for name, nested in header.items() if nested] == ["org_id", "source_bill_id"]
    pricing_header = header_standard_fields(pricing=True)
    assert set(pricing_header) - set(header) == {
        "price_includes_tax",
        "amount",
        "tax_amount",
        "amount_with_tax",
    }
    assert list(line_standard_fields(pricing=False, line_quantity=False)) == [
        "id",
        "line_no",
        "source_line_id",
    ]
    assert "qty_base" in line_standard_fields(pricing=False, line_quantity=True)
    assert "qty" not in line_standard_fields(pricing=False, line_quantity=True)
    assert {"qty", "qty_base", "price", "tax_rate"} <= set(
        line_standard_fields(pricing=True, line_quantity=False)
    )
    assert not any(line_standard_fields(pricing=True, line_quantity=True).values())


@pytest.mark.req("1.3")
def test_standard_fields_of_an_entity() -> None:
    assert list(entity_standard_fields(code=False, has_status=False)) == ["id"]
    assert list(entity_standard_fields(code=True, has_status=True)) == ["id", "code", "status"]
    assert not any(entity_standard_fields(code=True, has_status=True).values())


@pytest.mark.req("1.12")
def test_entity_ref_names_are_checked_against_the_standard_fields() -> None:
    assert errors(
        EntityDef,
        """
        entity: supplier
        id_prefix: sup
        code: { prefix: S, digits: 4 }
        has_status: true
        fields:
          status_id: { type: ref, ref: supplier_status }
          code_id:   { type: ref, ref: code_table }
          name:      { type: string }
        """,
    ) == [
        (("fields", "status_id"), "ref_name_conflict"),
        (("fields", "code_id"), "ref_name_conflict"),
    ]


@pytest.mark.req("1.12")
def test_without_a_code_rule_and_a_status_those_names_are_free() -> None:
    entity = load(
        EntityDef,
        """
        entity: supplier
        id_prefix: sup
        fields:
          status_id: { type: ref, ref: supplier_status }
          code_id:   { type: ref, ref: code_table }
          name:      { type: string }
        """,
    )
    assert list(entity.fields) == ["status_id", "code_id", "name"]


@pytest.mark.req("1.12")
def test_bill_ref_names_are_checked_against_the_standard_fields_of_each_level() -> None:
    document = """
        bill: purchase_order
        id_prefix: po
        number: { prefix: CGDD }
        state_machine: standard
        pricing: true
        header:
          org:         { type: string }
          org_id:      { label: { zh: 组织 } }
          number_id:   { type: ref, ref: numbering }
          source_bill: { type: string }
        lines:
          qty_id: { type: ref, ref: qty_table }
        """
    # org_id is a same-name declaration of the standard ref field, so its key is org too.
    assert errors(BillDef, document) == [
        (("header", "org_id"), "ref_name_conflict"),
        (("header", "number_id"), "ref_name_conflict"),
        (("header", "source_bill"), "ref_name_conflict"),
        (("lines", "qty_id"), "ref_name_conflict"),
    ]


@pytest.mark.req("1.12")
def test_field_errors_of_a_level_stop_the_name_check_of_the_document() -> None:
    # The field declarations are validated first; their errors are reported alone.
    document = """
        bill: purchase_order
        id_prefix: po
        number: { prefix: CGDD }
        state_machine: standard
        header:
          org: { type: ref, ref: org }
        lines:
          material_id: { type: ref }
        """
    assert errors(BillDef, document) == [
        (("header", "org"), "ref_name_suffix"),
        (("lines", "material_id", "ref"), "missing"),
    ]


# --- conversion and writeback (meta-model.md section 7) ----------------------------------


@pytest.mark.req("1.1")
def test_meta_model_conversion_example() -> None:
    document = load(
        ConversionFile,
        """
        conversion:
          id: purchase_order_to_receipt
          source: purchase_order
          target: purchase_receipt
          require_source_status: approved
          limit_by: qty
          line_selection: by_source_line_id
          header_mapping:
            supplier_id: source.supplier_id
            price_includes_tax: source.price_includes_tax
          line_mapping:
            material_id: source_line.material_id
            unit_id: source_line.unit_id
            price: source_line.price
            tax_rate: source_line.tax_rate
          required_line_inputs: [source_line_id, qty, warehouse_id]
        """,
    )
    conversion = document.conversion
    assert conversion.id == "purchase_order_to_receipt"
    assert (conversion.source, conversion.target) == ("purchase_order", "purchase_receipt")
    assert conversion.require_source_status == "approved"
    assert conversion.limit_by == "qty"
    assert conversion.line_selection == "by_source_line_id"
    assert list(conversion.header_mapping) == ["supplier_id", "price_includes_tax"]
    assert conversion.header_mapping["supplier_id"] == "source.supplier_id"
    assert list(conversion.line_mapping) == ["material_id", "unit_id", "price", "tax_rate"]
    assert conversion.required_line_inputs == ("source_line_id", "qty", "warehouse_id")


@pytest.mark.req("1.1")
def test_conversion_defaults_and_amount_quota() -> None:
    conversion = load(
        ConversionFile,
        """
        conversion:
          id: ap_bill_to_payment
          source: ap_bill
          target: ap_payment
          require_source_status: approved
          limit_by: amount
          required_line_inputs: [source_line_id, amount]
        """,
    ).conversion
    assert conversion.limit_by == "amount"
    assert conversion.line_selection == "by_source_line_id"
    assert (conversion.header_mapping, conversion.line_mapping) == (FrozenMap(), FrozenMap())


@pytest.mark.req("1.1", "1.11")
@pytest.mark.parametrize(
    ("body", "loc", "error_type"),
    [
        ("{ source: a, target: b, require_source_status: approved, limit_by: qty, required_line_inputs: [source_line_id] }", ("id",), "missing"),
        ("{ id: c, target: b, require_source_status: approved, limit_by: qty, required_line_inputs: [source_line_id] }", ("source",), "missing"),
        ("{ id: c, source: a, require_source_status: approved, limit_by: qty, required_line_inputs: [source_line_id] }", ("target",), "missing"),
        ("{ id: c, source: a, target: b, limit_by: qty, required_line_inputs: [source_line_id] }", ("require_source_status",), "missing"),
        ("{ id: c, source: a, target: b, require_source_status: approved, required_line_inputs: [source_line_id] }", ("limit_by",), "missing"),
        ("{ id: c, source: a, target: b, require_source_status: approved, limit_by: qty }", ("required_line_inputs",), "missing"),
        ("{ id: c, source: a, target: b, require_source_status: approved, limit_by: qty, required_line_inputs: [] }", ("required_line_inputs",), "too_short"),
        ("{ id: c, source: a, target: b, require_source_status: done, limit_by: qty, required_line_inputs: [x] }", ("require_source_status",), "literal_error"),
        ("{ id: c, source: a, target: b, require_source_status: approved, limit_by: amount_with_tax, required_line_inputs: [x] }", ("limit_by",), "literal_error"),
        ("{ id: c, source: a, target: b, require_source_status: approved, limit_by: qty, line_selection: by_line_no, required_line_inputs: [x] }", ("line_selection",), "literal_error"),
        ("{ id: c, source: a, target: b, require_source_status: approved, limit_by: qty, required_line_inputs: [x], header_mapping: { a: source_line.b } }", ("header_mapping", "a"), "string_pattern_mismatch"),
        ("{ id: c, source: a, target: b, require_source_status: approved, limit_by: qty, required_line_inputs: [x], line_mapping: { a: source.b } }", ("line_mapping", "a"), "string_pattern_mismatch"),
        ("{ id: c, source: a, target: b, require_source_status: approved, limit_by: qty, required_line_inputs: [x], target_status: draft }", ("target_status",), "extra_forbidden"),
    ],
)  # fmt: skip
def test_conversion_errors(body: str, loc: Loc, error_type: str) -> None:
    assert errors(ConversionFile, f"conversion: {body}\n") == [(("conversion", *loc), error_type)]


@pytest.mark.req("1.1")
def test_the_document_key_locates_the_object() -> None:
    assert errors(ConversionFile, "conversions: []\n") == [
        (("conversion",), "missing"),
        (("conversions",), "extra_forbidden"),
    ]


@pytest.mark.req("1.1")
def test_meta_model_writeback_example() -> None:
    writeback = load(
        WritebackFile,
        """
        writeback:
          id: receipt_approve_writeback_order
          conversion: purchase_order_to_receipt
          on: approve
          target: source_line.linked_qty
          op: add
          value: line.qty_base
          reverse_on: unapprove
          auto_close_source_when: all_lines_linked
        """,
    ).writeback
    assert writeback.id == "receipt_approve_writeback_order"
    assert writeback.conversion == "purchase_order_to_receipt"
    assert writeback.on == "approve"
    assert writeback.target == "source_line.linked_qty"
    assert (writeback.op, writeback.value) == ("add", "line.qty_base")
    assert writeback.reverse_on == "unapprove"
    assert writeback.auto_close_source_when == "all_lines_linked"


@pytest.mark.req("1.1")
def test_writeback_of_a_header_field_without_auto_close() -> None:
    writeback = load(
        WritebackFile,
        """
        writeback:
          id: payment_approve_writeback_bill
          conversion: ap_bill_to_payment
          on: approve
          target: source.linked_amount
          op: add
          value: header.amount_with_tax
        """,
    ).writeback
    assert writeback.target == "source.linked_amount"
    assert writeback.auto_close_source_when is None
    assert writeback.reverse_on == "unapprove"


@pytest.mark.req("1.1", "1.11")
@pytest.mark.parametrize(
    ("body", "loc", "error_type"),
    [
        ("{ conversion: c, on: approve, target: source.x, op: add, value: '1' }", ("id",), "missing"),
        ("{ id: w, on: approve, target: source.x, op: add, value: '1' }", ("conversion",), "missing"),
        ("{ id: w, conversion: c, target: source.x, op: add, value: '1' }", ("on",), "missing"),
        ("{ id: w, conversion: c, on: approve, op: add, value: '1' }", ("target",), "missing"),
        ("{ id: w, conversion: c, on: approve, target: source.x, value: '1' }", ("op",), "missing"),
        ("{ id: w, conversion: c, on: approve, target: source.x, op: add }", ("value",), "missing"),
        ("{ id: w, conversion: c, on: submit, target: source.x, op: add, value: '1' }", ("on",), "literal_error"),
        ("{ id: w, conversion: c, on: approve, target: linked_qty, op: add, value: '1' }", ("target",), "string_pattern_mismatch"),
        ("{ id: w, conversion: c, on: approve, target: source.x, op: subtract, value: '1' }", ("op",), "literal_error"),
        ("{ id: w, conversion: c, on: approve, target: source.x, op: add, value: '' }", ("value",), "string_too_short"),
        ("{ id: w, conversion: c, on: approve, target: source.x, op: add, value: '1', reverse_on: delete }", ("reverse_on",), "literal_error"),
        ("{ id: w, conversion: c, on: approve, target: source.x, op: add, value: '1', auto_close_source_when: all_amount_linked }", ("auto_close_source_when",), "literal_error"),
    ],
)  # fmt: skip
def test_writeback_errors(body: str, loc: Loc, error_type: str) -> None:
    assert errors(WritebackFile, f"writeback: {body}\n") == [(("writeback", *loc), error_type)]


# --- inventory linkage (meta-model.md section 8) -----------------------------------------


@pytest.mark.req("1.1")
def test_meta_model_inventory_linkage_example() -> None:
    linkage = load(
        LinkageFile,
        """
        linkage:
          id: purchase_receipt_inventory
          bill: purchase_receipt
          primitive: inventory
          on: approve
          direction: in
          warehouse_field: line.warehouse_id
          qty_field: line.qty_base
        """,
    ).linkage
    assert linkage.id == "purchase_receipt_inventory"
    assert (linkage.bill, linkage.primitive, linkage.on) == (
        "purchase_receipt",
        "inventory",
        "approve",
    )
    assert linkage.direction == "in"
    assert linkage.warehouse_field == "line.warehouse_id"
    assert linkage.qty_field == "line.qty_base"
    assert (linkage.from_warehouse_field, linkage.to_warehouse_field, linkage.adjust) == (
        None,
        None,
        None,
    )


@pytest.mark.req("9.3")
def test_design_adjust_linkage_example() -> None:
    linkage = load(
        LinkageFile,
        """
        linkage:
          id: stock_adjustment_inventory
          bill: stock_adjustment
          primitive: inventory
          on: approve
          direction: adjust
          warehouse_field: header.warehouse_id
          qty_field: line.qty_base
          adjust: { field: header.reason, increase: [gain], decrease: [loss] }
        """,
    ).linkage
    assert linkage.direction == "adjust"
    assert linkage.warehouse_field == "header.warehouse_id"
    assert linkage.adjust is not None
    assert linkage.adjust.field == "header.reason"
    assert (linkage.adjust.increase, linkage.adjust.decrease) == (("gain",), ("loss",))


@pytest.mark.req("9.3")
def test_transfer_linkage_declares_both_warehouse_fields() -> None:
    linkage = load(
        LinkageFile,
        """
        linkage:
          id: transfer_order_inventory
          bill: transfer_order
          primitive: inventory
          on: approve
          direction: transfer
          from_warehouse_field: header.from_warehouse_id
          to_warehouse_field: header.to_warehouse_id
          qty_field: line.qty_base
        """,
    ).linkage
    assert linkage.from_warehouse_field == "header.from_warehouse_id"
    assert linkage.to_warehouse_field == "header.to_warehouse_id"
    assert linkage.warehouse_field is None


@pytest.mark.req("1.1", "2.6")
@pytest.mark.parametrize(
    ("body", "loc", "error_type"),
    [
        ("{ bill: b, primitive: inventory, on: approve, direction: in, qty_field: line.qty_base }", ("id",), "missing"),
        ("{ id: l, primitive: inventory, on: approve, direction: in, qty_field: line.qty_base }", ("bill",), "missing"),
        ("{ id: l, bill: b, on: approve, direction: in, qty_field: line.qty_base }", ("primitive",), "missing"),
        ("{ id: l, bill: b, primitive: partner_balance, on: approve, direction: in, qty_field: line.qty_base }", ("primitive",), "literal_error"),
        ("{ id: l, bill: b, primitive: inventory, direction: in, qty_field: line.qty_base }", ("on",), "missing"),
        ("{ id: l, bill: b, primitive: inventory, on: approve, qty_field: line.qty_base }", ("direction",), "missing"),
        ("{ id: l, bill: b, primitive: inventory, on: approve, direction: increase, qty_field: line.qty_base }", ("direction",), "literal_error"),
        ("{ id: l, bill: b, primitive: inventory, on: approve, direction: in }", ("qty_field",), "missing"),
        ("{ id: l, bill: b, primitive: inventory, on: approve, direction: in, qty_field: qty_base }", ("qty_field",), "string_pattern_mismatch"),
        ("{ id: l, bill: b, primitive: inventory, on: approve, direction: in, qty_field: line.qty_base, warehouse: line.warehouse_id }", ("warehouse",), "extra_forbidden"),
        ("{ id: l, bill: b, primitive: inventory, on: approve, direction: adjust, qty_field: line.qty_base, adjust: { field: header.reason, increase: [gain] } }", ("adjust", "decrease"), "missing"),
        ("{ id: l, bill: b, primitive: inventory, on: approve, direction: adjust, qty_field: line.qty_base, adjust: { field: header.reason, increase: [], decrease: [loss] } }", ("adjust", "increase"), "too_short"),
        ("{ id: l, bill: b, primitive: inventory, on: approve, direction: adjust, qty_field: line.qty_base, adjust: { field: reason, increase: [gain], decrease: [loss] } }", ("adjust", "field"), "string_pattern_mismatch"),
    ],
)  # fmt: skip
def test_inventory_linkage_errors(body: str, loc: Loc, error_type: str) -> None:
    assert errors(LinkageFile, f"linkage: {body}\n") == [(("linkage", *loc), error_type)]


# --- rules (meta-model.md section 9) -----------------------------------------------------


@pytest.mark.req("1.1")
def test_meta_model_enforced_rule_example() -> None:
    rule = load(
        RuleFile,
        """
        rule:
          id: R-PUR-001
          bill: purchase_order
          enforcement: enforced
          on: [create, update, submit]
          check: "header.delivery_date >= header.biz_date"
          error_code: VALIDATION_FAILED
          field: delivery_date
          message: { zh: 交货日期不得早于业务日期, en: Delivery date must not be earlier than the business date }
        """,
    ).rule
    assert isinstance(rule, EnforcedRuleDef)
    assert rule.id == "R-PUR-001"
    assert rule.bill == "purchase_order"
    assert rule.on == ("create", "update", "submit")
    assert rule.check == "header.delivery_date >= header.biz_date"
    assert rule.error_code == "VALIDATION_FAILED"
    assert rule.field == "delivery_date"
    assert rule.message.zh == "交货日期不得早于业务日期"


@pytest.mark.req("1.1")
def test_meta_model_described_rule_example() -> None:
    rule = load(
        RuleFile,
        """
        rule:
          id: R-INV-002
          enforcement: described
          description: { zh: 库存调整单只用于盘点差异，采购到货必须通过采购入库单入库 }
          applies_to_tools: [stock_adjustment_create]
        """,
    ).rule
    assert isinstance(rule, DescribedRuleDef)
    assert rule.id == "R-INV-002"
    assert rule.description.zh.startswith("库存调整单只用于盘点差异")
    assert rule.applies_to_tools == ("stock_adjustment_create",)


@pytest.mark.req("1.1", "3.8")
def test_described_rule_without_tools_is_reported_at_step_7() -> None:
    rule = load(
        RuleFile,
        """
        rule:
          id: R-AP-001
          enforcement: described
          description: { zh: 已有应付单时，付款应从应付单下推生成 }
        """,
    ).rule
    assert isinstance(rule, DescribedRuleDef)
    assert rule.applies_to_tools == ()


@pytest.mark.req("1.1", "10.7")
def test_rule_error_codes_are_error_codes() -> None:
    document = """
        rule:
          id: R-PUR-002
          bill: purchase_order
          enforcement: enforced
          on: [approve]
          check: "count(lines) > 0"
          error_code: {code}
          message: {{ zh: 不满足 }}
        """
    for code in ("VALIDATION_FAILED", "BUSINESS_RULE_VIOLATION", "INVALID_STATE", "CONFLICT"):
        rule = load(RuleFile, document.format(code=code)).rule
        assert isinstance(rule, EnforcedRuleDef)
        assert ErrorCode(rule.error_code) is ErrorCode[code]
    for code in ("NOT_FOUND", "LIMIT_EXCEEDED", "INTERNAL_ERROR", "WRONG"):
        assert errors(RuleFile, document.format(code=code)) == [
            (("rule", "error_code"), "literal_error")
        ]


@pytest.mark.req("1.1", "1.11")
@pytest.mark.parametrize(
    ("body", "loc", "error_type"),
    [
        ("{ id: R-PUR-001, bill: b, on: [create], check: 'a', error_code: VALIDATION_FAILED, message: { zh: x } }", ("enforcement",), "missing"),
        ("{ id: R-PUR-001, enforcement: warned, bill: b }", ("enforcement",), "literal_error"),
        ("{ id: R-PUR-001, enforcement: enforced, on: [create], check: 'a', error_code: VALIDATION_FAILED, message: { zh: x } }", ("bill",), "missing"),
        ("{ id: R-PUR-001, enforcement: enforced, bill: b, check: 'a', error_code: VALIDATION_FAILED, message: { zh: x } }", ("on",), "missing"),
        ("{ id: R-PUR-001, enforcement: enforced, bill: b, on: [], check: 'a', error_code: VALIDATION_FAILED, message: { zh: x } }", ("on",), "too_short"),
        ("{ id: R-PUR-001, enforcement: enforced, bill: b, on: [search], check: 'a', error_code: VALIDATION_FAILED, message: { zh: x } }", ("on", 0), "literal_error"),
        ("{ id: R-PUR-001, enforcement: enforced, bill: b, on: [create], error_code: VALIDATION_FAILED, message: { zh: x } }", ("check",), "missing"),
        ("{ id: R-PUR-001, enforcement: enforced, bill: b, on: [create], check: 'a', message: { zh: x } }", ("error_code",), "missing"),
        ("{ id: R-PUR-001, enforcement: enforced, bill: b, on: [create], check: 'a', error_code: VALIDATION_FAILED }", ("message",), "missing"),
        ("{ id: R-PUR-001, enforcement: enforced, bill: b, on: [create], check: 'a', error_code: VALIDATION_FAILED, message: { en: x } }", ("message", "zh"), "missing"),
        ("{ id: '', enforcement: described, description: { zh: x }, applies_to_tools: [t] }", ("id",), "string_too_short"),
        ("{ id: R-INV-002, enforcement: described, applies_to_tools: [t] }", ("description",), "missing"),
        ("{ id: R-INV-002, enforcement: described, description: { zh: x }, bill: b }", ("bill",), "extra_forbidden"),
        ("{ id: R-INV-002, enforcement: described, description: { zh: x }, applies_to_tools: [Tool] }", ("applies_to_tools", 0), "string_pattern_mismatch"),
    ],
)  # fmt: skip
def test_rule_errors(body: str, loc: Loc, error_type: str) -> None:
    assert errors(RuleFile, f"rule: {body}\n") == [(("rule", *loc), error_type)]


@pytest.mark.req("1.1")
def test_rule_id_format_is_checked_at_step_5() -> None:
    # Step 5 compares the number with the pack's rule_prefix (requirement 2.8), so any
    # non-empty text is accepted here.
    rule = load(
        RuleFile,
        "rule: { id: PUR-1, enforcement: described, description: { zh: x } }\n",
    ).rule
    assert rule.id == "PUR-1"


@pytest.mark.req("1.1")
def test_rule_must_be_a_mapping() -> None:
    assert errors(RuleFile, "rule: R-PUR-001\n") == [(("rule",), "dict_type")]


# --- tool exposure (tool-design.md section 2) --------------------------------------------


@pytest.mark.req("1.1", "11.13")
def test_tool_design_exposure_example() -> None:
    exposure = load(
        ExposureDef,
        """
        pack: purchase
        exclude: []
        rename: {}
        source_short_names:
          purchase_requisition: requisition
          purchase_order: order
        queries:
          - tool: purchase_price_query
            source: purchase_order_line
            params:
              supplier_id: { type: ref, ref: supplier, required: true, maps_to: header.supplier_id }
              material_id: { type: ref, ref: material, required: true, maps_to: line.material_id }
            filter: "header.status in ['approved', 'closed']"
            sort: [header.biz_date desc, header.number desc]
            returns:
              latest: [line.price, header.price_includes_tax, header.biz_date, header.number]
              history: { fields: [line.price, header.biz_date, header.number], limit: 5 }
        """,
    )
    assert exposure.pack == "purchase"
    assert (exposure.exclude, exposure.rename) == ((), FrozenMap())
    assert list(exposure.source_short_names) == ["purchase_requisition", "purchase_order"]
    assert exposure.source_short_names["purchase_order"] == "order"
    (query,) = exposure.queries
    assert isinstance(query, DeclarativeQueryDef)
    assert (query.tool, query.source) == ("purchase_price_query", "purchase_order_line")
    assert list(query.params) == ["supplier_id", "material_id"]
    supplier = query.params["supplier_id"]
    assert (supplier.type, supplier.ref, supplier.required) == ("ref", "supplier", True)
    assert supplier.maps_to == "header.supplier_id"
    assert query.filter == "header.status in ['approved', 'closed']"
    assert query.sort == ("header.biz_date desc", "header.number desc")
    assert query.returns.latest[0] == "line.price"
    assert query.returns.history is not None
    assert (query.returns.history.fields[0], query.returns.history.limit) == ("line.price", 5)


@pytest.mark.req("1.1", "2.6")
def test_design_primitive_query_exposure_example() -> None:
    exposure = load(
        ExposureDef,
        """
        pack: inventory
        exclude: [stock_adjustment_submit, stock_adjustment_approve]
        queries:
          - { tool: inventory_balance_query, primitive: inventory, query: balance }
        """,
    )
    assert exposure.exclude == ("stock_adjustment_submit", "stock_adjustment_approve")
    (query,) = exposure.queries
    assert isinstance(query, PrimitiveQueryDef)
    assert (query.tool, query.primitive, query.query) == (
        "inventory_balance_query",
        "inventory",
        "balance",
    )


@pytest.mark.req("1.1")
def test_exposure_defaults_and_rename() -> None:
    exposure = load(
        ExposureDef,
        """
        pack: purchase
        rename: { purchase_order_search: purchase_order_list_search }
        """,
    )
    assert exposure.queries == ()
    assert exposure.source_short_names == FrozenMap()
    assert exposure.rename["purchase_order_search"] == "purchase_order_list_search"


@pytest.mark.req("1.1", "1.11")
@pytest.mark.parametrize(
    ("document", "loc", "error_type"),
    [
        ("exclude: []\n", ("pack",), "missing"),
        ("pack: purchase\nexclude: {}\n", ("exclude",), "tuple_type"),
        ("pack: purchase\nrename: [a]\n", ("rename",), "dict_type"),
        ("pack: purchase\nqueries: [{ primitive: inventory, query: balance }]\n", ("queries", 0, "tool"), "missing"),
        ("pack: purchase\nqueries: [{ tool: t, primitive: write_off, query: balance }]\n", ("queries", 0, "primitive"), "literal_error"),
        ("pack: purchase\nqueries: [{ tool: t, primitive: inventory }]\n", ("queries", 0, "query"), "missing"),
        ("pack: purchase\nqueries: [{ tool: t, primitive: inventory, query: balance, source: s }]\n", ("queries", 0, "source"), "extra_forbidden"),
        ("pack: purchase\nqueries: [{ tool: t, returns: { latest: [line.price] } }]\n", ("queries", 0, "source"), "missing"),
        ("pack: purchase\nqueries: [{ tool: t, source: s }]\n", ("queries", 0, "returns"), "missing"),
        ("pack: purchase\nqueries: [{ tool: t, source: s, returns: { latest: [] } }]\n", ("queries", 0, "returns", "latest"), "too_short"),
        ("pack: purchase\nqueries: [{ tool: t, source: s, returns: { latest: [price] } }]\n", ("queries", 0, "returns", "latest", 0), "string_pattern_mismatch"),
        ("pack: purchase\nqueries: [{ tool: t, source: s, returns: { latest: [line.price], history: { fields: [line.price] } } }]\n", ("queries", 0, "returns", "history", "limit"), "missing"),
        ("pack: purchase\nqueries: [{ tool: t, source: s, returns: { latest: [line.price], history: { fields: [line.price], limit: 0 } } }]\n", ("queries", 0, "returns", "history", "limit"), "greater_than_equal"),
        ("pack: purchase\nqueries: [{ tool: t, source: s, sort: [price desc], returns: { latest: [line.price] } }]\n", ("queries", 0, "sort", 0), "string_pattern_mismatch"),
        ("pack: purchase\nqueries: [{ tool: t, source: s, filter: '', returns: { latest: [line.price] } }]\n", ("queries", 0, "filter"), "string_too_short"),
        ("pack: purchase\nqueries: [t]\n", ("queries", 0), "dict_type"),
    ],
)  # fmt: skip
def test_exposure_errors(document: str, loc: Loc, error_type: str) -> None:
    assert errors(ExposureDef, document) == [(loc, error_type)]


@pytest.mark.req("1.1", "1.12")
@pytest.mark.parametrize(
    ("param", "loc", "error_type"),
    [
        ("{ type: ref, maps_to: header.supplier_id }", ("ref",), "missing"),
        ("{ type: string, ref: supplier, maps_to: header.name }", ("ref",), "extra_forbidden"),
        ("{ type: ref, ref: supplier }", ("maps_to",), "missing"),
        ("{ ref: supplier, maps_to: header.supplier_id }", ("type",), "missing"),
        ("{ type: money2, maps_to: header.amount }", ("type",), "literal_error"),
        ("{ type: date, maps_to: biz_date }", ("maps_to",), "string_pattern_mismatch"),
        ("{ type: date, maps_to: header.biz_date, default: today }", ("default",), "extra_forbidden"),
    ],
)  # fmt: skip
def test_query_param_errors(param: str, loc: Loc, error_type: str) -> None:
    document = (
        "pack: purchase\n"
        "queries:\n"
        "  - tool: t\n"
        "    source: s\n"
        f"    params: {{ p: {param} }}\n"
        "    returns: { latest: [line.price] }\n"
    )
    assert errors(ExposureDef, document) == [(("queries", 0, "params", "p", *loc), error_type)]


# --- tool descriptions (requirement 3) ---------------------------------------------------


@pytest.mark.req("1.1", "3.2")
def test_design_description_file_example() -> None:
    document = load(
        DescriptionFile,
        """
        variant: zh-detailed
        tools:
          purchase_order_create:
            description: |-
              新建采购订单草稿……
            params:
              supplier_id: 供应商 ID，由 supplier_search 返回
              lines: 订单分录，至少 1 行
              lines[].qty: 数量，必须大于 0，单位见 unit_id
        """,
    )
    assert document.variant == "zh-detailed"
    assert list(document.tools) == ["purchase_order_create"]
    tool = document.tools["purchase_order_create"]
    assert tool.description == "新建采购订单草稿……"
    assert list(tool.params) == ["supplier_id", "lines", "lines[].qty"]
    assert tool.params["lines[].qty"].startswith("数量")


@pytest.mark.req("1.1", "3.1", "3.2")
def test_description_file_leaves_the_variant_and_the_texts_to_step_7() -> None:
    assert DESCRIPTION_VARIANTS == ("zh-detailed", "zh-brief", "en-detailed", "en-brief")
    # An unknown variant, an empty description and a tool without params are reported at
    # step 7, together with the other description errors.
    document = load(
        DescriptionFile,
        """
        variant: zh-plain
        tools:
          purchase_order_create: { description: "  " }
          purchase_order_get: {}
        """,
    )
    assert document.variant == "zh-plain"
    assert document.tools["purchase_order_create"].description == "  "
    assert document.tools["purchase_order_get"].description == ""
    assert document.tools["purchase_order_get"].params == FrozenMap()


@pytest.mark.req("1.1", "3.2")
@pytest.mark.parametrize(
    ("document", "loc", "error_type"),
    [
        ("tools: {}\n", ("variant",), "missing"),
        ("variant: ''\ntools: {}\n", ("variant",), "string_too_short"),
        ("variant: zh-detailed\ntools: { t: { text: x } }\n", ("tools", "t", "text"), "extra_forbidden"),
        ("variant: zh-detailed\ntools: { t: 描述 }\n", ("tools", "t"), "model_type"),
        ("variant: zh-detailed\ntools: { t: { description: 1 } }\n", ("tools", "t", "description"), "string_type"),
        ("variant: zh-detailed\ntools: { t: { params: { 'lines[]': x } } }\n", ("tools", "t", "params", "lines[]", "[key]"), "string_pattern_mismatch"),
        ("variant: zh-detailed\ntools: { t: { params: { 'lines[].qty.x': y } } }\n", ("tools", "t", "params", "lines[].qty.x", "[key]"), "string_pattern_mismatch"),
        ("variant: zh-detailed\ntools: { t: { params: { qty: 0 } } }\n", ("tools", "t", "params", "qty"), "string_type"),
    ],
)  # fmt: skip
def test_description_file_errors(document: str, loc: Loc, error_type: str) -> None:
    assert errors(DescriptionFile, document) == [(loc, error_type)]


# --- seed configuration (requirement 20) -------------------------------------------------


@pytest.mark.req("1.1", "20.1")
def test_seed_generators_file() -> None:
    document = load(
        SeedGeneratorsFile,
        """
        generators:
          supplier.name:              { gen: company_name, industry: 机械 }
          supplier.short_name:        { gen: short_name }
          supplier.payment_term_days: { gen: int_range, min: 30, max: 90 }
          material.name:              { gen: code_words, words: [不锈钢, 螺栓] }
          material.standard_cost:     { gen: decimal_range, min: "0.70", max: "0.95" }
          material.kind:              { gen: choice, values: [raw, finished] }
          employee.name:              { gen: person_name }
        """,
    )
    generators = document.generators
    assert list(generators) == [
        "supplier.name",
        "supplier.short_name",
        "supplier.payment_term_days",
        "material.name",
        "material.standard_cost",
        "material.kind",
        "employee.name",
    ]
    company = generators["supplier.name"]
    assert isinstance(company, CompanyNameGenerator)
    assert company.industry == "机械"
    assert isinstance(generators["supplier.short_name"], ShortNameGenerator)
    term = generators["supplier.payment_term_days"]
    assert isinstance(term, IntRangeGenerator)
    assert (term.min, term.max) == (30, 90)
    words = generators["material.name"]
    assert isinstance(words, CodeWordsGenerator)
    assert words.words == ("不锈钢", "螺栓")
    cost = generators["material.standard_cost"]
    assert isinstance(cost, DecimalRangeGenerator)
    assert (cost.min, cost.max) == ("0.70", "0.95")
    kind = generators["material.kind"]
    assert isinstance(kind, ChoiceGenerator)
    assert kind.values == ("raw", "finished")
    assert isinstance(generators["employee.name"], PersonNameGenerator)


@pytest.mark.req("1.1", "20.1")
def test_generators_are_the_builtin_ones() -> None:
    assert GENERATORS == (
        "company_name",
        "short_name",
        "person_name",
        "choice",
        "int_range",
        "decimal_range",
        "code_words",
    )
    assert load(SeedGeneratorsFile, "generators: {}\n").generators == FrozenMap()


@pytest.mark.req("1.1", "20.1")
@pytest.mark.parametrize(
    ("document", "loc", "error_type"),
    [
        ("generators: { name: { gen: person_name } }\n", ("generators", "name", "[key]"), "string_pattern_mismatch"),
        ("generators: { supplier.name: { generator: person_name } }\n", ("generators", "supplier.name", "gen"), "missing"),
        ("generators: { supplier.name: { gen: full_name } }\n", ("generators", "supplier.name", "gen"), "literal_error"),
        ("generators: { supplier.name: { gen: person_name, industry: 机械 } }\n", ("generators", "supplier.name", "industry"), "extra_forbidden"),
        ("generators: { supplier.name: { gen: choice } }\n", ("generators", "supplier.name", "values"), "missing"),
        ("generators: { supplier.name: { gen: choice, values: [] } }\n", ("generators", "supplier.name", "values"), "too_short"),
        ("generators: { supplier.name: { gen: choice, values: [0.5] } }\n", ("generators", "supplier.name", "values", 0), "scalar_type"),
        ("generators: { supplier.name: { gen: int_range, min: 1 } }\n", ("generators", "supplier.name", "max"), "missing"),
        ("generators: { supplier.name: { gen: int_range, min: '1', max: 9 } }\n", ("generators", "supplier.name", "min"), "int_type"),
        ("generators: { supplier.name: { gen: decimal_range, min: 0.7, max: 0.95 } }\n", ("generators", "supplier.name", "min"), "string_type"),
        ("generators: { supplier.name: { gen: code_words, words: [] } }\n", ("generators", "supplier.name", "words"), "too_short"),
        ("generators: { supplier.name: company_name }\n", ("generators", "supplier.name"), "dict_type"),
        ("bindings: {}\n", ("bindings",), "extra_forbidden"),
    ],
)  # fmt: skip
def test_seed_generators_errors(document: str, loc: Loc, error_type: str) -> None:
    found = errors(SeedGeneratorsFile, document)
    assert (loc, error_type) in found


@pytest.mark.req("1.1", "20.1")
def test_shared_data_file() -> None:
    document = load(
        SharedDataFile,
        """
        records:
          - { kind: org, code: ORG01, name: 示例集团, is_default: true }
          - kind: warehouse
            code: WH01
            name: 原料仓
            status: enabled
          - { kind: unit_conversion, material: M00001, from_unit: BOX, factor: "100" }
        """,
    )
    assert len(document.records) == 3
    org, warehouse, conversion = document.records
    assert org["kind"] == "org"
    assert org["is_default"] is True
    assert list(warehouse) == ["kind", "code", "name", "status"]
    # Decimals keep their exact value as text, as everywhere in a configuration.
    assert conversion["factor"] == "100"


@pytest.mark.req("1.1", "20.1")
@pytest.mark.parametrize(
    ("document", "loc", "error_type"),
    [
        ("records: [{ code: ORG01 }]\n", ("records", 0, "kind"), "missing"),
        ("records: [{ kind: Org }]\n", ("records", 0, "kind"), "string_pattern_mismatch"),
        ("records: [{ kind: 1 }]\n", ("records", 0, "kind"), "string_type"),
        ("records: [{ kind: org, Code: ORG01 }]\n", ("records", 0, "Code", "[key]"), "string_pattern_mismatch"),
        ("records: [{ kind: org, factor: 0.13 }]\n", ("records", 0, "factor"), "scalar_type"),
        ("records: [org]\n", ("records", 0), "dict_type"),
        ("records: {}\n", ("records",), "tuple_type"),
        ("rows: []\n", ("records",), "missing"),
    ],
)  # fmt: skip
def test_shared_data_errors(document: str, loc: Loc, error_type: str) -> None:
    found = errors(SharedDataFile, document)
    assert (loc, error_type) in found


# --- read-only objects and JSON Schema ---------------------------------------------------


@pytest.mark.req("1.1")
def test_objects_are_frozen() -> None:
    entity = load(EntityDef, "entity: unit\nid_prefix: unit\nfields: {}\n")
    with pytest.raises(ValidationError):
        entity.entity = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        entity.fields["name"] = StringField(type="string")  # type: ignore[index]


@pytest.mark.req("1.1")
def test_json_schema_of_every_root_model() -> None:
    schema = BillDef.model_json_schema(mode="validation")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["state_machine"]["enum"] == ["standard", "simple"]
    assert "state_machine" in schema["required"]
    manifest = PackManifest.model_json_schema(mode="validation")
    assert manifest["properties"]["version"] == {
        "type": "string",
        "pattern": r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$",
        "title": "Version",
    }
    # A map with patterned keys closes its other keys, as a NameMap does.
    params = DescriptionFile.model_json_schema()["$defs"]["ToolDescription"]["properties"]["params"]
    assert params["additionalProperties"] is False
    assert list(params["patternProperties"]) == [r"^[a-z][a-z0-9_]*(?:\[\]\.[a-z][a-z0-9_]*)?$"]
    # The rule document is the union of the two rule models.
    rule = RuleFile.model_json_schema()["properties"]["rule"]["anyOf"]
    assert [choice["$ref"].removeprefix("#/$defs/") for choice in rule] == [
        "EnforcedRuleDef",
        "DescribedRuleDef",
    ]
