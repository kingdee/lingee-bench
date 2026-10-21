"""What reaches the SDK session, and what is answered before it (design.md 7.1).

The filter sits between a transport's read stream and ``Server.run``, on both transports
(design.md architecture)::

    read, write = <transport streams>
    to_sdk, from_filter = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    async with anyio.create_task_group() as tg:
        tg.start_soon(message_filter, read, to_sdk, write, hooks)
        tg.start_soon(server.run, from_filter, write, INIT_OPTIONS)

Every message a client sends passes through :func:`message_filter` on its way to the SDK
session, and the filter does three things with it and nothing else:

1. a request whose method is not one of :data:`ALLOWED_METHODS` is answered here, with
   JSON-RPC -32601, and is not forwarded (requirement 14.10);
2. every arriving request - the rejected ones included - is reported to ``hooks.activity``,
   which is how a request that needs no sandbox lock still keeps its sandbox from turning
   idle (requirement 15.5);
3. an ``initialize`` is handed to ``hooks.initialize`` first, where there is one, so that
   the session's sandbox exists before the SDK answers (stdio alone, design.md 7.3).

Everything else is forwarded unchanged, in arrival order: the four whitelisted requests,
every notification, every response and every ``Exception`` the transport put on the stream
in place of a message it could not parse.

Why -32601 is written here rather than left to the SDK. Requirement 14.10 asks for -32601
for every method outside the whitelist, and the SDK gives that code to only some of them:
``BaseSession._receive_loop`` validates an incoming request against ``types.ClientRequest``
and answers -32602 "Invalid request parameters" to anything that is not one of its members
(any unknown method, ``completion/complete`` among the known ones that are), while -32601
comes from ``Server._handle_request`` and reaches only the ``ClientRequest`` methods that
have no handler registered - ``resources/list`` and ``prompts/list`` among them. The
difference is a property of one SDK version, probed in tests/probes/test_sdk_server.py on
mcp 1.30.0. Answering here makes the code the same for every rejected method and
independent of which methods the SDK's request union happens to carry.

What a rejected request leaves behind: nothing. No trace event is written
(requirement 14.10) - this module knows neither the sandbox nor its
:class:`~erp_mock.gateway.trace.TraceWriter`, which is what makes that structural rather
than remembered - the sandbox database is not touched, and the session goes on serving the
requests after it. The filter is not a session: it keeps no state between messages beyond
the loop itself, so a rejected request cannot change how the next one is treated.

Requirement 14.11 falls out of the same shape. The only message this module ever writes is
a response to a request a client sent: a ``JSONRPCError`` carrying that request's id. It
originates no request and no notification, and neither does the SDK session for the three
methods of :mod:`erp_mock.gateway.handlers`, so nothing with a ``method`` field ever
travels from the service to a client.

Two probe findings of the design's open items decide what this module does *not* do, both
on mcp 1.30.0 with pydantic 2.14.0 and anyio 4.15.1:

- V4 holds (tests/probes/test_sdk_server.py): the requests of one session reach their
  handlers in arrival order, so the sandbox lock is taken in that order and the ``seq`` of
  a call can be read under that lock (requirement 15.4). The filter therefore hands out no
  sequence number. Were V4 to break under a later SDK version, the fallback of task 22.2
  is to number the requests here, where they are still in one stream and in arrival order,
  and to have the handlers queue on that number instead of on the lock alone.
- V6 holds (tests/probes/test_sdk_client.py): the Streamable HTTP transport parses a POST
  body with ``json.loads`` and the stdio transport parses a line with pydantic-core's JSON
  parser, and the two agree on the type and the bit pattern of every number of a 32031
  literal corpus, so one tools/call means the same thing on both transports
  (requirement 24.1). The filter therefore does not touch the arguments of a request. Were
  V6 to break, the fallback is to re-parse the raw params here, once for both transports,
  before the request reaches the SDK.

The hooks are what the transports put in, and the filter asks for as little as it can:
``hooks.activity`` takes no argument and returns nothing, ``hooks.initialize`` takes the
request and returns nothing, and both are absent by default (:class:`FilterHooks`). So
this module holds no sandbox, no manager and no clock, and cannot be the reason two
transports treat a session differently:

===============  =====================================  =====================================
hook             Streamable HTTP (task 22.3)            stdio (task 22.4)
===============  =====================================  =====================================
``activity``     the sandbox of the session record      the sandbox of the process, once
                 the router looked up by session id     it has one
``initialize``   absent: the router creates the         creates or reuses the sandbox,
                 sandbox in the ASGI layer, before      writes ``session_start`` and
                 the transport sees the request         reports it on stderr (7.3)
===============  =====================================  =====================================

An initialize the hook cannot serve - the Snapshot of the initial dataset would not build -
is answered with JSON-RPC -32603 and not forwarded, so no session is initialized on a
sandbox that does not exist (requirement 15.14). The hook raises
:class:`InitializeFailed` with the message, which names the dataset and the anchor date and
is therefore the hook's to write; the code is this module's, requirement 15.14 giving the
same -32603 to both transports. Any other exception from a hook is left alone: it reaches
the task group of the transport, which ends the session rather than answering the request
with an error whose text nobody wrote.

Stream ownership, which the two transports rely on: the filter closes ``src`` and ``dst``
when the client's side ends - closing ``dst`` is what lets ``Server.run`` see the end of
its read stream and return - and never closes ``out``. ``out`` is the transport's write
stream, which the SDK session holds as well and closes itself when its receive loop ends
(``BaseSession._receive_loop``); a second closer would end the stdio stdout writer while
the SDK still means to write to it.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.shared.message import SessionMessage

__all__ = [
    "ALLOWED_METHODS",
    "INITIALIZE",
    "METHOD_NOT_FOUND_MESSAGE",
    "ActivityHook",
    "FilterHooks",
    "InitializeFailed",
    "InitializeHook",
    "message_filter",
]

# The one request method a hook may be interested in (design.md 7.3).
INITIALIZE: Final = "initialize"

# The request methods a session may call (requirement 14.10): the handshake, the keepalive
# and the two tool methods of :mod:`erp_mock.gateway.handlers`. Every other method - the
# ones the MCP specification defines for capabilities this service does not offer
# (``resources/list``, ``prompts/list``, ``completion/complete``) and any name at all - is
# answered with -32601 and never forwarded. Notifications are not checked against this:
# they are not requests and carry no id to answer (:func:`message_filter`).
ALLOWED_METHODS: Final = frozenset({INITIALIZE, "ping", "tools/list", "tools/call"})

# The message of the -32601 a request outside :data:`ALLOWED_METHODS` is answered with: it
# names the method that was asked for, so a client sees which of its requests was refused
# (requirement 14.10).
METHOD_NOT_FOUND_MESSAGE: Final = "不支持的方法：{method}"

# The ``jsonrpc`` of every message of the protocol.
_JSONRPC_VERSION: Final = "2.0"

# What a transport is told about an arriving request: that one arrived, and nothing else.
# The transport closes over the sandbox of the session and over the clock, so the filter
# needs neither (module docstring). Called for every request a client sends, before the
# request is looked at, so a request that is about to be refused counts as well: what
# requirement 15.5 measures the idle threshold against is any MCP request of a session
# bound to the sandbox.
type ActivityHook = Callable[[], None]

# What a transport does with an ``initialize`` before the SDK answers it: whatever has to
# exist by then. ``request`` is the message as it arrived, not yet validated by the SDK.
# Called for every initialize of the session, the second one included, so a transport that
# may receive more than one decides itself what to do with it (requirement 15.2).
type InitializeHook = Callable[[types.JSONRPCRequest], Awaitable[None]]


class InitializeFailed(Exception):
    """An ``initialize`` that cannot be served, raised by an :data:`InitializeHook`.

    ``message`` is what the client is told and belongs to the hook: requirement 15.14 asks
    for the initial dataset and the anchor date in it, which this module does not know. The
    filter answers the request with JSON-RPC -32603 and that message, and forwards nothing
    (:func:`message_filter`), so the session is never initialized against a sandbox that
    was not created.
    """

    __slots__ = ("message",)

    message: str

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class FilterHooks:
    """What a transport asks the filter to do on its behalf (design.md 7.1).

    - ``activity``: called once per arriving request, whitelisted or not, before the
      request is examined (requirement 15.5);
    - ``initialize``: awaited for every ``initialize`` before it is forwarded, and may
      raise :class:`InitializeFailed` to refuse it (requirement 15.14).

    Both default to absent, which is what a transport passes for a hook it has no use for:
    Streamable HTTP brings an ``activity`` hook alone, its router having created the
    sandbox and written ``session_start`` in the ASGI layer long before the transport reads
    the request (design.md 7.2), and a test of the filter itself brings neither. Frozen,
    and fixed for the life of a session: the hooks of a session are decided when it is
    connected.
    """

    activity: ActivityHook | None = None
    initialize: InitializeHook | None = None


async def message_filter(
    src: MemoryObjectReceiveStream[SessionMessage | Exception],
    dst: MemoryObjectSendStream[SessionMessage | Exception],
    out: MemoryObjectSendStream[SessionMessage],
    hooks: FilterHooks,
) -> None:
    """Pass the messages of one session from ``src`` to ``dst``, answering some on ``out``.

    ``src`` is the transport's read stream and ``dst`` the stream ``Server.run`` reads, so
    what this forwards is what the SDK sees; ``out`` is the transport's write stream, which
    goes straight to the client and is where a request answered here is answered. Runs
    until ``src`` ends - the client hung up, stdin reached EOF, the session was terminated -
    and closes ``src`` and ``dst`` on the way out, which ends ``Server.run``; ``out`` is
    left to the SDK session and the transport (module docstring).

    What happens to one message, in order:

    1. an ``Exception`` - a body the transport could not parse - is forwarded as it is, the
       SDK being the one that decides what such a message means;
    2. a request reports itself to ``hooks.activity`` (requirement 15.5);
    3. a request whose method is outside :data:`ALLOWED_METHODS` is answered with -32601
       and dropped (requirement 14.10);
    4. an ``initialize`` is awaited through ``hooks.initialize`` where there is one, and is
       dropped with -32603 where that raises :class:`InitializeFailed` (requirement 15.14);
    5. everything else - the whitelisted requests, notifications and responses - is
       forwarded unchanged.

    Notifications are forwarded without being checked against the whitelist on purpose:
    JSON-RPC has no response to a notification, so an unknown one cannot be refused, and
    the ones a client does send (``notifications/initialized``,
    ``notifications/cancelled``) are the SDK session's business.
    """
    async with src, dst:
        async for message in src:
            if isinstance(message, Exception):
                await dst.send(message)
                continue
            request = message.message.root
            if not isinstance(request, types.JSONRPCRequest):
                # A notification or a response: nothing to answer and nothing to record.
                await dst.send(message)
                continue
            if hooks.activity is not None:
                hooks.activity()
            if request.method not in ALLOWED_METHODS:
                await _write_error(
                    out,
                    request.id,
                    types.ErrorData(
                        code=types.METHOD_NOT_FOUND,
                        message=METHOD_NOT_FOUND_MESSAGE.format(method=request.method),
                    ),
                )
                continue
            if request.method == INITIALIZE and hooks.initialize is not None:
                try:
                    await hooks.initialize(request)
                except InitializeFailed as failed:
                    await _write_error(
                        out,
                        request.id,
                        types.ErrorData(code=types.INTERNAL_ERROR, message=failed.message),
                    )
                    continue
            await dst.send(message)


async def _write_error(
    out: MemoryObjectSendStream[SessionMessage],
    request_id: types.RequestId,
    error: types.ErrorData,
) -> None:
    """Answer the request ``request_id`` with ``error``, straight to the client.

    The response carries the id of the request it answers, which is what both transports
    route a response by: the Streamable HTTP transport matches it against the POST that is
    waiting, and the stdio transport writes it to stdout. ``data`` of the error is None and
    the SDK leaves it out, so the error object a client receives carries ``code`` and
    ``message`` alone (V2 of tests/probes/test_sdk_server.py).
    """
    answer = types.JSONRPCError(jsonrpc=_JSONRPC_VERSION, id=request_id, error=error)
    await out.send(SessionMessage(message=types.JSONRPCMessage(answer)))
