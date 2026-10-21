"""Which requests the MCP endpoint serves, which it refuses, and what that does to sandboxes.

:mod:`erp_mock.gateway.router` is an ASGI application, so every test here speaks HTTP to
it through :class:`httpx.ASGITransport` - no socket, no uvicorn - and reads the status code,
the headers and the body a client would see. That is the whole observable behaviour of the
router: requirement 14.3 is a 404, requirement 14.8 a 400, requirement 15.10 a 503 and
requirement 15.14 a 500 with one JSON-RPC error object in it, and the sandboxes and trace
files behind them say what the refusal left behind (requirement 17.9).

The service under test is the real thing short of the two middlewares and the Admin API,
which are tasks 23.1 to 23.4: a real :class:`~erp_mock.gateway.sandbox.SandboxManager` over
a data directory of the test, the real :class:`~erp_mock.gateway.handlers.SessionHandlers`
over the tools layer of the engine test fixture, and the router in front of them, its
session tasks in a task group of the test (:func:`service_over`). A session is therefore a
real SDK transport with a real low-level ``Server`` behind it, and a tools/call of one
reads the sandbox database and writes its trace event.

What is replaced, and why:

- the Snapshot source (:class:`Snapshots`): one Snapshot file built once for the module,
  from the standard basic data of :mod:`tests.helpers.engine_env`, so a call has records to
  point at and no test builds a dataset. ``fail`` is what makes a creation fail the way a
  Snapshot that cannot be built fails it (requirement 15.14);
- the clock (:class:`Ticker`, :func:`stepping_clock`): no test reads the system clock
  (requirement 25.10). The fixture's clock mode is fixed, so the anchor date of every
  sandbox is :data:`~tests.helpers.fixtures.ANCHOR_DATE` whatever the day of the run;
- the initial datasets of the loaded instance: loading step 8 is not run here, so the
  dataset it would leave behind is built by hand and put into the configuration
  (:func:`loaded_with_dataset`), which is what the router looks a dataset name up in.

The business configuration is the engine test fixture (:mod:`tests.helpers.fixtures`), not
the example instance: it exposes the same kinds of tools and loads in milliseconds, and the
example instance belongs to the contract and integration tests.
"""

import json
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import anyio
import httpx
import pytest

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.instance import DatasetDef
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import SERVER_NAME, SessionHandlers
from erp_mock.gateway.router import (
    ALLOWED_HTTP_METHODS,
    INVALID_CONTENT_TYPE,
    NOT_ACCEPTABLE_MESSAGE,
    SESSION_ID_HEADER,
    SNAPSHOT_FAILED_MESSAGE,
    UNSUPPORTED_MEDIA_TYPE_MESSAGE,
    McpRouter,
    SessionRecord,
    sandbox_path,
)
from erp_mock.gateway.sandbox import (
    SandboxLimits,
    SandboxManager,
    SandboxRuntime,
)
from erp_mock.gateway.trace import TRACE_FILENAME, EventName
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
    PROFILE,
    fixture_loaded,
)

# Upper bound for every test: a hang fails the test instead of blocking the run.
TIMEOUT_S: Final = 10

# Where the httpx client sends its requests; the ASGI transport never opens a socket, so
# the host and the port are only what ``scope["server"]`` and the Host header carry.
BASE_URL: Final = "http://127.0.0.1:8000"

# The four addresses whose initialize creates a sandbox, and the names they carry
# (requirement 14.2).
AUTO_PATH: Final = "/mcp"
PROFILE_PATH: Final = f"/p/{PROFILE}/mcp"
DATASET_PATH: Final = f"/d/{DATASET}/mcp"
BOTH_PATH: Final = f"/p/{PROFILE}/d/{DATASET}/mcp"
CREATING_PATHS: Final = (AUTO_PATH, PROFILE_PATH, DATASET_PATH, BOTH_PATH)

# The definition file of the initial dataset of the fixture, relative to its instance
# directory, as loading step 4 reads it.
DATASET_FILE: Final = f"datasets/{DATASET}.yaml"

# The instant the first reading of the test clock returns; the nth reading is this plus
# n - 1 seconds, so the ``ts`` of two events differ and neither is the system time.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# The thresholds every service of this file runs with: seconds of its own monotonic source
# rather than the five minutes of the service, and two concurrent sandboxes, which is what
# makes the 503 of requirement 15.10 reachable with two sessions.
MAX_SANDBOXES: Final = 2
LIMITS: Final = SandboxLimits(
    idle_seconds=60, freeze_seconds=300, retention_seconds=900, max_sandboxes=MAX_SANDBOXES
)

# The headers the official client puts on every request (mcp.client.streamable_http) and
# the protocol version it announces.
JSON_AND_SSE: Final = "application/json, text/event-stream"
POST_HEADERS: Final[Mapping[str, str]] = {
    "content-type": "application/json",
    "accept": JSON_AND_SSE,
}
PROTOCOL_VERSION: Final = "2025-11-25"

# The JSON-RPC codes the router answers with: -32600 for every HTTP-level refusal, as the
# SDK's transport does, and -32603 for a sandbox that could not be created (req 15.14).
INVALID_REQUEST: Final = -32600
INTERNAL_ERROR: Final = -32603

# The ``id`` of an answer to a request whose body the router did not read.
SERVER_ERROR_ID: Final = "server-error"

# The one built-in tool, which every profile exposes and which reads the business context
# of the sandbox (requirement 16.10); the end-to-end session of this file calls it.
CONTEXT_TOOL: Final = "business_context_get"

# What the Snapshot source raises when the Snapshot of a dataset cannot be built; the seed
# layer's own exception is a SeedError, which the gateway does not import (router docstring).
BUILD_FAILED: Final = "数据集的不变量不成立"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- the test clock (requirement 25.10) --------------------------------------------------


@dataclass(slots=True)
class Ticker:
    """A monotonic source the test moves itself (requirement 25.10).

    Every reading returns :attr:`seconds`, which :meth:`advance` raises. Nothing of the
    router measures time; the Sandbox_Manager reads this for the idle timing of every
    request, and a test that wanted to drive a threshold would move it.
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


def loaded_with_dataset() -> LoadedInstance:
    """The fixture configuration with the initial dataset loading step 8 would leave.

    The steps before step 8 leave no :class:`~erp_mock.loader.instance.DatasetModel`
    behind, and the router looks the name of an address up in exactly that map
    (requirement 14.3), so the model is built here the way step 8 builds it: the validated
    definition, the file it came from and the current user the instance configuration
    resolves for it (requirement 20.5). The Snapshot of the model is not set: the sandboxes
    of this file are created from :class:`Snapshots`, as the service creates them from its
    cache.
    """
    loaded = fixture_loaded()
    definition = DatasetDef.model_validate(load_yaml(INSTANCE_DIR / DATASET_FILE))
    dataset = DatasetModel(definition=definition, file=DATASET_FILE, current_user=CURRENT_USER)
    return replace(loaded, datasets=FrozenMap({dataset.name: dataset}))


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


def build_snapshot(directory: Path, env_schema: StorageSchema, layer: ToolLayer) -> Path:
    """Build the one Snapshot of this file below ``directory`` and return its path.

    The tables of the fixture and the standard basic data of
    :mod:`tests.helpers.engine_env` in them - the organization, the current user, the
    units and the basic records a call points at - created through the engine the way a
    seed build creates them, and written out as a Snapshot.
    """
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
    built fails it (requirement 15.14).
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


# --- the service under test ---------------------------------------------------------------


@dataclass(slots=True)
class Service:
    """A router with its manager, its sandboxes and an HTTP client that speaks to it."""

    router: McpRouter
    manager: SandboxManager
    client: httpx.AsyncClient
    snapshots: Snapshots
    ticker: Ticker
    env: Env

    async def post(
        self, path: str, body: Any, headers: Mapping[str, str] = POST_HEADERS
    ) -> httpx.Response:
        """POST ``body`` (JSON-encoded unless it is a str already) with exactly ``headers``."""
        content = body if isinstance(body, str) else json.dumps(body)
        return await self.client.post(path, content=content.encode(), headers=dict(headers))

    async def open(self, path: str = AUTO_PATH, request_id: int = 1) -> "Session":
        """Open a session on ``path`` with an initialize the transport accepts."""
        response = await self.post(path, initialize_body(request_id))
        assert response.status_code == httpx.codes.OK, response.text
        session_id = response.headers[SESSION_ID_HEADER]
        record = self.router.sessions.get(session_id)
        assert record is not None
        return Session(
            service=self,
            session_id=session_id,
            path=path,
            record=record,
            result=response.json()["result"],
        )

    async def prebuilt(self) -> SandboxRuntime:
        """A sandbox the Admin API would have prebuilt (requirement 19.3)."""
        return await self.manager.create(self.env.profile, self.env.dataset, origin="prebuilt")

    def sandbox_ids(self) -> list[str]:
        """The sandboxes of this service, in creation order."""
        return [sb.sandbox_id for sb in self.manager.sandboxes()]


@dataclass(slots=True)
class Session:
    """One open session, and what a test asks of it."""

    service: Service
    session_id: str
    path: str
    record: SessionRecord
    result: dict[str, Any]

    @property
    def sandbox(self) -> SandboxRuntime:
        """The sandbox the session is bound to (requirements 15.1 and 15.3)."""
        return self.record.sandbox

    def headers(self, **extra: str) -> dict[str, str]:
        """The headers of a request inside this session, as the official client sends them."""
        return {
            **POST_HEADERS,
            SESSION_ID_HEADER: self.session_id,
            "mcp-protocol-version": PROTOCOL_VERSION,
            **extra,
        }

    async def send(self, body: Any, path: str | None = None) -> httpx.Response:
        """POST ``body`` inside this session, on its own path unless another is given."""
        return await self.service.post(self.path if path is None else path, body, self.headers())

    async def request(
        self, request_id: int, method: str, params: Mapping[str, Any] | None = None
    ) -> httpx.Response:
        """Send one JSON-RPC request inside this session."""
        return await self.send(request_body(request_id, method, params))

    async def call(self, request_id: int, tool: str, args: Mapping[str, Any]) -> httpx.Response:
        """Send one tools/call inside this session."""
        return await self.request(request_id, "tools/call", {"name": tool, "arguments": dict(args)})

    async def delete(self, path: str | None = None) -> httpx.Response:
        """End the session the way a client ends it (requirements 15.7 and 15.8)."""
        target = self.path if path is None else path
        return await self.service.client.delete(target, headers=self.headers())

    async def get(self, accept: str = JSON_AND_SSE) -> httpx.Response:
        """Ask for the server-initiated stream this service does not offer (req 14.11)."""
        return await self.service.client.get(self.path, headers=self.headers(accept=accept))

    def events(self) -> list[dict[str, Any]]:
        """The events of the trace file of the sandbox, one per line (requirement 17.1)."""
        return events_of(self.sandbox)


def events_of(sandbox: SandboxRuntime) -> list[dict[str, Any]]:
    """The events in the trace file of ``sandbox``, or none where it has written none."""
    path = sandbox.directory / TRACE_FILENAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@asynccontextmanager
async def service_over(
    env: Env, data_dir: Path, limits: SandboxLimits = LIMITS
) -> AsyncIterator[Service]:
    """A router over a Sandbox_Manager in ``data_dir``, with an HTTP client in front of it.

    What task 23.4 will assemble, short of the middlewares, the Admin port and the two
    uvicorn servers: the session tasks run in a task group of this block, which is the
    serve task group of the service (design.md 7.2), and every session is ended and every
    sandbox closed when the block returns.
    """
    ticker = Ticker()
    snapshots = Snapshots(env.snapshot)
    manager = SandboxManager(
        data_dir=data_dir,
        loaded=env.loaded,
        schema=env.schema,
        snapshots=snapshots,
        clock=stepping_clock(ticker),
        limits=limits,
    )
    router = McpRouter(SessionHandlers(manager, env.layer), env.loaded)
    try:
        with anyio.fail_after(TIMEOUT_S):
            async with anyio.create_task_group() as tg:
                router.attach(tg)
                asgi = httpx.ASGITransport(app=router)
                async with httpx.AsyncClient(transport=asgi, base_url=BASE_URL) as client:
                    # httpx adds "Accept: */*" to every request by default; drop it so
                    # that each request carries exactly the headers the test gives it.
                    del client.headers["accept"]
                    try:
                        yield Service(
                            router=router,
                            manager=manager,
                            client=client,
                            snapshots=snapshots,
                            ticker=ticker,
                            env=env,
                        )
                    finally:
                        await router.shutdown()
                        tg.cancel_scope.cancel()
    finally:
        manager.close()


# --- the messages a client sends ----------------------------------------------------------


def initialize_body(request_id: int = 1) -> dict[str, Any]:
    """The initialize request the official client opens a session with."""
    return request_body(
        request_id,
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.0.0"},
        },
    )


def request_body(
    request_id: int, method: str, params: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """One JSON-RPC request."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = dict(params)
    return body


INITIALIZED: Final[Mapping[str, Any]] = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}


# --- what a refused request looks like ----------------------------------------------------


def assert_json_error(
    response: httpx.Response,
    status: int,
    code: int = INVALID_REQUEST,
    request_id: Any = SERVER_ERROR_ID,
) -> str:
    """Assert ``response`` is the JSON-RPC error of ``status`` and return its message.

    The shape is the SDK transport's own: ``jsonrpc``, ``id`` and an ``error`` of ``code``
    and ``message`` alone, with ``Content-Type: application/json`` and no session header -
    a request the router refused belongs to no session (router docstring).
    """
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    assert SESSION_ID_HEADER not in response.headers
    body = response.json()
    assert list(body) == ["jsonrpc", "id", "error"]
    assert (body["jsonrpc"], body["id"]) == ("2.0", request_id)
    assert list(body["error"]) == ["code", "message"]
    assert body["error"]["code"] == code
    message = body["error"]["message"]
    assert isinstance(message, str)
    return message


# --- the five addresses (requirements 14.2, 14.3, 15.1) -----------------------------------


@pytest.mark.anyio
@pytest.mark.req("14.2", "15.1")
@pytest.mark.parametrize("path", CREATING_PATHS)
async def test_each_creating_address_opens_a_session_on_a_new_sandbox(
    env: Env, tmp_path: Path, path: str
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.open(path)

        # The session is bound to a sandbox of its own, created from the Snapshot of the
        # initial dataset the address named at the anchor date of the configuration
        # (requirements 15.1 and 16.1); the four addresses of the fixture instance name
        # the same profile and dataset, its only ones (requirement 14.2).
        assert service.sandbox_ids() == [session.sandbox.sandbox_id]
        assert service.snapshots.asked == [(DATASET, ANCHOR_DATE)]
        assert session.sandbox.origin == "auto"
        assert session.sandbox.anchor_date == ANCHOR_DATE
        assert session.sandbox.profile.name == PROFILE
        assert session.sandbox.dataset.name == DATASET
        # The session id is this service's own, and the transport announced it.
        assert session.sandbox.session_ids == [session.session_id]
        assert session.record.path == path
        assert session.result["serverInfo"]["name"] == SERVER_NAME


@pytest.mark.anyio
@pytest.mark.req("14.3")
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/mcp/",
        "/MCP",
        "/Mcp",
        "/mcp/extra",
        "/p/semantic/mcp/extra",
        "/x/semantic/mcp",
        "/p/semantic",
        "/d/fx-basic/p/semantic/mcp",
        "/admin/health",
        "/admin/sandboxes",
    ],
)
async def test_a_path_outside_the_five_addresses_is_not_found(
    env: Env, tmp_path: Path, path: str
) -> None:
    async with service_over(env, tmp_path) as service:
        response = await service.post(path, initialize_body())

        message = assert_json_error(response, httpx.codes.NOT_FOUND)
        assert path in message
        # Neither a session nor a sandbox (requirement 14.3), and no trace anywhere.
        assert len(service.router.sessions) == 0
        assert service.sandbox_ids() == []
        assert service.snapshots.asked == []


@pytest.mark.anyio
@pytest.mark.req("14.3")
@pytest.mark.parametrize(
    ("path", "named"),
    [
        ("/p/no_such_profile/mcp", "no_such_profile"),
        (f"/p/{PROFILE.upper()}/mcp", PROFILE.upper()),
        ("/p//mcp", ""),
        ("/d/no-such-dataset/mcp", "no-such-dataset"),
        (f"/d/{DATASET.upper()}/mcp", DATASET.upper()),
        (f"/p/{PROFILE}/d/no-such-dataset/mcp", "no-such-dataset"),
        (f"/p/no_such_profile/d/{DATASET}/mcp", "no_such_profile"),
    ],
)
async def test_a_profile_or_dataset_the_instance_has_not_is_not_found(
    env: Env, tmp_path: Path, path: str, named: str
) -> None:
    async with service_over(env, tmp_path) as service:
        response = await service.post(path, initialize_body())

        # The name is compared case-sensitively and the 404 names it (requirement 14.3).
        message = assert_json_error(response, httpx.codes.NOT_FOUND)
        assert named in message
        assert len(service.router.sessions) == 0
        assert service.sandbox_ids() == []


@pytest.mark.anyio
@pytest.mark.req("14.3")
async def test_a_sandbox_id_that_is_no_live_prebuilt_sandbox_is_not_found(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        # A sandbox of a session is not reachable by its id: requirement 14.3 asks for a
        # sandbox the Admin API prebuilt.
        session = await service.open(AUTO_PATH)
        frozen = await service.prebuilt()
        await service.manager.freeze(frozen)
        deleted = await service.prebuilt()
        await service.manager.delete(deleted)

        for sandbox_id in [
            "sb_0123456789abcdef",
            session.sandbox.sandbox_id,
            frozen.sandbox_id,
            deleted.sandbox_id,
        ]:
            response = await service.post(sandbox_path(sandbox_id), initialize_body())

            message = assert_json_error(response, httpx.codes.NOT_FOUND)
            assert sandbox_id in message

        # The one session that was open is untouched by any of them.
        assert len(service.router.sessions) == 1
        assert (await session.request(2, "ping")).status_code == httpx.codes.OK


# --- a request without a session id (requirements 14.8, 15.1) -----------------------------


@pytest.mark.anyio
@pytest.mark.req("14.8")
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(request_body(2, "ping"), id="another-method"),
        pytest.param(request_body(2, "tools/list"), id="tools-list"),
        pytest.param(INITIALIZED, id="notification"),
        pytest.param({"jsonrpc": "2.0", "id": 2, "result": {}}, id="response"),
        pytest.param([initialize_body()], id="batch"),
        pytest.param({"jsonrpc": "2.0", "id": 2}, id="no-method"),
        pytest.param("{", id="not-json"),
        pytest.param("", id="empty-body"),
    ],
)
async def test_a_request_without_a_session_id_must_be_an_initialize(
    env: Env, tmp_path: Path, body: Any
) -> None:
    async with service_over(env, tmp_path) as service:
        response = await service.post(AUTO_PATH, body)

        assert_json_error(response, httpx.codes.BAD_REQUEST)
        # No session and no sandbox (requirement 14.8).
        assert len(service.router.sessions) == 0
        assert service.sandbox_ids() == []


@pytest.mark.anyio
@pytest.mark.req("14.8")
@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "HEAD"])
async def test_a_method_without_a_session_id_is_a_bad_request(
    env: Env, tmp_path: Path, method: str
) -> None:
    async with service_over(env, tmp_path) as service:
        response = await service.client.request(method, AUTO_PATH, headers=dict(POST_HEADERS))

        assert response.status_code == httpx.codes.BAD_REQUEST
        assert service.sandbox_ids() == []


@pytest.mark.anyio
@pytest.mark.req("15.1")
@pytest.mark.parametrize(
    ("headers", "status", "message"),
    [
        pytest.param(
            {"accept": JSON_AND_SSE}, 400, INVALID_CONTENT_TYPE, id="content-type-missing"
        ),
        pytest.param(
            {"content-type": "text/plain", "accept": JSON_AND_SSE},
            400,
            INVALID_CONTENT_TYPE,
            id="content-type-text-plain",
        ),
        pytest.param(
            {"content-type": "APPLICATION/JSON", "accept": JSON_AND_SSE},
            415,
            UNSUPPORTED_MEDIA_TYPE_MESSAGE,
            id="content-type-upper-case",
        ),
        pytest.param(
            {"content-type": "application/json-seq", "accept": JSON_AND_SSE},
            415,
            UNSUPPORTED_MEDIA_TYPE_MESSAGE,
            id="content-type-json-prefix",
        ),
        pytest.param(
            {"content-type": "application/json"}, 406, NOT_ACCEPTABLE_MESSAGE, id="accept-missing"
        ),
        pytest.param(
            {"content-type": "application/json", "accept": "*/*"},
            406,
            NOT_ACCEPTABLE_MESSAGE,
            id="accept-any",
        ),
        pytest.param(
            {"content-type": "application/json", "accept": "text/event-stream"},
            406,
            NOT_ACCEPTABLE_MESSAGE,
            id="accept-sse-only",
        ),
        pytest.param(
            {"content-type": "APPLICATION/JSON", "accept": "text/html"},
            406,
            NOT_ACCEPTABLE_MESSAGE,
            id="order-accept-before-media-type",
        ),
    ],
)
async def test_the_preflight_refuses_an_initialize_the_transport_would_refuse(
    env: Env, tmp_path: Path, headers: Mapping[str, str], status: int, message: str
) -> None:
    async with service_over(env, tmp_path) as service:
        response = await service.post(AUTO_PATH, initialize_body(), headers)

        # The answer is the transport's own, so one condition has one answer wherever it
        # was detected (probe V3): a plain-text body for the Content-Type check of the
        # security middleware, a JSON-RPC error for the other two.
        assert response.status_code == status
        if status == httpx.codes.BAD_REQUEST:
            assert response.text == message
            assert "content-type" not in response.headers
        else:
            assert assert_json_error(response, status) == message
        # And no sandbox was created for a request that would never have reached a session.
        assert service.sandbox_ids() == []
        assert len(service.router.sessions) == 0


@pytest.mark.anyio
@pytest.mark.req("15.1")
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(POST_HEADERS, id="official-client"),
        pytest.param(
            {"content-type": "application/json", "accept": "application/json"}, id="json-only"
        ),
        pytest.param(
            {
                "content-type": "application/json; charset=utf-8",
                "accept": "text/html;q=0.9, application/json;q=0.1",
            },
            id="parameters-on-both",
        ),
    ],
)
async def test_the_preflight_lets_the_headers_the_transport_accepts_through(
    env: Env, tmp_path: Path, headers: Mapping[str, str]
) -> None:
    async with service_over(env, tmp_path) as service:
        response = await service.post(AUTO_PATH, initialize_body(), headers)

        assert response.status_code == httpx.codes.OK, response.text
        assert len(service.router.sessions) == 1


# --- the concurrency limit and a Snapshot that cannot be built (15.10, 15.14) -------------


@pytest.mark.anyio
@pytest.mark.req("15.10")
async def test_the_concurrency_limit_answers_503_and_leaves_the_others_alone(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        # Two sandboxes that are not frozen is the limit of this service: one prebuilt by
        # the Admin API and one of a session (requirement 15.10).
        prebuilt = await service.prebuilt()
        session = await service.open(AUTO_PATH)
        assert service.manager.non_frozen_count() == MAX_SANDBOXES

        response = await service.post(AUTO_PATH, initialize_body(9))

        # The 503 carries the id of the initialize it answers and says what the limit is.
        message = assert_json_error(response, httpx.codes.SERVICE_UNAVAILABLE, request_id=9)
        assert str(MAX_SANDBOXES) in message
        assert len(service.router.sessions) == 1
        assert service.sandbox_ids() == [prebuilt.sandbox_id, session.sandbox.sandbox_id]

        # The limit is about creating a sandbox: an initialize on the address of a prebuilt
        # one and the requests of the session that is running are served as ever.
        shared = await service.open(sandbox_path(prebuilt.sandbox_id))
        assert shared.sandbox is prebuilt
        assert (await session.request(3, "ping")).status_code == httpx.codes.OK


@pytest.mark.anyio
@pytest.mark.req("15.14")
async def test_a_snapshot_that_cannot_be_built_answers_500_with_32603(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        running = await service.open(AUTO_PATH)
        service.snapshots.fail = True

        response = await service.post(AUTO_PATH, initialize_body(7))

        # The body is the JSON-RPC error of design.md 7.2, naming the initial dataset and
        # the anchor date whose Snapshot could not be built (requirement 15.14).
        assert response.status_code == httpx.codes.INTERNAL_SERVER_ERROR
        assert response.json() == {
            "jsonrpc": "2.0",
            "id": 7,
            "error": {
                "code": INTERNAL_ERROR,
                "message": SNAPSHOT_FAILED_MESSAGE.format(
                    dataset=DATASET, anchor=ANCHOR_DATE.isoformat()
                ),
            },
        }
        # No session and no sandbox, not even a directory left behind (requirement 19.19).
        assert len(service.router.sessions) == 1
        assert service.sandbox_ids() == [running.sandbox.sandbox_id]
        assert sorted(p.name for p in service.manager.root.iterdir()) == [
            running.sandbox.sandbox_id
        ]
        # The session that was already running is unaffected.
        assert (await running.request(2, "ping")).status_code == httpx.codes.OK


# --- a session id and the path it was assigned on (requirement 14.7) ----------------------


@pytest.mark.anyio
@pytest.mark.req("14.7")
async def test_a_session_id_does_not_work_on_another_path(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.open(AUTO_PATH)
        prebuilt = await service.prebuilt()

        for other in [PROFILE_PATH, BOTH_PATH, sandbox_path(prebuilt.sandbox_id)]:
            response = await session.send(request_body(2, "ping"), path=other)

            # The path is compared literally, so the same id is unknown anywhere else
            # (requirement 14.7); the request is not performed.
            message = assert_json_error(response, httpx.codes.NOT_FOUND)
            assert session.session_id in message

        # And the session itself is unharmed on its own path.
        assert (await session.request(3, "ping")).status_code == httpx.codes.OK
        assert [event["event"] for event in session.events()] == [EventName.SESSION_START]


@pytest.mark.anyio
@pytest.mark.req("14.7")
@pytest.mark.parametrize("session_id", ["", "0123456789abcdef0123456789abcdef", "sb_1"])
async def test_a_session_id_this_service_never_assigned_is_not_found(
    env: Env, tmp_path: Path, session_id: str
) -> None:
    async with service_over(env, tmp_path) as service:
        await service.open(AUTO_PATH)

        response = await service.post(
            AUTO_PATH,
            request_body(2, "ping"),
            {**POST_HEADERS, SESSION_ID_HEADER: session_id},
        )

        assert_json_error(response, httpx.codes.NOT_FOUND)


@pytest.mark.anyio
@pytest.mark.req("14.7")
async def test_a_get_is_answered_with_405(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.open(AUTO_PATH)

        response = await session.get()

        # This service initiates no request and no notification, so it opens no
        # server-initiated stream and the GET never reaches the transport, which would
        # have opened one (requirement 14.11, probe V5).
        assert_json_error(response, httpx.codes.METHOD_NOT_ALLOWED)
        assert response.headers["allow"] == ALLOWED_HTTP_METHODS
        # The session goes on serving the requests after it.
        assert (await session.request(2, "ping")).status_code == httpx.codes.OK


# --- one session, end to end (requirements 15.1 and 17.9) --------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.1", "15.5", "17.9")
async def test_a_session_serves_the_handshake_a_list_and_a_call(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.open(AUTO_PATH)

        # The handshake, as the official client completes it: the notification is answered
        # with 202 and an empty body.
        initialized = await session.send(INITIALIZED)
        assert initialized.status_code == httpx.codes.ACCEPTED
        assert initialized.content == b""
        assert initialized.headers[SESSION_ID_HEADER] == session.session_id

        listed = await session.request(2, "tools/list")
        assert listed.status_code == httpx.codes.OK
        tools = [tool["name"] for tool in listed.json()["result"]["tools"]]
        assert tools == list(session.sandbox.profile.tool_names)

        called = await session.call(3, CONTEXT_TOOL, {})
        result = called.json()["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["business_date"] == ANCHOR_DATE.isoformat()

        # A ping takes no sandbox lock and runs no job, so the only thing that can move the
        # moment the idle threshold is measured from is the filter's activity hook, which
        # is the one hook this transport provides (requirement 15.5, design.md 7.2).
        service.ticker.advance(5)
        pinged = await session.request(4, "ping")
        assert pinged.json() == {"jsonrpc": "2.0", "id": 4, "result": {}}
        assert session.sandbox.last_done == service.ticker.seconds

        # One event per request that writes one, in order, all of this session and all of
        # this sandbox; the ping and the notification write none (requirement 17.9).
        events = session.events()
        assert [event["event"] for event in events] == [
            EventName.SESSION_START,
            EventName.TOOLS_LIST,
            EventName.TOOLS_CALL,
        ]
        assert {event["session_id"] for event in events} == {session.session_id}
        assert {event["sandbox_id"] for event in events} == {session.sandbox.sandbox_id}
        assert [event["seq"] for event in events] == [1, 2, 3]


# --- several sessions on one prebuilt sandbox (requirement 15.3) --------------------------


@pytest.mark.anyio
@pytest.mark.req("15.3")
async def test_sessions_of_a_prebuilt_sandbox_share_it(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        prebuilt = await service.prebuilt()
        asked = list(service.snapshots.asked)
        path = sandbox_path(prebuilt.sandbox_id)

        first = await service.open(path, request_id=1)
        second = await service.open(path, request_id=1)

        # Two sessions, one sandbox: no Snapshot was copied for either of them and the
        # sandbox kept its content and its anchor date (requirement 15.3).
        assert first.session_id != second.session_id
        assert first.sandbox is prebuilt
        assert second.sandbox is prebuilt
        assert service.sandbox_ids() == [prebuilt.sandbox_id]
        assert service.snapshots.asked == asked
        assert prebuilt.anchor_date == ANCHOR_DATE
        # The profile and the initial dataset of such a session are the sandbox's own
        # (requirement 14.2).
        assert prebuilt.session_ids == [first.session_id, second.session_id]

        # Both serve calls, and the two session_start events of the one trace name them.
        assert (await first.call(2, CONTEXT_TOOL, {})).status_code == httpx.codes.OK
        assert (await second.call(2, CONTEXT_TOOL, {})).status_code == httpx.codes.OK
        events = events_of(prebuilt)
        assert [event["event"] for event in events] == [
            EventName.SESSION_START,
            EventName.SESSION_START,
            EventName.TOOLS_CALL,
            EventName.TOOLS_CALL,
        ]
        assert [event["session_id"] for event in events] == [
            first.session_id,
            second.session_id,
            first.session_id,
            second.session_id,
        ]


# --- the end of a session (requirements 15.7, 15.8, 17.9) --------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.7", "17.9")
async def test_a_delete_writes_session_end_and_freezes_the_sandbox_it_created(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.open(AUTO_PATH)
        sandbox = session.sandbox

        response = await session.delete()

        # The SDK transport answers the DELETE itself, with 200 and an empty body (V3).
        assert response.status_code == httpx.codes.OK
        assert response.content == b""
        # The sandbox this service created for the session is frozen with it
        # (requirement 15.7), and the event of the end is in its trace (requirement 17.9).
        assert sandbox.status == "frozen"
        events = events_of(sandbox)
        assert [event["event"] for event in events] == [
            EventName.SESSION_START,
            EventName.SESSION_END,
        ]
        assert events[-1]["session_id"] == session.session_id
        # A frozen sandbox keeps its files, the trace among them (requirement 15.12).
        assert (sandbox.directory / TRACE_FILENAME).is_file()
        # The session is gone: its id answers 404 and its transport is terminated.
        assert len(service.router.sessions) == 0
        assert session.record.terminated
        assert session.record.transport.is_terminated
        assert_json_error(await session.request(2, "ping"), httpx.codes.NOT_FOUND)


@pytest.mark.anyio
@pytest.mark.req("15.8", "17.9")
async def test_a_delete_of_a_prebuilt_sandbox_releases_only_that_binding(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        prebuilt = await service.prebuilt()
        path = sandbox_path(prebuilt.sandbox_id)
        first = await service.open(path)
        second = await service.open(path)

        assert (await first.delete()).status_code == httpx.codes.OK

        # The sandbox keeps its state, its content, its anchor date and its other session
        # (requirement 15.8); only the binding of the session that ended is released.
        assert prebuilt.status == "active"
        assert prebuilt.anchor_date == ANCHOR_DATE
        assert service.sandbox_ids() == [prebuilt.sandbox_id]
        assert (await second.call(2, CONTEXT_TOOL, {})).status_code == httpx.codes.OK
        assert_json_error(await first.request(2, "ping"), httpx.codes.NOT_FOUND)

        # And a new session may still connect to it (requirement 15.8).
        third = await service.open(path)
        assert third.sandbox is prebuilt
        events = events_of(prebuilt)
        assert [event["event"] for event in events] == [
            EventName.SESSION_START,
            EventName.SESSION_START,
            EventName.SESSION_END,
            EventName.TOOLS_CALL,
            EventName.SESSION_START,
        ]
        assert events[2]["session_id"] == first.session_id


@pytest.mark.anyio
@pytest.mark.req("14.7", "15.7")
@pytest.mark.parametrize("how", ["freeze", "delete"])
async def test_a_sandbox_that_is_taken_away_ends_its_sessions(
    env: Env, tmp_path: Path, how: str
) -> None:
    async with service_over(env, tmp_path) as service:
        prebuilt = await service.prebuilt()
        path = sandbox_path(prebuilt.sandbox_id)
        first = await service.open(path)
        second = await service.open(path)

        # What the janitor does on its own (requirement 15.7) and what the Admin API does
        # without telling the router (requirement 19.13).
        if how == "freeze":
            await service.manager.freeze(prebuilt)
        else:
            await service.manager.delete(prebuilt)

        response = await first.request(2, "ping")

        # The request is refused, and every session of that sandbox is ended with it: the
        # transports are terminated and the ids answer 404 (requirement 14.7).
        assert_json_error(response, httpx.codes.NOT_FOUND)
        assert len(service.router.sessions) == 0
        assert first.record.transport.is_terminated
        assert second.record.transport.is_terminated
        assert_json_error(await second.request(2, "ping"), httpx.codes.NOT_FOUND)
        # And the address of the sandbox is a 404 as well (requirement 14.3).
        assert_json_error(await service.post(path, initialize_body()), httpx.codes.NOT_FOUND)


@pytest.mark.anyio
@pytest.mark.req("14.7", "15.7")
async def test_terminate_sandbox_ends_the_sessions_of_one_sandbox_only(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        prebuilt = await service.prebuilt()
        shared = await service.open(sandbox_path(prebuilt.sandbox_id))
        other = await service.open(AUTO_PATH)

        # The entry point the Admin API calls after it froze or deleted a sandbox
        # (task 23.2): the sessions of that sandbox end, and no other.
        await service.manager.freeze(prebuilt)
        await service.router.terminate_sandbox(prebuilt.sandbox_id)

        assert shared.record.terminated
        assert shared.record.transport.is_terminated
        assert [record.session_id for record in service.router.sessions.all()] == [other.session_id]
        assert not other.record.terminated
        assert (await other.request(2, "ping")).status_code == httpx.codes.OK
