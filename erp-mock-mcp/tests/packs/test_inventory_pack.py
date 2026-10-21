"""The configuration test of the inventory business pack (requirements 21.1, 21.4 to 21.7).

``packs/inventory`` is the stock part of the example: the stock adjustment of
example-p2p.md section 4, its adjust linkage of section 6 and the stock balance query of
section 9. It is loaded here the way every pack level test loads one, in a temporary
instance configuration over the pack and the packs it depends on
(:func:`~tests.helpers.pack_instance.pack_instance`), which runs all eight validation steps
and the description cross-check on it; a pack that got through them is a pack an instance
can be served from (requirement 2.11).

What this module asserts beyond that the loading passed:

- the pack exposes exactly the inventory tools of the M0 tool list of example-p2p.md
  section 9 (:data:`M0_INVENTORY_TOOLS`), in the output order of requirement 11.9, and the
  two tools it declares and does not expose - ``stock_adjustment_submit`` and
  ``stock_adjustment_approve``, which section 8 lists for inventory and section 9 leaves out
  of M0 - are generated and dropped by ``exclude`` and by nothing else (requirement 21.4);
- the manifest declares the five primitives the configuration uses and ``zh-detailed``
  alone (requirement 21.1);
- the bill carries the two header fields the linkage needs: the warehouse of the stock
  transactions and the required enum that says which way the quantity goes
  (requirement 9.11), and its lines declare quantity semantics without pricing;
- the linkage is the adjust one of requirement 21.7: on approve, the warehouse of the
  header, ``line.qty_base`` as the quantity, and the values of the header enum partitioned
  into increase and decrease (requirements 9.2 and 9.3);
- R-INV-002 is described, applies to ``stock_adjustment_create`` and its text occurs
  verbatim in the zh-detailed description of that tool (requirements 21.6 and 3.6);
- the balance query is the primitive query of the inventory primitive, with the parameters
  that primitive defines (requirements 9.7 and 9.12).

The behaviour of the linkage and of the two tools is covered by the pack's own case file,
``packs/inventory/tests/`` (:mod:`tests.packs.test_pack_cases`, requirement 21.9), which
replays an approved adjustment of each direction and reads the balance off; the README of
the pack is checked by :mod:`tests.packs.test_readme` (task 19.7).
"""

from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import BillModel, FieldRef, LinkageModel, QueryModel, RuleModel
from erp_mock.loader.step4 import RawPack
from erp_mock.tools.generator import generated_tool_names
from tests.helpers.pack_instance import PackInstance, pack_instance

# The example business packs of the repository, the pack this module is about and the pack
# it depends on.
PACKS_DIR: Final = Path(__file__).resolve().parents[2] / "packs"
PACK: Final = "inventory"
BASE_PACK: Final = "core"

# The one bill of the pack, its linkage, its rule and its query.
BILL: Final = "stock_adjustment"
LINKAGE: Final = "stock_adjustment_inventory"
RULE: Final = "R-INV-002"
QUERY: Final = "inventory_balance_query"
CREATE: Final = "stock_adjustment_create"

# The inventory tools of the M0 tool list of example-p2p.md section 9. The other six tools
# section 8 lists for inventory are out of M0: the transfer order's three, the stock
# transaction search and the adjustment's submit and approve (requirement 21.4).
M0_INVENTORY_TOOLS: Final[frozenset[str]] = frozenset({QUERY, CREATE})

# Those tools in the order tools/list returns them (requirement 11.9): inside a pack the
# operations of the bills by the path of the declaring file first, the query tools after
# them in the order the exposure declares them.
EXPOSED: Final[tuple[str, ...]] = (CREATE, QUERY)

# What the pack declares and does not expose: section 8 lists both for inventory and the M0
# tool list of section 9 leaves both out, so the operations stay in the configuration -
# a dataset reaches them through a record's ``status`` - and ``exclude`` drops the tools.
EXCLUDED: Final[tuple[str, ...]] = ("stock_adjustment_submit", "stock_adjustment_approve")

# The primitives the configuration of the pack uses, by the list of requirement 2.6: crud
# and numbering for the bill, state_machine for its state machine, unit_conversion for the
# quantity semantics of its lines and inventory for the linkage and the balance query.
# Nothing else: the bill has no pricing, is no end of a push and the pack's one rule is
# described, so it needs no rule expression.
PRIMITIVES: Final[tuple[str, ...]] = (
    "crud",
    "numbering",
    "state_machine",
    "unit_conversion",
    "inventory",
)

# The one description variant of M0, which is what a profile may ask for (requirement 2.13).
VARIANTS: Final[tuple[str, ...]] = ("zh-detailed",)

# The operations example-p2p.md section 8 lists for the bill (requirement 21.4), in the
# order the bill declares them, which is the order their tools are generated in.
OPERATIONS: Final[tuple[str, ...]] = ("create", "submit", "approve")

# The two values of the header enum the linkage reads, one per direction (requirement 9.2).
GAIN: Final = "gain"
LOSS: Final = "loss"


@pytest.fixture(scope="module")
def inventory(tmp_path_factory: pytest.TempPathFactory) -> PackInstance:
    """``packs/inventory`` in a temporary instance, past every validation step (req 21.1).

    Built once for the module: the pack depends on core, so the instance holds both packs
    and the Snapshot of its base dataset brings in the Shared_Data of core, which is where
    the units, the warehouses and the context records come from
    (:func:`~tests.helpers.pack_instance.pack_instance`).
    """
    return pack_instance(tmp_path_factory.mktemp("inventory-pack"), PACKS_DIR, [PACK])


def manifest(instance: PackInstance) -> RawPack:
    """The documents of the inventory pack of ``instance``, with its validated pack.yaml."""
    return next(pack for pack in instance.loaded.packs if pack.name == PACK)


def bill(instance: PackInstance) -> BillModel:
    """The resolved stock adjustment of ``instance``."""
    return instance.loaded.domain.bills[BILL]


def linkage(instance: PackInstance) -> LinkageModel:
    """The resolved inventory linkage of that bill."""
    return instance.loaded.domain.linkages[LINKAGE]


def rule(instance: PackInstance) -> RuleModel:
    """The resolved R-INV-002 of the pack."""
    return instance.loaded.domain.rules[RULE]


def query(instance: PackInstance) -> QueryModel:
    """The resolved balance query the exposure declares."""
    return instance.loaded.domain.queries[QUERY]


@pytest.mark.req("21.1")
def test_the_pack_is_loaded_on_top_of_core(inventory: PackInstance) -> None:
    # inventory references the material, the unit and the warehouse of core, so core is
    # part of the instance and is ordered before it (requirement 2.5).
    assert inventory.pack_names == (BASE_PACK, PACK)
    assert tuple(manifest(inventory).manifest.depends_on) == (BASE_PACK,)

    # One bill and no entity of its own: the stock transactions and the balances are tables
    # the engine keeps itself (design.md data model 3).
    assert tuple(inventory.loaded.domain.bills) == (BILL,)
    assert [
        name for name, item in inventory.loaded.domain.entities.items() if item.pack == PACK
    ] == []


@pytest.mark.req("21.1")
def test_the_manifest_declares_only_the_primitives_the_configuration_uses(
    inventory: PackInstance,
) -> None:
    found = manifest(inventory).manifest
    assert tuple(found.primitives) == PRIMITIVES
    assert tuple(found.description_variants) == VARIANTS
    # The namespaces the one bill takes (business-pack.md section 4).
    assert tuple(found.id_prefixes) == ("sadj",)
    assert tuple(found.number_prefixes) == ("KCTZ",)
    assert found.rule_prefix == "INV"


@pytest.mark.req("21.4")
def test_the_exposed_tools_are_exactly_the_m0_inventory_tools(inventory: PackInstance) -> None:
    exposed = tuple(
        spec.name for spec in inventory.loaded.default_profile.tools if spec.pack == PACK
    )
    assert exposed == EXPOSED
    assert frozenset(exposed) == M0_INVENTORY_TOOLS
    # The tools of the pack come last in the profile: the engine's own tool first, then the
    # tools of core, then these two (requirement 11.9).
    names = tuple(spec.name for spec in inventory.loaded.default_profile.tools)
    assert names[-len(EXPOSED) :] == EXPOSED

    # The create tool writes, the query reads; neither was renamed (requirement 11.8).
    create = inventory.spec(CREATE)
    assert (create.kind, create.action) == ("entity_op", "create")
    assert not create.annotations.read_only_hint
    assert inventory.spec(QUERY).annotations.read_only_hint
    assert not create.renamed and not inventory.spec(QUERY).renamed


@pytest.mark.req("21.4")
def test_the_two_state_tools_outside_the_m0_list_are_generated_and_excluded(
    inventory: PackInstance,
) -> None:
    exposure = manifest(inventory).exposure
    assert exposure is not None
    assert tuple(exposure.document.exclude) == EXCLUDED
    assert dict(exposure.document.rename) == {}

    # Both names are tools the pack generates - that is what ``exclude`` may name
    # (requirement 11.14) - and the two operations behind them stay in the configuration,
    # which is how a dataset or a case reaches the approval through a record's ``status``.
    generated = generated_tool_names(inventory.loaded.domain, PACK, exposure.document)
    assert frozenset(EXCLUDED) <= frozenset(generated)
    assert frozenset(generated) == frozenset(EXPOSED) | frozenset(EXCLUDED)
    assert bill(inventory).operations == OPERATIONS
    for name in EXCLUDED:
        assert name not in inventory.tools


@pytest.mark.req("21.7")
def test_the_bill_carries_the_fields_the_adjust_linkage_needs(inventory: PackInstance) -> None:
    found = bill(inventory)
    assert (found.id_prefix, found.number_prefix, found.state_machine) == (
        "sadj",
        "KCTZ",
        "standard",
    )

    # The warehouse of the stock transactions is a required ref field of the header, so one
    # bill adjusts one warehouse (requirement 9.11).
    warehouse = found.header["warehouse_id"]
    assert (warehouse.type, warehouse.ref, warehouse.required) == ("ref", "warehouse", True)

    # The direction field is a required enum of the header with exactly the two values the
    # linkage partitions (requirement 9.3).
    reason = found.header["reason"]
    assert (reason.type, reason.required) == ("enum", True)
    assert tuple(reason.enum_values) == (GAIN, LOSS)

    # No pricing: a difference has a quantity and no price, so the lines declare their own
    # quantity field and the quantity semantics name it (requirements 1.4 and 6.6).
    assert not found.pricing
    quantity = found.line_quantity
    assert quantity is not None
    assert (quantity.material_field, quantity.unit_field, quantity.qty_field) == (
        "material_id",
        "unit_id",
        "qty",
    )
    assert found.qty_field == "qty"
    assert found.lines["qty"].type == "qty"
    # The unit defaults to the base unit of the material, since stock is kept in it.
    unit = found.lines["unit_id"].default_from
    assert unit is not None
    assert (unit.ref_field, unit.entity, unit.field) == ("material_id", "material", "base_unit_id")


@pytest.mark.req("21.7")
def test_the_linkage_is_the_adjust_one_of_the_stock_adjustment(inventory: PackInstance) -> None:
    found = linkage(inventory)
    assert (found.bill, found.primitive, found.on) == (BILL, "inventory", "approve")
    assert found.direction == "adjust"

    # The warehouse of the header, the base unit quantity of the line (requirement 9.11),
    # and no transfer field: those two belong to the transfer direction alone.
    assert found.warehouse == FieldRef("header", "warehouse_id")
    assert found.qty == FieldRef("line", "qty_base")
    assert (found.from_warehouse, found.to_warehouse) == (None, None)

    # Which value means plus and which means minus (requirement 9.2), together exactly the
    # values of the enum field they read.
    assert found.adjust_field == FieldRef("header", "reason")
    assert (found.increase, found.decrease) == ((GAIN,), (LOSS,))
    values = tuple(bill(inventory).header["reason"].enum_values)
    assert found.increase + found.decrease == values


@pytest.mark.req("21.6")
def test_r_inv_002_is_described_and_its_text_is_in_the_create_description(
    inventory: PackInstance,
) -> None:
    found = rule(inventory)
    assert (found.pack, found.enforcement) == (PACK, "described")
    assert found.applies_to_tools == (CREATE,)
    # A described rule is not checked by the engine, so it declares none of the parts an
    # enforced rule has (meta-model.md section 9).
    assert (found.bill, found.on, found.check, found.error_code) == (None, (), None, None)

    # The text of the rule occurs verbatim in the zh-detailed description of that tool:
    # character for character, case and punctuation included (requirement 3.6). Loading
    # step 7 would have refused it otherwise; this reads what reached the tool.
    assert found.description is not None
    assert found.description.zh in inventory.spec(CREATE).description

    # It is the pack's only rule, and no enforced rule exists to write a case for: R-INV-001
    # belongs to the transfer order, which is out of M0 (requirement 21.4).
    assert [name for name, item in inventory.loaded.domain.rules.items() if item.pack == PACK] == [
        RULE
    ]


@pytest.mark.req("21.4")
def test_the_balance_query_is_the_primitive_query_of_the_inventory_primitive(
    inventory: PackInstance,
) -> None:
    found = query(inventory)
    assert (found.pack, found.primitive, found.query) == (PACK, "inventory", "balance")
    # A primitive query declares no source, no filter and no fields: the primitive defines
    # the parameters and the result (requirement 9.7).
    assert (found.source, found.filter, found.sort) == (None, None, ())
    assert (found.latest, found.history) == ((), ())

    # The parameters the inventory primitive defines: the material and the warehouse, both
    # optional, and the two page parameters (requirement 9.12).
    spec = inventory.spec(QUERY)
    assert spec.kind == "query"
    assert spec.param_paths == ("material_id", "warehouse_id", "page", "page_size")
    assert "required" not in spec.input_schema


@pytest.mark.req("21.1")
def test_every_exposed_tool_and_parameter_carries_its_zh_detailed_text(
    inventory: PackInstance,
) -> None:
    # Loading step 7 would have refused a missing description (requirement 3.2); this reads
    # the text that reached the tools of the profile (requirement 3.7).
    missing = [text.about for text in inventory.described if not text.text.strip()]
    assert not missing, "没有描述的工具或参数：" + "、".join(missing)

    # The parameters of the create tool: the declared header fields in declaration order,
    # then the writable standard fields it did not declare, then the lines and their own
    # fields (requirement 11.4).
    assert inventory.spec(CREATE).param_paths == (
        "warehouse_id",
        "reason",
        "biz_date",
        "remark",
        "lines",
        "lines[].material_id",
        "lines[].unit_id",
        "lines[].qty",
    )
