"""Loading step 6: the generated tools and their names (design.md 2.3).

The sixth step runs the Tool_Generator (:mod:`erp_mock.tools.generator`) over the domain
model step 5 resolved and checks what comes out. The names of the tools are the contract
the model works against, so everything about a name is settled here, before a description
is read (step 7) or a dataset is built (step 8):

===============================================  ============================  ==========
check                                            where it is reported          requirement
===============================================  ============================  ==========
the name matches ``^[a-z][a-z0-9_]{0,63}$``      the item that names the tool   11.2
a push tool is ``<target>_create_from_<short>``  the rename item                11.2, 11.13
every other name ends with an action of the      the item that names the tool   11.2
word list of tool-design.md section 2
a rename does not change the action              the rename item                11.2
a name starts with a ``tool_prefixes`` of its    the item that names the tool   11.2
own pack, a built-in tool excepted
no two tools share a name, the built-in          every place involved           2.7
``business_context_get`` included
``exclude`` and ``rename`` name tools the pack   the exclude or rename item     11.14
generates, and name none of them twice
``include_tools`` and ``exclude_tools`` of a     the item of the profile        2.10
profile name tools its packs generate
===============================================  ============================  ==========

Every check runs, so one step reports the errors of all of them (requirement 2.2). A name
is checked as it is exposed - after ``exclude`` and ``rename`` - because that is the name
tools/list returns; the generated name is reported next to it where the two differ, which
is what makes a rename error readable (``rename 把 vendor_get 改为 ...``).

The step leaves :class:`GeneratedTools` behind: the built-in tools and the tools of each
pack, in the output order of requirement 11.9. Step 7 reads it to check that every tool of
a pack has a description, and the profile assembly builds the tools/list payload of a
profile from it (:meth:`GeneratedTools.of`).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.instance import ProfileDef
from erp_mock.config.model import DomainModel
from erp_mock.config.pack import ExposureDef
from erp_mock.loader.errors import Location, StepErrors
from erp_mock.loader.step1 import PACK_FILE
from erp_mock.loader.step4 import CONVERSION_KEY, EXPOSURE_FILE, RawDoc, RawPack
from erp_mock.tools.generator import (
    BUILTIN_TOOLS,
    NAME_PATTERN,
    ToolSpec,
    generate_pack_tools,
    generated_tool_names,
    tool_action,
)

__all__ = ["GeneratedTools", "check"]

# The items of an exposure, a manifest and a profile this step reports at.
EXCLUDE: Final = "exclude"
RENAME: Final = "rename"
QUERIES: Final = "queries"
TOOL: Final = "tool"
OPERATIONS: Final = "operations"
TOOL_PREFIXES: Final = "tool_prefixes"
INCLUDE_TOOLS: Final = "include_tools"
EXCLUDE_TOOLS: Final = "exclude_tools"

# The form of a tool name (requirement 11.2), compiled once.
_NAME: Final = re.compile(NAME_PATTERN)

# How an error names the engine's own tools, which no file declares.
_BUILTIN_LABEL: Final = "引擎内置工具"


@dataclass(frozen=True, slots=True)
class GeneratedTools:
    """Every tool of the instance: the engine's own ones and those of each pack.

    ``builtin`` holds the tools the engine brings itself, which belong to no pack and are
    part of every profile (requirement 11.1); ``packs`` holds the tools each pack exposes,
    keyed by pack name in the order of instance.yaml and each in the output order of
    requirement 11.9. The tools are those of a valid instance only when the step reported
    nothing, as the domain model they come from is.
    """

    builtin: tuple[ToolSpec, ...] = ()
    packs: FrozenMap[str, tuple[ToolSpec, ...]] = FrozenMap()

    @property
    def tools(self) -> tuple[ToolSpec, ...]:
        """Every tool, the built-in ones first and the packs in instance.yaml order."""
        return self.of(self.packs)

    def of(self, packs: Iterable[str]) -> tuple[ToolSpec, ...]:
        """The tools a profile over ``packs`` exposes, in output order (requirement 11.9).

        The built-in tools come first, then the tools of each pack in the order ``packs``
        gives them, which for a profile is the order of its own ``packs``; a name that is
        no pack of the instance contributes nothing.
        """
        chosen = (spec for pack in packs for spec in self.packs.get(pack, ()))
        return (*self.builtin, *chosen)


def check(
    packs: Sequence[RawPack],
    domain: DomainModel,
    profiles: Sequence[RawDoc[ProfileDef]],
    errors: StepErrors,
) -> GeneratedTools:
    """Run step 6 on the packs of the instance; return the tools they generate.

    ``packs`` are the packs instance.yaml references, in its order, with the documents
    step 4 validated, ``domain`` the model step 5 resolved out of them and ``profiles`` the
    profile definitions step 4 read. Everything found is recorded in ``errors``, so the
    step passed only when ``errors`` stayed empty; the tools are returned either way, so
    the steps after this one and a test can read what was generated.
    """
    generated = tuple(_PackTools(pack, domain) for pack in packs)
    for tools in generated:
        _check_exposure(tools, errors)
        for spec in tools.tools:
            _check_name(tools, spec, errors)
    _check_declared_queries(generated, errors)
    _check_duplicates(generated, errors)
    result = GeneratedTools(
        builtin=BUILTIN_TOOLS,
        packs=FrozenMap((tools.name, tools.tools) for tools in generated),
    )
    _check_profiles(result, profiles, errors)
    return result


# --- the tools of one pack ---------------------------------------------------------------


class _PackTools:
    """The tools one pack generates, with the place and the label of each of them.

    Built once per pack: ``generated`` are the names requirement 11.1 generates, before
    ``exclude`` and ``rename`` (what those two may name, requirement 11.14), and ``tools``
    the tools the pack exposes, in output order. ``places`` is where each generated tool is
    declared, so an error about a name is reported at the item that names it: the
    ``operations`` of an entity or a bill, the conversion document of a push, the
    ``queries`` item of a query and, for a renamed tool, the ``rename`` item itself.
    """

    __slots__ = ("domain", "exposure", "file", "generated", "places", "raw", "tools")

    def __init__(self, raw: RawPack, domain: DomainModel) -> None:
        self.raw = raw
        self.domain = domain
        self.exposure: ExposureDef | None = None if raw.exposure is None else raw.exposure.document
        # Where an error about the exposure is reported; a pack without the file has
        # nothing in it to report, and the path is then the one it would be written at.
        self.file = EXPOSURE_FILE if raw.exposure is None else raw.exposure.file
        self.generated = generated_tool_names(domain, raw.name, self.exposure)
        self.tools = generate_pack_tools(domain, raw.name, self.exposure)
        self.places = self._places()

    @property
    def name(self) -> str:
        """The pack name, as instance.yaml references it."""
        return self.raw.name

    @property
    def prefixes(self) -> tuple[str, ...]:
        """The ``tool_prefixes`` of the manifest, one of which every tool name starts with."""
        return tuple(self.raw.manifest.tool_prefixes)

    def location(self, spec: ToolSpec) -> Location:
        """The place the name of ``spec`` is written: the rename item, or its declaration."""
        if spec.renamed:
            return Location(self.file, f"{RENAME}.{spec.generated_name}")
        return self.places.get((spec.kind, spec.target), Location(self.file))

    def label(self, spec: ToolSpec) -> str:
        """How an error names ``spec`` among the parties to a conflict over its name."""
        match spec.kind:
            case "push":
                return f"业务包 {self.name} 的下推 {spec.target} 的工具"
            case "query":
                return f"业务包 {self.name} 的查询工具 {spec.generated_name}"
            case _:
                what = "单据" if spec.target in self.domain.bills else "实体"
                return f"业务包 {self.name} 的{what} {spec.target} 的 {spec.action} 工具"

    def _places(self) -> Mapping[tuple[str, str], Location]:
        """The place that declares each generated tool, by (kind, target)."""
        places: dict[tuple[str, str], Location] = {}
        for entity in self.domain.entities.values():
            if entity.pack == self.name:
                places["entity_op", entity.name] = Location(entity.file, OPERATIONS)
        for bill in self.domain.bills.values():
            if bill.pack == self.name:
                places["entity_op", bill.name] = Location(bill.file, OPERATIONS)
        for conversion in self.domain.conversions.values():
            if conversion.pack == self.name:
                places["push", conversion.id] = Location(conversion.file, CONVERSION_KEY)
        if self.exposure is not None:
            for index, query in enumerate(self.exposure.queries):
                item = f"{QUERIES}[{index}].{TOOL}"
                places.setdefault(("query", query.tool), Location(self.file, item))
        return places


# --- the name of one tool (requirement 11.2) ---------------------------------------------


def _check_name(tools: _PackTools, spec: ToolSpec, errors: StepErrors) -> None:
    """Check the exposed name of one tool: its form, its action and its prefix (req 11.2)."""
    pack = tools.name
    place = (tools.location(spec),)
    name = spec.name
    if _NAME.fullmatch(name) is None:
        errors.add(pack, place, f"工具名 {name} 不匹配 {NAME_PATTERN}")
    if spec.kind == "push":
        # A push tool ends with the short name of its source and not with an action of the
        # word list, so its whole name is checked against the generated form. That short
        # name also names its ``<short name>_id`` parameter (requirement 11.13), which is
        # why another short name, and not a rename, is how such a tool is named otherwise.
        if spec.renamed:
            reason = (
                f"下推工具名 {name} 不符合 <目标单据>_create_from_<来源简称> 形式，"
                f"应为 {spec.generated_name}"
            )
            errors.add(pack, place, reason)
    elif (found := tool_action(name)) == "":
        errors.add(pack, place, f"工具名 {name} 不以动作词表中的动作结尾")
    elif spec.renamed and found != spec.action:
        reason = (
            f"rename 把 {spec.generated_name} 改为 {name}，"
            f"动作从 {_action(spec.action)} 变为 {_action(found)}"
        )
        errors.add(pack, place, reason)
    if not any(name.startswith(prefix) for prefix in tools.prefixes):
        reason = f"工具名 {name} 不以业务包 {pack} 的 manifest 的 {TOOL_PREFIXES} 之一开头"
        errors.add(pack, (*place, Location(PACK_FILE, TOOL_PREFIXES)), reason)


def _action(action: str) -> str:
    """The action as an error names it; a name that ends with none performs no action."""
    return action or "无"


# --- exclude and rename (requirement 11.14) ----------------------------------------------


def _check_exposure(tools: _PackTools, errors: StepErrors) -> None:
    """``exclude`` and ``rename`` name generated tools, and name none of them twice (11.14)."""
    exposure = tools.exposure
    if exposure is None:
        return
    pack = tools.name
    generated = frozenset(tools.generated)
    excluded = frozenset(exposure.exclude)
    for index, name in enumerate(exposure.exclude):
        if name not in generated:
            reason = f"{EXCLUDE} 中的 {name} 不是业务包 {pack} 生成的工具名"
            errors.add_at(pack, tools.file, f"{EXCLUDE}[{index}]", reason)
    for name in exposure.rename:
        item = f"{RENAME}.{name}"
        if name not in generated:
            reason = f"{RENAME} 中的 {name} 不是业务包 {pack} 生成的工具名"
            errors.add_at(pack, tools.file, item, reason)
        if name in excluded:
            reason = f"工具 {name} 同时出现在 {EXCLUDE} 和 {RENAME} 中"
            errors.add_at(pack, tools.file, item, reason)


# --- one name per tool (requirement 2.7) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Decl:
    """One declaration that takes a tool name: how an error names it, and where it is.

    ``pack`` and ``location`` are None for a built-in tool, which no configuration writes
    and no file holds.
    """

    label: str
    pack: str | None = None
    location: Location | None = None


def _check_duplicates(generated: Sequence[_PackTools], errors: StepErrors) -> None:
    """No two tools share a name, the built-in tools included (requirement 2.7).

    A name two tools take is one error, not one per tool: it locates every place involved
    and names every party, the engine included, so one line carries the whole conflict.
    """
    declarations: list[tuple[str, _Decl]] = [
        (spec.name, _Decl(_BUILTIN_LABEL)) for spec in BUILTIN_TOOLS
    ]
    declarations += [
        (spec.name, _Decl(tools.label(spec), tools.name, tools.location(spec)))
        for tools in generated
        for spec in tools.tools
    ]
    found: dict[str, list[_Decl]] = {}
    for name, declaration in declarations:
        found.setdefault(name, []).append(declaration)
    for name, parties in found.items():
        if len(parties) > 1:
            labels = "、".join(party.label for party in parties)
            _conflict(parties, f"工具名 {name} 重复：{labels}", errors)


def _check_declared_queries(generated: Sequence[_PackTools], errors: StepErrors) -> None:
    """A ``queries`` item takes a tool name no other item takes (requirement 2.7).

    The domain model keys the queries by tool name, so a name two items declare arrives as
    one query and the generated tools cannot show the second one; the documents can, which
    is what this check reads.
    """
    declared: dict[str, list[_Decl]] = {}
    for tools in generated:
        if tools.exposure is None:
            continue
        for index, query in enumerate(tools.exposure.queries):
            label = f"业务包 {tools.name} 的 {QUERIES}[{index}]"
            place = Location(tools.file, f"{QUERIES}[{index}].{TOOL}")
            declared.setdefault(query.tool, []).append(_Decl(label, tools.name, place))
    for name, parties in declared.items():
        if len(parties) > 1:
            labels = "、".join(party.label for party in parties)
            _conflict(parties, f"查询工具名 {name} 被声明多次：{labels}", errors)


def _conflict(parties: Sequence[_Decl], reason: str, errors: StepErrors) -> None:
    """Record one conflict: ``reason`` at every place the parties to it are written.

    The error belongs to the first pack involved, so it is reported and sorted with that
    pack, and its reason names them all (requirement 2.2).
    """
    first = next((party.pack for party in parties if party.pack), None)
    places = [party.location for party in parties if party.location is not None]
    errors.add(first, places, reason)


# --- the tool names of a profile (requirement 2.10) --------------------------------------


def _check_profiles(
    tools: GeneratedTools, profiles: Sequence[RawDoc[ProfileDef]], errors: StepErrors
) -> None:
    """``include_tools`` and ``exclude_tools`` name tools the profile's packs generate (2.10).

    The tools of a profile are those of the packs it selects together with the built-in
    tools, which every profile exposes (requirement 11.1), so naming a built-in tool is no
    error while naming a tool of another pack, or one its pack's ``exclude`` dropped, is.
    """
    for raw in profiles:
        profile = raw.document
        known = {spec.name for spec in tools.of(profile.packs)}
        items = ((INCLUDE_TOOLS, profile.include_tools), (EXCLUDE_TOOLS, profile.exclude_tools))
        for item, names in items:
            for index, name in enumerate(names):
                if name not in known:
                    reason = (
                        f"profile {profile.profile} 的 {item} 含所选业务包没有生成的工具名 {name}"
                    )
                    errors.add_at(None, raw.file, f"{item}[{index}]", reason)
