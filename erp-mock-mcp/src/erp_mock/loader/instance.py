"""The read-only configuration object the Loader produces (design.md 2.4, req 2.11).

Each validation step leaves its own result on the :class:`~erp_mock.loader.loader.Loader`:
the instance configuration, the documents of every pack, the resolved domain model, the
generated tools, the descriptions. This module puts those results together into the one
object the rest of the service reads, :class:`LoadedInstance`::

    loaded = Loader(instance_dir, packs_dir).load()
    loaded.versions.framework                       # the engine's own version
    loaded.domain.bills["supply_order"]             # the resolved configuration
    loaded.default_profile.payload                  # the tools/list payload of a session

Everything in that object is read-only (requirement 2.11): every class here is a frozen
dataclass with ``slots``, every collection is a tuple or a
:class:`~erp_mock.common.frozen.FrozenMap`, and the objects they hold are the frozen
pydantic models of :mod:`erp_mock.config` and the frozen dataclasses of
:mod:`erp_mock.config.model` and :mod:`erp_mock.tools.generator`. An attempt to assign to
an attribute, to a mapping key or to insert, delete or append therefore raises and leaves
the content unchanged, which the property test of requirement 2.11 asserts over the whole
object tree.

:class:`ProfileModel` is the part a session works against: the tools of one profile, in the
output order of requirement 11.9, and the tools/list payload assembled from them. That
payload is assembled once per profile, here at load time, by building the SDK models
(``mcp.types.Tool`` and ``mcp.types.ListToolsResult``) and serializing them the way the SDK
sends a result (``model_dump(by_alias=True, mode="json", exclude_none=True)``, probed in
tests/probes/test_sdk_server.py), so the payload carries the SDK's own key names and key
order and cannot drift from what a client receives. It depends on the configuration and the
profile alone - not on the anchor date, the dataset, the session or the working directory
the service was started in - so two runs serialize it byte for byte the same
(requirement 11.10).

What is kept is that serialized payload as a frozen tree, and not the SDK model itself: a
pydantic model holds its ``tools`` in a plain list and its schemas in plain dicts, which no
configuration object may do (requirement 2.11), and neither can be frozen from the outside.
:meth:`ProfileModel.tools_list_result` builds the SDK result back from the cached payload
for the handler that answers tools/list (task 22.1), which is free to hold on to it: the
payload it comes from has already been through these models, so the only work left is
validation that cannot fail.

What this module does decide is which tools a profile exposes: the tools of the packs it
selects, in the order it lists them, narrowed by ``include_tools`` and ``exclude_tools``
(:func:`selected_tools`). What a tool is called and what it takes was settled by loading
step 6 (:mod:`erp_mock.loader.step6`) and the text in it by step 7
(:func:`~erp_mock.tools.descriptions.build_profile_tools`).

:class:`DatasetModel` is the other half of what a session runs on: one initial dataset, its
definition together with the current user it builds under and the Snapshot loading step 8
took up for it at :attr:`LoadedInstance.step8_anchor` (:mod:`erp_mock.loader.step8`). The
Snapshot is named by the directory of its cache entry and not by the files inside it, so
nothing here depends on :mod:`erp_mock.seed`, which reads this module in turn.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, Self

from mcp import types

from erp_mock import FRAMEWORK_VERSION, META_MODEL_VERSION
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.instance import DatasetDef, InstanceDef, ProfileDef
from erp_mock.config.model import DomainModel
from erp_mock.loader.step4 import RawDoc, RawPack
from erp_mock.loader.step6 import GeneratedTools
from erp_mock.tools.descriptions import DescriptionIndex, build_profile_tools
from erp_mock.tools.generator import ToolSpec

__all__ = [
    "TOOLS_KEY",
    "DatasetModel",
    "LoadedInstance",
    "ProfileModel",
    "VersionInfo",
    "selected_tools",
    "tools_list_payload",
]

# The one key of a tools/list payload: ``nextCursor`` and ``_meta`` are None and left out,
# so a payload is ``{"tools": [...]}`` (requirement 14.5, tests/probes/test_sdk_server.py).
TOOLS_KEY: Final = "tools"

# Plain JSON, as the SDK models take it and give it back: lists and dicts, never a tuple or
# a FrozenMap. :func:`_plain_json` turns a read-only tree into this and :func:`_frozen`
# turns it back.
type _PlainJson = None | bool | int | float | str | list["_PlainJson"] | dict[str, "_PlainJson"]


# --- versions ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """The versions of everything that decides what the service exposes (design.md 2.4).

    - ``framework`` and ``meta_model``: the engine's own versions
      (:data:`~erp_mock.FRAMEWORK_VERSION`, :data:`~erp_mock.META_MODEL_VERSION`), which no
      configuration can change; the second one is what every pack's
      ``requires_meta_model`` was matched against at step 2.
    - ``instance``: the ``version`` of instance.yaml.
    - ``packs``: the version instance.yaml pins each pack to, in its order
      (requirement 2.3).
    - ``profiles`` and ``datasets``: the ``version`` of each profile and dataset definition
      step 4 read, in the order instance.yaml lists them.

    Together they are the identity of a tools/list payload (tool-design.md section 11) and
    part of what decides whether a cached Snapshot still fits (requirement 20.12), which is
    why the Seed_Builder is given this record too (design.md component 6).
    """

    framework: str = FRAMEWORK_VERSION
    meta_model: str = META_MODEL_VERSION
    instance: str = ""
    packs: FrozenMap[str, str] = FrozenMap()
    profiles: FrozenMap[str, str] = FrozenMap()
    datasets: FrozenMap[str, str] = FrozenMap()

    @classmethod
    def of(
        cls,
        instance: InstanceDef,
        profiles: Sequence[RawDoc[ProfileDef]],
        datasets: Sequence[RawDoc[DatasetDef]],
    ) -> Self:
        """The versions of ``instance`` and of the definitions step 4 read for it."""
        return cls(
            instance=instance.version,
            packs=FrozenMap(instance.packs),
            profiles=FrozenMap((doc.document.profile, doc.document.version) for doc in profiles),
            datasets=FrozenMap((doc.document.dataset, doc.document.version) for doc in datasets),
        )


# --- one profile -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileModel:
    """One tool profile: which tools a session sees, and the payload that lists them.

    - ``definition``: the validated ``profiles/<name>.yaml``.
    - ``tools``: the tools the profile exposes, in the output order of requirement 11.9 and
      each carrying the descriptions of the profile's variant (requirement 3.7).
    - ``payload``: the tools/list result built from those tools, serialized the way the SDK
      sends it and frozen (module docstring); ``payload[TOOLS_KEY]`` is a tuple of one
      object per tool, in the same order.
    """

    definition: ProfileDef
    tools: tuple[ToolSpec, ...] = ()
    payload: FrozenMap[str, JsonValue] = FrozenMap()

    @classmethod
    def build(
        cls, profile: ProfileDef, tools: GeneratedTools, descriptions: DescriptionIndex
    ) -> Self:
        """The profile ``profile`` exposes out of ``tools``, with the text of its variant.

        ``tools`` are the tools of the whole instance, as loading step 6 generated them,
        and ``descriptions`` the description documents step 7 read.
        """
        specs = build_profile_tools(
            selected_tools(tools.of(profile.packs), profile), descriptions, profile
        )
        return cls(definition=profile, tools=specs, payload=tools_list_payload(specs))

    @property
    def name(self) -> str:
        """The profile name, as instance.yaml lists it and an address names it."""
        return self.definition.profile

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The names of the exposed tools, in the order tools/list returns them (req 11.9)."""
        return tuple(spec.name for spec in self.tools)

    def tools_list_result(self) -> types.ListToolsResult:
        """The cached payload as the SDK model a tools/list handler returns.

        A fresh model each call: the SDK models are mutable, so none of them is kept inside
        the read-only configuration (requirement 2.11, module docstring). The payload it is
        built from was validated through these same models at load time, so this cannot
        fail while serving.
        """
        return types.ListToolsResult.model_validate(_plain_json(self.payload))


def selected_tools(specs: Sequence[ToolSpec], profile: ProfileDef) -> tuple[ToolSpec, ...]:
    """The tools of ``specs`` that ``profile`` exposes, in the order they are given.

    ``specs`` are the tools of the packs the profile selects, together with the built-in
    tools, in the output order of requirement 11.9 (:meth:`GeneratedTools.of
    <erp_mock.loader.step6.GeneratedTools.of>`). An empty ``include_tools`` exposes all of
    them and a non-empty one exposes those it names; ``exclude_tools`` then drops what it
    names (tool-design.md section 8). Both lists are applied to every tool, the built-in
    ``business_context_get`` included, which is why naming it is no error of step 6
    (requirement 2.10); a name that matches no tool of the profile was reported there.
    """
    include = frozenset(profile.include_tools)
    exclude = frozenset(profile.exclude_tools)
    kept = (spec for spec in specs if not include or spec.name in include)
    return tuple(spec for spec in kept if spec.name not in exclude)


def tools_list_payload(specs: Sequence[ToolSpec]) -> FrozenMap[str, JsonValue]:
    """The tools/list payload of ``specs``, serialized as the SDK sends it and frozen.

    The tools are built as ``mcp.types.Tool`` and listed in a ``ListToolsResult``, which is
    then dumped with the parameters the SDK's own session uses for a result
    (``by_alias=True, mode="json", exclude_none=True``), so the payload carries the key
    names and the key order of the protocol: ``name``, ``description``, ``inputSchema``,
    ``outputSchema``, ``annotations`` per tool, and ``tools`` alone at the top, since
    ``nextCursor`` and ``_meta`` are None. The result is frozen, so the payload is as
    read-only as the rest of the configuration (requirement 2.11).
    """
    result = types.ListToolsResult(tools=[_sdk_tool(spec) for spec in specs])
    dumped = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    return FrozenMap((key, _frozen(value)) for key, value in dumped.items())


def _sdk_tool(spec: ToolSpec) -> types.Tool:
    """``spec`` as the SDK's ``Tool``: the name, the text, both schemas and the hints.

    The schemas are passed as plain JSON, since the model declares them as dicts; the
    annotations are those of requirement 11.8, with ``destructiveHint`` left out for a
    read-only tool (:class:`~erp_mock.tools.output_schema.ToolAnnotationsSpec`).
    """
    hints = spec.annotations
    return types.Tool(
        name=spec.name,
        description=spec.description,
        inputSchema=_plain_object(spec.input_schema),
        outputSchema=_plain_object(spec.output_schema),
        annotations=types.ToolAnnotations(
            readOnlyHint=hints.read_only_hint,
            destructiveHint=hints.destructive_hint,
            openWorldHint=hints.open_world_hint,
        ),
    )


def _plain_json(value: JsonValue) -> _PlainJson:
    """``value`` as plain JSON: every mapping a dict and every tuple a list.

    What the SDK models are given and validated from. A schema of a tool is a read-only
    tree, and the values below the top level of a ``dict[str, Any]`` field are not
    validated at all, so a FrozenMap or a tuple left inside one would reach the model's
    JSON serializer, which knows neither.
    """
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if value is None or isinstance(value, str | bool | int | float):
        return value
    return [_plain_json(item) for item in value]


def _plain_object(value: Mapping[str, JsonValue]) -> dict[str, _PlainJson]:
    """``value`` as a plain JSON object, which is what a schema field of a model takes."""
    return {key: _plain_json(item) for key, item in value.items()}


def _frozen(value: _PlainJson) -> JsonValue:
    """``value`` with every dict turned into a FrozenMap and every list into a tuple."""
    if isinstance(value, dict):
        return FrozenMap((key, _frozen(item)) for key, item in value.items())
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


# --- one initial dataset -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetModel:
    """One initial dataset: what it declares, and the Snapshot step 8 took up for it.

    - ``definition``: the validated ``datasets/<name>.yaml``.
    - ``file``: that file, relative to the instance directory, which is what an error about
      the dataset names (step 4 matches a definition by its ``dataset`` field, not by its
      file name).
    - ``current_user``: the current user every operation of a build runs as - the dataset's
      own ``current_user`` or, where it declares none, that of instance.yaml
      (requirement 20.5); loading step 8 checked that the Snapshot holds that record
      (requirement 2.10).
    - ``snapshot``: the directory of the cache entry step 8 took up at
      :attr:`LoadedInstance.step8_anchor`, holding the Snapshot, its metadata and its alias
      map (design.md data model 2); None where step 8 has not run.

    A sandbox of another anchor date is created from the Snapshot of that date, which the
    cache takes up or builds from ``definition`` (:mod:`erp_mock.seed.cache`); the alias
    map is the same whatever the anchor date (requirement 20.14).
    """

    definition: DatasetDef
    file: str = ""
    current_user: str | None = None
    snapshot: Path | None = None

    @property
    def name(self) -> str:
        """The dataset name, as instance.yaml lists it and an address names it."""
        return self.definition.dataset

    @property
    def version(self) -> str:
        """The ``version`` of the definition, part of the key of its Snapshot (req 20.11)."""
        return self.definition.version


# --- the loaded instance -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedInstance:
    """Everything a validated instance configuration amounts to (design.md 2.4).

    - ``instance``: the validated instance.yaml, which names the default profile and the
      default dataset of a session that asks for neither (requirement 15.1).
    - ``step8_anchor``: the anchor date loading step 8 took up the Snapshot of every
      initial dataset under (requirement 2.14); the one field besides ``instance`` that has
      no default, since it is given to the loader rather than read from a document.
    - ``versions``: the versions of the engine, of the instance and of every document
      (:class:`VersionInfo`).
    - ``packs``: the documents of each business pack, in the order instance.yaml references
      them; the Seed_Builder reads the generators and the Shared_Data from them.
    - ``domain``: the resolved configuration - entities, bills, conversions, writebacks,
      linkages, rules and queries - which every layer of the engine reads instead of the
      documents.
    - ``profiles``: one :class:`ProfileModel` per profile of instance.yaml, keyed by name
      and in its order.
    - ``datasets``: one :class:`DatasetModel` per initial dataset of instance.yaml, keyed by
      name and in its order; empty until step 8 has run.
    """

    instance: InstanceDef
    step8_anchor: date
    versions: VersionInfo = VersionInfo()
    packs: tuple[RawPack, ...] = ()
    domain: DomainModel = DomainModel()
    profiles: FrozenMap[str, ProfileModel] = FrozenMap()
    datasets: FrozenMap[str, DatasetModel] = FrozenMap()

    @classmethod
    def build(
        cls,
        instance: InstanceDef,
        packs: Sequence[RawPack],
        domain: DomainModel,
        profiles: Sequence[RawDoc[ProfileDef]],
        datasets: Sequence[RawDoc[DatasetDef]],
        tools: GeneratedTools,
        descriptions: DescriptionIndex,
        step8_anchor: date,
        dataset_models: Sequence[DatasetModel] = (),
    ) -> Self:
        """Put the results of the validation steps together into the read-only object.

        The arguments are what the steps left behind: ``instance`` and ``packs`` from
        steps 1 and 4, ``profiles`` and ``datasets`` from step 4, ``domain`` from step 5,
        ``tools`` from step 6, ``descriptions`` from step 7 and ``dataset_models`` from
        step 8, the datasets with the Snapshot it took up for each of them. Only an instance
        that passed every step is assembled, which is what :meth:`Loader.load
        <erp_mock.loader.loader.Loader.load>` guarantees.

        ``datasets`` are the definitions, which decide the dataset versions whether step 8
        has run or not, and ``dataset_models`` what that step made of them: a profile is
        assembled here out of its definition and the tools, while a dataset needs a
        Snapshot, which only step 8 can take up. The object is built without those models
        while the steps before step 8 are being run on their own, which is why
        ``dataset_models`` defaults to none.
        """
        return cls(
            instance=instance,
            step8_anchor=step8_anchor,
            versions=VersionInfo.of(instance, profiles, datasets),
            packs=tuple(packs),
            domain=domain,
            profiles=FrozenMap(
                (doc.document.profile, ProfileModel.build(doc.document, tools, descriptions))
                for doc in profiles
            ),
            datasets=FrozenMap((model.name, model) for model in dataset_models),
        )

    @property
    def default_profile(self) -> ProfileModel:
        """The profile of a session that asks for none (requirement 15.1).

        The ``default_profile`` of instance.yaml, which step 5 checked is one of the
        profiles (requirement 2.10).
        """
        return self.profiles[self.instance.default_profile]

    @property
    def default_dataset(self) -> DatasetModel:
        """The initial dataset of a session that asks for none (requirement 15.1).

        The ``default_dataset`` of instance.yaml, which step 5 checked is one of the
        datasets (requirement 2.10); raises KeyError before step 8 has run, the datasets
        being what that step leaves behind.
        """
        return self.datasets[self.instance.default_dataset]
