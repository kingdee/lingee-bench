"""serve on two real sockets: an official client on one port, the Admin API on the other.

Every other test of the gateway speaks HTTP to an ASGI application through
:class:`httpx.ASGITransport`, which opens no socket. This one starts
:mod:`erp_mock.gateway.serve` itself - two :class:`~erp_mock.gateway.serve.QuietServer`
instances on the event loop of the test, on sockets bound to a free port each - and asks of
it what no in-process transport can show:

- the official MCP Python SDK client, over Streamable HTTP and a real connection, completes
  the handshake, lists the tools of the profile and calls ``business_context_get``
  (requirement 22.1);
- the Admin API answers ``GET /admin/health`` with 200 on a port of its own, bound to
  127.0.0.1 whatever ``--host`` says, and the MCP port answers ``/admin/health`` with 404
  (requirement 19.1);
- the two middleware chains of design.md 7.4 are in front of the two routers: the Origin
  and the body size on the MCP port, the Admin token and the body size on the Admin port;
- the stop ends both servers, every session and the janitor, in that order, and serve
  returns the exit code of a service that was asked to stop (requirement 22.1).

``--port 0`` is how a test gets a port nobody else is using, and
:attr:`~erp_mock.gateway.serve.ServeRuntime.endpoints` is how it learns which one it got
(:func:`running_service`). The runtime is given no signals to watch: a real SIGINT would go
to pytest, so the exit path is triggered through
:meth:`~erp_mock.gateway.serve.ServeRuntime.stop`, which is the path a signal takes as well
(recorded in tests/probes/test_uvicorn.py and in the serve docstring).

What is replaced, and why - the same doubles the unit tests of the gateway use:

- the Snapshot source (:class:`Snapshots`): one Snapshot file built once for the module,
  from the standard basic data of :mod:`tests.helpers.engine_env`, so a call has records to
  read and no test builds a dataset. The service is given the source rather than building
  one, which is what keeps the seed layer out of the gateway (serve docstring);
- the clock (:class:`Ticker`, :func:`stepping_clock`): no test reads the system clock
  (requirement 25.10). The monotonic source is the test's own, which is what lets the idle
  threshold of a sandbox pass without waiting for it - and so shows that the janitor runs
  in the serve task group and stops with it;
- the initial dataset of the loaded instance: loading step 8 is not run here, so the
  dataset it would leave behind is built by hand (:func:`loaded_with_dataset`).

The business configuration is the engine test fixture (:mod:`tests.helpers.fixtures`),
which loads in milliseconds; the example instance and the twelve-step business flow over it
belong to the integration tests of task 26.1. No test of this file waits for a fixed
duration: every state it waits for is polled with a deadline (:func:`wait_until`).
"""

import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import anyio
import httpx
import pytest
from anyio.abc import TaskStatus
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.instance import DatasetDef
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import SERVER_NAME
from erp_mock.gateway.router import SESSION_ID_HEADER
from erp_mock.gateway.sandbox import JANITOR_INTERVAL_SECONDS, SandboxLimits, SandboxRuntime
from erp_mock.gateway.serve import (
    ADMIN_HOST,
    EXIT_BIND_FAILED,
    EXIT_OK,
    EnvSettings,
    GatewayState,
    ServeEndpoints,
    ServeOptions,
    ServeRuntime,
    bind_socket,
)
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.loader.yaml_io import load_yaml
from erp_mock.state.schema import StorageSchema, build_schema
from erp_mock.state.snapshot import build_snapshot_db, finalize_snapshot
from erp_mock.tools.layer import ToolLayer
from tests.helpers.engine_env import CURRENT_USER, create_basic_data
from tests.helpers.fixtures import (
    ANCHOR_DATE,
    DATASET,
    INSTANCE_DIR,
    INSTANCE_NAME,
    fixture_loaded,
)

# Upper bound for every test: a hang fails the test instead of blocking the run. The whole
# module stays well below it, the only waits being the two polled ones.
TIMEOUT_S: Final = 15
CONDITION_TIMEOUT_S: Final = 5.0
POLL_SECONDS: Final = 0.02

# The host and the two ports every service of this file is started on: port 0 twice, so the
# operating system hands out two ports nobody else is using (module docstring).
HOST: Final = "127.0.0.1"
FREE_PORT: Final = 0

# The Admin token of these services. A real one is 256 bits of system randomness written to
# the token file by the CLI (requirement 23.5); what matters here is that the Admin port
# checks every request against this value and against nothing else (requirement 19.2).
ADMIN_TOKEN: Final = "test-admin-token"

# The body size limit of these services, small enough that a test can exceed it in one
# line and far above every message the SDK client sends (requirement 23.7).
MAX_BODY_BYTES: Final = 4096

# The thresholds every service of this file runs with: seconds of its own monotonic source
# rather than the minutes of the real service (requirement 15.11).
IDLE_SECONDS: Final = 1
FREEZE_SECONDS: Final = 2
LIMITS: Final = SandboxLimits(
    idle_seconds=IDLE_SECONDS, freeze_seconds=FREEZE_SECONDS, retention_seconds=900, max_sandboxes=4
)

# The instant the first reading of the test clock returns; the nth reading is this plus
# n - 1 seconds, so the ``ts`` of two events differ and neither is the system time.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# The definition file of the initial dataset of the fixture, relative to its instance
# directory, as loading step 4 reads it.
DATASET_FILE: Final = f"datasets/{DATASET}.yaml"

# The addresses of this service: the MCP endpoint of requirement 14.2, the health interface
# of requirement 19.9 and the sandboxes interface, which is the one POST of the Admin API
# the body size test needs.
MCP_PATH: Final = "/mcp"
HEALTH_PATH: Final = "/admin/health"
SANDBOXES_PATH: Final = "/admin/sandboxes"

# The one built-in tool, which every profile exposes and which reads the business context
# of the sandbox (requirement 16.10).
CONTEXT_TOOL: Final = "business_context_get"

# The headers the official client puts on every POST (mcp.client.streamable_http), which
# the raw requests of this file carry as well, and the protocol version it announces.
JSON_AND_SSE: Final = "application/json, text/event-stream"
POST_HEADERS: Final[Mapping[str, str]] = {
    "content-type": "application/json",
    "accept": JSON_AND_SSE,
}
PROTOCOL_VERSION: Final = "2025-11-25"

# The keys of ``GET /admin/health``, in the order of requirement 19.9.
HEALTH_KEYS: Final = (
    "framework_version",
    "meta_model_version",
    "instance",
    "packs",
    "profiles",
    "datasets",
)

# An Origin that no rule of requirement 23.1 lets through: neither a loopback host nor one
# of the allowed origins, of which these services configure none.
FORBIDDEN_ORIGIN: Final = "https://evil.example"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- the test clock (requirement 25.10) --------------------------------------------------


@dataclass(slots=True)
class Ticker:
    """A monotonic source the test moves itself (requirement 25.10).

    Every reading returns :attr:`seconds`, which :meth:`advance` raises. The janitor of the
    Sandbox_Manager measures the idle, freeze and retention thresholds against this, so a
    test makes a threshold pass by moving the clock rather than by waiting.
    """

    seconds: float = 1000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> float:
        """Move the clock forward and return the new reading."""
        self.seconds += seconds
        return self.seconds


def stepping_clock(ticker: Ticker) -> SystemClock:
    """A clock whose nth instant is :data:`INSTANT` plus n - 1 seconds, over ``ticker``."""
    readings = iter(range(1 << 20))
    return SystemClock(now=lambda: INSTANT + timedelta(seconds=next(readings)), monotonic=ticker)


# --- the instance configuration and the one Snapshot of this file ------------------------


def loaded_with_dataset() -> LoadedInstance:
    """The fixture configuration with the initial dataset loading step 8 would leave.

    The steps before step 8 leave no :class:`~erp_mock.loader.instance.DatasetModel`
    behind, and both routers look the name of a dataset up in exactly that map
    (requirements 14.3 and 19.9), so the model is built here the way step 8 builds it. The
    Snapshot of the model is not set: the sandboxes of this file come from
    :class:`Snapshots`, as the service's come from its cache.
    """
    loaded = fixture_loaded()
    definition = DatasetDef.model_validate(load_yaml(INSTANCE_DIR / DATASET_FILE))
    dataset = DatasetModel(definition=definition, file=DATASET_FILE, current_user=CURRENT_USER)
    return replace(loaded, datasets=FrozenMap({dataset.name: dataset}))


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

    @property
    def instance_ref(self) -> str:
        """The instance as ``GET /admin/health`` writes it (requirement 19.9)."""
        return f"{self.loaded.instance.instance}@{self.loaded.instance.version}"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Env:
    """The fixture instance, its tools layer and one Snapshot of its initial dataset."""
    loaded = loaded_with_dataset()
    layer = ToolLayer(loaded)
    schema = build_schema(loaded.domain)
    return Env(
        loaded=loaded,
        schema=schema,
        layer=layer,
        snapshot=build_snapshot(tmp_path_factory.mktemp("snapshot"), schema, layer),
    )


def build_snapshot(directory: Path, schema: StorageSchema, layer: ToolLayer) -> Path:
    """Build the one Snapshot of this file below ``directory`` and return its path.

    The tables of the fixture and the standard basic data of
    :mod:`tests.helpers.engine_env` in them - the organization, the current user, the units
    and the basic records a call points at - created through the engine the way a seed build
    creates them, and written out as a Snapshot.
    """
    build = directory / "build.sqlite"
    snapshot = directory / "snapshot.sqlite"
    store = build_snapshot_db(build, schema)
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
    service's own source would have looked up in the Snapshot cache or built.
    """

    db: Path
    asked: list[tuple[str, date]] = field(default_factory=list)

    @contextmanager
    def snapshot(self, dataset: DatasetModel, anchor: date) -> Iterator[Path]:
        self.asked.append((dataset.name, anchor))
        yield self.db


# --- the service under test ---------------------------------------------------------------


@dataclass(slots=True)
class Service:
    """A running serve process of this event loop, and what a test asks of it."""

    runtime: ServeRuntime
    endpoints: ServeEndpoints
    exit_codes: list[int]
    ticker: Ticker
    snapshots: Snapshots

    @property
    def state(self) -> GatewayState:
        """What the two ports share (design.md 7.4)."""
        return self.runtime.state

    @property
    def mcp_url(self) -> str:
        """The address of the MCP endpoint, as a client is given it (requirement 14.2)."""
        return f"{self.endpoints.mcp.url}{MCP_PATH}"

    def admin_client(self, *, token: str | None = ADMIN_TOKEN) -> httpx.AsyncClient:
        """An HTTP client for the Admin port; ``token`` of None carries no Authorization."""
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return httpx.AsyncClient(base_url=self.endpoints.admin.url, headers=headers)

    def mcp_client(self) -> httpx.AsyncClient:
        """An HTTP client for the MCP port, for the requests the SDK client cannot send."""
        return httpx.AsyncClient(base_url=self.endpoints.mcp.url)

    def sandboxes(self) -> tuple[SandboxRuntime, ...]:
        """The sandboxes of this process, in creation order."""
        return self.state.manager.sandboxes()

    def one_sandbox(self) -> SandboxRuntime:
        """The single sandbox of this process; fails where there is not exactly one."""
        sandboxes = self.sandboxes()
        assert len(sandboxes) == 1, [sb.sandbox_id for sb in sandboxes]
        return sandboxes[0]


def serve_settings(limits: SandboxLimits = LIMITS) -> EnvSettings:
    """What the environment would have settled for a service of this file (req 23.9)."""
    return EnvSettings(
        admin_token=ADMIN_TOKEN,
        max_body_bytes=MAX_BODY_BYTES,
        sandbox=limits,
    )


def gateway_state(env: Env, options: ServeOptions, settings: EnvSettings) -> GatewayState:
    """The state of one serve process over the fixture instance and the test's doubles."""
    return GatewayState(
        loaded=env.loaded,
        options=options,
        env=settings,
        snapshots=Snapshots(env.snapshot),
        clock=stepping_clock(Ticker()),
        schema=env.schema,
        layer=env.layer,
    )


@asynccontextmanager
async def running_service(
    env: Env, data_dir: Path, *, settings: EnvSettings | None = None
) -> AsyncIterator[Service]:
    """Start serve on two free ports and yield it; the block ends with the service stopped.

    The whole of task 23.4: both applications over one
    :class:`~erp_mock.gateway.serve.GatewayState`, two uvicorn servers on pre-bound sockets
    and the serve task group that carries the janitor and the session tasks. The runtime
    watches no signal (module docstring), so the stop of the block is
    :meth:`~erp_mock.gateway.serve.ServeRuntime.stop`; leaving the block waits for serve to
    return, and :attr:`Service.exit_codes` then holds the one exit code it returned.
    """
    ticker = Ticker()
    snapshots = Snapshots(env.snapshot)
    options = ServeOptions(data_dir=data_dir, host=HOST, port=FREE_PORT, admin_port=FREE_PORT)
    state = GatewayState(
        loaded=env.loaded,
        options=options,
        env=serve_settings() if settings is None else settings,
        snapshots=snapshots,
        clock=stepping_clock(ticker),
        schema=env.schema,
        layer=env.layer,
    )
    runtime = ServeRuntime(state=state, options=options, signals=())
    exit_codes: list[int] = []

    async def run(*, task_status: TaskStatus[ServeEndpoints | None]) -> None:
        exit_codes.append(await runtime.serve(task_status=task_status))

    with anyio.fail_after(TIMEOUT_S):
        async with anyio.create_task_group() as tg:
            endpoints = await tg.start(run)
            assert endpoints is not None
            try:
                yield Service(
                    runtime=runtime,
                    endpoints=endpoints,
                    exit_codes=exit_codes,
                    ticker=ticker,
                    snapshots=snapshots,
                )
            finally:
                runtime.stop()


async def wait_until(
    condition: Callable[[], bool], *, timeout: float = CONDITION_TIMEOUT_S
) -> None:
    """Poll ``condition`` until it holds, and fail the test where it does not in time."""
    with anyio.fail_after(timeout):
        while not condition():
            await anyio.sleep(POLL_SECONDS)


def free_port() -> int:
    """A port of the loopback address that nothing held a moment ago.

    Bound the way serve binds its sockets and released again, so a test can name a port in
    its options and then check whether serve left it bound (:func:`bind_socket`).
    """
    with closing(bind_socket(HOST, FREE_PORT)) as sock:
        return int(sock.getsockname()[1])


# --- the messages a raw client sends ------------------------------------------------------


def initialize_body(request_id: int = 1) -> str:
    """The initialize request the official client opens a session with, as JSON text."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.0.0"},
            },
        }
    )


async def open_session(client: httpx.AsyncClient) -> str:
    """Open one session on ``/mcp`` with a raw POST and return its session id.

    What the SDK client's handshake amounts to, without the client: a test that wants a
    session to still be open when the service stops cannot hold an SDK client open, whose
    own shutdown would send a DELETE to a port that is no longer there.
    """
    response = await client.post(MCP_PATH, content=initialize_body(), headers=dict(POST_HEADERS))
    assert response.status_code == httpx.codes.OK, response.text
    return response.headers[SESSION_ID_HEADER]


# --- the two ports (requirements 19.1 and 22.1) -------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.1", "22.1")
async def test_an_official_client_and_the_admin_api_are_served_on_the_two_ports(
    env: Env, tmp_path: Path
) -> None:
    async with running_service(env, tmp_path) as service:
        # The ports the operating system handed out are two different ones, and the Admin
        # port is on the loopback address whatever --host says (requirement 19.1).
        assert service.endpoints.mcp.host == HOST
        assert service.endpoints.admin.host == ADMIN_HOST == "127.0.0.1"
        assert service.endpoints.mcp.port != service.endpoints.admin.port
        assert 0 not in (service.endpoints.mcp.port, service.endpoints.admin.port)

        # The official SDK client over a real connection: the handshake, the tools of the
        # profile and one call that reads the sandbox (requirement 22.1).
        async with (
            streamable_http_client(service.mcp_url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            initialized = await session.initialize()
            listed = await session.list_tools()
            called = await session.call_tool(CONTEXT_TOOL, {})

        assert initialized.serverInfo.name == SERVER_NAME
        assert [tool.name for tool in listed.tools] == list(env.profile.tool_names)
        assert called.isError is False
        assert called.structuredContent is not None
        assert called.structuredContent["business_date"] == ANCHOR_DATE.isoformat()

        # The call ran against a sandbox of this process, created from the Snapshot of the
        # initial dataset at the anchor date of the configuration (requirement 15.1).
        sandbox = service.one_sandbox()
        assert service.snapshots.asked == [(DATASET, ANCHOR_DATE)]
        assert (sandbox.origin, sandbox.anchor_date) == ("auto", ANCHOR_DATE)

        # The Admin API on its own port (requirement 19.9).
        async with service.admin_client() as admin:
            health = await admin.get(HEALTH_PATH)
        assert health.status_code == httpx.codes.OK
        body = health.json()
        assert tuple(body) == HEALTH_KEYS
        assert body["instance"] == env.instance_ref
        assert body["instance"].startswith(f"{INSTANCE_NAME}@")


@pytest.mark.anyio
@pytest.mark.req("19.1")
async def test_the_mcp_port_does_not_serve_the_admin_api(env: Env, tmp_path: Path) -> None:
    async with running_service(env, tmp_path) as service:
        async with service.mcp_client() as client:
            health = await client.get(HEALTH_PATH)
            sandboxes = await client.post(SANDBOXES_PATH, json={})
        # Not one of the five addresses of requirement 14.2, so a 404 of the MCP router -
        # and no Admin interface of this service is reachable without the Admin token.
        assert (health.status_code, sandboxes.status_code) == (
            httpx.codes.NOT_FOUND,
            httpx.codes.NOT_FOUND,
        )
        assert service.sandboxes() == ()


@pytest.mark.anyio
@pytest.mark.req("19.1", "22.1")
async def test_both_ports_are_the_middleware_chains_of_their_routers(
    env: Env, tmp_path: Path
) -> None:
    oversized = json.dumps({"filler": "x" * (MAX_BODY_BYTES + 1)})
    async with running_service(env, tmp_path) as service:
        async with service.mcp_client() as client:
            refused_origin = await client.post(
                MCP_PATH,
                content=initialize_body(),
                headers={**POST_HEADERS, "origin": FORBIDDEN_ORIGIN},
            )
            too_large = await client.post(MCP_PATH, content=oversized, headers=dict(POST_HEADERS))
        async with service.admin_client(token=None) as anonymous:
            unauthorized = await anonymous.get(HEALTH_PATH)
        async with service.admin_client() as admin:
            admin_too_large = await admin.post(SANDBOXES_PATH, content=oversized)

        # The MCP chain: the Origin before everything, the body size before the router.
        assert refused_origin.status_code == httpx.codes.FORBIDDEN
        assert too_large.status_code == httpx.codes.REQUEST_ENTITY_TOO_LARGE
        # The Admin chain: the token before every path, the body size before the table.
        assert unauthorized.status_code == httpx.codes.UNAUTHORIZED
        assert admin_too_large.status_code == httpx.codes.REQUEST_ENTITY_TOO_LARGE
        # Nothing any guard refused reached a session or a sandbox (requirement 23.7).
        assert service.sandboxes() == ()
        assert len(service.state.router.sessions) == 0

    # The Admin chain cannot be assembled without a token at all: an empty one would let
    # ``Authorization: Bearer `` through (requirements 23.4 and 23.5).
    with pytest.raises(ValueError, match="Admin 令牌"):
        EnvSettings(admin_token="")


# --- the stop (requirement 22.1) ----------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("22.1")
async def test_the_stop_ends_both_servers_every_session_and_the_janitor(
    env: Env, tmp_path: Path
) -> None:
    async with running_service(env, tmp_path) as service:
        async with service.mcp_client() as client:
            session_id = await open_session(client)
        sandbox = service.one_sandbox()
        assert service.state.router.sessions.get(session_id) is not None

        # The janitor runs in the serve task group: the idle threshold of the sandbox
        # passes on the test's clock and the sandbox turns idle within a scan or two
        # (requirement 15.5).
        service.ticker.advance(IDLE_SECONDS + 1)
        await wait_until(lambda: sandbox.status == "idle")

        service.runtime.stop()

    # Leaving the block waited for serve to return, which it does only after the five steps
    # of the stop: both servers returned, every session ended, the janitor was cancelled
    # and the Sandbox_Manager was closed.
    assert service.exit_codes == [EXIT_OK]
    assert len(service.state.router.sessions) == 0
    assert service.state.router.sessions.get(session_id) is None
    assert sandbox.store.is_open is False

    # The janitor is gone with the task group: the freeze threshold passes and several scan
    # intervals go by without the sandbox changing (requirement 15.7 is nobody's job here).
    service.ticker.advance(FREEZE_SECONDS + 1)
    await anyio.sleep(JANITOR_INTERVAL_SECONDS * 3)
    assert sandbox.status == "idle"

    # Both listeners are closed, so neither port answers any more.
    async with httpx.AsyncClient() as client:
        for url in (service.mcp_url, f"{service.endpoints.admin.url}{HEALTH_PATH}"):
            with pytest.raises(httpx.ConnectError):
                await client.get(url)


@pytest.mark.anyio
@pytest.mark.req("22.1")
async def test_a_port_that_is_taken_ends_serve_with_the_bind_failure_code(
    env: Env, tmp_path: Path
) -> None:
    with closing(bind_socket(HOST, FREE_PORT)) as taken:
        taken.listen(1)
        port = taken.getsockname()[1]
        options = ServeOptions(data_dir=tmp_path, host=HOST, port=port, admin_port=FREE_PORT)
        state = gateway_state(env, options, serve_settings())
        runtime = ServeRuntime(state=state, options=options, signals=())

        with anyio.fail_after(TIMEOUT_S):
            exit_code = await runtime.serve()

        # Nothing listened, nothing was created, and the caller is told which it was by
        # the exit code alone (requirement 22.8).
        assert exit_code == EXIT_BIND_FAILED
        assert runtime.endpoints is None
        assert state.manager.sandboxes() == ()
        assert not (tmp_path / "sandboxes").exists()


@pytest.mark.anyio
@pytest.mark.req("19.1", "22.1")
async def test_a_taken_admin_port_ends_serve_and_closes_the_mcp_socket_again(
    env: Env, tmp_path: Path
) -> None:
    """The Admin socket is bound on :data:`ADMIN_HOST`, and a failed bind leaves none open."""
    mcp_port = free_port()
    with closing(bind_socket(ADMIN_HOST, FREE_PORT)) as taken:
        taken.listen(1)
        options = ServeOptions(
            data_dir=tmp_path,
            host=HOST,
            port=mcp_port,
            admin_port=int(taken.getsockname()[1]),
        )
        state = gateway_state(env, options, serve_settings())
        runtime = ServeRuntime(state=state, options=options, signals=())

        with anyio.fail_after(TIMEOUT_S):
            exit_code = await runtime.serve()

        # The Admin port of the options was taken on 127.0.0.1 alone, which is where serve
        # binds it whatever --host says (requirement 19.1).
        assert exit_code == EXIT_BIND_FAILED
        assert runtime.endpoints is None
        assert state.manager.sandboxes() == ()

    # The MCP socket is bound before the Admin one and closed again when that fails, so its
    # port can be bound once more.
    with closing(bind_socket(HOST, mcp_port)) as again:
        assert int(again.getsockname()[1]) == mcp_port
