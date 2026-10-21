"""erp_mock.loader.step5_model: the field view and the references between packs.

Two kinds of test, after the two things step 5 builds:

- the field view (requirements 1.3 to 1.5) is read off the engine test fixture, which
  declares both state machines, pricing, quantity semantics without pricing and both kinds
  of push, so one real configuration shows every standard field the engine adds and which
  of them a write tool may write;
- the references (requirement 2.9) are checked on trees written below ``tmp_path`` by
  tests.helpers.tree, each with exactly the one name that cannot be resolved, and asserted
  by the place the loader reports - the (pack, file, config path) that points the author of
  the pack at the item that writes it.

Step 5 is run through :class:`~erp_mock.loader.loader.Loader`, the way loading runs it, so
steps 1 to 4 run first; what those steps settle is covered by their own tests.
"""

import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import (
    ENTITY_STATUSES,
    SIMPLE_STATUSES,
    STANDARD_STATUSES,
    DomainModel,
    EntityModel,
    FieldModel,
    entity_standard_view,
    header_standard_view,
    line_standard_view,
)
from erp_mock.config.pack import (
    entity_standard_fields,
    header_standard_fields,
    line_standard_fields,
)
from erp_mock.loader.errors import ConfigError, ConfigErrors
from erp_mock.loader.loader import Loader
from tests.helpers.fixtures import fixture_domain
from tests.helpers.tree import (
    CONTEXT_FILES,
    PACK_KEYS,
    bill_doc,
    dataset_doc,
    entity_doc,
    profile_doc,
    write_instance,
    write_pack,
)

INSTANCE_DIR = "instance"
PACKS_DIR = "packs"

# The pack the default instance.yaml of tests.helpers.tree references, and a second one a
# test adds to it for the references between two packs.
PACK = "core"
OTHER = "purchase"

# One place of a reported error: the pack, the file and the config path.
type Place = tuple[str | None, str, str | None]

# The definitions the default instance.yaml names, both valid.
DEFINITIONS: Final[Mapping[str, str]] = {
    "profiles/semantic.yaml": profile_doc(),
    "datasets/purchase-basic.yaml": dataset_doc(),
}

# The writable standard fields: no other standard field may be written by a write tool
# (requirement 1.3).
WRITABLE_HEADER: Final = frozenset({"biz_date", "remark"})
WRITABLE_PRICING_HEADER: Final = frozenset({"price_includes_tax"})
WRITABLE_PRICING_LINE: Final = frozenset({"qty", "price", "tax_rate"})


def doc(text: str) -> str:
    """A YAML document written indented in the test source."""
    return textwrap.dedent(text).lstrip("\n")


def standard(fields: Mapping[str, FieldModel]) -> list[str]:
    """The names of the standard fields of one level, in view order."""
    return [field.name for field in fields.values() if field.origin == "standard"]


def declared(fields: Mapping[str, FieldModel]) -> list[str]:
    """The names of the declared fields of one level, in view order."""
    return [field.name for field in fields.values() if field.origin == "declared"]


def writable(fields: Mapping[str, FieldModel]) -> set[str]:
    """The names of the writable standard fields of one level."""
    return {
        field.name for field in fields.values() if field.origin == "standard" and field.writable
    }


# --- the field view of the fixture (requirements 1.3 to 1.5) -----------------------------


@pytest.mark.req("1.3")
def test_a_bill_header_holds_the_standard_fields_then_the_declared_ones() -> None:
    bill = fixture_domain().bills["supply_order"]

    assert standard(bill.header) == [
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
        # Pricing follows the fields of every bill (meta-model.md section 4).
        "price_includes_tax",
        "amount",
        "tax_amount",
        "amount_with_tax",
    ]
    # price_includes_tax is declared by the pack as a same-name declaration, so it stays a
    # standard field at its standard place and adds no declared field (requirement 1.5).
    assert declared(bill.header) == ["vendor_id", "need_date"]
    # What the pack wrote, and in which order, is kept next to the view: the same-name
    # declaration is in it, at the place the pack wrote it, which is what the parameter
    # order of a generated tool is built from (requirement 11.4).
    assert bill.header_declared_fields == ("vendor_id", "need_date", "price_includes_tax")


@pytest.mark.req("1.3")
def test_the_lines_of_a_pricing_bill_hold_the_quantity_and_amount_fields() -> None:
    bill = fixture_domain().bills["supply_order"]

    assert standard(bill.lines) == [
        "id",
        "line_no",
        "source_line_id",
        "qty",
        "qty_base",
        "price",
        "tax_rate",
        "amount",
        "tax_amount",
        "amount_with_tax",
        # The bill is the source of a push whose limit_by is qty (requirement 1.3).
        "linked_qty",
    ]
    assert declared(bill.lines) == ["article_id", "measure_id"]
    # tax_rate is a same-name declaration of the standard field (requirement 1.5).
    assert bill.line_declared_fields == ("article_id", "measure_id", "tax_rate")


@pytest.mark.req("1.3")
def test_only_six_standard_fields_can_be_written_by_a_write_tool() -> None:
    domain = fixture_domain()

    for bill in domain.bills.values():
        header = WRITABLE_HEADER | (WRITABLE_PRICING_HEADER if bill.pricing else frozenset())
        lines = WRITABLE_PRICING_LINE if bill.pricing else frozenset()
        assert writable(bill.header) == header, bill.name
        assert writable(bill.lines) == lines, bill.name
    # Every other standard field is read-only, and the values the engine derives from the
    # rest of the bill are marked computed.
    order = domain.bills["supply_order"]
    assert [field.name for field in order.lines.values() if field.computed] == [
        "qty_base",
        "amount",
        "tax_amount",
        "amount_with_tax",
        "linked_qty",
    ]
    assert [field.name for field in order.header.values() if field.computed] == [
        "amount",
        "tax_amount",
        "amount_with_tax",
    ]


@pytest.mark.req("1.4")
def test_lines_that_declare_quantity_semantics_without_pricing_get_qty_base() -> None:
    bill = fixture_domain().bills["goods_entry"]

    # No pricing, so neither qty nor the amounts; qty_base alone is added, read-only, and
    # the quantity it is computed from is the declared field (requirements 1.4 and 6.6).
    assert standard(bill.lines) == ["id", "line_no", "source_line_id", "qty_base"]
    assert (bill.pricing, bill.qty_field) == (False, "entry_qty")
    qty_base = bill.lines["qty_base"]
    assert (qty_base.type, qty_base.writable, qty_base.computed) == ("qty", False, True)
    assert writable(bill.header) == WRITABLE_HEADER


@pytest.mark.req("1.4")
def test_a_bill_without_quantity_semantics_has_no_quantity_field() -> None:
    bill = fixture_domain().bills["fee_note"]

    assert (bill.line_quantity, bill.qty_field) == (None, None)
    # Pricing brings qty_base of its own, as meta-model.md section 4 lists it.
    assert "qty_base" in bill.lines
    # A simple bill has two states, a standard one four (meta-model.md section 6).
    assert bill.statuses == SIMPLE_STATUSES == ("draft", "approved")
    assert tuple(bill.header["status"].enum_values) == SIMPLE_STATUSES
    order = fixture_domain().bills["supply_order"]
    assert tuple(order.header["status"].enum_values) == STANDARD_STATUSES


@pytest.mark.req("1.5")
def test_a_same_name_declaration_gives_a_writable_standard_field_its_defaults() -> None:
    bill = fixture_domain().bills["supply_order"]

    # price_includes_tax: { label: ..., default: false } (requirement 1.5).
    includes_tax = bill.header["price_includes_tax"]
    assert (includes_tax.origin, includes_tax.type, includes_tax.writable) == (
        "standard",
        "bool",
        True,
    )
    assert includes_tax.default is False
    assert includes_tax.label is not None and includes_tax.label.zh == "单价含税"
    # tax_rate: { label: ..., default_from: article.tax_rate }: the ref field of the same
    # level and the field of what it refers to are both resolved (requirement 2.9).
    tax_rate = bill.lines["tax_rate"]
    assert (tax_rate.origin, tax_rate.type) == ("standard", "rate")
    assert tax_rate.default_from is not None
    assert (tax_rate.default_from.ref_field, tax_rate.default_from.entity) == (
        "article_id",
        "article",
    )
    assert tax_rate.default_from.field == "tax_rate"


@pytest.mark.req("1.3")
def test_the_engine_fills_the_standard_fields_it_has_a_default_for() -> None:
    domain = fixture_domain()
    bill = domain.bills["goods_entry"]

    # The anchor date and the default organization (requirement 5.1), and the price
    # including tax when the pack declares nothing (requirement 6.3).
    assert bill.header["biz_date"].default_context == "today"
    assert bill.header["org_id"].default_context == "org"
    assert domain.bills["fee_note"].header["price_includes_tax"].default is True
    # org_id refers to the one organization context entity of the instance.
    assert bill.header["org_id"].ref == "branch"
    assert domain.org_entity is not None and domain.org_entity.name == "branch"
    assert domain.user_entity is not None and domain.user_entity.name == "operator"


@pytest.mark.req("1.3")
def test_a_push_source_gets_the_link_field_of_the_push() -> None:
    domain = fixture_domain()

    # limit_by qty writes back to the source line, limit_by amount to the source header
    # (meta-model.md section 7); a bill that is no source of a push gets neither.
    order = domain.bills["supply_order"]
    fee = domain.bills["fee_note"]
    assert order.lines["linked_qty"].type == "qty"
    assert "linked_amount" not in order.header
    assert fee.header["linked_amount"].type == "money"
    assert "linked_qty" not in fee.lines
    assert "linked_qty" not in domain.bills["goods_entry"].lines
    # The conversions and their writebacks name the same field (requirement 8.8).
    assert domain.conversions["supply_order_to_entry"].link_field.name == "linked_qty"
    assert domain.conversions["supply_order_to_entry"].link_field.level == "line"
    assert domain.writebacks["entry_writeback_order"].target.name == "linked_qty"
    assert domain.conversions["fee_note_to_pay_slip"].link_field.level == "header"
    assert domain.writebacks["pay_writeback_fee"].target.name == "linked_amount"


@pytest.mark.req("1.3")
def test_an_entity_gets_id_and_the_code_and_status_it_declares() -> None:
    domain = fixture_domain()

    # A code rule means the engine generates the code, so it is a read-only standard field
    # (requirement 4.3); has_status adds the one writable standard field of an entity.
    vendor = domain.entities["vendor"]
    assert standard(vendor.fields) == ["id", "code", "status"]
    assert (vendor.fields["code"].writable, vendor.fields["status"].writable) == (False, True)
    assert vendor.fields["status"].default == "enabled"
    assert tuple(vendor.fields["status"].enum_values) == ENTITY_STATUSES == ("enabled", "disabled")
    # An entity without a code rule writes the code itself, as a declared field.
    measure = domain.entities["measure"]
    assert standard(measure.fields) == ["id"]
    assert declared(measure.fields) == ["code", "name"]
    assert measure.declared_fields == ("code", "name")
    assert measure.fields["code"].unique is True


@pytest.mark.req("1.3")
def test_a_declared_field_keeps_what_the_pack_declared() -> None:
    article = fixture_domain().entities["article"]

    name = article.fields["name"]
    assert (name.origin, name.type, name.writable, name.required) == (
        "declared",
        "string",
        True,
        True,
    )
    # maxLength of a string field that declares none (meta-model.md section 3).
    assert name.max_length == 100
    assert tuple(article.fields["grade"].enum_values) == ("plain", "fine")
    assert article.fields["tax_rate"].values == ("0", "0.06", "0.09", "0.13")
    assert article.fields["base_measure_id"].ref == "measure"


@pytest.mark.req("1.3")
def test_a_result_returns_the_ref_fields_of_a_level_as_nested_objects() -> None:
    # The fields a result nests under the name without _id are exactly the ref fields,
    # source_bill_id included; source_line_id keeps its name (requirement 13.3). The two
    # modules therefore agree on which standard field is a reference.
    header = {field.name: field.nested for field in header_standard_view(pricing=True)}
    lines = {
        field.name: field.nested for field in line_standard_view(pricing=True, line_quantity=True)
    }
    record = {
        field.name: field.nested for field in entity_standard_view(code=True, has_status=True)
    }
    assert header == dict(header_standard_fields(pricing=True))
    assert lines == dict(line_standard_fields(pricing=True, line_quantity=True))
    assert record == dict(entity_standard_fields(code=True, has_status=True))


# --- the other objects of the fixture resolve (requirement 2.9) --------------------------


@pytest.mark.req("2.9")
def test_every_reference_of_the_fixture_resolves() -> None:
    domain = fixture_domain()

    # One object of each kind, with the names the documents write resolved into the model.
    conversion = domain.conversions["supply_order_to_entry"]
    assert (conversion.source, conversion.target) == ("supply_order", "goods_entry")
    assert dict(conversion.line_mapping) == {
        "article_id": "article_id",
        "measure_id": "measure_id",
    }
    assert conversion.required_line_inputs == ("source_line_id", "entry_qty", "depot_id")
    linkage = domain.linkages["stock_move_inventory"]
    assert linkage.from_warehouse is not None and linkage.to_warehouse is not None
    assert (linkage.from_warehouse.level, linkage.from_warehouse.name) == (
        "header",
        "from_depot_id",
    )
    assert linkage.qty is not None and linkage.qty.name == "qty_base"
    adjust = domain.linkages["count_sheet_inventory"]
    assert adjust.adjust_field is not None and adjust.adjust_field.name == "count_kind"
    rule = domain.rules["R-FXT-001"]
    assert rule.field is not None and (rule.field.level, rule.field.name) == (
        "header",
        "to_depot_id",
    )
    query = domain.queries["supply_price_query"]
    assert (query.source, query.source_level) == ("supply_order", "line")
    assert query.params["vendor_id"].maps_to.level == "header"
    assert query.params["article_id"].maps_to.level == "line"
    assert [field.name for field in query.latest] == [
        "price",
        "price_includes_tax",
        "biz_date",
        "number",
    ]
    assert domain.queries["stock_level_query"].primitive == "inventory"
    # The trade pack reaches the entities of the base pack through its depends_on.
    assert domain.bills["goods_entry"].lines["article_id"].ref == "article"
    assert domain.entities["article"].unit_conversion is not None
    assert domain.entities["article"].unit_conversion.unit_entity == "measure"


@pytest.mark.req("2.9")
def test_the_model_keeps_the_pack_and_the_file_of_every_object() -> None:
    domain = fixture_domain()

    assert (domain.entities["article"].pack, domain.entities["article"].file) == (
        "fx_base",
        "entities/article.yaml",
    )
    assert domain.bills["supply_order"].pack == "fx_trade"
    assert domain.conversions["supply_order_to_entry"].file == (
        "conversions/supply_order_to_entry.yaml"
    )
    assert domain.queries["stock_level_query"].file == "tools/exposure.yaml"


# --- references that do not resolve (requirement 2.9) ------------------------------------


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


def pack_errors(
    tmp_path: Path, files: Mapping[str, str], id_prefixes: str = PACK_KEYS["id_prefixes"]
) -> tuple[ConfigError, ...]:
    """The step 5 errors of a pack ``core`` holding ``files``, next to a valid instance.

    The pack also holds the two context entities of the instance and states ``id_prefixes``
    as its ID namespaces, so that the other parts of step 5 (``loader.step5_namespace``)
    report nothing and the errors are the ones of the reference the test wrote.
    """
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={**CONTEXT_FILES, **files},
        id_prefixes=id_prefixes,
    )
    return failures(loaded(tmp_path))


@pytest.mark.req("2.9")
def test_a_ref_field_whose_target_entity_does_not_exist(tmp_path: Path) -> None:
    found = pack_errors(tmp_path, {"entities/purchase_order.yaml": bill_doc()})

    # The bill refers to a supplier and a material, neither of which is declared.
    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "header.supplier_id.ref"),
        (PACK, "entities/purchase_order.yaml", "lines.material_id.ref"),
    ]
    assert reasons(found) == [
        "引用的实体或单据 supplier 不存在",
        "引用的实体或单据 material 不存在",
    ]


def write_two_packs(tmp_path: Path, depends_on: str | None = None) -> Loader:
    """An instance of two packs, the second one referring to an entity of the first.

    ``depends_on`` is the manifest item of the second pack, which is how a test lets it
    reach that entity (requirement 2.9).
    """
    packs = f"{{ {PACK}: 1.0.0, {OTHER}: 1.0.0 }}"
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS, packs=packs)
    # Each pack states the namespaces of its own documents alone, since a prefix in two
    # manifests is a conflict of its own (requirement 2.7).
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        id_prefixes="[sup, orgu, emp]",
        number_prefixes="[]",
        files={**CONTEXT_FILES, "entities/supplier.yaml": entity_doc()},
    )
    write_pack(
        tmp_path / PACKS_DIR,
        OTHER,
        rule_prefix="PUR",
        id_prefixes="[po]",
        tool_prefixes="[purchase_]",
        depends_on=depends_on,
        files={"entities/purchase_order.yaml": bill_doc(lines="{ note: { type: string } }")},
    )
    return loaded(tmp_path)


@pytest.mark.req("2.9")
def test_a_reference_to_a_pack_that_is_not_a_direct_dependency(tmp_path: Path) -> None:
    found = failures(write_two_packs(tmp_path))

    assert places(found) == [(OTHER, "entities/purchase_order.yaml", "header.supplier_id.ref")]
    assert reasons(found) == [
        "引用的实体或单据 supplier 由业务包 core 声明，本包的 depends_on 没有直接声明对它的依赖"
    ]


@pytest.mark.req("2.9")
def test_a_reference_to_a_pack_listed_in_depends_on_resolves(tmp_path: Path) -> None:
    target = write_two_packs(tmp_path, depends_on=f"{{ {PACK}: '>=1.0.0 <2.0.0' }}")

    target.run_step(5)

    domain = target.domain
    assert domain is not None
    assert domain.bills["purchase_order"].header["supplier_id"].ref == "supplier"


@pytest.mark.req("2.9")
def test_a_default_from_whose_ref_field_or_field_does_not_exist(tmp_path: Path) -> None:
    found = pack_errors(
        tmp_path,
        {
            "entities/supplier.yaml": entity_doc(
                fields=(
                    "{ name: { type: string },"
                    " contact: { type: string, default_from: owner.name },"
                    " note: { type: string, default_from: parent.missing },"
                    " parent_id: { type: ref, ref: supplier } }"
                ),
            ),
        },
    )

    # The first path names no ref field of the level, the second one names a field the
    # referenced entity does not have.
    assert places(found) == [
        (PACK, "entities/supplier.yaml", "fields.contact.default_from"),
        (PACK, "entities/supplier.yaml", "fields.note.default_from"),
    ]
    assert reasons(found) == [
        "default_from 的 owner 不是本层的 ref 字段 owner_id",
        "实体 supplier 没有字段 missing",
    ]


@pytest.mark.req("2.9")
def test_a_search_a_partner_and_quantity_semantics_that_name_unknown_fields(
    tmp_path: Path,
) -> None:
    found = pack_errors(
        tmp_path,
        {
            "entities/purchase_order.yaml": bill_doc(
                partner="{ field: vendor_id }",
                line_quantity="{ material_field: material_id, unit_field: unit_id }",
                search="{ keyword_fields: [number], filters: [kind], default_sort: [biz_date desc] }",
                header="{ supplier_id: { type: ref, ref: purchase_order } }",
                lines="{ material_id: { type: ref, ref: purchase_order } }",
            )
        },
    )

    # Every item is reported at its own config path, and the step goes on after each of
    # them (requirement 2.2).
    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "line_quantity.unit_field"),
        (PACK, "entities/purchase_order.yaml", "partner.field"),
        (PACK, "entities/purchase_order.yaml", "search.filters[0]"),
    ]
    assert reasons(found) == [
        "单据 purchase_order 的分录没有字段 unit_id",
        "单据 purchase_order 的表头没有字段 vendor_id",
        "单据 purchase_order 的表头没有字段 kind",
    ]


@pytest.mark.req("2.9")
def test_a_conversion_whose_source_does_not_resolve_keeps_checking_its_target(
    tmp_path: Path,
) -> None:
    found = pack_errors(
        tmp_path,
        {
            "entities/purchase_order.yaml": bill_doc(
                header="{ supplier_id: { type: ref, ref: purchase_order } }",
                lines="{ qty_note: { type: string } }",
            ),
            "conversions/po_to_receipt.yaml": doc("""
                conversion:
                  id: po_to_receipt
                  source: purchase_requisition
                  target: purchase_order
                  require_source_status: approved
                  limit_by: qty
                  header_mapping: { supplier_id: source.supplier_id }
                  line_mapping: { missing_field: source_line.qty }
                  required_line_inputs: [qty_note, warehouse_id]
                """),
        },
    )

    # The source is no bill of the instance, so the fields it would be read from are not
    # looked up; the fields of the target are, and the one that is not there is reported
    # (requirement 2.9).
    assert places(found) == [
        (PACK, "conversions/po_to_receipt.yaml", "conversion.line_mapping.missing_field"),
        (PACK, "conversions/po_to_receipt.yaml", "conversion.required_line_inputs[1]"),
        (PACK, "conversions/po_to_receipt.yaml", "conversion.source"),
    ]
    assert reasons(found) == [
        "单据 purchase_order 的分录没有字段 missing_field",
        "单据 purchase_order 的分录没有字段 warehouse_id",
        "引用的单据 purchase_requisition 不存在",
    ]


@pytest.mark.req("2.9")
def test_a_writeback_a_linkage_and_a_rule_that_name_what_is_not_there(tmp_path: Path) -> None:
    found = pack_errors(
        tmp_path,
        {
            "entities/purchase_order.yaml": bill_doc(),
            "writebacks/po_linked_qty.yaml": doc("""
                writeback:
                  id: po_linked_qty
                  conversion: po_to_receipt
                  on: approve
                  target: source_line.linked_qty
                  op: add
                  value: line.qty_base
                """),
            "linkages/po_inventory.yaml": doc("""
                linkage:
                  id: po_inventory
                  bill: purchase_order
                  primitive: inventory
                  on: approve
                  direction: in
                  warehouse_field: line.warehouse_id
                  qty_field: line.qty_base
                """),
            "rules/delivery_date.yaml": doc("""
                rule:
                  id: R-CORE-001
                  bill: purchase_order
                  enforcement: enforced
                  on: [submit]
                  check: count(lines) > 0
                  error_code: VALIDATION_FAILED
                  field: delivery_date
                  message: { zh: 采购订单至少要有一行分录 }
                """),
        },
    )

    # The two ref fields of the bill are unresolved too, and every error is collected;
    # the lines of the bill declare no quantity semantics, which ``loader.step5_fields``
    # reports for the linkage that reads their base unit quantity (requirement 9.11).
    assert places(found) == [
        (PACK, "entities/purchase_order.yaml", "header.supplier_id.ref"),
        (PACK, "entities/purchase_order.yaml", "lines.material_id.ref"),
        (PACK, "linkages/po_inventory.yaml", "linkage.bill"),
        (PACK, "linkages/po_inventory.yaml", "linkage.qty_field"),
        (PACK, "linkages/po_inventory.yaml", "linkage.warehouse_field"),
        (PACK, "rules/delivery_date.yaml", "rule.field"),
        (PACK, "writebacks/po_linked_qty.yaml", "writeback.conversion"),
    ]
    assert reasons(found) == [
        "引用的实体或单据 supplier 不存在",
        "引用的实体或单据 material 不存在",
        "单据 purchase_order 的分录没有声明数量语义（line_quantity），库存联动取不到基本单位数量",
        # A bill without quantity semantics has no qty_base (requirement 1.4).
        "单据 purchase_order 的分录没有字段 qty_base",
        "单据 purchase_order 的分录没有字段 warehouse_id",
        "单据 purchase_order 的表头与分录都没有字段 delivery_date",
        "引用的下推配置 po_to_receipt 不存在",
    ]


@pytest.mark.req("2.9")
def test_a_declarative_query_whose_source_or_fields_do_not_resolve(tmp_path: Path) -> None:
    found = pack_errors(
        tmp_path,
        {
            "entities/supplier.yaml": entity_doc(),
            "tools/exposure.yaml": doc("""
                pack: core
                queries:
                  - tool: supplier_note_query
                    source: supplier
                    params:
                      supplier_id: { type: ref, ref: vendor, maps_to: header.id }
                    sort: [header.missing desc]
                    returns:
                      latest: [header.name, line.name]
                  - tool: price_query
                    source: purchase_order_line
                    returns: { latest: [line.price] }
                """),
        },
    )

    assert places(found) == [
        (PACK, "tools/exposure.yaml", "queries[0].params.supplier_id.ref"),
        (PACK, "tools/exposure.yaml", "queries[0].returns.latest[1]"),
        (PACK, "tools/exposure.yaml", "queries[0].sort[0]"),
        (PACK, "tools/exposure.yaml", "queries[1].source"),
    ]
    assert reasons(found) == [
        "引用的实体 vendor 不存在",
        # An entity has one level: header.<field> is its field, line.<field> reaches nothing.
        "实体 supplier 没有分录，无法解析 line.name",
        "实体 supplier 没有字段 missing",
        "引用的实体或单据 purchase_order_line 不存在",
    ]


@pytest.mark.req("2.9")
def test_a_unit_conversion_whose_records_entity_is_not_there(tmp_path: Path) -> None:
    found = pack_errors(
        tmp_path,
        {
            "entities/material.yaml": entity_doc(
                entity="material",
                id_prefix="mat",
                unit_conversion=doc("""
                    {
                      base_unit_field: base_unit_id,
                      entity: unit_conversion,
                      material_field: material_id,
                      unit_field: from_unit_id,
                      factor_field: factor,
                    }
                    """),
                fields="{ base_unit_id: { type: ref, ref: material, required: true } }",
            )
        },
        id_prefixes="[mat, orgu, emp]",
    )

    assert places(found) == [(PACK, "entities/material.yaml", "unit_conversion.entity")]
    assert reasons(found) == ["引用的实体 unit_conversion 不存在"]


# --- what the model holds after an error (requirement 2.2) -------------------------------


@pytest.mark.req("2.9")
def test_the_model_holds_what_resolved_and_none_where_a_reference_failed(
    tmp_path: Path,
) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={
            **CONTEXT_FILES,
            "entities/supplier.yaml": entity_doc(),
            "entities/purchase_order.yaml": bill_doc(
                lines="{ supplier_id: { type: ref, ref: supplier }, material_id: { type: ref, ref: material } }"
            ),
        },
    )
    target = loaded(tmp_path)
    errors = failures(target)

    assert len(errors) == 1
    domain: DomainModel | None = target.domain
    assert domain is not None
    lines = domain.bills["purchase_order"].lines
    # The reference that resolved is in the model; the one that did not is None, so no
    # later step reads a name that was never there.
    assert lines["supplier_id"].ref == "supplier"
    assert lines["material_id"].ref is None
    assert isinstance(domain.entities["supplier"], EntityModel)
