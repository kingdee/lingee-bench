"""Probe: two uvicorn servers on one event loop under the process's own signal handling
(design, open item V7).

serve runs the MCP server and the Admin server (127.0.0.1 only) as two ``uvicorn.Server``
instances on one asyncio event loop, on sockets it binds before starting them, so both
apps share the in-process state (design, component 7.4). The process handles SIGINT and
SIGTERM itself: it sets ``should_exit`` on both servers and then freezes the sandboxes, so
uvicorn's own signal capture must be off. The probe child process binds two loopback
sockets, starts both servers in one anyio task group with
``tg.start_soon(server.serve, [sock])``, waits for a signal through
``anyio.open_signal_receiver``, sets ``should_exit`` on both servers, and reports on stdout
what it saw; the parent requests both ports, then sends the signal.

Findings on uvicorn 0.54.0 (HTTP protocol "auto" resolves to h11 0.16.0, httptools is not
installed), anyio 4.15.1 (asyncio backend), CPython 3.12:

V7 holds. The override point is ``Server.capture_signals``; uvicorn 0.54.0 has no
``install_signal_handlers`` (the name survives only in a comment in uvicorn/workers.py).
``Server.serve`` runs ``_serve`` inside ``with self.capture_signals():``, a
``contextlib.contextmanager`` method. On the main thread it installs ``self.handle_exit``
for SIGINT and SIGTERM (SIGBREAK too on Windows) with ``signal.signal``, never with
``loop.add_signal_handler``; on exit it restores the handlers it found on entry and then
re-raises every signal it caught with ``signal.raise_signal``, latest first. Off the main
thread it does nothing. A subclass whose ``capture_signals`` only yields switches the
capture off (``QuietServer`` below, the class task 23.4 uses):

    class QuietServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

With two QuietServers on one loop, each given its pre-bound socket, both answer HTTP and
share the process's state. While they serve, the SIGINT and SIGTERM handlers are the
process's own: anyio's ``open_signal_receiver`` registers them with
``loop.add_signal_handler``, so ``signal.getsignal`` shows asyncio's ``_sighandler_noop``
and the loop learns of each signal through its wakeup fd. SIGINT or SIGTERM reaches the
process's handler exactly once and uvicorn's ``handle_exit`` never. After ``should_exit``
is set on both servers, both ``serve()`` calls return within about 0.2 s (one 0.1 s tick
of ``main_loop`` plus the 0.1 s pause in ``shutdown``, with no connection open),
``force_exit`` stays False, no signal is re-raised, the handlers stay the process's own,
and the process exits with code 0 and no KeyboardInterrupt.

Control, plain ``uvicorn.Server``: ``serve`` enters ``capture_signals`` before its first
await, so the server started last replaces the process's handlers with its
``handle_exit``. A SIGTERM then reaches that handler and, because the C-level handler
still writes to the wakeup fd, the process's handler as well. On shutdown each server
restores the handlers it found on entry (the second server restores the first one's
``handle_exit``) and re-raises the signal, which reaches the first server's
``handle_exit`` and the process's handler a second time. Which server finishes first is a
matter of timing; in every run the mcp server, started first, also finished first, so the
admin server's restore left the mcp server's stale ``handle_exit`` installed for both
signals. A handler installed with ``signal.signal`` instead of the loop would be replaced
outright and called only by the re-raise after shutdown (from the source; not exercised).

Details for task 23.4:

* ``serve([sock])`` hands each socket to ``loop.create_server(sock=..., backlog=...)``,
  which calls ``listen``; the Config's host, port, fd and uds are ignored, no "Uvicorn
  running on" line is logged (serve logs the addresses itself if wanted), and
  ``Server.shutdown`` closes the passed sockets. Bind like ``Config.bind_socket``:
  ``SO_REUSEADDR``, and AF_INET6 when the host contains ':' (the MCP --host may be IPv6).
  With pre-bound sockets and ``lifespan="off"``, neither of the ``sys.exit`` calls in
  ``Server.startup`` (lifespan failure, bind failure) can be reached.
* ``Config.load`` (run by ``serve``) calls the app once with no arguments to detect an app
  factory and keeps the app when that raises TypeError, so the ASGI callables given to
  Config must require their three arguments. ``proxy_headers`` defaults to True, so a
  request from 127.0.0.1 can rewrite ``scope["client"]`` and ``scope["scheme"]`` through
  X-Forwarded-For and X-Forwarded-Proto; pass ``proxy_headers=False`` unless that is
  wanted (both from the source).
* ``main_loop`` checks ``should_exit`` every 0.1 s; ``shutdown`` then closes the listeners,
  closes idle keep-alive connections, lets in-flight requests finish with keep-alive off,
  and waits until the connections are gone and the request tasks are done. Without
  ``timeout_graceful_shutdown`` (default None) that wait has no limit; ``force_exit`` only
  skips uvicorn's polling, as ``asyncio.Server.wait_closed`` still waits for every active
  connection. In-flight MCP requests wait on session tasks, so cancel the session tasks
  and the janitor (which never ends on its own, so the task group would not either) and
  freeze the sandboxes only after both ``serve()`` calls have returned.
* ``loop.add_signal_handler``, and with it ``open_signal_receiver``, raises RuntimeError
  off the main thread. Give serve a stop trigger besides the signals (an ``anyio.Event``,
  say) so tests/integration/test_serve_inprocess.py can stop it without a signal, and use
  QuietServer in in-process tests too: a plain ``uvicorn.Server`` on pytest's main thread
  takes over pytest's SIGINT handler while it runs.

Fallback if a test fails after a version change: run each server on its own event loop in
its own thread (``capture_signals`` does nothing off the main thread) and keep the signal
handling on the main thread.
"""

import contextlib
import http.client
import inspect
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

import anyio
import pytest
import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send

REPO_ROOT = Path(__file__).resolve().parents[2]

HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)
# Start order of the two servers, as in design component 7.4.
SERVER_NAMES = ("mcp", "admin")

# Parent side: the child's start-up (interpreter, imports, both servers), one HTTP request,
# and the child's exit after the signal.
STARTUP_TIMEOUT_S = 30.0
HTTP_TIMEOUT_S = 5.0
EXIT_TIMEOUT_S = 30.0
# Child side: give up if no signal arrives, so the child never outlives a crashed parent
# for long.
CHILD_DEADLINE_S = 90.0
# How long the child keeps listening for further deliveries after both servers returned.
DRAIN_S = 0.5


class QuietServer(uvicorn.Server):
    """A uvicorn server that leaves SIGINT and SIGTERM to the embedding process.

    ``Server.serve`` enters ``capture_signals`` around the whole server run; this one does
    nothing. Stop the server by setting ``should_exit``.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class RecordingServer(uvicorn.Server):
    """Records the signals that reach uvicorn's own handler, ``Server.handle_exit``."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.uvicorn_handled: list[str] = []

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self.uvicorn_handled.append(signal.Signals(sig).name)
        super().handle_exit(sig, frame)


class QuietRecordingServer(RecordingServer, QuietServer):
    """QuietServer with the recorder: the subject of the probe."""


SERVER_CLASSES: dict[str, type[RecordingServer]] = {
    "quiet": QuietRecordingServer,
    "plain": RecordingServer,
}


# ---------------------------------------------------------------------------
# Child process
# ---------------------------------------------------------------------------

CHILD_ENTRY = (
    "import sys\nfrom tests.probes.test_uvicorn import child_main\nchild_main(sys.argv[1])\n"
)


def child_main(mode: str) -> None:
    """Entry point of the child process; ``mode`` is a key of SERVER_CLASSES."""
    anyio.run(serve_until_signal, SERVER_CLASSES[mode])


def emit(**record: object) -> None:
    """Write one JSON record line to stdout (the parent reads it from a file)."""
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def bind_loopback() -> socket.socket:
    """Bind a TCP socket to a free loopback port the way ``Config.bind_socket`` does."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


def counting_app(name: str, hits: list[str]) -> ASGIApp:
    """An ASGI app that answers with its name and the request count it shares with others."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] == "http"
        hits.append(name)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": f"{name} {len(hits)}".encode()})

    return app


def describe_handler(handler: object, own: object, servers: Mapping[str, uvicorn.Server]) -> str:
    """Name the installed handler: "own", "<server>.handle_exit", or its repr."""
    if handler == own:
        return "own"
    for name, server in servers.items():
        if handler == server.handle_exit:
            return f"{name}.handle_exit"
    return repr(handler)


async def serve_until_signal(server_class: type[RecordingServer]) -> None:
    """Serve both apps until SIGINT or SIGTERM, stop both servers, and report on stdout.

    The start-up and the stop follow serve in design component 7.4: pre-bound sockets,
    one task group, the process's own signal receiver, ``should_exit`` on both servers.
    """
    hits: list[str] = []
    sockets = {name: bind_loopback() for name in SERVER_NAMES}
    ports = {name: sock.getsockname()[1] for name, sock in sockets.items()}
    servers = {
        name: server_class(
            uvicorn.Config(
                counting_app(name, hits), lifespan="off", access_log=False, log_config=None
            )
        )
        for name in SERVER_NAMES
    }
    returned: list[str] = []

    async def run_server(name: str) -> None:
        await servers[name].serve([sockets[name]])
        returned.append(name)

    with anyio.fail_after(CHILD_DEADLINE_S):
        with anyio.open_signal_receiver(*HANDLED_SIGNALS) as signals:
            own = {sig: signal.getsignal(sig) for sig in HANDLED_SIGNALS}

            def handlers() -> dict[str, str]:
                return {
                    sig.name: describe_handler(signal.getsignal(sig), own[sig], servers)
                    for sig in HANDLED_SIGNALS
                }

            async with anyio.create_task_group() as tg:
                for name in SERVER_NAMES:
                    tg.start_soon(run_server, name)
                while not all(server.started for server in servers.values()):
                    await anyio.sleep(0.01)
                emit(
                    event="ready",
                    ports=ports,
                    own={sig.name: getattr(own[sig], "__name__", repr(own[sig])) for sig in own},
                    handlers=handlers(),
                )
                received = await anext(signals)
                for server in servers.values():
                    server.should_exit = True
                exit_requested = time.monotonic()
            # The task group has ended: both serve() calls have returned.
            stopped_after_s = time.monotonic() - exit_requested
            handlers_after = handlers()
            extra: list[str] = []
            with anyio.move_on_after(DRAIN_S):
                async for sig in signals:
                    extra.append(sig.name)
    emit(
        event="done",
        received=received.name,
        extra=extra,
        handlers_after=handlers_after,
        stopped_after_s=round(stopped_after_s, 3),
        hits=hits,
        servers={
            name: {
                "started": server.started,
                "should_exit": server.should_exit,
                "force_exit": server.force_exit,
                "returned": name in returned,
                "socket_closed": sockets[name].fileno() == -1,
                "uvicorn_handled": server.uvicorn_handled,
            }
            for name, server in servers.items()
        },
    )


# ---------------------------------------------------------------------------
# Parent side
# ---------------------------------------------------------------------------


@dataclass
class Child:
    proc: subprocess.Popen[bytes]
    stdout: Path
    stderr: Path

    def stderr_text(self) -> str:
        return self.stderr.read_text(encoding="utf-8", errors="replace")

    def records(self) -> list[dict[str, Any]]:
        text = self.stdout.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines(keepends=True) if line.endswith("\n")]

    def wait_for(self, event: str, timeout: float) -> dict[str, Any]:
        """Return the child's first record of ``event``, failing once it exits without one."""
        deadline = time.monotonic() + timeout
        while True:
            exited = self.proc.poll() is not None
            for record in self.records():
                if record["event"] == event:
                    return record
            if exited or time.monotonic() >= deadline:
                pytest.fail(
                    f"no {event!r} record from the child (exit code {self.proc.returncode});"
                    f" stderr:\n{self.stderr_text()}"
                )
            time.sleep(0.02)

    def stop(self, sig: signal.Signals) -> dict[str, Any]:
        """Send ``sig``, wait for the child to exit with code 0, and return its "done" record."""
        self.proc.send_signal(sig)
        returncode = self.proc.wait(timeout=EXIT_TIMEOUT_S)
        assert returncode == 0, self.stderr_text()
        assert "Traceback" not in self.stderr_text()
        return self.wait_for("done", 0)


@contextlib.contextmanager
def child_process(tmp_path: Path, mode: str) -> Iterator[Child]:
    stdout = tmp_path / f"{mode}-stdout.jsonl"
    stderr = tmp_path / f"{mode}-stderr.txt"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    with stdout.open("wb") as out, stderr.open("wb") as err:
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD_ENTRY, mode],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
        )
    try:
        yield Child(proc, stdout, stderr)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=EXIT_TIMEOUT_S)


def http_get(port: int) -> str:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HTTP_TIMEOUT_S)
    try:
        conn.request("GET", "/")
        response = conn.getresponse()
        assert response.status == 200
        return response.read().decode()
    finally:
        conn.close()


def request_both(ready: Mapping[str, Any]) -> list[str]:
    """Request the MCP port, then the Admin port; the bodies show the shared count."""
    return [http_get(ready["ports"][name]) for name in SERVER_NAMES]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.req("19.1", "22.1")
def test_locked_uvicorn_installs_its_handlers_in_capture_signals() -> None:
    # serve() looks the context manager up on self, so a subclass override takes effect.
    assert "capture_signals" in uvicorn.Server.serve.__code__.co_names
    assert inspect.isgeneratorfunction(inspect.unwrap(uvicorn.Server.capture_signals))
    assert not hasattr(uvicorn.Server, "install_signal_handlers")


@pytest.mark.req("19.1", "22.1")
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=lambda sig: sig.name)
def test_quiet_servers_on_one_loop_leave_signals_to_the_process(
    tmp_path: Path, sig: signal.Signals
) -> None:
    with child_process(tmp_path, "quiet") as child:
        ready = child.wait_for("ready", STARTUP_TIMEOUT_S)
        # Both servers answer on their pre-bound sockets and share the process's state.
        assert request_both(ready) == ["mcp 1", "admin 2"]
        done = child.stop(sig)
    # The process's handlers came from loop.add_signal_handler, and uvicorn replaced none.
    assert ready["own"] == {"SIGINT": "_sighandler_noop", "SIGTERM": "_sighandler_noop"}
    assert ready["handlers"] == {"SIGINT": "own", "SIGTERM": "own"}
    # The signal reached the process's handler once, uvicorn's handler never, and nothing
    # re-raised it after the servers stopped.
    assert done["received"] == sig.name
    assert done["extra"] == []
    assert done["handlers_after"] == {"SIGINT": "own", "SIGTERM": "own"}
    # Setting should_exit stopped both servers; each closed the socket it was given.
    for name in SERVER_NAMES:
        assert done["servers"][name] == {
            "started": True,
            "should_exit": True,
            "force_exit": False,
            "returned": True,
            "socket_closed": True,
            "uvicorn_handled": [],
        }
    assert done["hits"] == ["mcp", "admin"]


@pytest.mark.req("19.1", "22.1")
def test_plain_servers_on_one_loop_capture_signals_themselves(tmp_path: Path) -> None:
    """Control: without the override, uvicorn takes over SIGINT and SIGTERM."""
    with child_process(tmp_path, "plain") as child:
        ready = child.wait_for("ready", STARTUP_TIMEOUT_S)
        assert request_both(ready) == ["mcp 1", "admin 2"]
        done = child.stop(signal.SIGTERM)
    # The server started last installed its handle_exit over the process's handlers.
    assert ready["handlers"] == {"SIGINT": "admin.handle_exit", "SIGTERM": "admin.handle_exit"}
    # SIGTERM reached that handler and, through the wakeup fd, the process's handler.
    assert done["received"] == "SIGTERM"
    assert done["servers"]["admin"]["uvicorn_handled"] == ["SIGTERM"]
    # The admin server restored the mcp server's handle_exit and re-raised the signal on
    # its way out, so the mcp server's handler and the process's handler saw it again.
    assert done["servers"]["mcp"]["uvicorn_handled"] == ["SIGTERM"]
    assert all(done["servers"][name]["returned"] for name in SERVER_NAMES)
    # Which server finishes first is a matter of timing. If the mcp server does, the admin
    # server's restore leaves the mcp server's stale handle_exit installed; if the admin
    # server does, the mcp server then restores the process's handlers and re-raises the
    # signal it caught from the admin server's re-raise, a third delivery.
    stale = {"SIGINT": "mcp.handle_exit", "SIGTERM": "mcp.handle_exit"}
    own = {"SIGINT": "own", "SIGTERM": "own"}
    assert (done["extra"], done["handlers_after"]) in [
        (["SIGTERM"], stale),
        (["SIGTERM", "SIGTERM"], own),
    ]
