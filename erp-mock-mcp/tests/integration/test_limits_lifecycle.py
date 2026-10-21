"""The life of a sandbox, the refusals of the two ports and the three resource limits.

What a deployer configures with the environment, observed on a real service: the thresholds
of requirement 15.11, the guards of requirement 23 and the limits of requirement 23.12 are
read once at startup (:mod:`erp_mock.cli.options`), so the only way to see them take effect
is to start ``erp-mock-mcp serve`` with them set and ask the two ports what they do
(task 26.3). Each process of this file is therefore built from nothing
(:func:`process_env`) with the few ``ERP_MOCK_*`` its own subject needs:

=========================  ==========================================  ====================
process                    environment beyond the Admin token           requirements
=========================  ==========================================  ====================
:func:`lifecycle_service`  the three thresholds at one second each      15.5, 15.7, 15.9
:func:`capacity_service`   ``ERP_MOCK_MAX_SANDBOXES=1``                 15.10
:func:`guards_service`     the MCP endpoint token, a small body limit   23.1, 23.3, 23.7
:func:`leak_service`       both tokens, so the test knows their values  23.6
:func:`calls_service`      ``ERP_MOCK_MAX_SANDBOX_CALLS=2``             23.8, 23.12
:func:`db_service`         ``ERP_MOCK_MAX_DB_BYTES`` below one sandbox  23.8, 23.12
:func:`rate_service`       ``ERP_MOCK_MAX_CALLS_PER_SECOND=2``          23.8, 23.12
=========================  ==========================================  ====================

One process per limit, because the three are checked in the order of requirement 23.12 and
the first one that is over ends the check: a process whose three limits were all small would
only ever report the calls of the sandbox, and no configuration of a single process can show
the other two. Each of those three therefore configures exactly the limit it is about and
leaves the other two at their defaults, which is also what makes the ``max`` of the one
detail item readable - it is the value that process was started with.

The timing of requirements 15.5, 15.7 and 15.9 is elapsed time, which no test clock can
measure: the thresholds are one second each, the smallest the CLI takes, and the transitions
are observed through ``GET /admin/sandboxes/{id}``, which is not a call of requirement 15.5
and therefore does not keep the sandbox from turning idle. The instant a condition held is
bracketed by two readings of the test's own monotonic clock - the one before the request that
caused it went out, the one after its answer came back - and the transition is then required
within the second of the requirement plus :data:`OBSERVATION_SLACK`, which is the overhead of
watching from another process and nothing more.

Everything else is raw HTTP on the two real ports: the MCP endpoint answers JSON rather than
SSE (tests/probes/test_sdk_http_transport.py), so one ``httpx.post`` per JSON-RPC request is
the whole client this file needs - and it is the only client that can leave out an
``Authorization`` header, carry a refused ``Origin`` or send a body over the limit. The
official SDK client over Streamable HTTP belongs to tests/integration/test_protocol_flow.py.

What the services run on: the engine test fixture (:mod:`tests.helpers.fixtures`), whose
clock is in mode fixed and which loads and builds the Snapshot of its one dataset in well
under a second, with ``--data-dir`` below ``tmp_path``. Every wait is a poll against a
deadline (:func:`wait_for`, :func:`observe`), and a poll that finds the process gone fails
the test with what it wrote; the one unavoidable wait of the file is the four seconds of the
sandbox life above, and the whole module stays a few seconds.
"""

import io
import json
import os
import signal
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from erp_mock.cli.options import (
    ADMIN_TOKEN_VAR,
    EXIT_OK,
    FREEZE_SECONDS_VAR,
    IDLE_SECONDS_VAR,
    MAX_BODY_BYTES_VAR,
    MAX_CALLS_PER_SECOND_VAR,
    MAX_DB_BYTES_VAR,
    MAX_SANDBOX_CALLS_VAR,
    MAX_SANDBOXES_VAR,
    MCP_TOKEN_VAR,
    RETENTION_SECONDS_VAR,
)
from erp_mock.cli.serve import TOKEN_FILENAME
from erp_mock.common.errors import ErrorCode
from erp_mock.gateway.middleware import (
    BEARER_PREFIX,
    FORBIDDEN_ORIGIN,
    PAYLOAD_TOO_LARGE,
    UNAUTHORIZED,
)
from erp_mock.gateway.router import SESSION_ID_HEADER, sandbox_path
from erp_mock.gateway.sandbox import JANITOR_INTERVAL_SECONDS, METADATA_FILENAME, SANDBOXES_DIR
from erp_mock.gateway.serve import ADMIN_HOST, bind_socket
from erp_mock.gateway.trace import COMMON_KEYS, TOOLS_CALL_KEYS, EventName
from erp_mock.state.store import DB_FILENAME
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET
from erp_mock.tools.layer import (
    CALLS_PER_SECOND,
    DB_BYTES,
    LIMIT_KEY,
    MAX_DB_BYTES,
    MAX_KEY,
    SANDBOX_CALLS,
)
from erp_mock.tools.results import CODE, DETAILS, ERROR_KEY, MESSAGE, RULE_ID
from tests.helpers.fixtures import INSTANCE_DIR, PACKS_DIR

# The installed command, beside the interpreter that runs the tests, as the other two
# integration tests of the CLI start it (tests/integration/test_cli_serve.py).
COMMAND: Final = Path(sys.executable).parent / "erp-mock-mcp"

# The variables a subprocess is given beyond what a test sets: enough to run, and not one of
# the ``ERP_MOCK_*`` the environment of the test process may carry (requirement 23.9).
PASSED_THROUGH: Final = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")

# The deadlines and the poll of every wait. Starting a service loads the fixture and builds
# the Snapshot of its dataset, which takes well under a second; a test that reaches one of
# these has already failed.
START_TIMEOUT_S: Final = 20.0
STOP_TIMEOUT_S: Final = 15.0
REQUEST_TIMEOUT_S: Final = 20.0
POLL_SECONDS: Final = 0.05

# The one address both ports of every service of this file are served on.
LOOPBACK: Final = ADMIN_HOST

# The two tokens the services of this file are configured with (requirements 23.3 and 23.4).
# Both are nonsense values, long enough that no file and no answer could carry one by
# chance, which is what :func:`test_no_output_of_the_service_carries_either_token` reads.
GIVEN_ADMIN_TOKEN: Final = "admin-token-f3a91c7e5b240d86"
GIVEN_MCP_TOKEN: Final = "mcp-token-0c58e742a9d13fb6"

# The addresses of the two ports these tests use: the MCP endpoint of requirement 14.2 and
# the Admin interfaces of requirement 19.1.
MCP_PATH: Final = "/mcp"
HEALTH_PATH: Final = "/admin/health"
SANDBOXES_PATH: Final = "/admin/sandboxes"
EXPORTS_PATH: Final = "/admin/exports"
UNKNOWN_ADMIN_PATH: Final = "/admin/no-such-interface"

# The headers the official client puts on every POST (mcp.client.streamable_http), which the
# raw requests of this file carry as well, and the protocol version it announces.
JSON_AND_SSE: Final = "application/json, text/event-stream"
POST_HEADERS: Final[Mapping[str, str]] = {
    "content-type": "application/json",
    "accept": JSON_AND_SSE,
}
PROTOCOL_HEADER: Final = "mcp-protocol-version"
PROTOCOL_VERSION: Final = "2025-11-25"
JSONRPC_VERSION: Final = "2.0"

# The three thresholds of the lifecycle process: one second each, the smallest value
# ``ERP_MOCK_IDLE_SECONDS`` and its two siblings take (requirement 23.10), so the whole life
# of a sandbox passes in about four seconds.
LIFECYCLE_SECONDS: Final = 1

# What requirements 15.5, 15.7 and 15.9 allow between a condition and its transition.
ALLOWED_DELAY: Final = 1.0

# What watching from another process costs on top of that second, and nothing else:
#
# - one scan interval of the janitor, since a condition that comes true just after a scan is
#   seen by the next one (:data:`JANITOR_INTERVAL_SECONDS`, design.md 7.5);
# - one poll interval, which is how coarsely :func:`observe` can place the moment;
# - one round trip of ``GET /admin/sandboxes/{id}`` on the loopback address, which takes
#   single-digit milliseconds here and is given :data:`ADMIN_ROUNDTRIP_SECONDS`.
#
# The first of the three is inside the allowed second already; it is counted again rather
# than argued away, because the scan that makes a transition and the request that sees it are
# two different things.
ADMIN_ROUNDTRIP_SECONDS: Final = 0.2
OBSERVATION_SLACK: Final = JANITOR_INTERVAL_SECONDS + POLL_SECONDS + ADMIN_ROUNDTRIP_SECONDS

# How long the lifecycle test waits for the three transitions together before giving up: the
# three thresholds, the second each is allowed and the cost of each observation.
LIFECYCLE_TIMEOUT_S: Final = 3 * (LIFECYCLE_SECONDS + ALLOWED_DELAY + OBSERVATION_SLACK)

# The three states of a sandbox, as the Admin API writes them (requirement 19.12); None is
# what :meth:`Service.status_of` answers for a sandbox that is gone (requirement 15.9).
ACTIVE: Final = "active"
IDLE: Final = "idle"
FROZEN: Final = "frozen"

# The body limit of the guards process: far above every message this file sends and small
# enough to exceed in one line (requirement 23.7). Not a round number of kibibytes, so an
# off-by-one in the comparison would show.
LOW_BODY_BYTES: Final = 1000

# The concurrency limit of the capacity process: one sandbox, so a single prebuilt sandbox
# fills the service (requirement 15.10).
LOW_MAX_SANDBOXES: Final = 1

# The call limit of the calls process (requirement 23.12): two calls pass and the third is
# the one over the limit.
LOW_SANDBOX_CALLS: Final = 2

# The rate limit of the rate process: two calls within a second pass, the third is over.
LOW_CALLS_PER_SECOND: Final = 2

# The database limit of the db process (requirement 23.12). A sandbox of the fixture dataset
# is 37 pages of 4096 bytes, so this is below the size of every sandbox of that service and
# far below the default of :data:`MAX_DB_BYTES`, under which no sandbox of this file would
# ever be over the limit; :func:`test_a_database_over_the_limit_is_refused` asserts that
# ordering on the sandbox it made rather than trusting these two numbers.
LOW_DB_BYTES: Final = 65536

# An Origin no rule of requirement 23.1 lets through: not a loopback host, and the services
# of this file configure no allowed origin at all.
REFUSED_ORIGIN: Final = "https://evil.example"

# A loopback Origin, which requirement 23.1 lets through on any port and either scheme.
ALLOWED_ORIGIN: Final = "http://localhost:5173"

# How many files a service of this file has below ``--data-dir``: the Snapshot of its one
# dataset with its metadata, its alias map and its lock, and the four files of one sandbox
# (design.md data model 2). The bound is a lower one on purpose - what it is there for is
# that :func:`test_no_output_of_the_service_carries_either_token` cannot pass on an empty
# scan, not to fix how many files a service keeps.
LEAST_DATA_FILES: Final = 8


@pytest.fixture(scope="module", autouse=True)
def installed_command() -> None:
    """Every test here runs the console script; a run without it would be meaningless."""
    assert COMMAND.is_file(), f"没有找到安装好的命令：{COMMAND}"
    assert os.access(COMMAND, os.X_OK)


def process_env(**extra: str) -> dict[str, str]:
    """The environment of one serve process: those of :data:`PASSED_THROUGH` and ``extra``."""
    inherited = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    return {**inherited, **extra}


def bearer(token: str) -> dict[str, str]:
    """The Authorization header both ports check (requirements 19.2 and 23.3)."""
    return {"Authorization": f"{BEARER_PREFIX}{token}"}


def free_port() -> int:
    """A port of the loopback address that nothing held a moment ago.

    Bound the way serve binds its sockets and released again, so a test can name it in
    ``--port`` or ``--admin-port`` (:func:`~erp_mock.gateway.serve.bind_socket`).
    """
    with closing(bind_socket(LOOPBACK, 0)) as sock:
        return int(sock.getsockname()[1])


def request_body(request_id: int, method: str, params: Mapping[str, Any] | None = None) -> bytes:
    """One JSON-RPC request as the Streamable HTTP transport carries it: one JSON object."""
    body: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        body["params"] = dict(params)
    return json.dumps(body).encode("utf-8")


def initialize_body(request_id: int = 1) -> bytes:
    """The initialize request the official client opens a session with."""
    return request_body(
        request_id,
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "limits-lifecycle-test", "version": "0.0.0"},
        },
    )


def call_body(request_id: int, tool: str = BUSINESS_CONTEXT_GET) -> bytes:
    """One tools/call of ``tool`` without arguments: the read every limit test makes."""
    return request_body(request_id, "tools/call", {"name": tool, "arguments": {}})


# --- the service under test ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Service:
    """A serve process of this file, and everything a test asks of it over its two ports."""

    process: subprocess.Popen[bytes]
    data_dir: Path
    port: int
    admin_port: int
    out: Path
    err: Path
    admin_token: str
    mcp_token: str | None

    # --- what the process wrote ----------------------------------------------------------

    @property
    def stdout(self) -> bytes:
        """What the process has written to stdout; requirement 22.1 puts nothing there."""
        return self.out.read_bytes()

    @property
    def stderr(self) -> bytes:
        """What the process has written to stderr: its log (design.md 7.3)."""
        return self.err.read_bytes()

    @property
    def log(self) -> str:
        """That stderr as text, for the message of a wait that failed."""
        return self.stderr.decode("utf-8", errors="replace")

    def sandbox_dir(self, sandbox_id: str) -> Path:
        """``<data-dir>/sandboxes/<sandbox_id>`` (design.md data model 2)."""
        return self.data_dir / SANDBOXES_DIR / sandbox_id

    # --- the Admin port (requirement 19.1) -----------------------------------------------

    def admin_url(self, path: str) -> str:
        """The address ``path`` of the Admin API of this process."""
        return f"http://{LOOPBACK}:{self.admin_port}{path}"

    def admin(
        self, method: str, path: str, *, token: str | None = "", **kwargs: Any
    ) -> httpx.Response:
        """One Admin request, carrying the Admin token of this process by default.

        ``token`` of ``""`` is that token, None is no Authorization header at all, and any
        other value is presented as it is (requirement 19.2).
        """
        headers = {} if token is None else bearer(self.admin_token if token == "" else token)
        return httpx.request(
            method, self.admin_url(path), headers=headers, timeout=REQUEST_TIMEOUT_S, **kwargs
        )

    def serving(self) -> bool:
        """Whether the Admin port answers the Admin token with 200 (requirement 19.9)."""
        try:
            return self.admin("GET", HEALTH_PATH).status_code == HTTPStatus.OK
        except httpx.TransportError:
            return False

    def create_sandbox(self, **body: Any) -> str:
        """Prebuild one sandbox and return its id (requirement 19.3)."""
        response = self.admin("POST", SANDBOXES_PATH, json=body)
        assert response.status_code == HTTPStatus.CREATED, response.text
        sandbox_id = response.json()["sandbox_id"]
        assert isinstance(sandbox_id, str)
        return sandbox_id

    def sandbox_ids(self) -> tuple[str, ...]:
        """The ids of the sandboxes of this service, in creation order (requirement 19.11)."""
        response = self.admin("GET", SANDBOXES_PATH)
        assert response.status_code == HTTPStatus.OK, response.text
        return tuple(str(item["sandbox_id"]) for item in response.json()["items"])

    def metadata(self, sandbox_id: str) -> httpx.Response:
        """``GET /admin/sandboxes/{id}``, which is not a call of requirement 15.5."""
        return self.admin("GET", f"{SANDBOXES_PATH}/{sandbox_id}")

    def status_of(self, sandbox_id: str) -> str | None:
        """The status of ``sandbox_id``, None where it is gone (requirements 15.9, 19.12)."""
        response = self.metadata(sandbox_id)
        if response.status_code == HTTPStatus.NOT_FOUND:
            return None
        assert response.status_code == HTTPStatus.OK, response.text
        status = response.json()["status"]
        assert isinstance(status, str)
        return status

    def trace_response(self, sandbox_id: str) -> httpx.Response:
        """``GET /admin/sandboxes/{id}/trace``, which is JSONL (requirement 19.7)."""
        return self.admin("GET", f"{SANDBOXES_PATH}/{sandbox_id}/trace")

    def trace_text(self, sandbox_id: str) -> str:
        """The trace of that sandbox as the Admin API hands it over."""
        response = self.trace_response(sandbox_id)
        assert response.status_code == HTTPStatus.OK, response.text
        return response.text

    def events(self, sandbox_id: str) -> list[dict[str, Any]]:
        """The events of that trace, one object per line (requirement 17.1)."""
        return [json.loads(line) for line in self.trace_text(sandbox_id).splitlines() if line]

    def diff_response(self, sandbox_id: str) -> httpx.Response:
        """``GET /admin/sandboxes/{id}/diff`` against the initial dataset (req 19.8)."""
        return self.admin("GET", f"{SANDBOXES_PATH}/{sandbox_id}/diff")

    def diff(self, sandbox_id: str) -> dict[str, Any]:
        """That diff, parsed (requirement 18.1)."""
        response = self.diff_response(sandbox_id)
        assert response.status_code == HTTPStatus.OK, response.text
        payload = response.json()
        assert isinstance(payload, dict)
        return payload

    # --- the MCP port (requirement 14.2) -------------------------------------------------

    def mcp_url(self, path: str) -> str:
        """The address ``path`` of the MCP port of this process."""
        return f"http://{LOOPBACK}:{self.port}{path}"

    def mcp_headers(
        self, *, session_id: str | None = None, origin: str | None = None, token: str | None = ""
    ) -> dict[str, str]:
        """The headers of one MCP request.

        ``token`` of ``""`` is the MCP endpoint token of this process - no header at all
        where it has none - None is no header, and any other value is presented as it is
        (requirement 23.3). A session id brings the protocol version header with it, as the
        official client sends it on every request after the handshake.
        """
        headers = dict(POST_HEADERS)
        presented = self.mcp_token if token == "" else token
        if presented is not None:
            headers.update(bearer(presented))
        if session_id is not None:
            headers[SESSION_ID_HEADER] = session_id
            headers[PROTOCOL_HEADER] = PROTOCOL_VERSION
        if origin is not None:
            headers["origin"] = origin
        return headers

    def mcp_post(self, path: str, body: bytes, **headers: Any) -> httpx.Response:
        """One POST on the MCP port of this process, with the headers of ``headers``."""
        return httpx.post(
            self.mcp_url(path),
            content=body,
            headers=self.mcp_headers(**headers),
            timeout=REQUEST_TIMEOUT_S,
        )

    def initialize(
        self, sandbox_id: str | None = None, **headers: Any
    ) -> tuple[str, httpx.Response]:
        """Send one initialize and return the path it went to and the answer.

        ``sandbox_id`` of None is ``/mcp``, which creates a sandbox of its own
        (requirement 15.1); a sandbox id is ``/s/<sandbox_id>/mcp``, which binds the session
        to that prebuilt sandbox (requirement 15.3).
        """
        path = MCP_PATH if sandbox_id is None else sandbox_path(sandbox_id)
        return path, self.mcp_post(path, initialize_body(), **headers)

    def open_session(self, sandbox_id: str | None = None) -> tuple[str, str]:
        """Open one session and return its path and its session id."""
        path, response = self.initialize(sandbox_id)
        assert response.status_code == HTTPStatus.OK, response.text
        return path, response.headers[SESSION_ID_HEADER]

    def call(self, path: str, session_id: str, request_id: int) -> httpx.Response:
        """One ``business_context_get`` inside the session ``session_id``."""
        return self.mcp_post(path, call_body(request_id), session_id=session_id)

    # --- the stop -------------------------------------------------------------------------

    def terminate(self) -> int:
        """Send SIGTERM and return the exit code of the finished process (req 22.1)."""
        self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=STOP_TIMEOUT_S)


def serve_args(data_dir: Path, *, port: int, admin_port: int) -> list[str]:
    """The command line of one serve run over the engine fixture (requirement 22.1)."""
    return [
        str(COMMAND),
        "serve",
        "--instance",
        str(INSTANCE_DIR),
        "--packs-dir",
        str(PACKS_DIR),
        "--data-dir",
        str(data_dir),
        "--host",
        LOOPBACK,
        "--port",
        str(port),
        "--admin-port",
        str(admin_port),
    ]


@contextmanager
def serve_process(directory: Path, **variables: str) -> Iterator[Service]:
    """Start a serve process on two free ports with ``variables`` set, and yield it serving.

    The Admin token is :data:`GIVEN_ADMIN_TOKEN` unless ``variables`` says otherwise, so a
    test reaches the Admin API without reading the token file (requirement 23.4). The
    process is waited for before the block is entered and is gone after it: whatever is
    still running is killed, so no test leaves a service behind. stdout and stderr go to
    files below ``directory`` rather than to pipes, so a test reads them while the process
    runs without having to drain a pipe to keep it going.
    """
    data_dir = directory / "var"
    out, err = directory / "stdout.txt", directory / "stderr.txt"
    port, admin_port = free_port(), free_port()
    env = process_env(**{ADMIN_TOKEN_VAR: GIVEN_ADMIN_TOKEN, **variables})
    with out.open("wb") as out_file, err.open("wb") as err_file:
        process = subprocess.Popen(
            serve_args(data_dir, port=port, admin_port=admin_port),
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out_file,
            stderr=err_file,
        )
    service = Service(
        process=process,
        data_dir=data_dir,
        port=port,
        admin_port=admin_port,
        out=out,
        err=err,
        admin_token=env[ADMIN_TOKEN_VAR],
        mcp_token=env.get(MCP_TOKEN_VAR),
    )
    try:
        wait_for(service, service.serving, "两个端口开始监听")
        yield service
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=STOP_TIMEOUT_S)


def wait_for(service: Service, condition: Callable[[], bool], what: str) -> None:
    """Poll ``condition`` until it holds, failing the test where it does not in time.

    A process that ended while the wait was on fails the test at once, with what it wrote on
    stderr: a service that could not start has already said why.
    """
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if condition():
            return
        code = service.process.poll()
        if code is not None:
            pytest.fail(f"serve 进程在{what}之前以 {code} 退出：\n{service.log}")
        time.sleep(POLL_SECONDS)
    pytest.fail(f"等待{what}超时（{START_TIMEOUT_S} 秒）：\n{service.log}")


def observe(
    service: Service, sandbox_id: str, state: str | None, deadline: float
) -> tuple[float, float]:
    """Poll the status of ``sandbox_id`` until it is ``state``; return when it was seen.

    The pair is the reading before the request that saw it went out and the reading after its
    answer came back, so the transition happened at or before the second of the two - the
    Admin API reads the status without touching the sandbox - and the first of the two is a
    reading at which the transition had not been seen yet. The clock is never read before the
    status is known, so a transition that was too early shows as a reading before its
    condition and one that was too late as a deadline that passed.
    """
    while True:
        before = time.monotonic()
        seen = service.status_of(sandbox_id)
        after = time.monotonic()
        if seen == state:
            return before, after
        if after > deadline:
            pytest.fail(f"沙箱 {sandbox_id} 未在期限内变为 {state}，当前为 {seen}：\n{service.log}")
        time.sleep(POLL_SECONDS)


# --- what a refused request and a refused call look like ----------------------------------


def assert_refused(response: httpx.Response, status: HTTPStatus, code: str) -> str:
    """Assert ``response`` is the refusal of ``status`` and ``code``, and return its message.

    The shape of design.md 7.7: a JSON object of ``error`` and ``message`` in that order and
    nothing else, with ``Content-Type: application/json``.
    """
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert list(body) == ["error", "message"]
    assert body["error"] == code
    message = body["message"]
    assert isinstance(message, str) and message
    return message


def tool_error(response: httpx.Response) -> dict[str, Any]:
    """The error object of a tools/call that ended with an error code (requirement 13.6).

    The transport answered 200 - a tool error is a result, not a protocol error - and the
    result carries ``isError``, one text item and no structuredContent.
    """
    assert response.status_code == HTTPStatus.OK, response.text
    result = response.json()["result"]
    assert result["isError"] is True, result
    assert "structuredContent" not in result
    payload = json.loads(result["content"][0]["text"])
    assert list(payload) == [ERROR_KEY]
    error = payload[ERROR_KEY]
    assert isinstance(error, dict)
    return error


def assert_limit_exceeded(response: httpx.Response, limit: str, maximum: int) -> dict[str, Any]:
    """Assert ``response`` is the LIMIT_EXCEEDED of ``limit`` and return its error object.

    Requirement 23.8 and design.md data model 6: ``rule_id`` is null and ``details`` holds
    exactly one item, which is ``limit`` and ``max`` in that order and nothing else - no
    ``field``, no parameter of the call being at fault.
    """
    error = tool_error(response)
    assert list(error) == [CODE, MESSAGE, RULE_ID, DETAILS]
    assert error[CODE] == ErrorCode.LIMIT_EXCEEDED
    assert error[RULE_ID] is None
    assert error[DETAILS] == [{LIMIT_KEY: limit, MAX_KEY: maximum}]
    assert list(error[DETAILS][0]) == [LIMIT_KEY, MAX_KEY]
    assert str(maximum) in error[MESSAGE]
    return error


def assert_limit_event(event: Mapping[str, Any], error: Mapping[str, Any]) -> None:
    """Assert ``event`` is the tools_call of a call the resource limits refused.

    Requirement 17.5: the event is written like any other tools_call, with the keys of
    requirement 17.3 in their order and an empty ``writes``; ``coerced`` is empty too, the
    call having ended before input validation (requirement 17.11).
    """
    assert list(event) == [*COMMON_KEYS, *TOOLS_CALL_KEYS]
    assert event["event"] == EventName.TOOLS_CALL
    assert event["tool"] == BUSINESS_CONTEXT_GET
    assert event["known_tool"] is True
    assert event["is_error"] is True
    assert event["error_code"] == ErrorCode.LIMIT_EXCEEDED
    assert event["rule_id"] is None
    assert event["result"] == dict(error)
    assert event["writes"] == []
    assert event["coerced"] == []
    assert event["flags"] == []
    assert isinstance(event["latency_ms"], int)
    assert event["latency_ms"] >= 0


def assert_refused_call(service: Service, sandbox_id: str, error: Mapping[str, Any]) -> None:
    """Assert the trace of ``sandbox_id`` ends with the refusal ``error`` and nothing changed.

    The last event of the trace is the tools_call of the refused call (requirement 17.5) and
    the database of the sandbox is as it was: its diff against the initial dataset is still
    empty, every call of these tests being a read (requirement 23.8).
    """
    assert_limit_event(service.events(sandbox_id)[-1], error)
    assert service.diff(sandbox_id)["changes"] == []


# --- the processes of this file ------------------------------------------------------------


@pytest.fixture
def lifecycle_service(tmp_path: Path) -> Iterator[Service]:
    """A service whose idle, freeze and retention thresholds are one second each (15.11)."""
    seconds = str(LIFECYCLE_SECONDS)
    with serve_process(
        tmp_path,
        **{
            IDLE_SECONDS_VAR: seconds,
            FREEZE_SECONDS_VAR: seconds,
            RETENTION_SECONDS_VAR: seconds,
        },
    ) as service:
        yield service


@pytest.fixture
def capacity_service(tmp_path: Path) -> Iterator[Service]:
    """A service that serves one non-frozen sandbox at a time (requirement 15.10)."""
    with serve_process(tmp_path, **{MAX_SANDBOXES_VAR: str(LOW_MAX_SANDBOXES)}) as service:
        yield service


@pytest.fixture(scope="module")
def guards_service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    """A service behind an MCP endpoint token and a small body limit (req 23.3 and 23.7).

    Shared by the three tests of the guards: each of them asserts that the request it made
    created no session and no sandbox by comparing the sandboxes of the service before and
    after it, so the three are independent of each other and of their order.
    """
    with serve_process(
        tmp_path_factory.mktemp("guards"),
        **{MCP_TOKEN_VAR: GIVEN_MCP_TOKEN, MAX_BODY_BYTES_VAR: str(LOW_BODY_BYTES)},
    ) as service:
        yield service


@pytest.fixture
def leak_service(tmp_path: Path) -> Iterator[Service]:
    """A service whose two tokens both come from the environment (requirements 23.3, 23.4)."""
    with serve_process(tmp_path, **{MCP_TOKEN_VAR: GIVEN_MCP_TOKEN}) as service:
        yield service


@pytest.fixture
def calls_service(tmp_path: Path) -> Iterator[Service]:
    """A service whose sandboxes serve two calls each (requirements 23.9 and 23.12)."""
    with serve_process(tmp_path, **{MAX_SANDBOX_CALLS_VAR: str(LOW_SANDBOX_CALLS)}) as service:
        yield service


@pytest.fixture
def db_service(tmp_path: Path) -> Iterator[Service]:
    """A service whose database limit is below the size of one sandbox (req 23.12)."""
    with serve_process(tmp_path, **{MAX_DB_BYTES_VAR: str(LOW_DB_BYTES)}) as service:
        yield service


@pytest.fixture
def rate_service(tmp_path: Path) -> Iterator[Service]:
    """A service that serves two calls per second and session (requirement 23.12)."""
    with serve_process(
        tmp_path, **{MAX_CALLS_PER_SECOND_VAR: str(LOW_CALLS_PER_SECOND)}
    ) as service:
        yield service


# --- the life of a sandbox (requirements 15.5, 15.7 and 15.9) -----------------------------


@pytest.mark.req("15.5", "15.7", "15.9")
def test_a_sandbox_turns_idle_then_frozen_then_gone_each_within_a_second(
    lifecycle_service: Service,
) -> None:
    """The three transitions of a sandbox nobody calls, each within a second of its condition.

    The conditions chain: the idle threshold is counted from the creation of the sandbox, the
    freeze threshold from the moment it turned idle and the retention period from the moment
    it froze (design.md 7.5). Each of those three instants is bracketed by two readings of
    the test's clock, and each transition is then required between the lower bound plus its
    threshold and the upper bound plus its threshold plus the second the requirement allows,
    :data:`OBSERVATION_SLACK` on top being the cost of watching from here.
    """
    service = lifecycle_service

    sent = time.monotonic()
    sandbox_id = service.create_sandbox()
    created = time.monotonic()
    assert service.status_of(sandbox_id) == ACTIVE
    directory = service.sandbox_dir(sandbox_id)
    before = service.metadata(sandbox_id).json()
    trace = service.trace_text(sandbox_id)
    deadline = created + LIFECYCLE_TIMEOUT_S

    # Active to idle, counted from the creation of the sandbox (requirement 15.5).
    idle_seen, idle_after = observe(service, sandbox_id, IDLE, deadline)
    assert idle_seen >= sent + LIFECYCLE_SECONDS
    assert idle_seen <= created + LIFECYCLE_SECONDS + ALLOWED_DELAY + OBSERVATION_SLACK
    # The database file, the trace and the metadata are what they were - what turning idle
    # closes is the connection to that file - and the anchor date of the sandbox is untouched
    # by it (requirements 15.5 and 16.2).
    assert (directory / DB_FILENAME).is_file()
    assert service.trace_text(sandbox_id) == trace
    after_idle = service.metadata(sandbox_id).json()
    assert {key: value for key, value in after_idle.items() if key != "status"} == {
        key: value for key, value in before.items() if key != "status"
    }

    # Idle to frozen, counted from the moment it turned idle (requirement 15.7).
    frozen_seen, frozen_after = observe(service, sandbox_id, FROZEN, deadline)
    assert frozen_seen >= sent + 2 * LIFECYCLE_SECONDS
    assert frozen_seen <= idle_after + LIFECYCLE_SECONDS + ALLOWED_DELAY + OBSERVATION_SLACK
    # A frozen sandbox keeps its files and is read through the Admin API like any other
    # (requirement 15.12), and its metadata file now says what it is (design.md 7.5).
    assert json.loads((directory / METADATA_FILENAME).read_bytes())["status"] == FROZEN
    assert service.trace_text(sandbox_id) == trace
    assert service.diff_response(sandbox_id).status_code == HTTPStatus.OK

    # Frozen to gone, counted from the moment it froze (requirement 15.9).
    gone_seen, _ = observe(service, sandbox_id, None, deadline)
    assert gone_seen >= sent + 3 * LIFECYCLE_SECONDS
    assert gone_seen <= frozen_after + LIFECYCLE_SECONDS + ALLOWED_DELAY + OBSERVATION_SLACK

    # The database, the trace, the Named_Snapshots and the metadata were that directory, and
    # it is gone; every Admin request for the id is a 404 from here on (requirement 15.9).
    assert not directory.exists()
    assert service.sandbox_ids() == ()
    assert service.metadata(sandbox_id).status_code == HTTPStatus.NOT_FOUND
    assert service.trace_response(sandbox_id).status_code == HTTPStatus.NOT_FOUND
    assert service.diff_response(sandbox_id).status_code == HTTPStatus.NOT_FOUND
    assert service.terminate() == EXIT_OK


@pytest.mark.req("15.10")
def test_an_initialize_is_refused_with_503_when_every_sandbox_is_taken(
    capacity_service: Service,
) -> None:
    """The concurrency limit of requirement 15.10, as a client of the MCP port meets it."""
    service = capacity_service
    sandbox_id = service.create_sandbox()
    assert service.sandbox_ids() == (sandbox_id,)

    _, refused = service.initialize()

    # A JSON-RPC error with the status code of a service at capacity, and neither a session
    # nor a sandbox was created (requirement 15.10).
    assert refused.status_code == HTTPStatus.SERVICE_UNAVAILABLE, refused.text
    assert list(refused.json()) == ["jsonrpc", "id", "error"]
    assert SESSION_ID_HEADER not in refused.headers
    assert service.sandbox_ids() == (sandbox_id,)

    # An initialize on the prebuilt sandbox is served as usual: it binds a session to a
    # sandbox that is already there rather than creating one (requirements 15.3 and 15.10).
    _, session_id = service.open_session(sandbox_id)
    assert session_id
    assert service.sandbox_ids() == (sandbox_id,)
    assert service.terminate() == EXIT_OK


# --- what the two ports refuse (requirements 23.1, 23.3 and 23.7) -------------------------


@pytest.mark.req("23.1")
def test_a_refused_origin_is_answered_with_403_and_creates_nothing(
    guards_service: Service,
) -> None:
    service = guards_service
    before = service.sandbox_ids()

    _, refused = service.initialize(origin=REFUSED_ORIGIN)

    message = assert_refused(refused, HTTPStatus.FORBIDDEN, FORBIDDEN_ORIGIN)
    # The Origin that was refused is not echoed, and neither is the token the request carried
    # (requirement 23.6).
    assert REFUSED_ORIGIN not in message
    assert GIVEN_MCP_TOKEN not in refused.text
    # Requirement 23.1: the request was not executed, and no session or sandbox was created.
    assert SESSION_ID_HEADER not in refused.headers
    assert service.sandbox_ids() == before


@pytest.mark.req("23.3")
def test_the_mcp_endpoint_token_is_required_and_sufficient(guards_service: Service) -> None:
    """Requirement 23.3: without the configured token nothing is served, with it everything."""
    service = guards_service
    before = service.sandbox_ids()

    _, missing = service.initialize(token=None)
    _, wrong = service.initialize(token=f"{GIVEN_MCP_TOKEN}x")
    _, upper = service.initialize(token=GIVEN_MCP_TOKEN.upper())

    for refused in (missing, wrong, upper):
        assert_refused(refused, HTTPStatus.UNAUTHORIZED, UNAUTHORIZED)
        assert refused.headers["www-authenticate"] == "Bearer"
        assert SESSION_ID_HEADER not in refused.headers
    # Not one of the three reached a session or a sandbox (requirement 23.3).
    assert service.sandbox_ids() == before

    # The token itself, character for character, is served - on an Origin requirement 23.1
    # allows, which is the check right after it (requirement 23.11).
    path, session_id = service.open_session()
    served = service.mcp_post(path, call_body(2), session_id=session_id, origin=ALLOWED_ORIGIN)
    assert served.status_code == HTTPStatus.OK, served.text
    assert served.json()["result"]["isError"] is False
    assert len(service.sandbox_ids()) == len(before) + 1


@pytest.mark.req("23.7")
def test_a_body_over_the_limit_is_answered_with_413_on_both_ports(
    guards_service: Service,
) -> None:
    """The body limit of ``ERP_MOCK_MAX_BODY_BYTES`` on the two ports of one process.

    The criterion is requirement 23.7, which task 26.3 names in its description but not in
    its list of criteria; the mark is the criterion the test validates.
    """
    service = guards_service
    before = service.sandbox_ids()
    oversized = json.dumps({"filler": "x" * LOW_BODY_BYTES}).encode("utf-8")
    assert len(oversized) > LOW_BODY_BYTES

    on_mcp = service.mcp_post(MCP_PATH, oversized)
    on_admin = service.admin("POST", SANDBOXES_PATH, content=oversized)

    for refused in (on_mcp, on_admin):
        message = assert_refused(refused, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, PAYLOAD_TOO_LARGE)
        assert str(LOW_BODY_BYTES) in message
    # Neither request was executed: no session, no sandbox (requirement 23.7).
    assert SESSION_ID_HEADER not in on_mcp.headers
    assert service.sandbox_ids() == before


# --- the three resource limits (requirements 23.8 and 23.12) ------------------------------


@pytest.mark.req("23.8", "23.12")
def test_the_calls_of_a_sandbox_are_limited(calls_service: Service) -> None:
    """Requirement 23.12: the calls that passed the check are counted, and the bound bites."""
    service = calls_service
    sandbox_id = service.create_sandbox()
    path, session_id = service.open_session(sandbox_id)

    for request_id in range(LOW_SANDBOX_CALLS):
        passed = service.call(path, session_id, request_id + 2)
        assert passed.status_code == HTTPStatus.OK, passed.text
        assert passed.json()["result"]["isError"] is False

    refused = service.call(path, session_id, LOW_SANDBOX_CALLS + 2)

    error = assert_limit_exceeded(refused, SANDBOX_CALLS, LOW_SANDBOX_CALLS)
    assert_refused_call(service, sandbox_id, error)

    # A call that was refused is not a call that passed, so the count stands and the next
    # call is refused in exactly the same way (requirement 23.12).
    again = service.call(path, session_id, LOW_SANDBOX_CALLS + 3)
    assert assert_limit_exceeded(again, SANDBOX_CALLS, LOW_SANDBOX_CALLS) == error
    assert [event["error_code"] for event in service.events(sandbox_id)[-2:]] == [
        ErrorCode.LIMIT_EXCEEDED,
        ErrorCode.LIMIT_EXCEEDED,
    ]
    assert service.terminate() == EXIT_OK


@pytest.mark.req("23.8", "23.12")
def test_a_database_over_the_limit_is_refused(db_service: Service) -> None:
    """Requirement 23.12: the size of the database as the call starts, above the bound."""
    service = db_service
    sandbox_id = service.create_sandbox()

    # The limit this process was started with is below the size of its sandboxes and the
    # default of requirement 23.9 is above it, so the refusal below is this configuration and
    # not something every sandbox of the service would run into.
    size = (service.sandbox_dir(sandbox_id) / DB_FILENAME).stat().st_size
    assert LOW_DB_BYTES < size < MAX_DB_BYTES

    path, session_id = service.open_session(sandbox_id)
    refused = service.call(path, session_id, 2)

    error = assert_limit_exceeded(refused, DB_BYTES, LOW_DB_BYTES)
    assert_refused_call(service, sandbox_id, error)
    assert service.terminate() == EXIT_OK


@pytest.mark.req("23.8", "23.12")
def test_the_call_rate_of_a_session_is_limited(rate_service: Service) -> None:
    """Requirement 23.12: the calls of this session in the second before this one arrived."""
    service = rate_service
    sandbox_id = service.create_sandbox()
    path, session_id = service.open_session(sandbox_id)

    started = time.monotonic()
    for request_id in range(LOW_CALLS_PER_SECOND):
        passed = service.call(path, session_id, request_id + 2)
        assert passed.status_code == HTTPStatus.OK, passed.text
        assert passed.json()["result"]["isError"] is False
    refused = service.call(path, session_id, LOW_CALLS_PER_SECOND + 2)
    elapsed = time.monotonic() - started

    # The window is one second, so the calls above have to fall inside one: on the loopback
    # address each of them takes a few milliseconds, and a run that took longer is a machine
    # too slow to say anything about the limit rather than a limit that did not hold.
    assert elapsed < 1.0, f"三次调用用了 {elapsed:.3f} 秒，超过了速率窗口"

    error = assert_limit_exceeded(refused, CALLS_PER_SECOND, LOW_CALLS_PER_SECOND)
    assert_refused_call(service, sandbox_id, error)
    assert service.terminate() == EXIT_OK


# --- neither token leaves the process (requirement 23.6) ----------------------------------


def zip_entries(payload: bytes) -> list[tuple[str, bytes]]:
    """Every entry of the zip of an export, as its name and its bytes (requirement 19.14)."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return [(name, archive.read(name)) for name in archive.namelist()]


def data_dir_files(data_dir: Path) -> list[tuple[str, bytes]]:
    """Every file below ``data_dir`` but the token file, as its path and its bytes.

    The one file requirement 23.6 excepts is the token file of requirement 23.5, which the
    services of this file never write - their Admin token comes from the environment
    (requirement 23.4), and the test asserts that no such file is there - so the exception
    takes nothing out of the scan.
    """
    return [
        (str(path.relative_to(data_dir)), path.read_bytes())
        for path in sorted(data_dir.rglob("*"))
        if path.is_file() and path.name != TOKEN_FILENAME
    ]


def answer_bytes(label: str, response: httpx.Response) -> tuple[str, bytes]:
    """One HTTP answer as bytes: its status code, its headers and its body.

    Requirement 23.6 is about the answer, so its headers are in this: a token that reached a
    ``WWW-Authenticate`` or any other header would be as much a leak as one in a body.
    """
    headers = "".join(f"{name}: {value}\n" for name, value in response.headers.items())
    return label, f"{response.status_code}\n{headers}\n".encode() + response.content


@pytest.mark.req("23.6")
def test_no_output_of_the_service_carries_either_token(leak_service: Service) -> None:
    """Requirement 23.6: neither token is in a stream, a file or an answer of the service.

    Both are given in the environment, so their values are known here (requirements 23.3 and
    23.4). What is read: the two streams of the process, the trace and the diff of a sandbox
    that served a session, the zip of an export and every entry in it, every file below
    ``--data-dir``, and the answers of both ports - the successful ones and the refusals,
    whose bodies and headers are the ones most likely to repeat what a request carried.
    """
    service = leak_service
    sandbox_id = service.create_sandbox()
    path, initialized = service.initialize(sandbox_id)
    assert initialized.status_code == HTTPStatus.OK, initialized.text
    session_id = initialized.headers[SESSION_ID_HEADER]
    called = service.call(path, session_id, 2)
    assert called.json()["result"]["isError"] is False, called.text

    export = service.admin("POST", EXPORTS_PATH, json={})
    assert export.status_code == HTTPStatus.OK, export.text
    assert export.headers["content-type"] == "application/zip"
    entries = zip_entries(export.content)
    files = data_dir_files(service.data_dir)
    not_an_mcp_path = httpx.get(
        service.mcp_url(HEALTH_PATH), headers=service.mcp_headers(), timeout=REQUEST_TIMEOUT_S
    )
    assert not_an_mcp_path.status_code == HTTPStatus.NOT_FOUND, not_an_mcp_path.text

    sources: list[tuple[str, bytes]] = [
        ("stdout", service.stdout),
        ("stderr", service.stderr),
        ("trace", service.trace_text(sandbox_id).encode("utf-8")),
        ("diff", service.diff_response(sandbox_id).content),
        ("export.zip", export.content),
        *((f"export.zip/{name}", data) for name, data in entries),
        *((f"data-dir/{name}", data) for name, data in files),
        # The MCP port: the session, the call, and the refusals of requirements 23.1 and 23.3.
        answer_bytes("mcp initialize", initialized),
        answer_bytes("mcp tools/call", called),
        answer_bytes("mcp 404", not_an_mcp_path),
        answer_bytes("mcp 401", service.mcp_post(path, call_body(3), token=None)),
        answer_bytes("mcp 401 wrong", service.mcp_post(path, call_body(3), token="guessed")),
        answer_bytes("mcp 403", service.mcp_post(path, call_body(3), origin=REFUSED_ORIGIN)),
        # The Admin port: its own answers and the 401 and 404 of requirements 19.2 and 19.15.
        answer_bytes("admin health", service.admin("GET", HEALTH_PATH)),
        answer_bytes("admin sandboxes", service.admin("GET", SANDBOXES_PATH)),
        answer_bytes("admin sandbox", service.metadata(sandbox_id)),
        answer_bytes("admin trace", service.trace_response(sandbox_id)),
        answer_bytes("admin diff", service.diff_response(sandbox_id)),
        answer_bytes("admin export", export),
        answer_bytes("admin 401", service.admin("GET", HEALTH_PATH, token=None)),
        answer_bytes("admin 401 wrong", service.admin("GET", HEALTH_PATH, token="guessed")),
        answer_bytes("admin 404", service.admin("GET", UNKNOWN_ADMIN_PATH)),
    ]

    # The scan is not vacuous: the sandbox served a session and a call, the export holds its
    # two entries, the data directory holds the files of a service that has run, and no token
    # file was written at all (requirements 19.14 and 23.4).
    assert [name for name, _ in entries] == [
        f"{sandbox_id}/trace.jsonl",
        f"{sandbox_id}/diff.json",
    ]
    assert [event["event"] for event in service.events(sandbox_id)] == [
        EventName.SESSION_START,
        EventName.TOOLS_CALL,
    ]
    assert len(files) >= LEAST_DATA_FILES
    assert not (service.data_dir / TOKEN_FILENAME).exists()

    for label, data in sources:
        for name, token in (
            (ADMIN_TOKEN_VAR, GIVEN_ADMIN_TOKEN),
            (MCP_TOKEN_VAR, GIVEN_MCP_TOKEN),
        ):
            assert token.encode("utf-8") not in data, f"{name} 的值出现在 {label} 中"

    code = service.terminate()

    # The streams of the finished process, which name the two variables and carry neither of
    # their values (requirements 22.1 and 23.6).
    assert code == EXIT_OK
    assert service.stdout == b""
    assert ADMIN_TOKEN_VAR.encode("utf-8") in service.stderr
    for token in (GIVEN_ADMIN_TOKEN, GIVEN_MCP_TOKEN):
        assert token.encode("utf-8") not in service.stderr
        assert token.encode("utf-8") not in service.stdout
