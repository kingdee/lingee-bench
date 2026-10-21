"""The configuration test of the purchase business pack (requirements 21.1 and 21.4 to 21.13).

``packs/purchase`` is the second of the three example business packs: the purchasing main
line of example-p2p.md sections 4 to 7 - purchase requisition, purchase order and purchase
receipt, the two pushes between them with their writebacks, the stock linkage of the
receipt, R-PUR-001 and the purchase price query - on top of the master data of
``packs/core``. It is loaded the way every pack level test loads one, in a temporary
instance configuration over the pack and the packs it depends on
(:func:`~tests.helpers.pack_instance.pack_instance`), which runs all eight validation steps
and the description cross-check on it; a pack that got through them is a pack an instance
can be served from (requirement 2.11).

What this module asserts beyond that the loading passed:

- the pack exposes exactly the purchase tools of the M0 tool list of example-p2p.md
  section 9 (:data:`M0_PURCHASE_TOOLS`), 14 of the 27 tools section 8 lists for purchase,
  in the output order of requirement 11.9; the other 13 are generated and dropped by
  ``exclude`` and by nothing else (requirement 21.4);
- the two pushes carry the ``require_source_status``, ``limit_by``, ``line_selection`` and
  ``required_line_inputs`` of requirement 21.5, and each has the writeback of
  requirement 21.11, only the one of ``purchase_order_to_receipt`` closing its source;
- the receipt feeds the stock of the warehouse of each of its lines (requirement 21.7);
- R-PUR-001 is enforced on the order with the check, the code and the field of
  requirement 21.6;
- ``purchase_price_query`` is declared over the order lines with the parameters, the
  filter, the order and the returns of requirement 21.13;
- the manifest declares the nine M0 primitives the configuration uses and ``zh-detailed``
  alone (requirement 21.1).

The behaviour of the 14 tools is covered by the pack's own case files, ``packs/purchase/tests/``
(:mod:`tests.packs.test_pack_cases`, requirement 21.9), and the README of the pack by
:mod:`tests.packs.test_readme` (task 19.7). One replay lives here rather than there: the
writeback to a requisition line and the stock a receipt feeds are the two effects no tool
of the M0 list returns - the list has no ``purchase_requisition_get``, and the stock balance
query belongs to the inventory pack - so a case file cannot read them and
:func:`test_the_writeback_and_the_linkage_change_what_no_tool_of_the_pack_shows` reads them
off the sandbox.
"""

from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import (
    BillModel,
    ConversionModel,
    FieldRef,
    LinkageModel,
    QueryModel,
    RuleModel,
    SortRef,
    WritebackModel,
)
from erp_mock.domain.inventory import stock_balance
from erp_mock.loader.step4 import RawPack
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET, generated_tool_names
from tests.helpers.pack_instance import DatasetSpec, PackInstance, PackSandbox, pack_instance

# The example business packs of the repository, and the pack this module is about.
PACKS_DIR: Final = Path(__file__).resolve().parents[2] / "packs"
PACK: Final = "purchase"

# The purchase tools of the M0 tool list of example-p2p.md section 9: 14 of the 27 tools
# section 8 lists for the pack.
M0_PURCHASE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "purchase_requisition_search",
        "purchase_requisition_create",
        "purchase_order_search",
        "purchase_order_get",
        "purchase_order_create",
        "purchase_order_create_from_requisition",
        "purchase_order_update",
        "purchase_order_submit",
        "purchase_order_approve",
        "purchase_order_close",
        "purchase_price_query",
        "purchase_receipt_create_from_order",
        "purchase_receipt_submit",
        "purchase_receipt_approve",
    }
)

# Those same tools in the order tools/list returns them (requirement 11.9): the bills by the
# path of the declaring file and, per bill, in the order of its ``operations``; then the
# push tools by the path of the conversion document; then the query tools in declaration
# order.
EXPOSED: Final[tuple[str, ...]] = (
    "purchase_order_search",
    "purchase_order_get",
    "purchase_order_create",
    "purchase_order_update",
    "purchase_order_submit",
    "purchase_order_approve",
    "purchase_order_close",
    "purchase_receipt_submit",
    "purchase_receipt_approve",
    "purchase_requisition_search",
    "purchase_requisition_create",
    "purchase_receipt_create_from_order",
    "purchase_order_create_from_requisition",
    "purchase_price_query",
)

# What the pack declares and does not expose: example-p2p.md section 8 lists all 27 tools
# for purchase and the M0 tool list of section 9 keeps 14, so the operations stay in the
# configuration and ``exclude`` drops these 13 (requirement 21.4). Four of them are the
# tool gaps the introduction of the requirements calls out: the requisition's ``get`` and
# the receipt's ``get``, ``search`` and ``delete``.
EXCLUDED: Final[tuple[str, ...]] = (
    "purchase_requisition_get",
    "purchase_requisition_update",
    "purchase_requisition_delete",
    "purchase_requisition_submit",
    "purchase_requisition_approve",
    "purchase_order_delete",
    "purchase_order_unsubmit",
    "purchase_order_unapprove",
    "purchase_order_unclose",
    "purchase_receipt_search",
    "purchase_receipt_get",
    "purchase_receipt_delete",
    "purchase_receipt_unapprove",
)

# The primitives the configuration of the pack uses, by the list of requirement 2.6, which
# is every primitive M0 implements: crud and numbering for every bill, state_machine for
# their state machines, pricing for the order and the receipt, unit_conversion for the
# quantity semantics of all three, conversion and writeback for the pushes, inventory for
# the stock linkage of the receipt, and rule_expr for the check of R-PUR-001 and the filter
# of the price query.
PRIMITIVES: Final[tuple[str, ...]] = (
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

# The one description variant of M0, which is what a profile may ask for (requirement 2.13).
VARIANTS: Final[tuple[str, ...]] = ("zh-detailed",)

# The bills of the pack, in the order loading step 5 resolved them, which is the path order
# of the ``entities/`` documents, and the operations each of them declares: the operations
# behind the 27 tools of example-p2p.md section 8 (requirement 21.4).
BILLS: Final[dict[str, tuple[str, ...]]] = {
    "purchase_order": (
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
    ),
    "purchase_receipt": ("search", "get", "delete", "submit", "approve", "unapprove"),
    "purchase_requisition": ("search", "get", "create", "update", "delete", "submit", "approve"),
}

# The id prefixes and the bill number prefixes of example-p2p.md section 3, which the
# manifest has to claim for the pack (requirement 2.7).
ID_PREFIXES: Final[tuple[str, ...]] = ("preq", "po", "prcv")
NUMBER_PREFIXES: Final[tuple[str, ...]] = ("CGSQ", "CGDD", "CGRK")

# The two pushes of requirement 21.5, with the per-line inputs each of them takes.
REQUISITION_TO_ORDER: Final = "purchase_requisition_to_order"
ORDER_TO_RECEIPT: Final = "purchase_order_to_receipt"
LINE_INPUTS: Final[dict[str, tuple[str, ...]]] = {
    REQUISITION_TO_ORDER: ("source_line_id", "qty", "price"),
    ORDER_TO_RECEIPT: ("source_line_id", "qty", "warehouse_id"),
}

# The writeback of each push (requirement 21.11); only the one of the receipt closes its
# source, the order being the bill whose lines can all be received.
WRITEBACKS: Final[dict[str, str]] = {
    REQUISITION_TO_ORDER: "order_approve_writeback_requisition",
    ORDER_TO_RECEIPT: "receipt_approve_writeback_order",
}

# The one pack rule of purchase (requirement 21.6) and the one query tool
# (requirement 21.13).
RULE: Final = "R-PUR-001"
PRICE_QUERY: Final = "purchase_price_query"


@pytest.fixture(scope="module")
def purchase(tmp_path_factory: pytest.TempPathFactory) -> PackInstance:
    """``packs/purchase`` and ``packs/core`` in a temporary instance, past every step.

    Built once for the module: writing the configuration and running the steps includes
    building the Snapshot of the base dataset, which brings in the Shared_Data of both
    packs (:func:`~tests.helpers.pack_instance.pack_instance`).
    """
    return pack_instance(tmp_path_factory.mktemp("purchase-pack"), PACKS_DIR, [PACK])


def manifest(instance: PackInstance) -> RawPack:
    """The documents of the purchase pack of ``instance``, with its validated pack.yaml."""
    return next(pack for pack in instance.loaded.packs if pack.name == PACK)


def bill(instance: PackInstance, name: str) -> BillModel:
    """The resolved bill ``name`` of ``instance``."""
    return instance.loaded.domain.bills[name]


def conversion(instance: PackInstance, name: str) -> ConversionModel:
    """The resolved conversion ``name`` of ``instance``."""
    return instance.loaded.domain.conversions[name]


def writeback(instance: PackInstance, name: str) -> WritebackModel:
    """The resolved writeback ``name`` of ``instance``."""
    return instance.loaded.domain.writebacks[name]


def linkage(instance: PackInstance, name: str) -> LinkageModel:
    """The resolved linkage ``name`` of ``instance``."""
    return instance.loaded.domain.linkages[name]


def rule(instance: PackInstance, name: str) -> RuleModel:
    """The resolved rule ``name`` of ``instance``."""
    return instance.loaded.domain.rules[name]


def query(instance: PackInstance, tool: str) -> QueryModel:
    """The resolved query of the tool ``tool`` of ``instance``."""
    return instance.loaded.domain.queries[tool]


@pytest.mark.req("21.1")
def test_the_pack_depends_on_core_alone_and_declares_three_bills(purchase: PackInstance) -> None:
    # The master data comes from core, which is therefore part of the instance and ordered
    # before purchase (requirement 2.5); the pack declares no entity of its own.
    assert purchase.pack_names == ("core", PACK)
    assert tuple(manifest(purchase).manifest.depends_on) == ("core",)
    declared = purchase.loaded.domain.bills
    assert {name: found.operations for name, found in declared.items()} == BILLS
    assert all(found.pack == PACK for found in declared.values())
    entities = purchase.loaded.domain.entities.values()
    assert not [entity for entity in entities if entity.pack == PACK]
    # The order and the receipt price their lines, the requisition does not: a requisition
    # asks for a quantity and the price is entered when the order is pushed from it, which
    # is why the push takes it as a per-line input (requirement 21.5).
    assert [name for name, found in declared.items() if found.pricing] == [
        "purchase_order",
        "purchase_receipt",
    ]
    # All three measure their lines, so every line carries the read-only qty_base the
    # pushes and the stock linkage read (requirements 1.4 and 9.11).
    assert all(found.line_quantity is not None for found in declared.values())


@pytest.mark.req("21.1")
def test_the_manifest_declares_the_m0_primitives_and_its_namespaces(
    purchase: PackInstance,
) -> None:
    found = manifest(purchase).manifest
    assert tuple(found.primitives) == PRIMITIVES
    assert tuple(found.description_variants) == VARIANTS
    assert found.rule_prefix == "PUR"
    # The prefixes of example-p2p.md section 3, which the pack claims for the instance.
    assert tuple(found.id_prefixes) == ID_PREFIXES
    assert tuple(found.number_prefixes) == NUMBER_PREFIXES
    # One tool prefix covers every tool of the pack, the two push tools and the query
    # included (requirement 2.7).
    assert tuple(found.tool_prefixes) == ("purchase_",)
    assert all(name.startswith("purchase_") for name in EXPOSED)


@pytest.mark.req("21.4")
def test_the_exposed_tools_are_exactly_the_m0_purchase_tools(purchase: PackInstance) -> None:
    exposed = tuple(
        spec.name for spec in purchase.loaded.default_profile.tools if spec.pack == PACK
    )
    assert exposed == EXPOSED
    assert frozenset(EXPOSED) == M0_PURCHASE_TOOLS
    assert len(EXPOSED) == 14
    # The profile of the temporary instance exposes the engine's own tool first, then the
    # tools of core and then those of purchase (requirement 11.9).
    names = tuple(spec.name for spec in purchase.loaded.default_profile.tools)
    assert names[0] == BUSINESS_CONTEXT_GET
    assert names[-len(EXPOSED) :] == EXPOSED
    # Nothing is renamed, the reads are the only read-only tools, and the two pushes and
    # the four state operations that advance a bill are not destructive (requirement 11.8).
    reads = frozenset(
        {
            "purchase_order_search",
            "purchase_order_get",
            "purchase_requisition_search",
            PRICE_QUERY,
        }
    )
    for name in EXPOSED:
        spec = purchase.spec(name)
        assert not spec.renamed, name
        assert spec.annotations.read_only_hint == (name in reads), name
        assert not spec.annotations.open_world_hint, name
    assert purchase.spec("purchase_order_update").annotations.destructive_hint
    assert not purchase.spec("purchase_receipt_create_from_order").annotations.destructive_hint


@pytest.mark.req("21.4")
def test_the_thirteen_tools_outside_the_m0_list_are_generated_and_excluded(
    purchase: PackInstance,
) -> None:
    exposure = manifest(purchase).exposure
    assert exposure is not None
    assert tuple(exposure.document.exclude) == EXCLUDED
    # Nothing is renamed: every generated name already has the form of requirement 11.2.
    assert dict(exposure.document.rename) == {}
    # All 13 are tools the pack generates - that is what ``exclude`` may name
    # (requirement 11.14) - and together with the 14 exposed ones they are the 27 tools
    # example-p2p.md section 8 lists for purchase.
    generated = generated_tool_names(purchase.loaded.domain, PACK, exposure.document)
    assert frozenset(EXCLUDED) <= frozenset(generated)
    assert frozenset(generated) == frozenset(EXPOSED) | frozenset(EXCLUDED)
    assert len(generated) == 27
    for name in EXCLUDED:
        assert name not in purchase.tools


@pytest.mark.req("21.5")
def test_the_two_pushes_are_configured_as_the_requirement_writes_them(
    purchase: PackInstance,
) -> None:
    for name, inputs in LINE_INPUTS.items():
        found = conversion(purchase, name)
        # Both push from an approved source, count their quota in base unit quantities and
        # require every target line to name the source line it comes from.
        assert found.require_source_status == "approved", name
        assert found.limit_by == "qty", name
        assert found.line_selection == "by_source_line_id", name
        assert found.required_line_inputs == inputs, name
        # limit_by qty puts the link field on the source's lines (requirement 8.5).
        assert found.link_field == FieldRef("line", "linked_qty"), name
    # Who pushes into whom: the requisition into the order, the order into the receipt.
    requisition = conversion(purchase, REQUISITION_TO_ORDER)
    assert (requisition.source, requisition.target) == ("purchase_requisition", "purchase_order")
    # The order takes the requested date of the requisition as its delivery date; the
    # requisition names no supplier, so the pushed draft has none and purchase_order_update
    # is how it is filled in.
    assert dict(requisition.header_mapping) == {"delivery_date": "need_date"}
    assert dict(requisition.line_mapping) == {
        "material_id": "material_id",
        "unit_id": "unit_id",
    }

    receipt = conversion(purchase, ORDER_TO_RECEIPT)
    assert (receipt.source, receipt.target) == ("purchase_order", "purchase_receipt")
    # The receipt has to price its lines exactly as the order did, so the price, the tax
    # rate and the tax inclusion of the order are mapped rather than entered again.
    assert dict(receipt.header_mapping) == {
        "supplier_id": "supplier_id",
        "price_includes_tax": "price_includes_tax",
    }
    assert dict(receipt.line_mapping) == {
        "material_id": "material_id",
        "unit_id": "unit_id",
        "price": "price",
        "tax_rate": "tax_rate",
    }
    # The push tools and their source parameters take the short names of the exposure
    # (requirement 11.13).
    assert purchase.spec("purchase_order_create_from_requisition").param_paths == (
        "requisition_id",
        "lines",
        "lines[].source_line_id",
        "lines[].qty",
        "lines[].price",
    )
    assert purchase.spec("purchase_receipt_create_from_order").param_paths == (
        "order_id",
        "lines",
        "lines[].source_line_id",
        "lines[].qty",
        "lines[].warehouse_id",
    )


@pytest.mark.req("21.11")
def test_each_push_has_the_writeback_of_the_requirement(purchase: PackInstance) -> None:
    for push, name in WRITEBACKS.items():
        found = writeback(purchase, name)
        assert found.conversion == push, name
        assert (found.on, found.reverse_on, found.op) == ("approve", "unapprove", "add"), name
        # Each target line adds its own base unit quantity to the source line it came from.
        assert found.target == FieldRef("line", "linked_qty"), name
        assert found.value == "line.qty_base", name
    # Only the receipt closes its source: an order whose every line has been received is
    # done, while a requisition may be ordered in parts and M0 exposes no close or unclose
    # tool for it anyway.
    assert writeback(purchase, WRITEBACKS[ORDER_TO_RECEIPT]).auto_close_source_when == (
        "all_lines_linked"
    )
    assert writeback(purchase, WRITEBACKS[REQUISITION_TO_ORDER]).auto_close_source_when is None
    # The two are the only writebacks of the pack, one per push (requirement 21.11).
    assert tuple(purchase.loaded.domain.writebacks) == tuple(sorted(WRITEBACKS.values()))


@pytest.mark.req("21.7")
def test_the_receipt_feeds_the_stock_of_the_warehouse_of_each_line(
    purchase: PackInstance,
) -> None:
    found = linkage(purchase, "purchase_receipt_inventory")
    assert (found.bill, found.primitive, found.on) == ("purchase_receipt", "inventory", "approve")
    # Receiving goods adds to the stock, per line and in the warehouse of that line, so one
    # order line can be received into several warehouses (requirement 8.7).
    assert found.direction == "in"
    assert found.warehouse == FieldRef("line", "warehouse_id")
    assert found.qty == FieldRef("line", "qty_base")
    # The fields the other directions would need are not declared (requirement 9.3).
    assert (found.from_warehouse, found.to_warehouse, found.adjust_field) == (None, None, None)
    # The warehouse is a required reference of the lines, which is what the primitive needs
    # to write a stock transaction for every line (requirement 9.11).
    warehouse = bill(purchase, "purchase_receipt").lines["warehouse_id"]
    assert (warehouse.type, warehouse.ref, warehouse.required) == ("ref", "warehouse", True)
    # The receipt is the only bill of the pack with a linkage: an order changes no stock.
    assert tuple(purchase.loaded.domain.linkages) == ("purchase_receipt_inventory",)


@pytest.mark.req("21.6")
def test_the_delivery_date_rule_is_enforced_on_the_order(purchase: PackInstance) -> None:
    found = rule(purchase, RULE)
    assert (found.pack, found.enforcement, found.bill) == (PACK, "enforced", "purchase_order")
    # Evaluated after each of the three operations that write the two dates; create covers
    # the push from a requisition as well (requirement 10.9).
    assert found.on == ("create", "update", "submit")
    assert found.check == "header.delivery_date >= header.biz_date"
    assert found.error_code == "VALIDATION_FAILED"
    assert found.field == FieldRef("header", "delivery_date")
    assert found.message is not None
    assert found.message.zh == "交货日期不得早于业务日期"
    # The only rule of the pack: R-INV-002, the other rule of requirement 21.6, is declared
    # by the inventory pack, which owns the tool its text has to appear in.
    assert tuple(purchase.loaded.domain.rules) == (RULE,)


@pytest.mark.req("21.13")
def test_the_price_query_is_configured_as_the_requirement_writes_it(
    purchase: PackInstance,
) -> None:
    found = query(purchase, PRICE_QUERY)
    # Declared over ``purchase_order_line``, which the model keeps as the bill and the
    # level it reads (requirement 5.13).
    assert (found.pack, found.source, found.source_level) == (PACK, "purchase_order", "line")
    # Both parameters are required; the supplier is compared on the header of the order and
    # the material on the line, which is what a query over bill lines can do
    # (requirement 10.10).
    assert tuple(found.params) == ("supplier_id", "material_id")
    assert [param.ref for param in found.params.values()] == ["supplier", "material"]
    assert [param.required for param in found.params.values()] == [True, True]
    assert found.params["supplier_id"].maps_to == FieldRef("header", "supplier_id")
    assert found.params["material_id"].maps_to == FieldRef("line", "material_id")
    # A draft or a submitted order is no deal, so the filter keeps the approved and the
    # closed ones; the newest business date comes first and the bill number breaks a tie.
    assert found.filter == "header.status in ['approved', 'closed']"
    assert found.sort == (
        SortRef(FieldRef("header", "biz_date"), descending=True),
        SortRef(FieldRef("header", "number"), descending=True),
    )
    assert found.latest == (
        FieldRef("line", "price"),
        FieldRef("header", "price_includes_tax"),
        FieldRef("header", "biz_date"),
        FieldRef("header", "number"),
    )
    assert found.history == (
        FieldRef("line", "price"),
        FieldRef("header", "biz_date"),
        FieldRef("header", "number"),
    )
    assert found.history_limit == 5
    # The one query tool of the pack, and the parameters it takes.
    assert tuple(purchase.loaded.domain.queries) == (PRICE_QUERY,)
    assert purchase.spec(PRICE_QUERY).param_paths == ("supplier_id", "material_id")


@pytest.mark.req("21.1")
def test_every_exposed_tool_and_parameter_carries_its_zh_detailed_text(
    purchase: PackInstance,
) -> None:
    # Loading step 7 would have refused a missing description (requirement 3.2); this reads
    # the text that reached the tools of the profile (requirement 3.7).
    missing = [text.about for text in purchase.described if not text.text.strip()]
    assert not missing, "没有描述的工具或参数：" + "、".join(missing)
    # The parameters of a bill's create tool: the declared header fields, the writable
    # standard ones, then the lines and their own fields (requirement 11.4).
    assert purchase.spec("purchase_order_create").param_paths == (
        "supplier_id",
        "delivery_date",
        "biz_date",
        "price_includes_tax",
        "remark",
        "lines",
        "lines[].material_id",
        "lines[].unit_id",
        "lines[].tax_rate",
        "lines[].qty",
        "lines[].price",
    )
    # A date filter of a search becomes two optional ends (requirement 11.5).
    assert purchase.spec("purchase_requisition_search").param_paths == (
        "keyword",
        "status",
        "department_id",
        "date_from",
        "date_to",
        "page",
        "page_size",
    )
    # Every state operation takes the id alone.
    for name in ("purchase_order_submit", "purchase_receipt_approve", "purchase_order_close"):
        assert purchase.spec(name).param_paths == ("id",)


# --- the two effects no tool of the M0 list returns (requirements 21.7 and 21.11) --------

# The dataset the replay starts from: a supplier, a material whose purchase unit is an
# alternative unit (1 box = 12 pieces) and an approved purchase requisition for 20 boxes.
# The requisition is approved by the build, which runs create, submit and approve for it
# (requirement 20.2) - the pack exposes neither of the latter two.
REPLAY: Final = DatasetSpec(
    name="purchase-replay",
    records={
        "supplier:main": {
            "kind": "supplier",
            "name": "回放用例专用轴承供应商",
            "short_name": "回放轴承",
        },
        "material:boxed": {
            "kind": "material",
            "name": "回放用例专用滚针轴承",
            "spec": "回放用例规格 D20",
            "material_kind": "raw",
            "base_unit": "PCS",
            "purchase_unit": "BOX",
            "tax_rate": "0.13",
        },
        "unit_conversion:boxed": {
            "kind": "unit_conversion",
            "material": "material:boxed",
            "from_unit": "BOX",
            "factor": "12",
        },
        "purchase_requisition:asked": {
            "kind": "purchase_requisition",
            "department": "D001",
            "applicant": "E001",
            "biz_date": "month(0).day(1)",
            "need_date": "month(0).day(28)",
            "status": "approved",
            "lines": [{"material": "material:boxed", "unit": "BOX", "qty": "20"}],
        },
    },
)

# The first warehouse of the Shared_Data of core, which the receipt of the replay feeds:
# the counters of a build start at 0 and that data is created first (requirement 20.17).
WAREHOUSE: Final = "wh_000001"

# What the replay orders and receives: 12 of the 20 requested boxes, 5 of which arrive.
ORDERED_BOXES: Final = "12"
RECEIVED_BOXES: Final = "5"
# Their base unit quantities, which are what the writeback and the linkage work in: one box
# is 12 pieces, so 12 boxes are 144 pieces and 5 boxes are 60.
ORDERED_BASE: Final = Decimal("144")
RECEIVED_BASE: Final = Decimal("60")


def succeeded(sandbox: PackSandbox, tool: str, args: dict[str, object]) -> dict[str, object]:
    """Call ``tool`` with ``args`` resolved, assert it succeeded and return its result."""
    resolved = sandbox.resolve(args)  # type: ignore[arg-type]
    assert isinstance(resolved, dict)
    outcome = sandbox.call(tool, resolved)
    assert not outcome.is_error, f"{tool} 本应成功，却失败了：{outcome.text}"
    assert outcome.structured is not None
    assert not sandbox.violations(), f"{tool} 之后不变量不成立"
    return dict(outcome.structured)


@pytest.mark.req("21.7", "21.11")
def test_the_writeback_and_the_linkage_change_what_no_tool_of_the_pack_shows(
    purchase: PackInstance,
) -> None:
    snapshot = purchase.build(REPLAY)
    requisition_line = f"{snapshot.id('purchase_requisition:asked')}-1"
    with purchase.sandbox(snapshot) as sandbox:
        # Order 12 of the 20 requested boxes, fill in the supplier the requisition does not
        # carry, and approve: the writeback adds the order line's qty_base to the
        # requisition line.
        order = succeeded(
            sandbox,
            "purchase_order_create_from_requisition",
            {
                "requisition_id": "$purchase_requisition:asked",
                "lines": [
                    {"source_line_id": requisition_line, "qty": ORDERED_BOXES, "price": "10.17"}
                ],
            },
        )
        order_id = order["id"]
        assert isinstance(order_id, str)
        succeeded(
            sandbox,
            "purchase_order_update",
            {"id": order_id, "supplier_id": "$supplier:main"},
        )
        succeeded(sandbox, "purchase_order_submit", {"id": order_id})
        succeeded(sandbox, "purchase_order_approve", {"id": order_id})

        # The M0 tool list has no purchase_requisition_get, so the model cannot read this;
        # the requisition line now holds the ordered quantity in base units.
        with sandbox.store.transaction() as uow:
            line = uow.get("purchase_requisition_line", requisition_line)
        assert line is not None
        assert line["linked_qty"] == ORDERED_BASE

        # Receive 5 of the 12 boxes into the first warehouse and approve: the linkage writes
        # one stock transaction per line and feeds the stock of (material, warehouse).
        receipt = succeeded(
            sandbox,
            "purchase_receipt_create_from_order",
            {
                "order_id": order_id,
                "lines": [
                    {
                        "source_line_id": f"{order_id}-1",
                        "qty": RECEIVED_BOXES,
                        "warehouse_id": WAREHOUSE,
                    }
                ],
            },
        )
        receipt_id = receipt["id"]
        assert isinstance(receipt_id, str)
        succeeded(sandbox, "purchase_receipt_submit", {"id": receipt_id})
        succeeded(sandbox, "purchase_receipt_approve", {"id": receipt_id})

        # The stock balance query belongs to the inventory pack, so this is the other effect
        # no tool of this pack returns (requirement 9.1).
        material = snapshot.id("material:boxed")
        with sandbox.store.transaction() as uow:
            assert stock_balance(uow, material, WAREHOUSE) == RECEIVED_BASE

        # What the model does see of the receipt: the order's own lines, whose linked_qty
        # the second writeback kept, and a status that is still approved - 60 of the 144
        # ordered pieces have arrived, so the order is not linked in full yet.
        found = succeeded(sandbox, "purchase_order_get", {"id": order_id})
        assert found["status"] == "approved"
        lines = found["lines"]
        assert isinstance(lines, list)
        assert lines[0]["linked_qty"] == str(RECEIVED_BASE)
        assert lines[0]["qty_base"] == str(ORDERED_BASE)
