"""Loading step 7: the tool descriptions (requirements 3.1 to 3.4, 3.6 and 3.8).

Two halves, the way the step 6 tests are built:

- the engine test fixture passes step 7 on the real directories, so the descriptions its
  two packs carry are a worked example of what the step accepts: a description for every
  generated tool and every parameter of it, each within the 400 code points of
  requirement 3.3, none of them naming a guiding phrase, and the text of the described rule
  written verbatim into the tool it applies to;
- one example per kind of error the step reports, each on a tree written below ``tmp_path``
  that holds exactly the one thing that is wrong, asserted by the places and the reasons the
  loader reports.

:data:`BASE_FILES` is the pack every tree is written against: an entity with one operation
and a pricing bill with a create tool, so that the tools of the tree take the two kinds of
parameter requirement 3.2 distinguishes - a top-level parameter and a parameter of the
lines element, written ``lines[].<name>``. :data:`TREE_PARAMS` states what those tools take,
which is what the description files of the trees are written from.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest

from erp_mock.loader.errors import ConfigError, ConfigErrors
from erp_mock.loader.loader import Loader
from erp_mock.loader.step4 import DESCRIPTION_VARIANT
from erp_mock.loader.step7 import DESCRIPTION_LIMIT, description_file
from erp_mock.tools.descriptions import GUIDING_PHRASES
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET
from tests.helpers.fixtures import BASE_PACK, TRADE_PACK, load_fixture
from tests.helpers.tree import (
    CONTEXT_FILES,
    bill_doc,
    dataset_doc,
    entity_doc,
    profile_doc,
    write_instance,
    write_pack,
)

INSTANCE_DIR = "instance"
PACKS_DIR = "packs"

# The pack the default instance.yaml of tests.helpers.tree references, and the two files an
# error of this module is reported at besides the description file.
PACK = "core"
PACK_FILE = "pack.yaml"
RULE_FILE = "rules/described.yaml"

# The variant M0 serves and the file a pack writes it in (business-pack.md section 3).
VARIANT: Final = DESCRIPTION_VARIANT
VARIANT_FILE: Final = description_file(VARIANT)

# One place of a reported error: the pack, the file and the config path.
type Place = tuple[str | None, str, str | None]
# The texts of one tool: its tool description and its parameter descriptions by path.
type ToolTexts = tuple[str, Mapping[str, str]]


# --- the descriptions of the engine test fixture -----------------------------------------


@pytest.mark.req("3.1", "3.2", "3.3", "3.4", "3.6", "3.8")
def test_the_fixture_passes_step_7() -> None:
    loader = load_fixture(7)

    # Step 7 raises ConfigErrors when it finds anything, so a fixture whose descriptions
    # are incomplete, too long, guiding or missing a rule text fails here.
    index = loader.descriptions
    assert index is not None
    assert {pack: tuple(variants) for pack, variants in index.packs.items()} == {
        BASE_PACK: (VARIANT,),
        TRADE_PACK: (VARIANT,),
    }
    # The index is keyed by the name a pack exposes a tool under, after exclude and
    # rename, which is how a description file names its tools (requirement 3.2).
    tools = loader.tools
    assert tools is not None
    for pack in (BASE_PACK, TRADE_PACK):
        written = index.of(pack, VARIANT)
        assert set(written) == {spec.name for spec in tools.packs[pack]}
    assert index.of(BASE_PACK, VARIANT)["vendor_detail_get"].description


@pytest.mark.req("3.9")
def test_no_pack_describes_the_builtin_tool() -> None:
    loader = load_fixture(7)

    # The text of business_context_get is the framework's own, so no pack writes it and
    # step 7 asks none of them for it (requirement 3.9).
    index = loader.descriptions
    assert index is not None
    for pack in (BASE_PACK, TRADE_PACK):
        assert BUSINESS_CONTEXT_GET not in index.of(pack, VARIANT)
    assert index.of(None, VARIANT)[BUSINESS_CONTEXT_GET].description


# --- the pack every tree of the error cases is written against ---------------------------

# The objects of the tree: an entity with one operation and a pricing bill with a create
# tool, whose lines carry a declared field and the standard fields of pricing.
ENTITY: Final = "supplier"
BILL: Final = "purchase_order"
ENTITY_TOOL: Final = f"{ENTITY}_get"
BILL_TOOL: Final = f"{BILL}_create"

# The namespaces of the documents below, plus those of CONTEXT_FILES.
IDS: Final = "[sup, po, orgu, emp]"
NUMBERS: Final = "[CGDD]"
TOOL_PREFIXES: Final = "[supplier_, purchase_]"

BASE_FILES: Final[Mapping[str, str]] = {
    **CONTEXT_FILES,
    f"entities/{ENTITY}.yaml": entity_doc(operations="[get]"),
    f"entities/{BILL}.yaml": bill_doc(
        pricing="true",
        header=f"{{ {ENTITY}_id: {{ type: ref, ref: {ENTITY}, required: true }} }}",
        lines="{ memo: { type: string } }",
        operations="[create]",
    ),
}

# The parameter of the lines element whose description the tests leave out, the path of
# requirement 3.2's own example.
LINE_PARAM: Final = "lines[].qty"

# What the two tools of the tree take, in the order requirement 11.4 puts them in: the
# declared header field, the writable standard header fields, lines, and the parameters of
# the lines element, a declared one and the three of pricing (requirement 3.2).
TREE_PARAMS: Final[Mapping[str, tuple[str, ...]]] = {
    ENTITY_TOOL: ("id",),
    BILL_TOOL: (
        f"{ENTITY}_id",
        "biz_date",
        "price_includes_tax",
        "remark",
        "lines",
        "lines[].memo",
        LINE_PARAM,
        "lines[].price",
        "lines[].tax_rate",
    ),
}

# The texts the trees are written with: a tool description in the five parts of
# tool-design.md section 3 and one parameter description, neither of them naming a guiding
# phrase or a record of any dataset.
TOOL_TEXT: Final = (
    "按内部 ID 获取一条记录的全部字段。\n"
    "如需按名称或编码查找，请使用对应的 search 工具。\n"
    "前置条件：id 由 search 工具返回。\n"
    "副作用：无，只读。\n"
    "返回：记录的全部字段，值为空的字段返回 null。"
)
PARAM_TEXT: Final = "参数的含义、格式与取值来源；不填时的缺省行为见工具描述"


def texts_of(params: Sequence[str], description: str = TOOL_TEXT) -> ToolTexts:
    """The texts of one tool: ``description`` and the default text for every parameter."""
    return (description, {path: PARAM_TEXT for path in params})


def entity_texts(description: str = TOOL_TEXT, param: str = PARAM_TEXT) -> ToolTexts:
    """The texts of the entity tool, which takes ``id`` and nothing else."""
    return (description, {"id": param})


def bill_texts(description: str = TOOL_TEXT) -> ToolTexts:
    """The texts of the bill tool, whose parameters include those of the lines element."""
    return texts_of(TREE_PARAMS[BILL_TOOL], description)


def tree_texts(**overrides: ToolTexts | None) -> Mapping[str, ToolTexts]:
    """The texts of both tools of the tree, with ``overrides`` applied.

    An override replaces the texts of one tool and, when it is None, drops the tool from
    the description file, which is how a test leaves a tool undescribed.
    """
    written = {name: texts_of(params) for name, params in TREE_PARAMS.items()}
    chosen = {**written, **overrides}
    return {name: texts for name, texts in chosen.items() if texts is not None}


def description_doc(tools: Mapping[str, ToolTexts], variant: str = VARIANT) -> str:
    """A ``tools/descriptions/<variant>.yaml`` document holding the texts of ``tools``.

    Every text is written as a double-quoted YAML scalar, so a line break inside a
    description and a parameter path such as ``lines[].qty`` are written as they are.
    """
    lines = [f"variant: {variant}", "tools:"]
    for name, (description, params) in tools.items():
        lines.append(f"  {name}:")
        lines.append(f"    description: {_scalar(description)}")
        if params:
            lines.append("    params:")
            lines += [f"      {_scalar(path)}: {_scalar(text)}" for path, text in params.items()]
    return "\n".join(lines) + "\n"


def _scalar(text: str) -> str:
    """``text`` as a double-quoted YAML scalar; JSON string syntax is a subset of it."""
    return json.dumps(text, ensure_ascii=False)


def rule_doc(text: str, applies_to: str) -> str:
    """A described rule whose ``description.zh`` is ``text`` (meta-model.md section 9)."""
    return (
        "rule:\n"
        "  id: R-CORE-001\n"
        "  enforcement: described\n"
        f"  description: {{ zh: {_scalar(text)} }}\n"
        f"  applies_to_tools: {applies_to}\n"
    )


def write_core(
    tmp_path: Path,
    descriptions: str | None = None,
    *,
    variant: str = VARIANT,
    variants: str | None = None,
    rule: str | None = None,
) -> Loader:
    """An instance of the one pack ``core``, holding :data:`BASE_FILES` and a description file.

    ``descriptions`` defaults to a document that describes both tools and every parameter of
    them, so a test writes the one thing it is about; ``variant`` is the variant that
    document declares and the file it is written in, ``variants`` the
    ``description_variants`` of the manifest, which defaults to the variant alone, and
    ``rule`` a described rule document.
    """
    write_instance(
        tmp_path / INSTANCE_DIR,
        {
            "profiles/semantic.yaml": profile_doc(),
            "datasets/purchase-basic.yaml": dataset_doc(),
        },
    )
    files = {
        **BASE_FILES,
        description_file(variant): (
            descriptions if descriptions is not None else description_doc(tree_texts(), variant)
        ),
    }
    if rule is not None:
        files[RULE_FILE] = rule
    write_pack(
        tmp_path / PACKS_DIR,
        PACK,
        files=files,
        id_prefixes=IDS,
        number_prefixes=NUMBERS,
        tool_prefixes=TOOL_PREFIXES,
        description_variants=variants if variants is not None else f"[{variant}]",
    )
    target = Loader(tmp_path / INSTANCE_DIR, tmp_path / PACKS_DIR)
    for step in (1, 2, 3, 4, 5, 6):
        target.run_step(step)
    return target


def failures(target: Loader) -> tuple[ConfigError, ...]:
    """The errors step 7 reports for ``target``, in report order; it must find some."""
    with pytest.raises(ConfigErrors) as caught:
        target.run_step(7)
    assert caught.value.step == 7
    return caught.value.errors


def places(errors: Sequence[ConfigError]) -> list[Place]:
    """The places of ``errors``, in report order."""
    return [
        (error.pack, location.file, location.config_path)
        for error in errors
        for location in error.locations
    ]


def reasons(errors: Sequence[ConfigError]) -> list[str]:
    """The reasons of ``errors``, in report order."""
    return [error.reason for error in errors]


@pytest.mark.req("3.2")
def test_a_tree_that_describes_every_tool_and_every_parameter_passes(tmp_path: Path) -> None:
    target = write_core(tmp_path)
    target.run_step(7)

    index = target.descriptions
    assert index is not None
    written = index.of(PACK, VARIANT)
    assert set(written) == set(TREE_PARAMS)
    # The parameters of the description file are those of the inputSchema, which is what
    # the step checks the file against (requirement 3.2).
    tools = target.tools
    assert tools is not None
    assert {spec.name: spec.param_paths for spec in tools.packs[PACK]} == dict(TREE_PARAMS)
    assert tuple(written[BILL_TOOL].params) == TREE_PARAMS[BILL_TOOL]


# --- the variants a pack declares (requirement 3.1) --------------------------------------


@pytest.mark.req("3.1")
def test_a_manifest_without_zh_detailed(tmp_path: Path) -> None:
    # The pack describes en-detailed completely, so the one thing wrong is that the
    # variant every profile of M0 asks for is not among its variants.
    found = failures(write_core(tmp_path, variant="en-detailed"))

    assert places(found) == [(PACK, PACK_FILE, "description_variants")]
    assert reasons(found) == [f"manifest 的 description_variants 不含 {VARIANT}"]


@pytest.mark.req("3.1")
def test_a_manifest_with_an_unrecognized_variant_name(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, variants=f"[{VARIANT}, zh-long, fr-detailed]"))

    assert places(found) == [(PACK, PACK_FILE, "description_variants")]
    assert reasons(found) == [
        "manifest 的 description_variants 含无法识别的变体名 zh-long、fr-detailed，"
        "可用的变体名为 zh-detailed、zh-brief、en-detailed、en-brief"
    ]


# --- a description for every tool and every parameter (requirement 3.2) ------------------


@pytest.mark.req("3.2")
def test_a_tool_without_any_description(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, description_doc(tree_texts(supplier_get=None))))

    # The tool and its one parameter are both without a text, so the step reports both.
    assert places(found) == [
        (PACK, VARIANT_FILE, f"tools.{ENTITY_TOOL}.description"),
        (PACK, VARIANT_FILE, f"tools.{ENTITY_TOOL}.params.id"),
    ]
    assert reasons(found) == [
        f"{VARIANT} 变体没有为工具 {ENTITY_TOOL} 提供工具描述",
        f"{VARIANT} 变体没有为工具 {ENTITY_TOOL} 的参数 id 提供参数描述",
    ]


@pytest.mark.req("3.2")
@pytest.mark.parametrize(
    "written", ["", "   \n\t", None], ids=["empty", "whitespace", "not written"]
)
def test_a_missing_parameter_description_of_a_lines_element(
    tmp_path: Path, written: str | None
) -> None:
    # Dropped, empty or nothing but whitespace: all three count as missing (req 3.2).
    params = {path: PARAM_TEXT for path in TREE_PARAMS[BILL_TOOL] if path != LINE_PARAM}
    if written is not None:
        params[LINE_PARAM] = written
    doc = description_doc(tree_texts(purchase_order_create=(TOOL_TEXT, params)))
    found = failures(write_core(tmp_path, doc))

    assert places(found) == [(PACK, VARIANT_FILE, f"tools.{BILL_TOOL}.params.{LINE_PARAM}")]
    assert reasons(found) == [
        f"{VARIANT} 变体没有为工具 {BILL_TOOL} 的参数 {LINE_PARAM} 提供参数描述"
    ]


# --- the length of a zh-detailed tool description (requirement 3.3) ----------------------


def description_of(length: int) -> str:
    """A tool description of exactly ``length`` code points, line breaks included.

    Counted in code points, so a Chinese character counts as one, like the ASCII letters
    and the line break the text also holds (requirement 3.3).
    """
    filler = "按内部 ID 获取一条记录的全部字段，返回 id、code 与 name。\n"
    text = (filler * (length // len(filler) + 1))[:length]
    assert len(text) == length
    return text


@pytest.mark.req("3.3")
def test_a_tool_description_of_exactly_400_code_points(tmp_path: Path) -> None:
    texts = entity_texts(description_of(DESCRIPTION_LIMIT))
    target = write_core(tmp_path, description_doc(tree_texts(supplier_get=texts)))

    # The limit is inclusive: 400 passes, 401 does not.
    target.run_step(7)


@pytest.mark.req("3.3")
def test_a_tool_description_of_401_code_points(tmp_path: Path) -> None:
    texts = entity_texts(description_of(DESCRIPTION_LIMIT + 1))
    found = failures(write_core(tmp_path, description_doc(tree_texts(supplier_get=texts))))

    assert places(found) == [(PACK, VARIANT_FILE, f"tools.{ENTITY_TOOL}.description")]
    assert reasons(found) == [
        f"工具 {ENTITY_TOOL} 的 {VARIANT} 工具描述为 401 个字符，超过上限 {DESCRIPTION_LIMIT}"
    ]


@pytest.mark.req("3.3")
def test_a_parameter_description_is_not_counted(tmp_path: Path) -> None:
    texts = entity_texts(param=description_of(DESCRIPTION_LIMIT * 2))
    target = write_core(tmp_path, description_doc(tree_texts(supplier_get=texts)))

    # The limit is on the tool description alone (requirement 3.3).
    target.run_step(7)


# --- guiding phrases (requirement 3.4) ---------------------------------------------------


@pytest.mark.req("3.4")
def test_the_framework_maintains_the_guiding_phrase_list() -> None:
    # The two phrases requirement 3.4 names are part of the list the framework ships.
    assert {"推荐优先使用", "这是正确的工具"} <= set(GUIDING_PHRASES)
    assert all(phrase == phrase.strip() and phrase for phrase in GUIDING_PHRASES)


@pytest.mark.req("3.4")
def test_a_guiding_phrase_in_a_tool_description(tmp_path: Path) -> None:
    texts = entity_texts(f"{TOOL_TEXT}\n读取单条记录推荐优先使用该工具。")
    found = failures(write_core(tmp_path, description_doc(tree_texts(supplier_get=texts))))

    assert places(found) == [(PACK, VARIANT_FILE, f"tools.{ENTITY_TOOL}.description")]
    assert reasons(found) == [
        f"{VARIANT} 变体中工具 {ENTITY_TOOL} 的工具描述含引导性用语 推荐优先使用"
    ]


@pytest.mark.req("3.4")
def test_a_guiding_phrase_in_a_parameter_description(tmp_path: Path) -> None:
    texts = entity_texts(param=f"{PARAM_TEXT}；这是正确的工具")
    found = failures(write_core(tmp_path, description_doc(tree_texts(supplier_get=texts))))

    assert places(found) == [(PACK, VARIANT_FILE, f"tools.{ENTITY_TOOL}.params.id")]
    assert reasons(found) == [
        f"{VARIANT} 变体中工具 {ENTITY_TOOL} 的参数 id 的描述含引导性用语 这是正确的工具"
    ]


@pytest.mark.req("3.4")
def test_ascii_letters_of_a_guiding_phrase_are_compared_without_case(tmp_path: Path) -> None:
    texts = entity_texts(f"{TOOL_TEXT}\nAlways Use This Tool to read one record.")
    found = failures(write_core(tmp_path, description_doc(tree_texts(supplier_get=texts))))

    # The phrase is reported as the list writes it, not as the description spells it.
    assert places(found) == [(PACK, VARIANT_FILE, f"tools.{ENTITY_TOOL}.description")]
    assert reasons(found) == [
        f"{VARIANT} 变体中工具 {ENTITY_TOOL} 的工具描述含引导性用语 always use this tool"
    ]


# --- the rules the descriptions carry (requirements 3.6 and 3.8) -------------------------

RULE_ID: Final = "R-CORE-001"
RULE_TEXT: Final = "本工具只生成草稿，审核后才改变库存"


@pytest.mark.req("3.6")
def test_a_described_rule_whose_text_appears_verbatim(tmp_path: Path) -> None:
    described = bill_texts(f"{RULE_TEXT}。{TOOL_TEXT}")
    target = write_core(
        tmp_path,
        description_doc(tree_texts(purchase_order_create=described)),
        rule=rule_doc(RULE_TEXT, f"[{BILL_TOOL}]"),
    )

    target.run_step(7)


@pytest.mark.req("3.6")
def test_a_described_rule_whose_text_is_not_written_verbatim(tmp_path: Path) -> None:
    # The same words with two characters changed: the whole text must occur as one
    # continuous substring, character for character (requirement 3.6).
    described = bill_texts(f"{RULE_TEXT.replace('草稿', '单据')}。{TOOL_TEXT}")
    found = failures(
        write_core(
            tmp_path,
            description_doc(tree_texts(purchase_order_create=described)),
            rule=rule_doc(RULE_TEXT, f"[{BILL_TOOL}]"),
        )
    )

    # The error locates the rule that requires the text and the description it is missing
    # from (requirement 2.2).
    assert places(found) == [
        (PACK, RULE_FILE, "rule.applies_to_tools[0]"),
        (PACK, VARIANT_FILE, f"tools.{BILL_TOOL}.description"),
    ]
    assert reasons(found) == [
        f"described 规则 {RULE_ID} 的 description.zh 没有原样出现在工具 {BILL_TOOL} 的 "
        f"{VARIANT} 工具描述中"
    ]


@pytest.mark.req("3.8")
def test_a_described_rule_without_any_tool(tmp_path: Path) -> None:
    found = failures(write_core(tmp_path, rule=rule_doc(RULE_TEXT, "[]")))

    assert places(found) == [(PACK, RULE_FILE, "rule.applies_to_tools")]
    assert reasons(found) == [f"described 规则 {RULE_ID} 的 applies_to_tools 为空"]


@pytest.mark.req("3.8")
def test_a_described_rule_that_names_tools_its_pack_does_not_generate(tmp_path: Path) -> None:
    described = bill_texts(f"{RULE_TEXT}。{TOOL_TEXT}")
    applies_to = f"[{BUSINESS_CONTEXT_GET}, {ENTITY}_delete, {BILL_TOOL}]"
    found = failures(
        write_core(
            tmp_path,
            description_doc(tree_texts(purchase_order_create=described)),
            rule=rule_doc(RULE_TEXT, applies_to),
        )
    )

    # The built-in tool and a tool the entity does not declare an operation for both count
    # as not generated; the third name is a tool of the pack and carries the text.
    assert places(found) == [
        (PACK, RULE_FILE, "rule.applies_to_tools[0]"),
        (PACK, RULE_FILE, "rule.applies_to_tools[1]"),
    ]
    assert reasons(found) == [
        f"described 规则 {RULE_ID} 的 applies_to_tools 含业务包 {PACK} "
        f"没有生成的工具名 {BUSINESS_CONTEXT_GET}",
        f"described 规则 {RULE_ID} 的 applies_to_tools 含业务包 {PACK} "
        f"没有生成的工具名 {ENTITY}_delete",
    ]
