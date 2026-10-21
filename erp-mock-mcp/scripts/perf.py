"""Performance measurement command (requirement 25.11).

Run it from the repository root::

    uv run --locked python scripts/perf.py --instance instances/p2p-demo

It measures the first three figures of engineering.md section 6 on one real service - a
``erp-mock-mcp serve`` subprocess over ``instances/p2p-demo``, reached by the official MCP
Python SDK client over Streamable HTTP and by the Admin API over HTTP:

=========================  ====================================================  ==========
metric                     what is measured                                      default n
=========================  ====================================================  ==========
:data:`STARTUP`            wall clock from starting the serve process until both  1
                           of its ports serve: the Admin port answers
                           ``GET /admin/health`` with 200 and the MCP port
                           answers a request of its own (:meth:`Service.serving`).
                           Loading and validating the instance configuration and
                           building the Snapshot of the initial dataset are inside
                           this, which is what engineering.md means by service
                           startup
:data:`SANDBOX_P95`        p95 of the duration of one ``initialize`` on ``/mcp``,  100
                           each in a session of its own, where the service creates
                           a sandbox for the new session (requirement 15.1):
                           measured by the client around the call, which is
                           "initialize 到可用". Every session is ended before the
                           next one starts
:data:`CALL_P95`           p95 of the server-side processing time of one           1000
                           tools/call: the ``latency_ms`` of the ``tools_call``
                           events of the sandbox's trace (requirement 17.3), read
                           from ``GET /admin/sandboxes/{id}/trace`` after the
                           session ended. Not the round trip the client sees, so
                           the client, the transport and this script's own loop
                           are outside the figure
=========================  ====================================================  ==========

``--initializes`` and ``--calls`` set the two sample counts, which is what lets
tests/static/test_perf_script.py run the whole command with a handful of samples; the
defaults are the 100 and the 1000 of requirement 25.11. The p95 is the nearest-rank one
(:func:`percentile`): the sample at ``ceil(0.95 * n)`` of the sorted samples, so the figure
is always one measured value and no interpolation.

The resource limits, and why they are raised (requirement 23.12). With the defaults - 500
calls per sandbox and 20 calls per second per session - the 1000 calls of the third metric
could not be made at all: the five hundred and first would be refused with LIMIT_EXCEEDED,
and twenty calls per second would stretch the measurement over fifty seconds of waiting
that has nothing to do with how long a call takes. The two ways out are spreading the calls
over several sessions and sandboxes, or raising the limits of the process under measurement;
this script raises them (:func:`service_env`), because the alternative would measure
something else: a sandbox that is replaced every 500 calls, and a client that sleeps between
calls, make the samples depend on the pacing rather than on the service. The limits are
raised to exactly one more than the measurement needs, and the trace is checked afterwards
(:func:`call_latencies`): a run in which any call was refused by a resource limit, or ended
with any other error, reports what happened and exits with 1 rather than computing a p95
over the calls that were let through.

What is measured, and what is deliberately not:

- the tool of the third metric is ``business_context_get``, the one tool every profile has
  (requirement 16.10). It is read-only, so the thousand calls leave the sandbox database the
  size it was and the samples stay comparable with each other, and it runs the whole
  fourteen step pipeline of requirement 12.1 including the transaction;
- the calls go out one after another in one session, which is the Request_Sequence shape of
  the requirements; this script measures no concurrency (the fourth figure of
  engineering.md section 6 is not part of requirement 25.11);
- the measurement is not a check: it has no thresholds, and nothing of this file belongs to
  ``scripts/check.py`` (requirement 25.11).

No system clock is read for anything but durations (:func:`time.perf_counter` and
:func:`time.monotonic`): the clock of p2p-demo is in mode today and serve has no
``--anchor-date``, so the anchor date is the service's own business and this script never
computes a date (requirement 25.10). Every wait is a poll against a deadline
(:func:`Service.wait_until_serving`), the service is always stopped with SIGTERM, and a
``--data-dir`` this script made itself is removed when it is done.

This script writes its own lines to stderr, as ``scripts/check.py`` does, and leaves stdout
to the three metric lines, one per line, each with its name, its value, its unit and its
sample count (:meth:`Metric.line`). Exit code 0 means the three figures were measured; 1
means they were not, and the reason is on stderr.
"""

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import anyio
import httpx
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from erp_mock.cli.options import (
    MAX_CALLS_PER_SECOND_VAR,
    MAX_SANDBOX_CALLS_VAR,
    MAX_SANDBOXES_VAR,
)
from erp_mock.cli.serve import TOKEN_FILENAME
from erp_mock.common.errors import ErrorCode
from erp_mock.gateway.router import sandbox_path
from erp_mock.gateway.serve import ADMIN_HOST, bind_socket
from erp_mock.gateway.trace import EventName
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# What is measured by default: the example instance and the example business packs of the
# repository (requirement 25.11), served by the console script of ``[project.scripts]``
# beside the interpreter this script runs on - the command a deployer types.
DEFAULT_INSTANCE: Final = REPO_ROOT / "instances" / "p2p-demo"
DEFAULT_PACKS_DIR: Final = REPO_ROOT / "packs"
COMMAND: Final = Path(sys.executable).parent / "erp-mock-mcp"

# The two sample counts of requirement 25.11.
DEFAULT_INITIALIZES: Final = 100
DEFAULT_CALLS: Final = 1000

# The quantile of the second and the third metric.
P95: Final = 0.95

# The names of the three metrics, which are the names of the three lines on stdout.
STARTUP: Final = "service_startup_ms"
SANDBOX_P95: Final = "sandbox_create_p95_ms"
CALL_P95: Final = "tool_call_p95_ms"

# The unit of all three: whole and fractional milliseconds. ``latency_ms`` of a trace event
# is whole milliseconds (requirement 17.3), so the third figure is an integer in a float.
UNIT: Final = "ms"

# The variables a serve subprocess is given beyond what this script sets: enough to run, and
# not one of the ``ERP_MOCK_*`` the environment of this process may carry, which would
# otherwise decide the limits of the service under measurement (requirement 23.9).
PASSED_THROUGH: Final = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")

# Where the service listens: the loopback address, which needs no MCP endpoint token
# (requirement 23.2), and which is the only address an Admin port is ever on (19.1).
LOOPBACK: Final = ADMIN_HOST

# The addresses this script uses (requirements 14.2, 19.3, 19.7 and 19.9).
MCP_PATH: Final = "/mcp"
HEALTH_PATH: Final = "/admin/health"
SANDBOXES_PATH: Final = "/admin/sandboxes"

# How the waits are bounded. The startup poll is fine grained because its interval is the
# resolution of the first metric; a service that has not come up within the deadline, or a
# session that has not finished within its own, has already failed the measurement.
POLL_SECONDS: Final = 0.01
START_TIMEOUT_S: Final = 120.0
STOP_TIMEOUT_S: Final = 30.0
ADMIN_TIMEOUT_S: Final = 60.0

# The deadline of one measurement run, as a fixed part plus a part per sample: creating a
# sandbox and performing a call are milliseconds, so these are wide enough to be reached
# only by a service that stopped answering.
SESSION_SETUP_S: Final = 60.0
PER_INITIALIZE_S: Final = 1.0
PER_CALL_S: Final = 0.2

# What the client calls itself in the handshake of every session this script opens.
CLIENT_INFO: Final = types.Implementation(name="erp-mock-perf", version="0.0.0")

# The one tool the third metric calls, and the request that calls it (module docstring).
# Built once: ``send_request`` serializes the request and does not change it.
CALL_REQUEST: Final = types.ClientRequest(
    types.CallToolRequest(
        params=types.CallToolRequestParams(name=BUSINESS_CONTEXT_GET, arguments={})
    )
)

# The keys of a ``tools_call`` trace event this script reads (requirement 17.3).
EVENT_KEY: Final = "event"
LATENCY_KEY: Final = "latency_ms"
IS_ERROR_KEY: Final = "is_error"
ERROR_CODE_KEY: Final = "error_code"
TOOL_KEY: Final = "tool"


def _say(message: str) -> None:
    """Write one line of this script's own output to stderr."""
    sys.stderr.write(f"==> {message}\n")
    sys.stderr.flush()


class MeasurementFailed(Exception):
    """A measurement that could not be made, and why; :func:`main` reports and exits 1."""


# --- the command line --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Options:
    """What one run of this script was asked to measure.

    - ``instance`` and ``packs_dir``: what the service under measurement serves, the
      example instance and the example packs by default (requirement 25.11);
    - ``data_dir``: the ``--data-dir`` of that service, None for a temporary directory this
      script creates and removes again (:func:`data_directory`);
    - ``initializes`` and ``calls``: the sample counts of the second and the third metric.

    The three paths are absolute: :func:`parse_args` resolves what the command line gave
    against the working directory of this script, as the CLI resolves its own relative paths
    (requirement 22.1), so what the subprocess is given cannot depend on the directory it is
    started in.
    """

    instance: Path
    packs_dir: Path
    data_dir: Path | None
    initializes: int
    calls: int


def positive_int(value: str) -> int:
    """``value`` as a positive integer, for the two sample counts of the command line."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"必须是正整数，收到 {value!r}")
    return number


def parse_args(argv: Sequence[str] | None = None) -> Options:
    """The options of one run; a usage error ends the process with exit code 2."""
    parser = argparse.ArgumentParser(
        prog="scripts/perf.py",
        description=(
            "测量服务启动耗时、沙箱创建耗时 p95 和工具调用服务端处理时间 p95（需求 25.11）；"
            "测量结果不作为本地检查命令的通过条件。"
        ),
    )
    parser.add_argument(
        "--instance",
        type=Path,
        default=DEFAULT_INSTANCE,
        help="被测实例配置目录，缺省为 instances/p2p-demo。",
    )
    parser.add_argument(
        "--packs-dir",
        type=Path,
        default=DEFAULT_PACKS_DIR,
        help="业务包根目录，缺省为 packs。",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="被测服务的 --data-dir，缺省为本脚本创建并在结束时删除的临时目录。",
    )
    parser.add_argument(
        "--initializes",
        type=positive_int,
        default=DEFAULT_INITIALIZES,
        help=f"沙箱创建耗时的样本数，即 initialize 的次数，缺省 {DEFAULT_INITIALIZES}。",
    )
    parser.add_argument(
        "--calls",
        type=positive_int,
        default=DEFAULT_CALLS,
        help=f"工具调用的样本数，缺省 {DEFAULT_CALLS}。",
    )
    args = parser.parse_args(argv)
    return Options(
        instance=Path(args.instance).resolve(),
        packs_dir=Path(args.packs_dir).resolve(),
        data_dir=None if args.data_dir is None else Path(args.data_dir).resolve(),
        initializes=args.initializes,
        calls=args.calls,
    )


# --- the three figures -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metric:
    """One measured figure: its name, its value in milliseconds and its sample count."""

    name: str
    value: float
    samples: int
    what: str

    @property
    def line(self) -> str:
        """The one line this metric is reported on, name and unit included."""
        return f"{self.name:<22} {self.value:>10.1f} {UNIT}  n={self.samples:<5}  {self.what}"


def percentile(samples: Sequence[float], quantile: float) -> float:
    """The nearest-rank ``quantile`` of ``samples``: no interpolation, a measured value.

    The sample at rank ``ceil(quantile * n)`` of the sorted samples, counting from 1, which
    for n of 100 and a quantile of 0.95 is the 95th of them.

    Raises MeasurementFailed for an empty sequence, which is a run that measured nothing.
    """
    if not samples:
        raise MeasurementFailed("没有样本可用于计算分位数")
    ordered = sorted(samples)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


# --- the service under measurement -------------------------------------------------------


@dataclass(slots=True)
class Service:
    """The serve subprocess this script measures, and what it is asked for.

    ``admin`` and ``mcp`` are one HTTP client per port, built before the process is started
    (:func:`service`) and reused for every request this script makes, the polls of the
    startup wait included. One client per request would be the natural way to write this and
    it is the wrong way here: building a client builds a TLS context and a connection pool,
    which is milliseconds of this script's own work, and a poll loop that pays that every
    ten milliseconds competes with the service for the processor and lands in the first
    metric. With the clients built beforehand a poll is two loopback round trips.

    ``token`` is the Admin token the process generated and wrote to its token file
    (requirement 23.5), which is how this script reaches the Admin port; it is empty until
    :meth:`serving` has found the file, and it becomes the Authorization header of ``admin``
    (requirement 19.2).
    """

    process: subprocess.Popen[bytes]
    data_dir: Path
    port: int
    admin_port: int
    err: Path
    admin: httpx.Client
    mcp: httpx.Client
    token: str = ""
    startup_ms: float = field(default=0.0)

    @property
    def stderr(self) -> str:
        """What the process has written to stderr: its log (design.md 7.3)."""
        return self.err.read_text(encoding="utf-8")

    @property
    def token_file(self) -> Path:
        """``<data-dir>/admin-token``, written before the ports are bound (req 23.5)."""
        return self.data_dir / TOKEN_FILENAME

    def mcp_url(self, sandbox_id: str | None = None) -> str:
        """An MCP address of this process (requirements 14.2 and 15.3).

        None is ``/mcp``, where an initialize creates a sandbox of its own; a sandbox id is
        ``/s/<sandbox_id>/mcp``, the address of that prebuilt sandbox.
        """
        return self.mcp_port_url(MCP_PATH if sandbox_id is None else sandbox_path(sandbox_id))

    def mcp_port_url(self, path: str) -> str:
        """The address ``path`` of the MCP port, whatever that path is routed to."""
        return f"http://{LOOPBACK}:{self.port}{path}"

    def admin_get(self, path: str) -> httpx.Response:
        """``GET path`` on the Admin port, which has to answer with 200 (req 19.2)."""
        response = self.admin.get(path)
        if response.status_code != httpx.codes.OK:
            raise MeasurementFailed(
                f"Admin 接口 {path} 返回 {response.status_code}：{response.text}"
            )
        return response

    def serving(self) -> bool:
        """Whether both ports of the process answer a request of their own.

        The Admin port has to answer ``GET /admin/health`` with 200 (requirement 19.9), for
        which the token file has to be there first, and the MCP port has to answer at all -
        an Admin address of the MCP port is a 404 of its router (requirement 19.1), which is
        a routed answer and so says that this port serves too. The two sockets are bound
        before either server runs, but which server starts first is not fixed, so both are
        asked (:mod:`erp_mock.gateway.serve`).
        """
        if not self.token:
            if not self.token_file.is_file():
                return False
            self.token = self.token_file.read_text(encoding="utf-8")
            self.admin.headers["Authorization"] = f"Bearer {self.token}"
        try:
            health = self.admin.get(HEALTH_PATH)
            on_mcp = self.mcp.get(HEALTH_PATH)
        except httpx.TransportError:
            return False
        return health.status_code == httpx.codes.OK and on_mcp.status_code == httpx.codes.NOT_FOUND

    def wait_until_serving(self, started: float) -> float:
        """Poll until both ports serve and return the milliseconds since ``started``.

        ``started`` is the reading of :func:`time.perf_counter` from just before the process
        was started, so what this returns is the first metric: the wall clock of a service
        startup, loading, validation and the Snapshot of the initial dataset included.

        Raises MeasurementFailed where the process ended first or the deadline passed, in
        either case with what the process wrote on stderr.
        """
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.serving():
                return (time.perf_counter() - started) * 1000
            code = self.process.poll()
            if code is not None:
                raise MeasurementFailed(f"serve 进程以 {code} 退出，没有开始监听：\n{self.stderr}")
            time.sleep(POLL_SECONDS)
        raise MeasurementFailed(
            f"等待两个端口开始监听超时（{START_TIMEOUT_S} 秒）：\n{self.stderr}"
        )

    def prebuilt_sandbox(self) -> str:
        """Create one sandbox that is bound to no session and return its id (req 19.3).

        The profile, the initial dataset and the anchor date are the instance
        configuration's own (requirement 16.1); this script sends an empty body and
        computes no date (requirement 25.10).
        """
        response = self.admin.post(SANDBOXES_PATH, json={})
        if response.status_code != httpx.codes.CREATED:
            raise MeasurementFailed(
                f"预建沙箱返回 {response.status_code}：{response.text}\n{self.stderr}"
            )
        return str(response.json()["sandbox_id"])

    def trace(self, sandbox_id: str) -> tuple[Mapping[str, Any], ...]:
        """The trace events of ``sandbox_id``, as the Admin API hands them over (19.7)."""
        text = self.admin_get(f"{SANDBOXES_PATH}/{sandbox_id}/trace").content.decode("utf-8")
        return tuple(json.loads(line) for line in text.splitlines() if line)

    def terminate(self) -> int:
        """Stop the process with SIGTERM and return the exit code it ended with."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=STOP_TIMEOUT_S)


def free_port() -> int:
    """A port of the loopback address that nothing held a moment ago.

    Bound the way serve binds its sockets and released again, so it can be named in
    ``--port`` or ``--admin-port`` (:func:`~erp_mock.gateway.serve.bind_socket`).
    """
    with closing(bind_socket(LOOPBACK, 0)) as sock:
        return int(sock.getsockname()[1])


def service_env(options: Options) -> dict[str, str]:
    """The environment of the serve subprocess: the raised limits and nothing of this one.

    Three of the limits of requirement 23.12 are raised to exactly one more than this run
    needs (module docstring), so that no call and no sandbox of the measurement can be
    refused:

    - ``ERP_MOCK_MAX_SANDBOX_CALLS``: the calls of the third metric all happen in one
      sandbox, and the limit is reached rather than exceeded, so it is ``calls + 1``;
    - ``ERP_MOCK_MAX_CALLS_PER_SECOND``: the session of the third metric makes ``calls``
      calls in total, so the same bound can never be reached whatever the pace;
    - ``ERP_MOCK_MAX_SANDBOXES``: the sandboxes of the second metric plus the prebuilt one
      of the third. A session that ends freezes the sandbox it created and a frozen sandbox
      is not counted against this limit (requirement 19.18), so this is the worst case.

    Everything else is left at its default, including the idle, freeze and retention
    thresholds of requirement 15.11: a measurement run is over in seconds.
    """
    inherited = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    return {
        **inherited,
        MAX_SANDBOX_CALLS_VAR: str(options.calls + 1),
        MAX_CALLS_PER_SECOND_VAR: str(options.calls + 1),
        MAX_SANDBOXES_VAR: str(options.initializes + 2),
    }


@contextmanager
def data_directory(options: Options) -> Iterator[Path]:
    """The ``--data-dir`` of the service, removed again where this script made it.

    A directory given on the command line is left as it is, sandboxes and Snapshot cache
    included, so that a run can be looked at afterwards.
    """
    if options.data_dir is not None:
        options.data_dir.mkdir(parents=True, exist_ok=True)
        yield options.data_dir
        return
    created = Path(tempfile.mkdtemp(prefix="erp-mock-perf-"))
    try:
        yield created
    finally:
        shutil.rmtree(created, ignore_errors=True)


@contextmanager
def service(options: Options, data_dir: Path) -> Iterator[Service]:
    """Start one serve process over ``options``, measure its startup and yield it serving.

    The process is stopped with SIGTERM when the block ends, whatever happened inside it, so
    no measurement run leaves a service behind. Its stdout and stderr go to files below
    ``data_dir`` rather than to pipes, so nothing has to be drained to keep the process
    going and this script can quote the log of a service that failed.

    The two HTTP clients of the ports are built before the process is started, so that none
    of their setup is inside the first metric (:class:`Service`).

    Raises MeasurementFailed where the process did not come up (:meth:`wait_until_serving`).
    """
    out, err = data_dir / "perf-stdout.txt", data_dir / "perf-stderr.txt"
    port, admin_port = free_port(), free_port()
    args = [
        str(COMMAND),
        "serve",
        "--instance",
        str(options.instance),
        "--packs-dir",
        str(options.packs_dir),
        "--data-dir",
        str(data_dir),
        "--host",
        LOOPBACK,
        "--port",
        str(port),
        "--admin-port",
        str(admin_port),
    ]
    _say(f"启动服务：{' '.join(args)}")
    with (
        httpx.Client(base_url=f"http://{LOOPBACK}:{admin_port}", timeout=ADMIN_TIMEOUT_S) as admin,
        httpx.Client(base_url=f"http://{LOOPBACK}:{port}", timeout=ADMIN_TIMEOUT_S) as mcp,
        out.open("wb") as out_file,
        err.open("wb") as err_file,
    ):
        started = time.perf_counter()
        process = subprocess.Popen(
            args,
            cwd=REPO_ROOT,
            env=service_env(options),
            stdin=subprocess.DEVNULL,
            stdout=out_file,
            stderr=err_file,
        )
        started_service = Service(
            process=process,
            data_dir=data_dir,
            port=port,
            admin_port=admin_port,
            err=err,
            admin=admin,
            mcp=mcp,
        )
        try:
            started_service.startup_ms = started_service.wait_until_serving(started)
            _say(f"两个端口开始监听，耗时 {started_service.startup_ms:.1f} {UNIT}")
            yield started_service
        finally:
            started_service.terminate()


# --- the sessions of the official client -------------------------------------------------


async def initialize_samples(url: str, count: int) -> tuple[float, ...]:
    """Open ``count`` sessions on ``url`` one at a time and time each ``initialize``.

    Each session is opened, measured and ended before the next one starts, so the service
    creates one sandbox at a time and no two measurements overlap (requirement 15.1). The
    sample is the duration of the ``initialize`` call itself, which is what
    "initialize 到可用" of engineering.md means: once it returns, the session is bound to a
    sandbox and can call tools.
    """
    samples: list[float] = []
    deadline = SESSION_SETUP_S + PER_INITIALIZE_S * count
    with anyio.fail_after(deadline):
        for _ in range(count):
            async with streamable_http_client(url) as (read, write, _session_id):
                async with ClientSession(read, write, client_info=CLIENT_INFO) as client:
                    started = time.perf_counter()
                    await client.initialize()
                    samples.append((time.perf_counter() - started) * 1000)
    return tuple(samples)


async def perform_calls(url: str, count: int) -> int:
    """Perform ``count`` tools/call of :data:`CALL_REQUEST` in one session on ``url``.

    One call after another, each sent as a request of its own rather than through
    ``ClientSession.call_tool``, which would issue a tools/list of its own: the trace of the
    sandbox then holds exactly ``count`` ``tools_call`` events and nothing else
    (:func:`call_latencies`). Returns how many of them came back as an error result, which
    is reported by the caller; what each call took is read off the trace afterwards and not
    timed here.
    """
    failed = 0
    deadline = SESSION_SETUP_S + PER_CALL_S * count
    with anyio.fail_after(deadline):
        async with streamable_http_client(url) as (read, write, _session_id):
            async with ClientSession(read, write, client_info=CLIENT_INFO) as client:
                await client.initialize()
                for _ in range(count):
                    result = await client.send_request(CALL_REQUEST, types.CallToolResult)
                    if result.isError:
                        failed += 1
    return failed


def call_latencies(events: Sequence[Mapping[str, Any]], expected: int) -> tuple[float, ...]:
    """The ``latency_ms`` of the ``tools_call`` events of one trace (requirement 17.3).

    Raises MeasurementFailed where the trace does not hold exactly the ``expected`` calls
    the session made, or where any of them ended with an error: a call that was refused by a
    resource limit (LIMIT_EXCEEDED, requirement 23.8) was never performed, so its latency is
    not a sample, and a run in which anything was refused is reported rather than averaged
    over what was let through (module docstring).
    """
    calls = [event for event in events if event[EVENT_KEY] == EventName.TOOLS_CALL]
    refused = [event for event in calls if event[ERROR_CODE_KEY] == ErrorCode.LIMIT_EXCEEDED.value]
    if refused:
        raise MeasurementFailed(
            f"{len(refused)} 次工具调用被资源上限拒绝（{ErrorCode.LIMIT_EXCEEDED.value}），"
            f"这些调用不计入 p95；请检查 {MAX_SANDBOX_CALLS_VAR} 与 {MAX_CALLS_PER_SECOND_VAR}"
        )
    errors = {str(event[ERROR_CODE_KEY]) for event in calls if event[IS_ERROR_KEY]}
    if errors:
        raise MeasurementFailed(f"工具调用以错误结束，错误码 {sorted(errors)}")
    if len(calls) != expected:
        raise MeasurementFailed(f"trace 中有 {len(calls)} 个 tools_call 事件，期望 {expected} 个")
    tools = {str(event[TOOL_KEY]) for event in calls}
    if tools != {BUSINESS_CONTEXT_GET}:
        raise MeasurementFailed(f"trace 中的工具调用不止被测工具一种：{sorted(tools)}")
    return tuple(float(event[LATENCY_KEY]) for event in calls)


# --- the run -----------------------------------------------------------------------------


def measure(options: Options) -> tuple[Metric, ...]:
    """Measure the three figures of requirement 25.11 and return them in report order.

    Raises MeasurementFailed where a figure could not be measured, in which case the service
    has already been stopped (:func:`service`).
    """
    with data_directory(options) as data_dir, service(options, data_dir) as running:
        _say(f"测量 {options.initializes} 次 initialize 的沙箱创建耗时")
        samples = anyio.run(initialize_samples, running.mcp_url(), options.initializes)

        sandbox_id = running.prebuilt_sandbox()
        _say(f"在预建沙箱 {sandbox_id} 上测量 {options.calls} 次 {BUSINESS_CONTEXT_GET} 调用")
        failed = anyio.run(perform_calls, running.mcp_url(sandbox_id), options.calls)
        if failed:
            raise MeasurementFailed(f"{failed} 次工具调用返回了错误结果")
        latencies = call_latencies(running.trace(sandbox_id), options.calls)

        return (
            Metric(
                name=STARTUP,
                value=running.startup_ms,
                samples=1,
                what="服务启动耗时：从启动 serve 进程到 MCP 与 Admin 两个端口可服务",
            ),
            Metric(
                name=SANDBOX_P95,
                value=percentile(samples, P95),
                samples=len(samples),
                what="沙箱创建耗时 p95：/mcp 上每次 initialize 的耗时",
            ),
            Metric(
                name=CALL_P95,
                value=percentile(latencies, P95),
                samples=len(latencies),
                what=f"工具调用服务端处理时间 p95：trace 中 tools_call 的 {LATENCY_KEY}",
            ),
        )


def report(metrics: Sequence[Metric]) -> str:
    """The three metric lines, one per line, as they go to stdout."""
    return "".join(f"{metric.line}\n" for metric in metrics)


def main(argv: Sequence[str] | None = None) -> int:
    """Measure the three figures and return the exit code of the process."""
    options = parse_args(argv)
    if not COMMAND.is_file():
        _say(f"没有找到安装好的命令：{COMMAND}")
        return 1
    try:
        metrics = measure(options)
    except MeasurementFailed as failure:
        _say(f"测量失败：{failure}")
        return 1
    except TimeoutError:
        _say("测量失败：会话超时，服务没有在期限内应答")
        return 1
    sys.stdout.write(report(metrics))
    sys.stdout.flush()
    _say("测量完成；本命令不设阈值，结果不作为检查命令的通过条件（需求 25.11）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
