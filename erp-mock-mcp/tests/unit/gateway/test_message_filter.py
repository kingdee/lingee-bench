"""What the filter answers itself, what it passes on, and what it tells the transport.

:mod:`erp_mock.gateway.message_filter` is one coroutine over three streams, so most tests
here drive it directly: a list of messages goes into the stream a transport would write,
and :func:`pump` collects what came out on the two streams - the one ``Server.run`` reads
and the one the client reads - together with the order in which the hooks were called
(:class:`Run`). That is the whole observable behaviour of the filter, and it needs no SDK
session and no sandbox.

Three tests need more than that:

- ``test_a_session_goes_on_after_a_method_it_does_not_serve`` runs a real session - the
  filter, a low-level ``Server`` built by
  :class:`~erp_mock.gateway.handlers.SessionHandlers` and the official ``ClientSession``
  over the SDK memory streams, against a real sandbox - because requirement 14.10 is about
  what happens to the session *after* a refused request, and requirement 14.11 about every
  message the service ever writes;
- ``test_a_request_that_takes_no_lock_keeps_its_sandbox_active`` wires the activity hook to
  a real sandbox of a real Sandbox_Manager, which is the seam the two transports will use
  (task 22.3 and task 22.4): the filter reports the arrival,
  :meth:`~erp_mock.gateway.sandbox.SandboxRuntime.mark_activity` records it and the
  janitor's scan reads it. A ping is the request this matters for - it takes no sandbox
  lock and so runs no job (:mod:`erp_mock.gateway.handlers`) - and requirement 15.5 counts
  it as a call all the same;
- the two tests of the stream ownership call the filter with streams of their own, since
  what they assert is which stream is closed when it returns.

No test reads the system clock (requirement 25.10): the manager is given a
:class:`~erp_mock.gateway.clock.SystemClock` over fixed instants and :class:`Ticker`, a
monotonic source the test moves itself, and the thresholds of :data:`LIMITS` are seconds of
that source rather than the five minutes of the service.

The business configuration is the engine test fixture (:mod:`tests.helpers.fixtures`) and
the Snapshot its sandboxes are created from is the cheapest one that exists: the tables of
the fixture and nothing in them. Nothing here calls a tool, so nothing here needs a record.
"""

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import anyio
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage

from erp_mock.config.instance import DatasetDef
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import INIT_OPTIONS, McpSessionContext, SessionHandlers
from erp_mock.gateway.message_filter import (
    ALLOWED_METHODS,
    INITIALIZE,
    METHOD_NOT_FOUND_MESSAGE,
    FilterHooks,
    InitializeFailed,
    InitializeHook,
    message_filter,
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
from tests.helpers.fixtures import DATASET, INSTANCE_DIR, fixture_loaded

# The session the requests of this file arrive on; the transports assign such an id and
# nothing of the filter depends on the value.
SESSION: Final = "se_0123456789abcdef"

# Upper bound for every test: a hang fails the test instead of blocking the run.
TIMEOUT_S: Final = 10

# The definition file of the initial dataset of the fixture, relative to its instance
# directory, as loading step 4 reads it.
DATASET_FILE: Final = f"datasets/{DATASET}.yaml"

# The instant the first reading of the test clock returns; the nth reading is this plus
# n - 1 seconds, so the ``ts`` of two events differ and neither is the system time.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# The thresholds the one test that drives the janitor runs with: seconds of its own
# monotonic source, and three different numbers so that it is plain which threshold a
# transition was made on.
LIMITS: Final = SandboxLimits(idle_seconds=60, freeze_seconds=300, retention_seconds=900)

# The JSON-RPC codes a request may be answered with here (requirements 14.10 and 15.14).
METHOD_NOT_FOUND: Final = -32601
INTERNAL_ERROR: Final = -32603

# Methods outside the whitelist: the three the MCP specification defines for capabilities
# this service does not offer, one more it does not implement, and two names of no protocol
# at all (requirement 14.10).
REFUSED_METHODS: Final = (
    "resources/list",
    "prompts/list",
    "resources/templates/list",
    "completion/complete",
    "tools/call/extra",
    "erp_mock/not_a_method",
)

# What the stdio runtime's initialize hook would report when the Snapshot of its initial
# dataset does not build: the message of requirement 15.14, which names the dataset and the
# anchor date and is the hook's to write, not the filter's.
SNAPSHOT_FAILED: Final = "初始数据集 fx-basic 在锚点日期 2026-10-09 的快照构建失败"

# What a hook that fails for a reason nobody planned for raises.
UNEXPECTED: Final = "钩子里的意外失败"

# The steps :class:`Run` records, in the vocabulary of the filter's two hooks and its two
# output streams.
ACTIVITY: Final = "activity"
INITIALIZED: Final = "initialize"
FORWARDED: Final = "forwarded"
ANSWERED: Final = "answered"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- the test clock (requirement 25.10) --------------------------------------------------


@dataclass(slots=True)
class Ticker:
    """A monotonic source the test moves itself (requirement 25.10).

    Every reading returns :attr:`seconds`, which :meth:`advance` raises; the janitor reads
    it once per scan and measures every threshold against that reading, so a test says how
    much time has passed by saying how far to move this.
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


# --- the messages a client sends ---------------------------------------------------------


def request(request_id: int, method: str, params: dict[str, Any] | None = None) -> SessionMessage:
    """One JSON-RPC request, as a transport puts it on its read stream."""
    return SessionMessage(
        message=types.JSONRPCMessage(
            types.JSONRPCRequest(jsonrpc="2.0", id=request_id, method=method, params=params)
        )
    )


def notification(method: str) -> SessionMessage:
    """One JSON-RPC notification, which carries no id and cannot be answered."""
    return SessionMessage(
        message=types.JSONRPCMessage(types.JSONRPCNotification(jsonrpc="2.0", method=method))
    )


def response(request_id: int) -> SessionMessage:
    """One JSON-RPC response, as a client answers a request of the server with."""
    return SessionMessage(
        message=types.JSONRPCMessage(types.JSONRPCResponse(jsonrpc="2.0", id=request_id, result={}))
    )


def method_of(message: SessionMessage | Exception) -> str | None:
    """The method of ``message``, None where it has none (a response) or is an exception."""
    if isinstance(message, Exception):
        return None
    root = message.message.root
    if isinstance(root, types.JSONRPCRequest | types.JSONRPCNotification):
        return root.method
    return None


def error_of(message: SessionMessage) -> types.JSONRPCError:
    """``message`` as the JSON-RPC error it is; fails the test when it is anything else."""
    root = message.message.root
    assert isinstance(root, types.JSONRPCError), root
    return root


# --- one pass of the filter --------------------------------------------------------------


@dataclass(slots=True)
class Run:
    """What one pass of the filter did, in the order it happened.

    ``steps`` is the sequence of the four things that can happen to a message - the two
    hooks being called, a message reaching the stream ``Server.run`` reads, a message
    reaching the client - which is how a test states not only that the filter did
    something but when it did it. ``forwarded`` and ``answered`` are the messages
    themselves, and ``seen`` the requests the initialize hook was given.
    """

    steps: list[str] = field(default_factory=list)
    forwarded: list[SessionMessage | Exception] = field(default_factory=list)
    answered: list[SessionMessage] = field(default_factory=list)
    seen: list[types.JSONRPCRequest] = field(default_factory=list)

    def activity(self) -> None:
        """The ``activity`` hook: records the arrival of one request (requirement 15.5)."""
        self.steps.append(ACTIVITY)

    def initialize(self, failure: Exception | None = None) -> InitializeHook:
        """An ``initialize`` hook that records the request and then raises ``failure``."""

        async def hook(incoming: types.JSONRPCRequest) -> None:
            self.steps.append(INITIALIZED)
            self.seen.append(incoming)
            if failure is not None:
                raise failure

        return hook

    def hooks(self, *, activity: bool, initialize: bool, failure: Exception | None) -> FilterHooks:
        """The hooks of one pass: the ones the test asked for, recording into this run."""
        return FilterHooks(
            activity=self.activity if activity else None,
            initialize=self.initialize(failure) if initialize else None,
        )

    async def collect_forwarded(
        self, source: MemoryObjectReceiveStream[SessionMessage | Exception]
    ) -> None:
        """Read what the filter passed on until it closes the stream."""
        async with source:
            async for message in source:
                self.steps.append(FORWARDED)
                self.forwarded.append(message)

    async def collect_answered(self, source: MemoryObjectReceiveStream[SessionMessage]) -> None:
        """Read what the filter answered itself until the pass is over."""
        async with source:
            async for message in source:
                self.steps.append(ANSWERED)
                self.answered.append(message)

    def methods(self) -> list[str | None]:
        """The methods of the messages that were passed on, in order."""
        return [method_of(message) for message in self.forwarded]


async def pump(
    messages: Sequence[SessionMessage | Exception], hooks: FilterHooks, run: Run
) -> None:
    """Run the filter over ``messages`` with ``hooks``, collecting into ``run``.

    The three streams are the ones the transports hand over: ``src`` carries what the
    client sent and is closed when the last message is in, ``dst`` is the stream
    ``Server.run`` would read and is closed by the filter itself, and ``out`` goes to the
    client. Neither ``dst`` nor ``out`` is buffered, so a message is in ``run.steps``
    before the filter looks at the next one, and ``out`` is closed here rather than by the
    filter, which is what the two transports rely on (task 22.3 and task 22.4).
    """
    to_filter, src = anyio.create_memory_object_stream[SessionMessage | Exception](
        len(messages) + 1
    )
    dst, to_sdk = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    out, to_client = anyio.create_memory_object_stream[SessionMessage](0)
    with anyio.fail_after(TIMEOUT_S):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run.collect_forwarded, to_sdk)
            tg.start_soon(run.collect_answered, to_client)
            async with to_filter:
                for message in messages:
                    await to_filter.send(message)
            try:
                await message_filter(src, dst, out, hooks)
            finally:
                await out.aclose()


async def drive(
    messages: Sequence[SessionMessage | Exception],
    *,
    activity: bool = True,
    initialize: bool = False,
    failure: Exception | None = None,
) -> Run:
    """One pass of the filter over ``messages``, with hooks that record what they were asked.

    ``activity`` and ``initialize`` say which hooks the transport provides: an
    ``initialize`` hook is the stdio runtime's and absent for Streamable HTTP
    (design.md 7.3), and ``failure`` is what that hook raises.
    """
    run = Run()
    await pump(messages, run.hooks(activity=activity, initialize=initialize, failure=failure), run)
    return run


# --- the method whitelist (requirement 14.10) --------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("14.10")
@pytest.mark.parametrize("method", REFUSED_METHODS)
async def test_a_method_outside_the_whitelist_is_answered_with_32601(method: str) -> None:
    run = await drive([request(7, method, {"cursor": None})])

    # The one thing the client receives is the error of requirement 14.10, carrying the id
    # of the request it answers and naming the method that was asked for.
    answered = error_of(only(run.answered))
    assert answered.id == 7
    assert answered.error.code == METHOD_NOT_FOUND
    assert answered.error.message == METHOD_NOT_FOUND_MESSAGE.format(method=method)
    assert method in answered.error.message
    # ``data`` is None and left off the wire, so the error object carries two keys.
    assert json.loads(answered.model_dump_json(by_alias=True, exclude_none=True))["error"] == {
        "code": METHOD_NOT_FOUND,
        "message": answered.error.message,
    }

    # And the SDK never sees the request: the code of the answer is then the filter's
    # alone, whichever code this SDK version would have given the method itself.
    assert run.forwarded == []
    # The arrival was still reported: a refused request is an MCP request (req 15.5).
    assert run.steps == [ACTIVITY, ANSWERED]


@pytest.mark.anyio
@pytest.mark.req("14.10")
async def test_the_whitelisted_methods_reach_the_sdk_unchanged() -> None:
    # The four methods of requirement 14.10, each as it arrived.
    sent = [request(n, method) for n, method in enumerate(sorted(ALLOWED_METHODS))]

    run = await drive(sent)

    assert run.forwarded == sent
    assert run.answered == []
    assert sorted(ALLOWED_METHODS) == ["initialize", "ping", "tools/call", "tools/list"]


@pytest.mark.anyio
@pytest.mark.req("14.10")
async def test_notifications_and_responses_are_passed_on_whatever_their_method() -> None:
    # A notification carries no id, so JSON-RPC has no answer for it: an unknown one is
    # passed on rather than refused, and so is the one the handshake ends with.
    sent: list[SessionMessage | Exception] = [
        notification("notifications/initialized"),
        notification("notifications/cancelled"),
        notification("notifications/nothing_like_it"),
        response(1),
        # A body the transport could not parse reaches the stream in place of a message;
        # what it means is the SDK's to decide, so it is passed on as it is.
        ValueError("无法解析的请求体"),
    ]

    run = await drive(sent)

    assert run.forwarded == sent
    assert run.answered == []
    # None of them is a request, so none of them counts as activity either.
    assert run.steps == [FORWARDED] * len(sent)


@pytest.mark.anyio
@pytest.mark.req("14.10", "15.5")
async def test_every_arriving_request_is_reported_before_it_is_looked_at() -> None:
    sent: list[SessionMessage | Exception] = [
        request(1, "tools/list"),
        request(2, "resources/list"),
        notification("notifications/initialized"),
        request(3, "ping"),
        ValueError("无法解析的请求体"),
        request(4, "prompts/list"),
    ]

    run = await drive(sent)

    # One report per request, before whatever happened to it, and none for the two
    # messages that are not requests (requirement 15.5).
    assert run.steps == [
        ACTIVITY,
        FORWARDED,
        ACTIVITY,
        ANSWERED,
        FORWARDED,
        ACTIVITY,
        FORWARDED,
        FORWARDED,
        ACTIVITY,
        ANSWERED,
    ]
    assert run.methods() == [
        "tools/list",
        "notifications/initialized",
        "ping",
        None,
    ]


@pytest.mark.anyio
@pytest.mark.req("14.10")
async def test_a_transport_without_hooks_still_passes_everything_on() -> None:
    sent = [request(1, "tools/list"), request(2, "resources/list")]

    run = await drive(sent, activity=False)

    assert run.forwarded == [sent[0]]
    assert error_of(only(run.answered)).error.code == METHOD_NOT_FOUND
    assert ACTIVITY not in run.steps


# --- the initialize hook (design.md 7.3, requirements 15.2 and 15.14) --------------------


@pytest.mark.anyio
@pytest.mark.req("14.10")
async def test_the_initialize_hook_runs_before_initialize_reaches_the_sdk() -> None:
    sent = [request(1, INITIALIZE, {"protocolVersion": "2025-06-18"})]

    run = await drive(sent, initialize=True)

    # The hook came first, and the request reached the SDK afterwards, unchanged: a
    # session is never initialized before its sandbox exists (design.md 7.3).
    assert run.steps == [ACTIVITY, INITIALIZED, FORWARDED]
    assert run.forwarded == sent
    assert run.answered == []
    # The hook is given the request as it arrived.
    assert only(run.seen).params == {"protocolVersion": "2025-06-18"}


@pytest.mark.anyio
@pytest.mark.req("14.10")
async def test_a_second_initialize_calls_the_hook_again() -> None:
    sent = [request(1, INITIALIZE), request(2, INITIALIZE)]

    run = await drive(sent, initialize=True)

    # Both are handed to the hook and both reach the SDK: what a second initialize means -
    # the same sandbox and one more session_start (requirement 15.2) - is the hook's to
    # decide, and the filter keeps nothing of the first one.
    assert run.steps == [ACTIVITY, INITIALIZED, FORWARDED, ACTIVITY, INITIALIZED, FORWARDED]
    assert [incoming.id for incoming in run.seen] == [1, 2]
    assert run.forwarded == sent


@pytest.mark.anyio
@pytest.mark.req("14.10")
async def test_without_an_initialize_hook_the_handshake_goes_straight_through() -> None:
    # What Streamable HTTP passes: its router created the sandbox and wrote session_start
    # in the ASGI layer, long before the transport read the request (design.md 7.2).
    sent = [request(1, INITIALIZE)]

    run = await drive(sent)

    assert run.steps == [ACTIVITY, FORWARDED]
    assert run.forwarded == sent
    assert run.seen == []


@pytest.mark.anyio
@pytest.mark.req("15.14")
async def test_an_initialize_the_hook_cannot_serve_is_answered_with_32603() -> None:
    sent = [request(1, INITIALIZE), request(2, "ping")]

    run = await drive(sent, initialize=True, failure=InitializeFailed(SNAPSHOT_FAILED))

    # The code is the filter's, the message the hook's: it names the initial dataset and
    # the anchor date, which the filter does not know (requirement 15.14).
    answered = error_of(only(run.answered))
    assert answered.id == 1
    assert answered.error.code == INTERNAL_ERROR
    assert answered.error.message == SNAPSHOT_FAILED
    # The SDK never sees the initialize, so no session is initialized on a sandbox that
    # was not created; the request after it is treated as any other.
    assert run.forwarded == [sent[1]]
    assert run.steps == [ACTIVITY, INITIALIZED, ANSWERED, ACTIVITY, FORWARDED]


@pytest.mark.anyio
@pytest.mark.req("15.14")
async def test_an_unexpected_failure_of_the_hook_is_not_turned_into_an_answer() -> None:
    # Driven without :func:`pump`, whose task group would wrap the exception: what this
    # asserts is the exception the transport's task group receives.
    to_filter, src = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    dst, to_sdk = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    out, to_client = anyio.create_memory_object_stream[SessionMessage](1)
    await to_filter.send(request(1, INITIALIZE))
    await to_filter.aclose()
    run = Run()
    hooks = run.hooks(activity=True, initialize=True, failure=RuntimeError(UNEXPECTED))

    # Only InitializeFailed is an answer the filter knows how to write; anything else
    # reaches the task group of the transport, which ends the session rather than sending
    # an error whose text nobody wrote.
    with anyio.fail_after(TIMEOUT_S), pytest.raises(RuntimeError, match=UNEXPECTED):
        await message_filter(src, dst, out, hooks)

    assert run.steps == [ACTIVITY, INITIALIZED]
    assert to_client.statistics().current_buffer_used == 0
    assert to_sdk.statistics().current_buffer_used == 0


# --- what the two transports rely on: which stream the filter closes ---------------------


@pytest.mark.anyio
@pytest.mark.req("14.10")
async def test_the_filter_closes_the_sdk_stream_and_leaves_the_client_stream_open() -> None:
    to_filter, src = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    dst, to_sdk = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    out, to_client = anyio.create_memory_object_stream[SessionMessage](1)
    await to_filter.send(request(1, "ping"))
    await to_filter.aclose()

    with anyio.fail_after(TIMEOUT_S):
        await message_filter(src, dst, out, FilterHooks())

    # The end of the client's side ends the filter, and the filter closes the stream
    # ``Server.run`` reads, which is what lets ``Server.run`` return.
    with pytest.raises(anyio.ClosedResourceError):
        await dst.send(request(2, "ping"))
    # The client's stream is not the filter's to close: the SDK session holds it too and
    # closes it itself when its receive loop ends.
    await out.send(request(2, "ping"))
    assert to_client.statistics().current_buffer_used == 1
    assert to_sdk.statistics().current_buffer_used == 1


# --- the instance configuration, its sandboxes and one live session ----------------------


@dataclass(frozen=True, slots=True)
class Env:
    """What every test that needs a sandbox starts from, built once for the module."""

    loaded: LoadedInstance
    schema: StorageSchema
    layer: ToolLayer
    dataset: DatasetModel
    snapshot: Path

    @property
    def profile(self) -> ProfileModel:
        """The one profile of the fixture instance, whose tools a session sees."""
        return self.loaded.default_profile


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Env:
    """The fixture instance, its tools layer and one Snapshot of its initial dataset."""
    loaded = fixture_loaded()
    schema = build_schema(loaded.domain)
    definition = DatasetDef.model_validate(load_yaml(INSTANCE_DIR / DATASET_FILE))
    return Env(
        loaded=loaded,
        schema=schema,
        layer=ToolLayer(loaded),
        dataset=DatasetModel(definition=definition, file=DATASET_FILE),
        snapshot=build_snapshot(tmp_path_factory.mktemp("snapshot"), schema),
    )


def build_snapshot(directory: Path, schema: StorageSchema) -> Path:
    """Build the one Snapshot of this file below ``directory``: the tables and nothing in them."""
    build = directory / "build.sqlite"
    snapshot = directory / "snapshot.sqlite"
    build_snapshot_db(build, schema).close()
    finalize_snapshot(build, snapshot)
    build.unlink()
    return snapshot


@dataclass(slots=True)
class Snapshots:
    """A :class:`~erp_mock.gateway.sandbox.SnapshotSource` over one Snapshot file."""

    db: Path

    @contextmanager
    def snapshot(self, dataset: DatasetModel, anchor: date) -> Iterator[Path]:
        yield self.db


@dataclass(slots=True)
class Service:
    """A Sandbox_Manager over a data directory of its own, and the clock it reads."""

    manager: SandboxManager
    ticker: Ticker
    env: Env

    def hooks(self, sb: SandboxRuntime) -> FilterHooks:
        """The hooks a transport of the session on ``sb`` provides (task 22.3, task 22.4)."""
        return FilterHooks(activity=lambda: sb.mark_activity(self.manager.clock.monotonic()))


@contextmanager
def service_over(env: Env, data_dir: Path) -> Iterator[Service]:
    """Yield a manager over ``data_dir``, closing it and its thread pool on the way out."""
    ticker = Ticker()
    manager = SandboxManager(
        data_dir=data_dir,
        loaded=env.loaded,
        schema=env.schema,
        snapshots=Snapshots(env.snapshot),
        clock=stepping_clock(ticker),
        limits=LIMITS,
    )
    try:
        yield Service(manager=manager, ticker=ticker, env=env)
    finally:
        manager.close()


@dataclass(slots=True)
class Live:
    """A session that is connected and initialized through the filter."""

    client: ClientSession
    sandbox: SandboxRuntime
    sent: list[SessionMessage]

    def wire_messages(self) -> list[dict[str, Any]]:
        """Every message the service wrote, as the two transports serialize it."""
        return [
            json.loads(message.message.model_dump_json(by_alias=True, exclude_none=True))
            for message in self.sent
        ]

    def events(self) -> list[dict[str, Any]]:
        """The events of the trace file of the sandbox, one per line (requirement 17.1)."""
        path = self.sandbox.directory / TRACE_FILENAME
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line]


async def tap(
    source: MemoryObjectReceiveStream[SessionMessage],
    sink: MemoryObjectSendStream[SessionMessage],
    record: list[SessionMessage],
) -> None:
    """Forward every message from ``source`` to ``sink``, recording it first."""
    async with source, sink:
        async for message in source:
            record.append(message)
            await sink.send(message)


@asynccontextmanager
async def session_over(service: Service) -> AsyncIterator[Live]:
    """One initialized session on a new sandbox, with the filter in front of its server.

    What the service runs: the filter between the transport's read stream and the stream
    ``Server.run`` reads, both writing to the one stream that goes to the client, which a
    tap records on the way (design.md architecture).
    """
    sandbox = await service.manager.create(service.env.profile, service.env.dataset)
    sandbox.bind(SESSION)
    session = McpSessionContext.of(SESSION, sandbox)
    server = SessionHandlers(service.manager, service.env.layer).build_server(session)
    recorded: list[SessionMessage] = []
    with anyio.fail_after(TIMEOUT_S):
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_to_client = server_streams
            to_sdk, filtered = anyio.create_memory_object_stream[SessionMessage | Exception](0)
            server_write, tapped = anyio.create_memory_object_stream[SessionMessage](1)
            async with anyio.create_task_group() as tg:
                tg.start_soon(tap, tapped, server_to_client, recorded)
                tg.start_soon(
                    message_filter, server_read, to_sdk, server_write, service.hooks(sandbox)
                )
                # raise_exceptions stays false, as in the transports (design.md 7.2).
                tg.start_soon(server.run, filtered, server_write, INIT_OPTIONS)
                try:
                    async with ClientSession(client_read, client_write) as client:
                        await client.initialize()
                        yield Live(client=client, sandbox=sandbox, sent=recorded)
                finally:
                    tg.cancel_scope.cancel()


def only[T](items: Sequence[T]) -> T:
    """The one item of ``items``; fails the test when there is not exactly one."""
    assert len(items) == 1, items
    return items[0]


# --- one live session (requirements 14.10 and 14.11) -------------------------------------


@pytest.mark.anyio
@pytest.mark.req("14.10", "14.11")
async def test_a_session_goes_on_after_a_method_it_does_not_serve(env: Env, tmp_path: Path) -> None:
    with service_over(env, tmp_path) as service:
        async with session_over(service) as live:
            # A method of a capability this service does not offer, sent by the official
            # client: the code is -32601 and the message names the method.
            with pytest.raises(McpError) as raised:
                await live.client.send_request(
                    types.ClientRequest(types.ListResourcesRequest()),
                    types.ListResourcesResult,
                )
            assert raised.value.error.code == METHOD_NOT_FOUND
            assert "resources/list" in raised.value.error.message

            # The session is unharmed: the requests after it are answered as they would
            # have been without it (requirement 14.10).
            listed = await live.client.list_tools()
            assert tuple(tool.name for tool in listed.tools) == env.profile.tool_names
            await live.client.send_ping()

            # The refused request wrote no trace event; the tools/list after it did.
            assert [event["event"] for event in live.events()] == [EventName.TOOLS_LIST]

            # Requirement 14.11: everything the service wrote is an answer to a request of
            # the client - a result or an error - and nothing carries a method, so the
            # service issued no request and sent no notification of its own.
            written = live.wire_messages()
            assert [message for message in written if "method" in message] == []
            assert len(written) == 4
            assert [sorted(message) for message in written] == [
                ["id", "jsonrpc", "result"],
                ["error", "id", "jsonrpc"],
                ["id", "jsonrpc", "result"],
                ["id", "jsonrpc", "result"],
            ]


# --- the activity seam (requirement 15.5) ------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.5")
async def test_a_request_that_takes_no_lock_keeps_its_sandbox_active(
    env: Env, tmp_path: Path
) -> None:
    with service_over(env, tmp_path) as service:
        sandbox = await service.manager.create(env.profile, env.dataset)
        run = Run()
        # Halfway to the idle threshold, a ping arrives: it takes no sandbox lock and runs
        # no job, so the filter's report is the only thing that records it.
        service.ticker.advance(LIMITS.idle_seconds / 2)
        await pump([request(1, "ping")], service.hooks(sandbox), run)
        assert sandbox.last_done == service.ticker.seconds

        # The threshold is now measured from the ping: a scan that would have found the
        # sandbox idle without it leaves it active (requirements 15.5 and 15.6).
        service.ticker.advance(LIMITS.idle_seconds * 0.9)
        await service.manager.sweep()
        assert sandbox.status == "active"

        # And nothing else keeps it active: once the threshold passes the ping, the same
        # scan turns it idle.
        service.ticker.advance(LIMITS.idle_seconds * 0.2)
        await service.manager.sweep()
        assert sandbox.status == "idle"
