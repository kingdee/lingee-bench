"""erp_mock.tools.generator and loading step 6: the tool list, the order and the names.

Two halves, both about names alone (the schemas arrive with tasks 9.2 and 9.3):

- the generated list: the engine test fixture is loaded through step 6 and every tool it
  exposes is asserted, in the output order of requirement 11.9 - the entities and bills of
  a pack by the path of the declaring file and, per object, in the order of its
  ``operations``, then the push tools by file path, then the query tools in declaration
  order, with ``business_context_get`` before the first pack. The fixture declares
  ``exclude``, ``rename`` and ``source_short_names``, so what each of them does to a name
  is read off it (requirements 11.1 and 11.13);
- one example per kind of error step 6 reports, each on a tree written below ``tmp_path``
  by tests.helpers.tree that holds exactly the one thing that is wrong, asserted by what
  the loader reports: the places - the (pack, file, config path) of every location of the
  error - and the reason. The trees run through :class:`~erp_mock.loader.loader.Loader`,
  the way loading runs step 6, so steps 1 to 5 pass first and the names of the documents
  resolve.

:data:`BASE_FILES` is the pack every tree is written against: the two context entities, an
entity with two operations, two pricing bills and the push from one to the other, so that
an exposure can exclude, rename and declare a query over objects that exist.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import LINE_SUFFIX, DomainModel
from erp_mock.config.pack import ExposureDef
from erp_mock.loader.errors import ConfigError, ConfigErrors
from erp_mock.loader.loader import Loader
from erp_mock.loader.step6 import GeneratedTools
from erp_mock.tools.generator import (
    BUILTIN_TOOLS,
    BUSINESS_CONTEXT_GET,
    NAME_PATTERN,
    generate_pack_tools,
    generated_tool_names,
    tool_action,
)
from tests.helpers.fixtures import BASE_PACK, TRADE_PACK, load_fixture
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

# The pack the default instance.yaml of tests.helpers.tree references, and the files an
# error of this module is reported at.
PACK = "core"
PACK_FILE = "pack.yaml"
EXPOSURE_FILE = "tools/exposure.yaml"
PROFILE_FILE = "profiles/semantic.yaml"

# One place of a reported error: the pack, the file and the config path.
type Place = tuple[str | None, str, str | None]


# --- the tools of the engine test fixture ------------------------------------------------


def names(bill: str, actions: str) -> tuple[str, ...]:
    """The tool names of ``bill`` for the space-separated ``actions``, in that order."""
    return tuple(f"{bill}_{action}" for action in actions.split())


# What fx_base exposes: two operations of the entity of entities/article.yaml, then those
# of depot.yaml (whose get tool the exposure excludes), measure.yaml and vendor.yaml
# (whose get tool it renames); branch, measure_link and operator declare no operation.
BASE_TOOLS: Final[tuple[str, ...]] = (
    *names("article", "search get"),
    "depot_search",
    "measure_search",
    "vendor_search",
    "vendor_detail_get",
)

# What fx_trade exposes: its bills by the path of the declaring file, then the two push
# tools by the path of the conversion document, then the queries in declaration order.
TRADE_TOOLS: Final[tuple[str, ...]] = (
    # entities/count_sheet.yaml, whose unapprove tool the exposure excludes.
    *names("count_sheet", "search get create update delete submit unsubmit approve"),
    # entities/fee_note.yaml: a simple bill, which declares no submit, unsubmit or close.
    *names("fee_note", "search get create update delete approve unapprove"),
    *names("goods_entry", "search get create update delete submit unsubmit approve unapprove"),
    *names("goods_entry", "close unclose"),
    *names("issue_note", "search get create update delete submit unsubmit approve unapprove"),
    *names("pay_slip", "search get create update delete submit unsubmit approve unapprove"),
    # entities/stock_move.yaml, whose create tool the exposure renames.
    "stock_move_search",
    "stock_move_get",
    "stock_move_draft_create",
    *names("stock_move", "update delete submit unsubmit approve unapprove"),
    *names("supply_order", "search get create update delete submit unsubmit approve unapprove"),
    *names("supply_order", "close unclose"),
    # conversions/fee_note_to_pay_slip.yaml, then conversions/supply_order_to_entry.yaml.
    "pay_slip_create_from_fee",
    "goods_entry_create_from_order",
    # tools/exposure.yaml: the declarative query, then the inventory balance query.
    "supply_price_query",
    "stock_level_query",
)


def tools_of(loader: Loader) -> GeneratedTools:
    """The tools step 6 left on ``loader``."""
    tools = loader.tools
    assert tools is not None
    return tools


def domain_of(loader: Loader) -> DomainModel:
    """The domain model step 5 left on ``loader``."""
    domain = loader.domain
    assert domain is not None
    return domain


def exposure_of(loader: Loader, pack: str) -> ExposureDef:
    """The validated ``tools/exposure.yaml`` of the pack ``pack``."""
    exposure = next(raw.exposure for raw in loader.raw_packs if raw.name == pack)
    assert exposure is not None
    return exposure.document


@pytest.mark.req("11.1", "11.9")
def test_the_fixture_exposes_every_generated_tool_in_output_order() -> None:
    tools = tools_of(load_fixture(6))

    assert tuple(spec.name for spec in tools.packs[BASE_PACK]) == BASE_TOOLS
    assert tuple(spec.name for spec in tools.packs[TRADE_PACK]) == TRADE_TOOLS
    # business_context_get comes first, the packs follow in the order of instance.yaml,
    # which is the order of the profile of the fixture (requirement 11.9).
    assert tuple(spec.name for spec in tools.tools) == (
        BUSINESS_CONTEXT_GET,
        *BASE_TOOLS,
        *TRADE_TOOLS,
    )
    # A profile over one pack takes the built-in tools and that pack's, in its own order.
    assert tools.of([TRADE_PACK, BASE_PACK]) == (
        *BUILTIN_TOOLS,
        *tools.packs[TRADE_PACK],
        *tools.packs[BASE_PACK],
    )


@pytest.mark.req("11.1")
def test_the_builtin_tool_belongs_to_no_pack() -> None:
    (builtin,) = BUILTIN_TOOLS

    assert (builtin.name, builtin.generated_name) == (BUSINESS_CONTEXT_GET, BUSINESS_CONTEXT_GET)
    assert (builtin.pack, builtin.kind, builtin.action) == (None, "builtin", "get")
    assert builtin.renamed is False


@pytest.mark.req("11.1")
def test_exclude_and_rename_decide_which_names_a_pack_exposes() -> None:
    loader = load_fixture(6)
    domain = domain_of(loader)
    exposure = exposure_of(loader, BASE_PACK)

    # The generated names, before exclude and rename: what the two may name (req 11.14).
    assert generated_tool_names(domain, BASE_PACK, exposure) == (
        *names("article", "search get"),
        *names("depot", "search get"),
        "measure_search",
        *names("vendor", "search get"),
    )
    # A pack without an exposure exposes exactly the generated names.
    without = generate_pack_tools(domain, BASE_PACK)
    assert tuple(spec.name for spec in without) == generated_tool_names(domain, BASE_PACK)
    # A renamed tool keeps the generated name and the action of its operation (req 11.2).
    renamed = [spec for spec in generate_pack_tools(domain, BASE_PACK, exposure) if spec.renamed]
    assert [(spec.name, spec.generated_name, spec.action) for spec in renamed] == [
        ("vendor_detail_get", "vendor_get", "get")
    ]


@pytest.mark.req("11.1", "11.13")
def test_a_push_tool_is_named_after_the_target_bill_and_the_source_short_name() -> None:
    loader = load_fixture(6)
    domain = domain_of(loader)

    pushes = [
        spec
        for spec in generate_pack_tools(domain, TRADE_PACK, exposure_of(loader, TRADE_PACK))
        if spec.kind == "push"
    ]
    assert [(spec.name, spec.action, spec.target) for spec in pushes] == [
        ("pay_slip_create_from_fee", "create_from", "fee_note_to_pay_slip"),
        ("goods_entry_create_from_order", "create_from", "supply_order_to_entry"),
    ]
    # Without source_short_names the short name is the source bill's own name (req 11.13).
    declared = generate_pack_tools(domain, TRADE_PACK)
    assert [spec.name for spec in declared if spec.kind == "push"] == [
        "pay_slip_create_from_fee_note",
        "goods_entry_create_from_supply_order",
    ]


@pytest.mark.req("11.1")
def test_a_query_tool_takes_the_name_the_exposure_declares() -> None:
    loader = load_fixture(6)

    queries = [spec for spec in tools_of(loader).packs[TRADE_PACK] if spec.kind == "query"]
    assert [(spec.name, spec.action, spec.target) for spec in queries] == [
        ("supply_price_query", "query", "supply_price_query"),
        ("stock_level_query", "query", "stock_level_query"),
    ]


@pytest.mark.req("11.2")
@pytest.mark.parametrize(
    ("name", "action"),
    [
        ("vendor_get", "get"),
        ("vendor_forget", ""),
        ("supply_price_query", "query"),
        ("count_sheet_unapprove", "unapprove"),
        ("ap_write_off", "write_off"),
        ("goods_entry_create_from_order", ""),
    ],
)
def test_the_action_of_a_tool_name_is_its_last_underscore_separated_part(
    name: str, action: str
) -> None:
    assert tool_action(name) == action


# --- the pack every tree of the error cases is written against ---------------------------

# The objects of the tree: an entity with two operations, the bill a push reads, the bill
# it produces, and the one query tool the exposure declares.
ENTITY: Final = "supplier"
BILL: Final = "purchase_order"
TARGET_BILL: Final = "goods_receipt"
LINE_SOURCE: Final = f"{BILL}{LINE_SUFFIX}"
CONVERSION: Final = "order_to_receipt"
QUERY_TOOL: Final = "purchase_price_query"

# The short name of the source bill, which names the push tool (requirement 11.13).
SHORT_NAME: Final = "order"
PUSH_TOOL: Final = f"{TARGET_BILL}_create_from_{SHORT_NAME}"

# The namespaces of the documents below, plus those of CONTEXT_FILES; every generated name
# starts with one of the tool prefixes.
IDS: Final = "[sup, po, gr, orgu, emp]"
NUMBERS: Final = "[CGDD, SHRK]"
TOOL_PREFIXES: Final = "[supplier_, purchase_, goods_]"

BASE_FILES: Final[Mapping[str, str]] = {
    **CONTEXT_FILES,
    f"entities/{ENTITY}.yaml": entity_doc(operations="[search, get]"),
    f"entities/{BILL}.yaml": bill_doc(
        pricing="true",
        header=f"{{ {ENTITY}_id: {{ type: ref, ref: {ENTITY}, required: true }} }}",
        lines="{ memo: { type: string } }",
        operations="[create, submit, approve]",
    ),
    f"entities/{TARGET_BILL}.yaml": bill_doc(
        bill=TARGET_BILL,
        id_prefix="gr",
        number="{ prefix: SHRK }",
        pricing="true",
        header=f"{{ {ENTITY}_id: {{ type: ref, ref: {ENTITY}, required: true }} }}",
        lines="{ memo: { type: string } }",
        operations="[create]",
    ),
    f"conversions/{CONVERSION}.yaml": (
        "conversion:\n"
        f"  id: {CONVERSION}\n"
        f"  source: {BILL}\n"
        f"  target: {TARGET_BILL}\n"
        "  require_source_status: approved\n"
        "  limit_by: qty\n"
        "  required_line_inputs: [source_line_id, qty, price]\n"
    ),
}

# What the tree generates with the default exposure, in the output order of requirement
# 11.9: the three objects by file path, the push tool, then the query tool.
TREE_TOOLS: Final[tuple[str, ...]] = (
    f"{TARGET_BILL}_create",
    *names(BILL, "create submit approve"),
    *names(ENTITY, "search get"),
    PUSH_TOOL,
    QUERY_TOOL,
)


def query_block(*tools: str) -> str:
    """The ``queries`` of an exposure: one declarative query per tool name given."""
    items = "".join(
        f"  - tool: {tool}\n    source: {LINE_SOURCE}\n    returns: {{ latest: [line.price] }}\n"
        for tool in tools
    )
    return f"queries:\n{items}" if items else ""


def exposure_doc(*queries: str, **overrides: str | None) -> str:
    """The exposure of the pack: the short name of the source bill, and ``queries``."""
    keys = {"pack": PACK, "source_short_names": f"{{ {BILL}: {SHORT_NAME} }}"}
    return document(keys, overrides) + query_block(*queries)


def write_core(
    tmp_path: Path,
    exposure: str | None = None,
    profile: str | None = None,
    *,
    tool_prefixes: str = TOOL_PREFIXES,
) -> Loader:
    """An instance of the one pack ``core``, holding :data:`BASE_FILES` and ``exposure``.

    ``exposure`` and ``profile`` default to documents that pass the step, so a test writes
    the one document it is about.
    """
    write_instance(
        tmp_path / INSTANCE_DIR,
        {
            PROFILE_FILE: profile if profile is not None else profile_doc(),
            "datasets/purchase-basic.yaml": dataset_doc(),
        },
    )
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files={
            **BASE_FILES,
            EXPOSURE_FILE: exposure if exposure is not None else exposure_doc(QUERY_TOOL),
        },
        id_prefixes=IDS,
        number_prefixes=NUMBERS,
        tool_prefixes=tool_prefixes,
    )
    return loaded(tmp_path)


def loaded(tmp_path: Path) -> Loader:
    """A loader for the tree below ``tmp_path`` with steps 1 to 5 run; they must pass."""
    target = Loader(tmp_path / INSTANCE_DIR, tmp_path / PACKS_DIR)
    for step in (1, 2, 3, 4, 5):
        target.run_step(step)
    return target


def failures(target: Loader) -> tuple[ConfigError, ...]:
    """The errors step 6 reports for ``target``, in report order; it must find some."""
    with pytest.raises(ConfigErrors) as caught:
        target.run_step(6)
    assert caught.value.step == 6
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


def exposed(target: Loader) -> tuple[str, ...]:
    """The names of the tools the pack of the tree exposes, in output order."""
    return tuple(spec.name for spec in tools_of(target).packs[PACK])


@pytest.mark.req("11.1", "11.9")
def test_the_tree_generates_one_tool_per_operation_conversion_and_query(tmp_path: Path) -> None:
    target = write_core(tmp_path)
    target.run_step(6)

    assert exposed(target) == TREE_TOOLS
    # The push tool and the query tool carry what they were generated from (req 11.1).
    found = {spec.name: spec for spec in tools_of(target).packs[PACK]}
    assert (found[PUSH_TOOL].kind, found[PUSH_TOOL].target) == ("push", CONVERSION)
    assert (found[QUERY_TOOL].kind, found[QUERY_TOOL].target) == ("query", QUERY_TOOL)


# --- the form of a tool name (requirement 11.2) ------------------------------------------

# A rename that makes the name 65 code points long, one more than a tool name may be.
LONG_NAME: Final = f"{ENTITY}_{'a' * (65 - len(ENTITY) - len('__get'))}_get"


@pytest.mark.req("11.2")
def test_a_name_longer_than_64_code_points(tmp_path: Path) -> None:
    assert len(LONG_NAME) == 65
    found = failures(
        write_core(tmp_path, exposure_doc(QUERY_TOOL, rename=f"{{ {ENTITY}_get: {LONG_NAME} }}"))
    )

    assert places(found) == [(PACK, EXPOSURE_FILE, f"rename.{ENTITY}_get")]
    assert reasons(found) == [f"工具名 {LONG_NAME} 不匹配 {NAME_PATTERN}"]


@pytest.mark.req("11.2", "11.13")
def test_a_renamed_push_tool_no_longer_names_the_source_short_name(tmp_path: Path) -> None:
    renamed = f"{TARGET_BILL}_create_from_po"
    found = failures(
        write_core(tmp_path, exposure_doc(QUERY_TOOL, rename=f"{{ {PUSH_TOOL}: {renamed} }}"))
    )

    # The short name names the tool and its ``<short name>_id`` parameter, so it is
    # source_short_names that decides the name of a push tool, not rename.
    assert places(found) == [(PACK, EXPOSURE_FILE, f"rename.{PUSH_TOOL}")]
    assert reasons(found) == [
        f"下推工具名 {renamed} 不符合 <目标单据>_create_from_<来源简称> 形式，应为 {PUSH_TOOL}"
    ]


@pytest.mark.req("11.2")
def test_a_query_tool_whose_name_ends_with_no_action(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, exposure_doc("purchase_price_lookup")))

    assert places(found) == [(PACK, EXPOSURE_FILE, "queries[0].tool")]
    assert reasons(found) == ["工具名 purchase_price_lookup 不以动作词表中的动作结尾"]


@pytest.mark.req("11.2")
def test_a_rename_that_changes_the_action(tmp_path: Path) -> None:
    renamed = f"{ENTITY}_detail_search"
    found = failures(
        write_core(tmp_path, exposure_doc(QUERY_TOOL, rename=f"{{ {ENTITY}_get: {renamed} }}"))
    )

    assert places(found) == [(PACK, EXPOSURE_FILE, f"rename.{ENTITY}_get")]
    assert reasons(found) == [f"rename 把 {ENTITY}_get 改为 {renamed}，动作从 get 变为 search"]


@pytest.mark.req("11.2")
def test_a_name_that_starts_with_no_tool_prefix_of_its_pack(tmp_path: Path) -> None:
    found = failures(
        write_core(tmp_path, exposure_doc(QUERY_TOOL, rename=f"{{ {ENTITY}_get: vendor_get }}"))
    )

    # The error locates the name and the manifest list that would have to state its prefix.
    assert places(found) == [
        (PACK, EXPOSURE_FILE, f"rename.{ENTITY}_get"),
        (PACK, PACK_FILE, "tool_prefixes"),
    ]
    assert reasons(found) == [
        f"工具名 vendor_get 不以业务包 {PACK} 的 manifest 的 tool_prefixes 之一开头"
    ]


# --- one name per tool (requirement 2.7) -------------------------------------------------


@pytest.mark.req("2.7")
def test_a_rename_onto_the_name_of_another_tool_of_the_pack(tmp_path: Path) -> None:
    # The create tool of one bill renamed to that of the other: the action stays create,
    # so the only error is the name the two tools would then share.
    renamed = f"{{ {BILL}_create: {TARGET_BILL}_create }}"
    found = failures(write_core(tmp_path, exposure_doc(QUERY_TOOL, rename=renamed)))

    # One error for the whole conflict, locating both tools and naming both of them.
    assert places(found) == [
        (PACK, f"entities/{TARGET_BILL}.yaml", "operations"),
        (PACK, EXPOSURE_FILE, f"rename.{BILL}_create"),
    ]
    assert reasons(found) == [
        f"工具名 {TARGET_BILL}_create 重复："
        f"业务包 {PACK} 的单据 {TARGET_BILL} 的 create 工具、"
        f"业务包 {PACK} 的单据 {BILL} 的 create 工具"
    ]


@pytest.mark.req("2.7")
def test_a_pack_tool_named_like_the_builtin_tool(tmp_path: Path) -> None:
    found = failures(
        write_core(
            tmp_path,
            exposure_doc(QUERY_TOOL, rename=f"{{ {ENTITY}_get: {BUSINESS_CONTEXT_GET} }}"),
            tool_prefixes="[supplier_, purchase_, goods_, business_]",
        )
    )

    # The built-in tool is written in no file, so the error locates the pack's tool alone
    # and names the engine in its reason.
    assert places(found) == [(PACK, EXPOSURE_FILE, f"rename.{ENTITY}_get")]
    assert reasons(found) == [
        f"工具名 {BUSINESS_CONTEXT_GET} 重复：引擎内置工具、"
        f"业务包 {PACK} 的实体 {ENTITY} 的 get 工具"
    ]


@pytest.mark.req("2.7")
def test_two_queries_that_declare_the_same_tool_name(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, exposure_doc(QUERY_TOOL, QUERY_TOOL)))

    assert places(found) == [
        (PACK, EXPOSURE_FILE, "queries[0].tool"),
        (PACK, EXPOSURE_FILE, "queries[1].tool"),
    ]
    assert reasons(found) == [
        f"查询工具名 {QUERY_TOOL} 被声明多次：业务包 {PACK} 的 queries[0]、业务包 {PACK} 的 queries[1]"
    ]


# --- exclude and rename (requirement 11.14) ----------------------------------------------


@pytest.mark.req("11.14")
def test_exclude_names_a_tool_the_pack_does_not_generate(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, exposure_doc(QUERY_TOOL, exclude=f"[{ENTITY}_delete]")))

    # The entity declares search and get alone, so no delete tool is generated.
    assert places(found) == [(PACK, EXPOSURE_FILE, "exclude[0]")]
    assert reasons(found) == [f"exclude 中的 {ENTITY}_delete 不是业务包 {PACK} 生成的工具名"]


@pytest.mark.req("11.14")
def test_rename_names_a_tool_the_pack_does_not_generate(tmp_path: Path) -> None:
    found = failures(
        write_core(
            tmp_path,
            exposure_doc(QUERY_TOOL, rename=f"{{ {ENTITY}_delete: {ENTITY}_drop_delete }}"),
        )
    )

    assert places(found) == [(PACK, EXPOSURE_FILE, f"rename.{ENTITY}_delete")]
    assert reasons(found) == [f"rename 中的 {ENTITY}_delete 不是业务包 {PACK} 生成的工具名"]


@pytest.mark.req("11.14")
def test_a_tool_in_both_exclude_and_rename(tmp_path: Path) -> None:
    target = write_core(
        tmp_path,
        exposure_doc(
            QUERY_TOOL,
            exclude=f"[{ENTITY}_get]",
            rename=f"{{ {ENTITY}_get: {ENTITY}_detail_get }}",
        ),
    )
    found = failures(target)

    assert places(found) == [(PACK, EXPOSURE_FILE, f"rename.{ENTITY}_get")]
    assert reasons(found) == [f"工具 {ENTITY}_get 同时出现在 exclude 和 rename 中"]
    # The tool is dropped, so neither name is exposed and no name of it is checked.
    assert {f"{ENTITY}_get", f"{ENTITY}_detail_get"}.isdisjoint(exposed(target))


# --- the tool names of a profile (requirement 2.10) --------------------------------------


@pytest.mark.req("2.10")
@pytest.mark.parametrize("item", ["include_tools", "exclude_tools"])
def test_a_profile_that_names_a_tool_its_packs_do_not_generate(tmp_path: Path, item: str) -> None:
    found = failures(write_core(tmp_path, profile=profile_doc(**{item: f"[{ENTITY}_delete]"})))

    assert places(found) == [(None, PROFILE_FILE, f"{item}[0]")]
    assert reasons(found) == [
        f"profile semantic 的 {item} 含所选业务包没有生成的工具名 {ENTITY}_delete"
    ]


@pytest.mark.req("2.10")
def test_a_profile_may_name_a_generated_tool_and_the_builtin_tool(tmp_path: Path) -> None:
    target = write_core(
        tmp_path,
        profile=profile_doc(include_tools=f"[{BUSINESS_CONTEXT_GET}, {ENTITY}_get]"),
    )

    # Every instance exposes the built-in tool, so a profile may name it (requirement 11.1).
    target.run_step(6)
    assert exposed(target) == TREE_TOOLS
