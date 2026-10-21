"""The configuration test of the core business pack (requirements 21.1, 21.4 and 21.10).

``packs/core`` is the first of the three example business packs: the master data of
example-p2p.md section 3 - organization, department, employee, supplier, material, unit,
unit conversion and warehouse - and no bill at all. It is loaded here the way every pack
level test loads one, in a temporary instance configuration over the pack and the packs it
depends on (:func:`~tests.helpers.pack_instance.pack_instance`), which runs all eight
validation steps and the description cross-check on it; a pack that got through them is a
pack an instance can be served from (requirement 2.11).

What this module asserts beyond that the loading passed:

- the pack exposes exactly the core tools of the M0 tool list of example-p2p.md section 9
  (:data:`M0_CORE_TOOLS`), in the output order of requirement 11.9, and the profile of the
  temporary instance exposes those plus the engine's own ``business_context_get``
  (requirement 21.3 counts that tool among the 22);
- the two tools the pack declares and does not expose - ``department_search`` and
  ``employee_search``, which section 8 lists for core and section 9 leaves out of M0 - are
  generated and dropped by ``exclude`` and by nothing else (requirement 21.4);
- the manifest declares the three primitives the configuration uses and ``zh-detailed``
  alone (requirement 21.1);
- the eight entities carry the roles and the key declarations the rest of the example packs
  build on: the two context entities, the base unit and the conversion records of the
  material entity, and which data is Shared_Data and which comes from a dataset.

The behaviour of the five tools is covered by the pack's own case file,
``packs/core/tests/`` (:mod:`tests.packs.test_pack_cases`, requirement 21.9), and the
README of the pack by :mod:`tests.packs.test_readme` (task 19.7).
"""

from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import EntityModel
from erp_mock.loader.step4 import RawPack
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET, generated_tool_names
from tests.helpers.pack_instance import PackInstance, pack_instance

# The example business packs of the repository, and the pack this module is about.
PACKS_DIR: Final = Path(__file__).resolve().parents[2] / "packs"
PACK: Final = "core"

# The core tools of the M0 tool list of example-p2p.md section 9, in the order that table
# writes them. ``business_context_get`` is listed there too and belongs to no pack: the
# engine exposes it in every profile (requirement 11.1).
M0_CORE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "supplier_search",
        "supplier_get",
        "material_search",
        "material_get",
        "warehouse_search",
    }
)

# Those same tools in the order tools/list returns them (requirement 11.9): inside a pack
# the tools of the entities by the path of the declaring file and, per entity, in the order
# of its ``operations``.
EXPOSED: Final[tuple[str, ...]] = (
    "material_search",
    "material_get",
    "supplier_search",
    "supplier_get",
    "warehouse_search",
)

# What the pack declares and does not expose: example-p2p.md section 8 lists both for core
# and the M0 tool list of section 9 leaves both out, so the operations stay in the
# configuration and ``exclude`` drops the tools (requirement 21.4).
EXCLUDED: Final[tuple[str, ...]] = ("department_search", "employee_search")

# The primitives the configuration of the pack uses, by the list of requirement 2.6: crud
# and numbering for every entity, and unit_conversion for the base unit and the conversion
# records of the material entity (requirement 6.8). Nothing else: the pack has no bill, no
# push, no writeback, no linkage and no enforced rule.
PRIMITIVES: Final[tuple[str, ...]] = ("crud", "numbering", "unit_conversion")

# The one description variant of M0, which is what a profile may ask for (requirement 2.13).
VARIANTS: Final[tuple[str, ...]] = ("zh-detailed",)

# The entities of the pack, in the order loading step 5 resolved them, which is the path
# order of the ``entities/`` documents.
ENTITIES: Final[tuple[str, ...]] = (
    "department",
    "employee",
    "material",
    "org",
    "supplier",
    "unit",
    "unit_conversion",
    "warehouse",
)

# The Shared_Data the pack provides itself (``shared: true``), which every dataset brings in
# with ``include_shared``; the other three entities hold Business_Data a dataset writes.
SHARED: Final[frozenset[str]] = frozenset({"department", "employee", "org", "unit", "warehouse"})

# The entities whose records the engine keeps a status for, so R-GEN-006 keeps a disabled
# one out of a new bill (requirement 5.7).
WITH_STATUS: Final[frozenset[str]] = frozenset({"material", "supplier", "warehouse"})


@pytest.fixture(scope="module")
def core(tmp_path_factory: pytest.TempPathFactory) -> PackInstance:
    """``packs/core`` in a temporary instance, past every validation step (req 21.1).

    Built once for the module: writing the configuration and running the steps includes
    building the Snapshot of the base dataset, which brings in the Shared_Data of the pack
    (:func:`~tests.helpers.pack_instance.pack_instance`).
    """
    return pack_instance(tmp_path_factory.mktemp("core-pack"), PACKS_DIR, [PACK])


def manifest(instance: PackInstance) -> RawPack:
    """The documents of the core pack of ``instance``, with its validated pack.yaml."""
    return next(pack for pack in instance.loaded.packs if pack.name == PACK)


def entity(instance: PackInstance, name: str) -> EntityModel:
    """The resolved entity ``name`` of ``instance``."""
    return instance.loaded.domain.entities[name]


@pytest.mark.req("21.1")
def test_the_pack_is_loaded_alone_and_needs_no_other_pack(core: PackInstance) -> None:
    # core is the base pack of the example: it declares no ``depends_on``, so the temporary
    # instance holds it alone (requirement 2.5).
    assert core.pack_names == (PACK,)
    assert dict(manifest(core).manifest.depends_on) == {}

    # Eight entities and no bill: the master data of example-p2p.md section 3.
    assert tuple(core.loaded.domain.entities) == ENTITIES
    assert tuple(core.loaded.domain.bills) == ()


@pytest.mark.req("21.1")
def test_the_manifest_declares_only_the_primitives_the_configuration_uses(
    core: PackInstance,
) -> None:
    found = manifest(core).manifest
    assert tuple(found.primitives) == PRIMITIVES
    assert tuple(found.description_variants) == VARIANTS
    # No bill, so no bill number prefix is taken.
    assert tuple(found.number_prefixes) == ()


@pytest.mark.req("21.4")
def test_the_exposed_tools_are_exactly_the_m0_core_tools(core: PackInstance) -> None:
    exposed = tuple(spec.name for spec in core.loaded.default_profile.tools)
    # The engine's own tool comes first and the pack's follow (requirement 11.9).
    assert exposed == (BUSINESS_CONTEXT_GET, *EXPOSED)
    assert frozenset(EXPOSED) == M0_CORE_TOOLS

    # Every exposed tool belongs to core and is a read of basic data: a search or a get,
    # so none of them changes anything (requirement 11.8).
    for name in EXPOSED:
        spec = core.spec(name)
        assert spec.pack == PACK
        assert spec.kind == "entity_op"
        assert spec.annotations.read_only_hint, name
        assert not spec.renamed, name


@pytest.mark.req("21.4")
def test_the_two_tools_outside_the_m0_list_are_generated_and_excluded(
    core: PackInstance,
) -> None:
    exposure = manifest(core).exposure
    assert exposure is not None
    assert tuple(exposure.document.exclude) == EXCLUDED
    # Nothing is renamed and no query tool is declared: the two query tools of M0 belong to
    # the purchase and the inventory pack.
    assert dict(exposure.document.rename) == {}
    assert tuple(exposure.document.queries) == ()

    # Both names are tools the pack generates - that is what ``exclude`` may name
    # (requirement 11.14) - and the operations behind them stay in the configuration.
    generated = generated_tool_names(core.loaded.domain, PACK, exposure.document)
    assert frozenset(EXCLUDED) <= frozenset(generated)
    assert frozenset(generated) == frozenset(EXPOSED) | frozenset(EXCLUDED)
    assert entity(core, "department").operations == ("search",)
    assert entity(core, "employee").operations == ("search",)
    for name in EXCLUDED:
        assert name not in core.tools


@pytest.mark.req("21.1")
def test_the_context_entities_are_the_organization_and_the_employee(core: PackInstance) -> None:
    org = core.loaded.domain.org_entity
    assert org is not None
    assert (org.name, org.default_flag) == ("org", "is_default")
    # The org role must name a bool field as its default flag (requirement 1.8).
    assert org.fields["is_default"].type == "bool"

    user = core.loaded.domain.user_entity
    assert user is not None
    assert user.name == "employee"
    # The current user of the temporary instance is the first record of that entity, which
    # is one of the employees of the pack's Shared_Data (requirement 20.17).
    assert core.current_user == "emp_000001"


@pytest.mark.req("21.1")
def test_the_material_entity_declares_the_base_unit_and_its_conversion_records(
    core: PackInstance,
) -> None:
    material = entity(core, "material")
    conversion = material.unit_conversion
    assert conversion is not None
    declared = (
        conversion.base_unit_field,
        conversion.entity,
        conversion.material_field,
        conversion.unit_field,
        conversion.factor_field,
    )
    assert declared == ("base_unit_id", "unit_conversion", "material_id", "from_unit_id", "factor")

    # The base unit is a required reference to the unit entity (requirement 6.12) and the
    # purchase unit defaults to it, so a material measured in one unit needs no second
    # declaration (example-p2p.md section 3).
    base = material.fields["base_unit_id"]
    assert (base.type, base.ref, base.required) == ("ref", "unit", True)
    purchase = material.fields["purchase_unit_id"].default_from
    assert purchase is not None
    assert (purchase.ref_field, purchase.entity, purchase.field) == ("base_unit_id", "unit", "id")

    # The tax rate carries the allowed values of requirement 1.7, which constrain the
    # input of every write tool that writes it.
    assert tuple(material.fields["tax_rate"].values) == ("0", "0.06", "0.09", "0.13")


@pytest.mark.req("21.1")
def test_the_shared_data_and_the_business_data_are_separated(core: PackInstance) -> None:
    entities = core.loaded.domain.entities
    assert frozenset(name for name, item in entities.items() if item.shared) == SHARED
    assert frozenset(name for name, item in entities.items() if item.has_status) == WITH_STATUS

    # The Shared_Data of the pack is what the base dataset of the temporary instance holds,
    # so the default organization and the current user of loading step 8 came from it
    # (requirement 2.10). The Snapshot that step took up is there.
    dataset = core.loaded.default_dataset
    assert dataset.snapshot is not None
    assert dataset.current_user == core.current_user


@pytest.mark.req("21.1")
def test_every_exposed_tool_and_parameter_carries_its_zh_detailed_text(
    core: PackInstance,
) -> None:
    # Loading step 7 would have refused a missing description (requirement 3.2); this reads
    # the text that reached the tools of the profile (requirement 3.7).
    missing = [text.about for text in core.described if not text.text.strip()]
    assert not missing, "没有描述的工具或参数：" + "、".join(missing)

    # The parameters of the two kinds of core tool: a search takes the keyword, its filters
    # and the page parameters, a get takes the id alone (requirement 11.4).
    assert core.spec("material_search").param_paths == (
        "keyword",
        "kind",
        "status",
        "page",
        "page_size",
    )
    assert core.spec("material_get").param_paths == ("id",)
    assert core.spec("warehouse_search").param_paths == ("keyword", "status", "page", "page_size")
