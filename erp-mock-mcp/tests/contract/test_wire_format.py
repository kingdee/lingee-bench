"""What one locked SDK version actually puts on the wire, on both transports.

Several acceptance criteria are about bytes a client receives rather than about objects
this service builds: ``capabilities`` carries ``tools`` alone (requirement 14.4), a
tools/list result carries no ``nextCursor`` (requirement 14.5), the ``null`` of an empty
field is still there and every object keeps its key order (requirements 13.1 and 13.4), an
error result carries no ``structuredContent`` at all (requirement 13.6), and the two
JSON-RPC errors of requirements 14.6 and 14.10 carry ``code`` and ``message`` and nothing
else. None of those is something the service codes directly: between a
``types.ServerResult`` and the wire sits the SDK, which serializes every message with
``model_dump_json(by_alias=True, exclude_none=True)``, and whether that option reaches
*inside* a ``dict[str, Any]`` field - dropping the None values of a structuredContent, or
reordering its keys - is a property of the SDK and of pydantic-core, not of this project
(design.md 7.1, the serialization item).

This module is where that is checked on the real assembly, and on both transports:

- Streamable HTTP: the real :class:`~erp_mock.gateway.router.McpRouter` over a real
  Sandbox_Manager, driven through :class:`httpx.ASGITransport`, where the body of the
  response to a POST is the bytes the transport wrote (JSON response mode, design.md 7.2);
- stdio: the real :class:`~erp_mock.gateway.stdio.StdioRuntime` over memory streams in
  place of stdin and stdout, where every message the service wrote is serialized the way
  ``mcp.server.stdio``'s own stdout writer serializes it - that is, the line it would have
  written to stdout.

Both, rather than one, because the two transports serialize a message in two different
places in the SDK (``_create_json_response`` and ``stdout_writer``) and reach them through
two different session setups, and requirement 24.1 asks that one call mean the same thing
on either. :func:`test_both_transports_answer_the_same_call_with_the_same_bytes` makes that
explicit for one call.

How this differs from tests/probes/. The probes answer "does the SDK behave this way at
all" on a bare ``Server`` with hand-built results (V1 and V2 of
tests/probes/test_sdk_server.py, V5 and V6 of tests/probes/test_sdk_client.py) and are the
record of the design decisions that were taken because of it. This module answers "does
the service, as assembled, put these bytes on the wire", through its own router, its own
handlers, its own sandboxes and its own tools layer. A probe failing says the SDK changed;
this failing says a client's view of this service changed.

The business configuration is the engine test fixture (:mod:`tests.helpers.fixtures`): a
wire format is a property of the framework and not of the example packs, and the fixture
loads in milliseconds. No snapshot file is involved - what is asserted here is the shape of
a payload, not that it has not changed - so nothing in this module reacts to
``--update-snapshots`` (tests/contract/conftest.py).

No test reads the system clock (requirement 25.10): the Sandbox_Manager is given a
:class:`~erp_mock.gateway.clock.SystemClock` over a fixed instant and a monotonic source
the test owns, and its sandboxes are created from one Snapshot built once for the module
through a double of the Snapshot source.
"""

import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from io import StringIO
from pathlib import Path
from typing import Any, ClassVar, Final

import anyio
import httpx
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.shared.message import SessionMessage

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_compact, encode_utf8
from erp_mock.config.instance import DatasetDef
from erp_mock.domain.inventory import NEGATIVE_STOCK_RULE
from erp_mock.domain.state_machine import DRAFT_RULE
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import (
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    UNKNOWN_TOOL_MESSAGE,
    SessionHandlers,
)
from erp_mock.gateway.message_filter import METHOD_NOT_FOUND_MESSAGE
from erp_mock.gateway.router import SESSION_ID_HEADER, McpRouter
from erp_mock.gateway.sandbox import SandboxManager
from erp_mock.gateway.stdio import StdioOptions, run_stdio
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.loader.yaml_io import load_yaml
from erp_mock.state.schema import StorageSchema, build_schema
from erp_mock.state.snapshot import build_snapshot_db, finalize_snapshot
from erp_mock.tools.generator import ToolSpec
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
TIMEOUT_S: Final = 20

# --- the two transports ------------------------------------------------------------------

HTTP: Final = "http"
STDIO: Final = "stdio"
TRANSPORTS: Final = (HTTP, STDIO)

# --- the addresses and headers of the Streamable HTTP transport --------------------------

# Where the httpx client sends its requests; the ASGI transport never opens a socket.
BASE_URL: Final = "http://127.0.0.1:8000"

# The four addresses whose initialize creates a sandbox, and the names they carry
# (requirement 14.2); the fixture instance has one profile and one initial dataset, so all
# four name the same two and differ only in whether they name them at all.
AUTO_PATH: Final = "/mcp"
CREATING_PATHS: Final = (
    AUTO_PATH,
    f"/p/{PROFILE}/mcp",
    f"/d/{DATASET}/mcp",
    f"/p/{PROFILE}/d/{DATASET}/mcp",
)

# The headers the official client puts on every POST (mcp.client.streamable_http) and the
# protocol version it announces.
POST_HEADERS: Final[Mapping[str, str]] = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}
PROTOCOL_HEADER: Final = "mcp-protocol-version"
PROTOCOL_VERSION: Final = "2025-11-25"
CONTENT_TYPE_JSON: Final = "application/json"

# --- the messages a client sends ----------------------------------------------------------

JSONRPC_VERSION: Final = "2.0"
INITIALIZE: Final = "initialize"
TOOLS_LIST: Final = "tools/list"
TOOLS_CALL: Final = "tools/call"

# What an initialize carries; the same params on both transports, so the two initialize
# results differ in nothing a client sent.
INITIALIZE_PARAMS: Final[Mapping[str, JsonValue]] = {
    "protocolVersion": PROTOCOL_VERSION,
    "capabilities": {},
    "clientInfo": {"name": "wire-format-test", "version": "0.0.0"},
}

# The notification that completes the handshake; the SDK session refuses every request
# before it (``ServerSession._received_request``).
INITIALIZED: Final[Mapping[str, JsonValue]] = {
    "jsonrpc": JSONRPC_VERSION,
    "method": "notifications/initialized",
}

# A method outside the whitelist of requirement 14.10, and a tool name outside the profile
# (requirement 14.6).
UNLISTED_METHOD: Final = "resources/list"
UNKNOWN_TOOL: Final = "vendor_detail_fetch"

# The one built-in tool, which every profile exposes and which reports the anchor date of
# the sandbox (requirement 16.10).
CONTEXT_TOOL: Final = "business_context_get"

# --- the one call whose nulls and key order are read off the wire -------------------------

# ``vendor_get``, which the fixture's ``rename`` exposes under this name. Chosen because it
# is a read - it writes nothing, so the bytes do not depend on when the call happened - and
# because :data:`RETIRED_VENDOR` was created without its two optional string fields, so the
# result really carries empty values: requirement 13.4 asks for them as ``null`` with their
# keys kept, and both sit in the middle of the key order, with non-null keys before and
# after them. A serializer that dropped a None inside structuredContent would therefore
# show up twice over - as two missing keys and as a shifted key order.
VENDOR_GET: Final = "vendor_detail_get"
RETIRED_VENDOR: Final = "ven_000003"

# The keys of that result, in the order requirement 13.1 asks for: the property order of
# the tool's outputSchema. ``short_name`` and ``contact`` are the empty ones.
VENDOR_KEYS: Final = (
    "id",
    "code",
    "status",
    "name",
    "short_name",
    "contact",
    "pay_days",
    "default_tax_rate",
)
EMPTY_FIELDS: Final = ("short_name", "contact")

# The two empty fields as they have to appear in the response bytes: adjacent, each with its
# key, each with ``null`` (requirements 13.1 and 13.4). The SDK's serializer writes compact
# JSON, so there is no whitespace between the tokens.
NULLS_ON_THE_WIRE: Final = '"short_name":null,"contact":null'

# A vendor id no record has, which makes the same tool fail with NOT_FOUND - the error
# result of requirement 13.6, reached without touching the sandbox database.
MISSING_VENDOR: Final = "ven_000099"
NOT_FOUND: Final = "NOT_FOUND"

# --- what requirement 14.4 asks the instructions not to be -------------------------------

# "不超过 200 个 Unicode 字符" counted as Python counts a str: code points.
MAX_INSTRUCTIONS_CHARS: Final = 200

# A rule number as every business pack and the engine write them: ``R-GEN-001``,
# ``R-PUR-001``, ``R-FXT-001``. Matched as a pattern rather than looked up one by one, so a
# rule number the instructions do not name today cannot slip in later
# (:func:`test_the_rule_number_pattern_matches_the_rule_numbers_in_use` keeps the pattern
# honest).
RULE_NUMBER: Final = re.compile(r"R-[A-Z]+-[0-9]+")

# The capabilities this service does not offer, which may not turn up anywhere in the
# initialize response (requirement 14.4). ``experimental`` is the one
# ``Server.create_initialization_options()`` would have added, which is why
# :data:`~erp_mock.gateway.handlers.INIT_OPTIONS` is built by hand.
UNOFFERED_CAPABILITIES: Final = ("experimental", "resources", "prompts", "logging", "completions")

# --- the keys and codes of the protocol --------------------------------------------------

RESULT_KEY: Final = "result"
ERROR_KEY: Final = "error"
STRUCTURED_KEY: Final = "structuredContent"
CONTENT_KEY: Final = "content"
TOOLS_KEY: Final = "tools"
NEXT_CURSOR_KEY: Final = "nextCursor"
CAPABILITIES_KEY: Final = "capabilities"
INSTRUCTIONS_KEY: Final = "instructions"
DATA_KEY: Final = "data"

# The keys of a JSON-RPC response, in the order the protocol models write them.
ENVELOPE_KEYS: Final = ("jsonrpc", "id", RESULT_KEY)
ERROR_ENVELOPE_KEYS: Final = ("jsonrpc", "id", ERROR_KEY)

# The keys of an error object either way: a JSON-RPC error carries two (``data`` is None
# and left out), a tool error object the four of requirement 13.6, always all four.
JSONRPC_ERROR_KEYS: Final = ("code", "message")
TOOL_ERROR_KEYS: Final = ("code", "message", "rule_id", "details")

# --- the test clock and the Snapshot source ----------------------------------------------

# The instant the first reading of the test clock returns; the nth reading is this plus
# n - 1 seconds, so the ``ts`` of two trace events differ and neither is the system time.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# How far the monotonic source moves per reading (requirement 25.10).
STEP_SECONDS: Final = 0.25

# The definition file of the initial dataset of the fixture, relative to its instance
# directory, as loading step 4 reads it.
DATASET_FILE: Final = f"datasets/{DATASET}.yaml"

# The date one stdio process of :func:`test_the_instructions_do_not_vary` asks for with
# ``--anchor-date``: not the fixed date of the fixture's clock configuration, so the anchor
# date of its sandbox really differs (requirement 16.1).
OTHER_ANCHOR: Final = date(2000, 2, 29)

# How many messages a transport's stream holds; every exchange of this module sends one
# request and reads its answer, so one slot is enough and a message nobody asked for shows
# up as an id that does not match (:meth:`Answer.body`).
STREAM_BUFFER: Final = 1


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass(slots=True)
class Ticker:
    """A monotonic source that advances by ``step`` per reading (requirement 25.10)."""

    step: float = STEP_SECONDS
    base: float = 1000.0
    reads: int = 0

    def __call__(self) -> float:
        value = self.base + self.step * self.reads
        self.reads += 1
        return value


def stepping_clock() -> SystemClock:
    """A clock whose nth instant is :data:`INSTANT` plus n - 1 seconds, over a Ticker."""
    readings = iter(range(1 << 20))
    return SystemClock(now=lambda: INSTANT + timedelta(seconds=next(readings)), monotonic=Ticker())


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


# --- the instance configuration and the one Snapshot of this module ----------------------


@dataclass(frozen=True, slots=True)
class Env:
    """What every test of this module starts from, built once for it."""

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

    def tool(self, name: str) -> ToolSpec:
        """The exposed tool ``name`` of that profile."""
        found = next((spec for spec in self.profile.tools if spec.name == name), None)
        assert found is not None, name
        return found

    def output_properties(self, name: str) -> tuple[str, ...]:
        """The properties of the outputSchema of ``name``, in declaration order (11.7).

        Which is the key order requirement 13.1 asks of every object of a successful
        result, read from the schema the client was handed rather than written out here.
        """
        properties = self.tool(name).output_schema["properties"]
        assert isinstance(properties, Mapping), properties
        return tuple(properties)


def loaded_with_dataset() -> LoadedInstance:
    """The fixture configuration with the initial dataset loading step 8 would leave.

    The steps before step 8 leave no :class:`~erp_mock.loader.instance.DatasetModel`
    behind, and the router looks the name of an address up in exactly that map
    (requirement 14.3), so the model is built here the way step 8 builds it: the validated
    definition, the file it came from and the current user the instance configuration
    resolves for it (requirement 20.5).
    """
    loaded = fixture_loaded()
    definition = DatasetDef.model_validate(load_yaml(INSTANCE_DIR / DATASET_FILE))
    dataset = DatasetModel(definition=definition, file=DATASET_FILE, current_user=CURRENT_USER)
    return replace(loaded, datasets=FrozenMap({dataset.name: dataset}))


def build_snapshot(directory: Path, schema: StorageSchema, layer: ToolLayer) -> Path:
    """Build the one Snapshot of this module below ``directory`` and return its path.

    The tables of the fixture and the standard basic data of
    :mod:`tests.helpers.engine_env` in them - :data:`RETIRED_VENDOR` among the records -
    created through the engine the way a seed build creates them (requirement 20.17), and
    written out as a Snapshot.
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


@contextmanager
def manager_over(env: Env, data_dir: Path) -> Iterator[SandboxManager]:
    """A Sandbox_Manager over ``data_dir``, closed again when the block returns."""
    manager = SandboxManager(
        data_dir=data_dir,
        loaded=env.loaded,
        schema=env.schema,
        snapshots=Snapshots(env.snapshot),
        clock=stepping_clock(),
    )
    try:
        yield manager
    finally:
        manager.close()


# --- one open session, and the raw bytes its requests are answered with ------------------


def request_body(
    request_id: int, method: str, params: Mapping[str, JsonValue] | None = None
) -> dict[str, JsonValue]:
    """One JSON-RPC request, as a client writes it."""
    body: dict[str, JsonValue] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        body["params"] = dict(params)
    return body


def call_params(tool: str, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    """The params of one tools/call."""
    return {"name": tool, "arguments": dict(arguments)}


@dataclass(frozen=True, slots=True)
class Answer:
    """One answer, as the bytes a client received and as what they parse to.

    ``raw`` is the whole JSON-RPC message: the body of the HTTP response for Streamable
    HTTP, the stdout line for stdio. Everything a test asserts comes off these bytes -
    :meth:`body` parses them, and ``json.loads`` builds a dict in the order the keys appear
    in the text, so the key order of a parsed object *is* the key order on the wire.
    """

    transport: str
    request_id: int
    raw: bytes

    @property
    def text(self) -> str:
        """The bytes as text, for an assertion about what is or is not written at all."""
        return self.raw.decode("utf-8")

    @property
    def body(self) -> dict[str, Any]:
        """The message, parsed; fails the test unless it answers this very request."""
        payload = json.loads(self.raw)
        assert isinstance(payload, dict), payload
        assert payload["jsonrpc"] == JSONRPC_VERSION, payload
        assert payload["id"] == self.request_id, payload
        return payload

    @property
    def result(self) -> dict[str, Any]:
        """The ``result`` of a successful answer."""
        body = self.body
        assert tuple(body) == ENVELOPE_KEYS, body
        result = body[RESULT_KEY]
        assert isinstance(result, dict), result
        return result

    @property
    def error(self) -> dict[str, Any]:
        """The ``error`` of a JSON-RPC error answer (requirements 14.6 and 14.10)."""
        body = self.body
        assert tuple(body) == ERROR_ENVELOPE_KEYS, body
        error = body[ERROR_KEY]
        assert isinstance(error, dict), error
        return error


class Wire(ABC):
    """One open MCP session, which answers a request with the bytes it wrote.

    Two implementations, one per transport (:class:`HttpWire`, :class:`StdioWire`). What a
    test uses is :meth:`ask` and :attr:`initialized`; the handshake is done by
    :func:`wire_over` before a test sees the session, and the request ids are this object's
    so that the same exchange on the two transports carries the same id.
    """

    transport: ClassVar[str]

    def __init__(self) -> None:
        self._sent = 0
        self._initialized: Answer | None = None

    @property
    def initialized(self) -> Answer:
        """The answer to the initialize this session was opened with."""
        assert self._initialized is not None, "the session was not opened"
        return self._initialized

    async def ask(self, method: str, params: Mapping[str, JsonValue] | None = None) -> Answer:
        """Send one request and return what came back, byte for byte."""
        self._sent += 1
        request_id = self._sent
        raw = await self._exchange(request_body(request_id, method, params))
        return Answer(transport=self.transport, request_id=request_id, raw=raw)

    async def open(self) -> None:
        """Do the handshake every MCP client does: initialize, then the notification."""
        self._initialized = await self.ask(INITIALIZE, INITIALIZE_PARAMS)
        await self._tell(INITIALIZED)

    @abstractmethod
    async def _exchange(self, body: Mapping[str, JsonValue]) -> bytes:
        """Write ``body`` as the transport writes it and return the answer's raw bytes."""

    @abstractmethod
    async def _tell(self, body: Mapping[str, JsonValue]) -> None:
        """Write the notification ``body``, which nothing answers."""


class HttpWire(Wire):
    """A session on the Streamable HTTP transport (design.md 7.2).

    One POST per message through :class:`httpx.ASGITransport`, so the body of the response
    is what :meth:`~mcp.server.streamable_http.StreamableHTTPServerTransport._create_json_response`
    wrote: ``model_dump_json(by_alias=True, exclude_none=True)`` of the message, encoded as
    UTF-8. The session id comes back in a header of the initialize response and goes on
    every request after it (requirement 14.7).
    """

    transport = HTTP

    def __init__(self, client: httpx.AsyncClient, path: str) -> None:
        super().__init__()
        self._client = client
        self._path = path
        self._session_id: str | None = None

    @property
    def session_id(self) -> str:
        """The ``Mcp-Session-Id`` the initialize was answered with."""
        assert self._session_id is not None, "no session id was assigned"
        return self._session_id

    async def _post(self, body: Mapping[str, JsonValue]) -> httpx.Response:
        headers = dict(POST_HEADERS)
        if self._session_id is not None:
            headers[SESSION_ID_HEADER] = self._session_id
            headers[PROTOCOL_HEADER] = PROTOCOL_VERSION
        response = await self._client.post(
            self._path, content=encode_utf8(dumps_compact(body)), headers=headers
        )
        if self._session_id is None:
            self._session_id = response.headers.get(SESSION_ID_HEADER)
        return response

    async def _exchange(self, body: Mapping[str, JsonValue]) -> bytes:
        response = await self._post(body)
        # Every answer of this module is a JSON-RPC message in a 200, the two JSON-RPC
        # errors included: a method or a tool the service refuses is a protocol-level
        # error, not an HTTP-level one (tests/unit/gateway/test_router.py covers those).
        assert response.status_code == httpx.codes.OK, response.text
        assert response.headers["content-type"] == CONTENT_TYPE_JSON
        return response.content

    async def _tell(self, body: Mapping[str, JsonValue]) -> None:
        response = await self._post(body)
        assert response.status_code == httpx.codes.ACCEPTED, response.text
        assert response.content == b""


class StdioWire(Wire):
    """A session on the stdio transport (design.md 7.3).

    The streams are the pair ``stdio_server`` would have handed the runtime, so a request
    is written the way ``stdin_reader`` would have parsed it off a line - the body is
    serialized and parsed back - and an answer is serialized the way ``stdout_writer``
    writes it: ``model_dump_json(by_alias=True, exclude_none=True)``, which is the line
    that would have gone to stdout.
    """

    transport = STDIO

    def __init__(
        self,
        to_service: MemoryObjectSendStream[SessionMessage | Exception],
        from_service: MemoryObjectReceiveStream[SessionMessage],
    ) -> None:
        super().__init__()
        self._to_service = to_service
        self._from_service = from_service

    async def _exchange(self, body: Mapping[str, JsonValue]) -> bytes:
        await self._tell(body)
        message = await self._from_service.receive()
        return encode_utf8(message.message.model_dump_json(by_alias=True, exclude_none=True))

    async def _tell(self, body: Mapping[str, JsonValue]) -> None:
        parsed = types.JSONRPCMessage.model_validate_json(dumps_compact(body))
        await self._to_service.send(SessionMessage(message=parsed))


@asynccontextmanager
async def wire_over(
    env: Env,
    data_dir: Path,
    transport: str,
    *,
    path: str = AUTO_PATH,
    anchor_date: date | None = None,
) -> AsyncIterator[Wire]:
    """One open session of ``transport``, over a Sandbox_Manager in ``data_dir``.

    The handshake is done before the block body, so a test starts from a session that may
    call tools/list and tools/call, with :attr:`Wire.initialized` holding what the
    initialize was answered with.

    ``path`` and ``anchor_date`` are what each transport can vary about a session, and each
    ignores the other's: Streamable HTTP takes the profile and the initial dataset from the
    address its initialize arrived on (requirement 14.2), and stdio takes the anchor date
    from ``--anchor-date`` (requirement 16.1). Both default to what every other test of this
    module wants - the plain ``/mcp`` and the date of the clock configuration.
    """
    if transport == HTTP:
        async with http_wire(env, data_dir, path) as wire:
            yield wire
    else:
        async with stdio_wire(env, data_dir, anchor_date) as wire:
            yield wire


@asynccontextmanager
async def http_wire(env: Env, data_dir: Path, path: str) -> AsyncIterator[Wire]:
    """The MCP port of the serve command, short of the middlewares and the Admin port.

    The session tasks run in a task group of this block, which is the serve task group of
    the service (design.md 7.2); leaving the block ends every session and closes every
    sandbox.
    """
    with manager_over(env, data_dir) as manager:
        router = McpRouter(SessionHandlers(manager, env.layer), env.loaded)
        with anyio.fail_after(TIMEOUT_S):
            async with anyio.create_task_group() as tg:
                router.attach(tg)
                asgi = httpx.ASGITransport(app=router)
                async with httpx.AsyncClient(transport=asgi, base_url=BASE_URL) as client:
                    # httpx adds "Accept: */*" to every request by default; drop it so that
                    # each request carries exactly the headers given to it.
                    del client.headers["accept"]
                    wire = HttpWire(client, path)
                    try:
                        await wire.open()
                        yield wire
                    finally:
                        await router.shutdown()
                        tg.cancel_scope.cancel()


@asynccontextmanager
async def stdio_wire(env: Env, data_dir: Path, anchor_date: date | None) -> AsyncIterator[Wire]:
    """One stdio process over memory streams in place of stdin and stdout.

    The streams belong to the test, which is what lets it be the client's end: closing the
    one the service reads is the EOF stdin reaches, and the block returns once
    ``run_stdio`` has written its exit (requirement 22.4). The notice of requirement 22.3
    goes into a buffer of the test rather than to stderr.
    """
    with manager_over(env, data_dir) as manager:
        to_service, service_read = anyio.create_memory_object_stream[SessionMessage | Exception](
            STREAM_BUFFER
        )
        service_write, from_service = anyio.create_memory_object_stream[SessionMessage](
            STREAM_BUFFER
        )
        serve = partial(
            run_stdio,
            loaded=env.loaded,
            schema=env.schema,
            manager=manager,
            handlers=SessionHandlers(manager, env.layer),
            options=StdioOptions(profile=env.profile, dataset=env.dataset, anchor_date=anchor_date),
            streams=(service_read, service_write),
            notices=StringIO(),
            # No signal handler: a SIGINT sent here would be a SIGINT sent to pytest
            # (tests/unit/gateway/test_stdio_runtime.py).
            signals=(),
        )
        with anyio.fail_after(TIMEOUT_S):
            async with anyio.create_task_group() as tg:
                tg.start_soon(serve)
                wire = StdioWire(to_service, from_service)
                try:
                    await wire.open()
                    yield wire
                finally:
                    # stdin reaches EOF, which is what the process exits on.
                    await to_service.aclose()
            # The service has written its exit. The streams are the test's, so it closes
            # its own ends; the SDK closed the write stream already, whichever got there
            # first.
            await from_service.aclose()
            await service_write.aclose()


def only(items: Sequence[Any]) -> Any:
    """The one item of ``items``; fails the test when there is not exactly one."""
    assert len(items) == 1, items
    return items[0]


def text_item(result: Mapping[str, Any]) -> str:
    """The text of the one content item of a tool result (requirements 13.1 and 13.6)."""
    item = only(result[CONTENT_KEY])
    assert item["type"] == "text", item
    text = item["text"]
    assert isinstance(text, str), item
    return text


# --- capabilities and instructions (requirement 14.4) ------------------------------------


@pytest.mark.anyio
@pytest.mark.req("14.4")
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_initialize_offers_tools_alone(env: Env, tmp_path: Path, transport: str) -> None:
    async with wire_over(env, tmp_path, transport) as wire:
        answer = wire.initialized

    result = answer.result
    # On the wire, ``capabilities`` is this object and nothing else: one key, whose one key
    # is ``listChanged`` and whose value is false - the tool list of a session cannot change
    # while it runs (requirement 14.4).
    assert result[CAPABILITIES_KEY] == {TOOLS_KEY: {"listChanged": False}}
    assert tuple(result[CAPABILITIES_KEY]) == (TOOLS_KEY,)
    assert tuple(result[CAPABILITIES_KEY][TOOLS_KEY]) == ("listChanged",)
    # And none of the capabilities this service does not offer is written anywhere in the
    # response, ``experimental`` - which ``create_initialization_options()`` would have
    # added - included.
    for absent in UNOFFERED_CAPABILITIES:
        assert absent not in answer.text, absent
    assert result["serverInfo"]["name"] == SERVER_NAME


@pytest.mark.anyio
@pytest.mark.req("14.4")
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_the_instructions_are_one_short_notice(
    env: Env, tmp_path: Path, transport: str
) -> None:
    async with wire_over(env, tmp_path, transport) as wire:
        answer = wire.initialized

    instructions = answer.result[INSTRUCTIONS_KEY]
    assert instructions == SERVER_INSTRUCTIONS
    # No more than 200 Unicode characters, counted as code points (requirement 14.4).
    assert len(instructions) <= MAX_INSTRUCTIONS_CHARS

    # It names no tool of the profile, so it cannot drift from the tool list.
    for tool in env.profile.tool_names:
        assert tool not in instructions, tool
    # And no rule number, in any of the shapes a pack or the engine writes them.
    assert RULE_NUMBER.search(instructions) is None


@pytest.mark.req("14.4")
def test_the_rule_number_pattern_matches_the_rule_numbers_in_use(env: Env) -> None:
    # :data:`RULE_NUMBER` is what the test above searches the instructions for, so it is
    # worth nothing unless it matches the numbers that exist: the pack rules the fixture
    # configuration declares and the engine's own, whose names come from the engine rather
    # than from a literal here (requirement 10.7 numbers them R-GEN-001 to R-GEN-007).
    in_use = (*env.loaded.domain.rules, DRAFT_RULE, NEGATIVE_STOCK_RULE)
    assert len(in_use) > 2
    for rule_id in in_use:
        assert RULE_NUMBER.fullmatch(rule_id) is not None, rule_id


@pytest.mark.anyio
@pytest.mark.req("14.4")
async def test_the_instructions_do_not_vary(env: Env, tmp_path: Path) -> None:
    seen: list[str] = []

    # Over Streamable HTTP, what varies between sessions is the address: the four creating
    # addresses of requirement 14.2 name the profile and the initial dataset, or leave
    # either to the configuration's default.
    for index, path in enumerate(CREATING_PATHS):
        async with wire_over(env, tmp_path / f"{HTTP}-{index}", HTTP, path=path) as wire:
            seen.append(wire.initialized.result[INSTRUCTIONS_KEY])

    # Over stdio, what varies is the anchor date, which ``--anchor-date`` really changes
    # for the sandbox of the process (requirement 16.1); the second process also gets a
    # session id and a sandbox of its own.
    dates: list[str] = []
    for index, anchor in enumerate([None, OTHER_ANCHOR]):
        async with wire_over(env, tmp_path / f"{STDIO}-{index}", STDIO, anchor_date=anchor) as wire:
            seen.append(wire.initialized.result[INSTRUCTIONS_KEY])
            # The two processes really do work on two different anchor dates, which is one
            # of the things the instructions have to be independent of; the business
            # context of a session is where the date is observable (requirement 16.10).
            context = await wire.ask(TOOLS_CALL, call_params(CONTEXT_TOOL, {}))
            dates.append(context.result[STRUCTURED_KEY]["business_date"])

    assert dates == [ANCHOR_DATE.isoformat(), OTHER_ANCHOR.isoformat()]
    # Six sessions over two transports, four addresses and two anchor dates: one fixed
    # notice (requirement 14.4).
    assert len(seen) == len(CREATING_PATHS) + len(dates)
    assert set(seen) == {SERVER_INSTRUCTIONS}


# --- tools/list (requirement 14.5) -------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("14.5")
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_tools_list_carries_no_cursor(env: Env, tmp_path: Path, transport: str) -> None:
    async with wire_over(env, tmp_path, transport) as wire:
        answer = await wire.ask(TOOLS_LIST)

    # ``nextCursor`` is None and the serializer leaves it out, so the result is ``tools``
    # alone and the word does not occur in the response at all: one response carries every
    # tool of the profile and there is nothing to continue from (requirement 14.5).
    assert tuple(answer.result) == (TOOLS_KEY,)
    assert NEXT_CURSOR_KEY not in answer.text
    assert tuple(tool["name"] for tool in answer.result[TOOLS_KEY]) == env.profile.tool_names


# --- a successful result (requirements 13.1 and 13.4) ------------------------------------


@pytest.mark.anyio
@pytest.mark.req("13.1", "13.4")
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_a_result_keeps_its_nulls_and_its_key_order(
    env: Env, tmp_path: Path, transport: str
) -> None:
    async with wire_over(env, tmp_path, transport) as wire:
        answer = await wire.ask(TOOLS_CALL, call_params(VENDOR_GET, {"id": RETIRED_VENDOR}))

    result = answer.result
    assert result["isError"] is False
    structured = result[STRUCTURED_KEY]

    # The two fields the record was created without are on the wire, each with its key and
    # the value ``null``: ``exclude_none=True`` drops a None *model field* and not a None
    # inside a ``dict[str, Any]`` field (requirement 13.4, design.md 7.1).
    for empty in EMPTY_FIELDS:
        assert empty in structured, empty
        assert structured[empty] is None, empty
    assert NULLS_ON_THE_WIRE in answer.text

    # Every key of the object, in the property order of the tool's outputSchema; json.loads
    # builds the dict in the order the keys appear in the text, so this is the wire order
    # (requirements 13.1 and 11.7).
    assert tuple(structured) == VENDOR_KEYS
    assert tuple(structured) == tuple(
        name for name in env.output_properties(VENDOR_GET) if name in structured
    )
    # The values are requirement 13.4's: an int as a JSON integer, a rate as a string.
    assert structured["pay_days"] == 30
    assert structured["default_tax_rate"] == "0.13"
    assert '"pay_days":30' in answer.text
    assert '"default_tax_rate":"0.13"' in answer.text

    # And ``content`` is one text item whose text parses to that very object, keys in the
    # same order - compared as text, so a reordering would show (requirement 13.1).
    text = text_item(result)
    assert json.loads(text) == structured
    assert text == dumps_compact(structured)


# --- an error result (requirement 13.6) --------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("13.6")
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_an_error_result_carries_no_structured_content(
    env: Env, tmp_path: Path, transport: str
) -> None:
    async with wire_over(env, tmp_path, transport) as wire:
        answer = await wire.ask(TOOLS_CALL, call_params(VENDOR_GET, {"id": MISSING_VENDOR}))

    result = answer.result
    assert result["isError"] is True
    # ``structuredContent`` is a None model field here, so the serializer leaves it out:
    # the key is not in the result and the word is nowhere in the response
    # (requirement 13.6).
    assert STRUCTURED_KEY not in result
    assert STRUCTURED_KEY not in answer.text

    # One text item, whose text is an object with ``error`` as its one key; the error
    # carries the four keys of requirement 13.6 in that order, and all four are there -
    # ``rule_id`` is null for a NOT_FOUND and its key is kept all the same.
    payload = json.loads(text_item(result))
    assert tuple(payload) == (ERROR_KEY,)
    error = payload[ERROR_KEY]
    assert tuple(error) == TOOL_ERROR_KEYS
    assert error["code"] == NOT_FOUND
    assert error["rule_id"] is None
    assert isinstance(error["message"], str)
    assert error["message"] != ""
    # ``details`` is an array, and a detail item about an input field names its path under
    # ``field``; the id this call asked for is that field (requirement 13.6).
    assert isinstance(error["details"], list)
    assert only(error["details"])["field"] == "id"


# --- the two JSON-RPC errors (requirements 14.6 and 14.10) -------------------------------


@pytest.mark.anyio
@pytest.mark.req("14.6")
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_an_unknown_tool_is_invalid_params(env: Env, tmp_path: Path, transport: str) -> None:
    async with wire_over(env, tmp_path, transport) as wire:
        answer = await wire.ask(TOOLS_CALL, call_params(UNKNOWN_TOOL, {}))

    # A JSON-RPC error rather than a tool result: no ``result`` key at all, and the error
    # carries ``code`` and ``message`` alone - ``data`` is None and left out
    # (requirement 14.6).
    error = answer.error
    assert tuple(error) == JSONRPC_ERROR_KEYS
    assert DATA_KEY not in error
    assert error["code"] == types.INVALID_PARAMS
    # The message names the tool that was asked for, so a client sees which name was not
    # there.
    assert UNKNOWN_TOOL in error["message"]
    assert error["message"] == UNKNOWN_TOOL_MESSAGE.format(tool=UNKNOWN_TOOL)


@pytest.mark.anyio
@pytest.mark.req("14.10")
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_a_method_outside_the_whitelist_is_method_not_found(
    env: Env, tmp_path: Path, transport: str
) -> None:
    async with wire_over(env, tmp_path, transport) as wire:
        refused = await wire.ask(UNLISTED_METHOD)
        # The session goes on serving the requests after it (requirement 14.10).
        listed = await wire.ask(TOOLS_LIST)

    error = refused.error
    assert tuple(error) == JSONRPC_ERROR_KEYS
    assert DATA_KEY not in error
    assert error["code"] == types.METHOD_NOT_FOUND
    assert error["message"] == METHOD_NOT_FOUND_MESSAGE.format(method=UNLISTED_METHOD)
    assert tuple(listed.result) == (TOOLS_KEY,)


# --- the two transports against each other (requirement 24.1) ----------------------------


@pytest.mark.anyio
@pytest.mark.req("13.1", "14.4")
async def test_both_transports_answer_the_same_call_with_the_same_bytes(
    env: Env, tmp_path: Path
) -> None:
    # The basis of the comparison: the same instance configuration, the same initial
    # dataset and the same anchor date, two sandboxes freshly created from the same
    # Snapshot, the same request bodies in the same order and therefore the same request
    # ids. Nothing is stripped before comparing, because nothing of the session reaches
    # the payload: the only session-dependent value of a Streamable HTTP exchange is the
    # ``Mcp-Session-Id``, which travels in a header, and a stdio session has no envelope of
    # its own at all. The call is the read of :data:`RETIRED_VENDOR`, whose result carries
    # no generated id and no timestamp.
    answers: dict[str, tuple[bytes, bytes]] = {}
    for transport in TRANSPORTS:
        async with wire_over(env, tmp_path / transport, transport) as wire:
            called = await wire.ask(TOOLS_CALL, call_params(VENDOR_GET, {"id": RETIRED_VENDOR}))
            answers[transport] = (wire.initialized.raw, called.raw)

    handshake, result = answers[HTTP]
    assert answers[STDIO] == (handshake, result)
    # Not vacuously equal: both halves really are the messages of this exchange.
    assert INSTRUCTIONS_KEY.encode() in handshake
    assert NULLS_ON_THE_WIRE.encode() in result
