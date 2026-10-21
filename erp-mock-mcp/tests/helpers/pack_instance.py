"""A temporary instance configuration over one business pack and the packs it depends on.

A business pack carries no instance configuration of its own, so nothing can be loaded from
a pack directory alone: the validation steps read an ``instance.yaml`` that references the
packs, a profile that selects them and at least one initial dataset
(:mod:`erp_mock.loader.loader`). This module writes that configuration into a temporary
directory and runs the steps on it, which is what the pack level tests of requirement 21.9
and the configuration tests of the example packs work against::

    instance = pack_instance(workdir, PACKS_DIR, ["purchase"])   # core comes with it
    instance.loaded.default_profile.tool_names                   # what the packs expose
    snapshot = instance.build(DatasetSpec("one_case", records={...}))
    with instance.sandbox(snapshot) as sandbox:
        outcome = sandbox.call("purchase_order_create", {...})
        sandbox.violations()                                     # INV-1, 2, 3 and 6

The caller passes the packs directory and the pack names, so the same helper serves
``packs/`` and the engine test fixture of :mod:`tests.helpers.fixtures`; the packs each
name in ``depends_on`` are added and every dependency is ordered before the pack that
declares it (:func:`pack_refs`), which is the order ``instance.yaml`` references them in,
the profile lists them in and the Shared_Data of the initial dataset is created in
(requirement 20.17). An empty packs directory simply has no pack to load, which is the
state ``packs/`` is in until task 19.2 writes the first one.

What the written configuration fixes, so that a pack is loaded under the same conditions
whatever it declares:

- the clock is in mode fixed at :data:`ANCHOR_DATE`, so every run of every step works on
  one date and nothing reads the system clock (requirements 16.3 and 25.10);
- the one profile is the semantic surface with the ``zh-detailed`` variant, which is what
  M0 serves (requirement 2.13), and selects every pack with no ``include_tools`` or
  ``exclude_tools``, so the tools of the instance are exactly the ones the packs expose;
- the base dataset :data:`BASE_DATASET` brings in the Shared_Data of every pack and
  declares no record of its own, so loading step 8 checks the current user and the default
  organization of that data alone (requirement 2.10);
- the current user is the first record of the user context entity
  (:func:`first_user_id`) unless the caller names one.

The documents are written as JSON, which is YAML 1.2 (:func:`write_document`): a dataset
record of a test case is read out of a YAML file and written back into one, so a value
keeps the type it was read as and nothing has to be re-quoted.

:class:`PackInstance` is what a test is given: the loaded configuration together with the
objects a call needs - the tools layer, the engine, the tables, the Seed_Builder and its
Snapshot cache - plus two things of its own:

- :meth:`PackInstance.build` builds the Snapshot of one :class:`DatasetSpec`, which is how
  a test case gets its ``setup`` into a database: the records go through the engine, a
  ``status`` runs the state operations that lead to it - an approval of a bill whose
  approve tool M0 does not expose included - and the builder checks INV-1, INV-2, INV-3 and
  INV-6 before it writes the files (requirements 20.2 and 20.9);
- :meth:`PackInstance.sandbox` opens a sandbox copied from such a Snapshot, which
  :class:`PackSandbox` performs tools/call against and reads the invariants off.

:attr:`PackInstance.described` is every description text of the profile, which a test
compares its own data against: no description may contain the name or the code of a record
of the Business_Data (requirement 3.5), so a test case whose ``setup`` writes a name that a
description uses as an example would break the cross-check of the instance it is loaded in.
:func:`pack_config` runs the steps up to 7 and leaves the texts there for that comparison,
and :meth:`PackConfig.finish` runs step 8 and the cross-check after it; the two together
are :func:`pack_instance`.
"""

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_pretty, encode_utf8
from erp_mock.config.instance import ALIAS_PATTERN, DatasetDef
from erp_mock.config.model import DomainModel
from erp_mock.config.pack import PackManifest
from erp_mock.domain.engine import Engine
from erp_mock.domain.invariants import InvariantViolation, check_invariants
from erp_mock.domain.numbering import format_id
from erp_mock.loader.errors import Step
from erp_mock.loader.instance import LoadedInstance
from erp_mock.loader.loader import STEPS, Loader
from erp_mock.loader.step1 import INSTANCE_FILE, PACK_FILE
from erp_mock.loader.step4 import DATASETS_DIR, DESCRIPTION_VARIANT, PROFILES_DIR
from erp_mock.loader.step8 import engine_of
from erp_mock.loader.yaml_io import load_yaml
from erp_mock.seed.builder import SeedBuilder, SnapshotRef
from erp_mock.seed.cache import SnapshotCache
from erp_mock.state.schema import StorageSchema
from erp_mock.state.snapshot import BASE_DB_FILENAME, copy_snapshot, link_snapshot
from erp_mock.state.store import DB_FILENAME, SandboxStore
from erp_mock.tools.descriptions import ascii_folded
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.layer import CallContext, ToolLayer, ToolOutcome

__all__ = [
    "ALIAS_REF",
    "ANCHOR_DATE",
    "BASE_DATASET",
    "CONFIG_STEPS",
    "INSTANCE_NAME",
    "PROFILE",
    "RANDOM_SEED",
    "SESSION_ID",
    "TIMEZONE",
    "VERSION",
    "DatasetSpec",
    "DescribedText",
    "OpenProbe",
    "PackConfig",
    "PackInstance",
    "PackRef",
    "PackSandbox",
    "PackSnapshot",
    "dataset_document",
    "described_texts",
    "first_user_id",
    "instance_document",
    "pack_config",
    "pack_instance",
    "pack_refs",
    "profile_document",
    "read_aliases",
    "substitute",
    "write_document",
]

# What the written configuration calls itself. The instance name and the dataset name reach
# the snapshot cache as directory names, so both are written like a configuration name
# (:data:`~erp_mock.config.instance.CONFIG_NAME_PATTERN`).
INSTANCE_NAME: Final = "pack-case"
PROFILE: Final = "semantic"
BASE_DATASET: Final = "pack-base"
VERSION: Final = "1.0.0"

# The clock of the written instance: mode fixed at this date, so the steps and every call
# work on one business date whatever the system clock says (requirements 16.3 and 25.10).
# It is the date of the engine test fixture, so a case may be read against either.
ANCHOR_DATE: Final = date(2026, 10, 9)
TIMEZONE: Final = "Asia/Shanghai"

# The seed of the one pseudo random sequence of a build (requirement 20.3). A test case
# writes the field values it is about and leaves the rest to the generators of the packs,
# which draw from this seed, so two runs of a case build the same records.
RANDOM_SEED: Final = 20261009

# The session every call of a sandbox arrives on; nothing of the pipeline depends on the
# value (:class:`~erp_mock.tools.layer.CallContext`).
SESSION_ID: Final = "se_7061636b63617365"

# The steps :func:`pack_config` runs: everything that reads documents, which is every step
# that needs no data directory (:data:`~tests.helpers.fixtures.IMPLEMENTED_STEPS`).
CONFIG_STEPS: Final[tuple[Step, ...]] = (1, 2, 3, 4, 5, 6, 7)

# How an argument of a step names a record of the ``setup``: ``$<kind>:<name>``, the alias
# the record was declared with (requirement 20.16). The alias is matched at the start of
# the value and whatever follows it is kept, so the line of a record is named
# ``$<alias>-<line no>`` the way the Numbering_Primitive writes a line ID
# (:func:`~erp_mock.domain.numbering.line_id`, requirement 4.2).
ALIAS_REF: Final = re.compile(r"\$(" + ALIAS_PATTERN.removeprefix("^").removesuffix("$") + ")")

# The reasons this module refuses to build a configuration, one per class.
_NO_PACK: Final = "{packs} 下没有业务包 {name}"
_PACK_CYCLE: Final = "业务包 {name} 的 depends_on 成环：{cycle}"
_NO_USER_ENTITY: Final = "实例配置没有用户上下文实体，无法确定当前用户，请显式传入 current_user"
_UNKNOWN_ALIAS: Final = "参数 {value} 引用的别名 {alias} 不在 setup 中：已有 {known}"
_BAD_REF: Final = "参数 {value} 以 $ 开头，但其后不是 `<kind>:<名称>` 形式的别名"


# --- the packs of one temporary instance -------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackRef:
    """One business pack of the written instance.yaml: its name, version and directory.

    ``version`` is the ``version`` of its own pack.yaml, which is what instance.yaml has to
    pin it to (requirement 2.3); ``requires`` are the packs it names in ``depends_on``,
    each of which is part of the instance as well (requirement 2.5).
    """

    name: str
    version: str
    directory: Path
    requires: tuple[str, ...] = ()


def pack_refs(packs_dir: Path, names: Sequence[str]) -> tuple[PackRef, ...]:
    """The packs ``names`` of ``packs_dir`` and their dependencies, dependencies first.

    Each name is read as a pack directory with a ``pack.yaml``
    (:class:`~erp_mock.config.pack.PackManifest`) and every pack of its ``depends_on`` is
    added, recursively; a pack is ordered after every pack it depends on and otherwise in
    the order it was reached, so the Shared_Data of a dependency is created before the
    records that reference it (requirement 20.17). Raises FileNotFoundError for a name that
    is no pack of ``packs_dir`` and ValueError for a cycle, which is what loading step 3
    would report (requirement 2.5).
    """
    found: dict[str, PackRef] = {}
    visiting: list[str] = []
    for name in names:
        _visit_pack(packs_dir, name, found, visiting)
    return tuple(found.values())


def _visit_pack(packs_dir: Path, name: str, found: dict[str, PackRef], visiting: list[str]) -> None:
    """Add the pack ``name`` to ``found``, its dependencies before it (:func:`pack_refs`)."""
    if name in found:
        return
    if name in visiting:
        cycle = " -> ".join([*visiting[visiting.index(name) :], name])
        raise ValueError(_PACK_CYCLE.format(name=name, cycle=cycle))
    manifest = _manifest(packs_dir, name)
    visiting.append(name)
    for required in manifest.depends_on:
        _visit_pack(packs_dir, required, found, visiting)
    visiting.pop()
    found[name] = PackRef(
        name=name,
        version=manifest.version,
        directory=packs_dir / name,
        requires=tuple(manifest.depends_on),
    )


def _manifest(packs_dir: Path, name: str) -> PackManifest:
    """The validated pack.yaml of the pack ``name`` of ``packs_dir``."""
    path = packs_dir / name / PACK_FILE
    if not path.is_file():
        raise FileNotFoundError(_NO_PACK.format(packs=packs_dir, name=name))
    return PackManifest.model_validate(load_yaml(path))


# --- the documents of one temporary instance ---------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """One initial dataset of the written instance, as a test declares it.

    ``name`` is the dataset name, which is also the name of its definition file and of its
    directory in the snapshot cache; ``records`` are the records of ``entities``, keyed by
    the alias each is declared with, and ``bulk`` the items of ``bulk``, both exactly as a
    dataset definition writes them (:class:`~erp_mock.config.instance.DatasetDef`). A test
    case passes the ``setup`` of the case as ``records``.

    ``closed_before`` is the ``periods.closed_before`` of the definition, None leaving it at
    the default, which closes every period before the month of the anchor date
    (requirement 20.1).
    """

    name: str
    records: Mapping[str, JsonValue] = FrozenMap()
    bulk: Sequence[JsonValue] = ()
    random_seed: int = RANDOM_SEED
    closed_before: str | None = None
    version: str = VERSION

    @property
    def file(self) -> str:
        """The definition file of the dataset, relative to the instance directory."""
        return f"{DATASETS_DIR}/{self.name}.yaml"


def instance_document(
    refs: Sequence[PackRef],
    datasets: Sequence[DatasetSpec],
    *,
    name: str = INSTANCE_NAME,
    anchor_date: date = ANCHOR_DATE,
    current_user: str | None = None,
) -> dict[str, JsonValue]:
    """The instance.yaml of the written configuration (business-pack.md section 8).

    ``refs`` are the packs, in the order :func:`pack_refs` put them, and ``datasets`` the
    initial datasets, the first of which is the default one. ``current_user`` is the current
    user of every build and every call, left out when it is None - which is what
    :func:`pack_config` writes before it knows the user context entity (module docstring).
    """
    names = [spec.name for spec in datasets]
    document: dict[str, JsonValue] = {
        "instance": name,
        "version": VERSION,
        "title": {"zh": "包级测试临时实例"},
        "packs": {ref.name: ref.version for ref in refs},
        "profiles": [PROFILE],
        "default_profile": PROFILE,
        "datasets": names,
        "default_dataset": names[0],
    }
    if current_user is not None:
        document["current_user"] = current_user
    document["clock"] = {
        "mode": "fixed",
        "anchor_date": anchor_date.isoformat(),
        "timezone": TIMEZONE,
    }
    return document


def profile_document(refs: Sequence[PackRef]) -> dict[str, JsonValue]:
    """The one profile of the written configuration (tool-design.md section 8).

    The semantic surface with the ``zh-detailed`` variant, which is what M0 serves
    (requirement 2.13); it selects every pack in the order of ``refs``, which is the order
    tools/list returns the tools in (requirement 11.9), and narrows nothing, so the profile
    exposes exactly the tools the packs expose (requirement 11.10).
    """
    return {
        "profile": PROFILE,
        "version": VERSION,
        "surface": "semantic",
        "description_variant": DESCRIPTION_VARIANT,
        "packs": [ref.name for ref in refs],
        "include_tools": [],
        "exclude_tools": [],
        "include_deprecated": False,
    }


def dataset_document(
    spec: DatasetSpec, refs: Sequence[PackRef], *, current_user: str | None = None
) -> dict[str, JsonValue]:
    """The definition of the dataset ``spec`` (seed-data.md section 2).

    ``include_shared`` is every pack of ``refs``, in their order, so the Shared_Data of a
    dependency is created before that of the pack that depends on it and before the records
    of ``entities`` (requirement 20.17); a pack that provides none contributes nothing.
    ``current_user`` overrides the one of instance.yaml, None leaving it to that file
    (requirement 20.5).
    """
    document: dict[str, JsonValue] = {
        "dataset": spec.name,
        "version": spec.version,
        "random_seed": spec.random_seed,
        "include_shared": [ref.name for ref in refs],
    }
    if current_user is not None:
        document["current_user"] = current_user
    if spec.closed_before is not None:
        document["periods"] = {"closed_before": spec.closed_before}
    if spec.records:
        document["entities"] = dict(spec.records)
    if spec.bulk:
        document["bulk"] = list(spec.bulk)
    return document


def write_document(path: Path, document: Mapping[str, JsonValue]) -> Path:
    """Write ``document`` to ``path`` as a configuration file; return the path.

    The text is indented JSON, which is YAML 1.2 and is read by the loader's own reader
    (:mod:`erp_mock.loader.yaml_io`): every key is quoted, so an alias such as
    ``supplier:main`` needs no escaping, and every value keeps the type it has here, so a
    decimal written as a string stays a string and a date expression stays text.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_utf8(dumps_pretty(document)))
    return path


def first_user_id(domain: DomainModel) -> str:
    """The internal ID of the first record of the user context entity (requirement 4.1).

    The current user a written configuration takes when the caller names none: the counters
    of a build start at 0 and the Shared_Data of the packs is created first, so the first
    record of that entity is the first user the dataset has (requirement 20.17). A pack that
    creates no such record in its Shared_Data needs an explicit ``current_user``, which
    loading step 8 would otherwise report as a user that is no record (requirement 2.10).
    Raises ValueError for an instance without a user context entity, which step 5 reports
    (requirement 1.14).
    """
    entity = domain.user_entity
    if entity is None:
        raise ValueError(_NO_USER_ENTITY)
    return format_id(entity.id_prefix, 1)


# --- the description texts of the profile (requirement 3.5) ------------------------------


@dataclass(frozen=True, slots=True)
class DescribedText:
    """One description of the profile: a tool description or a parameter description.

    ``tool`` is the exposed tool name, ``param`` the parameter path a description file
    names the parameter under (``lines[].qty``) and None for the tool description itself,
    and ``text`` the text as the variant of the profile writes it (requirement 3.7).
    """

    tool: str
    param: str | None
    text: str

    @property
    def about(self) -> str:
        """How an error names this description, as loading step 7 words it (req 3.2)."""
        if self.param is None:
            return f"工具 {self.tool} 的工具描述"
        return f"工具 {self.tool} 的参数 {self.param} 的描述"

    def contains(self, text: str) -> bool:
        """Whether ``text`` occurs in this description (the comparison of req 3.5).

        A substring, ASCII letters compared without regard to case
        (:func:`~erp_mock.tools.descriptions.ascii_folded`); an empty ``text`` occurs
        nowhere, being left out of the comparison.
        """
        return bool(text) and ascii_folded(text) in ascii_folded(self.text)


def described_texts(loaded: LoadedInstance) -> tuple[DescribedText, ...]:
    """Every description of the default profile, tools in output order (requirement 11.9).

    Per tool, its tool description and then the description of each of its parameters in
    declaration order (:attr:`~erp_mock.tools.generator.ToolSpec.param_paths`). A loaded
    instance has them all: whether a pack wrote one for every tool and every parameter was
    settled by loading step 7 (requirement 3.2).
    """
    found: list[DescribedText] = []
    for spec in loaded.default_profile.tools:
        found.append(DescribedText(spec.name, None, spec.description))
        schema = spec.input_schema
        for path in spec.param_paths:
            found.append(DescribedText(spec.name, path, _param_text(schema, path)))
    return tuple(found)


def _param_text(schema: Mapping[str, JsonValue], path: str) -> str:
    """The ``description`` the inputSchema holds for the parameter ``path``.

    The path is the one a description file writes, so a parameter of the lines element is
    looked up inside the ``items`` of the lines array (requirement 3.2). The empty text for
    a parameter the schema does not hold, which a loaded instance has none of.
    """
    name, _, inside = path.partition(".")
    if inside:
        lines = _object(_object(schema.get("properties")).get(name.removesuffix("[]")))
        return _param_text(_object(lines.get("items")), inside)
    param = _object(_object(schema.get("properties")).get(name))
    found = param.get("description")
    return found if isinstance(found, str) else ""


def _object(value: JsonValue) -> Mapping[str, JsonValue]:
    """``value`` as a schema object, an empty one when it is absent or not an object."""
    return value if isinstance(value, Mapping) else FrozenMap()


# --- one Snapshot of a written dataset ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackSnapshot:
    """The Snapshot of one dataset of the written instance, with its alias map.

    ``ref`` are the three files of the cache entry (design.md 6.2) and ``aliases`` the
    internal ID of each record of ``entities`` under the alias it was declared with, as
    ``aliases.json`` holds it (requirement 20.10): that map is what an argument of a step
    names a record through (:func:`substitute`).
    """

    name: str
    ref: SnapshotRef
    anchor_date: date
    aliases: FrozenMap[str, str] = FrozenMap()

    @property
    def db(self) -> Path:
        """The Snapshot file itself, which a sandbox is copied from (requirement 15.1)."""
        return self.ref.db

    def id(self, alias: str) -> str:
        """The internal ID of the record ``alias``; raises KeyError for an unknown one."""
        return self.aliases[alias]


def read_aliases(ref: SnapshotRef) -> FrozenMap[str, str]:
    """The alias map beside the Snapshot ``ref``, as ``aliases.json`` holds it (20.10).

    The file is read with the loader's own reader, JSON being YAML 1.2
    (:func:`~erp_mock.loader.yaml_io.load_yaml`); it holds the dataset the Snapshot is of
    and the aliases below the ``aliases`` key, sorted by code point.
    """
    document = load_yaml(ref.aliases)
    aliases = document.get("aliases") if isinstance(document, dict) else None
    items = aliases.items() if isinstance(aliases, dict) else ()
    return FrozenMap((str(alias), str(found)) for alias, found in items)


def substitute(value: JsonValue, aliases: Mapping[str, str]) -> JsonValue:
    """``value`` with every ``$<alias>`` replaced by the internal ID it stands for.

    The arguments of a step are walked as they are, objects and arrays included, and a
    string that starts with ``$`` has the alias right after it replaced by the internal ID
    of ``aliases``, keeping whatever follows: ``$supply_order:first`` is the bill and
    ``$supply_order:first-1`` the first line of it (:data:`ALIAS_REF`). Every other value
    is returned unchanged, a string that holds a ``$`` anywhere but at the start included.

    Raises KeyError for an alias the Snapshot does not hold and ValueError for a value that
    starts with ``$`` and names no alias at all, both of which are mistakes in the case.
    """
    if isinstance(value, Mapping):
        return {key: substitute(item, aliases) for key, item in value.items()}
    if isinstance(value, str):
        return _substitute_text(value, aliases)
    if isinstance(value, Sequence):
        return [substitute(item, aliases) for item in value]
    return value


def _substitute_text(value: str, aliases: Mapping[str, str]) -> str:
    """``value`` with a leading ``$<alias>`` replaced (:func:`substitute`)."""
    if not value.startswith("$"):
        return value
    found = ALIAS_REF.match(value)
    if found is None:
        raise ValueError(_BAD_REF.format(value=value))
    alias = found.group(1)
    if alias not in aliases:
        known = "、".join(sorted(aliases)) or "（无）"
        raise KeyError(_UNKNOWN_ALIAS.format(value=value, alias=alias, known=known))
    return aliases[alias] + value[found.end() :]


# --- the resource limits of a pack level sandbox (requirement 23.12) ---------------------


@dataclass(slots=True)
class OpenProbe:
    """A :class:`~erp_mock.tools.layer.LimitProbe` that is never over any bound.

    The three counts are reported as 0, so no call of a case is refused with
    LIMIT_EXCEEDED while the bounds of the layer stay the ones a plain process has: the
    three checks then run in the order and at the moment they do in production (design.md
    5.3 step 1) and none of them ever bites. ``passed`` counts the calls that got through
    the check, which is every call a sandbox made.
    """

    passed: int = 0

    def sandbox_calls(self) -> int:
        return 0

    def db_bytes(self) -> int:
        return 0

    def recent_session_calls(self) -> int:
        return 0

    def mark_passed(self) -> None:
        self.passed += 1


# --- one sandbox of one Snapshot ---------------------------------------------------------


@dataclass(slots=True)
class PackSandbox:
    """A sandbox copied from the Snapshot of a dataset, and the calls made in it.

    Built by :meth:`PackInstance.sandbox`, which copies the database and closes it again.
    Every call gets the next trace sequence number of the session, reads and failed calls
    included, which is what the gateway does with the events of a session
    (requirement 18.10).
    """

    instance: "PackInstance"
    snapshot: PackSnapshot
    store: SandboxStore
    probe: OpenProbe = field(default_factory=OpenProbe)
    seq: int = 0

    @property
    def aliases(self) -> FrozenMap[str, str]:
        """The internal ID of each record the Snapshot was built with (requirement 20.10)."""
        return self.snapshot.aliases

    def context(self) -> CallContext:
        """The context of the next call: the business date, the user and a fresh seq."""
        self.seq += 1
        return CallContext(
            session_id=SESSION_ID,
            anchor_date=self.snapshot.anchor_date,
            current_user_id=self.instance.current_user,
            call_seq=self.seq,
            limits=self.probe,
        )

    def call(self, tool: str, args: Mapping[str, JsonValue] | None = None) -> ToolOutcome:
        """Perform one tools/call of ``tool`` against the sandbox (requirement 12.1).

        ``args`` is the ``arguments`` of the request, parsed JSON and nothing else; it is
        passed as it is, so a caller that wants ``$<alias>`` replaced calls
        :meth:`resolve` first. The outcome is returned whatever it is: the tools layer
        raises nothing (requirement 12.8). Raises KeyError when the profile exposes no such
        tool, which is a mistake in the case.
        """
        spec = self.instance.spec(tool)
        return self.instance.layer.call(self.store, self.context(), spec, args)

    def resolve(self, value: JsonValue) -> JsonValue:
        """``value`` with every ``$<alias>`` replaced by its internal ID (:func:`substitute`)."""
        return substitute(value, self.aliases)

    def violations(self) -> list[InvariantViolation]:
        """Every place INV-1, INV-2, INV-3 or INV-6 does not hold (requirement 20.9).

        One read of the sandbox, in invariant order
        (:func:`~erp_mock.domain.invariants.check_invariants`); an empty list means all
        four hold, which is what a pack level case asserts after the setup and after every
        step (requirement 21.9).
        """
        with self.store.transaction() as uow:
            return check_invariants(uow, self.instance.domain)


# --- the written instance ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackInstance:
    """A temporary instance configuration over some business packs, loaded (design.md 2.4).

    - ``packs``: the packs of the instance, dependencies first (:func:`pack_refs`);
    - ``instance_dir``, ``packs_dir`` and ``data_dir``: the three directories the loader
      was given, the first one written by this module;
    - ``anchor_date`` and ``current_user``: the business date and the current user of every
      build and every call (requirements 16.2 and 1.8);
    - ``loader`` and ``loaded``: the loader that ran every step and the read-only
      configuration object it assembled (requirement 2.11);
    - ``layer``: the tools layer every call goes through, built with the expression trees
      step 5 parsed, so no rule text is parsed a second time (requirement 10.2);
    - ``cache``: the Snapshot cache :meth:`build` goes through, over ``data_dir``;
    - ``described``: every description text of the profile (:func:`described_texts`).
    """

    packs: tuple[PackRef, ...]
    instance_dir: Path
    packs_dir: Path
    data_dir: Path
    anchor_date: date
    current_user: str | None
    loader: Loader
    loaded: LoadedInstance
    layer: ToolLayer
    cache: SnapshotCache
    described: tuple[DescribedText, ...] = ()

    @property
    def pack_names(self) -> tuple[str, ...]:
        """The names of the packs, in the order instance.yaml references them."""
        return tuple(ref.name for ref in self.packs)

    @property
    def domain(self) -> DomainModel:
        """The resolved configuration of the instance (design.md 2.4)."""
        return self.loaded.domain

    @property
    def engine(self) -> Engine:
        """The engine every operation of a call and of a build runs through (design.md 4.1)."""
        return self.layer.engine

    @property
    def builder(self) -> SeedBuilder:
        """The Seed_Builder the Snapshot of a dataset is built through (design.md 6.1)."""
        return self.cache.builder

    @property
    def schema(self) -> StorageSchema:
        """The tables of the instance, which every Snapshot of it holds (design.md 3.1)."""
        return self.builder.schema

    @property
    def tools(self) -> FrozenMap[str, ToolSpec]:
        """The tools the profile exposes, by the name tools/list returns (req 11.9)."""
        return FrozenMap((spec.name, spec) for spec in self.loaded.default_profile.tools)

    def spec(self, tool: str) -> ToolSpec:
        """The tool ``tool`` of the profile; raises KeyError when it exposes no such tool."""
        return self.tools[tool]

    def build(self, spec: DatasetSpec, anchor_date: date | None = None) -> PackSnapshot:
        """Build the Snapshot of the dataset ``spec`` and return it with its alias map.

        The definition is written into the ``datasets/`` directory of the instance - which
        is the file the metadata of the Snapshot takes its digest over (requirement 20.12) -
        and built through the Snapshot cache, so the records go through the engine in the
        order of requirement 20.17 and the builder checks INV-1, INV-2, INV-3 and INV-6
        before it writes the files (requirement 20.9). ``anchor_date`` is the date the
        dataset is built under, None taking the one of the instance; a case that is about a
        date writes its own.

        The dataset is not one instance.yaml lists, so nothing of the loading reads it: it
        is built on top of the configuration that has already passed every step. Raises
        :class:`~erp_mock.seed.resolve.SeedError` for everything that can be wrong with the
        definition or with the data it produces (:mod:`erp_mock.seed.builder`).
        """
        anchor = self.anchor_date if anchor_date is None else anchor_date
        document = dataset_document(spec, self.packs, current_user=self.current_user)
        write_document(self.instance_dir / spec.file, document)
        definition = DatasetDef.model_validate(document)
        ref = self.cache.get_or_build(definition, anchor, file=spec.file)
        return PackSnapshot(name=spec.name, ref=ref, anchor_date=anchor, aliases=read_aliases(ref))

    @contextmanager
    def sandbox(self, snapshot: PackSnapshot) -> Iterator[PackSandbox]:
        """Yield a sandbox copied from ``snapshot``, closed and removed on the way out.

        What requirement 15.1 gives a new sandbox: ``db.sqlite`` copied from the Snapshot
        with the backup API and ``base.sqlite`` linked beside it, in a directory of its own
        below the data directory. The shared lock of the cache key is held while the copy
        is made, so a replacement in another process cannot rename the entry away under it
        (design.md 6.2).
        """
        with TemporaryDirectory(dir=self.data_dir, prefix="sandbox-") as made:
            sandbox_dir = Path(made)
            with self.cache.shared(snapshot.ref):
                copy_snapshot(snapshot.db, sandbox_dir / DB_FILENAME)
                link_snapshot(snapshot.db, sandbox_dir / BASE_DB_FILENAME)
            store = SandboxStore(sandbox_dir, self.schema)
            store.open()
            try:
                yield PackSandbox(instance=self, snapshot=snapshot, store=store)
            finally:
                store.close()


@dataclass(frozen=True, slots=True)
class PackConfig:
    """A written instance configuration whose document steps have passed (steps 1 to 7).

    What :func:`pack_config` leaves a caller: the configuration is resolved, the tools are
    generated and the descriptions are read, so ``described`` holds every description text
    of the profile, but no Snapshot has been taken up yet. A test compares its own data
    against those texts before :meth:`finish` runs step 8 and the cross-check, which is
    where a description that contains the name of a record would be reported as a
    configuration error (requirement 3.5).
    """

    packs: tuple[PackRef, ...]
    instance_dir: Path
    packs_dir: Path
    data_dir: Path
    anchor_date: date
    current_user: str
    loader: Loader
    loaded: LoadedInstance
    described: tuple[DescribedText, ...] = ()

    def finish(self) -> PackInstance:
        """Run step 8 and the cross-check, and return the loaded instance.

        Raises :class:`~erp_mock.loader.errors.ConfigErrors` at the first of the two that
        finds an error: the Snapshot of the base dataset does not satisfy the invariants,
        its current user is no record, it holds no single default organization, or a
        description contains a name, a code or a number of its records (requirements 2.10,
        2.14 and 3.5).
        """
        for step in STEPS[len(CONFIG_STEPS) :]:
            self.loader.run_step(step)
        loaded = self.loader.assemble()
        parsed = self.loader.parsed
        if parsed is None:
            raise AssertionError("第 5 步没有产出领域模型")
        builder = SeedBuilder(loaded, self.instance_dir, engine_of(parsed))
        return PackInstance(
            packs=self.packs,
            instance_dir=self.instance_dir,
            packs_dir=self.packs_dir,
            data_dir=self.data_dir,
            anchor_date=self.anchor_date,
            current_user=self.current_user,
            loader=self.loader,
            loaded=loaded,
            layer=ToolLayer(loaded, builder.engine),
            cache=SnapshotCache(self.data_dir, builder),
            described=self.described,
        )


def pack_config(
    workdir: Path,
    packs_dir: Path,
    packs: Sequence[str],
    *,
    anchor_date: date = ANCHOR_DATE,
    current_user: str | None = None,
    name: str = INSTANCE_NAME,
) -> PackConfig:
    """Write an instance configuration over ``packs`` below ``workdir`` and validate it.

    ``packs`` are pack names of ``packs_dir``, whose dependencies are added
    (:func:`pack_refs`); ``workdir`` is a directory of a test's own, which gets an
    ``instance/`` and a ``data/`` below it. ``anchor_date`` is the date the clock is fixed
    to and ``current_user`` the current user, None taking the first record of the user
    context entity (:func:`first_user_id`); ``name`` is the instance name.

    The steps of :data:`CONFIG_STEPS` are run, so everything the documents settle has
    passed and the descriptions are there (:class:`PackConfig`). The instance.yaml is
    written twice when the current user is derived: once without one, which is all the
    document steps need, and once with the user the resolved configuration gives, which
    step 8 reads. Raises :class:`~erp_mock.loader.errors.ConfigErrors` at the first step
    that finds an error.
    """
    refs = pack_refs(packs_dir, packs)
    instance_dir = workdir / "instance"
    data_dir = workdir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    base = DatasetSpec(BASE_DATASET)
    written = _Written(instance_dir, packs_dir, data_dir, refs, base, anchor_date, name)
    loader, loaded = written.validate(current_user)
    user = current_user
    if user is None:
        # The current user follows from the configuration the steps resolved, so the two
        # documents that name one are written again and the steps run a second time; they
        # read documents alone, which is a matter of milliseconds (module docstring).
        user = first_user_id(loaded.domain)
        loader, loaded = written.validate(user)
    return PackConfig(
        packs=refs,
        instance_dir=instance_dir,
        packs_dir=packs_dir,
        data_dir=data_dir,
        anchor_date=anchor_date,
        current_user=user,
        loader=loader,
        loaded=loaded,
        described=described_texts(loaded),
    )


@dataclass(frozen=True, slots=True)
class _Written:
    """The documents of one temporary instance, which :func:`pack_config` writes twice."""

    instance_dir: Path
    packs_dir: Path
    data_dir: Path
    refs: tuple[PackRef, ...]
    base: DatasetSpec
    anchor_date: date
    name: str

    def validate(self, current_user: str | None) -> tuple[Loader, LoadedInstance]:
        """Write the three documents with ``current_user`` and run :data:`CONFIG_STEPS`."""
        profile = profile_document(self.refs)
        write_document(self.instance_dir / f"{PROFILES_DIR}/{PROFILE}.yaml", profile)
        dataset = dataset_document(self.base, self.refs, current_user=current_user)
        write_document(self.instance_dir / self.base.file, dataset)
        document = instance_document(
            self.refs,
            [self.base],
            name=self.name,
            anchor_date=self.anchor_date,
            current_user=current_user,
        )
        write_document(self.instance_dir / INSTANCE_FILE, document)
        loader = Loader(self.instance_dir, self.packs_dir, self.anchor_date, self.data_dir)
        for step in CONFIG_STEPS:
            loader.run_step(step)
        return loader, loader.assemble()


def pack_instance(
    workdir: Path,
    packs_dir: Path,
    packs: Sequence[str],
    *,
    anchor_date: date = ANCHOR_DATE,
    current_user: str | None = None,
    name: str = INSTANCE_NAME,
) -> PackInstance:
    """Write an instance configuration over ``packs`` and run every validation step on it.

    :func:`pack_config` followed by :meth:`PackConfig.finish`, which is what a test that
    only wants the loaded instance calls; the arguments are those of the former. Raises
    :class:`~erp_mock.loader.errors.ConfigErrors` at the first step that finds an error, so
    the instance that comes back passed all of them (requirement 2.11).
    """
    return pack_config(
        workdir,
        packs_dir,
        packs,
        anchor_date=anchor_date,
        current_user=current_user,
        name=name,
    ).finish()
