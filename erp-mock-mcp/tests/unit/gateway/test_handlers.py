"""What one MCP session answers, and what it writes to the trace while doing it.

:mod:`erp_mock.gateway.handlers` is exercised as a client reaches it: a real low-level
``Server`` built by :class:`~erp_mock.gateway.handlers.SessionHandlers` runs over the SDK's
memory streams with the official ``ClientSession`` on the other end
(:func:`session_over`), against a real sandbox of the engine test fixture. Each test
therefore asserts three things of one request: what the client parses, what the transports
would write on the wire - the recorded messages are serialized exactly as the stdio and
Streamable HTTP transports serialize them (V2 of tests/probes/test_sdk_server.py) - and
what the sandbox's trace.jsonl holds afterwards.

The four groups:

- initialize: capabilities carries ``tools`` alone with ``listChanged`` false, and
  ``instructions`` is the one fixed sentence, short enough and naming no tool and no rule
  (requirement 14.4);
- tools/list: every tool of the profile in one response, in the order the Loader froze
  them, with no cursor to continue from, plus its ``tools_list`` event (requirement 14.5);
- tools/call: a successful write, a business error that rolled back and an unknown tool
  name, each with the ``tools_call`` event requirements 17.3, 17.5, 17.10 and 17.11
  describe - the keys and their order, ``known_tool``, ``error_code``, ``rule_id``,
  ``coerced``, ``writes`` and ``latency_ms``;
- ping: answered without touching the sandbox and without an event (requirement 17.9).

The business configuration is the engine test fixture (:mod:`tests.helpers.fixtures`), not
the example instance: it exposes the same kinds of tools and loads in milliseconds, and the
example instance belongs to the contract and integration tests. The Snapshot every sandbox
here is created from is built once for the module, from the standard basic data of
:mod:`tests.helpers.engine_env`, so a call has records to point at; it reaches the manager
through :class:`Snapshots`, a double of the
:class:`~erp_mock.gateway.sandbox.SnapshotSource` the service builds over the Snapshot
cache.

No test reads the system clock (requirement 25.10). The manager is given a
:class:`~erp_mock.gateway.clock.SystemClock` over :data:`INSTANT` and :class:`Ticker`, a
monotonic source that advances by :data:`STEP_SECONDS` per reading. The handlers take
exactly two readings of it per tools/call - the arrival of the request and the moment just
before its event is written, with the Sandbox_Manager's own reading for the idle timing
after them - so every ``latency_ms`` of this file is :data:`STEP_MS`, and a handler that
measured the latency against anything but the monotonic clock of the service would report
something else.

The events a session_start and a session_end belong to are written by the transports (the
router of task 22.3 and the stdio runtime of task 22.4), which is why an accepted
initialize leaves no event here (requirement 17.9).
"""

import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
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

from erp_mock import FRAMEWORK_VERSION
from erp_mock.common.errors import ErrorCode
from erp_mock.common.jsonio import dumps_compact
from erp_mock.config.instance import DatasetDef
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import (
    INIT_OPTIONS,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    UNKNOWN_TOOL_MESSAGE,
    McpSessionContext,
    SessionHandlers,
)
from erp_mock.gateway.sandbox import SandboxManager, SandboxRuntime
from erp_mock.gateway.trace import (
    COMMON_KEYS,
    TOOLS_CALL_KEYS,
    TRACE_FILENAME,
    WRITE_KEYS,
    EventName,
)
from erp_mock.loader.instance import TOOLS_KEY, DatasetModel, LoadedInstance, ProfileModel
from erp_mock.loader.yaml_io import load_yaml
from erp_mock.state.schema import EFFECTIVE_WRITES_TABLE, StorageSchema, build_schema
from erp_mock.state.snapshot import build_snapshot_db, finalize_snapshot
from erp_mock.state.store import DIRECT_CAUSE, Value
from erp_mock.tools.layer import ToolLayer
from erp_mock.tools.results import CODE, DETAILS, ERROR_KEY, MESSAGE, RULE_ID
from tests.helpers.engine_env import CURRENT_USER, create_basic_data
from tests.helpers.fixtures import ANCHOR_DATE, DATASET, INSTANCE_DIR, fixture_loaded

# The session every test of this file runs as; the transports assign such an id
# (requirement 24.6) and nothing of the handlers depends on the value.
SESSION: Final = "se_0123456789abcdef"

# Upper bound for every test: a hang fails the test instead of blocking the run.
TIMEOUT_S: Final = 10

# The instant the first reading of the test clock returns; the nth reading is this plus
# n - 1 seconds, so the ``ts`` of two events differ and neither is the system time.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# How far the monotonic source moves per reading, and what one step is in whole
# milliseconds: the latency of every tools/call of this file (module docstring).
STEP_SECONDS: Final = 0.25
STEP_MS: Final = 250

# The definition file of the initial dataset of the fixture, relative to its instance
# directory, as loading step 4 reads it.
DATASET_FILE: Final = f"datasets/{DATASET}.yaml"

# How many Unicode characters the instructions of an initialize may carry (req 14.4).
MAX_INSTRUCTIONS: Final = 200

# The tools of the fixture a test calls, and the records of the basic data they name
# (:data:`~tests.helpers.engine_env.BASIC_DATA`): the ids are fixed by the order the
# records are created in.
COUNT_CREATE: Final = "count_sheet_create"
COUNT_SEARCH: Final = "count_sheet_search"
FEE_CREATE: Final = "fee_note_create"
COUNT_SHEET: Final = "count_sheet"
COUNT_SHEET_LINE: Final = "count_sheet_line"
FEE_NOTE: Final = "fee_note"
DEPOT: Final = "dep_000001"
WASHER: Final = "art_000002"
RETIRED_VENDOR: Final = "ven_000003"

# A call of ``count_sheet_create`` that goes through: the two required header fields and
# one line, whose quantity arrives as a decimal string and is therefore reported as
# coerced (requirements 12.5 and 17.11).
NEW_COUNT: Final[Mapping[str, Any]] = {
    "depot_id": DEPOT,
    "count_kind": "gain",
    "lines": [{"article_id": WASHER, "diff_qty": "3"}],
}
COERCED_COUNT: Final = ["lines[0].diff_qty"]

# A call of ``fee_note_create`` that ends with a rule violation: the vendor it references
# is disabled, which R-GEN-006 forbids (requirement 5.7). Its two decimal strings are
# coerced before the rule is checked, so the event of a failed call reports them
# (requirement 17.11).
DISABLED_FEE: Final[Mapping[str, Any]] = {
    "vendor_id": RETIRED_VENDOR,
    "due_date": "2026-11-09",
    "lines": [{"item_text": "夹具运费", "qty": "1", "price": "100"}],
}
COERCED_FEE: Final = ["lines[0].qty", "lines[0].price"]
DISABLED_RULE: Final = "R-GEN-006"

# A tool name no profile of the fixture exposes (requirements 14.6 and 17.10).
UNKNOWN_TOOL: Final = "no_such_tool"

# The JSON-RPC code of an unknown tool name (requirement 14.6).
INVALID_PARAMS: Final = -32602


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- the test clock (requirement 25.10) --------------------------------------------------


@dataclass(slots=True)
class Ticker:
    """A monotonic source that advances by ``step`` per reading, driven by the test.

    The handlers read it twice per tools/call - when the request arrives and just before
    the event of the call is written - and the Sandbox_Manager once more per request, for
    the idle timing. ``reads`` counts the readings, so a test can see that a handler took
    the readings it was supposed to take.
    """

    step: float = STEP_SECONDS
    base: float = 1000.0
    reads: int = 0

    def __call__(self) -> float:
        value = self.base + self.step * self.reads
        self.reads += 1
        return value


def stepping_clock(ticker: Ticker) -> SystemClock:
    """A clock whose nth instant is :data:`INSTANT` plus n - 1 seconds, over ``ticker``."""
    readings = iter(range(1 << 20))
    return SystemClock(now=lambda: INSTANT + timedelta(seconds=next(readings)), monotonic=ticker)


# --- the Snapshot every sandbox is created from ------------------------------------------


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


def build_snapshot(directory: Path, env_schema: StorageSchema, layer: ToolLayer) -> Path:
    """Build the one Snapshot of this file below ``directory`` and return its path.

    A build database with the tables of the fixture and the standard basic data of
    :mod:`tests.helpers.engine_env` in it - the organization, the units, the current user,
    the materials, the warehouses and the partners a call of this file points at - created
    through the engine, one create per record, the way a seed build creates them
    (requirement 20.17), and written out as a Snapshot
    (:func:`~erp_mock.state.snapshot.finalize_snapshot`).
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


# --- the instance configuration under test -----------------------------------------------


@dataclass(frozen=True, slots=True)
class Env:
    """What every test of this file starts from, built once for the module."""

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
    layer = ToolLayer(loaded)
    schema = build_schema(loaded.domain)
    definition = DatasetDef.model_validate(load_yaml(INSTANCE_DIR / DATASET_FILE))
    dataset = DatasetModel(
        definition=definition,
        file=DATASET_FILE,
        # What loading step 8 resolves from the dataset and instance.yaml
        # (requirement 20.5): the current user every call of such a sandbox runs as.
        current_user=CURRENT_USER,
    )
    return Env(
        loaded=loaded,
        schema=schema,
        layer=layer,
        dataset=dataset,
        snapshot=build_snapshot(tmp_path_factory.mktemp("snapshot"), schema, layer),
    )


# --- one live session ---------------------------------------------------------------------


@dataclass(slots=True)
class Live:
    """A session that is connected and initialized, and what a test reads off it."""

    client: ClientSession
    init: types.InitializeResult
    sent: list[SessionMessage]
    sandbox: SandboxRuntime
    session: McpSessionContext
    ticker: Ticker

    async def call(self, tool: str, args: Mapping[str, Any]) -> types.CallToolResult:
        """Send one tools/call and parse its result.

        ``ClientSession.call_tool`` is avoided on purpose: it issues a tools/list of its
        own and validates structuredContent against the outputSchema on the client side,
        neither of which is part of what the server answered.
        """
        request = types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(name=tool, arguments=dict(args))
            )
        )
        return await self.client.send_request(request, types.CallToolResult)

    def wire(self) -> dict[str, Any]:
        """The last message the server wrote, as the two transports serialize it."""
        return self.wire_messages()[-1]

    def wire_messages(self) -> list[dict[str, Any]]:
        """Every message the server wrote, in order (V2 of tests/probes/test_sdk_server.py)."""
        return [
            json.loads(message.message.model_dump_json(by_alias=True, exclude_none=True))
            for message in self.sent
        ]

    def events(self) -> list[dict[str, Any]]:
        """The events of the trace file of the sandbox, one per line (requirement 17.1)."""
        text = (self.sandbox.directory / TRACE_FILENAME).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line]

    def event(self) -> dict[str, Any]:
        """The one event the trace file holds."""
        return only(self.events())

    def rows(self, table: str) -> list[dict[str, Value]]:
        """Every record of ``table`` of the sandbox database; no request is in flight."""
        with self.sandbox.store.transaction() as uow:
            return [dict(row) for row in uow.select(table)]

    def effective_seqs(self) -> list[int]:
        """The call sequence numbers the effective calls of the sandbox name (18.10).

        The table holds one row per (call, record), so the numbers are deduplicated: what
        a test reads here is which calls wrote something, not how much they wrote.
        """
        return sorted({int(str(row["seq"])) for row in self.rows(EFFECTIVE_WRITES_TABLE)})


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
async def session_over(env: Env, data_dir: Path) -> AsyncIterator[Live]:
    """One initialized session on a new sandbox below ``data_dir``.

    The sandbox is created the way the transports create one (requirement 15.1), the
    session is bound to it and its server is run over the SDK's memory streams with
    :data:`~erp_mock.gateway.handlers.INIT_OPTIONS`, a tap recording what the server
    writes. The manager - its sandboxes, its trace writers and its thread pool - is closed
    when the block returns, so a test reads the trace and the database inside it.
    """
    ticker = Ticker()
    manager = SandboxManager(
        data_dir=data_dir,
        loaded=env.loaded,
        schema=env.schema,
        snapshots=Snapshots(env.snapshot),
        clock=stepping_clock(ticker),
    )
    try:
        sandbox = await manager.create(env.profile, env.dataset)
        sandbox.bind(SESSION)
        session = McpSessionContext.of(SESSION, sandbox)
        server = SessionHandlers(manager, env.layer).build_server(session)
        recorded: list[SessionMessage] = []
        with anyio.fail_after(TIMEOUT_S):
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                client_read, client_write = client_streams
                server_read, server_to_client = server_streams
                server_write, tapped = anyio.create_memory_object_stream[SessionMessage](1)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(tap, tapped, server_to_client, recorded)
                    # raise_exceptions stays false, as in the transports (design.md 7.2).
                    tg.start_soon(server.run, server_read, server_write, INIT_OPTIONS)
                    try:
                        async with ClientSession(client_read, client_write) as client:
                            init = await client.initialize()
                            yield Live(
                                client=client,
                                init=init,
                                sent=recorded,
                                sandbox=sandbox,
                                session=session,
                                ticker=ticker,
                            )
                    finally:
                        tg.cancel_scope.cancel()
    finally:
        manager.close()


def only[T](items: Sequence[T]) -> T:
    """The one item of ``items``; fails the test when there is not exactly one."""
    assert len(items) == 1, items
    return items[0]


def text_of(result: types.CallToolResult) -> dict[str, Any]:
    """The one text item of a tool result, parsed (requirements 13.1 and 13.6)."""
    item = only(result.content)
    assert isinstance(item, types.TextContent), item
    payload = json.loads(item.text)
    assert isinstance(payload, dict), payload
    return payload


# --- initialize (requirement 14.4) -------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("14.4", "17.9")
async def test_initialize_offers_tools_alone_with_one_fixed_text(env: Env, tmp_path: Path) -> None:
    async with session_over(env, tmp_path) as live:
        # What the client parses, and what a transport writes: capabilities carries tools
        # alone, with listChanged false and nothing else (requirement 14.4).
        assert live.init.capabilities == INIT_OPTIONS.capabilities
        result = only(live.wire_messages())["result"]
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert result["serverInfo"]["name"] == SERVER_NAME
        assert result["serverInfo"]["version"] == FRAMEWORK_VERSION

        # The instructions are the one fixed sentence: short enough, naming no tool of the
        # profile and no rule, and carrying nothing of the session, the profile, the
        # dataset or the anchor date - the text is a constant of the module.
        assert live.init.instructions == SERVER_INSTRUCTIONS
        assert len(SERVER_INSTRUCTIONS) <= MAX_INSTRUCTIONS
        assert not [name for name in env.profile.tool_names if name in SERVER_INSTRUCTIONS]
        assert DISABLED_RULE[:6] not in SERVER_INSTRUCTIONS
        assert env.dataset.name not in SERVER_INSTRUCTIONS
        assert ANCHOR_DATE.isoformat() not in SERVER_INSTRUCTIONS

        # An accepted initialize writes session_start, which the transport that accepted it
        # writes (requirement 17.9); the handlers of the session write no event for it.
        assert live.events() == []


# --- tools/list (requirements 14.5 and 17.9) ---------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("14.5", "17.9")
async def test_tools_list_returns_every_tool_in_one_response(env: Env, tmp_path: Path) -> None:
    async with session_over(env, tmp_path) as live:
        listed = await live.client.list_tools()

        # Every tool of the profile, in the order of requirement 11.9, in one response.
        assert tuple(tool.name for tool in listed.tools) == env.profile.tool_names
        assert listed.nextCursor is None
        result = live.wire()["result"]
        # No cursor to continue from: ``nextCursor`` is None and left off the wire, so the
        # response carries ``tools`` alone (requirement 14.5).
        assert list(result) == [TOOLS_KEY]
        # And what it carries is the payload the Loader assembled and froze, unchanged.
        assert dumps_compact(result) == dumps_compact(env.profile.payload)

        # One tools_list event, the common fields and nothing else (requirement 17.9).
        event = live.event()
        assert list(event) == list(COMMON_KEYS)
        assert event["event"] == EventName.TOOLS_LIST
        assert event["seq"] == 1
        assert event["session_id"] == SESSION
        assert event["sandbox_id"] == live.sandbox.sandbox_id
        assert event["anchor_date"] == ANCHOR_DATE.isoformat()


# --- tools/call (requirements 17.3, 17.5 and 17.11) --------------------------------------


@pytest.mark.anyio
@pytest.mark.req("17.3", "17.11")
async def test_a_successful_call_answers_and_records_what_it_wrote(
    env: Env, tmp_path: Path
) -> None:
    async with session_over(env, tmp_path) as live:
        result = await live.call(COUNT_CREATE, NEW_COUNT)

        # The result: one text item that parses to the structuredContent (requirement 13.1).
        assert result.isError is False
        structured = result.structuredContent
        assert structured is not None
        assert text_of(result) == structured
        sheet_id = structured["id"]
        wire = live.wire()["result"]
        assert wire["structuredContent"] == structured
        assert wire["isError"] is False

        # The event: the keys of requirement 17.3, in that order, after the common fields.
        event = live.event()
        assert list(event) == [*COMMON_KEYS, *TOOLS_CALL_KEYS]
        assert event["event"] == EventName.TOOLS_CALL
        assert event["tool"] == COUNT_CREATE
        # The arguments as they arrived, before the lenient conversion: the quantity is
        # still the decimal string the client sent, and its path is reported as coerced
        # (requirement 17.11).
        assert event["args"] == NEW_COUNT
        assert event["coerced"] == COERCED_COUNT
        assert event["known_tool"] is True
        assert event["is_error"] is False
        assert event["error_code"] is None
        assert event["rule_id"] is None
        assert event["result"] == structured
        assert event["flags"] == []
        # Measured on the monotonic clock of the service: the arrival of the request in the
        # handler and the moment before this event was written are one step apart.
        assert event["latency_ms"] == STEP_MS

        # The net change of each (record, cause) the call made, with the keys of
        # requirement 17.4 and sorted by entity, id and cause.
        assert [list(item) for item in event["writes"]] == [list(WRITE_KEYS)] * 2
        assert event["writes"] == [
            {"entity": COUNT_SHEET, "op": "insert", "id": sheet_id, "cause": DIRECT_CAUSE},
            {
                "entity": COUNT_SHEET_LINE,
                "op": "insert",
                "id": f"{sheet_id}-1",
                "cause": DIRECT_CAUSE,
            },
        ]
        # The records were recorded inside the database under the ``seq`` of this very
        # event, which is what reading ``next_seq`` under the sandbox lock is for
        # (requirement 18.10, design.md 7.6).
        assert live.effective_seqs() == [event["seq"]]


@pytest.mark.anyio
@pytest.mark.req("17.3", "17.5", "17.11")
async def test_a_failed_call_carries_the_error_and_no_structured_content(
    env: Env, tmp_path: Path
) -> None:
    async with session_over(env, tmp_path) as live:
        result = await live.call(FEE_CREATE, DISABLED_FEE)

        # The result: isError, the four keys of the error object in order, and no
        # structuredContent at all - the key is not on the wire (requirement 13.6).
        assert result.isError is True
        assert result.structuredContent is None
        error = text_of(result)[ERROR_KEY]
        assert list(error) == [CODE, MESSAGE, RULE_ID, DETAILS]
        assert error[CODE] == ErrorCode.BUSINESS_RULE_VIOLATION.value
        assert error[RULE_ID] == DISABLED_RULE
        wire = live.wire()["result"]
        assert "structuredContent" not in wire
        assert wire["isError"] is True

        # The event: the code and the rule of the error object, the error object as the
        # result, and no write at all - the transaction of the call rolled back
        # (requirements 17.3 and 17.5).
        event = live.event()
        assert list(event) == [*COMMON_KEYS, *TOOLS_CALL_KEYS]
        assert event["known_tool"] is True
        assert event["is_error"] is True
        assert event["error_code"] == ErrorCode.BUSINESS_RULE_VIOLATION.value
        assert event["rule_id"] == DISABLED_RULE
        assert event["result"] == error
        assert event["writes"] == []
        assert event["latency_ms"] == STEP_MS
        # The call did reach input validation, so the paths it converted are reported even
        # though it ended with an error (requirement 17.11).
        assert event["coerced"] == COERCED_FEE

        # And nothing of it is in the database: no bill was created.
        assert live.rows(FEE_NOTE) == []
        assert live.effective_seqs() == []


@pytest.mark.anyio
@pytest.mark.req("14.6", "17.5", "17.10")
async def test_an_unknown_tool_is_rejected_and_recorded(env: Env, tmp_path: Path) -> None:
    async with session_over(env, tmp_path) as live:
        with pytest.raises(McpError) as raised:
            await live.call(UNKNOWN_TOOL, {"x": 1})

        # A JSON-RPC error of -32602 whose message names the tool (requirement 14.6).
        expected = UNKNOWN_TOOL_MESSAGE.format(tool=UNKNOWN_TOOL)
        assert raised.value.error.code == INVALID_PARAMS
        assert raised.value.error.message == expected
        assert UNKNOWN_TOOL in raised.value.error.message
        wire = live.wire()
        assert wire["error"] == {"code": INVALID_PARAMS, "message": expected}
        assert "result" not in wire

        # The event of requirement 17.10: known_tool false, is_error true, no code and no
        # rule, the JSON-RPC error object as the result, and three empty arrays.
        event = live.event()
        assert list(event) == [*COMMON_KEYS, *TOOLS_CALL_KEYS]
        assert event["tool"] == UNKNOWN_TOOL
        assert event["args"] == {"x": 1}
        assert event["known_tool"] is False
        assert event["is_error"] is True
        assert event["error_code"] is None
        assert event["rule_id"] is None
        assert event["result"] == {"code": INVALID_PARAMS, "message": expected}
        assert event["coerced"] == []
        assert event["writes"] == []
        assert event["flags"] == []
        assert event["latency_ms"] == STEP_MS

        # The sandbox data is unchanged: the call reached neither the resource limits nor
        # the database (requirement 14.6). And the session keeps serving requests.
        assert live.sandbox.sandbox_calls == 0
        assert live.rows(COUNT_SHEET) == []
        after = await live.call(COUNT_SEARCH, {})
        assert after.isError is False
        assert [event["event"] for event in live.events()] == [EventName.TOOLS_CALL] * 2


# --- ping (requirement 17.9) -------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("17.9")
async def test_a_ping_is_answered_without_a_trace_event(env: Env, tmp_path: Path) -> None:
    async with session_over(env, tmp_path) as live:
        await live.client.send_ping()

        # An empty result, and no event: a ping writes nothing (requirement 17.9).
        assert live.wire()["result"] == {}
        assert live.events() == []
        # It takes no sequence number either, so the next request is still the first event
        # of the sandbox.
        await live.client.list_tools()
        assert live.event()["seq"] == 1
        # Every message the server sent is a response to a request of the client: the
        # service issues no request and no notification of its own.
        assert not [message for message in live.wire_messages() if "method" in message]
