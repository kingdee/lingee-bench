"""Probe: the official MCP Python client of the locked SDK (design, open items V5, V6).

The gateway answers GET on its MCP endpoints with 405 instead of opening a standalone SSE
stream (task 22.3), returns tool errors as results with isError true and no
structuredContent although every tool declares an outputSchema (requirement 13 item 6),
and converts JSON numbers in tool arguments with ``Decimal(repr(x))`` (requirement 12
item 4), which is transport independent (requirement 24 item 1) only if both transports
hand the tools/call handler the same float. The probe server is a low-level ``Server``
with raw tools/list and tools/call handlers, as in tests/probes/test_sdk_server.py. Over
HTTP it runs behind ``StreamableHTTPSessionManager(json_response=True, stateless=False)``
and an outer ASGI layer that answers every GET with 405, under uvicorn on a free loopback
port; over stdio it runs on ``mcp.server.stdio.stdio_server`` reading an OS pipe through a
``TextIOWrapper``, as the real stdin is read; the client-side validation probes use the
SDK memory streams.

Findings on mcp 1.30.0, httpx 0.28.1, httpx-sse 0.4.3, starlette 1.7.0, uvicorn 0.54.0,
pydantic 2.14.0, pydantic-core 2.50.0, anyio 4.15.1 (asyncio):

V5, first part, holds. ``streamable_http_client``, and the deprecated
``streamablehttp_client`` (it emits a DeprecationWarning and delegates to the former; both
are probed), start ``StreamableHTTPTransport.handle_get_stream`` when they send
notifications/initialized. On a 405, ``raise_for_status()`` raises inside that task, and
its ``except Exception`` only logs at DEBUG ("GET stream error: Client error '405 Method
Not Allowed' ...") and retries after ``DEFAULT_RECONNECTION_DELAY_MS`` (1000 ms) until
``MAX_RECONNECTION_ATTEMPTS`` (2) failures, then returns. The server sees exactly two GETs
and no more; no exception or message reaches the session's message handler, nothing is
logged at WARNING or above, and initialize, tools/list and tools/call complete both while
the GETs are retried and after the GET stream has given up. On exit the client sends
DELETE (``terminate_on_close`` defaults to True).

V5, second part, holds. ``ClientSession.call_tool`` calls ``_validate_tool_result`` only
``if not result.isError`` (mcp/client/session.py), so a result with isError true and no
structuredContent comes back unchanged whether or not the tool's outputSchema is cached,
and it does not make the client send a tools/list. Results with isError false are
validated: a missing structuredContent raises ``RuntimeError("Tool <name> has an output
schema but did not return structured content")``, a schema violation raises
``RuntimeError("Invalid structured content returned by tool <name>: ...")``. Requirement
13 item 6 stands as written; tasks 22 and 23 need not be paused.

V6 holds. The Streamable HTTP transport parses a POST body with ``json.loads`` followed by
``JSONRPCMessage.model_validate`` (mcp/server/streamable_http.py, ``_handle_post_request``);
the stdio transport parses each stdin line with ``JSONRPCMessage.model_validate_json``,
i.e. pydantic-core's JSON parser (mcp/server/stdio.py, ``stdin_reader``). Both then share
``BaseSession._receive_loop``, which re-validates every request from
``model_dump(by_alias=True, mode="json", exclude_none=True)`` before the handler runs. The
two parsers agree on type and float bit pattern for every literal of a seeded corpus of
32031 numbers: hand-picked hard cases; random literals with up to 25 integer and 25
fraction digits and exponents up to 340 in magnitude; repr, 17-digit and 26-digit forms of
doubles with random bit patterns (subnormals included); exact midpoints between adjacent
doubles (ties to even) with their 21- and 18-digit roundings; business-style decimals.
The hand-picked literals reach the raw handler identically through a live HTTP server
and a live stdio server. Integers of any size stay exact ``int`` on both paths (2**53 + 1,
12345678901234567890123); a literal with a fraction or an exponent is a float (100.0,
1E2). ``Decimal(repr(x))`` of a literal with at most 15 significant digits equals the
literal's decimal value (0.85 gives Decimal("0.85"), requirement 12 item 4). No fallback
is needed: MessageFilter does not re-parse arguments (task 22.2).

Non-finite numbers, for task 15.1: both parsers accept overflowing literals (``1e400``,
``1.7976931348623159e308``) as infinities, and also the non-JSON names ``NaN``,
``Infinity`` and ``-Infinity``; the ``mode="json"`` dump in ``_receive_loop`` then turns
every non-finite float into None. The handler therefore receives null for them on both
transports, so the "non-finite float is a type error" branch of the number check (design,
component 5.2, step 3) is never reached, and such an argument is indistinguishable from an
explicit null.

Detail for tasks 22 and 23: ``ClientSession.call_tool`` validates a successful result
against the cached outputSchema; when the tool is not cached yet (no tools/list earlier in
the session) it first sends its own tools/list, after the tools/call. A client that calls
a tool before listing therefore produces tools/call, tools/list, tools/call, ... on the
wire (requirement 14 item 9 allows either order), and the trace records that tools/list.
"""

import io
import json
import logging
import math
import os
import random
import socket
import struct
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Context, Decimal
from io import TextIOWrapper
from typing import Any

import anyio
import httpx
import pytest
import uvicorn
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import (
    DEFAULT_RECONNECTION_DELAY_MS,
    MAX_RECONNECTION_ATTEMPTS,
    streamable_http_client,
    streamablehttp_client,
)
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.shared.session import RequestResponder
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Upper bound for every test; a hang fails the test instead of blocking the run.
TIMEOUT_S = 15

INIT_OPTIONS = InitializationOptions(
    server_name="erp-mock-mcp-probe",
    server_version="0.0.0",
    capabilities=types.ServerCapabilities(tools=types.ToolsCapability(listChanged=False)),
    instructions="Probe server.",
)

# Like every gateway tool, the probe tool declares an outputSchema; a result passes it
# only with a structuredContent object whose "qty" is a string.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"qty": {"type": "string"}},
    "required": ["qty"],
    "additionalProperties": False,
}
TOOL = types.Tool(
    name="probe_tool",
    description="Probe tool.",
    inputSchema={"type": "object"},
    outputSchema=OUTPUT_SCHEMA,
)


def text_content(value: Any) -> list[types.ContentBlock]:
    """One text item holding ``value`` as compact JSON, as the gateway writes it."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return [types.TextContent(type="text", text=text)]


# Shaped like the gateway's results: requirement 13 item 1 for success, item 6 for errors
# (isError true, no structuredContent, one text item with the error object).
ERROR_OBJECT: dict[str, Any] = {
    "error": {
        "code": "NOT_FOUND",
        "message": "记录不存在：po_999999",
        "rule_id": None,
        "details": [],
    }
}
SUCCESS = types.CallToolResult(
    content=text_content({"qty": "12.5"}), structuredContent={"qty": "12.5"}, isError=False
)
ERROR = types.CallToolResult(content=text_content(ERROR_OBJECT), isError=True)
# Controls: isError false results that violate the declared outputSchema.
MISSING = types.CallToolResult(content=text_content({"qty": "12.5"}), isError=False)
INVALID = types.CallToolResult(
    content=text_content({"qty": 12.5}), structuredContent={"qty": 12.5}, isError=False
)
RESULTS = {"success": SUCCESS, "error": ERROR, "missing": MISSING, "invalid": INVALID}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class Recorder:
    """What the server's raw handlers received, in order."""

    methods: list[str] = field(default_factory=list)
    arguments: list[dict[str, Any] | None] = field(default_factory=list)


def probe_server(recorder: Recorder) -> Server:
    """A low-level ``Server`` with raw tools/list and tools/call handlers (as in task 22.1).

    tools/call returns ``RESULTS[arguments["mode"]]`` ("success" when absent).
    """
    server = Server("erp-mock-mcp-probe")

    async def list_tools(req: types.ListToolsRequest) -> types.ServerResult:
        recorder.methods.append(req.method)
        return types.ServerResult(types.ListToolsResult(tools=[TOOL]))

    async def call_tool(req: types.CallToolRequest) -> types.ServerResult:
        recorder.methods.append(req.method)
        recorder.arguments.append(req.params.arguments)
        mode = (req.params.arguments or {}).get("mode", "success")
        return types.ServerResult(RESULTS[mode])

    server.request_handlers[types.ListToolsRequest] = list_tools
    server.request_handlers[types.CallToolRequest] = call_tool
    return server


@asynccontextmanager
async def memory_session(server: Server) -> AsyncIterator[ClientSession]:
    """An initialized ``ClientSession`` connected to ``server`` over the SDK memory streams."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.run, server_read, server_write, INIT_OPTIONS)
            try:
                async with ClientSession(client_read, client_write) as session:
                    await session.initialize()
                    yield session
            finally:
                tg.cancel_scope.cancel()


# ---------------------------------------------------------------------------
# Streamable HTTP server: uvicorn on a free loopback port
# ---------------------------------------------------------------------------


@dataclass
class HttpLog:
    """(method, status) of every HTTP request the probe server answered, in order."""

    exchanges: list[tuple[str, int]] = field(default_factory=list)

    def statuses(self, method: str) -> list[int]:
        return [status for m, status in self.exchanges if m == method]


def reject_get(manager: StreamableHTTPSessionManager, log: HttpLog) -> ASGIApp:
    """The session manager behind an outer layer that answers every GET with 405.

    This is what McpRouter does (design, component 7): no standalone SSE stream.
    """

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        method = scope["method"]
        if method == "GET":
            log.exchanges.append((method, 405))
            response = Response(status_code=405, headers={"Allow": "POST, DELETE"})
            await response(scope, receive, send)
            return

        async def send_recorded(message: Message) -> None:
            if message["type"] == "http.response.start":
                log.exchanges.append((method, message["status"]))
            await send(message)

        await manager.handle_request(scope, receive, send_recorded)

    return app


@asynccontextmanager
async def http_server(server: Server) -> AsyncIterator[tuple[str, HttpLog]]:
    """Serve ``server`` over Streamable HTTP (JSON responses, stateful sessions).

    Yields the endpoint URL and the log of answered requests. uvicorn runs as a task on
    the test's event loop and is told to exit when the block ends.
    """
    log = HttpLog()
    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=False,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        reject_get(manager, log),
        lifespan="off",
        log_config=None,
        log_level="warning",
        access_log=False,
    )
    uv = uvicorn.Server(config)
    try:
        async with manager.run(), anyio.create_task_group() as tg:
            tg.start_soon(uv.serve, [sock])
            while not uv.started:
                await anyio.sleep(0.01)
            try:
                yield f"http://127.0.0.1:{port}/mcp", log
            finally:
                uv.should_exit = True
    finally:
        sock.close()


ClientStreams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception], MemoryObjectSendStream[SessionMessage]
]


@asynccontextmanager
async def http_client_streams(api: str, url: str) -> AsyncIterator[ClientStreams]:
    """Open the official Streamable HTTP client transport with default settings.

    ``streamablehttp_client`` is the deprecated name of the same transport; it builds its
    own httpx client and delegates to ``streamable_http_client``.
    """
    if api == "streamable_http_client":
        transport = streamable_http_client(url)
    else:
        with pytest.deprecated_call():
            transport = streamablehttp_client(url)
    async with transport as (read, write, _):
        yield read, write


# ---------------------------------------------------------------------------
# V5, first part: the HTTP client and a GET answered with 405
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("14.9")
@pytest.mark.parametrize("api", ["streamable_http_client", "streamablehttp_client"])
async def test_v5_http_client_tolerates_get_405(api: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="mcp.client.streamable_http")
    recorder = Recorder()
    incoming: list[object] = []

    async def on_message(
        message: RequestResponder[types.ServerRequest, types.ClientResult]
        | types.ServerNotification
        | Exception,
    ) -> None:
        incoming.append(message)

    with anyio.fail_after(TIMEOUT_S):
        async with http_server(probe_server(recorder)) as (url, log):
            async with (
                http_client_streams(api, url) as (read, write),
                ClientSession(read, write, message_handler=on_message) as session,
            ):
                init = await session.initialize()
                listed = await session.list_tools()
                called = await session.call_tool(TOOL.name, {"mode": "success"})
                # The transport opens its GET stream when it sends notifications/initialized
                # and retries a failed GET; wait until it has used up its attempts ...
                while len(log.statuses("GET")) < MAX_RECONNECTION_ATTEMPTS:
                    await anyio.sleep(0.02)
                # ... and long enough for one more retry, which does not come.
                await anyio.sleep(DEFAULT_RECONNECTION_DELAY_MS / 1000 + 0.5)
                gets = log.statuses("GET")
                # The session keeps working after the GET stream has given up.
                called_again = await session.call_tool(TOOL.name, {"mode": "error"})
                listed_again = await session.list_tools()
                client_records = [
                    (record.levelno, record.getMessage())
                    for record in caplog.records
                    if record.name == "mcp.client.streamable_http"
                ]
            exchanges = list(log.exchanges)

    assert init.serverInfo.name == INIT_OPTIONS.server_name
    assert listed.tools == [TOOL]
    assert called == SUCCESS
    assert called_again == ERROR
    assert listed_again.tools == [TOOL]
    assert recorder.methods == ["tools/list", "tools/call", "tools/call", "tools/list"]

    # Exactly MAX_RECONNECTION_ATTEMPTS (2) GETs, all answered 405, then no more.
    assert MAX_RECONNECTION_ATTEMPTS == 2
    assert gets == [405, 405]
    # The SDK transport answered every POST: 200 for requests, 202 for the notification.
    assert sorted({status for method, status in exchanges if method == "POST"}) == [200, 202]
    # Closing the client sends DELETE (terminate_on_close defaults to True).
    assert exchanges[-1][0] == "DELETE"

    # The failed GETs reached neither the read stream nor the session's message handler,
    # and the transport logged them below WARNING.
    assert incoming == []
    assert all(level < logging.WARNING for level, _ in client_records)
    get_errors = [
        message for _, message in client_records if message.startswith("GET stream error")
    ]
    assert len(get_errors) == 2
    assert all("405 Method Not Allowed" in message for message in get_errors)


# ---------------------------------------------------------------------------
# V5, second part: client-side outputSchema validation of error results
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("13.6")
async def test_v5_call_tool_does_not_validate_error_results() -> None:
    recorder = Recorder()
    with anyio.fail_after(TIMEOUT_S):
        async with memory_session(probe_server(recorder)) as session:
            # Nothing listed yet in this session: the client's schema cache is empty.
            before_list = await session.call_tool(TOOL.name, {"mode": "error"})
            methods_before_list = list(recorder.methods)
            await session.list_tools()
            # Now the cache holds TOOL.outputSchema.
            after_list = await session.call_tool(TOOL.name, {"mode": "error"})
            # Controls: the same session does validate isError false results.
            success = await session.call_tool(TOOL.name, {"mode": "success"})
            with pytest.raises(RuntimeError) as missing:
                await session.call_tool(TOOL.name, {"mode": "missing"})
            with pytest.raises(RuntimeError) as invalid:
                await session.call_tool(TOOL.name, {"mode": "invalid"})

    # The error result is returned unchanged: no structuredContent, isError true, the
    # error object as the single text item.
    assert before_list == ERROR
    assert after_list == ERROR
    assert after_list.structuredContent is None
    assert after_list.isError is True
    assert len(after_list.content) == 1
    assert isinstance(after_list.content[0], types.TextContent)
    assert json.loads(after_list.content[0].text) == ERROR_OBJECT
    # An error result does not make the client fetch the schema with its own tools/list.
    assert methods_before_list == ["tools/call"]

    assert success == SUCCESS
    assert str(missing.value) == (
        f"Tool {TOOL.name} has an output schema but did not return structured content"
    )
    assert str(invalid.value).startswith(
        f"Invalid structured content returned by tool {TOOL.name}:"
    )


@pytest.mark.anyio
@pytest.mark.req("14.9")
async def test_v5_success_before_tools_list_makes_the_client_list_tools() -> None:
    recorder = Recorder()
    with anyio.fail_after(TIMEOUT_S):
        async with memory_session(probe_server(recorder)) as session:
            first = await session.call_tool(TOOL.name, {"mode": "success"})
            second = await session.call_tool(TOOL.name, {"mode": "success"})

    assert first == SUCCESS
    assert second == SUCCESS
    # The first successful call is validated after the client fetches the schema with
    # its own tools/list; the second uses the cached schema.
    assert recorder.methods == ["tools/call", "tools/list", "tools/call"]


# ---------------------------------------------------------------------------
# V6: JSON numbers over HTTP (json.loads) and stdio (pydantic's JSON parser)
# ---------------------------------------------------------------------------

# Hand-picked JSON number literals; the first three are the examples of task 2.3.
NUMBER_LITERALS: tuple[str, ...] = (
    "0.85",
    "1e-7",
    "123456789.123456",
    "0.130",
    "0.1",
    "12.5",
    "100",
    "100.0",
    "1E2",
    "0",
    "-0.0",
    "0.30000000000000004",
    "999999999999999.9",
    "100000000.000001",
    # 2**53 + 1: exact as an integer, rounds to 2**53 as a float.
    "9007199254740993",
    "9007199254740993.0",
    # Beyond 64 bits.
    "12345678901234567890123",
    # The exact decimal value of the double nearest to 0.1.
    "0.1000000000000000055511151231257827021181583404541015625",
    # Exactly halfway between 1.0 and the next double: ties to even, i.e. 1.0.
    "1.00000000000000011102230246251565404236316680908203125",
    # Near the smallest normal double (a classic slow path of float parsers).
    "2.2250738585072011e-308",
    # Just below and just above half the smallest subnormal: 0.0 and 5e-324.
    "2.4703282292062327e-324",
    "2.4703282292062328e-324",
    "4.9e-324",
    "1e-400",
    # The largest finite double, then overflows.
    "1.7976931348623157e308",
    "1.7976931348623159e308",
    "1e400",
    "-1e400",
)
# Not JSON, but accepted by both parsers.
NON_STANDARD_LITERALS: tuple[str, ...] = ("NaN", "Infinity", "-Infinity")

INITIALIZE_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": types.LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "probe-client", "version": "0.0.0"},
        },
    }
)
INITIALIZED_BODY = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})


def call_body(request_id: int, literals: Sequence[str]) -> str:
    """A tools/call request whose arguments hold ``literals`` verbatim as one JSON array."""
    return (
        f'{{"jsonrpc":"2.0","id":{request_id},"method":"tools/call","params":'
        f'{{"name":"{TOOL.name}","arguments":{{"values":[{",".join(literals)}]}}}}}}'
    )


def same_value(a: Any, b: Any) -> bool:
    """Same type and value; floats compare by repr, so -0.0 differs from 0.0."""
    if type(a) is not type(b):
        return False
    return repr(a) == repr(b) if isinstance(a, float) else bool(a == b)


def mismatches(literals: Sequence[str], actual: list[Any], expected: list[Any]) -> list[Any]:
    return [
        (literal, a, e)
        for literal, a, e in zip(literals, actual, expected, strict=True)
        if not same_value(a, e)
    ]


def values_of(message: types.JSONRPCMessage) -> list[Any]:
    """The ``values`` argument of a tools/call request message."""
    root = message.root
    assert isinstance(root, types.JSONRPCRequest)
    assert root.params is not None
    values: list[Any] = root.params["arguments"]["values"]
    return values


def parse_like_http(body: str) -> list[Any]:
    """The first parsing step of the Streamable HTTP transport (``_handle_post_request``)."""
    return values_of(types.JSONRPCMessage.model_validate(json.loads(body.encode("utf-8"))))


def parse_like_stdio(line: str) -> list[Any]:
    """The first parsing step of the stdio transport (``stdio_server.stdin_reader``)."""
    return values_of(types.JSONRPCMessage.model_validate_json(line))


async def values_over_http(literals: Sequence[str]) -> list[Any]:
    """POST ``call_body(literals)`` to a live server; return what the handler received."""
    recorder = Recorder()
    async with http_server(probe_server(recorder)) as (url, _):
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            init = await client.post(url, content=INITIALIZE_BODY, headers=headers)
            assert init.status_code == 200, init.text
            headers["mcp-session-id"] = init.headers["mcp-session-id"]
            headers["mcp-protocol-version"] = init.json()["result"]["protocolVersion"]
            initialized = await client.post(url, content=INITIALIZED_BODY, headers=headers)
            assert initialized.status_code == 202, initialized.text
            called = await client.post(url, content=call_body(2, literals), headers=headers)
            assert called.status_code == 200, called.text
            assert called.json()["result"]["isError"] is False
            await client.delete(url, headers=headers)
    (arguments,) = recorder.arguments
    assert arguments is not None
    values: list[Any] = arguments["values"]
    return values


async def values_over_stdio(literals: Sequence[str]) -> list[Any]:
    """Write ``call_body(literals)`` to a stdio server's stdin; return what the handler received.

    stdin is a TextIOWrapper over an OS pipe, as ``stdio_server`` builds over
    ``sys.stdin.buffer``; stdout is collected in memory.
    """
    recorder = Recorder()
    server = probe_server(recorder)
    lines = [INITIALIZE_BODY, INITIALIZED_BODY, call_body(2, literals)]
    read_fd, write_fd = os.pipe()
    stdin_writer = open(write_fd, "wb", buffering=0)
    stdin = anyio.wrap_file(TextIOWrapper(open(read_fd, "rb"), encoding="utf-8", errors="replace"))
    stdout_buffer = io.StringIO()
    try:
        async with stdio_server(stdin=stdin, stdout=anyio.wrap_file(stdout_buffer)) as streams:
            read_stream, write_stream = streams
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.run, read_stream, write_stream, INIT_OPTIONS)
                try:
                    stdin_writer.write("".join(line + "\n" for line in lines).encode("utf-8"))
                    # Wait for both responses before closing stdin: at EOF the server
                    # session closes its write stream.
                    while len(stdout_buffer.getvalue().splitlines()) < 2:
                        await anyio.sleep(0.01)
                finally:
                    # EOF ends the stdin reader (a worker thread that cancellation cannot
                    # interrupt), then the server session and server.run.
                    stdin_writer.close()
    finally:
        stdin_writer.close()
        await stdin.aclose()
    responses = [json.loads(line) for line in stdout_buffer.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [1, 2]
    assert responses[1]["result"]["isError"] is False
    (arguments,) = recorder.arguments
    assert arguments is not None
    values: list[Any] = arguments["values"]
    return values


def at_handler(literal: str) -> Any:
    """``json.loads(literal)`` as a raw handler receives it: non-finite floats become None."""
    value = json.loads(literal)
    return None if isinstance(value, float) and not math.isfinite(value) else value


@pytest.mark.anyio
@pytest.mark.req("12.4", "24.1")
async def test_v6_numbers_reach_the_handler_alike_over_http_and_stdio() -> None:
    literals = NUMBER_LITERALS + NON_STANDARD_LITERALS
    with anyio.fail_after(TIMEOUT_S):
        over_http = await values_over_http(literals)
        over_stdio = await values_over_stdio(literals)

    assert mismatches(literals, over_http, over_stdio) == []
    assert mismatches(literals, over_http, [at_handler(lit) for lit in literals]) == []

    by_literal = dict(zip(literals, over_http, strict=True))
    assert by_literal["0.85"] == 0.85
    assert type(by_literal["100"]) is int
    assert by_literal["100"] == 100
    assert type(by_literal["100.0"]) is float
    assert by_literal["9007199254740993"] == 2**53 + 1
    assert by_literal["9007199254740993.0"] == float(2**53)
    assert by_literal["12345678901234567890123"] == 12345678901234567890123
    assert repr(by_literal["-0.0"]) == "-0.0"
    assert by_literal["1.00000000000000011102230246251565404236316680908203125"] == 1.0
    assert by_literal["2.4703282292062327e-324"] == 0.0
    assert by_literal["2.4703282292062328e-324"] == 5e-324
    # Non-finite values (overflowing literals and the non-standard names) parse to
    # inf or nan, and the re-validation in BaseSession._receive_loop turns them into None.
    for literal in ("1.7976931348623159e308", "1e400", "-1e400", *NON_STANDARD_LITERALS):
        assert by_literal[literal] is None, literal
    # Requirement 12 item 4: with at most 15 significant digits, Decimal(repr(x)) is the
    # literal's decimal value.
    for literal in ("0.85", "1e-7", "123456789.123456", "0.130", "12.5", "100000000.000001"):
        assert Decimal(repr(by_literal[literal])) == Decimal(literal), literal


# Seeded corpus for the parser comparison.
_DIGITS = "0123456789"
_HALF_CONTEXT = Context(prec=2000)


def random_double(rng: random.Random) -> float:
    """A finite double with a uniformly random bit pattern (subnormals and both signs)."""
    while True:
        (value,) = struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))
        if math.isfinite(value):
            return float(value)


def random_literal(rng: random.Random) -> str:
    """A random JSON number literal: up to 25 integer and 25 fraction digits, any exponent."""
    sign = rng.choice(("", "-"))
    if rng.random() < 0.3:
        integer = "0"
    else:
        integer = rng.choice(_DIGITS[1:]) + "".join(rng.choices(_DIGITS, k=rng.randint(0, 24)))
    fraction = ""
    if rng.random() < 0.75:
        fraction = "." + "".join(rng.choices(_DIGITS, k=rng.randint(1, 25)))
    exponent = ""
    if rng.random() < 0.6:
        exponent = rng.choice("eE") + rng.choice(("", "+", "-")) + str(rng.randint(0, 340))
    return sign + integer + fraction + exponent


def halfway_literals(rng: random.Random) -> list[str]:
    """The exact midpoint of two adjacent positive doubles, and its 21- and 18-digit roundings."""
    while True:
        low = abs(random_double(rng))
        high = math.nextafter(low, math.inf)
        if math.isfinite(high):
            break
    middle = _HALF_CONTEXT.divide(_HALF_CONTEXT.add(Decimal(low), Decimal(high)), 2)
    return [f"{middle:e}", f"{middle:.20e}", f"{middle:.17e}"]


def business_literal(rng: random.Random) -> str:
    """A plain decimal with at most 15 significant digits and at most 6 decimal places."""
    digits = rng.randint(1, 15)
    places = rng.choice((0, 1, 2, 4, 6))
    coefficient = rng.randrange(10 ** (digits - 1) if digits > 1 else 0, 10**digits)
    return f"{Decimal(coefficient).scaleb(-places):f}"


@pytest.mark.req("12.4", "24.1")
def test_v6_json_loads_and_pydantic_parse_numbers_alike() -> None:
    rng = random.Random(20261011)
    business: list[str] = []
    literals: list[str] = list(NUMBER_LITERALS + NON_STANDARD_LITERALS)
    for _ in range(4000):
        value = random_double(rng)
        literals.extend((random_literal(rng), repr(value), f"{value:.17g}", f"{value:.25e}"))
        literals.extend(halfway_literals(rng))
        business.append(business_literal(rng))
    literals.extend(business)
    body = call_body(1, literals)

    over_http = parse_like_http(body)
    over_stdio = parse_like_stdio(body)

    assert len(literals) == 32031
    assert mismatches(literals, over_http, over_stdio) == []
    assert mismatches(literals, over_http, [json.loads(lit) for lit in literals]) == []
    # Requirement 12 item 4 for business-style decimals (at most 15 significant digits).
    parsed = dict(zip(literals, over_http, strict=True))
    wrong = [lit for lit in business if Decimal(repr(parsed[lit])) != Decimal(lit)]
    assert wrong == []
