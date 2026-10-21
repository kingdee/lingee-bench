"""erp_mock.loader.step5_expr: the rule expressions of the rules, writebacks and queries.

One example per kind of error the module reports, each on a tree written below ``tmp_path``
by tests.helpers.tree that holds exactly the one expression that is wrong, asserted by what
the loader reports: the places - the (pack, file, config path) of every location of the
error - and the reason, which names the rule number, the writeback id or the query tool
name, the position in the expression and the check that found it (requirement 10.2).

The trees run through :class:`~erp_mock.loader.loader.Loader`, the way loading runs step 5,
so steps 1 to 4 pass first and the names of the documents resolve (``loader.step5_model``).
:data:`BASE_FILES` is what every expression here is written against: the two context
entities, a partner entity with a status and a money field, and a pricing bill whose header
references that entity; :func:`rule_doc`, :func:`conversion_doc`, :func:`writeback_doc` and
:func:`exposure_doc` write the one document a test is about, so what it writes is the only
error that comes out.

The syntax trees are read off the model the step returns (``Loader.parsed``): the trees of
a hand-written tree and of the engine test fixture, which passes the step.
"""

import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import LINE_SUFFIX
from erp_mock.domain.rule_expr.parser import parse_expr
from erp_mock.domain.rule_expr.printer import print_expr
from erp_mock.loader.errors import ConfigError, ConfigErrors
from erp_mock.loader.loader import Loader
from erp_mock.loader.step5_expr import ParsedDomain
from tests.helpers.fixtures import load_fixture
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

# The objects every expression of this module is written against: the bill a rule applies
# to and a push reads, the bill that push produces, and the entity its header references.
ENTITY: Final = "supplier"
BILL: Final = "purchase_order"
TARGET_BILL: Final = "goods_receipt"
LINE_SOURCE: Final = f"{BILL}{LINE_SUFFIX}"

# The one rule, writeback and query tool a test writes, as an error names them.
RULE: Final = "R-CORE-001"
WRITEBACK: Final = "receipt_to_order"
CONVERSION: Final = "order_to_receipt"
QUERY_TOOL: Final = "core_price_query"

# The files of each document kind, as the places of an error report them.
RULE_FILE: Final = "rules/one.yaml"
CONVERSION_FILE: Final = f"conversions/{CONVERSION}.yaml"
WRITEBACK_FILE: Final = f"writebacks/{WRITEBACK}.yaml"
EXPOSURE_FILE: Final = "tools/exposure.yaml"

# The ID and number namespaces of the documents below, plus those of CONTEXT_FILES.
IDS: Final = "[sup, po, gr, orgu, emp]"
NUMBERS: Final = "[CGDD, SHRK]"


def doc(text: str) -> str:
    """A YAML document written indented in the test source."""
    return textwrap.dedent(text).lstrip("\n")


def quoted(text: str) -> str:
    """A rule expression as the double-quoted YAML scalar a document writes it as."""
    return f'"{text}"'


# --- the tree every expression is written against ----------------------------------------

# The partner entity: a status the engine keeps (requirement 5.7) and a money field, so an
# exists or sum_of over it has fields of both type classes.
PARTNER: Final = entity_doc(
    entity=ENTITY,
    id_prefix="sup",
    has_status="true",
    fields="{ name: { type: string, required: true }, credit: { type: money } }",
)

# The bill a rule applies to: pricing, so its header and lines carry the computed amounts
# and the lines the standard qty and price (requirement 6.1).
ORDER: Final = bill_doc(
    pricing="true",
    header=f"{{ {ENTITY}_id: {{ type: ref, ref: {ENTITY}, required: true }},"
    " note: { type: string } }",
    lines="{ memo: { type: string } }",
)

# The bill the push produces, from whose lines a writeback value is read.
RECEIPT: Final = bill_doc(
    bill=TARGET_BILL,
    id_prefix="gr",
    number="{ prefix: SHRK }",
    pricing="true",
    header=f"{{ {ENTITY}_id: {{ type: ref, ref: {ENTITY}, required: true }} }}",
    lines="{ memo: { type: string } }",
)

BASE_FILES: Final[Mapping[str, str]] = {
    **CONTEXT_FILES,
    f"entities/{ENTITY}.yaml": PARTNER,
    f"entities/{BILL}.yaml": ORDER,
    f"entities/{TARGET_BILL}.yaml": RECEIPT,
}

# The required keys of an enforced rule document (meta-model.md section 9).
RULE_KEYS: Final[Mapping[str, str]] = {
    "id": RULE,
    "bill": BILL,
    "enforcement": "enforced",
    "on": "[submit]",
    "error_code": "VALIDATION_FAILED",
    "message": "{ zh: 不满足本规则 }",
}

# The required keys of a conversion and of its writeback (meta-model.md section 7). The
# quota is counted by quantity, so the link field is ``linked_qty`` on the source lines.
CONVERSION_KEYS: Final[Mapping[str, str]] = {
    "id": CONVERSION,
    "source": BILL,
    "target": TARGET_BILL,
    "require_source_status": "approved",
    "limit_by": "qty",
    "required_line_inputs": "[source_line_id, qty, price]",
}
WRITEBACK_KEYS: Final[Mapping[str, str]] = {
    "id": WRITEBACK,
    "conversion": CONVERSION,
    "on": "approve",
    "target": "source_line.linked_qty",
    "op": "add",
}


def rule_doc(check: str, **overrides: str | None) -> str:
    """An enforced rule of the bill :data:`BILL` whose check is ``check``."""
    keys = {**RULE_KEYS, "check": quoted(check)}
    return "rule:\n" + textwrap.indent(document(keys, overrides), "  ")


def conversion_doc(**overrides: str | None) -> str:
    """The push from :data:`BILL` to :data:`TARGET_BILL`, with ``overrides`` applied."""
    return "conversion:\n" + textwrap.indent(document(CONVERSION_KEYS, overrides), "  ")


def writeback_doc(value: str, **overrides: str | None) -> str:
    """The writeback of that push, whose value is ``value``."""
    keys = {**WRITEBACK_KEYS, "value": quoted(value)}
    return "writeback:\n" + textwrap.indent(document(keys, overrides), "  ")


def exposure_doc(filter_text: str, source: str = LINE_SOURCE, latest: str = "header.number") -> str:
    """An exposure with one declarative query over ``source`` whose filter is ``filter_text``."""
    return doc(f"""
        pack: {PACK}
        queries:
          - tool: {QUERY_TOOL}
            source: {source}
            filter: {quoted(filter_text)}
            returns:
              latest: [{latest}]
        """)


def push_files(value: str, limit_by: str = "qty", target: str | None = None) -> Mapping[str, str]:
    """The conversion and the writeback of one push, as the files of a pack."""
    return {
        CONVERSION_FILE: conversion_doc(limit_by=limit_by),
        WRITEBACK_FILE: writeback_doc(value, target=target or WRITEBACK_KEYS["target"]),
    }


def write_core(tmp_path: Path, files: Mapping[str, str] | None = None) -> Loader:
    """An instance of the one pack ``core``, holding :data:`BASE_FILES` and ``files``."""
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={**BASE_FILES, **(files or {})},
        id_prefixes=IDS,
        number_prefixes=NUMBERS,
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


def parsed_of(target: Loader) -> ParsedDomain:
    """The model and the syntax trees step 5 left on ``target``."""
    parsed = target.parsed
    assert parsed is not None
    return parsed


# --- the check of an enforced rule (requirements 10.1, 10.5, 10.11) ----------------------

# Expressions a rule of :data:`BILL` may write: every path form, the functions, the
# operators and the literal forms of requirement 10.1, each of the right type.
SOUND_CHECKS: Final = [
    "header.status == 'draft' and count(lines) > 0",
    f"header.{ENTITY}.name != ''",
    f"exists({ENTITY}, status == 'enabled' and credit > header.amount)",
    f"sum_of({ENTITY}.credit, status == 'enabled') > 0",
    "context.user.name != '' and header.biz_date <= context.today",
    "line.qty > 0 and sum(lines.qty) <= 200",
    "header.remark in ['x', 'y'] or not header.price_includes_tax",
    "line.qty * line.price + 0.5 >= line.amount / 2",
]


@pytest.mark.req("10.2")
@pytest.mark.parametrize("check", SOUND_CHECKS)
def test_a_check_the_bill_resolves_passes(tmp_path: Path, check: str) -> None:
    files = {RULE_FILE: rule_doc(check)}

    # A rule is evaluated on the bill of its ``bill``: ``header.`` and ``line.`` are its
    # two levels, a ref field expands to the record it names, ``context.user`` is the user
    # context entity and an exists or sum_of reads any entity or bill (requirement 10.5).
    write_core(tmp_path, files).run_step(5)


@pytest.mark.req("10.2")
@pytest.mark.parametrize(
    ("check", "reason"),
    [
        pytest.param(
            "header.status in ('draft')",
            f"规则 {RULE} 的 check 第 18 位有语法错误：unexpected '(', expected '['",
            id="syntax-token",
        ),
        pytest.param(
            "header.status == 'draft' and",
            f"规则 {RULE} 的 check 第 29 位有语法错误：unexpected end of text",
            id="syntax-end",
        ),
        pytest.param(
            "header.supplier_name == 'x'",
            f"规则 {RULE} 的 check 第 1 位有名称错误：no field 'supplier_name' in header",
            id="names-header-field",
        ),
        pytest.param(
            "line.amount > line.cost",
            f"规则 {RULE} 的 check 第 15 位有名称错误：no field 'cost' in line",
            id="names-line-field",
        ),
        pytest.param(
            "header.note.name == 'x'",
            f"规则 {RULE} 的 check 第 1 位有名称错误："
            "field 'note' in header is not a ref field, it cannot be expanded",
            id="names-not-a-ref-field",
        ),
        pytest.param(
            f"header.{ENTITY}.missing == 'x'",
            f"规则 {RULE} 的 check 第 1 位有名称错误：no field 'missing' in {ENTITY}",
            id="names-expanded-field",
        ),
        pytest.param(
            "status == 'draft'",
            f"规则 {RULE} 的 check 第 1 位有名称错误："
            "an unprefixed field path stands only in an exists or sum_of condition",
            id="names-unprefixed",
        ),
        pytest.param(
            "exists(vendor, name == 'x')",
            f"规则 {RULE} 的 check 第 8 位有名称错误：no entity or bill named 'vendor'",
            id="names-target",
        ),
        pytest.param(
            "context.user.missing == 'x'",
            f"规则 {RULE} 的 check 第 1 位有名称错误：no field 'missing' in context.user",
            id="names-user-field",
        ),
        pytest.param(
            "header.biz_date > 0",
            f"规则 {RULE} 的 check 第 17 位有运算类型错误："
            "the operands of > must be of one type, got date and number",
            id="operators-comparison",
        ),
        pytest.param(
            "sum(lines.memo) > 0",
            f"规则 {RULE} 的 check 第 1 位有运算类型错误："
            "the field of sum must be number, got string",
            id="operators-sum-field",
        ),
        pytest.param(
            "count(lines)",
            f"规则 {RULE} 的 check 第 1 位有结果类型错误："
            "the expression must be boolean, got number",
            id="result-type",
        ),
    ],
)
def test_a_check_that_fails_one_of_the_four_checks(tmp_path: Path, check: str, reason: str) -> None:
    found = failures(write_core(tmp_path, {RULE_FILE: rule_doc(check)}))

    # One error per expression, at the item that wrote it, naming the rule number, the
    # position as a 1-based code point index and the check that found it (req 10.2).
    assert places(found) == [(PACK, RULE_FILE, "rule.check")]
    assert reasons(found) == [reason]


@pytest.mark.req("10.2")
@pytest.mark.parametrize(
    ("check", "reason"),
    [
        pytest.param(
            "line.unknown == 'x' and header.biz_date > 0",
            f"规则 {RULE} 的 check 第 1 位有名称错误：no field 'unknown' in line",
            id="names-before-operators",
        ),
        pytest.param(
            "exists(vendor, name == 'x') and header.unknown == 1",
            f"规则 {RULE} 的 check 第 8 位有名称错误：no entity or bill named 'vendor'",
            id="smallest-position",
        ),
        pytest.param(
            "sum(lines.memo) > header.biz_date",
            f"规则 {RULE} 的 check 第 1 位有运算类型错误："
            "the field of sum must be number, got string",
            id="operators-before-result",
        ),
    ],
)
def test_an_expression_reports_one_error_of_the_first_failing_check(
    tmp_path: Path, check: str, reason: str
) -> None:
    found = failures(write_core(tmp_path, {RULE_FILE: rule_doc(check)}))

    # The four checks run in order and only the first one that finds anything reports,
    # with the smallest position of that check (requirement 10.2).
    assert reasons(found) == [reason]


@pytest.mark.req("10.2")
def test_a_rule_whose_bill_does_not_resolve_is_not_checked(tmp_path: Path) -> None:
    files = {RULE_FILE: rule_doc("header.missing == 'x'", bill="other_bill")}
    found = failures(write_core(tmp_path, files))

    # There is no bill to resolve the names against; the reference itself is the one error,
    # reported by ``loader.step5_model`` (requirement 2.9).
    assert places(found) == [(PACK, RULE_FILE, "rule.bill")]
    assert reasons(found) == ["引用的单据 other_bill 不存在"]


@pytest.mark.req("10.2")
@pytest.mark.parametrize(
    "check",
    [
        "context.user.missing == 'x'",
        "not (context.user.missing in ['x'])",
        f"exists({ENTITY}, credit > 0 and context.user.missing == 'x')",
        f"sum_of({ENTITY}.credit, context.user.missing == 'x') > 0",
    ],
)
def test_a_check_that_reads_an_unknown_current_user_is_not_checked(
    tmp_path: Path, check: str
) -> None:
    write_instance(tmp_path / INSTANCE_DIR, DEFINITIONS)
    files = {
        "entities/org_unit.yaml": CONTEXT_FILES["entities/org_unit.yaml"],
        f"entities/{ENTITY}.yaml": PARTNER,
        f"entities/{BILL}.yaml": ORDER,
        RULE_FILE: rule_doc(check),
    }
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files=files,
        id_prefixes="[sup, po, orgu]",
        number_prefixes=NUMBERS,
    )
    found = failures(loaded(tmp_path))

    # Without exactly one user context entity there is no record to resolve
    # ``context.user`` against, wherever the path stands - in a condition included; the
    # missing entity is the one error, reported by ``loader.step5_namespace`` (req 1.14).
    assert places(found) == [(None, "instance.yaml", "packs")]
    assert reasons(found) == ["引用的业务包都没有声明用户上下文实体"]


# --- the value of a writeback (requirement 8.8) ------------------------------------------


@pytest.mark.req("10.2")
@pytest.mark.parametrize(
    ("value", "limit_by", "target"),
    [
        pytest.param("line.qty", "qty", "source_line.linked_qty", id="line-of-the-target-bill"),
        pytest.param(
            "line.qty * line.price", "qty", "source_line.linked_qty", id="arithmetic-per-line"
        ),
        pytest.param("sum(lines.qty)", "amount", "source.linked_amount", id="lines-summed-once"),
        pytest.param(
            "header.amount_with_tax", "amount", "source.linked_amount", id="header-of-the-target"
        ),
    ],
)
def test_a_writeback_value_read_from_the_pushed_bill_passes(
    tmp_path: Path, value: str, limit_by: str, target: str
) -> None:
    # The value is evaluated on the bill the push produces: per target line for a
    # ``source_line.<field>`` target and once for a ``source.<field>`` one (req 8.8).
    write_core(tmp_path, push_files(value, limit_by, target)).run_step(5)


@pytest.mark.req("10.2")
@pytest.mark.parametrize(
    ("value", "limit_by", "target", "reason"),
    [
        pytest.param(
            "line.qty",
            "amount",
            "source.linked_amount",
            f"反写 {WRITEBACK} 的 value 第 1 位有名称错误："
            "line paths are not available here, the evaluated records are not bill lines",
            id="no-current-line",
        ),
        pytest.param(
            "line.missing",
            "qty",
            "source_line.linked_qty",
            f"反写 {WRITEBACK} 的 value 第 1 位有名称错误：no field 'missing' in line",
            id="names",
        ),
        pytest.param(
            "header.status == 'approved'",
            "qty",
            "source_line.linked_qty",
            f"反写 {WRITEBACK} 的 value 第 1 位有结果类型错误："
            "the expression must be number, got boolean",
            id="result-type",
        ),
    ],
)
def test_a_writeback_value_that_fails_one_of_the_checks(
    tmp_path: Path, value: str, limit_by: str, target: str, reason: str
) -> None:
    found = failures(write_core(tmp_path, push_files(value, limit_by, target)))

    # The writeback adds the value to a qty or money field of the source, so the value is
    # a number; the error names the writeback id (requirements 8.8 and 10.2).
    assert places(found) == [(PACK, WRITEBACK_FILE, "writeback.value")]
    assert reasons(found) == [reason]


# --- the filter of a declarative query (requirement 10.10) -------------------------------


@pytest.mark.req("10.2")
@pytest.mark.parametrize(
    ("filter_text", "source", "latest"),
    [
        pytest.param("line.qty > 0", LINE_SOURCE, "header.number", id="line-of-the-source-lines"),
        pytest.param(
            "header.status in ['approved', 'closed']",
            LINE_SOURCE,
            "line.price",
            id="header-of-the-lines-bill",
        ),
        pytest.param("count(lines) > 0", BILL, "header.number", id="lines-of-a-bill-header"),
        pytest.param(
            f"header.credit > 0 or exists({BILL}, status == 'draft')",
            ENTITY,
            "header.name",
            id="entity-record",
        ),
    ],
)
def test_a_filter_of_a_source_that_resolves_passes(
    tmp_path: Path, filter_text: str, source: str, latest: str
) -> None:
    files = {EXPOSURE_FILE: exposure_doc(filter_text, source, latest)}

    # ``line.`` is the source line of a ``<bill>_line`` source, ``header.`` the record a
    # bill or an entity source names, and the lines of a bill are summed whichever of its
    # rows the query reads (requirement 10.10).
    write_core(tmp_path, files).run_step(5)


@pytest.mark.req("10.2")
@pytest.mark.parametrize(
    ("filter_text", "source", "latest", "reason"),
    [
        pytest.param(
            "line.qty > 0",
            BILL,
            "header.number",
            f"查询工具 {QUERY_TOOL} 的 filter 第 1 位有名称错误："
            "line paths are not available here, the evaluated records are not bill lines",
            id="line-on-a-bill-header",
        ),
        pytest.param(
            "line.credit > 0",
            ENTITY,
            "header.name",
            f"查询工具 {QUERY_TOOL} 的 filter 第 1 位有名称错误："
            "line paths are not available here, the evaluated records are not bill lines",
            id="line-on-an-entity",
        ),
        pytest.param(
            "count(lines) > 0",
            ENTITY,
            "header.name",
            f"查询工具 {QUERY_TOOL} 的 filter 第 7 位有名称错误："
            "lines are not available here, the evaluated records belong to no bill",
            id="lines-on-an-entity",
        ),
        pytest.param(
            "header.credit",
            ENTITY,
            "header.name",
            f"查询工具 {QUERY_TOOL} 的 filter 第 1 位有结果类型错误："
            "the expression must be boolean, got number",
            id="result-type",
        ),
    ],
)
def test_a_filter_that_fails_one_of_the_checks(
    tmp_path: Path, filter_text: str, source: str, latest: str, reason: str
) -> None:
    files = {EXPOSURE_FILE: exposure_doc(filter_text, source, latest)}
    found = failures(write_core(tmp_path, files))

    # A filter out of a bill's lines has no current line, and the records of an entity
    # belong to no bill at all; the error names the query tool (requirements 10.2, 10.10).
    assert places(found) == [(PACK, EXPOSURE_FILE, "queries[0].filter")]
    assert reasons(found) == [reason]


@pytest.mark.req("10.2")
def test_a_filter_is_located_by_its_position_in_the_exposure(tmp_path: Path) -> None:
    exposure = doc(f"""
        pack: {PACK}
        queries:
          - {{ tool: core_stock_query, primitive: inventory, query: balance }}
          - tool: {QUERY_TOOL}
            source: {LINE_SOURCE}
            filter: {quoted("line.memo > 0")}
            returns:
              latest: [header.number]
        """)
    found = failures(write_core(tmp_path, {EXPOSURE_FILE: exposure}))

    # The config path is the position of the query in ``queries``, whatever the kind of
    # the queries before it (requirement 2.2).
    assert places(found) == [(PACK, EXPOSURE_FILE, "queries[1].filter")]
    assert reasons(found) == [
        f"查询工具 {QUERY_TOOL} 的 filter 第 11 位有运算类型错误："
        "the operands of > must be of one type, got string and number"
    ]


# --- the syntax trees the step leaves behind ---------------------------------------------


@pytest.mark.req("10.2")
def test_the_syntax_tree_of_every_expression_is_kept_in_the_model(tmp_path: Path) -> None:
    check = "count(lines) > 0"
    value = "line.qty"
    filter_text = "header.status == 'approved'"
    files = {
        RULE_FILE: rule_doc(f"(  {check}  )"),
        **push_files(value),
        EXPOSURE_FILE: exposure_doc(filter_text),
    }
    target = write_core(tmp_path, files)
    target.run_step(5)

    parsed = parsed_of(target)
    # Keyed by the rule number, the writeback id and the query tool name; the tree is what
    # the parser read, so whitespace and redundant parentheses are gone (requirement 10.3).
    assert dict(parsed.rules) == {RULE: parse_expr(check)}
    assert dict(parsed.writebacks) == {WRITEBACK: parse_expr(value)}
    assert dict(parsed.queries) == {QUERY_TOOL: parse_expr(filter_text)}
    assert print_expr(parsed.rules[RULE]) == check
    # The model itself is the one the earlier parts of step 5 built.
    assert parsed.domain is target.domain
    assert set(parsed.domain.bills) == {BILL, TARGET_BILL}


@pytest.mark.req("10.2")
def test_a_described_rule_declares_no_check_and_has_no_tree(tmp_path: Path) -> None:
    described = doc("""
        rule:
          id: R-CORE-002
          enforcement: described
          description: { zh: 收料必须通过收料单 }
          applies_to_tools: [core_order_create]
        """)
    files = {RULE_FILE: rule_doc("count(lines) > 0"), "rules/two.yaml": described}
    target = write_core(tmp_path, files)
    target.run_step(5)

    # A described rule is never evaluated, so there is nothing to check or to keep
    # (requirement 10.9).
    assert set(parsed_of(target).rules) == {RULE}


@pytest.mark.req("10.2", "25.2")
def test_the_fixture_passes_step_5_with_every_expression_parsed() -> None:
    parsed = parsed_of(load_fixture(5))

    # The fixture's enforced rules, both writebacks and its declarative query, as the
    # canonical text of the trees the step kept (tests/fixtures/packs/fx_trade). The
    # described rule declares no check and is absent (requirement 10.9).
    assert {name: print_expr(tree) for name, tree in parsed.rules.items()} == {
        "R-FXT-001": "header.from_depot_id != header.to_depot_id",
        "R-FXT-004": "sum(lines.entry_qty) <= 2000",
        "R-FXT-003": "line.entry_qty <= 1000",
    }
    assert {name: print_expr(tree) for name, tree in parsed.writebacks.items()} == {
        "entry_writeback_order": "line.qty_base",
        "pay_writeback_fee": "header.amount_with_tax",
    }
    assert {name: print_expr(tree) for name, tree in parsed.queries.items()} == {
        "supply_price_query": "header.status in ['approved', 'closed']"
    }
