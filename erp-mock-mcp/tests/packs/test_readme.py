"""The README.md of every business pack (requirement 21.10).

A business pack holds no code, so its README is where a reader learns what the declarative
configuration adds up to. Requirement 21.10 asks each pack directory for a README.md that
explains its business scope, the primitives it uses and its known simplifications, the tool
gaps of the M0 known limitations of the requirements introduction included. This module
checks that of every pack of ``packs/``: core, purchase and inventory
(docs/packs/example-p2p.md).

The three sections are found by the keyword their heading holds (:data:`SECTIONS`), so a
pack may word the heading as it likes (``## 业务范围与边界``) and may nest the sections under
any heading level; what a section holds is the text up to the next heading at the same level
or above (:func:`sections`). Beyond their presence, each is checked against the
configuration of the pack, so a README cannot drift from what the pack declares:

- the business scope names every entity and every bill of the pack's ``entities/``;
- the primitive section names every primitive of the pack's ``pack.yaml``; a pack that lists
  the primitives it does *not* use in that section is free to, nothing says the section may
  name only those;
- the known simplifications speak of the tool gaps (:data:`TOOL_GAPS`), which is what
  requirement 21.10 asks for by name.

The three READMEs are prose and the checks are about structure, so what a section says is
not compared with anything: a reader still has to read it. The configuration of each pack is
covered by :mod:`tests.packs.test_core_pack`, :mod:`tests.packs.test_purchase_pack` and
:mod:`tests.packs.test_inventory_pack`, and its behaviour by
:mod:`tests.packs.test_pack_cases`.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.pack import PackManifest
from erp_mock.loader.step1 import PACK_FILE
from erp_mock.loader.step4 import BILL_KEY, ENTITIES_DIR, ENTITY_KEY
from erp_mock.loader.yaml_io import list_files, load_yaml

# The example business packs of the repository and the file requirement 21.10 asks each of
# them for.
PACKS_DIR: Final = Path(__file__).resolve().parents[2] / "packs"
README_FILE: Final = "README.md"

# The three sections of requirement 21.10, by the keyword a heading of the section holds.
SECTIONS: Final[tuple[str, ...]] = ("业务范围", "使用的原语", "已知简化")
SCOPE, PRIMITIVES_USED, SIMPLIFICATIONS = SECTIONS

# What the known simplifications have to speak of: the tool gaps of the M0 known limitations
# of the requirements introduction (requirement 21.10).
TOOL_GAPS: Final = "工具缺口"

# An ATX heading of a Markdown document: one to six number signs, a space and the title.
# A fence opens and closes a code block, whose lines are text and no heading.
_HEADING: Final = re.compile(r"(?P<level>#{1,6})[ \t]+(?P<title>.*?)[ \t]*$")
_FENCE: Final = "```"


@dataclass(frozen=True, slots=True)
class Section:
    """One section of a Markdown document: a heading and the text below it.

    ``level`` is the number of number signs of the heading, ``title`` its text and ``body``
    the lines up to the next heading at the same level or above, the heading itself left
    out.
    """

    level: int
    title: str
    body: str


def sections(text: str) -> tuple[Section, ...]:
    """The sections of the Markdown document ``text``, in the order they are written.

    Only ATX headings are read (``## 业务范围``); a document may hold text before its first
    heading, which belongs to no section. The body of a section stops at the next heading at
    the same level or above, so the body of a ``##`` section holds its ``###`` subsections.
    Lines inside a fenced code block are text, so a configuration sample whose comment
    starts with a number sign does not end the section it is in.
    """
    found: list[Section] = []
    starts: list[tuple[int, str, int]] = []
    lines = text.splitlines()
    fenced = False
    for number, line in enumerate(lines):
        if line.lstrip().startswith(_FENCE):
            fenced = not fenced
            continue
        heading = None if fenced else _HEADING.fullmatch(line)
        if heading is not None:
            starts.append((len(heading["level"]), heading["title"], number))
    for index, (level, title, start) in enumerate(starts):
        end = next(
            (line for next_level, _, line in starts[index + 1 :] if next_level <= level),
            len(lines),
        )
        found.append(Section(level, title, "\n".join(lines[start + 1 : end])))
    return tuple(found)


def section_body(text: str, keyword: str) -> str:
    """The body of the section of ``text`` whose heading holds ``keyword``.

    The first such section, and the empty text when the document has none; a section whose
    heading holds the keyword but whose body is blank gives the empty text as well, so one
    test covers a missing section and an empty one.
    """
    for section in sections(text):
        if keyword in section.title and section.body.strip():
            return section.body
    return ""


def missing_sections(text: str) -> tuple[str, ...]:
    """The keywords of :data:`SECTIONS` that ``text`` has no section with a body for."""
    return tuple(keyword for keyword in SECTIONS if not section_body(text, keyword))


def pack_dirs(packs_dir: Path) -> tuple[Path, ...]:
    """The pack directories of ``packs_dir``, sorted by name.

    A pack is a directory that holds a ``pack.yaml``, named after the directory the way
    instance.yaml references it. A missing ``packs_dir`` gives no pack, which collects no
    test rather than failing at import time.
    """
    if not packs_dir.is_dir():
        return ()
    return tuple(
        path
        for path in sorted(packs_dir.iterdir())
        if path.is_dir() and (path / PACK_FILE).is_file()
    )


def declared_names(pack_dir: Path) -> tuple[str, ...]:
    """The entity and bill names the pack at ``pack_dir`` declares, in file order.

    The top-level key of a document of ``entities/`` says which of the two it declares
    (:mod:`erp_mock.loader.step4`). The same names are collected by
    :mod:`tests.static.test_no_business_names`, which keeps them out of the engine source.
    """
    found: list[str] = []
    for path in list_files(pack_dir / ENTITIES_DIR):
        document = load_yaml(path)
        if not isinstance(document, dict):
            continue
        found.extend(
            name for key in (ENTITY_KEY, BILL_KEY) if isinstance(name := document.get(key), str)
        )
    return tuple(found)


def manifest(pack_dir: Path) -> PackManifest:
    """The validated pack.yaml of the pack at ``pack_dir`` (requirement 2.3)."""
    return PackManifest.model_validate(load_yaml(pack_dir / PACK_FILE))


def readme(pack_dir: Path) -> str:
    """The README.md of the pack at ``pack_dir``, decoded as UTF-8."""
    return (pack_dir / README_FILE).read_text(encoding="utf-8")


# The packs this module is about; every test runs once per pack, named after it.
PACKS: Final = pack_dirs(PACKS_DIR)
PARAMS: Final = tuple(pytest.param(pack, id=pack.name) for pack in PACKS)


@pytest.mark.req("21.10")
@pytest.mark.parametrize("pack", PARAMS)
def test_the_pack_directory_holds_a_readme(pack: Path) -> None:
    path = pack / README_FILE
    assert path.is_file(), f"{pack.name} 没有 {README_FILE}（需求 21 第 10 条）"
    # Readable as UTF-8 and not empty: the decoding would raise otherwise.
    assert readme(pack).strip()


@pytest.mark.req("21.10")
@pytest.mark.parametrize("pack", PARAMS)
def test_the_readme_holds_the_three_sections(pack: Path) -> None:
    missing = missing_sections(readme(pack))
    assert missing == (), f"{pack.name}/{README_FILE} 缺少章节：{'、'.join(missing)}"


@pytest.mark.req("21.10")
@pytest.mark.parametrize("pack", PARAMS)
def test_the_business_scope_names_every_entity_and_bill(pack: Path) -> None:
    body = section_body(readme(pack), SCOPE)
    unnamed = [name for name in declared_names(pack) if name not in body]
    assert unnamed == [], f"{pack.name} 的{SCOPE}没有提到：{'、'.join(unnamed)}"


@pytest.mark.req("21.10")
@pytest.mark.parametrize("pack", PARAMS)
def test_the_primitive_section_names_every_primitive_of_the_manifest(pack: Path) -> None:
    body = section_body(readme(pack), PRIMITIVES_USED)
    unnamed = [name for name in manifest(pack).primitives if name not in body]
    assert unnamed == [], f"{pack.name} 的{PRIMITIVES_USED}没有提到：{'、'.join(unnamed)}"


@pytest.mark.req("21.10")
@pytest.mark.parametrize("pack", PARAMS)
def test_the_known_simplifications_speak_of_the_tool_gaps(pack: Path) -> None:
    body = section_body(readme(pack), SIMPLIFICATIONS)
    assert TOOL_GAPS in body, f"{pack.name} 的{SIMPLIFICATIONS}没有说明{TOOL_GAPS}"


@pytest.mark.req("21.10")
def test_the_example_packs_are_the_packs_that_are_checked() -> None:
    # A new business pack is checked by the same tests, so it has to bring a README too.
    # core, purchase and inventory are hand written (docs/packs/example-p2p.md). The packs of
    # the erp-benchmark instance ship with the datasets (datasets/README.md), not here.
    assert [pack.name for pack in PACKS] == ["core", "inventory", "purchase"]


SAMPLE: Final = """
# demo 业务包

## 业务范围

一个实体：widget。

```yaml
# 一条注释，不是标题
entity: widget
```

### 不在范围内

调拨单。

## 使用的原语

crud 与 numbering。

## 已知简化

- 工具缺口：没有写工具。
"""


@pytest.mark.req("21.10")
def test_a_section_is_the_text_up_to_the_next_heading_of_its_level() -> None:
    found = {section.title: section for section in sections(SAMPLE)}
    assert list(found) == ["demo 业务包", "业务范围", "不在范围内", "使用的原语", "已知简化"]
    assert missing_sections(SAMPLE) == ()
    # The body of the scope section holds its subsection and the comment of the fenced
    # block, and stops at the next ``##``.
    assert "不在范围内" in found[SCOPE].body
    assert "一条注释，不是标题" in found[SCOPE].body
    assert "crud" not in found[SCOPE].body
    assert found["不在范围内"].level == 3


@pytest.mark.req("21.10")
@pytest.mark.parametrize("keyword", SECTIONS)
def test_a_readme_without_one_of_the_sections_is_reported(keyword: str) -> None:
    # The heading is dropped, which leaves its text in the section before it.
    without = "\n".join(line for line in SAMPLE.splitlines() if keyword not in line)
    assert missing_sections(without) == (keyword,)


# A README whose primitive section is a heading and nothing else, which is what a pack with
# a placeholder section looks like.
EMPTY_SECTION: Final = """
# demo 业务包

## 业务范围

一个实体：widget。

## 使用的原语

## 已知简化

- 工具缺口：没有写工具。
"""


@pytest.mark.req("21.10")
def test_a_readme_with_an_empty_section_is_reported() -> None:
    assert missing_sections(EMPTY_SECTION) == (PRIMITIVES_USED,)
