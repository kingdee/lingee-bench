"""Probe: the low-level MCP ``Server`` of the locked SDK (design, open items V1, V2, V4).

The gateway registers tools/list, tools/call and ping as raw entries of
``Server.request_handlers`` (not ``@server.call_tool()``, which validates arguments against
inputSchema, wraps exceptions into text errors and validates structuredContent). It relies
on the SDK to send a handler's ``ServerResult`` unchanged, to turn ``McpError`` into a
JSON-RPC error, to keep ``None`` values and key order inside structuredContent, and to hand
the requests of one session to their handlers in arrival order. Each test connects a
low-level ``Server`` to a ``ClientSession`` over the SDK memory streams
(``mcp.shared.memory.create_client_server_memory_streams``) with a recording tap on both
directions, so the messages the server writes are inspected before the client parses them.

Findings on mcp 1.30.0, pydantic 2.14.0, pydantic-core 2.50.0, anyio 4.15.1 (asyncio):

V1 holds. ``Server._handle_request`` looks the handler up with
``self.request_handlers.get(type(req))`` and awaits ``handler(req)``; the returned
``ServerResult`` goes to ``RequestResponder.respond`` and ``BaseSession._send_response``
without inspection. Arguments that violate the declared inputSchema reach the handler
unchanged (including ``None`` values), a structuredContent that violates the declared
outputSchema reaches the client unchanged, and the tools/list result is sent as built
(no nextCursor when it is None). ``except McpError as err: response = err.error`` sends
the ``ErrorData`` as ``JSONRPCError(jsonrpc="2.0", id=<request id>, error=err.error)``, so
code and message arrive unchanged; the session keeps serving requests afterwards. A raw
ping handler replaces the SDK's default one. Explicit ``InitializationOptions`` reach the
client as given: capabilities serialize to exactly ``{"tools": {"listChanged": false}}``.

V2 holds. ``_send_response`` builds the JSON-RPC result with
``response.model_dump(by_alias=True, mode="json", exclude_none=True)``, and both
transports write ``message.model_dump_json(by_alias=True, exclude_none=True)``
(``mcp/server/stdio.py`` stdout writer; ``mcp/server/streamable_http.py`` JSON response
and SSE event data). ``exclude_none`` drops only model fields whose value is None (for
example ``structuredContent=None`` of an error result, ``nextCursor``, ``_meta``,
``annotations``); keys with value None inside the ``dict[str, Any]`` structuredContent,
in nested objects and in arrays are kept as ``null``, and every object keeps its key
order. The incoming direction goes through the same ``model_dump`` call
(``BaseSession._receive_loop`` re-validates each request from it), and ``None`` values in
tools/call arguments survive it as well.

V4 holds. ``BaseSession._receive_loop`` reads the stream one message at a time and queues
each request on ``incoming_messages``; ``Server.run`` starts one task per message with
``tg.start_soon(self._handle_message, ...)`` in that order, and the task reaches
``await handler(req)`` without suspending first, so handlers are entered in arrival
order. Handlers of one session run concurrently (a request does not wait for the previous
one to finish), so the gateway must serialize them itself: a FIFO ``asyncio.Lock`` taken
at handler entry is acquired in arrival order, which is what the sandbox lock relies on.

No fallback is needed: tools/list and tools/call stay raw handlers (task 22.1), response
serialization stays with the SDK, and MessageFilter does not assign sequence numbers
(task 22.2).

Details for tasks 22.1 and 22.2:

- ``Server.request_handlers`` is ``dict[type, Callable[..., Awaitable[types.ServerResult]]]``
  keyed by the request model class: ``types.ListToolsRequest``, ``types.CallToolRequest``,
  ``types.PingRequest`` (a default ping handler is pre-registered and may be replaced).
  ``Server.get_capabilities`` keys off the same dict, but the gateway passes explicit
  ``InitializationOptions`` and does not call it.
- A handler is ``async def handler(req: types.CallToolRequest) -> types.ServerResult``,
  awaited with the validated request model as its only argument; the tool name and
  arguments are ``req.params.name`` and ``req.params.arguments`` (``dict[str, Any] | None``).
  ``server.request_context`` (``request_id``, ``meta``, ``session``) is set while it runs.
- Results: ``types.ServerResult(types.CallToolResult(content=[types.TextContent(type="text",
  text=...)], structuredContent=..., isError=...))`` and
  ``types.ServerResult(types.ListToolsResult(tools=[...]))``; ``types.EmptyResult()`` for
  ping. A content list built ahead of the call needs the annotation
  ``list[types.ContentBlock]`` for mypy (``list`` is invariant).
- Errors: ``raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message=...))`` with
  ``McpError`` from ``mcp.shared.exceptions``; ``ErrorData.data`` defaults to None and is
  then left out of the error object.
- Serialization parameters: results ``model_dump(by_alias=True, mode="json",
  exclude_none=True)``; wire ``model_dump_json(by_alias=True, exclude_none=True)``.

Read from the SDK source, not asserted here:

- Any other exception from a handler becomes ``ErrorData(code=0, message=str(err))`` when
  ``run(..., raise_exceptions=False)``, so handlers must map their own failures (the text
  would otherwise reach the client).
- A ``types.ClientRequest`` method without a handler (e.g. resources/list) gets -32601
  "Method not found". A method outside ``types.ClientRequest`` (any unknown method), or
  params that fail its validation, gets -32602 "Invalid request parameters" from
  ``BaseSession._receive_loop`` before any handler runs, which is why MessageFilter
  answers non-whitelisted methods with -32601 itself.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.client.session import ClientSession
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage

# Upper bound for every test; a hang fails the test instead of blocking the run.
TIMEOUT_S = 10

INIT_OPTIONS = InitializationOptions(
    server_name="erp-mock-mcp-probe",
    server_version="0.0.0",
    capabilities=types.ServerCapabilities(tools=types.ToolsCapability(listChanged=False)),
    instructions="Probe server.",
)

# Both schemas accept only a JSON number "qty" and nothing else; the probes send and
# return values that violate them to show the raw handlers bypass SDK validation.
_QTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"qty": {"type": "number"}},
    "required": ["qty"],
    "additionalProperties": False,
}
TOOL = types.Tool(
    name="probe_tool",
    description="Probe tool.",
    inputSchema=_QTY_SCHEMA,
    outputSchema=_QTY_SCHEMA,
    annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
LIST_RESULT = types.ServerResult(types.ListToolsResult(tools=[TOOL]))

# Keys deliberately out of sorted order, with None at the top level, in nested objects
# and in arrays.
STRUCTURED: dict[str, Any] = {
    "zeta": None,
    "id": "po_000001",
    "supplier": {"id": "sup_000001", "code": "S0001", "name": None},
    "lines": [
        {"line_no": 1, "remark": None, "qty": "12.5"},
        {"line_no": 2, "remark": "备注", "qty": None},
    ],
    "tags": [None, "b", None],
    "empty": {},
    "amount": "1700.00",
    "alpha": False,
    "truncated": True,
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class Wire:
    """Messages recorded by the taps, in the order they passed through."""

    to_server: list[SessionMessage] = field(default_factory=list)
    to_client: list[SessionMessage] = field(default_factory=list)


async def _tap(
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
async def connected(
    server: Server,
) -> AsyncIterator[tuple[ClientSession, types.InitializeResult, Wire]]:
    """Run ``server`` with an initialized ``ClientSession`` over the SDK memory streams.

    The server writes into a tap that records each message and forwards it to the client,
    and the client writes into a tap that does the same towards the server.
    """
    wire = Wire()
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_to_server = client_streams
        server_read, server_to_client = server_streams
        client_write, tapped_from_client = anyio.create_memory_object_stream[SessionMessage](1)
        server_write, tapped_from_server = anyio.create_memory_object_stream[SessionMessage](1)
        async with anyio.create_task_group() as tg:
            tg.start_soon(_tap, tapped_from_client, client_to_server, wire.to_server)
            tg.start_soon(_tap, tapped_from_server, server_to_client, wire.to_client)
            # raise_exceptions=False as in the gateway (design, component 7.2).
            tg.start_soon(server.run, server_read, server_write, INIT_OPTIONS)
            try:
                async with ClientSession(client_read, client_write) as session:
                    init = await session.initialize()
                    yield session, init, wire
            finally:
                tg.cancel_scope.cancel()


async def call(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> types.CallToolResult:
    """Send one tools/call and parse the result.

    ``ClientSession.call_tool`` is avoided on purpose: it issues its own tools/list and
    validates structuredContent against outputSchema on the client side.
    """
    request = types.ClientRequest(
        types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
    )
    return await session.send_request(request, types.CallToolResult)


def request_ids(messages: list[SessionMessage], method: str) -> list[types.RequestId]:
    """Ids of the requests for ``method`` among ``messages``, in order."""
    return [
        m.message.root.id
        for m in messages
        if isinstance(m.message.root, types.JSONRPCRequest) and m.message.root.method == method
    ]


def response_to(messages: list[SessionMessage], request_id: types.RequestId) -> SessionMessage:
    """The single response (result or error) among ``messages`` for ``request_id``."""
    found = [
        m
        for m in messages
        if isinstance(m.message.root, types.JSONRPCResponse | types.JSONRPCError)
        and m.message.root.id == request_id
    ]
    assert len(found) == 1, found
    return found[0]


def result_dict(message: SessionMessage) -> dict[str, Any]:
    """The ``result`` dict of a response as the server session built it."""
    root = message.message.root
    assert isinstance(root, types.JSONRPCResponse), root
    return root.result


def on_the_wire(message: SessionMessage) -> str:
    """``message`` serialized the way the stdio and Streamable HTTP transports write it."""
    return message.message.model_dump_json(by_alias=True, exclude_none=True)


def key_paths(value: Any, path: str = "$") -> list[str]:
    """Every object key under ``value`` with its path, in document order."""
    if isinstance(value, dict):
        paths: list[str] = []
        for key, item in value.items():
            paths.append(f"{path}.{key}")
            paths.extend(key_paths(item, f"{path}.{key}"))
        return paths
    if isinstance(value, list):
        return [p for i, item in enumerate(value) for p in key_paths(item, f"{path}[{i}]")]
    return []


def assert_same_json(actual: Any, expected: Any) -> None:
    """Equal values, and every object has the same keys in the same order."""
    assert actual == expected
    assert key_paths(actual) == key_paths(expected)


@dataclass
class Recorder:
    calls: list[types.CallToolRequest] = field(default_factory=list)
    pings: int = 0


def raw_server(results: dict[str, types.ServerResult], recorder: Recorder) -> Server:
    """A low-level ``Server`` whose tools/list, tools/call and ping are raw handlers."""
    server = Server("erp-mock-mcp-probe")

    async def list_tools(req: types.ListToolsRequest) -> types.ServerResult:
        return LIST_RESULT

    async def call_tool(req: types.CallToolRequest) -> types.ServerResult:
        recorder.calls.append(req)
        result = results.get(req.params.name)
        if result is None:
            raise McpError(
                types.ErrorData(code=types.INVALID_PARAMS, message=f"未知工具：{req.params.name}")
            )
        return result

    async def ping(req: types.PingRequest) -> types.ServerResult:
        recorder.pings += 1
        return types.ServerResult(types.EmptyResult())

    server.request_handlers[types.ListToolsRequest] = list_tools
    server.request_handlers[types.CallToolRequest] = call_tool
    server.request_handlers[types.PingRequest] = ping
    return server


def text_result(structured: dict[str, Any] | None, *, is_error: bool) -> types.ServerResult:
    """A tools/call result shaped like the gateway's: one text item plus structuredContent."""
    text = json.dumps({"error": "x"} if is_error else structured, ensure_ascii=False)
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structuredContent=structured,
            isError=is_error,
        )
    )


@pytest.mark.anyio
@pytest.mark.req("13.1")
async def test_v1_raw_handler_results_arrive_unchanged() -> None:
    # Violates inputSchema: a decimal string for a number, an undeclared key, None values.
    arguments: dict[str, Any] = {"qty": "12.50", "remark": None, "extra": {"z": 1, "a": None}}
    # Violates outputSchema: a string qty and an undeclared key.
    result = text_result({"qty": "12.50", "unexpected": True}, is_error=False)
    recorder = Recorder()
    server = raw_server({TOOL.name: result}, recorder)

    with anyio.fail_after(TIMEOUT_S):
        async with connected(server) as (session, init, wire):
            listed = await session.list_tools()
            called = await call(session, TOOL.name, arguments)
            await session.send_ping()

    # Explicit InitializationOptions are sent as given.
    assert init.capabilities == INIT_OPTIONS.capabilities
    assert init.instructions == INIT_OPTIONS.instructions
    (init_id,) = request_ids(wire.to_server, "initialize")
    init_wire = json.loads(on_the_wire(response_to(wire.to_client, init_id)))
    assert init_wire["result"]["capabilities"] == {"tools": {"listChanged": False}}

    # tools/list: the ServerResult built by the handler is what the client receives.
    assert listed == LIST_RESULT.root
    (list_id,) = request_ids(wire.to_server, "tools/list")
    list_message = response_to(wire.to_client, list_id)
    expected_list = LIST_RESULT.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert_same_json(result_dict(list_message), expected_list)
    assert_same_json(json.loads(on_the_wire(list_message))["result"], expected_list)
    assert "nextCursor" not in expected_list

    # tools/call: no inputSchema check before the handler, no outputSchema check after it.
    assert len(recorder.calls) == 1
    assert recorder.calls[0].params.name == TOOL.name
    assert_same_json(recorder.calls[0].params.arguments, arguments)
    assert called == result.root
    (call_id,) = request_ids(wire.to_server, "tools/call")
    call_message = response_to(wire.to_client, call_id)
    expected_call = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert_same_json(result_dict(call_message), expected_call)

    # ping: the raw handler replaced the SDK's default one.
    assert recorder.pings == 1


@pytest.mark.anyio
@pytest.mark.req("14.6")
async def test_v1_mcp_error_becomes_the_jsonrpc_error() -> None:
    result = text_result({"qty": "1"}, is_error=False)
    recorder = Recorder()
    server = raw_server({TOOL.name: result}, recorder)

    with anyio.fail_after(TIMEOUT_S):
        async with connected(server) as (session, _, wire):
            with pytest.raises(McpError) as raised:
                await call(session, "no_such_tool", {})
            # The session keeps serving requests after the error.
            after = await call(session, TOOL.name, {"qty": "1"})

    expected = types.ErrorData(code=-32602, message="未知工具：no_such_tool")
    assert raised.value.error == expected
    assert after == result.root
    error_id, _ = request_ids(wire.to_server, "tools/call")
    error_message = response_to(wire.to_client, error_id)
    assert isinstance(error_message.message.root, types.JSONRPCError)
    assert error_message.message.root.error == expected
    assert json.loads(on_the_wire(error_message)) == {
        "jsonrpc": "2.0",
        "id": error_id,
        "error": {"code": -32602, "message": "未知工具：no_such_tool"},
    }


@pytest.mark.anyio
@pytest.mark.req("13.1", "13.4")
async def test_v2_structured_content_keeps_none_and_key_order() -> None:
    success = text_result(STRUCTURED, is_error=False)
    failure = text_result(None, is_error=True)
    recorder = Recorder()
    server = raw_server({"ok_tool": success, "error_tool": failure}, recorder)

    with anyio.fail_after(TIMEOUT_S):
        async with connected(server) as (session, _, wire):
            received = await call(session, "ok_tool", {})
            received_error = await call(session, "error_tool", {})

    ok_id, error_id = request_ids(wire.to_server, "tools/call")
    ok_message = response_to(wire.to_client, ok_id)
    # As the server session built the response (model_dump with exclude_none=True) ...
    assert_same_json(result_dict(ok_message)["structuredContent"], STRUCTURED)
    # ... as the transports write it (model_dump_json with exclude_none=True) ...
    raw = on_the_wire(ok_message)
    assert_same_json(json.loads(raw)["result"]["structuredContent"], STRUCTURED)
    assert '"zeta":null' in raw
    assert '"name":null' in raw
    assert '"tags":[null,"b",null]' in raw
    # ... and as the official client parses it.
    assert_same_json(received.structuredContent, STRUCTURED)

    # Only model fields whose value is None are dropped: an error result has no
    # structuredContent key at all, and TextContent's unset fields are left out.
    error_wire = json.loads(on_the_wire(response_to(wire.to_client, error_id)))["result"]
    assert error_wire == {
        "content": [{"type": "text", "text": '{"error": "x"}'}],
        "isError": True,
    }
    assert received_error.structuredContent is None
    assert received_error.isError is True


@pytest.mark.anyio
@pytest.mark.req("15.4")
async def test_v4_handlers_are_entered_in_arrival_order() -> None:
    count = 12
    entered: list[types.RequestId] = []
    acquired: list[types.RequestId] = []
    in_flight_at_release: list[int] = []
    all_entered = anyio.Event()
    # Stands in for the per-sandbox FIFO lock that the gateway takes on handler entry.
    sandbox_lock = asyncio.Lock()
    server = Server("erp-mock-mcp-probe")

    async def call_tool(req: types.CallToolRequest) -> types.ServerResult:
        request_id = server.request_context.request_id
        entered.append(request_id)
        if len(entered) == count:
            all_entered.set()
        async with sandbox_lock:
            acquired.append(request_id)
            if len(acquired) == 1:
                # Hold the lock until every request is in flight so the others queue on
                # it. Bounded: if the SDK ran handlers one at a time, the overlap
                # assertion below fails instead of the test hanging.
                with anyio.move_on_after(2):
                    await all_entered.wait()
                in_flight_at_release.append(len(entered))
        content: list[types.ContentBlock] = [types.TextContent(type="text", text=str(request_id))]
        return types.ServerResult(types.CallToolResult(content=content))

    server.request_handlers[types.CallToolRequest] = call_tool

    with anyio.fail_after(TIMEOUT_S):
        async with connected(server) as (session, _, wire):
            async with anyio.create_task_group() as tg:
                for n in range(count):
                    tg.start_soon(call, session, "probe_tool", {"n": n})

    # Arrival order is the order in which the requests reached the server's read stream.
    arrived = request_ids(wire.to_server, "tools/call")
    assert len(arrived) == count
    assert entered == arrived
    assert acquired == arrived
    # All requests were in flight while the first held the lock: the SDK dispatches the
    # requests of one session concurrently, so serialization is up to the gateway.
    assert in_flight_at_release == [count]
