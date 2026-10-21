"""erp_mock.loader.instance: the read-only configuration object and the tools/list payload.

What is asserted here, on the engine test fixture loaded through the implemented steps:

- :meth:`~erp_mock.loader.loader.Loader.load` returns the
  :class:`~erp_mock.loader.instance.LoadedInstance` of design.md 2.4, carrying the instance
  configuration, the versions of everything that decides what is exposed, the documents of
  each pack, the resolved domain model, one profile per profile of instance.yaml and one
  dataset per dataset of it;
- nothing in that object can be modified and nothing it holds is a mutable container
  (requirement 2.11); the exhaustive version of this is the property test of task 9.6;
- the tools/list payload is assembled per profile, carries the keys of the protocol in the
  SDK's order, and serializes byte for byte the same when the fixture is loaded twice from
  different working directories and with different anchor dates (requirement 11.10);
- ``include_tools`` and ``exclude_tools`` decide which of the tools of the selected packs a
  profile exposes.
"""

from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from erp_mock import FRAMEWORK_VERSION, META_MODEL_VERSION
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_compact, dumps_pretty, encode_utf8
from erp_mock.config.model import DomainModel
from erp_mock.loader.instance import TOOLS_KEY, LoadedInstance, ProfileModel
from erp_mock.loader.loader import Loader
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET
from tests.helpers.fixtures import (
    ANCHOR_DATE,
    BASE_PACK,
    DATASET,
    INSTANCE_DIR,
    INSTANCE_NAME,
    PACK_NAMES,
    PACKS_DIR,
    PROFILE,
    TRADE_PACK,
    fixture_loaded,
    load_fixture,
)

# The keys of one tool of a tools/list payload, in the order the SDK's ``Tool`` model
# writes them; ``title``, ``icons``, ``_meta`` and ``execution`` are None and left out
# (tests/probes/test_sdk_server.py).
TOOL_KEYS: Final = ("name", "description", "inputSchema", "outputSchema", "annotations")

# Two tools of the fixture that a profile can pick out by name: a read tool of the base
# pack and the renamed one next to it.
ARTICLE_GET: Final = "article_get"
VENDOR_GET: Final = "vendor_detail_get"


def payload_bytes(loaded: LoadedInstance) -> bytes:
    """The tools/list payload of every profile of ``loaded``, as bytes written to a file."""
    texts = [dumps_pretty(profile.payload) for profile in loaded.profiles.values()]
    return encode_utf8("".join(texts))


def listed_tools(profile: ProfileModel) -> tuple[Mapping[str, JsonValue], ...]:
    """The tool objects of the payload of ``profile``, in the order it lists them."""
    found = profile.payload[TOOLS_KEY]
    assert isinstance(found, tuple)
    listed = tuple(tool for tool in found if isinstance(tool, Mapping))
    assert len(listed) == len(found)
    return listed


def rebuilt(loader: Loader, **overrides: Any) -> ProfileModel:
    """The fixture profile with ``overrides`` applied to its definition, assembled again.

    The one profile of the fixture declares no ``include_tools`` and no ``exclude_tools``,
    so a test that needs them replaces them on the validated definition.
    """
    tools = loader.tools
    descriptions = loader.descriptions
    assert tools is not None and descriptions is not None
    definition = loader.raw_profiles[0].document
    return ProfileModel.build(definition.model_copy(update=overrides), tools, descriptions)


def mutable_sites(value: object, path: str) -> list[str]:
    """The paths below ``value`` that hold a container which is not read-only (req 2.11).

    A read-only tree is built of FrozenMaps, tuples and scalars; anything else - a list, a
    dict, a set - is reported with its path and its type.
    """
    if isinstance(value, FrozenMap):
        return [p for key, item in value.items() for p in mutable_sites(item, f"{path}.{key}")]
    if isinstance(value, tuple):
        return [p for n, item in enumerate(value) for p in mutable_sites(item, f"{path}[{n}]")]
    if isinstance(value, str) or not isinstance(value, Mapping | Sequence | set | frozenset):
        return []
    return [f"{path}: {type(value).__name__}"]


@pytest.mark.req("2.11")
def test_load_returns_the_configuration_of_the_whole_instance(tmp_path: Path) -> None:
    # Every step, the Snapshot of the dataset included, which step 8 caches below the data
    # directory (tests/unit/loader/test_step8.py is about that step).
    loaded = Loader(INSTANCE_DIR, PACKS_DIR, data_dir=tmp_path).load()

    assert loaded.instance.instance == INSTANCE_NAME
    assert [pack.name for pack in loaded.packs] == list(PACK_NAMES)
    # The resolved model, not the documents: what every layer of the engine reads.
    assert isinstance(loaded.domain, DomainModel)
    assert "supply_order" in loaded.domain.bills
    # One profile per name of instance.yaml, keyed by name and in its order.
    assert tuple(loaded.profiles) == (PROFILE,)
    assert loaded.default_profile is loaded.profiles[PROFILE]
    assert loaded.default_profile.name == PROFILE
    # One dataset per name of instance.yaml, with the Snapshot of the anchor date the clock
    # configuration fixes (requirement 2.14).
    assert tuple(loaded.datasets) == (DATASET,)
    assert loaded.step8_anchor == ANCHOR_DATE
    assert loaded.default_dataset.snapshot is not None


@pytest.mark.req("2.11")
def test_the_versions_record_the_engine_the_instance_and_every_definition() -> None:
    versions = fixture_loaded().versions

    # The engine's own versions, which no configuration can change.
    assert (versions.framework, versions.meta_model) == (FRAMEWORK_VERSION, META_MODEL_VERSION)
    assert versions.instance == "1.0.0"
    assert dict(versions.packs) == dict.fromkeys(PACK_NAMES, "1.0.0")
    assert tuple(versions.packs) == PACK_NAMES
    assert dict(versions.profiles) == {PROFILE: "1.0.0"}
    assert dict(versions.datasets) == {DATASET: "1.0.0"}


@pytest.mark.req("2.11")
def test_every_modification_of_the_object_tree_is_rejected() -> None:
    loaded = fixture_loaded()
    profile = loaded.default_profile
    before = dumps_pretty(profile.payload)

    # An attribute of the object, of a nested dataclass and of a validated document.
    with pytest.raises(FrozenInstanceError):
        loaded.domain = DomainModel()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        loaded.versions.instance = "9.9.9"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        profile.tools = ()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        profile.definition.packs = ()  # type: ignore[misc]
    # A mapping of the object tree, and the payload the gateway sends.
    with pytest.raises(TypeError):
        loaded.profiles["other"] = profile  # type: ignore[index]
    with pytest.raises(TypeError):
        profile.payload[TOOLS_KEY] = ()  # type: ignore[index]
    with pytest.raises(AttributeError):
        profile.payload.clear()  # type: ignore[attr-defined]

    assert dumps_pretty(profile.payload) == before


@pytest.mark.req("2.11")
def test_the_object_tree_holds_no_mutable_container() -> None:
    profile = fixture_loaded().default_profile

    found = mutable_sites(profile.payload, "payload")
    for spec in profile.tools:
        found += mutable_sites(spec.input_schema, f"{spec.name}.inputSchema")
        found += mutable_sites(spec.output_schema, f"{spec.name}.outputSchema")
        found += mutable_sites(spec.param_paths, f"{spec.name}.param_paths")

    assert found == []


@pytest.mark.req("11.10")
def test_the_payload_lists_every_exposed_tool_with_the_keys_of_the_protocol() -> None:
    profile = fixture_loaded().default_profile

    # ``tools`` alone: nextCursor and _meta are None and left out (requirement 14.5).
    assert tuple(profile.payload) == (TOOLS_KEY,)
    listed = listed_tools(profile)
    assert tuple(tool["name"] for tool in listed) == profile.tool_names
    # The built-in tool comes first and the packs follow (requirement 11.9).
    assert profile.tool_names[0] == BUSINESS_CONTEXT_GET
    assert set(profile.tool_names) > {ARTICLE_GET, VENDOR_GET}
    for tool in listed:
        assert tuple(tool) == TOOL_KEYS, tool["name"]
        assert tool["description"], tool["name"]
    # The hints of requirement 11.8 under the names of the protocol; a read-only tool
    # carries no destructiveHint.
    assert listed[0]["annotations"] == {"readOnlyHint": True, "openWorldHint": False}


@pytest.mark.req("11.10")
def test_the_payload_does_not_depend_on_the_working_directory_or_the_anchor_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Loaded from the directory the test run started in, with the anchor date of the
    # clock configuration.
    baseline = payload_bytes(fixture_loaded())

    monkeypatch.chdir(tmp_path)
    first = payload_bytes(fixture_loaded(anchor_date=date(2001, 2, 3)))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    second = payload_bytes(fixture_loaded(anchor_date=date(2099, 12, 31)))

    assert first == baseline
    assert second == baseline


@pytest.mark.req("11.10")
def test_the_sdk_result_is_built_from_the_cached_payload() -> None:
    profile = fixture_loaded().default_profile

    result = profile.tools_list_result()

    assert [tool.name for tool in result.tools] == list(profile.tool_names)
    assert result.nextCursor is None
    # The payload is what the SDK sends for this result (tests/probes/test_sdk_server.py).
    dumped = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert dumps_compact(dumped) == dumps_compact(profile.payload)
    # A fresh model each call, so a handler that is handed one cannot reach the
    # configuration through it (requirement 2.11).
    assert profile.tools_list_result() is not result


@pytest.mark.req("11.10")
def test_include_tools_and_exclude_tools_decide_what_a_profile_exposes() -> None:
    loader = load_fixture()
    every = rebuilt(loader).tool_names

    chosen = rebuilt(loader, include_tools=(ARTICLE_GET, BUSINESS_CONTEXT_GET))
    dropped = rebuilt(loader, exclude_tools=(ARTICLE_GET, BUSINESS_CONTEXT_GET))
    one_pack = rebuilt(loader, packs=(BASE_PACK,))

    # include_tools keeps the named tools, in the output order and not the written one.
    assert chosen.tool_names == (BUSINESS_CONTEXT_GET, ARTICLE_GET)
    assert tuple(tool["name"] for tool in listed_tools(chosen)) == chosen.tool_names
    # exclude_tools drops the named tools, the built-in one included.
    assert dropped.tool_names == tuple(
        name for name in every if name not in {ARTICLE_GET, BUSINESS_CONTEXT_GET}
    )
    # ``packs`` decides which tools there are to choose from at all.
    assert {spec.pack for spec in one_pack.tools} == {None, BASE_PACK}
    assert TRADE_PACK not in {spec.pack for spec in one_pack.tools}
    assert set(one_pack.tool_names) < set(every)
