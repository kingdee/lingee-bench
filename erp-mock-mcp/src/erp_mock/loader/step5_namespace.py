"""Step 5, part two: namespaces, prefixes, rule numbers, primitives and the instance.

The part of the fifth step that reads the packs of the instance side by side instead of
following a reference (``loader.step5_model``). What it settles:

- the names of the records (requirement 2.7): an entity name, a bill name, the name of a
  bill's lines (``<bill>_line``) and the two names the engine keeps itself
  (:data:`~erp_mock.config.model.ENGINE_RECORDS`) each name one table of a sandbox and one
  ``entity`` of a diff (design.md data model 3 and 5), so each of them belongs to one
  declaration alone;
- the prefixes (requirement 2.7): an ID prefix belongs to one entity or bill and a number
  prefix to one bill, since a prefix is what makes a generated ID or bill number tell its
  object (requirements 4.1 and 4.4); a prefix of either kind and a ``rule_prefix`` belong
  to one manifest, where a pack states the namespaces it takes, and an entity or a bill may
  only use a prefix its own manifest states (business-pack.md section 4); and no tool
  prefix of two packs may be a prefix of the other, equal included, every tool name
  beginning with one of them (requirement 11.2);
- the rule numbers (requirement 2.8): ``GEN`` numbers the engine's own rules (R-GEN-001 to
  R-GEN-007, meta-model.md section 9), so no pack takes it as its ``rule_prefix``; a pack
  rule is numbered ``R-<that prefix>-<001 to 999>``, and each number is declared once;
- the primitives (requirement 2.6): every primitive a configuration uses is declared by the
  ``primitives`` of its own manifest. Which configuration uses which primitive is what
  :func:`_primitive_uses` lists, item by item as requirement 2.6 states it; asking for a
  query a primitive provides is one such use, so :data:`PRIMITIVE_QUERIES` also says which
  queries there are to ask for (the stock balance of inventory, requirement 9.7);
- the context entities (requirement 1.14): the instance declares exactly one organization
  and exactly one user context entity, the records the default organization and the current
  user are taken from (requirements 1.8 and 16.10);
- the instance configuration (requirement 2.10): ``default_profile`` and
  ``default_dataset`` name a profile and a dataset instance.yaml lists, and the ``packs`` of
  a profile and the ``include_shared`` of a dataset name packs it references.

A conflict is one error, not one per party to it: a name, a prefix or a rule number that
two declarations share gives a single error that locates every one of them and names every
pack involved (requirement 2.2), so one line carries the whole conflict. For the same
reason the checks read the validated documents (:class:`~erp_mock.loader.step4.RawPack`)
instead of the domain model, which keeps the first declaration of a repeated name and so
could not show the second.

Every check runs: a conflict does not stop the ones after it, and none of them reads a
reference ``loader.step5_model`` resolved, so both parts of the step collect their errors
into the one collector of step 5 and one run reports them together.
"""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from erp_mock.config.instance import DatasetDef, InstanceDef, ProfileDef
from erp_mock.config.model import ENGINE_RECORDS, LINE_SUFFIX
from erp_mock.config.pack import (
    EnforcedRuleDef,
    PackManifest,
    Primitive,
    PrimitiveQueryDef,
)
from erp_mock.loader.errors import Location, StepErrors
from erp_mock.loader.step1 import INSTANCE_FILE, PACK_FILE
from erp_mock.loader.step4 import (
    BILL_KEY,
    CONVERSION_KEY,
    DATASETS_DIR,
    ENTITY_KEY,
    LINKAGE_KEY,
    PROFILES_DIR,
    RULE_KEY,
    WRITEBACK_KEY,
    RawDoc,
    RawPack,
)

__all__ = ["ENGINE_RULE_PREFIX", "PRIMITIVE_QUERIES", "check"]

# The manifest items this step checks (business-pack.md section 4).
ID_PREFIXES: Final = "id_prefixes"
NUMBER_PREFIXES: Final = "number_prefixes"
TOOL_PREFIXES: Final = "tool_prefixes"
RULE_PREFIX: Final = "rule_prefix"
PRIMITIVES: Final = "primitives"

# The items of an entity, a bill, a rule and an instance configuration it reports at;
# ``profiles`` and ``datasets`` are the keys of instance.yaml that list the definitions,
# which are also the directories holding them (:mod:`erp_mock.loader.step4`).
ID_PREFIX: Final = "id_prefix"
NUMBER_PREFIX: Final = "number.prefix"
CONTEXT_ROLE: Final = "context.role"
RULE_ID: Final = f"{RULE_KEY}.id"
PACKS: Final = "packs"
DEFAULT_PROFILE: Final = "default_profile"
DEFAULT_DATASET: Final = "default_dataset"
INCLUDE_SHARED: Final = "include_shared"

# The rule prefix of the engine's own rules, which a pack must not take (requirement 2.8).
ENGINE_RULE_PREFIX: Final = "GEN"

# A rule number: ``R-<rule_prefix>-<sequence>``. The prefix is matched as a prefix of its
# own grammar (:data:`~erp_mock.config.pack.RulePrefix`) and compared with the declaring
# pack's, and the sequence is 3 digits, so 001 to 999 is the same as "not 000".
_RULE_NUMBER: Final = re.compile(r"R-([A-Z][A-Z0-9]*)-([0-9]{3})")
_NO_SEQUENCE: Final = "000"

# The queries each primitive provides, which a ``queries`` item of an exposure asks for by
# primitive and name: the stock balance of a material in a warehouse, which the inventory
# primitive answers from its own records (requirement 9.7). Asking for one is a use of that
# primitive (requirement 2.6).
PRIMITIVE_QUERIES: Final[Mapping[Primitive, tuple[str, ...]]] = {"inventory": ("balance",)}

# The two context roles and how an error names each of them (requirement 1.14).
_CONTEXT_ROLES: Final[tuple[Literal["org", "user"], ...]] = ("org", "user")
_ROLE_NAMES: Final[Mapping[str, str]] = {"org": "组织上下文实体", "user": "用户上下文实体"}

# How an error names the two kinds of prefix (requirement 2.7).
_ID_KIND: Final = "ID 前缀"
_NUMBER_KIND: Final = "编号前缀"


def check(
    packs: Sequence[RawPack],
    instance: InstanceDef,
    profiles: Sequence[RawDoc[ProfileDef]],
    datasets: Sequence[RawDoc[DatasetDef]],
    errors: StepErrors,
) -> None:
    """Run this part of step 5, recording what it finds.

    ``packs`` are the packs instance.yaml references, in its order, with the documents
    step 4 validated; ``instance`` is that instance.yaml and ``profiles`` and ``datasets``
    are the definitions step 4 read for the names it lists. This part of the step passed
    when ``errors`` stayed empty.
    """
    _check_record_names(packs, errors)
    _check_prefixes(packs, errors)
    _check_tool_prefixes(packs, errors)
    _check_rules(packs, errors)
    _check_primitives(packs, errors)
    _check_context_entities(packs, errors)
    _check_instance(instance, profiles, datasets, errors)


# --- declarations that can conflict ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Decl:
    """One declaration that takes a name, a prefix or a rule number.

    ``label`` is how an error names it among the parties to a conflict, such as
    ``业务包 fx_base 的实体``. ``pack`` and ``location`` are None for a name the engine
    keeps itself, which no configuration writes and no file holds.
    """

    label: str
    pack: str | None = None
    location: Location | None = None


def _grouped[T](declarations: Iterable[tuple[str, T]]) -> dict[str, list[T]]:
    """``(name, declaration)`` pairs grouped by name, both orders as they came in."""
    found: dict[str, list[T]] = {}
    for name, declaration in declarations:
        found.setdefault(name, []).append(declaration)
    return found


def _labels(found: Sequence[_Decl]) -> str:
    """The labels of the parties to a conflict, in declaration order."""
    return "、".join(declaration.label for declaration in found)


def _pack_names(found: Sequence[_Decl]) -> str:
    """The packs the parties to a conflict belong to, each named once, in order."""
    names = list(dict.fromkeys(declaration.pack for declaration in found if declaration.pack))
    return "、".join(names)


def _conflict(found: Sequence[_Decl], reason: str, errors: StepErrors) -> None:
    """Record one conflict: ``reason`` at every place the parties to it are written.

    The error belongs to the first pack involved, so it is reported and sorted with that
    pack, and its reason names them all (requirement 2.2).
    """
    first = next((declaration.pack for declaration in found if declaration.pack), None)
    places = [declaration.location for declaration in found if declaration.location is not None]
    errors.add(first, places, reason)


def _distinct(prefixes: Sequence[str]) -> Iterator[tuple[int, str]]:
    """The prefixes of one manifest, each at the index of its first entry.

    A manifest that writes the same prefix twice takes one namespace and not two, so the
    repeated entry is no conflict with itself.
    """
    seen: set[str] = set()
    for index, prefix in enumerate(prefixes):
        if prefix not in seen:
            seen.add(prefix)
            yield index, prefix


# --- the names of the records (requirement 2.7) ------------------------------------------


def _record_names(packs: Sequence[RawPack]) -> Iterator[tuple[str, _Decl]]:
    """Every name of the record namespace of the instance, with the declaration it is from.

    The engine's own names come first, so a pack that takes one of them is reported as the
    second party to the conflict; the packs follow in the order of instance.yaml and, inside
    a pack, in file order, which is the order of the model as well (``loader.step5_model``).
    """
    for name in ENGINE_RECORDS:
        yield name, _Decl("引擎内部表")
    for pack in packs:
        for entity in pack.entities:
            place = Location(entity.file, ENTITY_KEY)
            yield entity.document.entity, _Decl(f"业务包 {pack.name} 的实体", pack.name, place)
        for bill in pack.bills:
            name = bill.document.bill
            place = Location(bill.file, BILL_KEY)
            yield name, _Decl(f"业务包 {pack.name} 的单据", pack.name, place)
            lines = f"业务包 {pack.name} 的单据 {name} 的分录"
            yield f"{name}{LINE_SUFFIX}", _Decl(lines, pack.name, place)


def _check_record_names(packs: Sequence[RawPack], errors: StepErrors) -> None:
    """No two declarations may take the same record name (requirement 2.7)."""
    for name, found in _grouped(_record_names(packs)).items():
        if len(found) > 1:
            _conflict(found, f"名称 {name} 重名：{_labels(found)}", errors)


# --- the ID and number prefixes (requirement 2.7) ----------------------------------------


@dataclass(frozen=True, slots=True)
class _Use:
    """One prefix an entity or a bill uses, and whether its own manifest states it."""

    prefix: str
    pack: str
    location: Location
    label: str
    stated: bool

    @property
    def declaration(self) -> _Decl:
        """The use as a party to a conflict with another use of the same prefix."""
        return _Decl(self.label, self.pack, self.location)


def _use(
    pack: RawPack, file: str, item: str, prefix: str, what: str, stated: Iterable[str]
) -> _Use:
    """One use of ``prefix`` at ``item`` of ``file``; ``stated`` is the manifest's list."""
    return _Use(
        prefix=prefix,
        pack=pack.name,
        location=Location(file, item),
        label=f"业务包 {pack.name} 的{what}",
        stated=prefix in stated,
    )


def _id_uses(packs: Sequence[RawPack]) -> Iterator[_Use]:
    """The ID prefix of every entity and every bill (requirement 4.1)."""
    for pack in packs:
        stated = frozenset(pack.manifest.id_prefixes)
        for entity in pack.entities:
            name = entity.document.entity
            yield _use(
                pack, entity.file, ID_PREFIX, entity.document.id_prefix, f"实体 {name}", stated
            )
        for bill in pack.bills:
            name = bill.document.bill
            yield _use(pack, bill.file, ID_PREFIX, bill.document.id_prefix, f"单据 {name}", stated)


def _number_uses(packs: Sequence[RawPack]) -> Iterator[_Use]:
    """The number prefix of every bill (requirement 4.4)."""
    for pack in packs:
        stated = frozenset(pack.manifest.number_prefixes)
        for bill in pack.bills:
            name = bill.document.bill
            prefix = bill.document.number.prefix
            yield _use(pack, bill.file, NUMBER_PREFIX, prefix, f"单据 {name}", stated)


def _check_prefixes(packs: Sequence[RawPack], errors: StepErrors) -> None:
    """The prefixes of the entities, the bills and the manifests (requirement 2.7)."""
    _check_uses(tuple(_id_uses(packs)), _ID_KIND, "实体或单据", ID_PREFIXES, errors)
    _check_uses(tuple(_number_uses(packs)), _NUMBER_KIND, "单据", NUMBER_PREFIXES, errors)
    _check_manifest_prefixes(
        packs, _ID_KIND, ID_PREFIXES, lambda manifest: manifest.id_prefixes, errors
    )
    _check_manifest_prefixes(
        packs, _NUMBER_KIND, NUMBER_PREFIXES, lambda manifest: manifest.number_prefixes, errors
    )


def _check_uses(uses: Sequence[_Use], kind: str, users: str, item: str, errors: StepErrors) -> None:
    """One prefix kind of the entities and bills: shared prefixes, and prefixes not stated.

    ``kind`` names the prefix kind, ``users`` what may use it and ``item`` the manifest list
    that has to state it.
    """
    pairs = ((use.prefix, use.declaration) for use in uses)
    for prefix, found in _grouped(pairs).items():
        if len(found) > 1:
            _conflict(found, f"{kind} {prefix} 被多个{users}使用：{_labels(found)}", errors)
    for use in uses:
        if not use.stated:
            reason = f"{kind} {use.prefix} 没有列在业务包 {use.pack} 的 manifest 的 {item} 中"
            errors.add(use.pack, (use.location,), reason)


def _check_manifest_prefixes(
    packs: Sequence[RawPack],
    kind: str,
    item: str,
    stated: Callable[[PackManifest], Sequence[str]],
    errors: StepErrors,
) -> None:
    """One prefix kind of the manifests: a prefix two of them state is a conflict (req 2.7)."""
    declarations = [
        (prefix, _Decl(pack.name, pack.name, Location(PACK_FILE, f"{item}[{index}]")))
        for pack in packs
        for index, prefix in _distinct(stated(pack.manifest))
    ]
    for prefix, found in _grouped(declarations).items():
        if len(found) > 1:
            reason = f"{kind} {prefix} 同时出现在业务包 {_labels(found)} 的 manifest 中"
            _conflict(found, reason, errors)


def _check_tool_prefixes(packs: Sequence[RawPack], errors: StepErrors) -> None:
    """No tool prefix of two packs may be a prefix of the other, equal included (req 2.7).

    Two such prefixes would leave the pack a tool name belongs to undecided, which is what
    the prefix of a tool name says (requirement 11.2). Prefixes of one pack are left alone:
    a pack decides for itself how it divides its own tools.
    """
    stated = [
        (pack.name, index, prefix)
        for pack in packs
        for index, prefix in _distinct(pack.manifest.tool_prefixes)
    ]
    for position, (pack, index, prefix) in enumerate(stated):
        for other, other_index, other_prefix in stated[position + 1 :]:
            if pack == other or not _overlapping(prefix, other_prefix):
                continue
            places = (
                Location(PACK_FILE, f"{TOOL_PREFIXES}[{index}]"),
                Location(PACK_FILE, f"{TOOL_PREFIXES}[{other_index}]"),
            )
            reason = (
                f"工具名前缀 {prefix} 与 {other_prefix} 中一个是另一个的前缀，"
                f"涉及业务包 {pack}、{other}"
            )
            errors.add(pack, places, reason)


def _overlapping(prefix: str, other: str) -> bool:
    """Whether one of the two is a prefix of the other, two equal prefixes included."""
    return prefix.startswith(other) or other.startswith(prefix)


# --- the rule prefix and the rule numbers (requirement 2.8) ------------------------------


def _check_rules(packs: Sequence[RawPack], errors: StepErrors) -> None:
    """The ``rule_prefix`` of each manifest and the number of each pack rule (req 2.8)."""
    _check_rule_prefixes(packs, errors)
    declarations: list[tuple[str, _Decl]] = []
    for pack in packs:
        prefix = pack.manifest.rule_prefix
        for rule in pack.rules:
            number = rule.document.id
            place = Location(rule.file, RULE_ID)
            if not _numbered(number, prefix):
                reason = (
                    f"规则编号 {number} 不是 R-{prefix}-<序号> 形式"
                    "（序号为 001 到 999 的 3 位数字）"
                )
                errors.add(pack.name, (place,), reason)
            declarations.append((number, _Decl(pack.name, pack.name, place)))
    for number, found in _grouped(declarations).items():
        if len(found) > 1:
            reason = f"规则编号 {number} 被声明多次，涉及业务包 {_pack_names(found)}"
            _conflict(found, reason, errors)


def _check_rule_prefixes(packs: Sequence[RawPack], errors: StepErrors) -> None:
    """``GEN`` is the engine's, and no two manifests state the same prefix (req 2.7, 2.8)."""
    declarations: list[tuple[str, _Decl]] = []
    for pack in packs:
        prefix = pack.manifest.rule_prefix
        place = Location(PACK_FILE, RULE_PREFIX)
        if prefix == ENGINE_RULE_PREFIX:
            reason = f"{RULE_PREFIX} 不能为 {ENGINE_RULE_PREFIX}，它保留给引擎级规则"
            errors.add(pack.name, (place,), reason)
        declarations.append((prefix, _Decl(pack.name, pack.name, place)))
    for prefix, found in _grouped(declarations).items():
        if len(found) > 1:
            reason = f"{RULE_PREFIX} {prefix} 同时出现在业务包 {_labels(found)} 的 manifest 中"
            _conflict(found, reason, errors)


def _numbered(number: str, prefix: str) -> bool:
    """Whether ``number`` is ``R-<prefix>-<001 to 999>`` (requirement 2.8)."""
    found = _RULE_NUMBER.fullmatch(number)
    return found is not None and found[1] == prefix and found[2] != _NO_SEQUENCE


# --- the primitives a configuration uses (requirement 2.6) -------------------------------


def _primitive_uses(pack: RawPack) -> Iterator[tuple[Primitive, Location]]:
    """Every primitive the configuration of ``pack`` uses, with the item that uses it.

    The list of requirement 2.6, in the order of the documents: an entity and a bill use
    crud and numbering, a bill also state_machine and, with ``pricing``, pricing; quantity
    semantics and the declaration of a base unit with its conversion records use
    unit_conversion; a conversion and a writeback use the primitive of their own name; a
    linkage uses the primitive it names; an enforced rule and a declarative query with a
    filter use rule_expr; and a ``queries`` item that asks a primitive for one of its
    queries uses that primitive.
    """
    for entity in pack.entities:
        place = Location(entity.file, ENTITY_KEY)
        yield "crud", place
        yield "numbering", place
        if entity.document.unit_conversion is not None:
            yield "unit_conversion", Location(entity.file, "unit_conversion")
    for bill in pack.bills:
        place = Location(bill.file, BILL_KEY)
        yield "crud", place
        yield "numbering", place
        yield "state_machine", Location(bill.file, "state_machine")
        if bill.document.pricing:
            yield "pricing", Location(bill.file, "pricing")
        if bill.document.line_quantity is not None:
            yield "unit_conversion", Location(bill.file, "line_quantity")
    for conversion in pack.conversions:
        yield "conversion", Location(conversion.file, CONVERSION_KEY)
    for writeback in pack.writebacks:
        yield "writeback", Location(writeback.file, WRITEBACK_KEY)
    for linkage in pack.linkages:
        place = Location(linkage.file, f"{LINKAGE_KEY}.primitive")
        yield linkage.document.primitive, place
    for rule in pack.rules:
        if isinstance(rule.document, EnforcedRuleDef):
            yield "rule_expr", Location(rule.file, f"{RULE_KEY}.check")
    exposure = pack.exposure
    if exposure is None:
        return
    for index, query in enumerate(exposure.document.queries):
        item = f"queries[{index}]"
        if isinstance(query, PrimitiveQueryDef):
            yield query.primitive, Location(exposure.file, f"{item}.primitive")
        elif query.filter is not None:
            yield "rule_expr", Location(exposure.file, f"{item}.filter")


def _check_primitives(packs: Sequence[RawPack], errors: StepErrors) -> None:
    """Each pack declares the primitives its own configuration uses (requirement 2.6)."""
    for pack in packs:
        _check_pack_primitives(pack, errors)
        _check_primitive_queries(pack, errors)


def _check_pack_primitives(pack: RawPack, errors: StepErrors) -> None:
    """Every primitive ``pack`` uses is declared by its own manifest (requirement 2.6).

    One error per primitive that is missing, locating every item of the pack that uses it,
    so a manifest that forgot a primitive is one error and not one per document.
    """
    declared = frozenset(pack.manifest.primitives)
    missing: dict[Primitive, list[Location]] = {}
    for primitive, place in _primitive_uses(pack):
        if primitive not in declared:
            missing.setdefault(primitive, []).append(place)
    for primitive, places in missing.items():
        reason = (
            f"配置使用了原语 {primitive}，业务包 {pack.name} 的 manifest 的 {PRIMITIVES} 没有声明它"
        )
        errors.add(pack.name, places, reason)


def _check_primitive_queries(pack: RawPack, errors: StepErrors) -> None:
    """A ``queries`` item may only ask for a query its primitive provides (req 2.6, 9.7)."""
    exposure = pack.exposure
    if exposure is None:
        return
    for index, query in enumerate(exposure.document.queries):
        if not isinstance(query, PrimitiveQueryDef):
            continue
        provided = PRIMITIVE_QUERIES.get(query.primitive, ())
        if query.query not in provided:
            reason = f"原语 {query.primitive} 不提供查询 {query.query}"
            errors.add_at(pack.name, exposure.file, f"queries[{index}].query", reason)


# --- the context entities (requirement 1.14) ---------------------------------------------


def _check_context_entities(packs: Sequence[RawPack], errors: StepErrors) -> None:
    """Exactly one organization and one user context entity (requirements 1.8 and 1.14).

    More than one is reported at the ``context`` of each entity that declares the role,
    naming every pack and entity involved; none is an error of the instance configuration,
    whose packs leave the engine without a default organization or a current user.
    """
    for role in _CONTEXT_ROLES:
        found = [
            (pack, entity)
            for pack in packs
            for entity in pack.entities
            if entity.document.context is not None and entity.document.context.role == role
        ]
        if len(found) == 1:
            continue
        name = _ROLE_NAMES[role]
        if not found:
            errors.add_at(None, INSTANCE_FILE, PACKS, f"引用的业务包都没有声明{name}")
            continue
        declarations = [
            _Decl(
                f"业务包 {pack.name} 的实体 {entity.document.entity}",
                pack.name,
                Location(entity.file, CONTEXT_ROLE),
            )
            for pack, entity in found
        ]
        _conflict(declarations, f"声明为{name}的实体不止一个：{_labels(declarations)}", errors)


# --- the instance configuration (requirement 2.10) ---------------------------------------


def _check_instance(
    instance: InstanceDef,
    profiles: Sequence[RawDoc[ProfileDef]],
    datasets: Sequence[RawDoc[DatasetDef]],
    errors: StepErrors,
) -> None:
    """The references of instance.yaml, of the profiles and of the datasets (req 2.10)."""
    if instance.default_profile not in instance.profiles:
        reason = f"{DEFAULT_PROFILE} {instance.default_profile} 不在 {PROFILES_DIR} 中"
        errors.add_at(None, INSTANCE_FILE, DEFAULT_PROFILE, reason)
    if instance.default_dataset not in instance.datasets:
        reason = f"{DEFAULT_DATASET} {instance.default_dataset} 不在 {DATASETS_DIR} 中"
        errors.add_at(None, INSTANCE_FILE, DEFAULT_DATASET, reason)
    referenced = frozenset(instance.packs)
    for profile in profiles:
        name = profile.document.profile
        for index, pack in enumerate(profile.document.packs):
            if pack not in referenced:
                reason = f"profile {name} 的 {PACKS} 含 instance.yaml 未引用的业务包 {pack}"
                errors.add_at(None, profile.file, f"{PACKS}[{index}]", reason)
    for dataset in datasets:
        name = dataset.document.dataset
        for index, pack in enumerate(dataset.document.include_shared):
            if pack not in referenced:
                reason = (
                    f"初始数据集 {name} 的 {INCLUDE_SHARED} 含 instance.yaml 未引用的业务包 {pack}"
                )
                errors.add_at(None, dataset.file, f"{INCLUDE_SHARED}[{index}]", reason)
