"""erp_mock.loader.step5_namespace: namespaces, prefixes, rule numbers and primitives.

One example per kind of error the module reports, each on a tree written below ``tmp_path``
by tests.helpers.tree that holds exactly the one thing that is wrong, and each asserted by
what the loader reports: the places - the (pack, file, config path) of every location of
the error - and the reason. An error several declarations share (a name, a prefix, a rule
number, a primitive) lists every one of those places in one error, which is what these
tests read off ``places`` (requirement 2.2).

The trees run through :class:`~erp_mock.loader.loader.Loader`, the way loading runs step 5,
so steps 1 to 4 pass first and the references of the documents resolve
(``loader.step5_model``); a tree therefore holds the two context entities of
:data:`~tests.helpers.tree.CONTEXT_FILES` and states its own prefixes, and what the test
writes is the only error that comes out.

:data:`FULL_FILES` is the pack that makes one declaration of every kind requirement 2.6
names. It serves both ways round: with the complete manifest it passes the step, and with
one primitive dropped from it the step reports every declaration that uses that primitive.
"""

import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import STOCK_BALANCE, STOCK_TXN
from erp_mock.config.pack import PRIMITIVES
from erp_mock.loader.errors import ConfigError, ConfigErrors
from erp_mock.loader.loader import Loader
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
# test adds to it for a conflict between two packs.
PACK = "core"
OTHER = "purchase"

# One place of a reported error: the pack, the file and the config path.
type Place = tuple[str | None, str, str | None]

# The definitions the default instance.yaml names, both valid.
DEFINITIONS: Final[Mapping[str, str]] = {
    "profiles/semantic.yaml": profile_doc(),
    "datasets/purchase-basic.yaml": dataset_doc(),
}

# The ID namespaces of a pack that holds the two context entities alone: a pack states the
# namespaces of its own documents and nothing else, since a prefix in two manifests is a
# conflict of its own (requirement 2.7).
CONTEXT_IDS: Final = "[orgu, emp]"


def doc(text: str) -> str:
    """A YAML document written indented in the test source."""
    return textwrap.dedent(text).lstrip("\n")


def simple_bill(**overrides: str | None) -> str:
    """A bill document whose two levels hold one plain field and reference nothing."""
    levels = {"header": "{ note: { type: string } }", "lines": "{ note: { type: string } }"}
    return bill_doc(**{**levels, **overrides})


def rule_doc(number: str) -> str:
    """An enforced rule of the bill ``purchase_order``, numbered ``number``."""
    return doc(f"""
        rule:
          id: {number}
          bill: purchase_order
          enforcement: enforced
          on: [submit]
          check: count(lines) > 0
          error_code: VALIDATION_FAILED
          message: {{ zh: 至少要有一行分录 }}
        """)


def write_core(
    tmp_path: Path,
    files: Mapping[str, str] | None = None,
    *,
    primitives: str = PACK_KEYS["primitives"],
    rule_prefix: str = PACK_KEYS["rule_prefix"],
    id_prefixes: str = PACK_KEYS["id_prefixes"],
    number_prefixes: str = PACK_KEYS["number_prefixes"],
    tool_prefixes: str = PACK_KEYS["tool_prefixes"],
) -> Loader:
    """An instance of the one pack ``core``, which holds the context entities and ``files``.

    The manifest items default to those of :data:`~tests.helpers.tree.PACK_KEYS`; a test
    overrides the one it is about, or states the namespaces of the documents it writes.
    """
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={**CONTEXT_FILES, **(files or {})},
        primitives=primitives,
        rule_prefix=rule_prefix,
        id_prefixes=id_prefixes,
        number_prefixes=number_prefixes,
        tool_prefixes=tool_prefixes,
    )
    return loaded(tmp_path)


def write_context_pack(tmp_path: Path, number_prefixes: str = "[]") -> None:
    """Write the pack ``core`` holding the two context entities alone."""
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files=CONTEXT_FILES,
        id_prefixes=CONTEXT_IDS,
        number_prefixes=number_prefixes,
    )


def write_both(
    tmp_path: Path,
    *,
    rule_prefix: str = "PUR",
    id_prefixes: str = "[po]",
    number_prefixes: str = "[CGDD]",
    tool_prefixes: str = "[purchase_]",
) -> None:
    """An instance of ``core`` and ``purchase``, the second manifest taking the arguments.

    ``core`` holds the context entities alone and ``purchase`` no document at all, and the
    namespaces of the two manifests are disjoint, so a conflict between them is the only
    error a test has to write.
    """
    packs = f"{{ {PACK}: 1.0.0, {OTHER}: 1.0.0 }}"
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS, packs=packs)
    write_context_pack(tmp_path)
    write_pack(
        tmp_path / PACKS_DIR,
        OTHER,
        rule_prefix=rule_prefix,
        id_prefixes=id_prefixes,
        number_prefixes=number_prefixes,
        tool_prefixes=tool_prefixes,
    )


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


# --- the names of the records (requirement 2.7) ------------------------------------------


@pytest.mark.req("2.7")
def test_two_packs_that_declare_the_same_entity_name(tmp_path: Path) -> None:
    packs = f"{{ {PACK}: 1.0.0, {OTHER}: 1.0.0 }}"
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS, packs=packs)
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={**CONTEXT_FILES, "entities/supplier.yaml": entity_doc()},
        id_prefixes="[sup, orgu, emp]",
        number_prefixes="[]",
    )
    write_pack(
        tmp_path / PACKS_DIR,
        OTHER,
        files={"entities/vendor.yaml": entity_doc(id_prefix="po")},
        rule_prefix="PUR",
        id_prefixes="[po]",
        number_prefixes="[CGDD]",
        tool_prefixes="[purchase_]",
    )
    found = failures(loaded(tmp_path))

    # One error for the whole conflict, locating both declarations and naming both packs;
    # the error belongs to the first pack of instance.yaml, which sorts it with that pack.
    assert places(found) == [
        (PACK, "entities/supplier.yaml", "entity"),
        (PACK, "entities/vendor.yaml", "entity"),
    ]
    assert reasons(found) == ["名称 supplier 重名：业务包 core 的实体、业务包 purchase 的实体"]


@pytest.mark.req("2.7")
def test_an_entity_named_like_the_lines_of_a_bill(tmp_path: Path) -> None:
    found = failures(
        write_core(
            tmp_path,
            {
                "entities/purchase_order.yaml": simple_bill(),
                "entities/purchase_order_line.yaml": entity_doc(
                    entity="purchase_order_line", id_prefix="pol"
                ),
            },
            id_prefixes="[po, pol, orgu, emp]",
        )
    )

    # ``<bill>_line`` names the line table of the bill, so no other object may take it.
    # The entities of a pack are walked before its bills, which orders the two places.
    assert places(found) == [
        (PACK, "entities/purchase_order_line.yaml", "entity"),
        (PACK, "entities/purchase_order.yaml", "bill"),
    ]
    assert reasons(found) == [
        "名称 purchase_order_line 重名：业务包 core 的实体、业务包 core 的单据 purchase_order 的分录"
    ]


@pytest.mark.req("2.7")
@pytest.mark.parametrize("name", [STOCK_TXN, STOCK_BALANCE])
def test_an_entity_named_like_a_record_of_the_engine(tmp_path: Path, name: str) -> None:
    found = failures(
        write_core(
            tmp_path,
            {f"entities/{name}.yaml": entity_doc(entity=name, id_prefix="stk")},
            id_prefixes="[stk, orgu, emp]",
        )
    )

    # The engine's own name is written in no file, so the error locates the declaration
    # that takes it and names the engine in its reason.
    assert places(found) == [(PACK, f"entities/{name}.yaml", "entity")]
    assert reasons(found) == [f"名称 {name} 重名：引擎内部表、业务包 core 的实体"]


@pytest.mark.req("2.7")
def test_a_bill_named_like_an_entity(tmp_path: Path) -> None:
    found = failures(
        write_core(
            tmp_path, {"entities/supplier_bill.yaml": simple_bill(bill="employee", id_prefix="po")}
        )
    )

    assert places(found) == [
        (PACK, "entities/employee.yaml", "entity"),
        (PACK, "entities/supplier_bill.yaml", "bill"),
    ]
    assert reasons(found) == ["名称 employee 重名：业务包 core 的实体、业务包 core 的单据"]


# --- the ID and number prefixes (requirement 2.7) ----------------------------------------


@pytest.mark.req("2.7")
def test_an_id_prefix_two_entities_use(tmp_path: Path) -> None:
    found = failures(
        write_core(
            tmp_path,
            {
                "entities/customer.yaml": entity_doc(entity="customer"),
                "entities/supplier.yaml": entity_doc(),
            },
        )
    )

    # Both declare the prefix ``sup``, so a generated ID would not say which of the two it
    # belongs to (requirement 4.1).
    assert places(found) == [
        (PACK, "entities/customer.yaml", "id_prefix"),
        (PACK, "entities/supplier.yaml", "id_prefix"),
    ]
    assert reasons(found) == [
        "ID 前缀 sup 被多个实体或单据使用：业务包 core 的实体 customer、业务包 core 的实体 supplier"
    ]


@pytest.mark.req("2.7")
def test_a_number_prefix_two_bills_use(tmp_path: Path) -> None:
    found = failures(
        write_core(
            tmp_path,
            {
                "entities/order_one.yaml": simple_bill(bill="order_one"),
                "entities/order_two.yaml": simple_bill(bill="order_two", id_prefix="ot"),
            },
            id_prefixes="[po, ot, orgu, emp]",
        )
    )

    assert places(found) == [
        (PACK, "entities/order_one.yaml", "number.prefix"),
        (PACK, "entities/order_two.yaml", "number.prefix"),
    ]
    assert reasons(found) == [
        "编号前缀 CGDD 被多个单据使用：业务包 core 的单据 order_one、业务包 core 的单据 order_two"
    ]


@pytest.mark.req("2.7")
def test_an_id_prefix_the_manifest_does_not_state(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, {"entities/supplier.yaml": entity_doc(id_prefix="ven")}))

    assert places(found) == [(PACK, "entities/supplier.yaml", "id_prefix")]
    assert reasons(found) == ["ID 前缀 ven 没有列在业务包 core 的 manifest 的 id_prefixes 中"]


@pytest.mark.req("2.7")
def test_a_number_prefix_the_manifest_does_not_state(tmp_path: Path) -> None:
    found = failures(
        write_core(tmp_path, {"entities/purchase_order.yaml": simple_bill(number="{ prefix: PO }")})
    )

    assert places(found) == [(PACK, "entities/purchase_order.yaml", "number.prefix")]
    assert reasons(found) == ["编号前缀 PO 没有列在业务包 core 的 manifest 的 number_prefixes 中"]


@pytest.mark.req("2.7")
def test_an_id_prefix_in_two_manifests(tmp_path: Path) -> None:
    write_both(tmp_path, id_prefixes="[po, emp]")
    found = failures(loaded(tmp_path))

    # ``emp`` is stated by both manifests, each located at its own item of its own
    # pack.yaml; a Location holds a pack-relative file, so the reason names the two packs
    # (as a dependency cycle does in step 3).
    assert places(found) == [
        (PACK, "pack.yaml", "id_prefixes[1]"),
        (PACK, "pack.yaml", "id_prefixes[1]"),
    ]
    assert reasons(found) == ["ID 前缀 emp 同时出现在业务包 core、purchase 的 manifest 中"]


@pytest.mark.req("2.7")
def test_a_number_prefix_in_two_manifests(tmp_path: Path) -> None:
    write_instance(
        tmp_path / INSTANCE_DIR, DEFINITIONS, packs=f"{{ {PACK}: 1.0.0, {OTHER}: 1.0.0 }}"
    )
    # The pack that holds the context entities states a number prefix it does not use, the
    # same one the second manifest states.
    write_context_pack(tmp_path, number_prefixes="[CGDD]")
    write_pack(
        tmp_path / PACKS_DIR,
        OTHER,
        rule_prefix="PUR",
        id_prefixes="[po]",
        number_prefixes="[CGDD]",
        tool_prefixes="[purchase_]",
    )
    found = failures(loaded(tmp_path))

    assert places(found) == [
        (PACK, "pack.yaml", "number_prefixes[0]"),
        (PACK, "pack.yaml", "number_prefixes[0]"),
    ]
    assert reasons(found) == ["编号前缀 CGDD 同时出现在业务包 core、purchase 的 manifest 中"]


@pytest.mark.req("2.7")
def test_a_rule_prefix_in_two_manifests(tmp_path: Path) -> None:
    write_both(tmp_path, rule_prefix="CORE")
    found = failures(loaded(tmp_path))

    assert places(found) == [(PACK, "pack.yaml", "rule_prefix"), (PACK, "pack.yaml", "rule_prefix")]
    assert reasons(found) == ["rule_prefix CORE 同时出现在业务包 core、purchase 的 manifest 中"]


@pytest.mark.req("2.7")
@pytest.mark.parametrize(
    "prefix", ["core_", "core_bill_", "cor"], ids=["equal", "longer", "shorter"]
)
def test_a_tool_prefix_that_is_a_prefix_of_another_packs(tmp_path: Path, prefix: str) -> None:
    write_both(tmp_path, tool_prefixes=f"[{prefix}]")
    found = failures(loaded(tmp_path))

    # Every tool name begins with one of the prefixes of its pack (requirement 11.2), so
    # two prefixes that run into each other would leave the pack of a name undecided.
    assert places(found) == [
        (PACK, "pack.yaml", "tool_prefixes[0]"),
        (PACK, "pack.yaml", "tool_prefixes[0]"),
    ]
    assert reasons(found) == [
        f"工具名前缀 core_ 与 {prefix} 中一个是另一个的前缀，涉及业务包 core、purchase"
    ]


@pytest.mark.req("2.7")
def test_tool_prefixes_of_one_pack_may_run_into_each_other(tmp_path: Path) -> None:
    write_core(tmp_path, tool_prefixes="[core_, core_bill_]").run_step(5)


# --- the rule prefix and the rule numbers (requirement 2.8) ------------------------------


@pytest.mark.req("2.8")
def test_a_rule_prefix_of_gen(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, rule_prefix="GEN"))

    assert places(found) == [(PACK, "pack.yaml", "rule_prefix")]
    assert reasons(found) == ["rule_prefix 不能为 GEN，它保留给引擎级规则"]


@pytest.mark.req("2.8")
@pytest.mark.parametrize(
    "number",
    ["R-CORE-1", "R-CORE-0001", "R-CORE-000", "R-PUR-001", "R-CORE-001-A", "CORE-001", "R--001"],
    ids=["one_digit", "four_digits", "zero", "other_prefix", "suffix", "no_r", "no_prefix"],
)
def test_a_rule_number_that_is_not_of_the_packs_form(tmp_path: Path, number: str) -> None:
    files = {"entities/purchase_order.yaml": simple_bill(), "rules/lines.yaml": rule_doc(number)}
    found = failures(write_core(tmp_path, files))

    assert places(found) == [(PACK, "rules/lines.yaml", "rule.id")]
    assert reasons(found) == [
        f"规则编号 {number} 不是 R-CORE-<序号> 形式（序号为 001 到 999 的 3 位数字）"
    ]


@pytest.mark.req("2.8")
@pytest.mark.parametrize("number", ["R-CORE-001", "R-CORE-999", "R-CORE-010"])
def test_a_rule_number_of_the_packs_form_passes(tmp_path: Path, number: str) -> None:
    files = {"entities/purchase_order.yaml": simple_bill(), "rules/lines.yaml": rule_doc(number)}
    write_core(tmp_path, files).run_step(5)


@pytest.mark.req("2.8")
def test_a_rule_number_declared_twice(tmp_path: Path) -> None:
    files = {
        "entities/purchase_order.yaml": simple_bill(),
        "rules/first.yaml": rule_doc("R-CORE-001"),
        "rules/second.yaml": rule_doc("R-CORE-001"),
    }
    found = failures(write_core(tmp_path, files))

    assert places(found) == [
        (PACK, "rules/first.yaml", "rule.id"),
        (PACK, "rules/second.yaml", "rule.id"),
    ]
    assert reasons(found) == ["规则编号 R-CORE-001 被声明多次，涉及业务包 core"]


# --- the primitives a configuration uses (requirement 2.6) -------------------------------

# The lines of a bill that declares quantity semantics: the material and the unit it is
# measured in (requirement 1.4). A bill with pricing measures its standard qty, so the
# declaration names no quantity field of its own.
MEASURED_LINES: Final = (
    "{ supplier_id: { type: ref, ref: supplier, required: true },"
    " measure_id: { type: ref, ref: measure } }"
)
LINE_QUANTITY: Final = "{ material_field: supplier_id, unit_field: measure_id }"

# A pack that makes one declaration of every kind requirement 2.6 names. The declarations
# are sound beyond their names too, which is ``loader.step5_fields``: the measured entity
# declares a base unit and conversion records, the quantity semantics of both bills name
# ref fields of their lines, and the linkage names a required ref field of its bill. So the
# only error these trees produce is the one about the primitives.
FULL_FILES: Final[Mapping[str, str]] = {
    # The unit entity, the warehouse the linkage writes to, the measured entity with its
    # base unit, and the entity that holds its conversion records (requirement 6.12).
    "entities/a_depot.yaml": entity_doc(entity="depot", id_prefix="dep"),
    "entities/a_measure.yaml": entity_doc(entity="measure", id_prefix="msr"),
    "entities/a_supplier.yaml": entity_doc(
        fields=(
            "{ name: { type: string, required: true },"
            " measure_id: { type: ref, ref: measure, required: true } }"
        ),
        unit_conversion=(
            "{ base_unit_field: measure_id, entity: supplier_measure,"
            " material_field: supplier_id, unit_field: measure_id, factor_field: factor }"
        ),
    ),
    "entities/a_supplier_measure.yaml": entity_doc(
        entity="supplier_measure",
        id_prefix="supm",
        fields=(
            "{ supplier_id: { type: ref, ref: supplier, required: true },"
            " measure_id: { type: ref, ref: measure, required: true },"
            " factor: { type: qty, required: true } }"
        ),
    ),
    # Pricing and the quantity semantics of the lines.
    "entities/b_order.yaml": simple_bill(
        pricing="true", lines=MEASURED_LINES, line_quantity=LINE_QUANTITY
    ),
    "entities/c_entry.yaml": simple_bill(
        bill="entry",
        id_prefix="ent",
        number="{ prefix: ENTRY }",
        pricing="true",
        header="{ depot_id: { type: ref, ref: depot, required: true } }",
        lines=MEASURED_LINES,
        line_quantity=LINE_QUANTITY,
    ),
    "conversions/push.yaml": doc("""
        conversion:
          id: po_to_entry
          source: purchase_order
          target: entry
          require_source_status: approved
          limit_by: qty
          required_line_inputs: [source_line_id]
        """),
    "writebacks/back.yaml": doc("""
        writeback:
          id: entry_linked_qty
          conversion: po_to_entry
          on: approve
          target: source_line.linked_qty
          op: add
          value: line.qty_base
        """),
    "linkages/stock.yaml": doc("""
        linkage:
          id: entry_stock
          bill: entry
          primitive: inventory
          on: approve
          direction: in
          warehouse_field: header.depot_id
          qty_field: line.qty_base
        """),
    "rules/lines.yaml": rule_doc("R-CORE-001"),
    # A declarative query with a filter, and a query the inventory primitive provides.
    "tools/exposure.yaml": doc("""
        pack: core
        queries:
          - tool: core_supplier_query
            source: supplier
            filter: "header.name == 'x'"
            returns: { latest: [header.name] }
          - { tool: core_stock_query, primitive: inventory, query: balance }
        """),
}

# The namespaces FULL_FILES takes, together with those of the context entities.
FULL_IDS: Final = "[dep, msr, sup, supm, po, ent, orgu, emp]"
FULL_NUMBERS: Final = "[CGDD, ENTRY]"

# Where each primitive of FULL_FILES is used, in the order the step walks the documents:
# the entities, then the bills, then the conversions, writebacks, linkages, rules and the
# exposure (requirement 2.6).
ENTITY_FILES: Final[tuple[Place, ...]] = (
    (PACK, "entities/a_depot.yaml", "entity"),
    (PACK, "entities/a_measure.yaml", "entity"),
    (PACK, "entities/a_supplier.yaml", "entity"),
    (PACK, "entities/a_supplier_measure.yaml", "entity"),
    (PACK, "entities/employee.yaml", "entity"),
    (PACK, "entities/org_unit.yaml", "entity"),
    (PACK, "entities/b_order.yaml", "bill"),
    (PACK, "entities/c_entry.yaml", "bill"),
)
PRIMITIVE_PLACES: Final[Mapping[str, tuple[Place, ...]]] = {
    "crud": ENTITY_FILES,
    "numbering": ENTITY_FILES,
    "state_machine": (
        (PACK, "entities/b_order.yaml", "state_machine"),
        (PACK, "entities/c_entry.yaml", "state_machine"),
    ),
    "pricing": (
        (PACK, "entities/b_order.yaml", "pricing"),
        (PACK, "entities/c_entry.yaml", "pricing"),
    ),
    "unit_conversion": (
        (PACK, "entities/a_supplier.yaml", "unit_conversion"),
        (PACK, "entities/b_order.yaml", "line_quantity"),
        (PACK, "entities/c_entry.yaml", "line_quantity"),
    ),
    "conversion": ((PACK, "conversions/push.yaml", "conversion"),),
    "writeback": ((PACK, "writebacks/back.yaml", "writeback"),),
    "inventory": (
        (PACK, "linkages/stock.yaml", "linkage.primitive"),
        (PACK, "tools/exposure.yaml", "queries[1].primitive"),
    ),
    "rule_expr": (
        (PACK, "rules/lines.yaml", "rule.check"),
        (PACK, "tools/exposure.yaml", "queries[0].filter"),
    ),
}


def without(primitive: str) -> str:
    """Every M0 primitive but ``primitive``, as a manifest writes the list."""
    return "[" + ", ".join(name for name in PRIMITIVES if name != primitive) + "]"


@pytest.mark.req("2.6")
def test_a_pack_that_declares_every_primitive_it_uses_passes(tmp_path: Path) -> None:
    target = write_core(tmp_path, FULL_FILES, id_prefixes=FULL_IDS, number_prefixes=FULL_NUMBERS)

    target.run_step(5)


@pytest.mark.req("2.6")
@pytest.mark.parametrize("primitive", PRIMITIVES)
def test_a_primitive_the_manifest_does_not_declare(tmp_path: Path, primitive: str) -> None:
    found = failures(
        write_core(
            tmp_path,
            FULL_FILES,
            primitives=without(primitive),
            id_prefixes=FULL_IDS,
            number_prefixes=FULL_NUMBERS,
        )
    )

    # One error per missing primitive, locating every item of the pack that uses it.
    assert places(found) == list(PRIMITIVE_PLACES[primitive])
    assert reasons(found) == [
        f"配置使用了原语 {primitive}，业务包 core 的 manifest 的 primitives 没有声明它"
    ]


@pytest.mark.req("2.6")
def test_a_query_the_primitive_does_not_provide(tmp_path: Path) -> None:
    exposure = doc("""
        pack: core
        queries:
          - { tool: core_stock_query, primitive: inventory, query: on_hand }
        """)
    found = failures(write_core(tmp_path, {"tools/exposure.yaml": exposure}))

    assert places(found) == [(PACK, "tools/exposure.yaml", "queries[0].query")]
    assert reasons(found) == ["原语 inventory 不提供查询 on_hand"]


# --- the context entities (requirement 1.14) ---------------------------------------------


@pytest.mark.req("1.14")
@pytest.mark.parametrize(
    ("kept", "missing"),
    [("entities/employee.yaml", "组织上下文实体"), ("entities/org_unit.yaml", "用户上下文实体")],
    ids=["no_org", "no_user"],
)
def test_a_context_role_no_pack_declares(tmp_path: Path, kept: str, missing: str) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={kept: CONTEXT_FILES[kept]},
        id_prefixes=CONTEXT_IDS,
        number_prefixes="[]",
    )
    found = failures(loaded(tmp_path))

    # No configuration item corresponds to the role that is missing, so the error is
    # reported at the packs of the instance configuration.
    assert places(found) == [(None, "instance.yaml", "packs")]
    assert reasons(found) == [f"引用的业务包都没有声明{missing}"]


@pytest.mark.req("1.14")
def test_two_entities_that_declare_the_same_context_role(tmp_path: Path) -> None:
    second = entity_doc(
        entity="branch",
        id_prefix="brc",
        context="{ role: org, default_flag: is_default }",
        fields="{ name: { type: string }, is_default: { type: bool } }",
    )
    found = failures(
        write_core(tmp_path, {"entities/branch.yaml": second}, id_prefixes="[brc, orgu, emp]")
    )

    # Every pack and entity that declares the role is listed, each at its own ``context``.
    assert places(found) == [
        (PACK, "entities/branch.yaml", "context.role"),
        (PACK, "entities/org_unit.yaml", "context.role"),
    ]
    assert reasons(found) == [
        "声明为组织上下文实体的实体不止一个：业务包 core 的实体 branch、业务包 core 的实体 org_unit"
    ]


# --- the instance configuration (requirement 2.10) ---------------------------------------


@pytest.mark.req("2.10")
def test_a_profile_that_selects_a_pack_the_instance_does_not_reference(tmp_path: Path) -> None:
    definitions = {**DEFINITIONS, "profiles/semantic.yaml": profile_doc(packs=f"[{PACK}, {OTHER}]")}
    write_instance(tmp_path / INSTANCE_DIR, definitions)
    write_context_pack(tmp_path)
    found = failures(loaded(tmp_path))

    assert places(found) == [(None, "profiles/semantic.yaml", "packs[1]")]
    assert reasons(found) == ["profile semantic 的 packs 含 instance.yaml 未引用的业务包 purchase"]


@pytest.mark.req("2.10")
def test_a_default_profile_that_is_not_listed(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS, default_profile="generic")
    write_context_pack(tmp_path)
    found = failures(loaded(tmp_path))

    assert places(found) == [(None, "instance.yaml", "default_profile")]
    assert reasons(found) == ["default_profile generic 不在 profiles 中"]


@pytest.mark.req("2.10")
def test_a_default_dataset_that_is_not_listed(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS, default_dataset="month-end")
    write_context_pack(tmp_path)
    found = failures(loaded(tmp_path))

    assert places(found) == [(None, "instance.yaml", "default_dataset")]
    assert reasons(found) == ["default_dataset month-end 不在 datasets 中"]


@pytest.mark.req("2.10")
def test_a_dataset_that_includes_the_shared_data_of_an_unreferenced_pack(tmp_path: Path) -> None:
    definitions = {
        **DEFINITIONS,
        "datasets/purchase-basic.yaml": dataset_doc(include_shared=f"[{PACK}, {OTHER}]"),
    }
    write_instance(tmp_path / INSTANCE_DIR, definitions)
    write_context_pack(tmp_path)
    found = failures(loaded(tmp_path))

    assert places(found) == [(None, "datasets/purchase-basic.yaml", "include_shared[1]")]
    assert reasons(found) == [
        "初始数据集 purchase-basic 的 include_shared 含 instance.yaml 未引用的业务包 purchase"
    ]
