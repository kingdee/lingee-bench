"""Two ports on one event loop, and the stop that ends both: the serve command (7.4).

The serve command of requirement 22.1 is one process that serves two things: the Streamable
HTTP MCP endpoint on ``--host``/``--port``, and the Admin API on the loopback address alone
(requirement 19.1). This module is that process from the first socket to the last sandbox
it closes::

    exit_code = await serve(loaded, options, env, snapshots=snapshot_source)

What it assembles. One :class:`GatewayState` holds everything the two ports share - the
Sandbox_Manager, the session handlers, the :class:`~erp_mock.gateway.router.McpRouter` and
the :class:`~erp_mock.gateway.admin.AdminRouter` - and the two applications are the
middleware chains of requirement 23.11 around those two routers (design.md 7.4,
:mod:`erp_mock.gateway.middleware`)::

    mcp_app   = OriginGuard(McpTokenGuard(BodyLimit(McpRouter(...)), mcp_token), origins)
    admin_app = AdminTokenGuard(BodyLimit(AdminRouter(...)), admin_token)

Both are pure ASGI callables, so nothing wraps the response stream of the SDK's transport,
and both run as a :class:`QuietServer` on a socket this module bound beforehand. A path of
the MCP port that starts with ``/admin`` is a 404 of the router, which matches the five
addresses of requirement 14.2 and nothing else (requirement 19.1); the Admin API is
reachable on the Admin port alone.

Who decides what. This module is given objects that are already assembled and settles
nothing of the command line:

=====================  =====================================================
what                   whose
=====================  =====================================================
:class:`ServeOptions`  the CLI, from ``--host``, ``--port``, ``--admin-port``
                       and ``--data-dir``, the defaults of requirement 22.1,
                       relative paths resolved against the working directory
                       (task 24.3)
:class:`EnvSettings`   the CLI, from the environment variables of
                       requirement 23.9, read once at startup (task 24.1)
the Admin token        the CLI: ``ERP_MOCK_ADMIN_TOKEN`` where it is set,
                       otherwise generated and written to the token file
                       (requirements 23.4 and 23.5, task 24.3). This module
                       receives the value and refuses an empty one
the Snapshot source    the CLI, an adapter over the Snapshot cache
                       (task 24.3)
the ``--host`` check   the CLI, before it loads: a non-loopback host without
                       an MCP token is a usage error (requirement 23.2)
=====================  =====================================================

The Snapshot source is injected rather than built here on purpose: the gateway imports
neither the domain layer (tests/static/test_layering.py) nor the seed layer, which would
pull the domain layer in behind it - :mod:`erp_mock.gateway.admin` repeats a file name
rather than import it for the same reason. The tools layer is injectable for a smaller
reason: :class:`~erp_mock.tools.layer.ToolLayer` builds an engine of its own from the
domain model when it is given none, which parses every expression of the configuration a
second time, and the CLI has the engine loading already built (task 24.3).

What a request meets, in order (requirement 23.11, only the first failure is answered):

=====  ======  ==============================================  ==============
port   step    what                                            status
=====  ======  ==============================================  ==============
MCP    1       the Origin                                      403
MCP    2       the MCP endpoint token, where one is configured  401
MCP    3       the size of the body                            413
MCP    4 to 7  the address, the session, the concurrency        404, 400, 503,
               limit, the Snapshot                             500
Admin  1       the Admin token, for every path                 401
Admin  2       the size of the body                            413
Admin  3       the route table                                 404
=====  ======  ==============================================  ==============

The two sockets are bound before either server runs, as ``uvicorn.Config.bind_socket``
binds them (:func:`bind_socket`): ``SO_REUSEADDR``, and ``AF_INET6`` when the host carries
a ``:``, since ``--host`` may be an IPv6 address. The Admin socket is bound to
:data:`ADMIN_HOST` whatever ``--host`` says, which is requirement 19.1 made structural.
Binding first is what lets a port that cannot be had end the command with
:data:`EXIT_BIND_FAILED` before a sandbox exists (requirement 22.8, task 24.3), and what
makes ``--port 0`` usable: the port the operating system chose is reported through
:attr:`ServeRuntime.endpoints` and by the ``task_status`` of :meth:`ServeRuntime.serve`,
which a test needs to address a service it started on a free port.

What uvicorn is told, and why (all of it recorded in tests/probes/test_uvicorn.py on the
locked uvicorn 0.54.0):

- V7 holds: the signal capture of a server is ``Server.capture_signals``, and
  :class:`QuietServer` overrides it with a context manager that does nothing. The process
  handles SIGINT and SIGTERM itself (:meth:`ServeRuntime._watch_signals`,
  requirement 22.1); without the override the server started last would replace the
  process's handlers with its own ``handle_exit`` and re-raise every signal it caught on
  its way out. Were V7 to break after a version change, the fallback of task 23.4 is to
  run each server on an event loop of its own in a thread of its own - off the main thread
  ``capture_signals`` installs nothing - and keep the signal handling on the main thread;
- ``lifespan="off"``, ``access_log=False`` and ``log_config=None`` (design.md 7.4): the
  service logs what it wants to log, the ASGI applications are given no lifespan message,
  and uvicorn configures no logging handler of its own. With pre-bound sockets and the
  lifespan protocol off, neither of the two ``sys.exit`` calls in ``Server.startup`` can
  be reached;
- ``proxy_headers=False``: the default True puts uvicorn's ``ProxyHeadersMiddleware`` in
  front of the application, where an X-Forwarded-For or X-Forwarded-Proto of a request
  from the loopback address rewrites ``scope["client"]`` and ``scope["scheme"]``. Nothing
  of this service reads either, and a header of a client is not allowed to decide what the
  service thinks a request is;
- ``serve([sock])`` hands the socket to ``loop.create_server``, which listens on it; the
  host and the port of the Config are ignored, no address is logged (this module logs the
  two endpoints itself) and ``Server.shutdown`` closes the socket it was given.

The stop, whatever triggered it (requirement 22.1):

====  ==================================================  ==========================
step  what                                                why
====  ==================================================  ==========================
1     ``should_exit`` on both servers                     the only way to stop a
                                                          server that captures no
                                                          signal
2     wait until both ``serve()`` calls have returned     uvicorn closes the
                                                          listeners, lets the
                                                          requests in flight finish
                                                          and waits for the
                                                          connections to go. An
                                                          in-flight MCP request
                                                          waits on its session task,
                                                          so nothing of a session
                                                          may be cancelled first
3     end every session (``McpRouter.shutdown``)          no session is left reading
                                                          the streams of a server
                                                          that is going away
4     cancel the serve task group                         the janitor ends only by
                                                          cancellation, and so would
                                                          hold the group open
5     close the Sandbox_Manager                           no database and no trace
                                                          file is left open; the
                                                          sandbox directories stay
                                                          on disk for the next
                                                          process
====  ==================================================  ==========================

Steps 3 to 5 run after the two servers have returned, which is why the servers run in a
task group of their own inside the serve task group: the end of that inner group *is* the
fact that both ``serve()`` calls have returned. The design sketch of component 7.4 starts
both servers in the serve task group itself; the inner group is a child of it, so the
lifetime, the cancellation and the one event loop are the same, and the janitor and the
session tasks belong to the serve task group as the design asks.

Why :meth:`ServeRuntime.stop` exists beside the signal watcher, as in
:mod:`erp_mock.gateway.stdio`: the watcher is ``anyio.open_signal_receiver``, which
registers with ``loop.add_signal_handler`` and raises RuntimeError off the main thread,
and a test that sent a real SIGINT would be sending it to pytest. The watcher therefore
does one thing - call :meth:`ServeRuntime.stop` - so the exit path of a signal and the
exit path a caller triggers are one path; ``signals`` of an empty sequence installs no
handler at all, which is what an in-process test passes.

What this module does not do. It does not restore the sandboxes a previous process left in
the data directory (design.md 7.5), it writes no ``session_end`` and freezes no sandbox
when it stops - requirement 17.9 writes that event for a session its client ended - and it
exits with :data:`EXIT_OK` for every stop that was asked for, the exit code of a load that
failed being the CLI's business (requirements 22.7 and 22.8).
"""

import contextlib
import logging
import signal
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio
import uvicorn
from anyio.abc import TaskStatus
from starlette.types import ASGIApp

from erp_mock.gateway.admin import AdminRouter
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import SessionHandlers
from erp_mock.gateway.middleware import AdminTokenGuard, BodyLimit, McpTokenGuard, OriginGuard
from erp_mock.gateway.router import McpRouter
from erp_mock.gateway.sandbox import (
    DEFAULT_SANDBOX_LIMITS,
    SandboxLimits,
    SandboxManager,
    SnapshotSource,
)
from erp_mock.loader.instance import LoadedInstance
from erp_mock.state.schema import StorageSchema, build_schema
from erp_mock.tools.layer import DEFAULT_LIMITS, ResourceLimits, ToolLayer

__all__ = [
    "ADMIN_HOST",
    "DEFAULT_ADMIN_PORT",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_SIGNALS",
    "EXIT_BIND_FAILED",
    "EXIT_OK",
    "MAX_BODY_BYTES",
    "Endpoint",
    "EnvSettings",
    "GatewayState",
    "QuietServer",
    "ServeEndpoints",
    "ServeOptions",
    "ServeRuntime",
    "bind_socket",
    "serve",
]

# Where everything this module has to say goes: the service log, whose handlers point at
# stderr alone (design.md 7.3). uvicorn is given ``log_config=None``, so it adds no
# handler of its own and writes nothing about the sockets it was handed.
_LOG: Final = logging.getLogger(__name__)

# The defaults of requirement 22.1, which the CLI fills these options with (task 24.3).
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8000
DEFAULT_ADMIN_PORT: Final = 8001

# The one host the Admin API is ever reachable on (requirement 19.1). Not ``--host``: an
# Admin port on a routable address would be an administration interface of this machine on
# the network, which no option of this command offers.
ADMIN_HOST: Final = "127.0.0.1"

# ``ERP_MOCK_MAX_BODY_BYTES`` where the environment sets none (requirement 23.9): 256 KB,
# the limit both ports count a request body against.
MAX_BODY_BYTES: Final = 262144

# The signals that stop the service (requirement 22.1): SIGINT is the interrupt of a
# terminal, SIGTERM the way a supervisor stops the process. Either leads to the same exit
# path as :meth:`ServeRuntime.stop`.
DEFAULT_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM)

# The exit codes this module decides (requirement 22.8): a stop that was asked for is a
# success, and a port that could not be bound is the 1 of a service that could not start.
# Every other exit code of the command - 2 for a usage error, 1 for a load that failed -
# is settled before serve runs (task 24.3).
EXIT_OK: Final = 0
EXIT_BIND_FAILED: Final = 1

# How often :meth:`ServeRuntime._wait_until_started` looks at the two servers. uvicorn has
# no "started" event to await, only the flag ``Server.started``; the probe watches it the
# same way. The wait is over within a tick or two of this.
_STARTED_POLL_SECONDS: Final = 0.01

# What the log says when the service is up and when it is going down again. The two URLs
# are public configuration; no message of this module carries a token (requirement 23.6).
_SERVING: Final = "MCP 端点 %s/mcp，Admin 接口 %s"
_SIGNAL_RECEIVED: Final = "收到信号 %s，serve 进程开始退出"
_STOPPING: Final = "serve 进程停止监听，正在结束会话并关闭沙箱"

# What a port that could not be had is reported as, and the one assembly mistake this
# module refuses to run with: an Admin port whose token is empty would let
# ``Authorization: Bearer `` through (requirement 19.2), so the service does not start.
_BIND_FAILED: Final = "无法绑定 %s 的 %d 端口：%s"
_NO_ADMIN_TOKEN: Final = "Admin 令牌不能为空，Admin 端口必须由令牌保护（需求 23 第 4、5 条）"


class QuietServer(uvicorn.Server):
    """A uvicorn server that leaves SIGINT and SIGTERM to the process around it.

    ``Server.serve`` runs the whole server inside ``with self.capture_signals():``, which
    installs ``Server.handle_exit`` for SIGINT and SIGTERM and re-raises on its way out
    every signal it caught; this override does nothing at all, so the handlers of the
    process stay the process's own (V7 of tests/probes/test_uvicorn.py). Stop such a server
    by setting ``should_exit``, which :class:`ServeRuntime` does for both of them at once.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


# --- what the command settled, and what the environment did ------------------------------


@dataclass(frozen=True, slots=True)
class ServeOptions:
    """The options of one serve command that this module reads (requirement 22.1).

    - ``data_dir``: ``--data-dir``, below whose ``sandboxes/`` every sandbox of the process
      gets a directory. It has no default here because the CLI resolves ``./var`` against
      the working directory of the command before it builds this (task 24.3);
    - ``host`` and ``port``: where the MCP endpoint listens, ``127.0.0.1`` and 8000 by
      default. A host that is not a loopback address needs an MCP endpoint token, which the
      CLI refuses to start without (requirement 23.2);
    - ``admin_port``: the port of the Admin API, 8001 by default; its host is
      :data:`ADMIN_HOST` and no option of the command can change that (requirement 19.1).

    ``--instance`` and ``--packs-dir`` are not here: what they name has already been
    loaded and validated by the time this module runs (requirement 22.7).
    """

    data_dir: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    admin_port: int = DEFAULT_ADMIN_PORT


@dataclass(frozen=True, slots=True)
class EnvSettings:
    """What the environment of a serve process settled (requirement 23.9).

    Read once at startup by the CLI and unchanged for as long as the process runs
    (task 24.1); the field names are the variables without their ``ERP_MOCK_`` prefix,
    with the two groups of limits in the objects that own them:

    =====================  =============================  ==============================
    field                  variable                       default
    =====================  =============================  ==============================
    ``admin_token``        ``ERP_MOCK_ADMIN_TOKEN``       generated by the CLI (req 23.5)
    ``mcp_token``          ``ERP_MOCK_MCP_TOKEN``         None, no token required
    ``allowed_origins``    ``ERP_MOCK_ALLOWED_ORIGINS``   none beyond the loopback
                                                          origins of requirement 23.1
    ``max_body_bytes``     ``ERP_MOCK_MAX_BODY_BYTES``    :data:`MAX_BODY_BYTES`
    ``sandbox``            the four of req 15.11          :data:`DEFAULT_SANDBOX_LIMITS`
    ``resources``          the three of req 23.12         :data:`DEFAULT_LIMITS`
    =====================  =============================  ==============================

    ``admin_token`` is the value the Admin port checks every request against
    (requirement 19.2): ``ERP_MOCK_ADMIN_TOKEN`` where the environment set one
    (requirement 23.4), otherwise the one the CLI generated and wrote to the token file
    (requirement 23.5). Generating it and writing that file belong to the CLI; this
    carries the value and refuses an empty one, an Admin port without a token being a
    thing this service does not offer.

    ``mcp_token`` of None - and so of an unset or empty ``ERP_MOCK_MCP_TOKEN`` - means the
    MCP port requires no Authorization header (requirement 23.3), which is why the CLI
    refuses a non-loopback ``--host`` without one (requirement 23.2).
    """

    admin_token: str
    mcp_token: str | None = None
    allowed_origins: tuple[str, ...] = ()
    max_body_bytes: int = MAX_BODY_BYTES
    sandbox: SandboxLimits = DEFAULT_SANDBOX_LIMITS
    resources: ResourceLimits = DEFAULT_LIMITS

    def __post_init__(self) -> None:
        """Refuse an empty Admin token, which would guard the Admin port with nothing."""
        if not self.admin_token:
            raise ValueError(_NO_ADMIN_TOKEN)


# --- where the two servers listen --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Endpoint:
    """The address a socket of this service is bound to, as the socket reports it.

    ``port`` is the port that is actually in use, which is what ``--port 0`` makes worth
    reporting: the operating system picked it and the caller cannot know it otherwise
    (:attr:`ServeRuntime.endpoints`).
    """

    host: str
    port: int

    @classmethod
    def of(cls, sock: socket.socket) -> "Endpoint":
        """The address ``sock`` is bound to; the tail of an AF_INET6 name is dropped."""
        host, port = sock.getsockname()[:2]
        return cls(host=str(host), port=int(port))

    @property
    def url(self) -> str:
        """``http://<host>:<port>``, with the brackets an IPv6 host needs in a URL."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


@dataclass(frozen=True, slots=True)
class ServeEndpoints:
    """Where the two ports of one serve process ended up listening.

    ``mcp`` is the MCP endpoint, whose address is ``<url>/mcp`` and the four other
    addresses of requirement 14.2 below it; ``admin`` is the Admin API, whose host is
    always :data:`ADMIN_HOST` (requirement 19.1).
    """

    mcp: Endpoint
    admin: Endpoint


def bind_socket(host: str, port: int) -> socket.socket:
    """A TCP socket bound to ``host`` and ``port``, not listening yet.

    Bound the way ``uvicorn.Config.bind_socket`` binds one (tests/probes/test_uvicorn.py):
    ``SO_REUSEADDR``, so a port whose last connections are in TIME_WAIT can be taken up
    again, and ``AF_INET6`` for a host that carries a ``:``, since ``--host`` may be an
    IPv6 address. ``port`` of 0 leaves the choice to the operating system and the socket
    then reports what it got (:meth:`Endpoint.of`).

    Listening is ``loop.create_server``'s, which ``Server.serve([sock])`` calls, and
    closing is ``Server.shutdown``'s for a socket a server was given; a socket this returns
    that reaches no server is closed by :meth:`ServeRuntime.serve`.

    Raises OSError where the address cannot be had - a port in use, a host that is not an
    address of this machine, a privileged port - which is the
    :data:`EXIT_BIND_FAILED` of requirement 22.8.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family=family, type=socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except BaseException:
        sock.close()
        raise
    return sock


# --- what the two ports share (design.md 7.4) --------------------------------------------


class GatewayState:
    """Everything one serve process holds, shared by its two ports (design.md 7.4).

    - ``loaded``: the validated instance configuration both routers read;
    - ``options``: the options of the command, of which this takes the data directory;
    - ``env``: what the environment settled, of which this takes the two groups of limits
      and the tokens the chains are built with;
    - ``snapshots``: where the Snapshot of a new sandbox comes from, injected because the
      gateway imports no seed layer (module docstring);
    - ``clock``: the one clock of the service; None reads the system clock, and a test
      passes a clock over sources it moves itself (requirement 25.10);
    - ``schema``: the tables of the instance; None builds them from the domain model, which
      is all they are a function of;
    - ``layer``: the tools layer a tools/call is performed by; None builds one over an
      engine of its own, and the CLI passes a layer over the engine loading has already
      built (module docstring).

    One per process. The MCP port and the Admin port are two applications over these same
    objects - :attr:`mcp_app` and :attr:`admin_app` - so a sandbox the Admin API created is
    a sandbox a session can reach (requirement 15.3) and a sandbox the Admin API froze ends
    the sessions bound to it (requirement 19.13).
    """

    __slots__ = (
        "_admin",
        "_admin_app",
        "_clock",
        "_env",
        "_handlers",
        "_layer",
        "_loaded",
        "_manager",
        "_mcp_app",
        "_router",
        "_schema",
    )

    def __init__(
        self,
        *,
        loaded: LoadedInstance,
        options: ServeOptions,
        env: EnvSettings,
        snapshots: SnapshotSource,
        clock: SystemClock | None = None,
        schema: StorageSchema | None = None,
        layer: ToolLayer | None = None,
    ) -> None:
        self._loaded = loaded
        self._env = env
        self._clock = SystemClock() if clock is None else clock
        self._schema = build_schema(loaded.domain) if schema is None else schema
        self._layer = ToolLayer(loaded, limits=env.resources) if layer is None else layer
        self._manager = SandboxManager(
            data_dir=options.data_dir,
            loaded=loaded,
            schema=self._schema,
            snapshots=snapshots,
            clock=self._clock,
            limits=env.sandbox,
        )
        self._handlers = SessionHandlers(self._manager, self._layer)
        self._router = McpRouter(self._handlers, loaded)
        self._admin = AdminRouter(self._router, loaded, self._schema)
        # The two chains of requirement 23.11, built once: a guard holds one value and
        # answers the first check that fails, so the order of the nesting is the order of
        # the checks (design.md 7.4).
        self._mcp_app: ASGIApp = OriginGuard(
            McpTokenGuard(
                BodyLimit(self._router, env.max_body_bytes),
                env.mcp_token,
            ),
            env.allowed_origins,
        )
        self._admin_app: ASGIApp = AdminTokenGuard(
            BodyLimit(self._admin, env.max_body_bytes), env.admin_token
        )

    @property
    def loaded(self) -> LoadedInstance:
        """The validated instance configuration this process serves (design.md 2.4)."""
        return self._loaded

    @property
    def env(self) -> EnvSettings:
        """What the environment settled for this process (requirement 23.9)."""
        return self._env

    @property
    def clock(self) -> SystemClock:
        """The one clock every sandbox of this process is timed by (requirement 24.7)."""
        return self._clock

    @property
    def schema(self) -> StorageSchema:
        """The tables every sandbox database of this process holds."""
        return self._schema

    @property
    def layer(self) -> ToolLayer:
        """The tools layer a tools/call of any session is performed by (design.md 5.3)."""
        return self._layer

    @property
    def manager(self) -> SandboxManager:
        """The Sandbox_Manager of this process, whose janitor serve runs (req 15.5)."""
        return self._manager

    @property
    def router(self) -> McpRouter:
        """The MCP endpoint of this process, behind the chain of :attr:`mcp_app`."""
        return self._router

    @property
    def admin(self) -> AdminRouter:
        """The Admin API of this process, behind the chain of :attr:`admin_app`."""
        return self._admin

    @property
    def admin_token(self) -> str:
        """The token every request of the Admin port is checked against (req 19.2)."""
        return self._env.admin_token

    @property
    def mcp_app(self) -> ASGIApp:
        """The application of the MCP port: the three guards and the router."""
        return self._mcp_app

    @property
    def admin_app(self) -> ASGIApp:
        """The application of the Admin port: the two guards and the Admin API."""
        return self._admin_app

    def close(self) -> None:
        """Close every sandbox of this process and the thread pool behind them.

        The last step of the stop (module docstring): no database and no trace file is left
        open and the sandbox directories stay on disk, where the Admin API of the next
        process could read them. Awaited nothing: the caller has already cancelled the
        janitor and ended every session by the time it gets here.
        """
        self._manager.close()


# --- the process (requirement 22.1) ------------------------------------------------------


class ServeRuntime:
    """The two servers of one serve process, from the sockets to the exit code.

    - ``state``: what the two ports share (:class:`GatewayState`);
    - ``options``: the options the sockets are bound by, which must be the ones ``state``
      was built with;
    - ``signals``: the signals that stop the service, empty installing no handler
      (:meth:`stop`).

    One runtime per process and one :meth:`serve` per runtime.
    """

    __slots__ = ("_endpoints", "_options", "_servers", "_signals", "_state", "_stop")

    def __init__(
        self,
        *,
        state: GatewayState,
        options: ServeOptions,
        signals: Sequence[signal.Signals] = DEFAULT_SIGNALS,
    ) -> None:
        self._state = state
        self._options = options
        self._signals = tuple(signals)
        self._servers = (
            QuietServer(_server_config(state.mcp_app)),
            QuietServer(_server_config(state.admin_app)),
        )
        self._endpoints: ServeEndpoints | None = None
        self._stop: anyio.Event | None = None

    @property
    def state(self) -> GatewayState:
        """What the two ports of this process share (design.md 7.4)."""
        return self._state

    @property
    def endpoints(self) -> ServeEndpoints | None:
        """Where the two ports are bound, None until :meth:`serve` has bound them.

        What ``--port 0`` makes worth reading, and the same value the ``task_status`` of
        :meth:`serve` reports once both servers are listening.
        """
        return self._endpoints

    async def serve(
        self, *, task_status: TaskStatus[ServeEndpoints | None] = anyio.TASK_STATUS_IGNORED
    ) -> int:
        """Serve both ports until a signal or :meth:`stop`, and return the exit code.

        ``task_status`` is reported with the two endpoints once both servers are listening,
        so a caller that started this with ``await task_group.start(...)`` may address the
        service as soon as it gets them (``--port 0``, module docstring); a run whose
        sockets could not be bound reports None and returns at once.

        Returns :data:`EXIT_BIND_FAILED` where a socket could not be bound, in which case
        nothing was started and no sandbox was created, and :data:`EXIT_OK` for a stop that
        was asked for - once the five steps of the stop are done (module docstring): both
        servers have returned, every session has ended, the janitor is cancelled and every
        sandbox is closed.
        """
        bound = self._bind()
        if bound is None:
            task_status.started(None)
            return EXIT_BIND_FAILED
        mcp_sock, admin_sock, endpoints = bound
        sockets = (mcp_sock, admin_sock)
        self._stop = anyio.Event()
        try:
            # The serve task group: the janitor, the session tasks of every session the
            # router opens, the signal watcher and - in a group of its own - the two
            # servers (design.md 7.4).
            async with anyio.create_task_group() as tasks:
                self._state.router.attach(tasks)
                tasks.start_soon(self._state.manager.janitor)
                await tasks.start(self._watch_signals)
                await self._run_servers(sockets, endpoints, task_status)
                # Both ``serve()`` calls have returned: the listeners are closed and every
                # request that was in flight has been answered, so nothing is left that a
                # session or the janitor is holding up.
                _LOG.info(_STOPPING)
                await self._state.router.shutdown()
                tasks.cancel_scope.cancel()
        finally:
            # ``Server.shutdown`` closed the sockets it was given; closing them again does
            # nothing, and a socket of a run that never got that far is closed here.
            for sock in sockets:
                sock.close()
            self._state.close()
        return EXIT_OK

    def stop(self) -> None:
        """Stop the service as a signal stops it, and let :meth:`serve` write the exit.

        The one door out: the signal watcher calls this and so may a caller that has no
        signal to send - the in-process tests above all (module docstring). Calling it
        before :meth:`serve` or after it has returned does nothing, and calling it twice is
        the same as calling it once.
        """
        stop = self._stop
        if stop is not None:
            stop.set()

    # --- the sockets -------------------------------------------------------------------

    def _bind(self) -> tuple[socket.socket, socket.socket, ServeEndpoints] | None:
        """Bind the MCP socket and the Admin socket, or report what could not be bound.

        The MCP socket takes the host and the port of the options and the Admin socket
        :data:`ADMIN_HOST` and ``--admin-port`` (requirement 19.1). Returns the two sockets
        and the addresses they ended up on, which is what ``--port 0`` makes worth
        returning, or None where either bind failed, after closing the other socket: a
        service that cannot have both of its ports starts neither of them
        (requirement 22.8).
        """
        options = self._options
        try:
            mcp_sock = bind_socket(options.host, options.port)
        except OSError as error:
            _LOG.error(_BIND_FAILED, options.host, options.port, error)
            return None
        try:
            admin_sock = bind_socket(ADMIN_HOST, options.admin_port)
        except OSError as error:
            mcp_sock.close()
            _LOG.error(_BIND_FAILED, ADMIN_HOST, options.admin_port, error)
            return None
        endpoints = ServeEndpoints(mcp=Endpoint.of(mcp_sock), admin=Endpoint.of(admin_sock))
        self._endpoints = endpoints
        return mcp_sock, admin_sock, endpoints

    # --- the two servers ---------------------------------------------------------------

    async def _run_servers(
        self,
        sockets: tuple[socket.socket, socket.socket],
        endpoints: ServeEndpoints,
        task_status: TaskStatus[ServeEndpoints | None],
    ) -> None:
        """Run both servers until they are told to exit, and return when both have.

        The two ``serve()`` calls are the only tasks of this group, so the group ending is
        the fact the three steps after it need (module docstring): the listeners are
        closed, the requests in flight are answered and the connections are gone.
        """
        async with anyio.create_task_group() as servers:
            for server, sock in zip(self._servers, sockets, strict=True):
                servers.start_soon(server.serve, [sock])
            await self._wait_until_started()
            task_status.started(endpoints)
            _LOG.info(_SERVING, endpoints.mcp.url, endpoints.admin.url)
            await self._await_stop()
            for server in self._servers:
                server.should_exit = True

    async def _wait_until_started(self) -> None:
        """Wait until both servers are listening, or until the stop was asked for.

        uvicorn reports a server that is up with the flag ``Server.started`` and with
        nothing to await, so this polls it (:data:`_STARTED_POLL_SECONDS`). A stop that
        arrives in the moment between the signal watcher and the first listener ends the
        wait as well, so that such a service stops instead of waiting for a server that is
        about to shut itself down.
        """
        while not (all(server.started for server in self._servers) or self._stopped()):
            await anyio.sleep(_STARTED_POLL_SECONDS)

    async def _await_stop(self) -> None:
        """Wait until a signal arrived or :meth:`stop` was called (requirement 22.1)."""
        stop = self._stop
        if stop is not None:
            await stop.wait()

    def _stopped(self) -> bool:
        """Whether the stop has been asked for already."""
        stop = self._stop
        return stop is not None and stop.is_set()

    async def _watch_signals(self, *, task_status: TaskStatus[None]) -> None:
        """Stop the service on the first of ``signals`` to arrive (requirement 22.1).

        ``task_status`` is reported once the handlers are installed, so no request is
        served before a signal can be caught. A runtime with no signals to watch reports
        and returns, which leaves the handlers of the process as they were (:meth:`stop`).
        """
        if not self._signals:
            task_status.started()
            return
        with anyio.open_signal_receiver(*self._signals) as signals:
            task_status.started()
            async for received in signals:
                _LOG.info(_SIGNAL_RECEIVED, signal.Signals(received).name)
                self.stop()
                return


def _server_config(app: ASGIApp) -> uvicorn.Config:
    """The uvicorn configuration of one of the two ports (module docstring).

    The host and the port are left out on purpose: the socket is bound before the server
    runs and ``serve([sock])`` ignores both.
    """
    return uvicorn.Config(
        app,
        lifespan="off",
        access_log=False,
        log_config=None,
        proxy_headers=False,
    )


async def serve(
    loaded: LoadedInstance,
    options: ServeOptions,
    env: EnvSettings,
    *,
    snapshots: SnapshotSource,
    clock: SystemClock | None = None,
    schema: StorageSchema | None = None,
    layer: ToolLayer | None = None,
    signals: Sequence[signal.Signals] = DEFAULT_SIGNALS,
) -> int:
    """Serve both ports of one process and return the exit code (design.md 7.4).

    What the serve command runs once its options are parsed and its instance configuration
    is loaded and validated (task 24.3): the arguments are what the command line and the
    environment settled and the Snapshot source the CLI built over the Snapshot cache.
    Returns when a signal arrived or the service was stopped and the exit of the module
    docstring is done - :data:`EXIT_OK` - or at once with :data:`EXIT_BIND_FAILED` where a
    port could not be bound, in which case nothing listened and no sandbox was created
    (requirement 22.8).

    See :class:`GatewayState` for ``clock``, ``schema`` and ``layer``, and
    :class:`ServeRuntime` for ``signals``.
    """
    state = GatewayState(
        loaded=loaded,
        options=options,
        env=env,
        snapshots=snapshots,
        clock=clock,
        schema=schema,
        layer=layer,
    )
    runtime = ServeRuntime(state=state, options=options, signals=signals)
    return await runtime.serve()
