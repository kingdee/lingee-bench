"""Loading step 7: the tool descriptions (design.md 2.3).

Step 6 settled the names of the tools; the seventh step settles the text that goes with
them. A model sees a tool through its description alone, so the description is as much part
of the contract as the name and the inputSchema, and everything about it that a machine can
check is checked here, before the datasets are built (step 8):

=================================================  =============================  ==========
check                                              where it is reported           requirement
=================================================  =============================  ==========
``description_variants`` holds zh-detailed and     the manifest item               3.1
no name outside the four known variants
every tool of the pack has a tool description      the description file            3.2
and every parameter of it a parameter
description; blank text counts as missing
a zh-detailed tool description counts at most      the description file            3.3
400 code points
no description of a declared variant contains a    the description file            3.4
phrase of the framework's guiding phrase list
the text of a described rule appears verbatim      the rule and the description    3.6
in the zh-detailed description of each tool        file
``applies_to_tools`` names it
``applies_to_tools`` is not empty and names        the rule item                   3.8
tools the rule's own pack generates
=================================================  =============================  ==========

Every check runs over every pack, so one step reports the errors of all of them
(requirement 2.2). A description is read for the tool names step 6 produced - after
``exclude`` and ``rename``, which is how a description file names its tools - and a
parameter by the path requirement 3.2 writes it with, a top level parameter by its name
and a parameter of the lines element as ``lines[].<name>``
(:attr:`ToolSpec.param_paths <erp_mock.tools.generator.ToolSpec.param_paths>`). The
built-in ``business_context_get`` is no part of any pack and its text is the framework's
own, so no pack is asked for it (requirement 3.9).

What is not checked here is what needs a Snapshot: that no description contains a name, a
code or a number of Business_Data (requirement 3.5) is the cross-check that runs after
step 8 (:func:`erp_mock.loader.step8.cross_check`), and it compares the same way
:func:`~erp_mock.tools.descriptions.guiding_hits` does - over the same variants
(:func:`known_variants`) and the same texts (:func:`entries`), so a text that a guiding
phrase and a record name both occur in is reported at one and the same place twice.

The step leaves the :class:`~erp_mock.tools.descriptions.DescriptionIndex` of the instance
behind: every description document, by pack and variant, which the profile assembly reads
to put the text into the tools of a profile
(:func:`~erp_mock.tools.descriptions.build_profile_tools`, requirement 3.7).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from erp_mock.config.model import DomainModel, RuleModel
from erp_mock.config.pack import DESCRIPTION_VARIANTS, ToolDescription
from erp_mock.loader.errors import Location, StepErrors
from erp_mock.loader.step1 import PACK_FILE
from erp_mock.loader.step4 import DESCRIPTION_VARIANT, DESCRIPTIONS_DIR, RawPack
from erp_mock.loader.step6 import GeneratedTools
from erp_mock.tools.descriptions import DescriptionIndex, guiding_hits
from erp_mock.tools.generator import ToolSpec

__all__ = [
    "DESCRIPTION_LIMIT",
    "Entry",
    "check",
    "description_file",
    "entries",
    "known_variants",
    "variant_file",
]

# The length limit of a zh-detailed tool description, in Unicode code points
# (requirement 3.3); whitespace and punctuation count, parameter descriptions do not.
DESCRIPTION_LIMIT: Final = 400

# The items this step reports at: the manifest list of the variants, the keys of a
# description document and the two items of a described rule.
VARIANTS_ITEM: Final = "description_variants"
TOOLS_KEY: Final = "tools"
DESCRIPTION_KEY: Final = "description"
PARAMS_KEY: Final = "params"
APPLIES_TO_ITEM: Final = "applies_to_tools"
RULE_TEXT_ITEM: Final = "description.zh"

# The first part of the config path of every error of a rule document, which nests its
# object under one key (:mod:`erp_mock.loader.step4`).
_RULE: Final = "rule"
# The enforcement whose text this step checks: the engine does not evaluate such a rule, so
# the description of the tools it applies to is the only place the model learns it
# (requirement 3.6).
_DESCRIBED: Final = "described"


def description_file(variant: str) -> str:
    """The file a pack writes the descriptions of ``variant`` in (business-pack.md 3)."""
    return f"{DESCRIPTIONS_DIR}/{variant}.yaml"


def check(
    packs: Sequence[RawPack], domain: DomainModel, tools: GeneratedTools, errors: StepErrors
) -> DescriptionIndex:
    """Run step 7 on the packs of the instance; return their descriptions by pack and variant.

    ``packs`` are the packs instance.yaml references, in its order, with the documents
    step 4 validated, ``domain`` the model step 5 resolved and ``tools`` the tools step 6
    generated. Everything found is recorded in ``errors``, so the step passed only when
    ``errors`` stayed empty; the index is returned either way, so the steps after this one
    and a test can read what was written.
    """
    index = DescriptionIndex.build(
        (pack.name, doc.document) for pack in packs for doc in pack.descriptions
    )
    for pack in packs:
        for variant in _check_variants(pack, errors):
            _check_variant(pack, tools.packs.get(pack.name, ()), index, variant, errors)
    files = {pack.name: variant_file(pack, DESCRIPTION_VARIANT) for pack in packs}
    _check_described_rules(domain, tools, index, files, errors)
    return index


# --- the variants a pack declares (requirement 3.1) --------------------------------------


def known_variants(pack: RawPack) -> tuple[str, ...]:
    """The variants ``pack`` declares that the framework knows, in the written order.

    The descriptions of these variants are the ones that are checked, here and by the
    cross-check of requirement 3.5 (:mod:`erp_mock.loader.step8`); an unrecognized name is
    an error of this step and its descriptions are not read any further.
    """
    return tuple(
        variant for variant in pack.manifest.description_variants if variant in DESCRIPTION_VARIANTS
    )


def _check_variants(pack: RawPack, errors: StepErrors) -> tuple[str, ...]:
    """Check ``description_variants`` and return the declared variants that are known.

    zh-detailed is the variant every pack must provide, since it is the one a profile of M0
    may ask for (requirement 2.13), and the four names of
    :data:`~erp_mock.config.pack.DESCRIPTION_VARIANTS` are the only ones there are. The
    descriptions of an unrecognized name are not checked any further: what such a file
    ought to contain is not defined.
    """
    declared = tuple(pack.manifest.description_variants)
    if DESCRIPTION_VARIANT not in declared:
        reason = f"manifest 的 {VARIANTS_ITEM} 不含 {DESCRIPTION_VARIANT}"
        errors.add_at(pack.name, PACK_FILE, VARIANTS_ITEM, reason)
    unknown = [variant for variant in declared if variant not in DESCRIPTION_VARIANTS]
    if unknown:
        reason = (
            f"manifest 的 {VARIANTS_ITEM} 含无法识别的变体名 {'、'.join(unknown)}，"
            f"可用的变体名为 {'、'.join(DESCRIPTION_VARIANTS)}"
        )
        errors.add_at(pack.name, PACK_FILE, VARIANTS_ITEM, reason)
    return known_variants(pack)


# --- the descriptions of one variant (requirements 3.2 to 3.4) ---------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    """One text of a description file: where it is written and what it says.

    ``path`` is the parameter path for a parameter description (requirement 3.2) and None
    for the tool description itself, which is what an error names the text by. The
    cross-check of requirement 3.5 reports at the same entries this step does
    (:mod:`erp_mock.loader.step8`).
    """

    location: Location
    text: str
    path: str | None = None

    def about(self, tool: str, variant: str) -> str:
        """How an error names this text: the variant, the tool and the parameter."""
        if self.path is None:
            return f"{variant} 变体中工具 {tool} 的工具描述"
        return f"{variant} 变体中工具 {tool} 的参数 {self.path} 的描述"


def _check_variant(
    pack: RawPack,
    specs: Sequence[ToolSpec],
    index: DescriptionIndex,
    variant: str,
    errors: StepErrors,
) -> None:
    """Check the descriptions ``pack`` wrote for ``variant`` (requirements 3.2 to 3.4).

    Every tool of the pack and every parameter of it needs a description, the tool
    description of zh-detailed has a length limit, and no text may contain a guiding
    phrase. A missing file is not an error of its own: the tools of the pack are then all
    without a description, which is what the step reports, at the path the file would be
    written at.
    """
    file = variant_file(pack, variant)
    written = index.of(pack.name, variant)
    for spec in specs:
        texts = written.get(spec.name, ToolDescription())
        for entry in entries(file, spec, texts):
            if not entry.text.strip():
                # An empty text and one of nothing but whitespace are both missing (3.2).
                errors.add(pack.name, (entry.location,), _missing(spec.name, variant, entry))
                continue
            if entry.path is None and variant == DESCRIPTION_VARIANT:
                _check_length(pack.name, spec.name, entry, errors)
            for phrase in guiding_hits(entry.text):
                reason = f"{entry.about(spec.name, variant)}含引导性用语 {phrase}"
                errors.add(pack.name, (entry.location,), reason)


def variant_file(pack: RawPack, variant: str) -> str:
    """The file ``pack`` wrote the descriptions of ``variant`` in, or where it would be.

    The documents are matched by the ``variant`` they declare and not by their file name
    (requirement 3.2 reads the variant, step 4 the document), so the path of the file is
    taken from the document that declares the variant.
    """
    found = next((doc.file for doc in pack.descriptions if doc.document.variant == variant), None)
    return description_file(variant) if found is None else found


def entries(file: str, spec: ToolSpec, texts: ToolDescription) -> tuple[Entry, ...]:
    """Every text ``spec`` needs in one variant: the tool description, then the parameters.

    The parameters are those of the inputSchema, in declaration order, each under the path
    a description file names it with (requirement 3.2).
    """
    item = f"{TOOLS_KEY}.{spec.name}"
    found = [Entry(Location(file, f"{item}.{DESCRIPTION_KEY}"), texts.description)]
    found += [
        Entry(Location(file, f"{item}.{PARAMS_KEY}.{path}"), texts.params.get(path, ""), path)
        for path in spec.param_paths
    ]
    return tuple(found)


def _missing(tool: str, variant: str, entry: Entry) -> str:
    """The reason for a description a pack did not write (requirement 3.2)."""
    if entry.path is None:
        return f"{variant} 变体没有为工具 {tool} 提供工具描述"
    return f"{variant} 变体没有为工具 {tool} 的参数 {entry.path} 提供参数描述"


def _check_length(pack: str, tool: str, entry: Entry, errors: StepErrors) -> None:
    """The zh-detailed tool description counts at most 400 code points (requirement 3.3).

    Counted in code points, so a Chinese character and an ASCII letter count the same, and
    whitespace, line breaks and punctuation count too; the parameter descriptions are
    counted separately and have no limit.
    """
    length = len(entry.text)
    if length > DESCRIPTION_LIMIT:
        reason = (
            f"工具 {tool} 的 {DESCRIPTION_VARIANT} 工具描述为 {length} 个字符，"
            f"超过上限 {DESCRIPTION_LIMIT}"
        )
        errors.add(pack, (entry.location,), reason)


# --- the rules the descriptions carry (requirements 3.6 and 3.8) -------------------------


def _check_described_rules(
    domain: DomainModel,
    tools: GeneratedTools,
    index: DescriptionIndex,
    files: Mapping[str, str],
    errors: StepErrors,
) -> None:
    """Every described rule reaches the model through the tools it applies to (3.6, 3.8).

    Such a rule is not enforced by the engine, so ``applies_to_tools`` must name at least
    one tool, each of them a tool of the rule's own pack - the tools of another pack and
    the built-in tool are no place for it - and the text of the rule must appear in the
    zh-detailed description of each of them, character for character. ``files`` holds the
    zh-detailed description file of each pack, so an error about the text locates the
    description it is missing from next to the rule that requires it.
    """
    for rule in domain.rules.values():
        if rule.enforcement != _DESCRIBED:
            continue
        generated = {spec.name for spec in tools.packs.get(rule.pack, ())}
        item = f"{_RULE}.{APPLIES_TO_ITEM}"
        if not rule.applies_to_tools:
            reason = f"{_DESCRIBED} 规则 {rule.id} 的 {APPLIES_TO_ITEM} 为空"
            errors.add_at(rule.pack, rule.file, item, reason)
            continue
        written = index.of(rule.pack, DESCRIPTION_VARIANT)
        file = files.get(rule.pack, description_file(DESCRIPTION_VARIANT))
        for position, tool in enumerate(rule.applies_to_tools):
            place = Location(rule.file, f"{item}[{position}]")
            if tool not in generated:
                reason = (
                    f"{_DESCRIBED} 规则 {rule.id} 的 {APPLIES_TO_ITEM} 含业务包 "
                    f"{rule.pack} 没有生成的工具名 {tool}"
                )
                errors.add(rule.pack, (place,), reason)
                continue
            _check_rule_text(rule, tool, place, file, written, errors)


def _check_rule_text(
    rule: RuleModel,
    tool: str,
    place: Location,
    file: str,
    written: Mapping[str, ToolDescription],
    errors: StepErrors,
) -> None:
    """The text of ``rule`` appears verbatim in the zh-detailed description of ``tool`` (3.6).

    Verbatim means the whole of ``description.zh`` as one continuous substring, compared
    character for character: case, whitespace, punctuation and full width forms all count.
    """
    text = "" if rule.description is None else rule.description.zh
    description = written.get(tool, ToolDescription()).description
    if text and text not in description:
        reason = (
            f"{_DESCRIBED} 规则 {rule.id} 的 {RULE_TEXT_ITEM} 没有原样出现在工具 {tool} 的 "
            f"{DESCRIPTION_VARIANT} 工具描述中"
        )
        written_at = Location(file, f"{TOOLS_KEY}.{tool}.{DESCRIPTION_KEY}")
        errors.add(rule.pack, (place, written_at), reason)
