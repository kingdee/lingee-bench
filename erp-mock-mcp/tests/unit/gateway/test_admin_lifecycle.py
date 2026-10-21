"""What the Admin API answers for the seven lifecycle interfaces, and what it refuses.

:mod:`erp_mock.gateway.admin` is an ASGI application, so every test here speaks HTTP to it
through :class:`httpx.ASGITransport` - no socket, no uvicorn - and reads the status code,
the headers and the body a deployer would see. That is the whole observable behaviour of
the module: requirement 19.9 is a key order, requirement 19.11 a filter and an order,
requirement 19.15 a 404, requirement 19.17 a 400, requirement 19.18 a 503 and
requirement 19.19 a 500 with one error object in it, and the sandboxes and sessions behind
them say what each answer left behind (requirement 19.13).

The service under test is the real thing short of the middlewares of task 23.1 and the two
sockets of task 23.4: a real :class:`~erp_mock.gateway.sandbox.SandboxManager` over a data
directory of the test, a real :class:`~erp_mock.gateway.router.McpRouter` in front of it -
which is where the sandboxes of the Admin API come from and what ends the sessions of one
it froze or deleted - and the :class:`~erp_mock.gateway.admin.AdminRouter` over both. A
session opened in these tests is therefore a real SDK transport, which is what makes the
freeze and the delete of requirement 19.13 observable.

What is replaced, and why:

- the Snapshot source (:class:`Snapshots`): one Snapshot file built once for the module,
  from the standard basic data of :mod:`tests.helpers.engine_env`, so no test builds a
  dataset. ``fail`` is what makes a creation fail the way a Snapshot that cannot be built
  fails it (requirement 19.19);
- the clock (:class:`Ticker`, :func:`stepping_clock`): no test reads the system clock
  (requirement 25.10). The fixture's clock mode is fixed, so the anchor date of a sandbox
  that asks for none is :data:`~tests.helpers.fixtures.ANCHOR_DATE` whatever the day of the
  run, and ``step`` decides whether two sandboxes get different ``created_at`` values -
  which is what the two orderings of requirement 19.11 need;
- the initial dataset of the loaded instance: loading step 8 is not run here, so the
  dataset it would leave behind is built by hand (:func:`loaded_with_dataset`), its
  ``snapshot`` pointing at a directory that holds the alias map of :data:`ALIASES` the way
  a cache entry holds it (requirement 19.10).

The business configuration is the engine test fixture (:mod:`tests.helpers.fixtures`), not
the example instance: it exposes the same kinds of tools and loads in milliseconds, and the
example instance belongs to the integration tests of task 26.2.
"""

import json
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import anyio
import httpx
import pytest

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import dumps_pretty, encode_utf8
from erp_mock.config.instance import DatasetDef
from erp_mock.gateway.admin import ALIASES_FILENAME, AdminError, AdminRouter
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import SessionHandlers
from erp_mock.gateway.router import SESSION_ID_HEADER, McpRouter, SessionRecord, sandbox_path
from erp_mock.gateway.sandbox import SandboxLimits, SandboxManager, SandboxRuntime
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.loader.yaml_io import load_yaml
from erp_mock.seed.builder import ALIASES_FILENAME as SEED_ALIASES_FILENAME
from erp_mock.state.schema import StorageSchema, build_schema
from erp_mock.state.snapshot import build_snapshot_db, finalize_snapshot
from erp_mock.tools.layer import ToolLayer
from tests.helpers.engine_env import CURRENT_USER, create_basic_data
from tests.helpers.fixtures import (
    ANCHOR_DATE,
    DATASET,
    INSTANCE_DIR,
    INSTANCE_NAME,
    PACK_NAMES,
    PROFILE,
    fixture_loaded,
)

# Upper bound for every test: a hang fails the test instead of blocking the run.
TIMEOUT_S: Final = 10

# Where the httpx clients send their requests; the ASGI transport never opens a socket.
BASE_URL: Final = "http://127.0.0.1:8000"

# The definition file of the initial dataset of the fixture, relative to its instance
# directory, as loading step 4 reads it.
DATASET_FILE: Final = f"datasets/{DATASET}.yaml"

# The instant the first reading of the test clock returns; with ``step`` seconds between
# readings, two sandboxes get ``created_at`` values ``step`` seconds apart.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# The thresholds every service of this file runs with: seconds of its own monotonic source
# rather than the five minutes of the service, and three concurrent sandboxes, which is
# what makes the 503 of requirement 19.18 reachable with three prebuilt ones.
MAX_SANDBOXES: Final = 3
LIMITS: Final = SandboxLimits(
    idle_seconds=60, freeze_seconds=300, retention_seconds=900, max_sandboxes=MAX_SANDBOXES
)

# The seven interfaces of task 23.2 and the five they are built from.
HEALTH: Final = "/admin/health"
SANDBOXES: Final = "/admin/sandboxes"
ALIASES_PATH: Final = f"/admin/datasets/{DATASET}/aliases"

# The alias map the Snapshot of the fixture dataset is accompanied by (requirement 20.10),
# written out sorted by code point the way the Seed_Builder writes it.
ALIASES: Final[Mapping[str, str]] = {
    "material:bolt": "mat_000001",
    "supplier:main": "par_000001",
    "warehouse:central": "wh_000001",
}

# The headers the official client puts on every request (mcp.client.streamable_http) and
# the protocol version it announces; a session is opened here only to show what a freeze
# and a delete do to it (requirement 19.13).
JSON_AND_SSE: Final = "application/json, text/event-stream"
POST_HEADERS: Final[Mapping[str, str]] = {
    "content-type": "application/json",
    "accept": JSON_AND_SSE,
}
PROTOCOL_VERSION: Final = "2025-11-25"

# The keys of each answer, in the order the requirements fix them.
HEALTH_KEYS: Final = (
    "framework_version",
    "meta_model_version",
    "instance",
    "packs",
    "profiles",
    "datasets",
)
CREATED_KEYS: Final = ("sandbox_id", "mcp_url", "profile", "dataset", "anchor_date")
METADATA_KEYS: Final = (
    "sandbox_id",
    "session_ids",
    "profile",
    "dataset",
    "anchor_date",
    "status",
    "created_at",
)
ERROR_KEYS: Final = ("error", "message")

# A sandbox id of the right shape that no service of this file has.
MISSING_ID: Final = "sb_0000000000000000"

# What the Snapshot source raises when the Snapshot of a dataset cannot be built; the seed
# layer's own exception is a SeedError, which the gateway does not import (admin docstring).
BUILD_FAILED: Final = "数据集的不变量不成立"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- the test clock (requirement 25.10) --------------------------------------------------


@dataclass(slots=True)
class Ticker:
    """A monotonic source the test moves itself (requirement 25.10).

    Every reading returns :attr:`seconds`, which :meth:`advance` raises. Nothing of the
    Admin API measures time; the Sandbox_Manager reads this for the idle timing of every
    job it runs.
    """

    seconds: float = 1000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> float:
        """Move the clock forward and return the new reading."""
        self.seconds += seconds
        return self.seconds


def stepping_clock(ticker: Ticker, step: int) -> SystemClock:
    """A clock whose nth instant is :data:`INSTANT` plus ``step`` * (n - 1) seconds.

    ``step`` of 1 gives every sandbox a ``created_at`` of its own, which is the first key
    the listing of requirement 19.11 orders by; ``step`` of 0 gives them all the same one,
    which is what makes the second key - the ``sandbox_id`` - decide.
    """
    readings = iter(range(1 << 20))
    return SystemClock(
        now=lambda: INSTANT + timedelta(seconds=step * next(readings)), monotonic=ticker
    )


# --- the instance configuration, its Snapshot and its alias map --------------------------


@dataclass(frozen=True, slots=True)
class Env:
    """What every test of this file starts from, built once for the module."""

    loaded: LoadedInstance
    schema: StorageSchema
    layer: ToolLayer
    snapshot: Path

    @property
    def profile(self) -> ProfileModel:
        """The one profile of the fixture instance, whose tools a session sees."""
        return self.loaded.default_profile

    @property
    def dataset(self) -> DatasetModel:
        """The one initial dataset of the fixture instance."""
        return self.loaded.default_dataset


def loaded_with_dataset(cache_dir: Path) -> LoadedInstance:
    """The fixture configuration with the initial dataset loading step 8 would leave.

    The steps before step 8 leave no :class:`~erp_mock.loader.instance.DatasetModel`
    behind, and the Admin API looks a dataset name up in exactly that map
    (requirements 19.15 and 19.17), so the model is built here the way step 8 builds it:
    the validated definition, the file it came from, the current user the instance
    configuration resolves for it and the cache entry the Snapshot was taken up in. That
    entry holds the alias map of :data:`ALIASES`, which is what
    GET /admin/datasets/{name}/aliases reads (requirement 19.10); the Snapshot database
    itself is not in it, the sandboxes of this file being created from :class:`Snapshots`
    the way the service creates them from its cache.
    """
    loaded = fixture_loaded()
    definition = DatasetDef.model_validate(load_yaml(INSTANCE_DIR / DATASET_FILE))
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / ALIASES_FILENAME).write_bytes(encode_utf8(dumps_pretty(aliases_payload())))
    dataset = DatasetModel(
        definition=definition,
        file=DATASET_FILE,
        current_user=CURRENT_USER,
        snapshot=cache_dir,
    )
    return replace(loaded, datasets=FrozenMap({dataset.name: dataset}))


def aliases_payload() -> dict[str, Any]:
    """The object ``aliases.json`` holds, as the Seed_Builder writes it (req 20.10)."""
    version = DatasetDef.model_validate(load_yaml(INSTANCE_DIR / DATASET_FILE)).version
    return {
        "dataset": f"{DATASET}@{version}",
        "aliases": {alias: ALIASES[alias] for alias in sorted(ALIASES)},
    }


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Env:
    """The fixture instance, its tools layer, one Snapshot and one alias map."""
    loaded = loaded_with_dataset(tmp_path_factory.mktemp("cache"))
    layer = ToolLayer(loaded)
    schema = build_schema(loaded.domain)
    return Env(
        loaded=loaded,
        schema=schema,
        layer=layer,
        snapshot=build_snapshot(tmp_path_factory.mktemp("snapshot"), schema, layer),
    )


def build_snapshot(directory: Path, env_schema: StorageSchema, layer: ToolLayer) -> Path:
    """Build the one Snapshot of this file below ``directory`` and return its path."""
    build = directory / "build.sqlite"
    snapshot = directory / "snapshot.sqlite"
    store = build_snapshot_db(build, env_schema)
    try:
        create_basic_data(layer.engine, store, anchor_date=ANCHOR_DATE)
    finally:
        store.close()
    finalize_snapshot(build, snapshot)
    build.unlink()
    return snapshot


@dataclass(slots=True)
class Snapshots:
    """A :class:`~erp_mock.gateway.sandbox.SnapshotSource` over one Snapshot file.

    ``asked`` records the (dataset, anchor date) of every creation, which is what the
    service's own source would have looked up in the Snapshot cache or built; ``fail``
    makes the next and every following creation fail the way a Snapshot that cannot be
    built fails it (requirement 19.19).
    """

    db: Path
    asked: list[tuple[str, date]] = field(default_factory=list)
    fail: bool = False

    @contextmanager
    def snapshot(self, dataset: DatasetModel, anchor: date) -> Iterator[Path]:
        self.asked.append((dataset.name, anchor))
        if self.fail:
            raise RuntimeError(BUILD_FAILED)
        yield self.db


# --- the service under test --------------------------------------------------------------


@dataclass(slots=True)
class Service:
    """The Admin API with the MCP endpoint, the sandboxes and two HTTP clients."""

    admin: AdminRouter
    mcp: McpRouter
    manager: SandboxManager
    client: httpx.AsyncClient
    mcp_client: httpx.AsyncClient
    snapshots: Snapshots
    ticker: Ticker
    env: Env

    async def create(self, body: Any | None = None) -> httpx.Response:
        """POST /admin/sandboxes with ``body``, or with no body at all for None."""
        if body is None:
            return await self.client.post(SANDBOXES)
        content = body if isinstance(body, str) else json.dumps(body)
        return await self.client.post(
            SANDBOXES, content=content.encode(), headers={"content-type": "application/json"}
        )

    async def prebuilt(self) -> str:
        """Prebuild one sandbox through the API and return its id (requirement 19.3)."""
        response = await self.create()
        assert response.status_code == httpx.codes.CREATED, response.text
        sandbox_id = response.json()["sandbox_id"]
        assert isinstance(sandbox_id, str)
        return sandbox_id

    async def metadata(self, sandbox_id: str) -> httpx.Response:
        """GET /admin/sandboxes/{id} (requirement 19.12)."""
        return await self.client.get(f"{SANDBOXES}/{sandbox_id}")

    async def listing(self, **query: str) -> httpx.Response:
        """GET /admin/sandboxes with ``query`` as its query string (requirement 19.11)."""
        return await self.client.get(SANDBOXES, params=query)

    def sandbox(self, sandbox_id: str) -> SandboxRuntime:
        """The runtime of the sandbox ``sandbox_id``, which must be one of this service."""
        found = self.manager.get(sandbox_id)
        assert found is not None
        return found

    async def open_session(self, sandbox_id: str) -> "Session":
        """Open an MCP session on ``/s/<sandbox_id>/mcp`` (requirement 15.3)."""
        path = sandbox_path(sandbox_id)
        response = await self.mcp_client.post(
            path, content=json.dumps(initialize_body()).encode(), headers=dict(POST_HEADERS)
        )
        assert response.status_code == httpx.codes.OK, response.text
        session_id = response.headers[SESSION_ID_HEADER]
        record = self.mcp.sessions.get(session_id)
        assert record is not None
        return Session(service=self, session_id=session_id, path=path, record=record)


@dataclass(slots=True)
class Session:
    """One open MCP session, and the one request a test of this file makes in it."""

    service: Service
    session_id: str
    path: str
    record: SessionRecord

    async def ping(self) -> httpx.Response:
        """Send one ping, which needs neither the database nor the sandbox lock."""
        body = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        headers = {
            **POST_HEADERS,
            SESSION_ID_HEADER: self.session_id,
            "mcp-protocol-version": PROTOCOL_VERSION,
        }
        return await self.service.mcp_client.post(
            self.path, content=json.dumps(body).encode(), headers=headers
        )


def initialize_body(request_id: int = 1) -> dict[str, Any]:
    """The initialize request the official client opens a session with."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.0.0"},
        },
    }


@asynccontextmanager
async def service_over(
    env: Env, data_dir: Path, limits: SandboxLimits = LIMITS, step: int = 1
) -> AsyncIterator[Service]:
    """The Admin API over an MCP router and a Sandbox_Manager in ``data_dir``.

    What task 23.4 will assemble, short of the middlewares and the two uvicorn servers: one
    Sandbox_Manager, the MCP endpoint over it with its session tasks in a task group of this
    block, and the Admin API over that endpoint, which is where its sandboxes come from
    (admin docstring). Every session is ended and every sandbox closed when the block
    returns.
    """
    ticker = Ticker()
    snapshots = Snapshots(env.snapshot)
    manager = SandboxManager(
        data_dir=data_dir,
        loaded=env.loaded,
        schema=env.schema,
        snapshots=snapshots,
        clock=stepping_clock(ticker, step),
        limits=limits,
    )
    mcp = McpRouter(SessionHandlers(manager, env.layer), env.loaded)
    admin = AdminRouter(mcp, env.loaded)
    try:
        with anyio.fail_after(TIMEOUT_S):
            async with anyio.create_task_group() as tg:
                mcp.attach(tg)
                async with (
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=admin), base_url=BASE_URL
                    ) as client,
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=mcp), base_url=BASE_URL
                    ) as mcp_client,
                ):
                    # httpx adds "Accept: */*" to every request by default; drop it on the
                    # MCP client so that each request carries exactly its own headers.
                    del mcp_client.headers["accept"]
                    try:
                        yield Service(
                            admin=admin,
                            mcp=mcp,
                            manager=manager,
                            client=client,
                            mcp_client=mcp_client,
                            snapshots=snapshots,
                            ticker=ticker,
                            env=env,
                        )
                    finally:
                        await mcp.shutdown()
                        tg.cancel_scope.cancel()
    finally:
        manager.close()


# --- what an answer looks like -----------------------------------------------------------


def assert_json(response: httpx.Response, status: int) -> Any:
    """Assert ``response`` is JSON of ``status`` and return its parsed body."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    return response.json()


def assert_error(response: httpx.Response, status: int, code: AdminError) -> str:
    """Assert ``response`` is the error object of ``status`` and return its message.

    The shape is design.md 7.7's and is the same whatever went wrong: ``error`` with a
    short code and ``message`` with a Chinese explanation, in that order and nothing else.
    """
    body = assert_json(response, status)
    assert tuple(body) == ERROR_KEYS
    assert body["error"] == code
    message = body["message"]
    assert isinstance(message, str) and message
    return message


# --- GET /admin/health (requirement 19.9) ------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.9")
async def test_health_reports_the_versions_in_the_order_of_the_requirement(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        body = assert_json(await service.client.get(HEALTH), httpx.codes.OK)

        # The six keys of requirement 19.9, in its order: the two engine versions, the
        # instance as <name>@<version>, and one <name>@<version> per pack, profile and
        # initial dataset, each in the order instance.yaml lists them.
        assert tuple(body) == HEALTH_KEYS
        versions = env.loaded.versions
        assert body["framework_version"] == versions.framework
        assert body["meta_model_version"] == versions.meta_model
        assert body["instance"] == f"{INSTANCE_NAME}@{env.loaded.instance.version}"
        assert [ref.split("@")[0] for ref in body["packs"]] == list(PACK_NAMES)
        assert body["profiles"] == [f"{PROFILE}@{versions.profiles[PROFILE]}"]
        assert body["datasets"] == [f"{DATASET}@{versions.datasets[DATASET]}"]


# --- GET /admin/datasets/{name}/aliases (requirements 19.10, 20.14) ----------------------


@pytest.mark.anyio
@pytest.mark.req("19.10")
async def test_the_alias_map_is_the_one_the_snapshot_was_written_with(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        body = assert_json(await service.client.get(ALIASES_PATH), httpx.codes.OK)

        # The content of the alias map file of the dataset, its two keys in the order the
        # file wrote them and its aliases still sorted by code point (requirement 20.10).
        assert body == aliases_payload()
        assert tuple(body) == ("dataset", "aliases")
        assert list(body["aliases"]) == sorted(ALIASES)


@pytest.mark.req("19.10")
def test_the_alias_file_name_is_the_one_the_seed_builder_writes() -> None:
    # The gateway does not import the seed layer, so the name is written in both places
    # (admin docstring); this is what keeps the two from drifting apart.
    assert ALIASES_FILENAME == SEED_ALIASES_FILENAME


@pytest.mark.anyio
@pytest.mark.req("19.10")
async def test_an_alias_map_that_is_not_there_is_an_internal_error(
    env: Env, tmp_path: Path
) -> None:
    # A cache entry the alias map is missing from: the service has lost a file of its data
    # directory, which is a 500 that names the dataset and nothing of the failure itself.
    dataset = replace(env.dataset, snapshot=tmp_path / "gone")
    loaded = replace(env.loaded, datasets=FrozenMap({dataset.name: dataset}))
    async with service_over(replace(env, loaded=loaded), tmp_path / "data") as service:
        response = await service.client.get(ALIASES_PATH)

        message = assert_error(
            response, httpx.codes.INTERNAL_SERVER_ERROR, AdminError.ALIASES_UNREADABLE
        )
        assert DATASET in message


@pytest.mark.anyio
@pytest.mark.req("19.10")
async def test_the_alias_map_does_not_depend_on_any_anchor_date(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        before = (await service.client.get(ALIASES_PATH)).content

        # Two sandboxes of the same dataset at two anchor dates neither of which is the one
        # the service loaded with: the interface takes no date and answers the same map,
        # two builds of one dataset writing the same aliases whatever the date (req 20.14).
        for anchor in ("2000-02-29", "2099-12-31"):
            assert (await service.create({"anchor_date": anchor})).status_code == (
                httpx.codes.CREATED
            )
        assert (await service.client.get(ALIASES_PATH)).content == before


@pytest.mark.anyio
@pytest.mark.req("19.15")
async def test_a_dataset_that_is_not_in_the_configuration_is_not_found(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        # The name of a path that the instance configuration does not have, compared
        # case-sensitively: the dataset of the fixture written in upper case is not it.
        for name in ("nope", DATASET.upper()):
            response = await service.client.get(f"/admin/datasets/{name}/aliases")
            assert name in assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)


# --- POST /admin/sandboxes (requirements 19.3, 19.18, 19.19) -----------------------------


@pytest.mark.anyio
@pytest.mark.req("19.3")
async def test_prebuilding_a_sandbox_answers_201_and_the_fields_of_the_requirement(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        body = assert_json(await service.create(), httpx.codes.CREATED)

        # The five keys of requirement 19.3, in its order, and the address of the sandbox
        # as its mcp_url; the defaults of the instance configuration decide the profile and
        # the dataset, and the clock configuration the anchor date (requirement 16.1).
        assert tuple(body) == CREATED_KEYS
        sandbox = service.sandbox(body["sandbox_id"])
        assert body["mcp_url"] == f"/s/{sandbox.sandbox_id}/mcp"
        assert body["profile"] == f"{PROFILE}@{env.profile.definition.version}"
        assert body["dataset"] == f"{DATASET}@{env.dataset.version}"
        assert body["anchor_date"] == ANCHOR_DATE.isoformat()
        # A prebuilt sandbox is active and bound to no session (requirement 19.3), and it
        # was copied from the Snapshot of its dataset at that date (requirement 15.1).
        assert (sandbox.origin, sandbox.status, sandbox.session_ids) == ("prebuilt", "active", [])
        assert service.snapshots.asked == [(DATASET, ANCHOR_DATE)]


@pytest.mark.anyio
@pytest.mark.req("19.3")
async def test_the_mcp_url_of_a_prebuilt_sandbox_reaches_that_sandbox(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        body = assert_json(await service.create(), httpx.codes.CREATED)

        # The address the answer reports is the one a client connects to, which is what
        # makes a prebuilt sandbox useful at all (requirement 15.3).
        session = await service.open_session(body["sandbox_id"])
        assert session.record.path == body["mcp_url"]
        assert session.record.sandbox.sandbox_id == body["sandbox_id"]


@pytest.mark.anyio
@pytest.mark.req("19.3")
async def test_prebuilding_takes_the_profile_dataset_and_anchor_date_of_the_body(
    env: Env, tmp_path: Path
) -> None:
    anchor = "2026-02-28"
    async with service_over(env, tmp_path) as service:
        body = assert_json(
            await service.create({"profile": PROFILE, "dataset": DATASET, "anchor_date": anchor}),
            httpx.codes.CREATED,
        )

        # A date given for this one sandbox wins over the clock configuration
        # (requirement 16.1) and is the date its Snapshot was taken up at.
        assert body["anchor_date"] == anchor
        assert service.snapshots.asked == [(DATASET, date(2026, 2, 28))]
        assert service.sandbox(body["sandbox_id"]).anchor_date == date(2026, 2, 28)


@pytest.mark.anyio
@pytest.mark.req("19.17")
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"profile": "nope"}, id="profile-not-configured"),
        pytest.param({"dataset": "nope"}, id="dataset-not-configured"),
        pytest.param({"profile": PROFILE.upper()}, id="profile-case-sensitive"),
    ],
)
async def test_a_profile_or_dataset_that_is_not_configured_is_a_bad_request(
    env: Env, tmp_path: Path, body: Mapping[str, str]
) -> None:
    async with service_over(env, tmp_path) as service:
        response = await service.create(body)

        assert_error(response, httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)
        # Nothing was created and no Snapshot was even asked for (requirement 19.17).
        assert service.manager.sandboxes() == ()
        assert service.snapshots.asked == []


@pytest.mark.anyio
@pytest.mark.req("19.17")
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"profile": PROFILE, "extra": 1}, id="undefined-key"),
        pytest.param({"profile": 1}, id="wrong-type"),
        pytest.param({"anchor_date": "2026-02-30"}, id="date-not-in-calendar"),
        pytest.param({"anchor_date": "2026-2-3"}, id="date-not-padded"),
        pytest.param({"anchor_date": None}, id="null-is-the-default"),
        pytest.param("[1, 2]", id="not-an-object"),
        pytest.param("{", id="not-json"),
    ],
)
async def test_a_body_that_does_not_fit_the_model_is_a_bad_request(
    env: Env, tmp_path: Path, body: Any
) -> None:
    async with service_over(env, tmp_path) as service:
        response = await service.create(body)

        # A null for an optional field is the default and therefore accepted; everything
        # else here is a 400 that creates nothing (requirement 19.17).
        if body == {"anchor_date": None}:
            assert response.status_code == httpx.codes.CREATED
            return
        assert_error(response, httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)
        assert service.manager.sandboxes() == ()


@pytest.mark.anyio
@pytest.mark.req("19.18")
async def test_the_concurrency_limit_answers_service_unavailable(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        made = [await service.prebuilt() for _ in range(MAX_SANDBOXES)]

        response = await service.create()

        assert_error(response, httpx.codes.SERVICE_UNAVAILABLE, AdminError.AT_CAPACITY)
        # No sandbox was created and the ones that are there are untouched (req 19.18).
        assert [sb.sandbox_id for sb in service.manager.sandboxes()] == made
        assert all(sb.status == "active" for sb in service.manager.sandboxes())
        # A sandbox that is frozen gives its place back (requirement 15.10).
        assert (await service.client.post(f"{SANDBOXES}/{made[0]}/freeze")).status_code == (
            httpx.codes.OK
        )
        assert (await service.create()).status_code == httpx.codes.CREATED


@pytest.mark.anyio
@pytest.mark.req("19.19")
async def test_a_snapshot_that_cannot_be_built_answers_500_naming_what_was_tried(
    env: Env, tmp_path: Path
) -> None:
    anchor = "2027-01-15"
    async with service_over(env, tmp_path) as service:
        kept = await service.prebuilt()
        service.snapshots.fail = True

        response = await service.create({"anchor_date": anchor})

        # The message names the initial dataset and the anchor date that were tried and
        # nothing of the exception itself (requirement 19.19).
        message = assert_error(
            response, httpx.codes.INTERNAL_SERVER_ERROR, AdminError.SNAPSHOT_FAILED
        )
        assert DATASET in message
        assert anchor in message
        assert BUILD_FAILED not in message
        # No sandbox was created, no directory was left behind and the existing one is
        # untouched (requirement 19.19).
        assert [sb.sandbox_id for sb in service.manager.sandboxes()] == [kept]
        assert sorted(path.name for path in service.manager.root.iterdir()) == [kept]


# --- GET /admin/sandboxes (requirement 19.11) --------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.11")
async def test_the_listing_holds_only_items_each_equal_to_the_metadata_interface(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        made = [await service.prebuilt() for _ in range(2)]

        body = assert_json(await service.listing(), httpx.codes.OK)

        # One object with items and nothing else, each item what GET /admin/sandboxes/{id}
        # answers for that sandbox (requirement 19.11).
        assert tuple(body) == ("items",)
        assert [item["sandbox_id"] for item in body["items"]] == made
        for sandbox_id, item in zip(made, body["items"], strict=True):
            assert item == assert_json(await service.metadata(sandbox_id), httpx.codes.OK)


@pytest.mark.anyio
@pytest.mark.req("19.11")
async def test_the_listing_orders_by_creation_time_and_then_by_sandbox_id(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path, step=0) as service:
        # Every reading of this service's clock is the same instant, so the creation times
        # are equal and the sandbox_id is what decides the order (requirement 19.11).
        made = [await service.prebuilt() for _ in range(3)]

        body = assert_json(await service.listing(), httpx.codes.OK)

        created = {item["created_at"] for item in body["items"]}
        assert len(created) == 1
        assert [item["sandbox_id"] for item in body["items"]] == sorted(made)


@pytest.mark.anyio
@pytest.mark.req("19.11")
async def test_the_listing_keeps_only_the_sandboxes_that_satisfy_every_parameter(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        made = [await service.prebuilt() for _ in range(3)]
        times = [
            assert_json(await service.metadata(sandbox_id), httpx.codes.OK)["created_at"]
            for sandbox_id in made
        ]
        assert (await service.client.post(f"{SANDBOXES}/{made[1]}/freeze")).status_code == (
            httpx.codes.OK
        )

        async def listed(**query: str) -> list[str]:
            body = assert_json(await service.listing(**query), httpx.codes.OK)
            return [item["sandbox_id"] for item in body["items"]]

        # Each parameter narrows the set; both ends of the time range are inclusive and a
        # parameter that was not given limits nothing (requirement 19.11).
        assert await listed() == made
        assert await listed(status="frozen") == [made[1]]
        assert await listed(status="active") == [made[0], made[2]]
        assert await listed(dataset=DATASET) == made
        assert await listed(created_from=times[1]) == [made[1], made[2]]
        assert await listed(created_to=times[1]) == [made[0], made[1]]
        assert await listed(created_from=times[1], created_to=times[1]) == [made[1]]
        assert await listed(created_from=times[0], created_to=times[2], status="frozen") == [
            made[1]
        ]
        assert await listed(created_from=times[2], created_to=times[0]) == []


@pytest.mark.anyio
@pytest.mark.req("19.17")
@pytest.mark.parametrize(
    "query",
    [
        pytest.param({"status": "gone"}, id="status-not-one-of-three"),
        pytest.param({"status": "ACTIVE"}, id="status-case-sensitive"),
        pytest.param({"dataset": "nope"}, id="dataset-not-configured"),
        pytest.param({"created_from": "2026-10-09"}, id="time-without-offset"),
        pytest.param({"created_to": "2026-10-09T10:15:32.481"}, id="instant-without-offset"),
        pytest.param({"created_from": "nonsense"}, id="not-a-time"),
        pytest.param({"undefined": "1"}, id="undefined-parameter"),
    ],
)
async def test_a_query_parameter_that_does_not_fit_the_model_is_a_bad_request(
    env: Env, tmp_path: Path, query: Mapping[str, str]
) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()

        response = await service.listing(**query)

        assert_error(response, httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)
        # No sandbox was changed by a request that was refused (requirement 19.17).
        assert service.sandbox(sandbox_id).status == "active"


# --- GET /admin/sandboxes/{id} (requirement 19.12) ---------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.12")
async def test_the_metadata_has_the_seven_fields_in_the_order_of_the_requirement(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()
        first = await service.open_session(sandbox_id)
        second = await service.open_session(sandbox_id)

        body = assert_json(await service.metadata(sandbox_id), httpx.codes.OK)

        # The seven keys of requirement 19.12, in its order; the sessions are the ones that
        # were bound to the sandbox, in first-binding order, several of them being allowed
        # on a prebuilt sandbox (requirement 15.3).
        assert tuple(body) == METADATA_KEYS
        assert body["sandbox_id"] == sandbox_id
        assert body["session_ids"] == [first.session_id, second.session_id]
        assert body["profile"] == f"{PROFILE}@{env.profile.definition.version}"
        assert body["dataset"] == f"{DATASET}@{env.dataset.version}"
        assert body["anchor_date"] == ANCHOR_DATE.isoformat()
        assert body["status"] == "active"
        # An ISO 8601 instant with a UTC offset, to the millisecond (requirement 19.12):
        # the first reading of the test clock, in the time zone the clock is configured
        # with (requirement 16.1).
        timezone = env.loaded.instance.clock.timezone
        assert timezone is not None
        assert body["created_at"] == INSTANT.astimezone(ZoneInfo(timezone)).isoformat(
            timespec="milliseconds"
        )
        assert datetime.fromisoformat(body["created_at"]).tzinfo is not None


@pytest.mark.anyio
@pytest.mark.req("19.12")
async def test_the_metadata_of_a_frozen_sandbox_is_answered_too(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()
        assert (await service.client.post(f"{SANDBOXES}/{sandbox_id}/freeze")).status_code == (
            httpx.codes.OK
        )

        body = assert_json(await service.metadata(sandbox_id), httpx.codes.OK)

        # A frozen sandbox stays readable until it is deleted (requirement 15.12).
        assert body["status"] == "frozen"


# --- the 404 of requirement 19.15 --------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.15")
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_a_sandbox_id_that_is_not_a_sandbox_is_not_found(
    env: Env, tmp_path: Path, method: str
) -> None:
    paths = {
        "GET": f"{SANDBOXES}/{MISSING_ID}",
        "POST": f"{SANDBOXES}/{MISSING_ID}/freeze",
        "DELETE": f"{SANDBOXES}/{MISSING_ID}",
    }
    async with service_over(env, tmp_path) as service:
        kept = await service.prebuilt()

        response = await service.client.request(method, paths[method])

        assert MISSING_ID in assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)
        assert service.sandbox(kept).status == "active"


@pytest.mark.anyio
@pytest.mark.req("19.15")
async def test_a_deleted_sandbox_is_not_found_from_then_on(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()
        assert (await service.client.delete(f"{SANDBOXES}/{sandbox_id}")).status_code == (
            httpx.codes.NO_CONTENT
        )

        # Every interface of that id answers 404 once the sandbox is gone (req 19.15).
        assert_error(
            await service.metadata(sandbox_id), httpx.codes.NOT_FOUND, AdminError.NOT_FOUND
        )
        assert_error(
            await service.client.post(f"{SANDBOXES}/{sandbox_id}/freeze"),
            httpx.codes.NOT_FOUND,
            AdminError.NOT_FOUND,
        )
        assert_error(
            await service.client.delete(f"{SANDBOXES}/{sandbox_id}"),
            httpx.codes.NOT_FOUND,
            AdminError.NOT_FOUND,
        )


@pytest.mark.anyio
@pytest.mark.req("19.15")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", "/admin", id="prefix-alone"),
        pytest.param("GET", "/admin/nope", id="path-of-no-interface"),
        pytest.param("GET", "/admin/health/extra", id="extra-segment"),
        pytest.param("GET", f"{SANDBOXES}/", id="trailing-slash"),
        pytest.param("GET", "/", id="root"),
        pytest.param("POST", HEALTH, id="health-is-get-only"),
        pytest.param("DELETE", SANDBOXES, id="listing-is-not-deletable"),
        pytest.param("POST", ALIASES_PATH, id="aliases-is-get-only"),
        pytest.param("DELETE", HEALTH, id="health-is-not-deletable"),
    ],
)
async def test_a_method_and_path_that_are_not_an_interface_are_not_found(
    env: Env, tmp_path: Path, method: str, path: str
) -> None:
    async with service_over(env, tmp_path) as service:
        response = await service.client.request(method, path)

        # 404 and not the 405 a framework router would write for a path that exists with
        # another method (requirement 19.15, admin docstring).
        assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)


@pytest.mark.anyio
@pytest.mark.req("19.15")
async def test_a_sandbox_path_with_a_method_of_another_interface_is_not_found(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()

        # The freeze of an existing sandbox is a POST; the same path with GET is 404, and
        # the sandbox is not frozen (requirement 19.15).
        response = await service.client.get(f"{SANDBOXES}/{sandbox_id}/freeze")

        assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)
        assert service.sandbox(sandbox_id).status == "active"


# --- POST .../freeze and DELETE .../{id} (requirement 19.13) -----------------------------


@pytest.mark.anyio
@pytest.mark.req("19.13")
async def test_freezing_answers_the_metadata_and_ends_the_sessions_of_that_sandbox(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        frozen = await service.prebuilt()
        kept = await service.prebuilt()
        first = await service.open_session(frozen)
        second = await service.open_session(frozen)
        other = await service.open_session(kept)

        body = assert_json(
            await service.client.post(f"{SANDBOXES}/{frozen}/freeze"), httpx.codes.OK
        )

        # The sandbox is frozen before the answer, which is the object the metadata
        # interface would answer with (requirement 19.13).
        assert tuple(body) == METADATA_KEYS
        assert body["status"] == "frozen"
        assert service.sandbox(frozen).status == "frozen"
        assert body == assert_json(await service.metadata(frozen), httpx.codes.OK)
        # Every session bound to it has ended, and no other (requirement 14.7).
        assert first.record.terminated and second.record.terminated
        assert first.record.transport.is_terminated
        assert (await first.ping()).status_code == httpx.codes.NOT_FOUND
        assert not other.record.terminated
        assert (await other.ping()).status_code == httpx.codes.OK


@pytest.mark.anyio
@pytest.mark.req("19.13")
async def test_freezing_a_frozen_sandbox_changes_nothing(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()
        first = assert_json(
            await service.client.post(f"{SANDBOXES}/{sandbox_id}/freeze"), httpx.codes.OK
        )

        second = assert_json(
            await service.client.post(f"{SANDBOXES}/{sandbox_id}/freeze"), httpx.codes.OK
        )

        # A second freeze is answered the same way and leaves the state as it was
        # (requirement 19.13).
        assert second == first
        assert service.sandbox(sandbox_id).status == "frozen"


@pytest.mark.anyio
@pytest.mark.req("19.13")
async def test_deleting_answers_204_with_no_body_and_removes_the_sandbox(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        deleted = await service.prebuilt()
        kept = await service.prebuilt()
        session = await service.open_session(deleted)
        other = await service.open_session(kept)
        directory = service.sandbox(deleted).directory

        response = await service.client.delete(f"{SANDBOXES}/{deleted}")

        # 204 and nothing at all in the body (requirement 19.13).
        assert response.status_code == httpx.codes.NO_CONTENT
        assert response.content == b""
        # The sandbox, its database, its trace and its metadata are gone (requirement 19.9)
        # and its sessions have ended, while the other sandbox is untouched (req 14.7).
        assert service.manager.get(deleted) is None
        assert not directory.exists()
        assert session.record.terminated
        assert (await session.ping()).status_code == httpx.codes.NOT_FOUND
        assert not other.record.terminated
        assert [sb.sandbox_id for sb in service.manager.sandboxes()] == [kept]


@pytest.mark.anyio
@pytest.mark.req("19.13")
async def test_a_frozen_sandbox_can_be_deleted(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()
        assert (await service.client.post(f"{SANDBOXES}/{sandbox_id}/freeze")).status_code == (
            httpx.codes.OK
        )

        response = await service.client.delete(f"{SANDBOXES}/{sandbox_id}")

        # A sandbox in any state may be deleted (requirement 15.9).
        assert response.status_code == httpx.codes.NO_CONTENT
        assert service.manager.get(sandbox_id) is None


@pytest.mark.anyio
@pytest.mark.req("19.17")
@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_a_body_on_an_interface_without_parameters_is_a_bad_request(
    env: Env, tmp_path: Path, method: str
) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()
        path = f"{SANDBOXES}/{sandbox_id}"
        target = f"{path}/freeze" if method == "POST" else path

        response = await service.client.request(
            method,
            target,
            content=b'{"name": "x"}',
            headers={"content-type": "application/json"},
        )

        # Freeze and delete define no parameters, so every key of a body is an undefined
        # key; the sandbox is neither frozen nor deleted (requirement 19.17).
        assert_error(response, httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)
        assert service.sandbox(sandbox_id).status == "active"


# --- the interfaces of this task are all served (requirement 19.1) -----------------------


@pytest.mark.anyio
@pytest.mark.req("19.1")
async def test_the_seven_lifecycle_interfaces_are_served(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()
        served = [
            ("GET", HEALTH, httpx.codes.OK),
            ("GET", ALIASES_PATH, httpx.codes.OK),
            ("POST", SANDBOXES, httpx.codes.CREATED),
            ("GET", SANDBOXES, httpx.codes.OK),
            ("GET", f"{SANDBOXES}/{sandbox_id}", httpx.codes.OK),
            ("POST", f"{SANDBOXES}/{sandbox_id}/freeze", httpx.codes.OK),
            ("DELETE", f"{SANDBOXES}/{sandbox_id}", httpx.codes.NO_CONTENT),
        ]

        # Every pair of the table answers its own status, and the last two are run in the
        # order a sandbox allows: frozen first, then deleted (requirement 19.1).
        for method, path, status in served:
            response = await service.client.request(method, path)
            assert response.status_code == status, f"{method} {path}: {response.text}"
