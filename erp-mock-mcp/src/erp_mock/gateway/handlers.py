"""What one MCP session answers: tools/list, tools/call and ping (design.md 7.1).

Everything a client can ask of this service after initialize is answered here. The
transports (tasks 22.3 and 22.4) own the connection, the session id and the sandbox a
session is bound to; this module owns the three methods of requirement 14.10 that a
session may call, and nothing else::

    handlers = SessionHandlers(manager, layer)
    session = McpSessionContext.of(session_id, sandbox)
    server = handlers.build_server(session)
    await server.run(read_stream, write_stream, INIT_OPTIONS)

One :class:`~mcp.server.lowlevel.Server` per session, its handlers closures over the
:class:`McpSessionContext` of that session (design.md 7.1): the session id, the sandbox it
is bound to and the profile whose tools it sees. The handlers therefore never look a
session up from the SDK's request context, and two sessions of one sandbox - which
requirement 15.3 allows - keep their own ids while sharing the sandbox and its lock.

What the SDK does and what this module does. The three handlers are raw entries of
``Server.request_handlers``, keyed by the request model class, and not the
``@server.call_tool()`` decorator: the decorator validates arguments against inputSchema,
which rejects the decimal strings requirement 12.5 accepts, turns an exception into a text
result and validates structuredContent against outputSchema. A raw handler returns the
``types.ServerResult`` the SDK sends unchanged, so the shape of a tool result stays this
service's decision (requirements 13.1 and 13.6). What is left to the SDK is the
serialization of a result and of an :class:`~mcp.shared.exceptions.McpError`.

Both of those are properties of one locked SDK version, and both were probed before this
module was written (tests/probes/test_sdk_server.py, on mcp 1.30.0 with pydantic 2.14.0
and anyio 4.15.1):

- V1 holds: a raw handler's ``ServerResult`` reaches the client as it was built, and an
  ``McpError`` becomes a JSON-RPC error of exactly its code and message.
- V2 holds: the SDK serializes a result with ``exclude_none=True``, which drops only model
  fields whose value is None - ``structuredContent`` of an error result, ``nextCursor`` of
  a tools/list result - and keeps the None values *inside* structuredContent as ``null``
  and every object's key order as it was built (requirements 13.1 and 13.4).
- V4 holds: the requests of one session reach their handlers in arrival order, so the
  sandbox lock they then queue on is taken in that order (requirement 15.4) and no
  sequence number has to be handed out anywhere else.

Should a later SDK version break V1 or V2, the fallback of task 22.1 is to move the
handling of tools/list and tools/call, or the serialization of their responses, into the
MessageFilter (:mod:`erp_mock.gateway.message_filter`), which sits between the transport
and the SDK session and can write a response itself; a broken V4 would move the reading of
``next_seq`` there as well. The probe module fails first in that case, which is where the
decision is recorded.

:data:`INIT_OPTIONS` is built explicitly rather than with
``Server.create_initialization_options()``, which fills in ``experimental: {}`` and would
break "capabilities carries tools alone" (requirement 14.4). The same options serve every
session of the process: the instructions are one fixed sentence about the service and
carry nothing of the session, the profile, the dataset or the anchor date, and name no tool
and no rule (requirement 14.4).

What reaches the trace, and when (requirement 17.9):

=============  ==========================  =============================================
request        event                       where
=============  ==========================  =============================================
initialize     ``session_start``           the transport that accepted it (7.2, 7.3)
tools/list     ``tools_list``              here, inside the sandbox lock
tools/call     ``tools_call``              here, inside the sandbox lock
ping           none                        -
=============  ==========================  =============================================

A ping needs nothing of the sandbox: it writes no event, takes no lock and answers an
empty result. It does keep the sandbox from turning idle (requirement 15.5), which the
MessageFilter records for every request that arrives, the ping included (design.md 7.1).

The steps of one tools/call, in order:

1. the handler reads the monotonic clock once. That one reading is both the start of
   ``latency_ms`` and the arrival time the call rate of the session is measured against
   (requirement 23.12), so the two cannot disagree about when the call arrived;
2. the tool name is looked up in the profile of the session. An unknown name writes its
   event and raises - see below - and reaches neither the resource limits nor the sandbox
   database (requirement 14.6);
3. :meth:`~erp_mock.gateway.sandbox.SandboxManager.run` takes the sandbox lock and runs
   the rest in the shared thread pool, which is what makes the following three steps one
   unit (requirement 15.4);
4. ``next_seq`` of the trace is read. It is the ``seq`` of the event this call will write
   and, as the ``call_seq`` of the call, the number the records it writes are recorded
   under inside the database (requirement 18.10); the lock is what keeps another event
   from taking that number in between (design.md 7.6);
5. :meth:`~erp_mock.tools.layer.ToolLayer.call` performs the call. It raises nothing: a
   business error, a resource limit and an unexpected exception all come back as a
   :class:`~erp_mock.tools.layer.ToolOutcome` (requirement 12.8);
6. the ``tools_call`` event is written, ``latency_ms`` read off the clock just before it
   (requirement 17.3), and the lock released. The event is in the file before the response
   is sent (requirement 17.6).

An unknown tool is step 2 and nothing else: the event, whose ``known_tool`` is false and
whose ``result`` is the JSON-RPC error object the client is about to get
(requirement 17.10), is written inside the sandbox lock - the trace of a sandbox has one
writer, whoever writes it - and the ``McpError`` is raised after it, so no response goes
out before its event (requirement 17.6). Nothing of the sandbox database is touched.

What this module does not catch:

- :class:`~erp_mock.gateway.trace.TraceWriteError`, a trace event that could not be
  written. The Sandbox_Manager has already turned that sandbox frozen by the time the
  exception arrives here (design.md error handling), and the transport answers that one
  request with JSON-RPC -32603: the router of task 22.3 for Streamable HTTP, the stdio
  runtime of task 22.4;
- :class:`~erp_mock.gateway.sandbox.SandboxFrozen`, a sandbox that was frozen before the
  request reached its lock. The router keeps such a request out by answering HTTP 404
  (requirement 14.7), which leaves the case to the transports as well.

Both are deliberate: a handler that answered them itself would have to invent a status
code for HTTP and a JSON-RPC error for stdio, which is the transports' decision.
"""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final, Self

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.shared.exceptions import McpError

from erp_mock import FRAMEWORK_VERSION
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.sandbox import SandboxManager, SandboxRuntime
from erp_mock.gateway.trace import (
    EventName,
    tools_call_body,
    unknown_tool_body,
    write_item,
)
from erp_mock.loader.instance import ProfileModel
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.layer import CallContext, ToolLayer, ToolOutcome

__all__ = [
    "INIT_OPTIONS",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "UNKNOWN_TOOL_MESSAGE",
    "McpSessionContext",
    "SessionHandlers",
]

# What the service calls itself in the initialize result, together with the framework
# version (design.md 7.1). The name is the same for every instance configuration: what a
# client is served depends on the address it connected to, not on the server name.
SERVER_NAME: Final = "erp-mock-mcp"

# The ``instructions`` of every initialize result (requirement 14.4): one fixed sentence,
# 55 Unicode characters, well below the 200 a client may count on. It says what the service
# is, that its data is synthetic and that writes of a session are visible to the calls
# after them, and it names no tool and no rule, so it cannot drift from the tool list.
SERVER_INSTRUCTIONS: Final = "ERP 模拟服务：通过工具检索基础资料并处理采购与库存单据。数据均为合成数据，同一会话内的写入对后续调用可见。"

# The capabilities and the text of every session of this process (requirement 14.4):
# ``tools`` alone, ``listChanged`` false - the tool list of a session cannot change while
# it runs - and nothing else. Built here rather than by
# ``Server.create_initialization_options()``, which adds ``experimental: {}``
# (module docstring).
INIT_OPTIONS: Final = InitializationOptions(
    server_name=SERVER_NAME,
    server_version=FRAMEWORK_VERSION,
    capabilities=types.ServerCapabilities(tools=types.ToolsCapability(listChanged=False)),
    instructions=SERVER_INSTRUCTIONS,
)

# The message of the -32602 a tools/call of an unknown tool is answered with: it names the
# tool the request asked for, so a client sees which name was not there (requirement 14.6).
UNKNOWN_TOOL_MESSAGE: Final = "未知工具：{tool}"

# The two keys of the JSON-RPC error object the trace event of an unknown tool carries, in
# the order the protocol writes them (requirement 17.10).
_CODE_KEY: Final = "code"
_MESSAGE_KEY: Final = "message"

# ``latency_ms`` is whole milliseconds and the clock counts seconds (requirement 17.3).
_MS_PER_SECOND: Final = 1000

# One raw handler of the low-level server: it takes the validated request model and gives
# back the result the SDK sends (``Server.request_handlers``, V1 of the probe).
type _RequestHandler[R] = Callable[[R], Coroutine[Any, Any, types.ServerResult]]


@dataclass(frozen=True, slots=True)
class McpSessionContext:
    """One MCP session, as its handlers see it (design.md 7.1).

    - ``session_id``: the ``Mcp-Session-Id`` the transport assigned, which every trace
      event of this session names (requirement 17.2);
    - ``sandbox``: the sandbox the session is bound to, whose lock its requests queue on
      and whose anchor date, database and trace they work with;
    - ``profile``: the tool profile the session sees, by the rules of requirement 14.2 -
      the one its address named, the sandbox's own for a session of a prebuilt sandbox;
    - ``tools``: the exposed tools by name, which is what a tools/call is looked up in
      (:meth:`tool`); built once per session by :meth:`of`, since a profile exposes a few
      dozen tools and the list cannot change while the session runs.

    Everything here is fixed for the life of the session, which is why the handlers hold
    the context in a closure rather than reading it back per request.
    """

    session_id: str
    sandbox: SandboxRuntime
    profile: ProfileModel
    tools: FrozenMap[str, ToolSpec]

    @classmethod
    def of(
        cls, session_id: str, sandbox: SandboxRuntime, profile: ProfileModel | None = None
    ) -> Self:
        """The context of the session ``session_id`` on ``sandbox``.

        ``profile`` is the profile of the session; None takes the one the sandbox was
        created with, which is what a session of a prebuilt sandbox gets
        (requirement 14.2). The two are the same profile for every session of M0: a
        sandbox of its own is created with the profile its initialize named.
        """
        exposed = sandbox.profile if profile is None else profile
        return cls(
            session_id=session_id,
            sandbox=sandbox,
            profile=exposed,
            tools=FrozenMap((spec.name, spec) for spec in exposed.tools),
        )

    def tool(self, name: str) -> ToolSpec | None:
        """The exposed tool ``name``, None where the profile has no such tool.

        None is what makes a tools/call an unknown tool: the name is compared exactly, so
        a tool the instance generates but this profile excludes is unknown here
        (requirements 14.6 and 17.10).
        """
        return self.tools.get(name)


class SessionHandlers:
    """Builds the low-level ``Server`` of a session and performs its requests.

    One per service process, shared by every session: what belongs to a session is the
    :class:`McpSessionContext` a handler is built with, and what belongs to the process is
    held here.

    - ``manager``: the Sandbox_Manager, through which every request of a sandbox runs one
      at a time (requirement 15.4) and whose clock ``latency_ms`` and the arrival time of
      a call are read from;
    - ``layer``: the tools layer of the instance configuration, which performs a call
      (design.md 5.3).
    """

    __slots__ = ("_layer", "_manager")

    def __init__(self, manager: SandboxManager, layer: ToolLayer) -> None:
        self._manager = manager
        self._layer = layer

    @property
    def manager(self) -> SandboxManager:
        """The Sandbox_Manager every request of a session is run through."""
        return self._manager

    @property
    def layer(self) -> ToolLayer:
        """The tools layer a tools/call is performed by (design.md 5.3)."""
        return self._layer

    @property
    def clock(self) -> SystemClock:
        """The one clock of the service, read for ``latency_ms`` and the arrival time.

        The Sandbox_Manager's own clock, so the timing of a call and the idle, freeze and
        retention timing of its sandbox cannot be measured against two different clocks
        (requirement 24.7).
        """
        return self._manager.clock

    def build_server(self, session: McpSessionContext) -> Server[Any, Any]:
        """The ``Server`` that answers the requests of ``session`` (design.md 7.1).

        One server per session, with the three methods of requirement 14.10 registered as
        raw handlers (module docstring); every other method is answered by the
        MessageFilter before it reaches this server, so nothing is registered for it. The
        server is run by the transport with :data:`INIT_OPTIONS`, which this module owns
        rather than ``Server.get_capabilities()``.
        """
        server: Server[Any, Any] = Server(SERVER_NAME)
        server.request_handlers[types.ListToolsRequest] = self._list_tools(session)
        server.request_handlers[types.CallToolRequest] = self._call_tool(session)
        # A ping needs nothing of the session (module docstring); the raw handler replaces
        # the one the SDK registers by default, so the handlers of a session are all here.
        server.request_handlers[types.PingRequest] = self._ping
        return server

    # --- tools/list (requirements 14.5 and 17.9) ---------------------------------------

    def _list_tools(self, session: McpSessionContext) -> _RequestHandler[types.ListToolsRequest]:
        """The tools/list handler of ``session``."""

        async def list_tools(request: types.ListToolsRequest) -> types.ServerResult:
            # The event first, inside the sandbox lock, so it is in the trace file before
            # the response goes out (requirements 17.6 and 17.9).
            await self._manager.run(session.sandbox, self._write_list_event(session))
            # Every tool of the profile in one response, in the order of requirement 11.9
            # and with no cursor to continue from: the payload was assembled and frozen at
            # load time, and ``nextCursor`` is None and left out (requirement 14.5).
            return types.ServerResult(session.profile.tools_list_result())

        return list_tools

    def _write_list_event(self, session: McpSessionContext) -> Callable[[SandboxRuntime], int]:
        """The job that writes the ``tools_list`` event of ``session`` (worker thread)."""

        def job(sb: SandboxRuntime) -> int:
            return sb.trace.write(EventName.TOOLS_LIST, session.session_id)

        return job

    # --- tools/call (requirements 14.6, 17.3, 17.5, 17.10, 17.11) ----------------------

    def _call_tool(self, session: McpSessionContext) -> _RequestHandler[types.CallToolRequest]:
        """The tools/call handler of ``session`` (the steps of the module docstring)."""

        async def call_tool(request: types.CallToolRequest) -> types.ServerResult:
            # One reading for both the latency of the call and the arrival time its rate
            # window is measured against (requirement 23.12).
            arrival = self.clock.monotonic()
            name = request.params.name
            raw_args = request.params.arguments
            spec = session.tool(name)
            if spec is None:
                error = types.ErrorData(
                    code=types.INVALID_PARAMS, message=UNKNOWN_TOOL_MESSAGE.format(tool=name)
                )
                await self._manager.run(
                    session.sandbox, self._write_unknown(session, name, raw_args, error, arrival)
                )
                # After the event, so no client sees the error before its event is in the
                # file (requirements 14.6, 17.6 and 17.10).
                raise McpError(error)
            outcome = await self._manager.run(
                session.sandbox, self._perform(session, spec, raw_args, arrival)
            )
            return types.ServerResult(_call_result(outcome))

        return call_tool

    def _perform(
        self,
        session: McpSessionContext,
        spec: ToolSpec,
        raw_args: dict[str, Any] | None,
        arrival: float,
    ) -> Callable[[SandboxRuntime], ToolOutcome]:
        """The job of one tools/call: the call and its event, in that order (worker thread).

        Runs under the sandbox lock, which is what lets the ``seq`` of the event be read
        before the call and used after it (design.md 7.6): no other event of this sandbox
        can be written in between.
        """

        def job(sb: SandboxRuntime) -> ToolOutcome:
            seq = sb.trace.next_seq
            ctx = CallContext(
                session_id=session.session_id,
                anchor_date=sb.anchor_date,
                # The current user of the sandbox's initial dataset, which loading step 8
                # resolved from the dataset and the instance configuration
                # (requirement 20.5); None where the configuration names none.
                current_user_id=sb.dataset.current_user,
                call_seq=seq,
                limits=sb.probe(session.session_id, arrival),
            )
            outcome = self._layer.call(sb.store, ctx, spec, raw_args)
            sb.trace.write(
                EventName.TOOLS_CALL,
                session.session_id,
                tools_call_body(
                    tool=spec.name,
                    args=raw_args,
                    # The structuredContent of a successful call, the error object of a
                    # failed one (requirement 17.3).
                    result=outcome.error if outcome.is_error else outcome.structured,
                    is_error=outcome.is_error,
                    latency_ms=self._elapsed_ms(arrival),
                    coerced=outcome.coerced,
                    error_code=outcome.error_code,
                    rule_id=outcome.rule_id,
                    writes=[
                        write_item(entity=write.entity, op=write.op, id=write.id, cause=write.cause)
                        for write in outcome.writes
                    ],
                ),
            )
            return outcome

        return job

    def _write_unknown(
        self,
        session: McpSessionContext,
        name: str,
        raw_args: dict[str, Any] | None,
        error: types.ErrorData,
        arrival: float,
    ) -> Callable[[SandboxRuntime], int]:
        """The job that writes the event of an unknown tool name (requirement 17.10).

        ``error`` is the JSON-RPC error the client is about to get, and its ``code`` and
        ``message`` are what the event reports as the result of the call; the sandbox
        database is not touched at all.
        """

        def job(sb: SandboxRuntime) -> int:
            result: dict[str, JsonValue] = {
                _CODE_KEY: error.code,
                _MESSAGE_KEY: error.message,
            }
            return sb.trace.write(
                EventName.TOOLS_CALL,
                session.session_id,
                unknown_tool_body(
                    tool=name,
                    args=raw_args,
                    result=result,
                    latency_ms=self._elapsed_ms(arrival),
                ),
            )

        return job

    # --- ping (requirements 14.10 and 17.9) --------------------------------------------

    async def _ping(self, request: types.PingRequest) -> types.ServerResult:
        """Answer a ping with an empty result; no sandbox, no lock, no event."""
        return types.ServerResult(types.EmptyResult())

    def _elapsed_ms(self, arrival: float) -> int:
        """Whole milliseconds from ``arrival`` to now, by the monotonic clock (17.3).

        Read just before the event is written, so the value covers everything the service
        did for the request, the queueing on the sandbox lock included. The monotonic clock
        never goes back, so the difference is not negative, and the fraction is dropped
        rather than rounded up: a call reports the milliseconds that have fully passed.
        """
        return int((self.clock.monotonic() - arrival) * _MS_PER_SECOND)


def _call_result(outcome: ToolOutcome) -> types.CallToolResult:
    """``outcome`` as the tool result the client receives (requirements 13.1 and 13.6).

    One text item either way, the compact JSON of the structuredContent of a successful
    call or of the ``{"error": ...}`` of a failed one, built by the tools layer. A failed
    call carries no structuredContent: the field is None here, and the SDK leaves a None
    model field out of the message it sends (V2 of the probe).
    """
    content: list[types.ContentBlock] = [types.TextContent(type="text", text=outcome.text)]
    return types.CallToolResult(
        content=content,
        structuredContent=outcome.structured,
        isError=outcome.is_error,
    )
