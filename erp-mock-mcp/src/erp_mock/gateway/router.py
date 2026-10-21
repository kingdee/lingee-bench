"""Which sandbox an HTTP request reaches, and what is refused before it (design.md 7.2).

The MCP port of the serve command is a chain of pure ASGI applications, and this is its
last link: the middlewares of task 23.1 check the Origin, the endpoint token and the size
of the body, and what passes them reaches :class:`McpRouter`, which decides what a request
means. Everything about an HTTP session that is not MCP itself is decided here - the
address it connected to, the session id it carries, the sandbox it is bound to, whether it
may be opened at all - and everything that is MCP is handed to the session's own
:class:`~mcp.server.streamable_http.StreamableHTTPServerTransport`::

    router = McpRouter(handlers, loaded)
    async with anyio.create_task_group() as tg:        # the serve task group (task 23.4)
        router.attach(tg)
        ...                                            # run the two uvicorn servers
        await router.shutdown()

The SDK's ``StreamableHTTPSessionManager`` is not used, and the reasons are structural: it
generates session ids of its own, ignores the request path - so it cannot bind a session to
the address its initialize arrived on (requirement 14.7) - answers unknown sessions with
its own status codes, and creates a server instance for any request that carries no session
id, which would create a sandbox for a request this service refuses. One transport per
session, with this module's own id, JSON responses, no event store and the SDK's DNS
rebinding protection off (the Origin is the middleware's business, requirement 23.1), is
what is left of it (design.md 7.2).

The five addresses of requirement 14.2, matched literally and case-sensitively, each path
segment compared as a whole (:meth:`McpRouter._route`):

================================  =========  ===================  ===================
address                           sandbox    profile              initial dataset
================================  =========  ===================  ===================
``/mcp``                          a new one  ``default_profile``  ``default_dataset``
``/p/<profile>/mcp``              a new one  the path's           ``default_dataset``
``/d/<dataset>/mcp``              a new one  ``default_profile``  the path's
``/p/<profile>/d/<dataset>/mcp``  a new one  the path's           the path's
``/s/<sandbox_id>/mcp``           the named  the sandbox's own    the sandbox's own
================================  =========  ===================  ===================

Every other path, a profile or a dataset the instance configuration does not have, and a
``sandbox_id`` that is not a prebuilt sandbox of this process that is not frozen, are
answered with 404 and create neither a session nor a sandbox (requirement 14.3).

The order the checks run in is requirement 23.11's, and only the first one that fails is
answered (design.md error handling). The three checks before these are the middlewares';
what is left is this module's:

====  =========================================================  ============  ============
step  what                                                       status        requirement
====  =========================================================  ============  ============
0     the sessions of a sandbox that is gone are ended           -             14.7
1     the address: one of the five, with names that exist        404           14.3
2     the session: known, not ended, assigned on this path       404           14.7
3     no session id: a POST whose body is one initialize         400           14.8
3a    the headers the transport would refuse anyway              400, 406,     15.1
      (the preflight)                                            415
4     a new sandbox: the concurrent sandboxes are all taken      503           15.10, 19.18
5     a new sandbox: its Snapshot could not be built             500           15.14
====  =========================================================  ============  ============

A request that reaches step 5 is the only one that is answered with a JSON-RPC error of
this service's own making (-32603); every other answer above carries the JSON-RPC envelope
the transport uses for its own HTTP-level refusals, so a client sees one shape of answer
whether the router or the transport refused its request. None of them writes a trace event
(requirement 17.9).

Two probe findings of the design's open items decide what this module does, both on
mcp 1.30.0 with starlette 1.7.0 and anyio 4.15.1:

- V3 holds (tests/probes/test_sdk_http_transport.py): a bare transport answers a DELETE
  that carries its own session id with 200 and terminates itself, which ends the session
  task without anybody cancelling it. The router therefore writes no DELETE response of
  its own: it does what requirement 15.7 asks of the end of a session and hands the DELETE
  to the transport. Were V3 to break, the fallback of task 22.3 is to answer the DELETE
  here and call ``transport.terminate()`` directly.
- V5 holds (tests/probes/test_sdk_client.py): the official client tolerates a 405 for the
  GET it opens a server-initiated stream with. GET is therefore answered with 405 and
  never reaches the transport, which would open an idle SSE stream even in JSON mode; this
  service initiates no request and no notification, so such a stream would carry nothing
  (requirement 14.11). Were V5 to break, the fallback is to hand GET to the transport.

The same probe records the transport's own POST checks, and the preflight of step 3a is
the first three of them, in their order: a request without a session id is refused here
exactly where the transport would have refused it, so no sandbox is created for a request
that would never have reached a session. The body is checked before those headers - the
reverse of the transport's order - which only changes which status a request with two
faults gets, and such a request never reaches a transport. The three messages are the
transport's own strings rather than this project's Chinese ones, so that one condition has
one answer wherever it was detected; a later SDK version that changes them makes the probe
fail first.

What one session is made of. :class:`SessionRecord` is the registry entry and
:class:`SessionRegistry` the registry; the session task of a record runs
``transport.connect()``, the MessageFilter and ``Server.run`` and belongs to the task group
:meth:`McpRouter.attach` was given, which is the serve task group (design.md 7.2). The
session id is ``secrets.token_hex(16)`` (requirement 24.6), bound to the path its
initialize arrived on and compared literally on every request after it: the same sandbox
reached through ``/s/<sandbox_id>/mcp`` and through the address that created it are two
different sessions to a client, and requirement 14.7 keeps one id from working on both.

How a session ends, and what that does to its sandbox:

=========================  ================================  =======================  ====
what happened              the sandbox                       the sessions             req
=========================  ================================  =======================  ====
DELETE, sandbox ``auto``   ``session_end``, then frozen      this one ends            15.7
DELETE, sandbox prebuilt   ``session_end``, nothing else     this one ends            15.8
frozen or deleted          already done by the manager       all of them end          14.7
the service stops          left as it is on disk             all of them end          22.1
=========================  ================================  =======================  ====

A session that ends has its transport terminated and its task cancelled, in that order -
termination alone already ends the task, the cancellation being the backstop - and its id
answers 404 from then on. :meth:`McpRouter.terminate_sandbox` is that for every session of
one sandbox and is the entry point the Admin API calls after it froze or deleted one
(task 23.2); :meth:`McpRouter.shutdown` is that for every session of the process
(task 23.4). The janitor of :class:`~erp_mock.gateway.sandbox.SandboxManager` freezes and
deletes a sandbox without telling anybody, so step 0 of the table above does the same for
whatever the router finds without a sandbox at the start of a request (requirement 14.7).

What this module leaves to others, deliberately: the middlewares of task 23.1 (Origin,
token, body size), the Admin API of tasks 23.2 and 23.3, the two uvicorn servers and the
``GatewayState`` that holds them of task 23.4, and the three MCP methods of
:mod:`erp_mock.gateway.handlers`. What it does not know, structurally: the seed layer. A
Snapshot reaches a new sandbox through the
:class:`~erp_mock.gateway.sandbox.SnapshotSource` the Sandbox_Manager was built with, so
the router cannot name the exception a failed build raises and answers 500 with the message
of requirement 15.14 for every failure of a creation that is not the concurrency limit.

Two deviations worth naming, both recorded rather than hidden:

- a second ``initialize`` inside an established session is handed to the SDK, which accepts
  it, and writes no ``session_start``: design.md 7.2 gives the ``initialize`` hook of the
  MessageFilter to stdio alone, and parsing the body of every POST to find the repeated
  ones would undo what probe finding V6 bought (one parse of one body per request). A
  client that sends one initialize per session - every MCP client does - cannot tell.
- :func:`message_filter` is wrapped by :func:`_filter_messages` here, which treats the
  closed streams of a terminated transport as the end of the session. The filter of
  task 22.2 lets ``anyio.ClosedResourceError`` through, and after a DELETE the transport
  closes both ends of its streams while the filter may still be reading or writing; the
  session is over either way, and the wrapper is what keeps that from reaching the serve
  task group.
"""

import json
import logging
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from http import HTTPStatus
from typing import Final

import anyio
from anyio.abc import TaskGroup, TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.message import SessionMessage
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from erp_mock.common.jsonio import JsonValue, dumps_compact, encode_utf8
from erp_mock.gateway.clock import resolve_anchor
from erp_mock.gateway.handlers import INIT_OPTIONS, McpSessionContext, SessionHandlers
from erp_mock.gateway.message_filter import INITIALIZE, FilterHooks, message_filter
from erp_mock.gateway.sandbox import (
    SandboxFrozen,
    SandboxLimitReached,
    SandboxManager,
    SandboxRuntime,
)
from erp_mock.gateway.trace import EventName, TraceWriteError
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel

__all__ = [
    "ALLOWED_HTTP_METHODS",
    "INVALID_CONTENT_TYPE",
    "NOT_ACCEPTABLE_MESSAGE",
    "SESSION_ID_BYTES",
    "SESSION_ID_HEADER",
    "SNAPSHOT_FAILED_MESSAGE",
    "UNSUPPORTED_MEDIA_TYPE_MESSAGE",
    "McpRouter",
    "Segment",
    "SessionRecord",
    "SessionRegistry",
    "sandbox_path",
]

# Where a failure this module does not answer with goes: the service log on stderr.
_LOG: Final = logging.getLogger(__name__)


class Segment(StrEnum):
    """The path segments the five addresses of requirement 14.2 are built from.

    ``ENDPOINT`` is what every one of them ends with and the other three are what
    introduces a name: ``/p/<profile>/mcp``, ``/d/<dataset>/mcp``,
    ``/s/<sandbox_id>/mcp``. A StrEnum member is a str and compares equal to it, so these
    are both the patterns :meth:`McpRouter._route` matches a path against and the text
    :func:`sandbox_path` writes an address with.
    """

    ENDPOINT = "mcp"
    PROFILE = "p"
    DATASET = "d"
    SANDBOX = "s"


def sandbox_path(sandbox_id: str) -> str:
    """The address of the prebuilt sandbox ``sandbox_id`` (requirement 14.2).

    ``/s/<sandbox_id>/mcp``, the one address that reaches an existing sandbox. The Admin
    API reports it as the ``mcp_url`` of a sandbox it created (requirement 19.3, task
    23.2) and it is written here, next to the matching it has to agree with.
    """
    return f"/{Segment.SANDBOX}/{sandbox_id}/{Segment.ENDPOINT}"


# The header a client carries its session in, lower case as ASGI delivers header names.
SESSION_ID_HEADER: Final = "mcp-session-id"

# How many random bytes a session id is drawn from (requirement 24.6): 16, written as 32
# hexadecimal digits, which is what the SDK's ``^[\x21-\x7E]+$`` asks of a session id and
# what a URL, a log line and a trace event carry unescaped.
SESSION_ID_BYTES: Final = 16

# The HTTP methods this endpoint serves (requirement 14.11): a POST carries a JSON-RPC
# message and a DELETE ends the session. Everything else - GET above all, which the SDK
# would answer with a server-initiated SSE stream - is answered with 405 and this header.
ALLOWED_HTTP_METHODS: Final = "POST, DELETE"
_POST: Final = "POST"
_DELETE: Final = "DELETE"

# The media type of every message of this transport, and the two headers it is read from.
_JSON_MEDIA_TYPE: Final = "application/json"
_CONTENT_TYPE_HEADER: Final = "content-type"
_ACCEPT_HEADER: Final = "accept"
_ALLOW_HEADER: Final = "Allow"
_CONTENT_TYPE: Final = "Content-Type"

# The ``jsonrpc`` of every message of the protocol and the ``id`` the SDK's transport puts
# on an answer to a request it did not read the id of; the router writes the id of the
# request it is answering where it has parsed one (the initialize of a session that is
# being opened) and this where it has not.
_JSONRPC_VERSION: Final = "2.0"
_SERVER_ERROR_ID: Final = "server-error"

# The three answers of the preflight, which are the transport's own (probe V3,
# tests/probes/test_sdk_http_transport.py): the same condition gets the same answer whether
# the router refused the request before the session existed or the transport refused it
# inside one. The first is a plain-text body with no Content-Type header, as
# ``TransportSecurityMiddleware`` writes it; the other two are JSON-RPC errors.
INVALID_CONTENT_TYPE: Final = "Invalid Content-Type header"
NOT_ACCEPTABLE_MESSAGE: Final = "Not Acceptable: Client must accept application/json"
UNSUPPORTED_MEDIA_TYPE_MESSAGE: Final = (
    "Unsupported Media Type: Content-Type must be application/json"
)

# What a request this module refuses is told, one message per reason. Every one of them
# names what was asked for, so a client sees which part of its request was refused; none of
# them names a token, a header value or an exception (design.md error handling).
_UNKNOWN_ADDRESS: Final = "路径 {path} 不是本服务的 MCP 端点"
_UNKNOWN_PROFILE: Final = "实例配置中没有名为 {name} 的 profile"
_UNKNOWN_DATASET: Final = "实例配置中没有名为 {name} 的初始数据集"
_UNKNOWN_SANDBOX: Final = "{sandbox_id} 不是经 Admin API 预建且未冻结的现存沙箱"
_UNKNOWN_SESSION: Final = "会话 {session_id} 不存在或已终止"
_WRONG_PATH: Final = "会话 {session_id} 由另一路径上的 initialize 分配，不能用于 {path}"
_MISSING_SESSION: Final = "initialize 以外的请求必须携带 Mcp-Session-Id 头"
_NOT_INITIALIZE: Final = "没有 Mcp-Session-Id 的请求体必须是一个 initialize 请求"
_METHOD_NOT_ALLOWED: Final = "MCP 端点只接受 POST 与 DELETE 方法"

# The message of the -32603 an initialize whose Snapshot could not be built is answered
# with (requirement 15.14): it names the initial dataset and the anchor date that were
# tried, which is what tells a deployer which build to look at.
SNAPSHOT_FAILED_MESSAGE: Final = "初始数据集 {dataset} 在锚点日期 {anchor} 的快照构建失败"

# The message of the -32603 an initialize whose ``session_start`` could not be written is
# answered with (design.md error handling): the Sandbox_Manager has turned the sandbox
# frozen by then, so the session is not opened at all.
_TRACE_FAILED: Final = "沙箱 {sandbox_id} 的 trace 无法写入，该沙箱已冻结"

# What a failure that is logged rather than answered says.
_SESSION_END_FAILED: Final = "会话 %s 的 session_end 未能写入，会话照常终止：%s"
_CREATE_FAILED: Final = "初始数据集 %s 在锚点日期 %s 的沙箱创建失败：%s"

# What a transport that is asked to open a session before :meth:`McpRouter.attach` says;
# a service that serves a request before it has a task group is a mistake in its assembly
# (task 23.4), not something a client can cause.
_NO_TASK_GROUP: Final = "McpRouter 还没有接到会话任务组，无法建立会话"

# The transport's own DNS rebinding protection is off: the Origin of a request is checked
# by the middleware of task 23.1, by the rules of requirement 23.1, before the request
# reaches this module. ``None`` would disable it as well, and this says so (probe V3).
_NO_DNS_REBINDING: Final = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# What a WebSocket upgrade of the MCP port is answered with: the port serves HTTP alone.
_WS_CLOSE_CODE: Final = 1000

# One message of the stream between the filter and the SDK session, which carries either a
# message a client sent or the exception a body that could not be parsed became.
type _Incoming = SessionMessage | Exception


# --- what an address names (requirements 14.2 and 14.3) ----------------------------------


@dataclass(frozen=True, slots=True)
class _AutoRoute:
    """One of the four addresses whose initialize creates a sandbox (requirement 15.1).

    ``path`` is the address as it arrived, which the session is bound to
    (requirement 14.7), and ``profile`` and ``dataset`` what the address named or what the
    instance configuration defaults to (requirement 14.2).
    """

    path: str
    profile: ProfileModel
    dataset: DatasetModel


@dataclass(frozen=True, slots=True)
class _PrebuiltRoute:
    """``/s/<sandbox_id>/mcp``: the address of one prebuilt sandbox (requirement 15.3).

    ``sandbox`` is that sandbox, which an initialize binds itself to without copying a
    Snapshot; its own profile and initial dataset are what the session sees
    (requirement 14.2), which :meth:`~erp_mock.gateway.handlers.McpSessionContext.of`
    takes from the sandbox when it is given no profile.
    """

    path: str
    sandbox: SandboxRuntime


# What :meth:`McpRouter._route` makes of a path that is one of the five addresses.
type _Route = _AutoRoute | _PrebuiltRoute


class _Refused(Exception):
    """A request the router answers itself, carrying the response to send.

    Raised by every check of this module and caught in :meth:`McpRouter.__call__`, which
    sends ``response`` and returns: a refused request reaches no transport, no sandbox and
    no trace (requirement 17.9). Private on purpose - it never leaves this module - and
    the reason it exists is that the checks of requirement 23.11 are a sequence of guards
    whose first failure is the answer.
    """

    __slots__ = ("response",)

    response: Response

    def __init__(self, response: Response) -> None:
        super().__init__(f"{response.status_code}")
        self.response = response


# --- one HTTP session (design.md 7.2) ----------------------------------------------------


@dataclass(slots=True)
class SessionRecord:
    """One Streamable HTTP session of this process, as the registry holds it.

    - ``session_id``: the ``Mcp-Session-Id`` this service assigned, which the transport
      announces in the initialize result and every trace event of the session names
      (requirements 17.2 and 24.6);
    - ``path``: the address its initialize arrived on, compared literally on every request
      after it (requirement 14.7);
    - ``sandbox``: the sandbox the session is bound to - the one its initialize created, or
      the prebuilt one its address named (requirements 15.1 and 15.3);
    - ``transport``: the SDK transport of this session, one per session, with
      ``session_id`` as its ``mcp_session_id``;
    - ``task_scope``: the cancel scope of the session task, entered by the task itself and
      cancelled when the session ends. design.md 7.2 calls this field ``scope``; it is
      named ``task_scope`` here because ``scope`` is the ASGI scope everywhere else in this
      module;
    - ``terminated``: whether the session has ended. A record is dropped from the registry
      when it ends, and the flag is what keeps a request that was already on its way from
      reaching a transport that is going away (requirement 14.7).
    """

    session_id: str
    path: str
    sandbox: SandboxRuntime
    transport: StreamableHTTPServerTransport
    task_scope: anyio.CancelScope
    terminated: bool = False


class SessionRegistry:
    """The HTTP sessions of one service process, by session id (design.md 7.2).

    One per :class:`McpRouter`. It holds records and hands out ids, and it decides nothing:
    which requests a session may serve is requirement 14.7, read off a record by the
    router. Everything here runs on the event loop between two ``await``s, so no lock of
    its own is needed - the same reason the Sandbox_Manager's registry needs none.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: dict[str, SessionRecord] = {}

    def __len__(self) -> int:
        """How many sessions are open."""
        return len(self._records)

    def get(self, session_id: str) -> SessionRecord | None:
        """The session ``session_id``, or None where this process has none of that id.

        None is what a request whose session never existed, or ended, gets, and the router
        answers it with 404 (requirement 14.7).
        """
        return self._records.get(session_id)

    def all(self) -> tuple[SessionRecord, ...]:
        """Every open session, in the order the sessions were opened."""
        return tuple(self._records.values())

    def of_sandbox(self, sandbox_id: str) -> tuple[SessionRecord, ...]:
        """The open sessions bound to the sandbox ``sandbox_id``, in opening order.

        A prebuilt sandbox may carry several (requirement 15.3), an automatically created
        one carries the one that created it: the four addresses that create a sandbox never
        name an existing one, and ``/s/<sandbox_id>/mcp`` reaches prebuilt sandboxes alone
        (requirement 14.3).
        """
        return tuple(
            record for record in self._records.values() if record.sandbox.sandbox_id == sandbox_id
        )

    def add(self, record: SessionRecord) -> None:
        """Put ``record`` in the registry, where requests of its id find it."""
        self._records[record.session_id] = record

    def discard(self, record: SessionRecord) -> None:
        """Mark ``record`` ended and take it out of the registry; doing it twice is safe.

        What a session that ends goes through first, whatever ended it: from here on its id
        is a session that does not exist (requirement 14.7).
        """
        record.terminated = True
        self._records.pop(record.session_id, None)

    def new_id(self) -> str:
        """A session id this registry does not use yet (requirement 24.6)."""
        while True:
            session_id = secrets.token_hex(SESSION_ID_BYTES)
            if session_id not in self._records:
                return session_id


# --- the router --------------------------------------------------------------------------


class McpRouter:
    """The MCP endpoint of the serve command: a pure ASGI application (design.md 7.2).

    - ``handlers``: the :class:`~erp_mock.gateway.handlers.SessionHandlers` of the process,
      which builds the server of a session and carries the Sandbox_Manager and the clock
      every sandbox of it is timed by;
    - ``loaded``: the validated instance configuration, which says whether the profile and
      the initial dataset an address names exist and what the defaults are
      (requirement 14.2), and whose ``clock`` an anchor date is resolved against
      (requirement 16.1).

    The session tasks run in the task group :meth:`attach` is given, which is the serve
    task group (design.md 7.2): a session outlives the request that opened it, and a
    service that stops cancels every session with the group it stops.

    Not a Starlette router: the five addresses are matched here (:meth:`route`) so that a
    path that is not one of them is a 404 of this module rather than of a framework, and so
    that nothing wraps the response stream of a transport (design.md 7.4).
    """

    __slots__ = ("_handlers", "_loaded", "_sessions", "_tasks")

    def __init__(self, handlers: SessionHandlers, loaded: LoadedInstance) -> None:
        self._handlers = handlers
        self._loaded = loaded
        self._sessions = SessionRegistry()
        self._tasks: TaskGroup | None = None

    @property
    def sessions(self) -> SessionRegistry:
        """The session registry of this router (design.md 7.2)."""
        return self._sessions

    @property
    def manager(self) -> SandboxManager:
        """The Sandbox_Manager every sandbox of a session comes from."""
        return self._handlers.manager

    def attach(self, tasks: TaskGroup) -> None:
        """Run the session tasks of this router in ``tasks`` (design.md 7.2).

        Called once, by the serve command, inside the task group that lives as long as the
        service does and before the first request is served (task 23.4). A session task
        runs ``transport.connect()``, the MessageFilter and ``Server.run`` of one session,
        and it must outlive the request that started it, which is why the group cannot be
        the request's.
        """
        self._tasks = tasks

    async def shutdown(self) -> None:
        """End every session of this router; the sandboxes are left as they are.

        What the serve command runs when it stops (requirement 22.1): every transport is
        terminated and every session task cancelled, so no session is left reading a stream
        of a server that is going away. No ``session_end`` is written and no sandbox is
        frozen - requirement 17.9 writes that event for a session that was ended by its
        client's DELETE or by the exit of a stdio process, and the sandboxes of a serve
        process stay on disk for the Admin API of the next one.
        """
        await self._end_sessions(self._sessions.all())

    async def terminate_sandbox(self, sandbox_id: str) -> None:
        """End every session bound to the sandbox ``sandbox_id`` (requirement 14.7).

        The entry point for whoever took a sandbox away from its sessions: the Admin API
        after a freeze or a delete (requirement 19.13, task 23.2), and
        :meth:`_end_session` after the DELETE of an automatically created sandbox's
        session. Each session's transport is terminated and its task cancelled, and its id
        answers 404 from then on; the sandbox itself is not touched, the caller having
        already done to it whatever it did.

        A sandbox the janitor froze on its own reaches no caller, which is why
        :meth:`_reap` does the same for every session of this router at the start of every
        request.
        """
        await self._end_sessions(self._sessions.of_sandbox(sandbox_id))

    async def _reap(self) -> None:
        """End the sessions whose sandbox is frozen or deleted (requirement 14.7).

        The first thing a request of this endpoint does, and the reason the checks after it
        need to know nothing about the state of a sandbox: a session whose sandbox was
        taken away is already gone by the time its id is looked up. Three of the ways a
        sandbox is taken away tell nobody - the idle timing that freezes it
        (requirement 15.7), the retention period that deletes it (requirement 15.9) and the
        freeze a failed trace write causes - and the Admin API, which does tell
        (:meth:`terminate_sandbox`), is not the only caller the invariant may rely on.

        One pass over the open sessions, which are a handful: two attribute reads and a
        dict lookup each, and nothing at all to do in the normal case.
        """
        dead = tuple(record for record in self._sessions.all() if not self._alive(record.sandbox))
        if dead:
            await self._end_sessions(dead)

    # --- the ASGI entry point ----------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one HTTP request of the MCP port (the steps of the module docstring).

        What the request is decided by, in order: the address (requirement 14.3), the
        session its ``Mcp-Session-Id`` names (requirement 14.7) or - where it carries none
        - whether it is an initialize this service will serve (requirements 14.8, 15.10,
        15.14). What passes is handed to the transport of its session, which is the only
        thing in this module that answers an MCP message.

        Before any of that, :meth:`_reap` clears away the sessions whose sandbox is gone,
        so that the checks after it read sessions and addresses alone. It answers nothing
        and therefore changes no status code, which is what requirement 23.11 fixes the
        order of.
        """
        if scope["type"] != "http":
            # The MCP port serves HTTP alone, and uvicorn runs with the lifespan protocol
            # off (design.md 7.4), so this is a WebSocket upgrade and nothing else.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": _WS_CLOSE_CODE})
            return
        try:
            await self._reap()
            route = self._route(str(scope["path"]))
            headers = Headers(scope=scope)
            session_id = headers.get(SESSION_ID_HEADER)
            method = str(scope["method"])
            if session_id is None:
                await self._open_session(scope, receive, send, route, method, headers)
            else:
                await self._in_session(scope, receive, send, route, method, session_id)
        except _Refused as refused:
            await refused.response(scope, receive, send)

    # --- the address (requirements 14.2 and 14.3) --------------------------------------

    def _route(self, path: str) -> _Route:
        """What ``path`` names, or raise :class:`_Refused` with a 404 (requirement 14.3).

        The five addresses of requirement 14.2, matched on the whole path split into
        segments, so nothing but one of them matches: a trailing slash, a prefix, an extra
        segment and a path of another shape are all 404. The names are compared
        case-sensitively, by looking them up in the instance configuration, and a
        ``sandbox_id`` must be a sandbox of this process that the Admin API prebuilt and
        that is not frozen - a frozen or deleted one is 404 as well, which is the same
        answer its sessions get (requirement 14.7).

        The shape of the one address that is written rather than read - the ``mcp_url`` of
        a prebuilt sandbox (requirement 19.3) - is :func:`sandbox_path`, next to this.
        """
        match tuple(path.split("/")):
            case ("", Segment.ENDPOINT):
                return self._auto(path, None, None)
            case ("", Segment.PROFILE, profile, Segment.ENDPOINT):
                return self._auto(path, profile, None)
            case ("", Segment.DATASET, dataset, Segment.ENDPOINT):
                return self._auto(path, None, dataset)
            case ("", Segment.PROFILE, profile, Segment.DATASET, dataset, Segment.ENDPOINT):
                return self._auto(path, profile, dataset)
            case ("", Segment.SANDBOX, sandbox_id, Segment.ENDPOINT):
                return self._prebuilt(path, sandbox_id)
            case _:
                raise _Refused(_not_found(_UNKNOWN_ADDRESS.format(path=path)))

    def _auto(self, path: str, profile: str | None, dataset: str | None) -> _AutoRoute:
        """The route of one of the four addresses whose initialize creates a sandbox.

        ``profile`` and ``dataset`` are the names the path carried, None where it carried
        none, in which case the ``default_profile`` and ``default_dataset`` of the instance
        configuration are taken (requirement 14.2). A name the configuration does not have
        is a 404 that names it (requirement 14.3).
        """
        return _AutoRoute(
            path=path,
            profile=self._profile(profile),
            dataset=self._dataset(dataset),
        )

    def _profile(self, name: str | None) -> ProfileModel:
        """The profile ``name``, the default one for None; 404 for a name that is not one."""
        if name is None:
            return self._loaded.default_profile
        found = self._loaded.profiles.get(name)
        if found is None:
            raise _Refused(_not_found(_UNKNOWN_PROFILE.format(name=name)))
        return found

    def _dataset(self, name: str | None) -> DatasetModel:
        """The initial dataset ``name``, the default for None; 404 for a name that is not one."""
        if name is None:
            return self._loaded.default_dataset
        found = self._loaded.datasets.get(name)
        if found is None:
            raise _Refused(_not_found(_UNKNOWN_DATASET.format(name=name)))
        return found

    def _prebuilt(self, path: str, sandbox_id: str) -> _PrebuiltRoute:
        """The route of ``/s/<sandbox_id>/mcp`` (requirements 14.3 and 15.3).

        The address reaches one kind of sandbox: one this process has, that the Admin API
        prebuilt (requirement 19.3) and that is not frozen. A sandbox of a session is
        therefore not reachable by its id, which is also what keeps a client that guesses
        an id from touching a session that is not its own.
        """
        sandbox = self.manager.get(sandbox_id)
        if sandbox is None or sandbox.origin != "prebuilt" or not self._alive(sandbox):
            raise _Refused(_not_found(_UNKNOWN_SANDBOX.format(sandbox_id=sandbox_id)))
        return _PrebuiltRoute(path=path, sandbox=sandbox)

    # --- a request of a session that exists (requirements 14.7, 15.7, 15.8) ------------

    async def _in_session(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        route: _Route,
        method: str,
        session_id: str,
    ) -> None:
        """Serve a request that carries ``session_id`` (requirement 14.7).

        The four reasons requirement 14.7 gives for a 404: the id is not a session of this
        process, the session has ended, it was assigned on another path - compared
        literally - or its sandbox is frozen or deleted. The last of them has already
        happened by the time this runs, :meth:`_reap` having ended the sessions of a
        sandbox that is gone, so such an id is one that does not exist.

        What is left goes to the transport, a DELETE after the session has been ended the
        way requirement 15.7 or 15.8 asks: the transport answers it with 200 and terminates
        itself, which ends the session task (probe V3). A GET, and any other method, is
        answered with 405 - this service opens no server-initiated stream
        (requirement 14.11, probe V5).
        """
        record = self._sessions.get(session_id)
        if record is None or record.terminated:
            raise _Refused(_not_found(_UNKNOWN_SESSION.format(session_id=session_id)))
        if record.path != route.path:
            raise _Refused(_not_found(_WRONG_PATH.format(session_id=session_id, path=route.path)))
        if method == _DELETE:
            await self._end_session(record)
        elif method != _POST:
            raise _Refused(
                _json_error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    types.INVALID_REQUEST,
                    _METHOD_NOT_ALLOWED,
                    headers={_ALLOW_HEADER: ALLOWED_HTTP_METHODS},
                )
            )
        await record.transport.handle_request(scope, receive, send)

    def _alive(self, sandbox: SandboxRuntime) -> bool:
        """Whether ``sandbox`` can still serve the requests of a session (req 14.7).

        A frozen sandbox is read-only until it is deleted (requirement 15.12) and a deleted
        one is gone from the registry of the Sandbox_Manager, whichever of the two
        happened: a freeze or a delete by the Admin API, a freeze by the idle timing, a
        delete by the retention period, or the freeze a failed trace write caused.
        """
        return sandbox.status != "frozen" and self.manager.get(sandbox.sandbox_id) is not None

    # --- opening a session (requirements 14.8, 15.1, 15.3, 15.10, 15.14, 17.9) ---------

    async def _open_session(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        route: _Route,
        method: str,
        headers: Headers,
    ) -> None:
        """Serve a request that carries no session id: an initialize, or a 400.

        The steps, in the order of requirement 23.11 and design.md 7.2: only a POST whose
        body is a single JSON-RPC initialize request opens a session and anything else is a
        400 (requirement 14.8); the headers the transport would refuse are refused here, so
        that no sandbox is created for a request that would never have reached a session;
        the sandbox is then had - created for the four auto addresses, looked up for
        ``/s/<sandbox_id>/mcp`` - the session is registered, its task started and its
        ``session_start`` written, and the initialize is handed to the transport, which
        answers it with the session id in the ``Mcp-Session-Id`` header.

        The body is read here and replayed to the transport, which reads it again through
        ``Request.body()``; the BodyLimit middleware of task 23.1 has already buffered it,
        so this reads a replay of a replay and no byte of it is read from the socket twice.
        """
        if method != _POST:
            raise _Refused(_bad_request(_MISSING_SESSION))
        body = await _read_body(receive)
        request = _initialize_request(body)
        _preflight(headers)
        if isinstance(route, _PrebuiltRoute):
            # No Snapshot is copied and the sandbox keeps its content and its anchor date;
            # the sessions that are already bound to it keep theirs (requirement 15.3).
            sandbox, profile = route.sandbox, None
        else:
            sandbox, profile = await self._create(route, request.id), route.profile
        record = await self._register(route.path, sandbox, profile, request.id)
        await record.transport.handle_request(scope, _replaying(body), send)

    async def _create(self, route: _AutoRoute, request_id: types.RequestId) -> SandboxRuntime:
        """Create the sandbox of a session being opened on ``route`` (requirement 15.1).

        The anchor date is resolved here rather than left to the Sandbox_Manager, which
        would resolve the same one: it is the date the error of requirement 15.14 has to
        name, and resolving it once means the message names the date that was tried.

        Raises :class:`_Refused` with 503 where the concurrent sandboxes are all taken
        (requirements 15.10 and 19.18) and with 500 and JSON-RPC -32603 where the sandbox
        could not be created (requirement 15.14). The second one catches every failure but
        the first, because the gateway does not know the Snapshot source it was given - the
        seed layer's exceptions are not imported here (module docstring) - and because
        every one of them means the same thing to the client: there is no sandbox, and no
        session was opened on one. The exception itself is logged, never answered with
        (design.md error handling).
        """
        anchor = resolve_anchor(None, self._loaded.instance.clock, self._handlers.clock)
        try:
            return await self.manager.create(
                route.profile, route.dataset, anchor=anchor, origin="auto"
            )
        except SandboxLimitReached as error:
            raise _Refused(
                _json_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    types.INVALID_REQUEST,
                    str(error),
                    request_id=request_id,
                )
            ) from error
        except Exception as error:
            _LOG.warning(_CREATE_FAILED, route.dataset.name, anchor.isoformat(), error)
            raise _Refused(_snapshot_failed(route.dataset, anchor, request_id)) from error

    async def _register(
        self,
        path: str,
        sandbox: SandboxRuntime,
        profile: ProfileModel | None,
        request_id: types.RequestId,
    ) -> SessionRecord:
        """Register the session of an accepted initialize and start its task (7.2).

        The order is design.md 7.2's: the record and its transport are made, the session is
        bound to the sandbox (requirements 15.3 and 19.12), the session task is started and
        waited for until ``transport.connect()`` has been entered - a transport that has
        not been connected raises instead of answering (probe V3) - and the
        ``session_start`` of requirement 17.9 is written inside the sandbox lock, which also
        counts the initialize as a call for the idle timing (requirement 15.5).

        A ``session_start`` that cannot be written leaves no session: the sandbox has been
        turned frozen by the Sandbox_Manager (design.md error handling), the session is
        ended again and the initialize is answered with 500 and JSON-RPC -32603. A sandbox
        that was frozen between the address check and the lock - the janitor's doing - is a
        404, the same answer the address would have got a moment later.
        """
        tasks = self._tasks
        if tasks is None:
            raise RuntimeError(_NO_TASK_GROUP)
        session_id = self._sessions.new_id()
        record = SessionRecord(
            session_id=session_id,
            path=path,
            sandbox=sandbox,
            transport=StreamableHTTPServerTransport(
                mcp_session_id=session_id,
                is_json_response_enabled=True,
                event_store=None,
                security_settings=_NO_DNS_REBINDING,
            ),
            task_scope=anyio.CancelScope(),
        )
        sandbox.bind(session_id)
        self._sessions.add(record)
        context = McpSessionContext.of(session_id, sandbox, profile)
        await tasks.start(self._run_session, record, context)
        try:
            await self.manager.run(sandbox, _session_start(session_id))
        except SandboxFrozen as error:
            await self._end_sessions((record,))
            message = _UNKNOWN_SANDBOX.format(sandbox_id=sandbox.sandbox_id)
            raise _Refused(_not_found(message)) from error
        except TraceWriteError as error:
            await self._end_sessions((record,))
            raise _Refused(
                _json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    types.INTERNAL_ERROR,
                    _TRACE_FAILED.format(sandbox_id=sandbox.sandbox_id),
                    request_id=request_id,
                )
            ) from error
        return record

    async def _run_session(
        self,
        record: SessionRecord,
        context: McpSessionContext,
        *,
        task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        """Run the session ``record`` until it ends (design.md 7.2, probe V3).

        The composition of the architecture diagram: the transport's streams, the
        MessageFilter between the read stream and the SDK session, and ``Server.run`` over
        what the filter passed on, all three writing to the one stream that goes to the
        client. ``raise_exceptions`` stays false, so an exception in a handler becomes a
        JSON-RPC error for that request and the session goes on serving the ones after it
        (design.md error handling).

        Reports started once ``connect()`` has been entered, which is when the transport
        begins to take requests, so the initialize that opened the session can be handed to
        it. Runs in the task group of :meth:`attach` and inside the record's own cancel
        scope, which is what lets one session be ended without touching the others.

        The filter's hooks are what design.md 7.2 gives Streamable HTTP: ``activity``, which
        records the arrival of every request of this session against the idle timing of its
        sandbox (requirement 15.5), and no ``initialize`` hook - the sandbox of this session
        exists before the transport sees its first request.
        """
        sandbox = record.sandbox
        clock = self._handlers.clock
        hooks = FilterHooks(activity=lambda: sandbox.mark_activity(clock.monotonic()))
        server = self._handlers.build_server(context)
        try:
            with record.task_scope:
                async with record.transport.connect() as (read_stream, write_stream):
                    task_status.started()
                    to_sdk, from_filter = anyio.create_memory_object_stream[_Incoming](0)
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(_filter_messages, read_stream, to_sdk, write_stream, hooks)
                        await server.run(
                            from_filter, write_stream, INIT_OPTIONS, raise_exceptions=False
                        )
        finally:
            # However the session ended - its client's DELETE, a sandbox that was taken
            # away, the service stopping - its id is gone from here on (requirement 14.7).
            self._sessions.discard(record)

    # --- ending a session (requirements 15.7, 15.8, 17.9) ------------------------------

    async def _end_session(self, record: SessionRecord) -> None:
        """End the session ``record`` because its client sent a DELETE (requirement 17.9).

        The ``session_end`` event is written first, inside the sandbox lock, so a call of
        this session that is still running finishes before it (requirement 15.4); the
        record leaves the registry before that, so no request of this id reaches the
        transport while the session is being closed. What happens to the sandbox then is
        where requirements 15.7 and 15.8 differ: a sandbox this service created for the
        session is frozen, and every session of it ends with it; a prebuilt sandbox keeps
        its state, its content, its anchor date and its other sessions, and only this
        session's binding is released.

        The release of the binding is this record leaving the registry: the ``session_ids``
        of the sandbox are the sessions that were ever bound to it, in binding order, which
        is what requirement 19.12 reports, and a session that ends is not taken off that
        list.

        The transport is not terminated here: the DELETE is handed to it afterwards, it
        answers 200 and terminates itself, and the session task ends on its own (probe V3).
        A ``session_end`` that cannot be written, or a sandbox that was frozen in the
        meantime, is logged and changes nothing about the DELETE: the session is over either
        way, and a sandbox whose trace failed has been frozen by the Sandbox_Manager.
        """
        sandbox = record.sandbox
        self._sessions.discard(record)
        try:
            await self.manager.run(sandbox, _session_end(record.session_id))
        except (TraceWriteError, SandboxFrozen) as error:
            _LOG.warning(_SESSION_END_FAILED, record.session_id, error)
        if sandbox.origin == "auto":
            await self.manager.freeze(sandbox)
            await self.terminate_sandbox(sandbox.sandbox_id)

    async def _end_sessions(self, records: Iterable[SessionRecord]) -> None:
        """Terminate the transports of ``records`` and cancel their tasks (req 14.7).

        What every way of ending a session that is not its client's DELETE goes through:
        the record leaves the registry, so its id answers 404; the transport is terminated,
        which closes its streams, lets ``Server.run`` return and makes the transport answer
        404 itself for anything that arrives after (probe V3); and the session task is
        cancelled, which is the backstop for a task that the termination did not end -
        a handler of the session waiting on the lock of a sandbox that is gone, above all.
        """
        for record in records:
            self._sessions.discard(record)
            await record.transport.terminate()
            record.task_scope.cancel()


# --- what the router writes itself -------------------------------------------------------


def _not_found(message: str) -> Response:
    """The 404 of requirements 14.3 and 14.7: a JSON-RPC error and nothing else.

    No trace event is written and no sandbox is touched, which is what requirement 14.7
    asks of a request that is refused here (requirement 17.9).
    """
    return _json_error(HTTPStatus.NOT_FOUND, types.INVALID_REQUEST, message)


def _bad_request(message: str) -> Response:
    """The 400 of requirement 14.8: a request that is no initialize and has no session."""
    return _json_error(HTTPStatus.BAD_REQUEST, types.INVALID_REQUEST, message)


def _snapshot_failed(dataset: DatasetModel, anchor: date, request_id: types.RequestId) -> Response:
    """The 500 of requirement 15.14, carrying the id of the initialize it answers.

    The body is the JSON-RPC error of design.md 7.2: code -32603 and a message that names
    the initial dataset and the anchor date whose Snapshot could not be built. No session
    and no sandbox are left behind, and the sessions that already run are untouched.
    """
    return _json_error(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        types.INTERNAL_ERROR,
        SNAPSHOT_FAILED_MESSAGE.format(dataset=dataset.name, anchor=anchor.isoformat()),
        request_id=request_id,
    )


def _json_error(
    status: HTTPStatus,
    code: int,
    message: str,
    *,
    request_id: types.RequestId = _SERVER_ERROR_ID,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """One JSON-RPC error response, in the shape the SDK's transport writes its own.

    ``request_id`` is the id of the request being answered where the router has parsed one
    - the initialize of a session that was being opened - and :data:`_SERVER_ERROR_ID`
    where it has not: a request whose body was never read has no id to answer with. The
    body carries ``code`` and ``message`` alone, as the transport's does, and the response
    carries no ``Mcp-Session-Id`` header: the refusals written here belong to no session.
    """
    body: dict[str, JsonValue] = {
        "jsonrpc": _JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    return Response(
        content=encode_utf8(dumps_compact(body)),
        status_code=status,
        headers={_CONTENT_TYPE: _JSON_MEDIA_TYPE, **(headers or {})},
    )


# --- the preflight of an opening request (probe V3, requirement 24.1) --------------------


def _preflight(headers: Headers) -> None:
    """Refuse the headers the transport would refuse anyway, in its order (probe V3).

    The first three of the transport's seven POST checks, which are the ones that can
    refuse the opening request of a session; items 4 and 5 are
    :func:`_initialize_request`, which accepts exactly the bodies the transport accepts,
    and items 6 and 7 are about a session id, which a request on this path does not carry.
    Repeating them here is what keeps a sandbox from being created for a request the
    transport would have thrown away:

    1. a missing Content-Type, or one whose value lower-cased does not start with
       ``application/json``: 400 and a plain-text body, as
       ``TransportSecurityMiddleware`` answers it;
    2. an Accept header no entry of which starts with ``application/json``
       (case-sensitive, so ``*/*``, ``application/*`` and a missing header are refused;
       ``text/event-stream`` is not required of a JSON-mode transport): 406;
    3. a Content-Type whose media type is not exactly ``application/json``
       (case-sensitive, so ``APPLICATION/JSON`` and ``application/json-seq`` are refused
       here although they passed check 1): 415.

    Raises :class:`_Refused` with the transport's own status and message for the first of
    the three that fails.
    """
    content_type = headers.get(_CONTENT_TYPE_HEADER)
    if content_type is None or not content_type.lower().startswith(_JSON_MEDIA_TYPE):
        raise _Refused(Response(INVALID_CONTENT_TYPE, status_code=HTTPStatus.BAD_REQUEST))
    accept = headers.get(_ACCEPT_HEADER, "")
    if not any(entry.strip().startswith(_JSON_MEDIA_TYPE) for entry in accept.split(",")):
        raise _Refused(
            _json_error(HTTPStatus.NOT_ACCEPTABLE, types.INVALID_REQUEST, NOT_ACCEPTABLE_MESSAGE)
        )
    media_types = content_type.partition(";")[0].split(",")
    if not any(media_type.strip() == _JSON_MEDIA_TYPE for media_type in media_types):
        raise _Refused(
            _json_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                types.INVALID_REQUEST,
                UNSUPPORTED_MEDIA_TYPE_MESSAGE,
            )
        )


def _initialize_request(body: bytes) -> types.JSONRPCRequest:
    """``body`` as the initialize request it must be, or raise a 400 (requirement 14.8).

    Parsed the way the transport parses a body (probe V3): ``json.loads``, then the
    JSON-RPC message model, then the one shape this path accepts - a request, not a
    notification, a response or a batch, whose method is ``initialize``. A request without
    a session id that is anything else creates neither a session nor a sandbox and is
    answered with 400, which is also what the three failures above get: what the client did
    wrong is the same in all three, and naming the parser's complaint would say nothing
    about it.
    """
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise _Refused(_bad_request(_NOT_INITIALIZE)) from error
    try:
        message = types.JSONRPCMessage.model_validate(payload)
    except ValidationError as error:
        raise _Refused(_bad_request(_NOT_INITIALIZE)) from error
    request = message.root
    if not isinstance(request, types.JSONRPCRequest) or request.method != INITIALIZE:
        raise _Refused(_bad_request(_NOT_INITIALIZE))
    return request


# --- the body of a request, read once and replayed ---------------------------------------


async def _read_body(receive: Receive) -> bytes:
    """Read the whole body of the request ``receive`` belongs to.

    The router needs the body of a request that carries no session id, to see whether it is
    an initialize (requirement 14.8), and the transport reads the body again through
    ``Request.body()``, which is what :func:`_replaying` is for. A client that disconnects
    in the middle ends the body where it stopped, and what was read is then not an
    initialize, so the request is refused like any other.
    """
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(bytes(message.get("body", b"")))
        more = bool(message.get("more_body", False))
    return b"".join(chunks)


def _replaying(body: bytes) -> Receive:
    """An ASGI ``receive`` that delivers ``body`` once and a disconnect after it.

    What the transport is given after the router has read the body (probe V3): it builds a
    ``Request`` of its own and reads the body through it, which must see the same bytes.
    The disconnect that follows is what an ASGI server sends when there is nothing more to
    read; ``Request.body()`` stops at the end of the body and never asks for it.
    """
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


# --- the session task's two helpers ------------------------------------------------------


async def _filter_messages(
    src: MemoryObjectReceiveStream[_Incoming],
    dst: MemoryObjectSendStream[_Incoming],
    out: MemoryObjectSendStream[SessionMessage],
    hooks: FilterHooks,
) -> None:
    """Run the MessageFilter of one session, treating closed streams as its end.

    A transport that is terminated closes both ends of both its streams (probe V3), so the
    filter may be reading from a stream that is closed under it, or writing an answer of
    its own to one. Either is the end of the session and not a failure of the service: the
    filter closes the stream ``Server.run`` reads on its way out whichever way it leaves,
    which is what lets ``Server.run`` return, and what is left here is to keep the
    exception out of the serve task group.
    """
    try:
        await message_filter(src, dst, out, hooks)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        _LOG.debug("会话的传输已终止，消息过滤器随之结束")


def _session_start(session_id: str) -> Callable[[SandboxRuntime], int]:
    """The job that writes the ``session_start`` of ``session_id`` (requirement 17.9)."""

    def job(sb: SandboxRuntime) -> int:
        return sb.trace.write(EventName.SESSION_START, session_id)

    return job


def _session_end(session_id: str) -> Callable[[SandboxRuntime], int]:
    """The job that writes the ``session_end`` of ``session_id`` (requirement 17.9)."""

    def job(sb: SandboxRuntime) -> int:
        return sb.trace.write(EventName.SESSION_END, session_id)

    return job
