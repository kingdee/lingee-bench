"""The engine test fixture passes the implemented loading steps (requirements 2.1, 25.2).

tests/helpers/fixtures describes what the fixture covers. These tests load it the way the
framework does, through :class:`~erp_mock.loader.loader.Loader` on the real directories,
and assert that steps 1 to 4 report nothing and that the documents the steps read are the
ones the fixture declares. A pack whose configuration no longer fits the models therefore
fails here, at the fixture, instead of in the tests that use it.

What the steps after 4 settle (references, namespaces, tool names, descriptions, the
datasets) is checked by the tasks that implement them; this module grows with
``IMPLEMENTED_STEPS``. The descriptions, the seed configuration and the initial dataset are
validated here as documents, and against the rules the later steps will apply to them: the
tool names of requirement 11.1, the completeness and the length limit of requirement 3, and
what requirement 20.1 lets a dataset declare.
"""

from collections.abc import Mapping
from typing import Final

import pytest

from erp_mock.config.instance import LineRecord, RecordValue
from erp_mock.config.pack import (
    GENERATORS,
    DeclarativeQueryDef,
    DescribedRuleDef,
    EnforcedRuleDef,
    PrimitiveQueryDef,
    ToolDescription,
)
from erp_mock.loader.loader import Loader
from erp_mock.loader.step4 import RawPack
from tests.helpers.fixtures import (
    ANCHOR_DATE,
    BASE_PACK,
    DATASET,
    IMPLEMENTED_STEPS,
    INSTANCE_DIR,
    INSTANCE_NAME,
    PACK_NAMES,
    PACKS_DIR,
    PROFILE,
    TRADE_PACK,
    fixture_loader,
    load_fixture,
)

# The description variant M0 serves, the length limit of a zh-detailed tool description
# (requirement 3.3) and the two guiding phrases requirement 3.4 names; step 7 checks all
# three, and the fixture has to satisfy them before it can be used there.
VARIANT: Final = "zh-detailed"
DESCRIPTION_LIMIT: Final = 400
GUIDING_PHRASES: Final = ("推荐优先使用", "这是正确的工具")


def pack_of(loader: Loader, name: str) -> RawPack:
    """The documents step 4 read for the pack ``name``."""
    return next(pack for pack in loader.raw_packs if pack.name == name)


def descriptions_of(pack: RawPack) -> Mapping[str, ToolDescription]:
    """The zh-detailed descriptions of ``pack``, by tool name."""
    return next(doc.document.tools for doc in pack.descriptions if doc.document.variant == VARIANT)


def tool_names_of(pack: RawPack) -> set[str]:
    """The names of the tools ``pack`` generates, by the naming rule of requirement 11.1.

    One tool per operation of each entity and bill, one per conversion (named after the
    target bill and the source's short name) and one per declared query, with ``exclude``
    removed and ``rename`` applied. The built-in business_context_get belongs to no pack.
    Step 6 generates the same names (task 9.1); until then this is what the description
    file is checked against.
    """
    exposure = None if pack.exposure is None else pack.exposure.document
    short_names = {} if exposure is None else dict(exposure.source_short_names)
    names = [
        f"{doc.document.entity}_{operation}"
        for doc in pack.entities
        for operation in doc.document.operations
    ]
    names += [
        f"{doc.document.bill}_{operation}"
        for doc in pack.bills
        for operation in doc.document.operations
    ]
    for doc in pack.conversions:
        conversion = doc.document
        source = short_names.get(conversion.source, conversion.source)
        names.append(f"{conversion.target}_create_from_{source}")
    if exposure is None:
        return set(names)
    names += [query.tool for query in exposure.queries]
    rename = dict(exposure.rename)
    return {rename.get(name, name) for name in names if name not in exposure.exclude}


def lines_of(record: Mapping[str, RecordValue]) -> tuple[LineRecord, ...]:
    """The lines a dataset record writes under ``lines``."""
    lines = record["lines"]
    assert isinstance(lines, tuple)
    return lines


@pytest.mark.req("2.1", "25.2")
def test_the_fixture_passes_every_implemented_step() -> None:
    loader = fixture_loader()

    for step in IMPLEMENTED_STEPS:
        # Each step raises ConfigErrors when it finds anything, so a fixture in error
        # fails here with the full error list of the first failing step.
        loader.run_step(step)

    assert loader.pack_order == PACK_NAMES


@pytest.mark.req("2.1", "25.2")
def test_the_instance_configuration_pins_both_packs_and_fixes_the_clock() -> None:
    loader = load_fixture()

    instance = loader.instance
    assert instance is not None
    assert instance.instance == INSTANCE_NAME
    assert tuple(instance.packs) == PACK_NAMES
    assert tuple(instance.packs.values()) == ("1.0.0", "1.0.0")
    assert (tuple(instance.profiles), instance.default_profile) == ((PROFILE,), PROFILE)
    assert (tuple(instance.datasets), instance.default_dataset) == ((DATASET,), DATASET)
    # A fixed clock keeps every result of the fixture reproducible (requirement 16.3).
    assert (instance.clock.mode, instance.clock.anchor_date) == ("fixed", ANCHOR_DATE.isoformat())
    assert [doc.document.profile for doc in loader.raw_profiles] == [PROFILE]
    assert [doc.document.dataset for doc in loader.raw_datasets] == [DATASET]


@pytest.mark.req("25.2")
def test_the_trade_pack_depends_on_the_base_pack() -> None:
    loader = load_fixture()

    base, trade = loader.raw_packs
    assert (base.name, trade.name) == PACK_NAMES
    assert dict(base.manifest.depends_on) == {}
    assert dict(trade.manifest.depends_on) == {BASE_PACK: ">=1.0.0 <2.0.0"}
    # Step 3 accepted the range against the version instance.yaml pins fx_base to.
    assert base.version == "1.0.0"


@pytest.mark.req("25.2")
def test_the_base_pack_declares_the_context_entities_and_the_measured_entity() -> None:
    base = pack_of(load_fixture(), BASE_PACK)

    entities = {doc.document.entity: doc.document for doc in base.entities}
    assert set(entities) == {
        "branch",
        "operator",
        "measure",
        "measure_link",
        "article",
        "vendor",
        "depot",
    }
    # One org context entity with a default flag, one user context entity (req 1.8).
    roles = {
        name: entity.context for name, entity in entities.items() if entity.context is not None
    }
    assert [(name, role.role) for name, role in roles.items()] == [
        ("branch", "org"),
        ("operator", "user"),
    ]
    assert roles["branch"].default_flag == "is_primary"
    assert roles["operator"].default_flag is None
    # Base unit, purchase unit and the entity holding the conversion records (req 6.8).
    article = entities["article"]
    conversion = article.unit_conversion
    assert conversion is not None
    assert (conversion.base_unit_field, conversion.entity) == ("base_measure_id", "measure_link")
    assert (conversion.unit_field, conversion.factor_field) == ("alt_measure_id", "ratio")
    assert article.fields["buy_measure_id"].default_from == "base_measure.id"
    # The partner entity keeps a status, the warehouse entity does not (req 5.7).
    assert (entities["vendor"].has_status, entities["depot"].has_status) == (True, False)


@pytest.mark.req("25.2")
def test_the_trade_pack_declares_both_state_machines_and_both_line_kinds() -> None:
    trade = pack_of(load_fixture(), TRADE_PACK)

    bills = {doc.document.bill: doc.document for doc in trade.bills}
    assert set(bills) == {
        "supply_order",
        "goods_entry",
        "fee_note",
        "pay_slip",
        "stock_move",
        "count_sheet",
        "issue_note",
    }
    assert bills["fee_note"].state_machine == "simple"
    assert bills["supply_order"].state_machine == "standard"
    # A simple bill declares none of submit, unsubmit, close and unclose (req 7.11).
    standard_only = {"submit", "unsubmit", "close", "unclose"}
    assert set(bills["fee_note"].operations).isdisjoint(standard_only)
    # Pricing with quantity semantics, pricing without it, and quantity semantics without
    # pricing, whose lines name their own qty field (requirement 1.4).
    order_lines = bills["supply_order"].line_quantity
    entry_lines = bills["goods_entry"].line_quantity
    assert order_lines is not None and entry_lines is not None
    assert (bills["supply_order"].pricing, order_lines.qty_field) == (True, None)
    assert (bills["fee_note"].pricing, bills["fee_note"].line_quantity) == (True, None)
    assert (bills["goods_entry"].pricing, entry_lines.qty_field) == (False, "entry_qty")
    # A same-name declaration gives a writable standard field a default (req 1.5).
    assert bills["supply_order"].header["price_includes_tax"].default is False
    assert bills["supply_order"].lines["tax_rate"].default_from == "article.tax_rate"


@pytest.mark.req("25.2")
def test_the_trade_pack_declares_both_push_quotas_and_their_writebacks() -> None:
    trade = pack_of(load_fixture(), TRADE_PACK)

    conversions = {doc.document.id: doc.document for doc in trade.conversions}
    writebacks = {doc.document.id: doc.document for doc in trade.writebacks}
    by_qty = conversions["supply_order_to_entry"]
    by_amount = conversions["fee_note_to_pay_slip"]
    assert (by_qty.source, by_qty.target, by_qty.limit_by) == (
        "supply_order",
        "goods_entry",
        "qty",
    )
    assert (by_amount.source, by_amount.target, by_amount.limit_by) == (
        "fee_note",
        "pay_slip",
        "amount",
    )
    # The qty push closes its source when every line is linked; the amount push comes
    # from a simple bill, which has no closed state, so it must not (req 7.11, 8.13).
    order_writeback = writebacks["entry_writeback_order"]
    fee_writeback = writebacks["pay_writeback_fee"]
    assert (order_writeback.target, order_writeback.value) == (
        "source_line.linked_qty",
        "line.qty_base",
    )
    assert order_writeback.auto_close_source_when == "all_lines_linked"
    assert (fee_writeback.target, fee_writeback.value) == (
        "source.linked_amount",
        "header.amount_with_tax",
    )
    assert fee_writeback.auto_close_source_when is None


@pytest.mark.req("25.2")
def test_the_trade_pack_declares_all_four_inventory_directions() -> None:
    trade = pack_of(load_fixture(), TRADE_PACK)

    linkages = {doc.document.id: doc.document for doc in trade.linkages}
    assert {linkage.direction for linkage in linkages.values()} == {
        "in",
        "out",
        "transfer",
        "adjust",
    }
    assert all(linkage.qty_field == "line.qty_base" for linkage in linkages.values())
    transfer = linkages["stock_move_inventory"]
    assert (transfer.from_warehouse_field, transfer.to_warehouse_field) == (
        "header.from_depot_id",
        "header.to_depot_id",
    )
    # The adjust linkage names the header enum field and partitions its values (req 9.3).
    adjust = linkages["count_sheet_inventory"].adjust
    assert adjust is not None
    assert (adjust.field, tuple(adjust.increase), tuple(adjust.decrease)) == (
        "header.count_kind",
        ("gain",),
        ("loss",),
    )


@pytest.mark.req("25.2")
def test_the_trade_pack_declares_enforced_and_described_rules() -> None:
    trade = pack_of(load_fixture(), TRADE_PACK)

    rules = {doc.document.id: doc.document for doc in trade.rules}
    enforced = rules["R-FXT-001"]
    described = rules["R-FXT-002"]
    assert isinstance(enforced, EnforcedRuleDef)
    assert isinstance(described, DescribedRuleDef)
    assert enforced.bill == "stock_move"
    assert enforced.check == "header.from_depot_id != header.to_depot_id"
    assert enforced.error_code == "VALIDATION_FAILED"
    assert tuple(described.applies_to_tools) == ("count_sheet_create",)
    # Two more enforced rules on one bill, declared in the opposite order of their
    # numbers, so the rules step is seen to check them by rule id (requirement 10.8).
    assert [doc.document.id for doc in trade.rules] == [
        "R-FXT-002",
        "R-FXT-001",
        "R-FXT-004",
        "R-FXT-003",
    ]
    for number in ("R-FXT-003", "R-FXT-004"):
        rule = rules[number]
        assert isinstance(rule, EnforcedRuleDef)
        assert rule.bill == "goods_entry"
        assert tuple(rule.on) == ("create", "update", "submit")


@pytest.mark.req("25.2")
def test_the_exposures_declare_exclude_rename_short_names_and_both_query_kinds() -> None:
    loader = load_fixture()

    base = pack_of(loader, BASE_PACK).exposure
    trade = pack_of(loader, TRADE_PACK).exposure
    assert base is not None and trade is not None
    assert tuple(base.document.exclude) == ("depot_get",)
    assert dict(base.document.rename) == {"vendor_get": "vendor_detail_get"}
    assert tuple(trade.document.exclude) == ("count_sheet_unapprove",)
    assert dict(trade.document.rename) == {"stock_move_create": "stock_move_draft_create"}
    # The short names the push tool names are built from (requirement 11.13).
    assert dict(trade.document.source_short_names) == {"supply_order": "order", "fee_note": "fee"}
    declarative, primitive = trade.document.queries
    assert isinstance(declarative, DeclarativeQueryDef)
    assert isinstance(primitive, PrimitiveQueryDef)
    assert (declarative.tool, declarative.source) == ("supply_price_query", "supply_order_line")
    assert declarative.filter == "header.status in ['approved', 'closed']"
    assert (primitive.tool, primitive.primitive, primitive.query) == (
        "stock_level_query",
        "inventory",
        "balance",
    )


@pytest.mark.req("2.1")
def test_only_the_referenced_packs_are_read() -> None:
    loader = load_fixture()

    # Every directory of tests/fixtures/packs/ is referenced by instance.yaml, so the
    # fixture has no pack that takes no part in the validation (requirement 2.1).
    assert sorted(path.name for path in PACKS_DIR.iterdir() if path.is_dir()) == sorted(PACK_NAMES)
    assert [pack.directory for pack in loader.raw_packs] == [
        PACKS_DIR / name for name in PACK_NAMES
    ]
    # The instance directory holds instance.yaml and the two directories step 4 reads.
    assert sorted(path.name for path in INSTANCE_DIR.iterdir()) == [
        "datasets",
        "instance.yaml",
        "profiles",
    ]


@pytest.mark.req("25.2")
def test_every_generated_tool_has_a_zh_detailed_description() -> None:
    loader = load_fixture()

    for pack in loader.raw_packs:
        # Each pack declares the one variant M0 serves and provides exactly that file.
        assert tuple(pack.manifest.description_variants) == (VARIANT,)
        assert [doc.document.variant for doc in pack.descriptions] == [VARIANT]
        descriptions = descriptions_of(pack)
        # No description for a tool the pack does not generate, and none missing (req 3.2).
        assert set(descriptions) == tool_names_of(pack)
        for name, tool in descriptions.items():
            assert tool.description.strip(), name
            # A zh-detailed tool description counts at most 400 code points (req 3.3).
            assert len(tool.description) <= DESCRIPTION_LIMIT, name
            assert tool.params, name
            assert all(text.strip() for text in tool.params.values()), name
            texts = (tool.description, *tool.params.values())
            hits = [phrase for phrase in GUIDING_PHRASES if any(phrase in t for t in texts)]
            assert hits == [], name


@pytest.mark.req("25.2")
def test_the_descriptions_name_the_renamed_pushed_and_query_tools() -> None:
    loader = load_fixture()

    base = descriptions_of(pack_of(loader, BASE_PACK))
    trade = descriptions_of(pack_of(loader, TRADE_PACK))
    # exclude and rename decide which names the description file carries (req 11.1).
    assert "vendor_detail_get" in base
    assert {"vendor_get", "depot_get"}.isdisjoint(base)
    assert "stock_move_draft_create" in trade
    assert {"stock_move_create", "count_sheet_unapprove"}.isdisjoint(trade)
    # A push tool is named after the target bill and the source's short name, and its
    # parameters are the source ID and the required_line_inputs (requirements 11.4, 11.13).
    assert tuple(trade["goods_entry_create_from_order"].params) == (
        "order_id",
        "lines",
        "lines[].source_line_id",
        "lines[].entry_qty",
        "lines[].depot_id",
    )
    assert tuple(trade["pay_slip_create_from_fee"].params) == (
        "fee_id",
        "lines",
        "lines[].source_line_id",
        "lines[].qty",
        "lines[].price",
    )
    # A declarative query takes its declared params; the inventory balance query takes the
    # parameters of the primitive, whose names do not come from the pack.
    assert tuple(trade["supply_price_query"].params) == ("vendor_id", "article_id")
    assert tuple(trade["stock_level_query"].params) == (
        "material_id",
        "warehouse_id",
        "page",
        "page_size",
    )


@pytest.mark.req("25.2")
def test_the_described_rule_text_appears_in_the_tools_it_applies_to() -> None:
    trade = pack_of(load_fixture(), TRADE_PACK)

    descriptions = descriptions_of(trade)
    described = [doc.document for doc in trade.rules if isinstance(doc.document, DescribedRuleDef)]
    assert described
    for rule in described:
        for name in rule.applies_to_tools:
            # The full text, character for character, as step 7 compares it (req 3.6).
            assert rule.description.zh in descriptions[name].description, rule.id


@pytest.mark.req("20.1", "25.2")
def test_the_base_pack_provides_the_shared_data_of_its_shared_entities() -> None:
    base = pack_of(load_fixture(), BASE_PACK)

    entities = {doc.document.entity: doc.document for doc in base.entities}
    records = [(doc.file, record) for doc in base.shared_data for record in doc.document.records]
    # Files are read in path order, which is also the order the Seed_Builder creates the
    # records in (requirement 20.17), so a referenced record is created first: both the
    # warehouses and the operators name the organization of branch.yaml by its code.
    assert [file for file, _ in records] == [
        "seed/shared/branch.yaml",
        "seed/shared/depot.yaml",
        "seed/shared/depot.yaml",
        "seed/shared/measure.yaml",
        "seed/shared/measure.yaml",
        "seed/shared/operator.yaml",
        "seed/shared/operator.yaml",
    ]
    # Shared data covers every entity whose ``shared`` is true, and nothing else; the
    # entities a dataset provides (article, measure_link, vendor) are not written here.
    kinds = {record["kind"] for _, record in records}
    assert kinds == {name for name, entity in entities.items() if entity.shared}
    assert kinds == {"branch", "depot", "measure", "operator"}
    # The org context entity has exactly one record with its default flag (req 1.8), and
    # the first operator is the current user of instance.yaml and of the dataset.
    org = [record for _, record in records if record["kind"] == "branch"]
    assert [record["is_primary"] for record in org] == [True]
    operators = [record for _, record in records if record["kind"] == "operator"]
    assert [record["duty"] for record in operators] == ["buyer", "keeper"]
    # A record of an entity with a code rule must not write ``code``: the engine assigns
    # it (requirement 4.3), and writing a read-only field would fail the build (req 20.6).
    for _, record in records:
        written = entities[str(record["kind"])].code is None
        assert ("code" in record) is written, record


@pytest.mark.req("20.1", "25.2")
def test_the_generators_bind_every_built_in_generator_to_a_declared_field() -> None:
    base = pack_of(load_fixture(), BASE_PACK)

    assert base.generators is not None
    generators = base.generators.document.generators
    fields = {doc.document.entity: doc.document.fields for doc in base.entities}
    for path, generator in generators.items():
        entity, _, field = path.partition(".")
        # Step 5 resolves these paths against the pack's entities and their fields.
        assert field in fields[entity], path
        assert generator.gen in GENERATORS, path
    # The fixture binds all seven built-in generators, so a build exercises each of them.
    assert {generator.gen for generator in generators.values()} == set(GENERATORS)


@pytest.mark.req("20.1")
def test_the_dataset_declares_the_shared_data_the_current_user_and_the_periods() -> None:
    loader = load_fixture()

    dataset = next(doc.document for doc in loader.raw_datasets if doc.document.dataset == DATASET)
    assert (dataset.dataset, dataset.version) == (DATASET, "1.0.0")
    assert dataset.random_seed == 20261009
    # The one pack that provides shared data, and the current user as an internal ID; both
    # are resolved at step 5 and step 8 (requirement 2.10).
    assert tuple(dataset.include_shared) == (BASE_PACK,)
    assert dataset.current_user == "opr_000001"
    # periods holds closed_before alone, as a period expression: the month before the
    # anchor month stays open, which is where the history bills are (requirement 20.1).
    assert dataset.periods.closed_before == "month(-1)"
    # Every alias is ``<kind>:<name>`` with the prefix equal to the record's kind, and it
    # is declared before it is referenced (requirement 20.6).
    aliases = list(dataset.entities)
    for index, (alias, record) in enumerate(dataset.entities.items()):
        kind, _, name = alias.partition(":")
        assert (kind, bool(name)) == (record["kind"], True)
        for value in record.values():
            if isinstance(value, str) and value in dataset.entities:
                assert aliases.index(value) < index, alias


@pytest.mark.req("20.1")
def test_the_dataset_declares_disabled_data_history_bills_and_bulk_records() -> None:
    loader = load_fixture()
    base = pack_of(loader, BASE_PACK)
    trade = pack_of(loader, TRADE_PACK)
    dataset = next(doc.document for doc in loader.raw_datasets if doc.document.dataset == DATASET)

    entities = {doc.document.entity: doc.document for doc in base.entities}
    records = dataset.entities
    # One disabled record of basic data, of the entity that keeps a status (req 5.7), for
    # the R-GEN-006 cases of the tests that build the dataset.
    disabled = [alias for alias, record in records.items() if record.get("status") == "disabled"]
    assert disabled == ["vendor:retired"]
    assert entities[str(records[disabled[0]]["kind"])].has_status
    # The history bills cover every status the Seed_Builder can be asked for: approved for
    # both state machines, submitted, closed (executed as submit, approve, close, which is
    # a manual close) and, without a status, draft (requirement 20.2).
    statuses = {alias: record.get("status") for alias, record in records.items()}
    assert statuses["supply_order:fully_received"] == "approved"
    assert statuses["fee_note:freight"] == "approved"
    assert statuses["supply_order:waiting_approval"] == "submitted"
    assert statuses["supply_order:closed_by_hand"] == "closed"
    assert "status" not in records["supply_order:open_draft"]
    # The pushed bills name their source bill by its alias and their source lines by row
    # number, and write none of the header fields the conversion maps (requirement 20.16).
    mapped = {doc.document.target: set(doc.document.header_mapping) for doc in trade.conversions}
    pushed = {alias: record for alias, record in records.items() if "source" in record}
    assert set(pushed) == {"goods_entry:full", "goods_entry:partial", "pay_slip:freight_part"}
    for alias, record in pushed.items():
        kind = str(record["kind"])
        assert set(record).isdisjoint(mapped[kind]), alias
        assert all("source_line" in line for line in lines_of(record)), alias
    # An approved bill of the bill type whose inventory linkage is the in direction, so
    # the built dataset has stock on hand to issue, move and count against.
    incoming = {doc.document.bill for doc in trade.linkages if doc.document.direction == "in"}
    assert incoming == {"goods_entry"}
    assert [
        alias
        for alias, record in records.items()
        if record["kind"] in incoming and record.get("status") == "approved"
    ] == ["goods_entry:full", "goods_entry:partial"]
    # bulk generates background records of an entity a dataset provides, without aliases;
    # every field of such a record is generated or has a default, otherwise the build would
    # stop at a missing required field (requirements 20.4, 20.6).
    assert base.generators is not None
    bound = set(base.generators.document.generators)
    assert [(item.kind, item.count) for item in dataset.bulk] == [("vendor", 12)]
    for item in dataset.bulk:
        entity = entities[item.kind]
        assert entity.shared is False
        for name, field in entity.fields.items():
            assert f"{item.kind}.{name}" in bound or not field.required or field.default, name
