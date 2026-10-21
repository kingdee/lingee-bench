"""One process, one sandbox, one session: the life of the stdio command (design.md 7.3).

The stdio transport is the second of the two transports of requirement 14.1, and the
simpler one: a client starts the process, talks JSON-RPC over its stdin and stdout, and
the process serves exactly one sandbox for as long as it runs (requirement 15.2).
:func:`run_stdio` is that life from the first byte to the diff it leaves behind::

    await run_stdio(
        loaded=loaded, schema=schema, manager=manager, handlers=handlers,
        options=StdioOptions(profile=profile, dataset=dataset, anchor_date=anchor),
    )

What this module composes, and what it owns. The three pieces it puts together are the
SDK's ``stdio_server`` (or the streams a caller injects), the
:func:`~erp_mock.gateway.message_filter.message_filter` and the per-session low-level
``Server`` of :mod:`erp_mock.gateway.handlers`::

    read, write = <stdio_server() or the injected streams>
    to_sdk, filtered = memory stream
    task group: message_filter(read -> to_sdk, write, hooks),
                server.run(filtered, write, INIT_OPTIONS)   # started by the initialize hook
                signal watcher (SIGINT, SIGTERM -> cancel the group)

and what it owns is everything around the session that the Streamable HTTP router
(design.md 7.2) owns on its side: the sandbox of the process, the one session id, the
``session_start`` of every initialize, the ``session_end`` of the exit and the
``diff.json`` the exit writes (requirements 22.3 and 22.4). The options and the exit code
of the command itself belong to the CLI (task 24.4); this is a coroutine over objects that
are already assembled.

The order of the first initialize (design.md 7.3, requirement 15.2). The filter hands
every initialize to :data:`~erp_mock.gateway.message_filter.InitializeHook` *before* the
SDK sees it, and the first one does all of this before it returns:

1. the anchor date is resolved by the priority of requirement 16.1, ``--anchor-date``
   first (:func:`~erp_mock.gateway.clock.resolve_anchor`). It is resolved here rather than
   left to :meth:`~erp_mock.gateway.sandbox.SandboxManager.create`, which would resolve
   the same date the same way, because the error of step 2 names it;
2. the sandbox is created: the Snapshot of the initial dataset at that date is taken up or
   built by the injected
   :class:`~erp_mock.gateway.sandbox.SnapshotSource` and copied into a sandbox of its own
   (requirement 15.1). A creation that fails raises
   :class:`~erp_mock.gateway.message_filter.InitializeFailed`, which the filter answers
   with JSON-RPC -32603 and does not forward, so no session is initialized against a
   sandbox that was not created and no sandbox is left behind (requirement 15.14);
3. the session is bound to it (requirement 19.12);
4. ``session_start`` is written, inside the sandbox lock, so it is the first event of the
   trace (requirements 17.6 and 17.9);
5. the sandbox id and the absolute path of the sandbox directory are reported on stderr
   (requirement 22.3);
6. the session's ``Server`` is built and started, which is why the task group is reachable
   from the hook: the handlers of a session close over the sandbox it is bound to
   (:class:`~erp_mock.gateway.handlers.McpSessionContext`), so the server of this session
   cannot exist before its sandbox does.

Every initialize after the first keeps that sandbox and writes one more ``session_start``
(requirement 15.2): the process creates no second sandbox and starts no second server.

The stream between the filter and the SDK session is unbounded, and that is step 6's
doing. Until the first initialize has been served there is nothing reading that stream, so
a bounded one would block the filter on the first request a client sent before its
initialize - and with it the initialize queued behind it, which would be a process that
answers nothing at all. Buffered, such a request waits for the session instead and is
served once the first initialize has made one: the SDK answers a ping at any time and
treats the rest as the protocol violation it is. A client that never initializes gets no
answer to it, which is the one way this differs from a server whose session exists before
its first message, and it follows from requirement 15.2 - there is no session before there
is a sandbox, and no sandbox before the first initialize. Nothing else about the service
depends on the buffer: requests still reach their handlers in arrival order (V4 of
tests/probes/test_sdk_server.py) and are still serialized by the sandbox lock alone
(requirement 15.4).

stdout carries MCP messages and nothing else (requirement 22.2). The SDK's stdout writer
is the only writer of it: every notice of this module goes to stderr (``notices``, which a
test replaces), the service log has stderr handlers alone, and ``print`` is not available
in this source tree at all (ruff rule T201).

The exit (requirement 22.4). stdin reaching EOF ends the filter, which closes the stream
the SDK reads and so ends the session; SIGINT and SIGTERM cancel the whole of it through
the signal watcher. Either way the same three steps follow, after the transport is closed
and outside the cancelled scope:

=============================  ==========================================  ==========
step                           why                                         req
=============================  ==========================================  ==========
``session_end``, under the     a call in flight finishes first: the lock    22.4, 17.9
lock                           is held until its job and its event are
                               written (design.md 7.5)
freeze the sandbox             the session of an automatically created      15.7
                               sandbox has ended
write ``<sandbox>/diff.json``  the net change of the run against the        22.4, 18.1
                               initial dataset, its ``base`` ``initial``
=============================  ==========================================  ==========

The diff is written with :func:`~erp_mock.common.jsonio.dumps_pretty`, which is what the
Admin API's diff endpoint serializes with (design.md 7.7), and with no trailing newline,
so the bytes of this file and of that response are the same bytes (requirement 24.2). It
is computed after the freeze, which closed the database: the comparison opens read
connections of its own (design.md 3.4) and the sandbox is read-only from then on, so
nothing can change under it. The job still runs through
:meth:`~erp_mock.gateway.sandbox.SandboxManager.run`, since every SQLite statement of the
service runs in the shared thread pool and never on the event loop.

A process that never received an initialize exits with nothing to do: there is no sandbox,
so there is no ``session_end`` and no ``diff.json``, and the exit code is 0 all the same
(requirement 22.4 speaks of a process that created a sandbox).

Why :meth:`StdioRuntime.stop` exists beside the signal watcher. The watcher is
``anyio.open_signal_receiver``, which registers with ``loop.add_signal_handler`` and
raises RuntimeError off the main thread (recorded in tests/probes/test_uvicorn.py), and a
test that sent a real SIGINT to the test runner would be sending it to pytest. The watcher
therefore does one thing - call :meth:`StdioRuntime.stop` - and :meth:`StdioRuntime.stop`
cancels the scope that holds the session, so the exit path of a signal and the exit path a
caller triggers are the same path. ``signals`` says which signals to watch and an empty
sequence installs no handler at all, which is what a test that does not want to touch the
runner's handlers passes.
"""

import logging
import math
import secrets
import signal
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Final, TextIO

import anyio
from anyio.abc import TaskGroup, TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from erp_mock.common.jsonio import dumps_pretty, encode_utf8
from erp_mock.gateway.clock import resolve_anchor
from erp_mock.gateway.handlers import INIT_OPTIONS, McpSessionContext, SessionHandlers
from erp_mock.gateway.message_filter import (
    FilterHooks,
    InitializeFailed,
    InitializeHook,
    message_filter,
)
from erp_mock.gateway.sandbox import SandboxManager, SandboxRuntime
from erp_mock.gateway.trace import EventName
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.state.diff import compute_diff
from erp_mock.state.schema import StorageSchema

__all__ = [
    "DEFAULT_SIGNALS",
    "DIFF_FILENAME",
    "SANDBOX_NOTICE",
    "SESSION_ID_PREFIX",
    "SNAPSHOT_FAILED_MESSAGE",
    "StdioOptions",
    "StdioRead",
    "StdioRuntime",
    "StdioStreams",
    "StdioWrite",
    "run_stdio",
]

# Where a notice of this module goes when it is not a notice for the client: the service
# log, whose handlers point at stderr alone (design.md 7.3).
_LOG: Final = logging.getLogger(__name__)

# The diff the stdio command writes into the sandbox directory when it exits
# (requirement 22.4, design.md data model 2); the Admin API's export writes a file of the
# same name and the same content into its zip (requirement 19.14).
DIFF_FILENAME: Final = "diff.json"

# The session id of the process: this prefix and 32 hexadecimal digits from the operating
# system's secure random source (requirement 24.6). One per process, since a stdio process
# serves one session (requirement 17.2: "stdio 为该进程的会话 ID"); the prefix carries no
# meaning and is there so that a session id is recognizable in a trace file beside the
# ``sb_`` of its sandbox.
SESSION_ID_PREFIX: Final = "se_"
_SESSION_ID_BYTES: Final = 16

# The signals a stdio process exits on (requirement 22.4). SIGINT is the client's
# interrupt, SIGTERM the way a supervisor stops the process; either leads to the same exit
# path as stdin reaching EOF (:meth:`StdioRuntime.stop`).
DEFAULT_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM)

# What the process reports on stderr once it has a sandbox (requirement 22.3): the sandbox
# id and the absolute path of the sandbox directory, which is where its trace.jsonl and,
# after the exit, its diff.json are.
SANDBOX_NOTICE: Final = "沙箱 {sandbox_id} 已创建，沙箱目录：{directory}"

# What a client is told when the sandbox of its initialize cannot be created: the dataset
# and the anchor date, as requirement 15.14 asks, and nothing of the reason - no path, no
# exception type (requirement 13.10). The filter turns it into JSON-RPC -32603.
SNAPSHOT_FAILED_MESSAGE: Final = "初始数据集 {dataset} 在锚点日期 {anchor_date} 的快照构建失败"

# What the service log gets of that failure: the reason, which the client does not see.
_CREATE_FAILED: Final = "无法为 initialize 创建沙箱（初始数据集 %s，锚点日期 %s）：%s"

# The signal that is ending the process, logged before the exit path runs.
_SIGNAL_RECEIVED: Final = "收到信号 %s，stdio 进程开始退出"

# A sandbox that was frozen before the exit - a trace event that could not be written
# freezes it (design.md error handling) - has a closed trace and gets no session_end; the
# diff is written all the same, its database being readable (requirement 15.12).
_ALREADY_FROZEN: Final = "沙箱 %s 已冻结，退出时不再写入 session_end，仅写出 diff.json"

# The two streams of one stdio connection, as ``stdio_server`` hands them over: what the
# client sent, with an Exception in place of a line that could not be parsed, and what the
# service writes back. A test injects a pair of memory streams of exactly these types and
# so replaces stdin and stdout (module docstring).
type StdioRead = MemoryObjectReceiveStream[SessionMessage | Exception]
type StdioWrite = MemoryObjectSendStream[SessionMessage]
type StdioStreams = tuple[StdioRead, StdioWrite]


@dataclass(frozen=True, slots=True)
class StdioOptions:
    """What the stdio command settled before its first message (requirement 14.2).

    - ``profile``: the tool profile the session sees, ``--profile`` or the instance's
      ``default_profile``;
    - ``dataset``: the initial dataset the sandbox is created from, ``--dataset`` or the
      instance's ``default_dataset``;
    - ``anchor_date``: ``--anchor-date``, the sandbox-level date of requirement 16.1 that
      wins over the clock configuration; None leaves the date to that configuration.

    The CLI resolves all three before it runs anything (task 24.4): a ``--profile`` or
    ``--dataset`` that the instance configuration does not have is a usage error and exits
    with 2 before a sandbox exists (requirement 14.12), which is why this carries the
    models rather than their names.
    """

    profile: ProfileModel
    dataset: DatasetModel
    anchor_date: date | None = None


class StdioRuntime:
    """The one session of one stdio process, from its first message to its diff.

    - ``loaded``: the validated instance configuration, which gives the ``instance`` of
      the diff and the ``clock`` an anchor date is resolved against;
    - ``schema``: the tables of that configuration, which the diff compares table by table;
    - ``manager``: the Sandbox_Manager the sandbox is created by and every job of it runs
      through (requirement 15.4);
    - ``handlers``: the session handlers of the process, which build the ``Server`` of this
      session once its sandbox exists;
    - ``options``: what the command settled beforehand (:class:`StdioOptions`);
    - ``streams``: the connection, None taking the process's stdin and stdout through the
      SDK's ``stdio_server`` (module docstring);
    - ``notices``: where the notice of requirement 22.3 is written, None taking
      ``sys.stderr`` at the moment it is written;
    - ``signals``: the signals that end the process, empty installing no handler
      (:meth:`stop`).

    One runtime per process and one :meth:`serve` per runtime: the session id is drawn when
    the runtime is made and the sandbox is created by the first initialize :meth:`serve`
    receives.
    """

    __slots__ = (
        "_handlers",
        "_instance_ref",
        "_loaded",
        "_manager",
        "_notices",
        "_options",
        "_sandbox",
        "_schema",
        "_session_id",
        "_signals",
        "_stop",
        "_streams",
    )

    def __init__(
        self,
        *,
        loaded: LoadedInstance,
        schema: StorageSchema,
        manager: SandboxManager,
        handlers: SessionHandlers,
        options: StdioOptions,
        streams: StdioStreams | None = None,
        notices: TextIO | None = None,
        signals: Sequence[signal.Signals] = DEFAULT_SIGNALS,
    ) -> None:
        self._loaded = loaded
        self._schema = schema
        self._manager = manager
        self._handlers = handlers
        self._options = options
        self._streams = streams
        self._notices = notices
        self._signals = tuple(signals)
        self._instance_ref = f"{loaded.instance.instance}@{loaded.instance.version}"
        self._session_id = SESSION_ID_PREFIX + secrets.token_hex(_SESSION_ID_BYTES)
        self._sandbox: SandboxRuntime | None = None
        self._stop: anyio.CancelScope | None = None

    @property
    def session_id(self) -> str:
        """The session id of this process, which every trace event of it names (17.2)."""
        return self._session_id

    @property
    def sandbox(self) -> SandboxRuntime | None:
        """The sandbox of this process, None until the first initialize created it."""
        return self._sandbox

    async def serve(self) -> None:
        """Serve the one session of this process and write what its exit writes.

        Returns when stdin has reached EOF or :meth:`stop` was called - by the signal
        watcher or by a caller - and not before the three steps of requirement 22.4 are
        done: the ``session_end``, the freeze and the ``diff.json``. Those steps run after
        the transport is closed and outside the scope a signal cancels, so a call that was
        in flight finishes first (design.md 7.3).

        Raises whatever the session could not handle itself: an unexpected failure of the
        initialize hook, a trace event that could not be written at the exit (which freezes
        the sandbox, design.md error handling) or a diff that could not be written. The
        exit path is not run after such a failure - the command reports it instead
        (task 24.4) - and a sandbox-level error of the session itself never reaches here:
        the SDK answers it (``raise_exceptions`` is false, design.md 7.2) and the session
        goes on.
        """
        async with anyio.create_task_group() as tg:
            self._stop = tg.cancel_scope
            try:
                await tg.start(self._watch_signals)
                async with self._transport() as (read, write):
                    await self._session(read, write)
            finally:
                # The session is over, so the signal watcher has nothing left to watch;
                # cancelling a scope that a signal already cancelled changes nothing.
                tg.cancel_scope.cancel()
        await self._finish()

    def stop(self) -> None:
        """End the session as a signal ends it, and let :meth:`serve` write the exit.

        The one door out: the signal watcher calls this and so may a caller that has no
        signal to send - the in-process tests above all (module docstring). Cancelling the
        scope of the session ends the transport, the filter and the SDK session; the exit
        path of requirement 22.4 runs afterwards, outside that scope. Calling it before
        :meth:`serve` or after it returns does nothing.
        """
        scope = self._stop
        if scope is not None:
            scope.cancel()

    # --- the connection ----------------------------------------------------------------

    def _transport(self) -> AbstractAsyncContextManager[StdioStreams]:
        """The streams of this session: the injected ones, or stdin and stdout.

        ``stdio_server`` wraps the process's stdin and stdout as UTF-8 text and reads and
        writes them in tasks of its own; it is entered inside the scope :meth:`stop`
        cancels, so a signal ends its reader rather than leaving the process waiting for a
        line that will not come.
        """
        given = self._streams
        return stdio_server() if given is None else _given_streams(given)

    async def _session(self, read: StdioRead, write: StdioWrite) -> None:
        """Run the filter and, once there is a sandbox, the session's server.

        Returns when the client's side has ended and the SDK session with it: the filter
        ends with ``read``, closes the stream the SDK reads, and this task group then waits
        for ``server.run`` to return. The ``initialize`` hook starts that server, which is
        why the group is passed on (module docstring).
        """
        to_sdk, filtered = anyio.create_memory_object_stream[SessionMessage | Exception](math.inf)
        async with anyio.create_task_group() as tg:
            hooks = FilterHooks(
                activity=self._mark_activity,
                initialize=self._initialize_hook(tg, filtered, write),
            )
            await message_filter(read, to_sdk, write, hooks)
        # The SDK session closes ``write`` itself when its receive loop ends
        # (``BaseSession._receive_loop``), and a process whose initialize never succeeded
        # started no session to do it. The writer of the transport ends with that stream,
        # so closing it here is what lets ``stdio_server`` return rather than wait for a
        # message that nobody is going to send; closing it twice does nothing.
        await write.aclose()

    async def _watch_signals(self, *, task_status: TaskStatus[None]) -> None:
        """Stop the session on the first of ``signals`` to arrive (requirement 22.4).

        ``task_status`` is reported once the handlers are installed, so :meth:`serve` reads
        no message before a signal can be caught. A runtime with no signals to watch
        reports and returns, which leaves the handlers of the process as they were.
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

    # --- the hooks the filter calls (design.md 7.1) ------------------------------------

    def _mark_activity(self) -> None:
        """Record that a request of this session arrived (requirement 15.5).

        Called for every request the filter sees, a refused one included, and before the
        first initialize has made a sandbox there is nothing to record it on. What reads
        the moment is the janitor of the Sandbox_Manager, which the serve command starts
        (design.md 7.4) and a stdio process does not: a stdio sandbox lives as long as its
        process, so the wiring is here for the one clock of the service rather than for a
        transition this process makes.
        """
        sandbox = self._sandbox
        if sandbox is not None:
            sandbox.mark_activity(self._manager.clock.monotonic())

    def _initialize_hook(
        self, tg: TaskGroup, filtered: StdioRead, write: StdioWrite
    ) -> InitializeHook:
        """The hook every initialize of this session goes through (design.md 7.3).

        The first one creates the sandbox and starts the server that will answer the
        request behind it; every later one keeps that sandbox and writes one more
        ``session_start`` (requirement 15.2).
        """

        async def hook(request: types.JSONRPCRequest) -> None:
            sandbox = self._sandbox
            if sandbox is not None:
                await self._session_start(sandbox)
                return
            sandbox = await self._create()
            self._sandbox = sandbox
            sandbox.bind(self._session_id)
            await self._session_start(sandbox)
            self._report(sandbox)
            tg.start_soon(self._run_server, sandbox, filtered, write)

        return hook

    async def _create(self) -> SandboxRuntime:
        """Create the one sandbox of this process (requirements 15.1 and 15.2).

        The anchor date is resolved first, by the priority of requirement 16.1, because
        the error names it. Raises :class:`InitializeFailed` where the sandbox could not be
        created, which the filter answers with -32603 (requirement 15.14): the reason - the
        Snapshot of the dataset would not build, above all - goes to the service log, where
        the dataset file and the configuration path belong, and the client is told the
        dataset and the date. What failed is identified by where it failed rather than by
        its class: the Snapshot reaches the gateway through
        :class:`~erp_mock.gateway.sandbox.SnapshotSource`, which is what keeps the seed
        layer out of this package (design.md 6.2), so the exception of a build is not a
        name this module knows. The one other way a creation can fail - a service
        configured to hold no sandbox at all
        (:class:`~erp_mock.gateway.sandbox.SandboxLimitReached`, which a fresh process
        cannot reach otherwise) - is answered with the same message, requirement 15.14
        being the only answer the transport has for an initialize without a sandbox.
        """
        options = self._options
        anchor = resolve_anchor(
            options.anchor_date, self._loaded.instance.clock, self._manager.clock
        )
        try:
            return await self._manager.create(options.profile, options.dataset, anchor=anchor)
        except Exception as error:
            _LOG.warning(_CREATE_FAILED, options.dataset.name, anchor.isoformat(), error)
            raise InitializeFailed(
                SNAPSHOT_FAILED_MESSAGE.format(
                    dataset=options.dataset.name, anchor_date=anchor.isoformat()
                )
            ) from error

    async def _run_server(
        self, sandbox: SandboxRuntime, filtered: StdioRead, write: StdioWrite
    ) -> None:
        """Answer the requests of this session until the client's side ends.

        One low-level ``Server`` for the one session of the process, built on the sandbox
        the first initialize created; ``raise_exceptions`` stays false, as on the other
        transport (design.md 7.2), so a handler that fails ends the request and not the
        session. Returns when ``filtered`` ends, which the filter closes when stdin does.
        """
        session = McpSessionContext.of(self._session_id, sandbox)
        await self._handlers.build_server(session).run(filtered, write, INIT_OPTIONS)

    async def _session_start(self, sandbox: SandboxRuntime) -> None:
        """Write the ``session_start`` of one accepted initialize (requirement 17.9).

        Inside the sandbox lock, like every other event of a sandbox, and before the
        initialize reaches the SDK: the event is in the file before the response goes out
        (requirement 17.6).
        """
        await self._manager.run(sandbox, self._start_job)

    def _start_job(self, sandbox: SandboxRuntime) -> int:
        """Append the ``session_start`` event (worker thread)."""
        return sandbox.trace.write(EventName.SESSION_START, self._session_id)

    def _report(self, sandbox: SandboxRuntime) -> None:
        """Report the new sandbox on stderr (requirement 22.3).

        The sandbox id and the absolute path of the sandbox directory, which is what a
        caller needs to find the trace and the diff of the run. ``sys.stderr`` is read at
        this moment rather than when the runtime was made, so a caller that replaced it
        sees the notice.
        """
        stream = sys.stderr if self._notices is None else self._notices
        notice = SANDBOX_NOTICE.format(
            sandbox_id=sandbox.sandbox_id, directory=sandbox.directory.resolve()
        )
        stream.write(notice + "\n")
        stream.flush()

    # --- the exit (requirement 22.4) ---------------------------------------------------

    async def _finish(self) -> None:
        """Write the ``session_end``, freeze the sandbox and write the diff, in that order.

        Nothing happens for a process that never received an initialize: there is no
        sandbox to end, to freeze or to compare (module docstring). The ``session_end`` and
        the diff each run through the Sandbox_Manager, so the first of them waits for a
        call that is still in flight and both run in the shared thread pool
        (design.md 7.5).
        """
        sandbox = self._sandbox
        if sandbox is None:
            return
        if sandbox.status == "frozen":
            _LOG.warning(_ALREADY_FROZEN, sandbox.sandbox_id)
        else:
            await self._manager.run(sandbox, self._end_job)
        await self._manager.freeze(sandbox)
        await self._manager.run(sandbox, self._diff_job, activity=False, allow_frozen=True)

    def _end_job(self, sandbox: SandboxRuntime) -> int:
        """Append the ``session_end`` event (worker thread, requirement 17.9)."""
        return sandbox.trace.write(EventName.SESSION_END, self._session_id)

    def _diff_job(self, sandbox: SandboxRuntime) -> None:
        """Write ``<sandbox>/diff.json`` against the initial dataset (worker thread).

        The basis is ``base.sqlite``, the Snapshot the sandbox was created from, and the
        ``base`` of the output is therefore ``initial`` (requirement 18.1); the four values
        that say which sandbox this is come from the runtime and the instance
        configuration. The text is the one the Admin API's diff endpoint sends, byte for
        byte (module docstring).
        """
        body = compute_diff(sandbox.store.db_path, sandbox.base_db, self._schema)
        payload = body.as_json(
            sandbox_id=sandbox.sandbox_id,
            instance=self._instance_ref,
            dataset=sandbox.dataset_ref,
            anchor_date=sandbox.anchor_date,
        )
        (sandbox.directory / DIFF_FILENAME).write_bytes(encode_utf8(dumps_pretty(payload)))


async def run_stdio(
    *,
    loaded: LoadedInstance,
    schema: StorageSchema,
    manager: SandboxManager,
    handlers: SessionHandlers,
    options: StdioOptions,
    streams: StdioStreams | None = None,
    notices: TextIO | None = None,
    signals: Sequence[signal.Signals] = DEFAULT_SIGNALS,
) -> SandboxRuntime | None:
    """Serve one stdio session and return the sandbox it served (design.md 7.3).

    What the stdio command runs once its options are parsed and its instance configuration
    is loaded (task 24.4): the arguments are the pieces of the service, already assembled,
    and :class:`StdioOptions` is what the command line settled. Returns when stdin reached
    EOF or a signal arrived and the exit of requirement 22.4 is written, which is the
    command's exit code 0; the sandbox it returns is the one of the process, None where no
    initialize ever arrived.

    See :class:`StdioRuntime` for the arguments and for the exceptions this lets through.
    """
    runtime = StdioRuntime(
        loaded=loaded,
        schema=schema,
        manager=manager,
        handlers=handlers,
        options=options,
        streams=streams,
        notices=notices,
        signals=signals,
    )
    await runtime.serve()
    return runtime.sandbox


@asynccontextmanager
async def _given_streams(streams: StdioStreams) -> AsyncIterator[StdioStreams]:
    """Hand over streams a caller already has, in the shape ``stdio_server`` has.

    The streams belong to the caller, which is what opened them and what closes them; this
    only puts them where :meth:`StdioRuntime._transport` expects a context manager.
    """
    yield streams
