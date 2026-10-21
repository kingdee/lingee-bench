"""schemas/: the JSON Schema of every root configuration model (requirement 1.1).

The configuration models of :mod:`erp_mock.config` are the only source of truth, and
``scripts/gen_schemas.py`` exports each root model to ``schemas/<kind>.schema.json``. Two
things are checked here:

- the files in the repository are byte for byte what the models export right now, so
  schemas/ cannot drift from the models (design.md 2.2);
- a set of documents is validated twice, once with jsonschema against the exported schema
  and once with the model, and the two agree on every one of them.

The models are the stricter of the two, so a document is chosen for the second check only
when both can judge it. What a JSON Schema cannot express, and what therefore stays a
model-only check, is:

- a calendar date behind a well formed one, such as ``2026-02-30`` - except that the date
  of a field default carries ``format: date``, which a format checker does validate;
- the decimals of a decimal value against its field type (``"0.12345"`` on a rate field),
  a decimal default outside the field's ``values``, and an enum default outside them;
- the length of a string default against the field's ``maxLength``;
- the name of a ref field: the ``_id`` suffix and the conflict of the name without it with
  another field of the same level (requirement 1.12);
- the reserved ``kind`` key of a shared data or dataset record;
- ``anchor_date`` being required when the clock mode is fixed, and ``timezone`` being a
  name of the IANA time zone database.

Loading therefore validates with the models, never with these files (design.md 2.3); the
schemas serve business pack authors, whose editor uses them for completion and for a first
validation of a document. Documents are read with ``loader.yaml_io``, the way a loading
step reads a file, so both checks see exactly the values a pack author writes.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from erp_mock.loader.yaml_io import parse_yaml
from scripts.gen_schemas import (
    DIALECT,
    REPO_ROOT,
    ROOT_MODELS,
    SCHEMAS_DIR,
    SUFFIX,
    build_schema,
    file_name,
    render_schema,
    stale_files,
)

# Exports every schema into the directory given as the first argument; run with -c from
# the repository root, which puts it on sys.path like pytest's pythonpath setting does.
EXPORT_SCRIPT: Final = (
    "import sys\n"
    "from pathlib import Path\n"
    "from scripts.gen_schemas import write_schemas\n"
    "write_schemas(Path(sys.argv[1]))\n"
)

# The 14 kinds of configuration document of M0 (requirement 1.1): the files of a business
# pack, then the files of an instance configuration.
EXPECTED_KINDS: Final = (
    "pack",
    "entity",
    "bill",
    "conversion",
    "writeback",
    "linkage",
    "rule",
    "exposure",
    "description",
    "seed-generators",
    "shared-data",
    "instance",
    "profile",
    "dataset",
)

FILE = Path("document.yaml")


def validator(kind: str) -> Draft202012Validator:
    """A draft 2020-12 validator for the exported schema of ``kind``, checking formats."""
    return Draft202012Validator(
        build_schema(kind), format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def model_accepts(kind: str, data: Any) -> bool:
    """Whether the root model of ``kind`` validates ``data``, as a loading step does."""
    try:
        ROOT_MODELS[kind].model_validate(data)
    except ValidationError:
        return False
    return True


# --- schemas/ is what the models export --------------------------------------------------


@pytest.mark.req("1.1")
def test_every_kind_of_configuration_document_has_a_root_model() -> None:
    assert tuple(ROOT_MODELS) == EXPECTED_KINDS


@pytest.mark.req("1.1")
def test_schemas_directory_holds_exactly_the_exported_schemas() -> None:
    found = sorted(path.name for path in SCHEMAS_DIR.iterdir() if path.name.endswith(SUFFIX))
    assert found == sorted(file_name(kind) for kind in ROOT_MODELS)
    assert stale_files(SCHEMAS_DIR) == ()


@pytest.mark.req("1.1")
@pytest.mark.parametrize("kind", EXPECTED_KINDS)
def test_schema_file_is_byte_for_byte_what_the_model_exports(kind: str) -> None:
    path = SCHEMAS_DIR / file_name(kind)
    assert path.read_bytes() == render_schema(kind), (
        f"{path.name} differs from {ROOT_MODELS[kind].__name__};"
        " run `uv run python scripts/gen_schemas.py`"
    )


@pytest.mark.req("1.1")
def test_export_is_reproducible_in_another_process(tmp_path: Path) -> None:
    """A second process, with another PYTHONHASHSEED, writes the same bytes.

    The files in schemas/ were written by yet another process, so this compares three
    hash seeds; nothing in a schema may depend on the iteration order of a set or on the
    address of an object.
    """
    result = subprocess.run(
        [sys.executable, "-c", EXPORT_SCRIPT, str(tmp_path)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONHASHSEED": "12345"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for kind in ROOT_MODELS:
        name = file_name(kind)
        assert (tmp_path / name).read_bytes() == (SCHEMAS_DIR / name).read_bytes(), name


@pytest.mark.req("1.1")
@pytest.mark.parametrize("kind", EXPECTED_KINDS)
def test_exported_schema_is_a_draft_2020_12_schema(kind: str) -> None:
    schema = build_schema(kind)
    assert list(schema)[:2] == ["$schema", "$id"]
    assert schema["$schema"] == DIALECT
    assert schema["$id"] == file_name(kind)
    assert schema["type"] == "object"
    # Undefined keys are rejected by the schema as the models reject them (req 1.11).
    assert schema["additionalProperties"] is False
    Draft202012Validator.check_schema(schema)


# --- jsonschema and the models agree on these documents ----------------------------------

# Each sample is a kind, a YAML document and whether it is valid. The invalid ones cover
# the error kinds of requirement 1.11: a missing required key, an undefined key, a value of
# the wrong type, a value outside a fixed set and a value that does not match its grammar.
SAMPLES: Final[tuple[tuple[str, str, bool], ...]] = (
    # --- pack.yaml ---
    (
        "pack",
        """
        pack: purchase
        version: 1.0.0
        title: { zh: 采购, en: Purchase }
        requires_meta_model: ">=0.1.0 <1.0.0"
        depends_on: { core: ">=0.1.0 <1.0.0" }
        primitives: [crud, numbering, pricing, state_machine, conversion, writeback, rule_expr]
        rule_prefix: PUR
        id_prefixes: [preq, po, prcv]
        number_prefixes: [CGSQ, CGDD, CGRK]
        tool_prefixes: [purchase_]
        description_variants: [zh-detailed]
        license: Apache-2.0
        maintainers: ["示例团队"]
        """,
        True,
    ),
    (
        # primitives is missing.
        "pack",
        """
        pack: purchase
        version: 1.0.0
        title: { zh: 采购 }
        requires_meta_model: 0.1.0
        rule_prefix: PUR
        id_prefixes: [po]
        number_prefixes: [CGDD]
        tool_prefixes: [purchase_]
        description_variants: [zh-detailed]
        license: Apache-2.0
        maintainers: []
        """,
        False,
    ),
    (
        # version is not a concrete <major>.<minor>.<patch>, and voucher is not an M0
        # primitive (requirement 2.6).
        "pack",
        """
        pack: purchase
        version: "1.0"
        title: { zh: 采购 }
        requires_meta_model: 0.1.0
        primitives: [crud, voucher]
        rule_prefix: PUR
        id_prefixes: [po]
        number_prefixes: [CGDD]
        tool_prefixes: [purchase_]
        description_variants: [zh-detailed]
        license: Apache-2.0
        maintainers: []
        """,
        False,
    ),
    # --- entities/*.yaml: an entity ---
    (
        "entity",
        """
        entity: supplier
        label: { zh: 供应商, en: Supplier }
        id_prefix: sup
        code: { prefix: S, digits: 4 }
        has_status: true
        operations: [search, get, create, update]
        search:
          keyword_fields: [code, name, short_name]
          filters: [status]
          default_sort: [code]
        fields:
          name:              { type: string, label: { zh: 名称 }, required: true }
          short_name:        { type: string, maxLength: 40 }
          payment_term_days: { type: int, default: 30 }
          default_tax_rate:  { type: rate, values: ["0", "0.13"], default: "0.13" }
          kind:              { type: enum, values: { oem: { zh: 代工, en: OEM } } }
          valid_until:       { type: date, default: "2026-12-31" }
          contact_id:        { type: ref, ref: employee }
        """,
        True,
    ),
    (
        # An entity name must match ^[a-z][a-z0-9_]*$ (requirement 1.11).
        "entity",
        "entity: Supplier\nid_prefix: sup\nfields: {}\n",
        False,
    ),
    (
        # state_machine belongs to a bill, so it is an undefined key here.
        "entity",
        "entity: supplier\nid_prefix: sup\nstate_machine: standard\nfields: {}\n",
        False,
    ),
    (
        # A ref field must declare its target (requirement 1.12).
        "entity",
        "entity: supplier\nid_prefix: sup\nfields: { contact_id: { type: ref } }\n",
        False,
    ),
    (
        # A date default must be a date of the calendar; the schema says format date.
        "entity",
        "entity: supplier\nid_prefix: sup\nfields: { valid_until: { type: date, default: '2026-02-30' } }\n",
        False,
    ),
    # --- entities/*.yaml: a bill ---
    (
        "bill",
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
          delivery_date: { type: date, required: true }
        lines:
          material_id: { type: ref, ref: material, required: true }
          unit_id:     { type: ref, ref: unit, default_from: material.purchase_unit_id }
          tax_rate:    { default_from: material.tax_rate }
        line_quantity: { material_field: material_id, unit_field: unit_id }
        search:
          keyword_fields: [number]
          filters: [status, supplier_id, biz_date]
          default_sort: [biz_date desc, number desc]
        """,
        True,
    ),
    (
        # state_machine is required (requirement 1.9).
        "bill",
        """
        bill: purchase_order
        id_prefix: po
        number: { prefix: CGDD }
        header: {}
        lines: {}
        """,
        False,
    ),
    (
        # state_machine takes standard or simple only (requirement 1.9).
        "bill",
        """
        bill: purchase_order
        id_prefix: po
        number: { prefix: CGDD }
        state_machine: Standard
        header: {}
        lines: {}
        """,
        False,
    ),
    # --- conversions/*.yaml ---
    (
        "conversion",
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
            price: source_line.price
          required_line_inputs: [source_line_id, qty, warehouse_id]
        """,
        True,
    ),
    (
        # required_line_inputs must not be empty, and a header mapping takes source.<field>.
        "conversion",
        """
        conversion:
          id: purchase_order_to_receipt
          source: purchase_order
          target: purchase_receipt
          require_source_status: approved
          limit_by: qty
          header_mapping: { supplier_id: source_line.supplier_id }
          required_line_inputs: []
        """,
        False,
    ),
    # --- writebacks/*.yaml ---
    (
        "writeback",
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
        True,
    ),
    (
        # A writeback runs on approve, and its target is source.<field> or
        # source_line.<field>.
        "writeback",
        """
        writeback:
          id: receipt_approve_writeback_order
          conversion: purchase_order_to_receipt
          on: submit
          target: linked_qty
          op: add
          value: line.qty_base
        """,
        False,
    ),
    # --- linkages/*.yaml ---
    (
        "linkage",
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
        True,
    ),
    (
        # M0 implements no other linkage primitive (requirement 2.6), and a field path is
        # written header.<field> or line.<field>.
        "linkage",
        """
        linkage:
          id: ap_bill_balance
          bill: ap_bill
          primitive: partner_balance
          on: approve
          direction: in
          qty_field: qty_base
        """,
        False,
    ),
    # --- rules/*.yaml ---
    (
        "rule",
        """
        rule:
          id: R-PUR-001
          bill: purchase_order
          enforcement: enforced
          on: [create, update, submit]
          check: "header.delivery_date >= header.biz_date"
          error_code: VALIDATION_FAILED
          field: delivery_date
          message: { zh: 交货日期不得早于业务日期, en: Delivery date must not be earlier }
        """,
        True,
    ),
    (
        "rule",
        """
        rule:
          id: R-INV-002
          enforcement: described
          description: { zh: 库存调整单只用于盘点差异 }
          applies_to_tools: [stock_adjustment_create]
        """,
        True,
    ),
    (
        # enforcement selects the rule model, so a rule without it is neither.
        "rule",
        """
        rule:
          id: R-PUR-001
          bill: purchase_order
          on: [create]
          check: "count(lines) > 0"
          error_code: VALIDATION_FAILED
          message: { zh: 不满足 }
        """,
        False,
    ),
    (
        # A rule declares one of the four error codes a rule may decide (req 10.7).
        "rule",
        """
        rule:
          id: R-PUR-001
          bill: purchase_order
          enforcement: enforced
          on: [create]
          check: "count(lines) > 0"
          error_code: NOT_FOUND
          message: { zh: 不满足 }
        """,
        False,
    ),
    # --- tools/exposure.yaml ---
    (
        "exposure",
        """
        pack: purchase
        exclude: [purchase_receipt_delete]
        rename: { purchase_order_search: purchase_order_list }
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
              latest: [line.price, header.biz_date, header.number]
              history: { fields: [line.price, header.biz_date], limit: 5 }
          - { tool: inventory_balance_query, primitive: inventory, query: balance }
        """,
        True,
    ),
    (
        # A primitive query names its query; a ref parameter declares its target.
        "exposure",
        """
        pack: inventory
        queries:
          - { tool: inventory_balance_query, primitive: inventory }
        """,
        False,
    ),
    # --- tools/descriptions/<variant>.yaml ---
    (
        "description",
        """
        variant: zh-detailed
        tools:
          purchase_order_create:
            description: 新建采购订单草稿。
            params:
              supplier_id: 供应商 ID，由 supplier_search 返回
              lines: 订单分录，至少 1 行
              lines[].qty: 数量，必须大于 0
          purchase_order_get: {}
        """,
        True,
    ),
    (
        # A parameter path is a name, or lines[].<name> (requirement 3.2).
        "description",
        """
        variant: zh-detailed
        tools:
          purchase_order_create:
            params: { "lines[]": 订单分录 }
        """,
        False,
    ),
    # --- seed/generators.yaml ---
    (
        "seed-generators",
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
        True,
    ),
    (
        # A generator is bound to <entity>.<field>, and full_name is not a built-in one.
        "seed-generators",
        "generators: { name: { gen: full_name } }\n",
        False,
    ),
    (
        # A decimal is written as text, so a YAML float is not a value a generator takes.
        "seed-generators",
        "generators: { material.kind: { gen: choice, values: [0.5] } }\n",
        False,
    ),
    # --- seed/shared/*.yaml ---
    (
        "shared-data",
        """
        records:
          - { kind: org, code: ORG01, name: 示例集团, is_default: true }
          - kind: warehouse
            code: WH01
            name: 原料仓
            status: enabled
          - { kind: unit_conversion, material: M00001, from_unit: BOX, factor: "100" }
        """,
        True,
    ),
    (
        # A field name matches ^[a-z][a-z0-9_]*$, and a decimal is written as text.
        "shared-data",
        "records: [{ kind: org, Code: ORG01, factor: 0.13 }]\n",
        False,
    ),
    (
        # records is a list of records.
        "shared-data",
        "records: {}\n",
        False,
    ),
    # --- instance.yaml ---
    (
        "instance",
        """
        instance: p2p-demo
        version: 1.0.0
        title: { zh: 采购到付款示例 }
        packs:
          core: 1.0.0
          purchase: 1.0.0
          inventory: 1.0.0
        profiles: [semantic]
        default_profile: semantic
        datasets: [purchase-basic]
        default_dataset: purchase-basic
        current_user: emp_000001
        clock: { mode: fixed, anchor_date: "2026-10-09", timezone: Asia/Shanghai }
        """,
        True,
    ),
    (
        # default_profile is missing, and the clock mode is today or fixed (req 16.3).
        "instance",
        """
        instance: p2p-demo
        version: 1.0.0
        title: { zh: 采购到付款示例 }
        packs: { core: 1.0.0 }
        profiles: [semantic]
        datasets: [purchase-basic]
        default_dataset: purchase-basic
        clock: { mode: yesterday }
        """,
        False,
    ),
    (
        # A current user is an internal ID (requirement 20.16).
        "instance",
        """
        instance: p2p-demo
        version: 1.0.0
        title: { zh: 采购到付款示例 }
        packs: { core: 1.0.0 }
        profiles: [semantic]
        default_profile: semantic
        datasets: [purchase-basic]
        default_dataset: purchase-basic
        current_user: emp_1
        """,
        False,
    ),
    # --- profiles/*.yaml ---
    (
        "profile",
        """
        profile: semantic
        version: 1.0.0
        surface: semantic
        description_variant: zh-detailed
        packs: [core, purchase, inventory]
        include_tools: []
        exclude_tools: [purchase_receipt_delete]
        include_deprecated: false
        """,
        True,
    ),
    (
        # There are two surfaces, and include_deprecated is a boolean, not text.
        "profile",
        """
        profile: semantic
        version: 1.0.0
        surface: legacy
        description_variant: zh-detailed
        packs: [core]
        include_deprecated: 'false'
        """,
        False,
    ),
    # --- datasets/*.yaml ---
    (
        "dataset",
        """
        dataset: purchase-basic
        version: 1.0.0
        random_seed: 20261009
        include_shared: [core]
        current_user: emp_000001
        periods: { closed_before: "month(0)" }
        entities:
          supplier:main:
            kind: supplier
            name: { gen: company_name, industry: 机械 }
            short_name: { gen: short_name }
            status: enabled
          material:bolt_m8:
            kind: material
            name: M8 不锈钢螺栓
            material_kind: raw
            tax_rate: "0.13"
          purchase_order:history_last:
            kind: purchase_order
            supplier: "supplier:main"
            biz_date: month(-1).day(12)
            status: closed
            lines:
              - material: "material:bolt_m8"
                qty: "5000"
                price: { gen: decimal_range, min: "0.70", max: "0.95" }
        bulk:
          - { kind: supplier, count: 50 }
          - { kind: material, count: 200 }
        """,
        True,
    ),
    (
        # An alias is written <kind>:<name>, and a bulk count is at least 1.
        "dataset",
        """
        dataset: purchase-basic
        version: 1.0.0
        random_seed: 20261009
        entities: { "supplier-main": { kind: supplier, status: enabled } }
        bulk: [{ kind: supplier, count: 0 }]
        """,
        False,
    ),
    (
        # closed_before is a period expression, and random_seed is not negative.
        "dataset",
        """
        dataset: purchase-basic
        version: 1.0.0
        random_seed: -1
        periods: { closed_before: "month(x)" }
        """,
        False,
    ),
    (
        # A decimal is written as text, so a YAML float is not a field value (req 20.3).
        "dataset",
        """
        dataset: purchase-basic
        version: 1.0.0
        random_seed: 20261009
        entities: { "material:bolt_m8": { kind: material, tax_rate: 0.13 } }
        """,
        False,
    ),
)

PARAMS: Final = tuple(
    pytest.param(kind, document, valid, id=f"{index:02d}-{kind}-{'valid' if valid else 'invalid'}")
    for index, (kind, document, valid) in enumerate(SAMPLES)
)


@pytest.mark.req("1.1")
def test_samples_cover_every_kind_with_a_valid_and_an_invalid_document() -> None:
    for kind in ROOT_MODELS:
        verdicts = {valid for sample_kind, _, valid in SAMPLES if sample_kind == kind}
        assert verdicts == {True, False}, kind


@pytest.mark.req("1.1")
@pytest.mark.parametrize(("kind", "document", "valid"), PARAMS)
def test_jsonschema_and_the_model_agree(kind: str, document: str, valid: bool) -> None:
    data = parse_yaml(textwrap.dedent(document), FILE)
    errors = list(validator(kind).iter_errors(data))
    assert (not errors) is valid, [error.message for error in errors]
    assert model_accepts(kind, data) is valid
