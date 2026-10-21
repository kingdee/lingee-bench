"""Probe: ``StreamableHTTPServerTransport`` of the locked SDK (design, open item V3).

The gateway does not use ``StreamableHTTPSessionManager``. Its own ``McpRouter`` keeps the
session registry, creates one transport per HTTP session with the gateway's session ID,
JSON responses, no event store and the SDK's DNS rebinding protection off (design,
component 7.2), and hands the session's HTTP requests to ``transport.handle_request``.
Before the first request of a session it repeats the transport's header checks for
initialize (task 22.3), so that a sandbox is only created for a request the transport
accepts. Each test drives a transport through ``httpx.ASGITransport`` (no socket); the
per-session task enters ``transport.connect()`` and runs a pass-through stand-in for
MessageFilter between the transport's read stream and ``Server.run``, as the gateway does.

Findings on mcp 1.30.0, starlette 1.7.0, httpx 0.28.1, anyio 4.15.1 (asyncio):

V3 holds; no fallback is needed.

Construction. ``StreamableHTTPServerTransport(mcp_session_id: str | None,
is_json_response_enabled: bool = False, event_store: EventStore | None = None,
security_settings: TransportSecuritySettings | None = None, retry_interval: int | None =
None, idle_timeout: float | None = None)``. The session ID must match
``^[\\x21-\\x7E]+$`` (``secrets.token_hex(16)`` always does), otherwise ``ValueError``.
``security_settings=None`` already disables DNS rebinding protection; the explicit
``TransportSecuritySettings(enable_dns_rebinding_protection=False)`` does the same, while
``TransportSecuritySettings()`` turns it on (421 for any Host not listed). Without
``idle_timeout`` the transport never ends a session by itself (``idle_scope`` stays None).
The transport ignores the request path, so binding a session to its path is McpRouter's job.

JSON response mode. A POST carrying a request is answered with status 200,
``Content-Type: application/json``, ``mcp-session-id: <own ID>`` and the JSON-RPC response
as the whole body (``model_dump_json(by_alias=True, exclude_none=True)``), never SSE; this
includes JSON-RPC error responses. A POST carrying a notification (or a response) gets 202
with an empty body and the same two headers; the 202 goes out before the message is
handed to the read stream. The response to initialize carries the transport's session ID,
so the SDK announces the gateway's ID.

POST checks, in this order (the table task 22.3 aligns with; "error" means a body
``{"jsonrpc":"2.0","id":"server-error","error":{"code":<code>,"message":<message>}}`` with
``Content-Type: application/json`` and ``mcp-session-id: <own ID>``):

1. Content-Type missing, or its value lower-cased does not start with ``application/json``:
   400, text body ``Invalid Content-Type header``, no Content-Type and no mcp-session-id
   header (``TransportSecurityMiddleware``; runs even with DNS rebinding protection off).
2. No entry of Accept (split on ``,``, each stripped) starts with ``application/json``
   (case-sensitive; ``*/*``, ``application/*`` and a missing Accept fail; ``text/event-stream``
   is not required in JSON mode): 406, error -32600 ``Not Acceptable: Client must accept
   application/json``.
3. The Content-Type value up to the first ``;``, split on ``,`` and stripped, has no part
   equal to ``application/json`` (case-sensitive, so ``APPLICATION/JSON`` and
   ``application/json-seq`` fail here): 415, error -32600 ``Unsupported Media Type:
   Content-Type must be application/json``.
4. Body is not JSON: 400, error -32700 ``Parse error: <json error>``.
5. Body is not one JSON-RPC message (batches included): 400, error -32602
   ``Validation error: <pydantic error>``.
6. initialize only: an mcp-session-id header that differs from the transport's: 404, error
   -32600 ``Not Found: Invalid or expired session ID``. A matching or missing header passes;
   mcp-protocol-version is not checked for initialize.
7. Everything else (requests, notifications, responses): missing mcp-session-id: 400, error
   -32600 ``Bad Request: Missing session ID``; a different one: 404, error -32600 ``Not
   Found: Invalid or expired session ID``; mcp-protocol-version outside 2024-11-05,
   2025-03-26, 2025-06-18, 2025-11-25: 400, error -32600 ``Bad Request: Unsupported
   protocol version: <v>. Supported versions: 2024-11-05, 2025-03-26, 2025-06-18,
   2025-11-25``; a missing header counts as 2025-03-26 and passes.

A rejected request never reaches the read stream and leaves the transport usable. For the
preflight only items 1 to 3 matter, in that order: McpRouter's own body check ("a single
JSON-RPC request with method initialize", else 400) covers items 4 and 5, and a request
with a session ID never takes the preflight path (item 6). The router checks the body
before the headers while the transport checks the headers first; that only changes which
status a request with both faults gets, and such a request never reaches a transport. To
accept exactly the bodies the transport accepts, parse with ``json.loads`` and
``types.JSONRPCMessage.model_validate`` and require ``isinstance(root, JSONRPCRequest)``
and ``root.method == "initialize"``, as the transport does.

DELETE. With the right mcp-session-id (and a supported or missing protocol version) the
transport calls ``terminate()`` and answers 200 with an empty body, ``Content-Type:
application/json`` and the mcp-session-id header; a missing or different session ID gets
400 or 404 as in item 7 and leaves the session alive. ``terminate()`` (idempotent) marks the
transport terminated and closes both ends of its streams: a reader already waiting on the
read stream sees the end of the stream, a ``receive()`` that starts afterwards raises
``anyio.ClosedResourceError``, and so does a ``send()`` on the write stream. With a filter
that handles both, the filter closes its output, ``Server.run`` cancels its in-flight
handlers and returns, and ``connect()`` is left, all without cancelling the session task.
Afterwards every request on the transport (POST, initialize included, DELETE, GET) gets
404, error -32600 ``Not Found: Session has been terminated``; only the Content-Type check
of item 1 still runs before it. A POST still waiting for its response when DELETE arrives
gets 500, error -32600 ``Error processing request: No response received``, and its handler
is cancelled.

Other methods. PUT and the like: 405, error -32600 ``Method Not Allowed`` with
``Allow: GET, POST, DELETE`` (no session check). GET without ``text/event-stream`` in
Accept: 406, error -32600 ``Not Acceptable: Client must accept text/event-stream``; with it,
the transport opens a standalone SSE stream even in JSON mode (read from the source, not
driven here), so McpRouter answers GET with 405 itself, as designed.

connect(). An async context manager; on entry it creates the four streams (zero buffer)
and starts the message router, then yields ``(read_stream, write_stream)``. Before entry a
POST raises ``ValueError("No read stream writer available. Ensure connect() is called
first.")`` out of ``handle_request`` instead of answering. From entry on the transport takes
requests: a POST waits until its message is read from the read stream, which is a
``SessionMessage`` with ``ServerMessageMetadata(request_context=<starlette Request>)``, and
is answered by whatever JSON-RPC response or error with the same id is written to the
write stream, so MessageFilter can answer a method itself by writing the ``JSONRPCError``
to the write stream. Recommended composition (as in
``StreamableHTTPSessionManager._serve_opening_request``)::

    async def run_session(transport, server, *, task_status=anyio.TASK_STATUS_IGNORED):
        async with transport.connect() as (read_stream, write_stream):
            task_status.started()
            filtered_send, filtered_receive = anyio.create_memory_object_stream[...](0)
            async with anyio.create_task_group() as tg:
                tg.start_soon(message_filter, read_stream, filtered_send, write_stream, hooks)
                await server.run(filtered_receive, write_stream, INIT_OPTIONS,
                                 raise_exceptions=False)

    await task_group.start(run_session, transport, server)  # returns once connect() is entered
    await transport.handle_request(scope, receive, send)

Details for tasks 22.2 and 22.3:

- MessageFilter must treat ``anyio.ClosedResourceError`` from the read stream like the end
  of the stream (``BaseSession._receive_loop`` in the SDK does the same) and then close its
  output stream; that is what lets ``Server.run`` return after DELETE or ``terminate()``.
  When it writes its own error response to the write stream after the session ended, it
  gets ``anyio.ClosedResourceError`` (``BrokenResourceError`` if only the reading end was
  closed) and should drop the response, as ``Server`` does.
- Leaving ``connect()`` (e.g. by cancelling the session task) does not mark the transport
  terminated: the next POST raises ``anyio.ClosedResourceError`` out of ``handle_request``
  (after sending a 500, per the source). Call ``transport.terminate()`` whenever a session
  ends (freeze, delete, shutdown) before or instead of cancelling its task; afterwards
  requests get 404.
- DELETE while a request of the session is in flight drops that request's response (500
  above). ``end_session`` takes the sandbox lock first, so a call in progress finishes its
  work, but its response is lost if DELETE reaches the transport before the response has
  been routed; to rule that out, McpRouter can wait until the session has no POST in
  flight before delegating DELETE.
- The transport reads the body through ``receive`` (``Request.body()``), so after
  McpRouter has consumed the body it must hand the transport a replaying ``receive``.
- ``Server.run`` cancels in-flight handlers when its read stream ends; a handler's late
  response to a closed transport is dropped (logged at debug level).

Read from the SDK source, not asserted here: ``StreamableHTTPSessionManager`` wraps its
transports in ``RequestBodyLimitMiddleware`` (413 above 4 MiB); a bare transport has no
body limit, which the gateway's BodyLimit provides. Responses and errors are routed by
``str(id)``, so concurrent requests with ids ``1`` and ``"1"`` in one session would share a
request stream.
"""

import inspect
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
import pytest
from anyio.abc import TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.message import ServerMessageMetadata, SessionMessage
from starlette.requests import Request

# Upper bound for every test; a hang fails the test instead of blocking the run.
TIMEOUT_S = 10

# The shape of the gateway's own IDs (secrets.token_hex(16)), fixed for readable failures.
SESSION_ID = "0123456789abcdef0123456789abcdef"
OTHER_SESSION_ID = "fedcba9876543210fedcba9876543210"
PATH = "/mcp"
PROTOCOL_VERSION = "2025-11-25"
SUPPORTED = "2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25"

INIT_OPTIONS = InitializationOptions(
    server_name="erp-mock-mcp-probe",
    server_version="0.0.0",
    capabilities=types.ServerCapabilities(tools=types.ToolsCapability(listChanged=False)),
    instructions="Probe server.",
)

NO_DNS_REBINDING_PROTECTION = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# The Accept and Content-Type headers the official client puts on every request
# (mcp.client.streamable_http, StreamableHTTPTransport._prepare_headers).
JSON_AND_SSE = "application/json, text/event-stream"
POST_HEADERS = {"content-type": "application/json", "accept": JSON_AND_SSE}

INITIALIZED: dict[str, Any] = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def session_headers(session_id: str = SESSION_ID) -> dict[str, str]:
    """Headers of a request inside an initialized session, as the official client sends them."""
    return {**POST_HEADERS, "mcp-session-id": session_id, "mcp-protocol-version": PROTOCOL_VERSION}


def without(headers: dict[str, str], name: str) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key != name}


def initialize_body(request_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "probe-client", "version": "0.0.0"},
        },
    }


def ping_body(request_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": "ping"}


def make_transport(
    security_settings: TransportSecuritySettings | None = NO_DNS_REBINDING_PROTECTION,
) -> StreamableHTTPServerTransport:
    """The construction of design component 7.2."""
    return StreamableHTTPServerTransport(
        mcp_session_id=SESSION_ID,
        is_json_response_enabled=True,
        event_store=None,
        security_settings=security_settings,
    )


def make_server() -> Server:
    """A low-level server with a raw tools/list handler (no tools) and the default ping."""
    server: Server = Server("erp-mock-mcp-probe")

    async def list_tools(req: types.ListToolsRequest) -> types.ServerResult:
        return types.ServerResult(types.ListToolsResult(tools=[]))

    server.request_handlers[types.ListToolsRequest] = list_tools
    return server


def blocking_server(entered: anyio.Event, cancelled: anyio.Event) -> Server:
    """``make_server`` plus a tools/call handler that waits until it is cancelled."""
    server = make_server()

    async def call_tool(req: types.CallToolRequest) -> types.ServerResult:
        entered.set()
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise
        return types.ServerResult(types.CallToolResult(content=[]))

    server.request_handlers[types.CallToolRequest] = call_tool
    return server


async def _pass_through(
    src: MemoryObjectReceiveStream[SessionMessage | Exception],
    dst: MemoryObjectSendStream[SessionMessage | Exception],
    record: list[SessionMessage | Exception],
) -> None:
    """Stand-in for MessageFilter: record and forward, close ``dst`` once ``src`` ends.

    ``terminate()`` closes both ends of the read stream, so a ``receive()`` that starts
    afterwards raises ``ClosedResourceError`` instead of ending the iteration; like the
    SDK's own receive loop, treat that as the end of the stream.
    """
    async with dst:
        try:
            async for message in src:
                record.append(message)
                await dst.send(message)
        except anyio.ClosedResourceError:
            pass


async def run_session(
    transport: StreamableHTTPServerTransport,
    server: Server,
    delivered: list[SessionMessage | Exception],
    finished: anyio.Event,
    *,
    task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """The per-session task of design component 7.2.

    Reports started once ``transport.connect()`` has been entered, and sets ``finished``
    only when ``Server.run`` has returned and ``connect()`` has been left on their own.
    """
    async with transport.connect() as (read_stream, write_stream):
        task_status.started()
        filtered_send, filtered_receive = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](0)
        async with anyio.create_task_group() as tg:
            tg.start_soon(_pass_through, read_stream, filtered_send, delivered)
            await server.run(filtered_receive, write_stream, INIT_OPTIONS, raise_exceptions=False)
    finished.set()


@asynccontextmanager
async def http_client(transport: StreamableHTTPServerTransport) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client whose requests go straight to ``transport.handle_request``."""
    asgi = httpx.ASGITransport(app=transport.handle_request)
    async with httpx.AsyncClient(transport=asgi, base_url="http://127.0.0.1:8000") as client:
        # httpx adds "Accept: */*" to every request by default; drop it so that each
        # request carries exactly the Accept header the test gives it.
        del client.headers["accept"]
        yield client


@dataclass
class Session:
    client: httpx.AsyncClient
    transport: StreamableHTTPServerTransport
    # Messages the transport put on its read stream, in order.
    delivered: list[SessionMessage | Exception] = field(default_factory=list)
    finished: anyio.Event = field(default_factory=anyio.Event)


@asynccontextmanager
async def running(
    server: Server | None = None,
    security_settings: TransportSecuritySettings | None = NO_DNS_REBINDING_PROTECTION,
) -> AsyncIterator[Session]:
    """A transport whose session task runs; the task is cancelled when the block ends."""
    transport = make_transport(security_settings)
    async with http_client(transport) as client, anyio.create_task_group() as tg:
        session = Session(client, transport)
        await tg.start(
            run_session, transport, server or make_server(), session.delivered, session.finished
        )
        try:
            yield session
        finally:
            tg.cancel_scope.cancel()


async def post(client: httpx.AsyncClient, body: Any, headers: dict[str, str]) -> httpx.Response:
    """POST ``body`` (JSON-encoded unless it is a str already) with exactly ``headers``."""
    content = body if isinstance(body, str) else json.dumps(body)
    return await client.post(PATH, content=content.encode(), headers=headers)


# Methods delivered by ``handshake``.
HANDSHAKE = ["initialize", "notifications/initialized", "ping"]


async def handshake(session: Session) -> None:
    """initialize and notifications/initialized as the official client sends them, then a ping.

    The 202 for a notification goes out before the transport hands the notification to the
    read stream; the ping round trip makes sure the filter has forwarded it.
    """
    response = await post(session.client, initialize_body(), POST_HEADERS)
    assert response.status_code == 200
    response = await post(session.client, INITIALIZED, session_headers())
    assert response.status_code == 202
    response = await post(session.client, ping_body(100), session_headers())
    assert response.json() == {"jsonrpc": "2.0", "id": 100, "result": {}}
    assert methods(session.delivered) == HANDSHAKE


def methods(messages: list[SessionMessage | Exception]) -> list[str]:
    """JSON-RPC methods of the delivered requests and notifications, in order."""
    found = []
    for message in messages:
        assert isinstance(message, SessionMessage)
        root = message.message.root
        assert isinstance(root, types.JSONRPCRequest | types.JSONRPCNotification)
        found.append(root.method)
    return found


@dataclass(frozen=True)
class Rejected:
    """How the transport refuses a request."""

    status: int
    # JSON-RPC error code; None for the plain-text answer of TransportSecurityMiddleware.
    code: int | None
    message: str
    # ``message`` is only a prefix (the rest quotes a parser error).
    prefix: bool = False


def assert_rejected(response: httpx.Response, expected: Rejected) -> None:
    assert response.status_code == expected.status
    if expected.code is None:
        assert response.text == expected.message
        assert "content-type" not in response.headers
        assert "mcp-session-id" not in response.headers
        return
    assert response.headers["content-type"] == "application/json"
    assert response.headers["mcp-session-id"] == SESSION_ID
    body = response.json()
    assert list(body) == ["jsonrpc", "id", "error"]
    assert (body["jsonrpc"], body["id"]) == ("2.0", "server-error")
    assert list(body["error"]) == ["code", "message"]
    assert body["error"]["code"] == expected.code
    if expected.prefix:
        assert body["error"]["message"].startswith(expected.message)
    else:
        assert body["error"]["message"] == expected.message


BAD_CONTENT_TYPE = Rejected(400, None, "Invalid Content-Type header")
NOT_ACCEPTABLE = Rejected(
    406, types.INVALID_REQUEST, "Not Acceptable: Client must accept application/json"
)
UNSUPPORTED_MEDIA_TYPE = Rejected(
    415, types.INVALID_REQUEST, "Unsupported Media Type: Content-Type must be application/json"
)
UNKNOWN_SESSION = Rejected(404, types.INVALID_REQUEST, "Not Found: Invalid or expired session ID")
MISSING_SESSION = Rejected(400, types.INVALID_REQUEST, "Bad Request: Missing session ID")
TERMINATED = Rejected(404, types.INVALID_REQUEST, "Not Found: Session has been terminated")

# The opening request of a session, refused by the transport (preflight items 1 to 6).
INITIALIZE_REJECTED = [
    pytest.param(
        {"accept": JSON_AND_SSE}, initialize_body(), BAD_CONTENT_TYPE, id="content-type-missing"
    ),
    pytest.param(
        {"content-type": "text/plain", "accept": JSON_AND_SSE},
        initialize_body(),
        BAD_CONTENT_TYPE,
        id="content-type-text-plain",
    ),
    pytest.param(
        {"content-type": "text/plain; x=application/json", "accept": JSON_AND_SSE},
        initialize_body(),
        BAD_CONTENT_TYPE,
        id="content-type-json-not-at-start",
    ),
    pytest.param(
        {"content-type": "APPLICATION/JSON", "accept": JSON_AND_SSE},
        initialize_body(),
        UNSUPPORTED_MEDIA_TYPE,
        id="content-type-upper-case",
    ),
    pytest.param(
        {"content-type": "application/json-seq", "accept": JSON_AND_SSE},
        initialize_body(),
        UNSUPPORTED_MEDIA_TYPE,
        id="content-type-json-prefix",
    ),
    pytest.param(
        {"content-type": "application/json"}, initialize_body(), NOT_ACCEPTABLE, id="accept-missing"
    ),
    pytest.param(
        {"content-type": "application/json", "accept": "*/*"},
        initialize_body(),
        NOT_ACCEPTABLE,
        id="accept-any",
    ),
    pytest.param(
        {"content-type": "application/json", "accept": "application/*"},
        initialize_body(),
        NOT_ACCEPTABLE,
        id="accept-application-any",
    ),
    pytest.param(
        {"content-type": "application/json", "accept": "text/event-stream"},
        initialize_body(),
        NOT_ACCEPTABLE,
        id="accept-sse-only",
    ),
    pytest.param(
        {"content-type": "application/json", "accept": "Application/JSON"},
        initialize_body(),
        NOT_ACCEPTABLE,
        id="accept-mixed-case",
    ),
    pytest.param(
        {"content-type": "text/plain", "accept": "text/html"},
        initialize_body(),
        BAD_CONTENT_TYPE,
        id="order-content-type-prefix-before-accept",
    ),
    pytest.param(
        {"content-type": "APPLICATION/JSON", "accept": "text/html"},
        initialize_body(),
        NOT_ACCEPTABLE,
        id="order-accept-before-media-type",
    ),
    pytest.param(
        POST_HEADERS,
        "{",
        Rejected(400, types.PARSE_ERROR, "Parse error: ", prefix=True),
        id="body-not-json",
    ),
    pytest.param(
        POST_HEADERS,
        [initialize_body()],
        Rejected(400, types.INVALID_PARAMS, "Validation error: ", prefix=True),
        id="body-batch",
    ),
    pytest.param(
        POST_HEADERS,
        {"jsonrpc": "2.0", "id": 1},
        Rejected(400, types.INVALID_PARAMS, "Validation error: ", prefix=True),
        id="body-not-jsonrpc",
    ),
    pytest.param(
        {**POST_HEADERS, "mcp-session-id": OTHER_SESSION_ID},
        initialize_body(),
        UNKNOWN_SESSION,
        id="foreign-session-id",
    ),
]

# Header variants of the opening request that the transport accepts.
INITIALIZE_ACCEPTED = [
    pytest.param(POST_HEADERS, id="official-client"),
    pytest.param(
        {"content-type": "application/json", "accept": "application/json"}, id="accept-json-only"
    ),
    pytest.param(
        {"content-type": "application/json", "accept": "text/html;q=0.9, application/json;q=0.1"},
        id="accept-json-with-parameters-among-others",
    ),
    pytest.param(
        {"content-type": "application/json", "accept": "application/json-seq"},
        id="accept-prefix-match",
    ),
    pytest.param(
        {"content-type": "application/json; charset=utf-8", "accept": "application/json"},
        id="content-type-with-parameter",
    ),
    pytest.param(
        {"content-type": "application/json, text/plain", "accept": "application/json"},
        id="content-type-list-with-json-first",
    ),
    pytest.param({**POST_HEADERS, "mcp-session-id": SESSION_ID}, id="own-session-id"),
    pytest.param(
        {**POST_HEADERS, "mcp-protocol-version": "1999-01-01"}, id="protocol-version-not-checked"
    ),
]

# Requests inside an initialized session that the transport refuses (preflight item 7).
IN_SESSION_REJECTED = [
    pytest.param(
        ping_body(2),
        without(session_headers(), "mcp-session-id"),
        MISSING_SESSION,
        id="no-session-id",
    ),
    pytest.param(
        INITIALIZED,
        without(session_headers(), "mcp-session-id"),
        MISSING_SESSION,
        id="notification-without-session-id",
    ),
    pytest.param(
        ping_body(2), session_headers(OTHER_SESSION_ID), UNKNOWN_SESSION, id="foreign-session-id"
    ),
    pytest.param(
        ping_body(2),
        {**session_headers(), "mcp-protocol-version": "1999-01-01"},
        Rejected(
            400,
            types.INVALID_REQUEST,
            f"Bad Request: Unsupported protocol version: 1999-01-01. Supported versions: {SUPPORTED}",
        ),
        id="unsupported-protocol-version",
    ),
    pytest.param(
        ping_body(2),
        {**session_headers(), "accept": "text/event-stream"},
        NOT_ACCEPTABLE,
        id="accept-without-json",
    ),
    pytest.param(
        ping_body(2),
        {**session_headers(), "content-type": "text/plain"},
        BAD_CONTENT_TYPE,
        id="content-type-not-json",
    ),
    pytest.param(
        ping_body(2),
        {"content-type": "application/json", "accept": "*/*"},
        NOT_ACCEPTABLE,
        id="order-accept-before-session-id",
    ),
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.req("15.1")
def test_v3_constructor_signature_and_session_id_rule() -> None:
    parameters = inspect.signature(StreamableHTTPServerTransport.__init__).parameters
    assert list(parameters) == [
        "self",
        "mcp_session_id",
        "is_json_response_enabled",
        "event_store",
        "security_settings",
        "retry_interval",
        "idle_timeout",
    ]
    transport = make_transport()
    assert transport.mcp_session_id == SESSION_ID
    assert transport.is_json_response_enabled
    assert not transport.is_terminated
    # Without idle_timeout the transport never ends the session by itself.
    assert transport.idle_scope is None

    StreamableHTTPServerTransport(mcp_session_id=secrets.token_hex(16))
    for bad in ["", "has space", "tab\tinside", "非ASCII"]:
        with pytest.raises(ValueError, match="visible ASCII"):
            StreamableHTTPServerTransport(mcp_session_id=bad)


@pytest.mark.anyio
@pytest.mark.req("15.1")
async def test_v3_json_response_mode_answers_each_post_with_one_json_body() -> None:
    with anyio.fail_after(TIMEOUT_S):
        async with running() as s:
            init = await post(s.client, initialize_body(1), POST_HEADERS)
            assert init.status_code == 200
            assert init.headers["content-type"] == "application/json"
            assert init.headers["mcp-session-id"] == SESSION_ID
            body = init.json()
            assert (body["jsonrpc"], body["id"]) == ("2.0", 1)
            assert body["result"]["protocolVersion"] == PROTOCOL_VERSION
            assert body["result"]["capabilities"] == {"tools": {"listChanged": False}}

            note = await post(s.client, INITIALIZED, session_headers())
            assert note.status_code == 202
            assert note.content == b""
            assert note.headers["content-type"] == "application/json"
            assert note.headers["mcp-session-id"] == SESSION_ID

            listed = await post(
                s.client, {"jsonrpc": "2.0", "id": "two", "method": "tools/list"}, session_headers()
            )
            assert listed.status_code == 200
            assert listed.headers["content-type"] == "application/json"
            assert listed.headers["mcp-session-id"] == SESSION_ID
            assert listed.json() == {"jsonrpc": "2.0", "id": "two", "result": {"tools": []}}

            # A missing Mcp-Protocol-Version counts as 2025-03-26, which is supported.
            pinged = await post(
                s.client, ping_body(3), without(session_headers(), "mcp-protocol-version")
            )
            assert pinged.status_code == 200
            assert pinged.json() == {"jsonrpc": "2.0", "id": 3, "result": {}}

            # The transport never looks at the path.
            elsewhere = await s.client.post(
                "/p/other/mcp", content=json.dumps(ping_body(4)).encode(), headers=session_headers()
            )
            assert elsewhere.json() == {"jsonrpc": "2.0", "id": 4, "result": {}}

            assert methods(s.delivered) == [
                "initialize",
                "notifications/initialized",
                "tools/list",
                "ping",
                "ping",
            ]


@pytest.mark.anyio
@pytest.mark.req("15.1")
@pytest.mark.parametrize(("headers", "body", "expected"), INITIALIZE_REJECTED)
async def test_v3_initialize_rejected_by_the_transport(
    headers: dict[str, str], body: Any, expected: Rejected
) -> None:
    with anyio.fail_after(TIMEOUT_S):
        async with running() as s:
            assert_rejected(await post(s.client, body, headers), expected)
            assert s.delivered == []
            assert not s.transport.is_terminated
            # The refusal leaves the transport usable.
            assert (await post(s.client, initialize_body(), POST_HEADERS)).status_code == 200


@pytest.mark.anyio
@pytest.mark.req("15.1")
@pytest.mark.parametrize("headers", INITIALIZE_ACCEPTED)
async def test_v3_initialize_accepted_by_the_transport(headers: dict[str, str]) -> None:
    with anyio.fail_after(TIMEOUT_S):
        async with running() as s:
            response = await post(s.client, initialize_body(), headers)
            assert response.status_code == 200
            assert response.headers["mcp-session-id"] == SESSION_ID
            assert response.json()["id"] == 1
            assert "result" in response.json()
            assert methods(s.delivered) == ["initialize"]


@pytest.mark.anyio
@pytest.mark.req("14.7", "14.8")
@pytest.mark.parametrize(("body", "headers", "expected"), IN_SESSION_REJECTED)
async def test_v3_requests_in_a_session_rejected_by_the_transport(
    body: dict[str, Any], headers: dict[str, str], expected: Rejected
) -> None:
    with anyio.fail_after(TIMEOUT_S):
        async with running() as s:
            await handshake(s)
            assert_rejected(await post(s.client, body, headers), expected)
            # The request did not reach the server, and the session goes on.
            assert not s.transport.is_terminated
            assert (await post(s.client, ping_body(3), session_headers())).status_code == 200
            assert methods(s.delivered) == [*HANDSHAKE, "ping"]


@pytest.mark.anyio
@pytest.mark.req("14.7")
async def test_v3_other_methods_and_get() -> None:
    with anyio.fail_after(TIMEOUT_S):
        async with running() as s:
            await handshake(s)
            put = await s.client.put(PATH, content=b"{}", headers=session_headers(OTHER_SESSION_ID))
            assert_rejected(put, Rejected(405, types.INVALID_REQUEST, "Method Not Allowed"))
            assert put.headers["allow"] == "GET, POST, DELETE"

            get = await s.client.get(
                PATH, headers={**session_headers(), "accept": "application/json"}
            )
            assert_rejected(
                get,
                Rejected(
                    406,
                    types.INVALID_REQUEST,
                    "Not Acceptable: Client must accept text/event-stream",
                ),
            )
            assert (await post(s.client, ping_body(3), session_headers())).status_code == 200
            assert methods(s.delivered) == [*HANDSHAKE, "ping"]


@pytest.mark.anyio
@pytest.mark.req("15.7", "14.7", "14.8")
async def test_v3_delete_answers_200_and_terminates_the_transport() -> None:
    with anyio.fail_after(TIMEOUT_S):
        async with running() as s:
            await handshake(s)
            missing = await s.client.delete(
                PATH, headers=without(session_headers(), "mcp-session-id")
            )
            assert_rejected(missing, MISSING_SESSION)
            foreign = await s.client.delete(PATH, headers=session_headers(OTHER_SESSION_ID))
            assert_rejected(foreign, UNKNOWN_SESSION)
            assert not s.transport.is_terminated

            deleted = await s.client.delete(PATH, headers=session_headers())
            assert deleted.status_code == 200
            assert deleted.content == b""
            assert deleted.headers["content-type"] == "application/json"
            assert deleted.headers["mcp-session-id"] == SESSION_ID
            assert s.transport.is_terminated
            # Server.run returns and connect() is left without cancelling the session task.
            await s.finished.wait()

            assert_rejected(await post(s.client, ping_body(5), session_headers()), TERMINATED)
            assert_rejected(await post(s.client, initialize_body(6), POST_HEADERS), TERMINATED)
            assert_rejected(await s.client.delete(PATH, headers=session_headers()), TERMINATED)
            assert_rejected(await s.client.get(PATH, headers=session_headers()), TERMINATED)
            # Only the Content-Type check of TransportSecurityMiddleware comes first.
            assert_rejected(
                await post(s.client, ping_body(7), without(session_headers(), "content-type")),
                BAD_CONTENT_TYPE,
            )
            await s.transport.terminate()  # idempotent
            assert methods(s.delivered) == HANDSHAKE


@pytest.mark.anyio
@pytest.mark.req("15.7")
async def test_v3_delete_while_a_request_is_in_flight() -> None:
    entered, cancelled = anyio.Event(), anyio.Event()
    call = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "probe_tool", "arguments": {}},
    }
    with anyio.fail_after(TIMEOUT_S):
        async with running(blocking_server(entered, cancelled)) as s:
            await handshake(s)
            responses: list[httpx.Response] = []

            async def in_flight() -> None:
                responses.append(await post(s.client, call, session_headers()))

            async with anyio.create_task_group() as tg:
                tg.start_soon(in_flight)
                await entered.wait()
                deleted = await s.client.delete(PATH, headers=session_headers())
                assert deleted.status_code == 200

            (response,) = responses
            assert_rejected(
                response,
                Rejected(
                    500, types.INVALID_REQUEST, "Error processing request: No response received"
                ),
            )
            await s.finished.wait()
            assert cancelled.is_set()


@pytest.mark.anyio
@pytest.mark.req("15.1")
async def test_v3_transport_takes_requests_once_connect_is_entered() -> None:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "method": "resources/list"}
    with anyio.fail_after(TIMEOUT_S):
        transport = make_transport()
        async with http_client(transport) as client:
            with pytest.raises(ValueError, match=r"Ensure connect\(\) is called first"):
                await post(client, initialize_body(), POST_HEADERS)

            responses: list[httpx.Response] = []

            async def send_request() -> None:
                responses.append(await post(client, request, session_headers()))

            async with transport.connect() as (read_stream, write_stream):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(send_request)
                    # The read stream has no buffer: the POST waits until its message is read.
                    await anyio.sleep(0.1)
                    assert responses == []
                    message = await read_stream.receive()
                    assert isinstance(message, SessionMessage)
                    root = message.message.root
                    assert isinstance(root, types.JSONRPCRequest)
                    assert (root.id, root.method) == (7, "resources/list")
                    assert isinstance(message.metadata, ServerMessageMetadata)
                    assert message.metadata.related_request_id is None
                    assert isinstance(message.metadata.request_context, Request)
                    assert message.metadata.request_context.url.path == PATH
                    # Any JSON-RPC response with the request's id answers the POST; this is
                    # how MessageFilter answers a method itself.
                    error = types.JSONRPCError(
                        jsonrpc="2.0",
                        id=7,
                        error=types.ErrorData(
                            code=types.METHOD_NOT_FOUND, message="Method not found"
                        ),
                    )
                    await write_stream.send(SessionMessage(types.JSONRPCMessage(error)))

            (response,) = responses
            assert response.status_code == 200
            assert response.headers["mcp-session-id"] == SESSION_ID
            assert response.json() == {
                "jsonrpc": "2.0",
                "id": 7,
                "error": {"code": types.METHOD_NOT_FOUND, "message": "Method not found"},
            }


@pytest.mark.anyio
@pytest.mark.req("14.7")
async def test_v3_leaving_connect_without_terminate_breaks_later_requests() -> None:
    with anyio.fail_after(TIMEOUT_S):
        transport = make_transport()
        async with http_client(transport) as client:
            async with transport.connect():
                pass
            assert not transport.is_terminated
            # A 500 goes out first, then the error escapes handle_request.
            with pytest.raises(anyio.ClosedResourceError):
                await post(client, ping_body(8), session_headers())

            await transport.terminate()
            assert_rejected(await post(client, ping_body(9), session_headers()), TERMINATED)


@pytest.mark.anyio
@pytest.mark.req("15.7")
async def test_v3_terminate_closes_both_ends_of_the_streams() -> None:
    ended: list[str] = []
    with anyio.fail_after(TIMEOUT_S):
        transport = make_transport()
        async with transport.connect() as (read_stream, write_stream):

            async def waiting_reader() -> None:
                async for _ in read_stream:
                    pass
                ended.append("end of stream")

            async with anyio.create_task_group() as tg:
                tg.start_soon(waiting_reader)
                await anyio.wait_all_tasks_blocked()
                await transport.terminate()
            # A reader already waiting sees the end of the stream ...
            assert ended == ["end of stream"]
            # ... a receive that starts afterwards fails, and so does a late response.
            with pytest.raises(anyio.ClosedResourceError):
                await read_stream.receive()
            with pytest.raises(anyio.ClosedResourceError):
                await write_stream.send(
                    SessionMessage(
                        types.JSONRPCMessage(
                            types.JSONRPCNotification(jsonrpc="2.0", method="notifications/message")
                        )
                    )
                )
        assert transport.is_terminated


@pytest.mark.anyio
@pytest.mark.req("15.1")
@pytest.mark.parametrize(
    ("settings", "expected_status"),
    [
        pytest.param(NO_DNS_REBINDING_PROTECTION, 200, id="explicitly-disabled"),
        pytest.param(None, 200, id="none-means-disabled"),
        pytest.param(TransportSecuritySettings(), 421, id="default-settings-enable-it"),
    ],
)
async def test_v3_dns_rebinding_protection_settings(
    settings: TransportSecuritySettings | None, expected_status: int
) -> None:
    headers = {**POST_HEADERS, "host": "evil.example", "origin": "https://evil.example"}
    with anyio.fail_after(TIMEOUT_S):
        async with running(security_settings=settings) as s:
            response = await post(s.client, initialize_body(), headers)
            if expected_status == 421:
                assert_rejected(response, Rejected(421, None, "Invalid Host header"))
            else:
                assert response.status_code == expected_status
                assert "result" in response.json()
