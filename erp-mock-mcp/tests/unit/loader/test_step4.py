"""erp_mock.loader.step4: every configuration file of a pack, and the instance definitions.

Step 4 is run through :class:`~erp_mock.loader.loader.Loader`, the way loading runs it, on a
tree written below ``tmp_path`` by tests.helpers.tree; step 1 runs first, since this step
reads the packs it accepted and the instance.yaml it validated. Each test writes exactly the
one thing that is wrong and asserts the place of the error, the (pack, file, config path) the
loader reports (requirement 2.2), since that place is what points the author of the pack at
the offending item.

The models themselves are covered by tests/unit/config; what is covered here is which files
the step reads, how it tells an entity document from a bill document, that a pydantic error
of any of those files reaches the report at its config path (requirements 1.9 and 1.11 to
1.13), and the checks of the instance profiles and datasets that belong to this step
(requirement 2.13).
"""

import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from erp_mock.loader.errors import ConfigError, ConfigErrors
from erp_mock.loader.loader import Loader
from tests.helpers.tree import (
    bill_doc,
    dataset_doc,
    entity_doc,
    pack_doc,
    profile_doc,
    write_instance,
    write_pack,
    write_tree,
)

INSTANCE_DIR = "instance"
PACKS_DIR = "packs"

# The pack the default instance.yaml of tests.helpers.tree references.
PACK = "core"

# One place of a reported error: the pack, the file and the config path.
type Place = tuple[str | None, str, str | None]


def doc(text: str) -> str:
    """A YAML document written indented in the test source."""
    return textwrap.dedent(text).lstrip("\n")


# The definitions the default instance.yaml names: one profile and one dataset, both valid,
# so that a test about a business pack reports nothing of the instance configuration.
DEFINITIONS: Final[Mapping[str, str]] = {
    "profiles/semantic.yaml": profile_doc(),
    "datasets/purchase-basic.yaml": dataset_doc(),
}

# One valid document of every kind a business pack holds (business-pack.md section 3).
PACK_FILES: Final[Mapping[str, str]] = {
    "entities/supplier.yaml": entity_doc(),
    "entities/purchase_order.yaml": bill_doc(),
    "conversions/po_to_receipt.yaml": doc("""
        conversion:
          id: po_to_receipt
          source: purchase_order
          target: purchase_receipt
          require_source_status: approved
          limit_by: qty
          line_mapping: { material_id: source_line.material_id }
          required_line_inputs: [qty]
        """),
    "writebacks/po_linked_qty.yaml": doc("""
        writeback:
          id: po_linked_qty
          conversion: po_to_receipt
          on: approve
          target: source_line.linked_qty
          op: add
          value: line.qty_base
        """),
    "linkages/purchase_receipt_inventory.yaml": doc("""
        linkage:
          id: purchase_receipt_inventory
          bill: purchase_receipt
          primitive: inventory
          on: approve
          direction: in
          warehouse_field: header.warehouse_id
          qty_field: line.qty_base
        """),
    "rules/lines_not_empty.yaml": doc("""
        rule:
          id: R-CORE-001
          bill: purchase_order
          enforcement: enforced
          on: [submit]
          check: count(lines) > 0
          error_code: VALIDATION_FAILED
          message: { zh: 采购订单至少要有一行分录 }
        """),
    "tools/exposure.yaml": doc("""
        pack: core
        exclude: [supplier_delete]
        """),
    "tools/descriptions/zh-detailed.yaml": doc("""
        variant: zh-detailed
        tools:
          supplier_search:
            description: 按关键字检索供应商。
            params: { keyword: 关键字，匹配编码与名称 }
        """),
    "seed/generators.yaml": doc("""
        generators:
          supplier.name: { gen: company_name, industry: 机械 }
        """),
    "seed/shared/org.yaml": doc("""
        records:
          - { kind: org, code: "001", name: 示例集团, is_default: true }
        """),
}


def loaded(tmp_path: Path) -> Loader:
    """A loader for the tree below ``tmp_path`` with step 1 run; it must pass."""
    target = Loader(tmp_path / INSTANCE_DIR, tmp_path / PACKS_DIR)
    target.run_step(1)
    return target


def failures(target: Loader) -> tuple[ConfigError, ...]:
    """The errors step 4 reports for ``target``, in report order; it must find some."""
    with pytest.raises(ConfigErrors) as caught:
        target.run_step(4)
    assert caught.value.step == 4
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


def pack_errors(tmp_path: Path, files: Mapping[str, str]) -> tuple[ConfigError, ...]:
    """The step 4 errors of a pack ``core`` holding ``files``, next to a valid instance."""
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(tmp_path / PACKS_DIR, PACK, files=files)
    return failures(loaded(tmp_path))


def instance_errors(
    tmp_path: Path, files: Mapping[str, str], **overrides: str | None
) -> tuple[ConfigError, ...]:
    """The step 4 errors of an instance holding ``files``, next to a pack without documents."""
    write_instance(tmp_path / INSTANCE_DIR, files, **overrides)
    write_pack(tmp_path / PACKS_DIR, PACK)
    return failures(loaded(tmp_path))


# --- a tree step 4 accepts ---------------------------------------------------------------


@pytest.mark.req("1.11", "2.13")
def test_step4_reads_every_kind_of_configuration_file(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(tmp_path / PACKS_DIR, PACK, files=PACK_FILES)
    target = loaded(tmp_path)

    target.run_step(4)

    raw = target.raw_packs[0]
    assert (raw.name, raw.version, raw.directory) == (PACK, "1.0.0", tmp_path / PACKS_DIR / PACK)
    assert raw.manifest.rule_prefix == "CORE"
    assert [(item.file, item.document.entity) for item in raw.entities] == [
        ("entities/supplier.yaml", "supplier")
    ]
    assert [(item.file, item.document.bill) for item in raw.bills] == [
        ("entities/purchase_order.yaml", "purchase_order")
    ]
    # A conversion, a writeback, a linkage and a rule are kept as the object below the one
    # key of their document.
    assert [item.document.id for item in raw.conversions] == ["po_to_receipt"]
    assert [item.document.id for item in raw.writebacks] == ["po_linked_qty"]
    assert [item.document.id for item in raw.linkages] == ["purchase_receipt_inventory"]
    assert [item.document.id for item in raw.rules] == ["R-CORE-001"]
    assert raw.exposure is not None
    assert (raw.exposure.file, raw.exposure.document.pack) == ("tools/exposure.yaml", PACK)
    assert [item.document.variant for item in raw.descriptions] == ["zh-detailed"]
    assert raw.generators is not None
    assert tuple(raw.generators.document.generators) == ("supplier.name",)
    assert [item.file for item in raw.shared_data] == ["seed/shared/org.yaml"]
    assert [item.document.profile for item in target.raw_profiles] == ["semantic"]
    assert [item.document.dataset for item in target.raw_datasets] == ["purchase-basic"]


@pytest.mark.req("1.11")
def test_a_pack_that_holds_nothing_but_its_manifest_passes(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(tmp_path / PACKS_DIR, PACK)
    target = loaded(tmp_path)

    target.run_step(4)

    # Every directory and every file of a pack is optional at this step: what a pack ought
    # to declare is settled by the steps after it.
    raw = target.raw_packs[0]
    assert raw.entities == () and raw.bills == ()
    assert raw.conversions == () and raw.writebacks == () and raw.linkages == ()
    assert raw.rules == () and raw.descriptions == () and raw.shared_data == ()
    assert raw.exposure is None and raw.generators is None


@pytest.mark.req("1.11")
def test_the_files_of_a_directory_are_read_in_path_order(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={
            "entities/b.yaml": entity_doc(entity="unit", id_prefix="unit"),
            "entities/a/z.yaml": entity_doc(entity="warehouse", id_prefix="wh"),
            "entities/a.yaml": entity_doc(entity="supplier"),
        },
    )
    target = loaded(tmp_path)

    target.run_step(4)

    # Sorted by the relative path, subdirectories included, so the order never depends on
    # the file system (requirement 24).
    assert [item.file for item in target.raw_packs[0].entities] == [
        "entities/a.yaml",
        "entities/a/z.yaml",
        "entities/b.yaml",
    ]


@pytest.mark.req("2.1")
def test_example_data_and_pack_tests_are_not_read(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={
            # Neither parses; neither is read (requirement 20.8, business-pack.md 3).
            "seed/examples/suppliers.yaml": "records: [\n",
            "tests/rules/lines_not_empty.yaml": "case: [\n",
        },
    )
    target = loaded(tmp_path)

    target.run_step(4)

    assert target.raw_packs[0].shared_data == ()


@pytest.mark.req("2.1")
def test_a_directory_of_packs_dir_that_is_not_referenced_is_not_read(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(tmp_path / PACKS_DIR, PACK)
    # A pack instance.yaml does not reference, with a document that does not even parse.
    write_tree(
        tmp_path / PACKS_DIR,
        {
            "legacy/pack.yaml": pack_doc(pack="legacy"),
            "legacy/entities/supplier.yaml": "entity: [\n",
        },
    )
    target = loaded(tmp_path)

    target.run_step(4)

    assert [raw.name for raw in target.raw_packs] == [PACK]


# --- entities/: an entity document or a bill document (requirement 1.11) -----------------


@pytest.mark.req("1.11")
def test_an_entity_and_a_bill_are_told_apart_by_the_key_of_the_document(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    # The file names say nothing about the kind; the top-level key decides.
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={"entities/one.yaml": bill_doc(), "entities/two.yaml": entity_doc()},
    )
    target = loaded(tmp_path)

    target.run_step(4)

    raw = target.raw_packs[0]
    assert [item.file for item in raw.bills] == ["entities/one.yaml"]
    assert [item.file for item in raw.entities] == ["entities/two.yaml"]


@pytest.mark.req("1.11")
def test_a_file_in_entities_that_names_neither_entity_nor_bill(tmp_path: Path) -> None:
    found = pack_errors(tmp_path, {"entities/supplier.yaml": "object: supplier\n"})

    assert places(found) == [(PACK, "entities/supplier.yaml", None)]
    assert reasons(found) == ["entities/ 下的配置文件必须以 entity 或 bill 为顶层键声明实体或单据"]


@pytest.mark.req("1.11")
def test_a_file_in_entities_that_names_both_entity_and_bill(tmp_path: Path) -> None:
    found = pack_errors(tmp_path, {"entities/supplier.yaml": bill_doc(entity="supplier")})

    assert places(found) == [(PACK, "entities/supplier.yaml", None)]
    assert reasons(found) == ["entities/ 下的配置文件不能同时以 entity 和 bill 为顶层键"]


@pytest.mark.req("1.11")
@pytest.mark.parametrize(
    "text", ["- supplier\n", "supplier\n", ""], ids=["sequence", "scalar", "empty"]
)
def test_a_file_in_entities_that_is_not_a_mapping(tmp_path: Path, text: str) -> None:
    found = pack_errors(tmp_path, {"entities/supplier.yaml": text})

    # An error about the document as a whole has no config path (requirement 2.2).
    assert places(found) == [(PACK, "entities/supplier.yaml", None)]
    assert reasons(found) == ["entities/ 下的配置文件必须是映射"]


# --- a document that does not fit its model (requirements 1.9 and 1.11 to 1.13) ----------


@pytest.mark.req("1.9")
def test_a_bill_without_state_machine(tmp_path: Path) -> None:
    found = pack_errors(tmp_path, {"entities/purchase_order.yaml": bill_doc(state_machine=None)})

    assert places(found) == [(PACK, "entities/purchase_order.yaml", "state_machine")]


@pytest.mark.req("1.9")
@pytest.mark.parametrize("value", ["Standard", "approval", "none", "[standard]"])
def test_a_bill_whose_state_machine_is_neither_standard_nor_simple(
    tmp_path: Path, value: str
) -> None:
    found = pack_errors(tmp_path, {"entities/purchase_order.yaml": bill_doc(state_machine=value)})

    assert places(found) == [(PACK, "entities/purchase_order.yaml", "state_machine")]


@pytest.mark.req("1.11")
@pytest.mark.parametrize(
    ("file", "text", "config_path"),
    [
        ("entities/supplier.yaml", entity_doc(fields=None), "fields"),
        ("entities/supplier.yaml", entity_doc(label_zh="供应商"), "label_zh"),
        ("entities/supplier.yaml", entity_doc(entity="Supplier"), "entity"),
        ("entities/supplier.yaml", entity_doc(fields="{ Name: { type: string } }"), "fields.Name"),
        (
            "conversions/po_to_receipt.yaml",
            doc("""
                conversion:
                  id: po_to_receipt
                  source: purchase_order
                  target: purchase_receipt
                  require_source_status: approved
                  limit_by: qty
                """),
            "conversion.required_line_inputs",
        ),
        (
            "writebacks/po_linked_qty.yaml",
            doc("""
                writeback:
                  id: po_linked_qty
                  conversion: po_to_receipt
                  on: unapprove
                  target: source_line.linked_qty
                  op: add
                  value: line.qty_base
                """),
            "writeback.on",
        ),
        (
            "linkages/stock_in.yaml",
            doc("""
                linkage:
                  id: stock_in
                  bill: purchase_receipt
                  primitive: partner_balance
                  on: approve
                  direction: in
                  qty_field: line.qty_base
                """),
            "linkage.primitive",
        ),
        (
            "rules/lines_not_empty.yaml",
            doc("""
                rule:
                  id: R-CORE-001
                  enforcement: strict
                """),
            "rule.enforcement",
        ),
        ("tools/exposure.yaml", "exclude: [supplier_delete]\n", "pack"),
        ("tools/descriptions/zh-detailed.yaml", "variant: ''\ntools: {}\n", "variant"),
        ("seed/generators.yaml", "generators:\n  supplier: { gen: company_name }\n", "generators.supplier"),
        ("seed/shared/org.yaml", "records:\n  - { code: '001' }\n", "records[0].kind"),
    ],
    ids=[
        "entity_missing_key",
        "entity_undefined_key",
        "entity_name",
        "entity_field_name",
        "conversion",
        "writeback",
        "linkage",
        "rule",
        "exposure",
        "description",
        "generators",
        "shared_data",
    ],
)  # fmt: skip
def test_a_document_that_does_not_fit_its_model(
    tmp_path: Path, file: str, text: str, config_path: str
) -> None:
    found = pack_errors(tmp_path, {file: text})

    # The config path of a document that nests its object below one key keeps that key.
    assert places(found) == [(PACK, file, config_path)]


@pytest.mark.req("1.12")
@pytest.mark.parametrize(
    ("file", "text", "config_path"),
    [
        (
            "entities/supplier.yaml",
            entity_doc(fields="{ supplier: { type: ref, ref: supplier } }"),
            "fields.supplier",
        ),
        (
            "entities/supplier.yaml",
            entity_doc(fields="{ org_id: { type: ref, ref: org }, org: { type: string } }"),
            "fields.org_id",
        ),
        (
            "entities/supplier.yaml",
            entity_doc(fields="{ org_id: { type: ref } }"),
            "fields.org_id.ref",
        ),
        (
            "entities/purchase_order.yaml",
            bill_doc(header="{ supplier_id: { type: ref, ref: supplier }, supplier: { type: string } }"),
            "header.supplier_id",
        ),
    ],
    ids=["suffix", "conflict", "no_target", "header_conflict"],
)  # fmt: skip
def test_a_ref_field_whose_name_or_target_is_wrong(
    tmp_path: Path, file: str, text: str, config_path: str
) -> None:
    found = pack_errors(tmp_path, {file: text})

    assert places(found) == [(PACK, file, config_path)]


@pytest.mark.req("1.13")
@pytest.mark.parametrize(
    ("values", "config_path"),
    [
        ("{}", "fields.reason.values"),
        ("{ Gain: { zh: 盘盈, en: Gain } }", "fields.reason.values.Gain"),
        ("{ gain: { zh: 盘盈 } }", "fields.reason.values.gain.en"),
    ],
    ids=["empty", "code", "label"],
)
def test_an_enum_field_whose_values_are_wrong(
    tmp_path: Path, values: str, config_path: str
) -> None:
    fields = f"{{ reason: {{ type: enum, values: {values} }} }}"
    found = pack_errors(tmp_path, {"entities/supplier.yaml": entity_doc(fields=fields)})

    assert places(found) == [(PACK, "entities/supplier.yaml", config_path)]


@pytest.mark.req("1.13", "2.2")
def test_a_repeated_enum_code_is_reported_at_its_line(tmp_path: Path) -> None:
    document = doc("""
        entity: stock_adjustment_reason
        id_prefix: sar
        fields:
          reason:
            type: enum
            values:
              gain: { zh: 盘盈, en: Gain }
              gain: { zh: 盘亏, en: Loss }
        """)
    found = pack_errors(tmp_path, {"entities/reason.yaml": document})

    # A key that repeats within a mapping is a YAML error, whose config path is its line.
    assert places(found) == [(PACK, "entities/reason.yaml", "line 8")]
    assert found[0].reason.startswith("YAML 无法解析：重复的键")


# --- files that cannot be read (requirement 2.2) -----------------------------------------


@pytest.mark.req("1.11", "2.2")
def test_an_unparseable_configuration_file_is_reported_at_its_line(tmp_path: Path) -> None:
    found = pack_errors(tmp_path, {"rules/lines_not_empty.yaml": "rule:\n  id: *missing\n"})

    assert places(found) == [(PACK, "rules/lines_not_empty.yaml", "line 2")]
    assert found[0].reason.startswith("YAML 无法解析：")


@pytest.mark.req("1.11", "2.2")
def test_a_configuration_directory_that_cannot_be_listed(tmp_path: Path) -> None:
    # A file where the step expects a directory: the error is about the directory itself.
    found = pack_errors(tmp_path, {"entities": "entity: supplier\n"})

    assert places(found) == [(PACK, "entities", None)]
    assert found[0].reason.startswith("无法读取目录：")


# --- every file of every pack (requirement 2.2) ------------------------------------------


@pytest.mark.req("2.2")
def test_step_4_reports_every_file_of_every_pack_in_instance_order(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS, packs="{ purchase: 1.0.0, core: 1.0.0 }")
    write_pack(
        tmp_path / PACKS_DIR,
        "purchase",
        rule_prefix="PUR",
        tool_prefixes="[pur_]",
        files={
            "entities/purchase_order.yaml": bill_doc(state_machine=None),
            "entities/a_supplier.yaml": entity_doc(entity="Supplier"),
        },
    )
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={"entities/org.yaml": entity_doc(entity="org", id_prefix="org", fields=None)},
    )

    found = failures(loaded(tmp_path))

    # Every file is read; the errors follow the pack order of instance.yaml and, inside a
    # pack, the file path.
    assert places(found) == [
        ("purchase", "entities/a_supplier.yaml", "entity"),
        ("purchase", "entities/purchase_order.yaml", "state_machine"),
        (PACK, "entities/org.yaml", "fields"),
    ]


@pytest.mark.req("2.2")
def test_every_error_of_one_document_is_reported(tmp_path: Path) -> None:
    document = entity_doc(entity="Supplier", id_prefix=None, shared="'true'")
    found = pack_errors(tmp_path, {"entities/supplier.yaml": document})

    assert places(found) == [
        (PACK, "entities/supplier.yaml", "entity"),
        (PACK, "entities/supplier.yaml", "id_prefix"),
        (PACK, "entities/supplier.yaml", "shared"),
    ]


@pytest.mark.req("2.2")
def test_a_step_4_error_is_reported_as_one_line(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(tmp_path / PACKS_DIR, PACK, files={"entities/supplier.yaml": "object: supplier\n"})

    with pytest.raises(ConfigErrors) as caught:
        loaded(tmp_path).run_step(4)

    assert caught.value.lines() == (
        "[step 4] core: entities/supplier.yaml:"
        " entities/ 下的配置文件必须以 entity 或 bill 为顶层键声明实体或单据",
    )


# --- the profiles and datasets of the instance configuration (requirement 2.13) ----------


@pytest.mark.req("2.13")
def test_a_definition_is_matched_by_its_field_and_not_by_its_file_name(tmp_path: Path) -> None:
    write_instance(
        tmp_path / INSTANCE_DIR,
        {"profiles/any-name.yaml": profile_doc(), "datasets/other.yaml": dataset_doc()},
    )
    write_pack(tmp_path / PACKS_DIR, PACK)
    target = loaded(tmp_path)

    target.run_step(4)

    assert [item.file for item in target.raw_profiles] == ["profiles/any-name.yaml"]
    assert [item.file for item in target.raw_datasets] == ["datasets/other.yaml"]


@pytest.mark.req("2.13")
@pytest.mark.parametrize(
    ("files", "config_path", "reason"),
    [
        (
            {"datasets/purchase-basic.yaml": dataset_doc()},
            "profiles[0]",
            "profiles/ 中没有 profile 为 semantic 的定义",
        ),
        (
            {"profiles/semantic.yaml": profile_doc()},
            "datasets[0]",
            "datasets/ 中没有 dataset 为 purchase-basic 的定义",
        ),
        (
            dict(DEFINITIONS) | {"profiles/semantic.yaml": profile_doc(profile="other")},
            "profiles[0]",
            "profiles/ 中没有 profile 为 semantic 的定义",
        ),
    ],
    ids=["no_profiles_dir", "no_datasets_dir", "another_name"],
)
def test_a_listed_name_without_a_definition(
    tmp_path: Path, files: Mapping[str, str], config_path: str, reason: str
) -> None:
    found = instance_errors(tmp_path, files)

    assert places(found) == [(None, "instance.yaml", config_path)]
    assert reasons(found) == [reason]


@pytest.mark.req("2.13")
@pytest.mark.parametrize(
    ("file", "text", "config_path"),
    [
        ("profiles/semantic.yaml", profile_doc(packs=None), "packs"),
        ("profiles/semantic.yaml", profile_doc(surface="[semantic]"), "surface"),
        ("datasets/purchase-basic.yaml", dataset_doc(random_seed="-1"), "random_seed"),
        (
            "datasets/purchase-basic.yaml",
            dataset_doc(periods="{ closed_before: today }"),
            "periods.closed_before",
        ),
        (
            "datasets/purchase-basic.yaml",
            dataset_doc(entities="\n  supplier:main: { name: 示例 }"),
            "entities.supplier:main.kind",
        ),
    ],
    ids=["profile_missing_key", "profile_value", "dataset_value", "dataset_period", "dataset_record"],
)  # fmt: skip
def test_a_listed_definition_that_does_not_fit_its_model(
    tmp_path: Path, file: str, text: str, config_path: str
) -> None:
    found = instance_errors(tmp_path, dict(DEFINITIONS) | {file: text})

    assert places(found) == [(None, file, config_path)]


@pytest.mark.req("2.13")
def test_a_definition_the_instance_does_not_list_is_not_validated(tmp_path: Path) -> None:
    write_instance(
        tmp_path / INSTANCE_DIR,
        # A profile and a dataset that no list names, neither of which fits its model.
        dict(DEFINITIONS)
        | {
            "profiles/generic.yaml": profile_doc(profile="generic", packs=None),
            "datasets/ap-month-end.yaml": dataset_doc(dataset="ap-month-end", version=None),
        },
    )
    write_pack(tmp_path / PACKS_DIR, PACK)
    target = loaded(tmp_path)

    target.run_step(4)

    assert [item.document.profile for item in target.raw_profiles] == ["semantic"]
    assert [item.document.dataset for item in target.raw_datasets] == ["purchase-basic"]


@pytest.mark.req("2.13")
@pytest.mark.parametrize(
    ("listed", "files", "reason"),
    [
        (
            "[semantic, generic]",
            {"profiles/generic.yaml": profile_doc(profile="generic")},
            "profiles 只能含 semantic 一项，当前为 semantic、generic",
        ),
        (
            "[generic]",
            {"profiles/generic.yaml": profile_doc(profile="generic")},
            "profiles 只能含 semantic 一项，当前为 generic",
        ),
        ("[]", {}, "profiles 只能含 semantic 一项，当前为空"),
    ],
    ids=["two", "another", "none"],
)
def test_profiles_must_hold_semantic_alone(
    tmp_path: Path, listed: str, files: Mapping[str, str], reason: str
) -> None:
    found = instance_errors(tmp_path, dict(DEFINITIONS) | dict(files), profiles=listed)

    assert places(found) == [(None, "instance.yaml", "profiles")]
    assert reasons(found) == [reason]


@pytest.mark.req("2.13")
@pytest.mark.parametrize(
    ("override", "config_path", "reason"),
    [
        ({"surface": "generic"}, "surface", "profile semantic 的 surface 必须是 semantic"),
        (
            {"description_variant": "zh-brief"},
            "description_variant",
            "profile semantic 的 description_variant 必须是 zh-detailed",
        ),
        (
            {"include_deprecated": "true"},
            "include_deprecated",
            "profile semantic 的 include_deprecated 必须是 false",
        ),
    ],
    ids=["surface", "description_variant", "include_deprecated"],
)
def test_the_three_values_m0_fixes_for_the_semantic_profile(
    tmp_path: Path, override: Mapping[str, str], config_path: str, reason: str
) -> None:
    # The models accept the other surface and the other variants, so these three values are
    # checked here and reported at the item of the profile definition that holds them.
    files = dict(DEFINITIONS) | {"profiles/semantic.yaml": profile_doc(**override)}
    found = instance_errors(tmp_path, files)

    assert places(found) == [(None, "profiles/semantic.yaml", config_path)]
    assert reasons(found) == [reason]


@pytest.mark.req("2.13")
def test_two_definitions_of_one_name_are_one_error_that_names_both_files(tmp_path: Path) -> None:
    files = {
        "profiles/first.yaml": profile_doc(),
        "profiles/second.yaml": profile_doc(),
        "datasets/purchase-basic.yaml": dataset_doc(),
    }
    write_instance(tmp_path / INSTANCE_DIR, files)
    write_pack(tmp_path / PACKS_DIR, PACK)
    target = loaded(tmp_path)

    found = failures(target)

    assert len(found) == 1
    assert places(found) == [
        (None, "profiles/first.yaml", "profile"),
        (None, "profiles/second.yaml", "profile"),
    ]
    assert reasons(found) == ["profiles/ 中有多个 profile 为 semantic 的定义"]
    assert target.raw_profiles == ()


@pytest.mark.req("2.2", "2.13")
def test_an_unparseable_definition_is_reported_at_its_line_and_leaves_its_name_unmatched(
    tmp_path: Path,
) -> None:
    files = dict(DEFINITIONS) | {"profiles/semantic.yaml": "profile: *missing\n"}
    found = instance_errors(tmp_path, files)

    assert places(found) == [
        (None, "instance.yaml", "profiles[0]"),
        (None, "profiles/semantic.yaml", "line 1"),
    ]


@pytest.mark.req("2.13")
def test_the_definitions_keep_the_order_of_instance_yaml(tmp_path: Path) -> None:
    files = {
        "profiles/semantic.yaml": profile_doc(),
        "datasets/a.yaml": dataset_doc(dataset="a-set"),
        "datasets/b.yaml": dataset_doc(dataset="b-set"),
    }
    write_instance(
        tmp_path / INSTANCE_DIR,
        files,
        datasets="[b-set, a-set, b-set]",
        default_dataset="b-set",
    )
    write_pack(tmp_path / PACKS_DIR, PACK)
    target = loaded(tmp_path)

    target.run_step(4)

    # The order of instance.yaml, and a name it repeats is read once.
    assert [item.document.dataset for item in target.raw_datasets] == ["b-set", "a-set"]
