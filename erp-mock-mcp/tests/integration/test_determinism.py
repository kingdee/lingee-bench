"""One Request_Sequence, two transports, two processes, two hash seeds (requirement 25.4).

tests/property/test_p30_determinism.py is the randomized half of requirement 24: hundreds
of drawn Request_Sequences, two sandboxes of one Sandbox_Manager inside the test process,
and the comparisons of requirements 24.1 to 24.4 and 24.11 over every one of them. What a
property that runs in one process cannot reach is what requirement 25.4 asks for by name -
two sandboxes created over *different transports*, by *different processes*, below
*different* ``--data-dir`` - and the three conditions of requirements 24.9, 24.10 and
24.11. This file is that half: one sequence, real subprocesses, the official MCP Python SDK
client on the other end of each of them.

The sequence (:func:`run_script`). Seventeen steps over ``instances/p2p-demo``: the
business context, two tools/list, the three searches, an order with one line per enabled
material of the dataset, an update of that draft, the four submit and approve calls of the
order and of the receipt pushed from it, the get that shows the order closed, the balance
query of the warehouse the receipt fed - and two calls that are wrong on purpose, a get of
an id no record answers (NOT_FOUND) and a second submit of a bill that is already submitted
(INVALID_STATE). So the sequence writes, reads, pushes, fails and lists, which is what
makes the three yardsticks below worth comparing; a tools/list changes nothing but takes a
sequence number and a trace event, which is exactly why it is part of it.

Nothing of the sequence is written down that the service can say itself: every internal id
comes from an earlier answer (the searches, the create, the push), the delivery date is the
business date the service reported, and the id of the missing record is built from the
prefix of the order's own id. The first run therefore *records* the requests it made
(:class:`Caller`), and every later run replays those recorded bytes - which is also how the
ids are checked: a sandbox that numbered its records differently would answer the replayed
requests differently and fail the comparison.

The three yardsticks, as requirements 24.1 to 24.3 define them and p30 applies them:

===========  =============================================================================
results      per step the ``isError``, the ``text`` as it came and the
             ``structuredContent`` as one line of JSON, so values and key order both count;
             a tools/list is compared by its whole payload (requirement 24.9)
diff         the bytes of the diff against ``initial``, with the value of ``sandbox_id``
             replaced by one string - over HTTP from ``GET /admin/sandboxes/{id}/diff``,
             over stdio from the ``diff.json`` the exit writes, which are the same bytes
             (requirement 24.2)
trace        every event with ``ts``, ``latency_ms``, ``sandbox_id`` and ``session_id``
             dropped, compared as JSON with its keys in order; both sessions have ended
             before a trace is read, which is the condition of requirement 24.3
===========  =============================================================================

The five scenarios, one test each:

==========================================  ===================================  =========
test                                        what differs                         covers
==========================================  ===================================  =========
:func:`test_the_same_sequence_over_two_transports_in_two_processes_agrees`
                                            transport, process, data directory,  24.1-24.3
                                            and how the sandbox was created
:func:`test_the_same_sequence_under_two_hash_seeds_agrees`
                                            ``PYTHONHASHSEED`` 0 and 12345       24.9
:func:`test_other_sandboxes_running_at_the_same_time_change_nothing`
                                            neighbours in the same process       24.10
:func:`test_the_replay_after_a_reset_agrees_with_the_first_run`
                                            an Admin reset in between            24.4
:func:`test_the_replay_after_restoring_a_named_snapshot_agrees`
                                            an Admin restore in between          24.11
==========================================  ===================================  =========

Every one of them starts from one run, recorded once for the module (:func:`recorded`): the
script in a prebuilt sandbox of the serve process, over Streamable HTTP. That run is where
the sequence and the anchor date come from; it is what the first, the third and the fourth
test compare their own run against - it had the service process to itself, no other
request of this module having reached it yet, which is the "no other sandbox executing" of
requirement 24.10 - and its sandbox is the one the fourth test resets. The second and the
fifth test compare two runs of their own and take nothing from it but the sequence and the
date.

The anchor date. ``clock.mode`` of p2p-demo is today and ``serve`` has no
``--anchor-date``, so the date is the day that process runs and no test may read a clock to
learn it (requirement 25.10): it comes from the ``anchor_date`` of the sandbox the Admin API
prebuilt (requirement 19.3), and the same value is then handed to every other sandbox - as
the ``anchor_date`` of an Admin request and as the ``--anchor-date`` of a stdio process - so
the premise of requirement 24.1 holds however long the module runs and whatever hour it
starts at. The business date the service reports and the ``anchor_date`` of its diff are
asserted against that one value rather than against anything computed here.

The processes, as in tests/integration/test_protocol_flow.py and
tests/integration/test_cli_serve.py: the console script of ``[project.scripts]`` beside the
interpreter running the tests (:data:`COMMAND`), the environment built from nothing but
:data:`PASSED_THROUGH` plus what a test sets, so no ``ERP_MOCK_*`` of the machine reaches a
service (requirement 23.9), and ``--data-dir`` below a ``tmp_path``, so each process builds
the Snapshot of ``purchase-basic`` in a cache of its own and pytest removes what it wrote.
One serve process serves the whole module and three stdio processes are started by the two
tests that need one. Every wait is a poll against a deadline (:func:`wait_for`,
:func:`wait_for_session_end`, :func:`anyio.fail_after`), and the module as a whole has a
wall clock budget (:class:`Budget`), so a run that went wrong says so instead of hanging.

The resource limits are the ones a plain process has (requirement 23.9) and no call of the
sequence may be refused by them, which is the condition requirement 24.1 puts on a
Request_Sequence: the sequence holds fewer tools/call than the call rate of a session
allows per second (:func:`check_sequence`), and every comparison asserts that no answer was
such a refusal rather than trusting the arithmetic. The neighbours of requirement 24.10 are
the exception that proves the rule - their own answers are compared with nothing, so a
refusal among them means nothing, while their requests still queue, write and trace in the
same process as the run under test.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import anyio
import httpx
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from erp_mock.cli.serve import TOKEN_FILENAME
from erp_mock.common.errors import ErrorCode
from erp_mock.gateway.handlers import SERVER_NAME
from erp_mock.gateway.router import sandbox_path
from erp_mock.gateway.sandbox import SANDBOXES_DIR
from erp_mock.gateway.serve import ADMIN_HOST, bind_socket
from erp_mock.gateway.stdio import DIFF_FILENAME
from erp_mock.gateway.trace import TRACE_FILENAME, EventName
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET
from erp_mock.tools.layer import LIMIT_KEY, LIMITS, MAX_CALLS_PER_SECOND
from erp_mock.tools.results import CODE, DETAILS, ERROR_KEY
from tests.property.opseq import Call, missing_id

# The example instance and the example business packs of the repository, which every
# process of this file serves (requirement 25.4).
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INSTANCE_DIR: Final = REPO_ROOT / "instances" / "p2p-demo"
PACKS_DIR: Final = REPO_ROOT / "packs"

# What the instance and its one initial dataset are called where the service writes each as
# ``<名称>@<版本>`` (requirements 18.1 and 19.12).
INSTANCE_REF: Final = "p2p-demo@1.0.0"
DATASET_REF: Final = "purchase-basic@1.0.0"

# The installed command, beside the interpreter that runs the tests (module docstring).
COMMAND: Final = Path(sys.executable).parent / "erp-mock-mcp"

# The variables a subprocess is given beyond what a test sets: enough to run, and not one
# of the ``ERP_MOCK_*`` the environment of the test process may carry (requirement 23.9).
PASSED_THROUGH: Final = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")

# The variable requirement 24.9 is about, and the two values it is given here.
HASH_SEED_VAR: Final = "PYTHONHASHSEED"
HASH_SEEDS: Final = ("0", "12345")

# The deadlines. Starting a service loads p2p-demo and builds the Snapshot of
# ``purchase-basic``, which together take a few hundred milliseconds, and a session is at
# most seventeen requests; a test that reaches one of these has already failed. The budget
# is the wall clock of the whole module (:class:`Budget`).
START_TIMEOUT_S: Final = 30.0
SESSION_TIMEOUT_S: Final = 60.0
STOP_TIMEOUT_S: Final = 15.0
ADMIN_TIMEOUT_S: Final = 10.0
TRACE_TIMEOUT_S: Final = 20.0
POLL_SECONDS: Final = 0.05
MODULE_BUDGET_S: Final = 360.0

# The loopback address both ports of the serve process are on: the only address the Admin
# port is ever on (requirement 19.1), and a ``--host`` that needs no MCP endpoint token.
LOOPBACK: Final = ADMIN_HOST

# The addresses this file uses (requirements 14.2, 19.8, 19.9 and 19.11).
MCP_PATH: Final = "/mcp"
HEALTH_PATH: Final = "/admin/health"
SANDBOXES_PATH: Final = "/admin/sandboxes"

# What the client calls itself in the handshake of every session here.
CLIENT_INFO: Final = types.Implementation(name="determinism-test", version="0.0.0")

# The Named_Snapshot of requirement 24.11, and the name of the base a diff is taken against
# (requirements 19.5 and 19.8).
NAMED: Final = "mid"

# What the value of ``sandbox_id`` is replaced by before two diffs are compared
# (requirement 24.2). The ids of the records of a sandbox carry no sandbox id, so replacing
# the id in the text replaces exactly the one value the key holds.
SANDBOX_PLACEHOLDER: Final = "sb_0000000000000000"

# The keys requirement 24.3 drops from a trace event before two of them are compared.
VOLATILE_KEYS: Final[frozenset[str]] = frozenset({"ts", "latency_ms", "sandbox_id", "session_id"})

# The keys of a diff this file reads by name: the change list and the one key requirement
# 24.4 leaves out of the comparison after a reset (requirements 18.1 and 18.3).
CHANGES: Final = "changes"
CAUSE_SEQS: Final = "cause_seqs"
ANCHOR_DATE_KEY: Final = "anchor_date"

# How many sandboxes execute requests beside the one under test in the test of
# requirement 24.10, and how much of a value an assertion message prints.
NEIGHBOURS: Final = 2
REPORTED_CHARS: Final = 240


# --- what the sequence of this module is made of -----------------------------------------

# The tool names of p2p-demo the script calls, as its packs generate them (req 21.4).
SUPPLIER_SEARCH: Final = "supplier_search"
MATERIAL_SEARCH: Final = "material_search"
WAREHOUSE_SEARCH: Final = "warehouse_search"
ORDER_CREATE: Final = "purchase_order_create"
ORDER_UPDATE: Final = "purchase_order_update"
ORDER_SUBMIT: Final = "purchase_order_submit"
ORDER_APPROVE: Final = "purchase_order_approve"
ORDER_GET: Final = "purchase_order_get"
RECEIPT_CREATE: Final = "purchase_receipt_create_from_order"
RECEIPT_SUBMIT: Final = "purchase_receipt_submit"
RECEIPT_APPROVE: Final = "purchase_receipt_approve"
BALANCE_QUERY: Final = "inventory_balance_query"

# The facts of the business configuration the script depends on, the same three
# tests/integration/test_protocol_flow.py writes down and tests/packs/test_p2p_demo.py
# checks against the Snapshot: every record of ``purchase-basic`` carries 演示 in its name,
# the enabled records are the ones a new bill may name (R-GEN-006, requirement 5.7), and
# WH01 is the first warehouse of core's shared basic data.
DEMO_KEYWORD: Final = "演示"
ENABLED: Final = "enabled"
WAREHOUSE_CODE: Final = "WH01"

# What the order of the script asks for per line, and the remark the update writes: both
# JSON numbers, as the input schema declares them (requirement 11.6).
ORDER_QTY: Final = 3
ORDER_PRICE: Final = 10.08
REMARK: Final = "确定性测试"

# The state the order reaches without any call asking for it: the receipt's writeback closes
# it (requirement 8.13), which is the last thing the script asserts about the state.
CLOSED: Final = "closed"

# What the sequence has to hold for the comparisons to mean anything (:func:`check_sequence`).
EXPECTED_LISTS: Final = 2
EXPECTED_FAILURES: Final = 2


@pytest.fixture(scope="module", autouse=True)
def installed_command() -> None:
    """Every test here runs the console script; a run without it would be meaningless."""
    assert COMMAND.is_file(), f"没有找到安装好的命令：{COMMAND}"
    assert os.access(COMMAND, os.X_OK)


@pytest.fixture
def anyio_backend() -> str:
    """The one event loop these tests run on, as every async test of the suite does."""
    return "asyncio"


@dataclass(frozen=True, slots=True)
class Budget:
    """The wall clock the whole module is allowed (module docstring).

    Every wait of this file has a deadline of its own, and this is the one above them: a
    run that is past it stops at the next test instead of adding minutes to the suite, and
    says how long it took.
    """

    started: float

    @property
    def spent(self) -> float:
        """How long the module has been running, in seconds."""
        return time.monotonic() - self.started

    def check(self, what: str) -> None:
        """Fail ``what`` where the module has already spent its budget."""
        if self.spent > MODULE_BUDGET_S:
            pytest.fail(
                f"本模块已运行 {self.spent:.1f} 秒，超过墙钟上限 {MODULE_BUDGET_S} 秒，"
                f"不再执行{what}；请检查是否有子进程或会话被拖慢"
            )


@pytest.fixture(scope="module")
def budget() -> Budget:
    """The wall clock budget of the module, started when the first test asks for it."""
    return Budget(started=time.monotonic())


@pytest.fixture(autouse=True)
def within_budget(request: pytest.FixtureRequest, budget: Budget) -> None:
    """Stop the module at the first test that starts after the budget is spent."""
    budget.check(f"用例 {request.node.name}")


def process_env(**extra: str) -> dict[str, str]:
    """The environment of one subprocess: those of :data:`PASSED_THROUGH` and ``extra``."""
    inherited = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    return {**inherited, **extra}


# --- the serve process, its MCP port and its Admin port (requirements 19.1 and 22.1) -----


@dataclass(frozen=True, slots=True)
class Service:
    """The one serve process of this module, and what a test asks of it.

    ``client`` carries the Authorization header of the token serve generated and wrote to
    its token file on every request of the Admin port (requirements 23.5 and 19.2).
    """

    process: subprocess.Popen[bytes]
    port: int
    client: httpx.Client
    err: Path

    @property
    def stderr(self) -> str:
        """What the process has written to stderr: its log (design.md 7.3)."""
        return self.err.read_text(encoding="utf-8")

    def mcp_url(self, sandbox_id: str | None = None) -> str:
        """An MCP address of this process (requirement 14.2).

        A sandbox id is the address of that prebuilt sandbox, which binds a session to it
        (requirement 15.3); None is ``/mcp``, where an initialize creates a sandbox of its
        own (requirement 15.1).
        """
        path = MCP_PATH if sandbox_id is None else sandbox_path(sandbox_id)
        return f"http://{LOOPBACK}:{self.port}{path}"

    def sandbox_of_session(self, session_id: str) -> Mapping[str, Any]:
        """The metadata of the sandbox the session ``session_id`` is bound to (19.12).

        The listing holds every sandbox of the process, each with the sessions that were
        ever bound to it, so the sandbox an initialize created for one session is found
        without a test having to know which sandboxes the module left behind
        (requirement 19.11).
        """
        response = self.client.get(SANDBOXES_PATH)
        assert response.status_code == httpx.codes.OK, response.text
        listed = response.json()
        found = [item for item in listed["items"] if session_id in item["session_ids"]]
        assert len(found) == 1, f"会话 {session_id} 没有对应唯一的沙箱：{listed}"
        return dict(found[0])

    def serving(self) -> bool:
        """Whether the Admin port answers the token of the process with 200 (req 19.9)."""
        try:
            response = self.client.get(HEALTH_PATH)
        except httpx.TransportError:
            return False
        return response.status_code == httpx.codes.OK

    def post(self, path: str, body: Mapping[str, Any]) -> httpx.Response:
        """``POST path`` with ``body`` as the JSON request body (requirement 19.2)."""
        return self.client.post(path, json=dict(body))

    def prebuilt(self, anchor_date: str | None = None) -> tuple[str, str]:
        """Prebuild one sandbox and return its id and its anchor date (req 19.3).

        ``anchor_date`` of None leaves the date to the clock of the instance, which is in
        mode today, and the answer is then what that day was for the process - the one date
        this module gets from the service rather than from a test (module docstring).
        """
        body: dict[str, Any] = {} if anchor_date is None else {ANCHOR_DATE_KEY: anchor_date}
        response = self.post(SANDBOXES_PATH, body)
        assert response.status_code == httpx.codes.CREATED, response.text
        payload = response.json()
        assert payload["dataset"] == DATASET_REF, payload
        if anchor_date is not None:
            assert payload[ANCHOR_DATE_KEY] == anchor_date, payload
        return str(payload["sandbox_id"]), str(payload[ANCHOR_DATE_KEY])

    def save(self, sandbox_id: str, name: str) -> None:
        """Save the Named_Snapshot ``name`` of ``sandbox_id`` (requirement 19.5)."""
        response = self.post(f"{SANDBOXES_PATH}/{sandbox_id}/snapshots", {"name": name})
        assert response.status_code == httpx.codes.CREATED, response.text

    def restore(self, sandbox_id: str, name: str) -> None:
        """Restore ``sandbox_id`` to the Named_Snapshot ``name`` (requirement 19.6)."""
        response = self.post(f"{SANDBOXES_PATH}/{sandbox_id}/restore", {"name": name})
        assert response.status_code == httpx.codes.OK, response.text

    def reset(self, sandbox_id: str) -> None:
        """Reset the database of ``sandbox_id`` to the Snapshot (requirement 19.4)."""
        response = self.post(f"{SANDBOXES_PATH}/{sandbox_id}/reset", {})
        assert response.status_code == httpx.codes.OK, response.text

    def trace(self, sandbox_id: str) -> tuple[dict[str, Any], ...]:
        """The trace events of ``sandbox_id``, as the API hands them over (req 19.7)."""
        response = self.client.get(f"{SANDBOXES_PATH}/{sandbox_id}/trace")
        assert response.status_code == httpx.codes.OK, response.text
        return events_of(response.content.decode("utf-8"))

    def diff(self, sandbox_id: str) -> bytes:
        """The bytes of the diff of ``sandbox_id`` against ``initial`` (req 19.8)."""
        response = self.client.get(f"{SANDBOXES_PATH}/{sandbox_id}/diff")
        assert response.status_code == httpx.codes.OK, response.text
        return response.content

    def terminate(self) -> int:
        """Stop the process with SIGTERM and return the exit code it ended with."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=STOP_TIMEOUT_S)


def free_port() -> int:
    """A port of the loopback address that nothing held a moment ago.

    Bound the way serve binds its sockets and released again, so a test can name it in
    ``--port`` or ``--admin-port`` (:func:`~erp_mock.gateway.serve.bind_socket`).
    """
    with closing(bind_socket(LOOPBACK, 0)) as sock:
        return int(sock.getsockname()[1])


def wait_for(
    process: subprocess.Popen[bytes], err: Path, condition: Callable[[], bool], what: str
) -> None:
    """Poll ``condition`` until it holds, failing the test where it does not in time.

    A process that ended while the wait was on fails the test at once, with what it wrote on
    stderr: a service that could not start has already said why.
    """
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if condition():
            return
        code = process.poll()
        if code is not None:
            pytest.fail(
                f"serve 进程在{what}之前以 {code} 退出：\n{err.read_text(encoding='utf-8')}"
            )
        time.sleep(POLL_SECONDS)
    pytest.fail(f"等待{what}超时（{START_TIMEOUT_S} 秒）：\n{err.read_text(encoding='utf-8')}")


@contextmanager
def serve_process(workdir: Path) -> Iterator[Service]:
    """Start one serve process over p2p-demo on two free ports and yield it, serving.

    stdout and stderr go to files below ``workdir`` rather than to pipes, so a test reads
    them while the process runs, and whatever is still running when the block ends is
    stopped: no test leaves a service behind.
    """
    data_dir = workdir / "var"
    out, err = workdir / "stdout.txt", workdir / "stderr.txt"
    port, admin_port = free_port(), free_port()
    args = [
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
    with out.open("wb") as out_file, err.open("wb") as err_file:
        process = subprocess.Popen(
            args,
            cwd=workdir,
            env=process_env(),
            stdin=subprocess.DEVNULL,
            stdout=out_file,
            stderr=err_file,
        )
    token_file = data_dir / TOKEN_FILENAME
    try:
        wait_for(process, err, token_file.is_file, "写出 Admin 令牌文件")
        token = token_file.read_text(encoding="utf-8")
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{admin_port}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=ADMIN_TIMEOUT_S,
        ) as client:
            service = Service(process=process, port=port, client=client, err=err)
            wait_for(process, err, service.serving, "两个端口开始监听")
            yield service
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        process.wait(timeout=STOP_TIMEOUT_S)


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    """One serve process for the whole module (module docstring).

    Loading p2p-demo and building the Snapshot of ``purchase-basic`` happens once; the
    sandboxes of the tests that speak Streamable HTTP are prebuilt through its Admin API.
    """
    with serve_process(tmp_path_factory.mktemp("serve")) as started:
        yield started


# --- one step of the sequence and what it was answered with ------------------------------


@dataclass(frozen=True, slots=True)
class ListStep:
    """One tools/list of the sequence (requirement 14.5).

    It changes nothing of the sandbox and returns the frozen payload of the profile, and it
    is part of a Request_Sequence all the same: it takes the next sequence number of the
    sandbox and writes a ``tools_list`` event, both of which the comparisons of
    requirements 24.2 and 24.3 cover, and its payload is one of the things requirement 24.9
    asks to be the same under two hash seeds.
    """


# The one tools/list step; the sequence holds this object at every list position.
LIST: Final = ListStep()

# One step of a Request_Sequence: a tools/call of a tool with its arguments
# (:class:`~tests.property.opseq.session.Call`), or a tools/list.
type Step = Call | ListStep


@dataclass(frozen=True, slots=True)
class Answered:
    """What one step of the sequence was answered with (requirements 24.1 and 24.9).

    - ``step``: the request that was sent, for the failure report;
    - ``is_error``: the ``isError`` of a tools/call result (requirement 13.6);
    - ``text``: its one text content item, as it came - two of these are Python strings
      the SDK decoded from the same UTF-8, so comparing them is the byte for byte
      comparison requirement 24.1 asks for;
    - ``structured``: its ``structuredContent`` as one line of JSON, so two of them differ
      as soon as a value or the order of a key does; None for a failed call, which carries
      no ``structuredContent`` at all;
    - ``listed``: the whole payload of a tools/list as one line of JSON, None for a
      tools/call.
    """

    step: Step
    is_error: bool = False
    text: str = ""
    structured: str | None = None
    listed: str | None = None

    @property
    def data(self) -> dict[str, Any]:
        """The structured result of a successful call, as a JSON tree (req 13.4)."""
        assert self.structured is not None, f"{_step_text(self.step)} 没有 structuredContent"
        payload = json.loads(self.structured)
        assert isinstance(payload, dict), payload
        return payload

    @property
    def error(self) -> Mapping[str, Any] | None:
        """The error object of a failed call, None for one that went through (13.6)."""
        if not self.is_error:
            return None
        payload = json.loads(self.text)
        found = payload[ERROR_KEY]
        assert isinstance(found, dict), f"错误结果的 {ERROR_KEY} 不是对象：{found}"
        return found

    @property
    def code(self) -> str | None:
        """The error code of a failed call, None for a successful one (requirement 13.7)."""
        error = self.error
        return None if error is None else str(error[CODE])

    @property
    def refused_limit(self) -> str | None:
        """The resource limit that refused this call, None where none did (req 23.12).

        The condition requirement 24.1 puts on a Request_Sequence is that no call of it was
        refused for calling too fast; the three limits are reported the same way, so this
        names whichever of them bit. A LIMIT_EXCEEDED of a parameter over its bound carries
        no such detail and is no refusal of the sandbox (requirement 12.3).
        """
        error = self.error
        if error is None or error[CODE] != ErrorCode.LIMIT_EXCEEDED.value:
            return None
        details = error.get(DETAILS) or []
        names = [item[LIMIT_KEY] for item in details if LIMIT_KEY in item]
        return next((str(name) for name in names if name in LIMITS), None)

    @property
    def compared(self) -> tuple[bool, str, str | None, str | None]:
        """Everything about this answer that two runs have to agree on (req 24.1)."""
        return (self.is_error, self.text, self.structured, self.listed)


# What one run of the sequence was answered with, step by step.
type Answers = tuple[Answered, ...]


@dataclass(slots=True)
class Caller:
    """A live session of the official SDK client, and the steps it has made.

    :meth:`call` and :meth:`list_tools` are what the script sends; :meth:`replay` sends
    recorded steps back as they were. ``steps`` and ``answers`` are what the run recorded,
    which is what a later run is replayed from and compared against; a caller with
    ``record`` false keeps neither, which is what the neighbours of requirement 24.10 are.
    """

    client: ClientSession
    record: bool = True
    steps: list[Step] = field(default_factory=list)
    answers: list[Answered] = field(default_factory=list)

    async def call(
        self, tool: str, args: Mapping[str, Any], *, fails: ErrorCode | None = None
    ) -> Answered:
        """Send one tools/call and assert it ended the way the script expects.

        ``fails`` of None is a call that has to go through; an error code is a call that has
        to end with exactly that code, which is what the two deliberate mistakes of the
        sequence are (module docstring).
        """
        answered = await self.send(Call(tool, dict(args)))
        if fails is None:
            assert not answered.is_error, f"{tool} 的调用结果 isError 为 true：{answered.text}"
        else:
            assert answered.is_error, f"{tool} 本应失败，却返回了 {answered.structured}"
            assert answered.code == fails.value, (
                f"{tool} 的错误码是 {answered.code}：{answered.text}"
            )
        return answered

    async def list_tools(self) -> Answered:
        """Send one tools/list and keep its whole payload (requirement 11.10)."""
        return await self.send(LIST)

    async def replay(self, steps: Sequence[Step]) -> Answers:
        """Send ``steps`` in order, as they were recorded, and return every answer."""
        return tuple([await self.send(step) for step in steps])

    async def send(self, step: Step) -> Answered:
        """Send one step of the sequence and record what it was answered with."""
        answered = await (self._list() if isinstance(step, ListStep) else self._call(step))
        if self.record:
            self.steps.append(step)
            self.answers.append(answered)
        return answered

    async def _call(self, step: Call) -> Answered:
        """One tools/call over the session, read off the response (requirement 13.1).

        ``ClientSession.call_tool`` is avoided on purpose: it issues a tools/list of its
        own, which would be a request of the sequence that nothing recorded, and validates
        ``structuredContent`` against the outputSchema on the client side, which is no part
        of what the server answered.
        """
        request = types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name=step.tool, arguments=None if step.args is None else dict(step.args)
                )
            )
        )
        result = await self.client.send_request(request, types.CallToolResult)
        assert len(result.content) == 1, f"{step.tool} 的结果有 {len(result.content)} 个内容项"
        item = result.content[0]
        assert isinstance(item, types.TextContent), f"{step.tool} 的结果不是文本内容：{item}"
        return Answered(
            step=step,
            is_error=bool(result.isError),
            text=item.text,
            structured=(
                None if result.structuredContent is None else canonical(result.structuredContent)
            ),
        )

    async def _list(self) -> Answered:
        """One tools/list over the session, payload and all (requirement 14.5)."""
        result = await self.client.list_tools()
        return Answered(
            step=LIST,
            listed=canonical(result.model_dump(mode="json", by_alias=True, exclude_none=True)),
        )


# --- the Request_Sequence of this module -------------------------------------------------


async def run_script(caller: Caller, anchor_date: str) -> None:
    """Make the seventeen steps of this module's Request_Sequence (module docstring).

    Every id comes from an earlier answer and nothing is computed from a clock: the business
    date the service reports has to be the anchor date of its sandbox (requirement 16.10),
    and it is what the order asks for as its delivery date, which R-PUR-001 allows
    (requirement 21.6). The two wrong calls name the error code they have to end with, and
    the last three calls assert what the sequence has left behind: the order was closed by
    the receipt's writeback, and the warehouse the receipt fed holds stock
    (requirements 8.13 and 9.12).
    """
    context = await caller.call(BUSINESS_CONTEXT_GET, {})
    business_date = str(context.data["business_date"])
    assert business_date == anchor_date, f"业务日期 {business_date} 不是锚点日期 {anchor_date}"
    await caller.list_tools()

    suppliers = await caller.call(SUPPLIER_SEARCH, {"keyword": DEMO_KEYWORD, "status": ENABLED})
    materials = await caller.call(MATERIAL_SEARCH, {"keyword": DEMO_KEYWORD, "status": ENABLED})
    warehouses = await caller.call(WAREHOUSE_SEARCH, {"status": ENABLED})
    supplier_id = str(enabled_items(suppliers)[0]["id"])
    material_ids = [str(item["id"]) for item in enabled_items(materials)]
    warehouse_id = str(by_code(enabled_items(warehouses), WAREHOUSE_CODE)["id"])
    assert len(material_ids) >= 2, f"数据集至少要有两个启用物料，实际 {material_ids}"

    order = await caller.call(
        ORDER_CREATE,
        {
            "supplier_id": supplier_id,
            "delivery_date": business_date,
            "lines": [
                {"material_id": material_id, "qty": ORDER_QTY, "price": ORDER_PRICE}
                for material_id in material_ids
            ],
        },
    )
    order_id = str(order.data["id"])
    lines = [dict(line) for line in order.data["lines"]]
    await caller.call(ORDER_UPDATE, {"id": order_id, "remark": REMARK})

    # The first of the two deliberate mistakes: an id of the right shape that no record of
    # the sandbox answers (requirement 4.1), built from the prefix of the order's own id.
    await caller.call(ORDER_GET, {"id": missing_id(prefix_of(order_id))}, fails=ErrorCode.NOT_FOUND)

    await caller.call(ORDER_SUBMIT, {"id": order_id})
    # And the second: the same action again, which the state machine refuses (req 7.4).
    await caller.call(ORDER_SUBMIT, {"id": order_id}, fails=ErrorCode.INVALID_STATE)
    await caller.call(ORDER_APPROVE, {"id": order_id})

    # Every line of the order, in full and into the one warehouse: the quantity is the text
    # the order line came back as, which requirement 12.5 lets a client send back as it got
    # it, so the receipt takes the whole order in and the writeback closes it.
    receipt = await caller.call(
        RECEIPT_CREATE,
        {
            "order_id": order_id,
            "lines": [
                {
                    "source_line_id": line["id"],
                    "qty": line["qty"],
                    "warehouse_id": warehouse_id,
                }
                for line in lines
            ],
        },
    )
    receipt_id = str(receipt.data["id"])
    await caller.call(RECEIPT_SUBMIT, {"id": receipt_id})
    await caller.call(RECEIPT_APPROVE, {"id": receipt_id})
    await caller.list_tools()

    closed = await caller.call(ORDER_GET, {"id": order_id})
    assert closed.data["status"] == CLOSED, closed.data
    balances = await caller.call(BALANCE_QUERY, {"warehouse_id": warehouse_id})
    assert balances.data["items"], f"入库后 {WAREHOUSE_CODE} 的即时库存为空：{balances.data}"


def enabled_items(answer: Answered) -> list[Mapping[str, Any]]:
    """The items of a search that filtered by status, every one of them enabled.

    A search that returned nothing would make the rest of the sequence meaningless, and a
    disabled record among the items would be one a create refuses (R-GEN-006): the dataset
    holds a disabled supplier and a disabled material, and the ``status`` of the request is
    what keeps them out of what a line is picked from (requirement 5.7).
    """
    items: list[Mapping[str, Any]] = [dict(item) for item in answer.data["items"]]
    assert items, f"{_step_text(answer.step)} 没有返回任何记录：{answer.data}"
    assert {str(item["status"]) for item in items} == {ENABLED}, items
    return items


def by_code(items: Sequence[Mapping[str, Any]], code: str) -> Mapping[str, Any]:
    """The one item of ``items`` whose ``code`` is ``code`` (requirement 13.3)."""
    found = [item for item in items if item["code"] == code]
    assert len(found) == 1, f"检索结果中编码为 {code} 的记录不唯一：{items}"
    return found[0]


def prefix_of(identifier: str) -> str:
    """The id prefix of ``identifier``, which an id of a missing record is built from."""
    prefix, _, number = identifier.partition("_")
    assert number, f"内部 ID {identifier} 不是 <前缀>_<序号> 的形式"
    return prefix


def split_at(steps: Sequence[Step]) -> int:
    """The step the Named_Snapshot of requirement 24.11 is taken before: the push.

    The tail that is replayed then holds the push, the two state operations of the receipt,
    a tools/list, the get that shows the order closed and the balance query - a part of the
    sequence that writes, so a restore that did not put the counters and the records back
    would answer it differently (requirement 19.6).
    """
    found = [
        index
        for index, step in enumerate(steps)
        if isinstance(step, Call) and step.tool == RECEIPT_CREATE
    ]
    assert len(found) == 1, f"序列中 {RECEIPT_CREATE} 的位置不唯一：{found}"
    return found[0]


# --- one run of the sequence, and what it left behind ------------------------------------


@dataclass(frozen=True, slots=True)
class Run:
    """What one execution of the sequence left behind, ready to be compared.

    - ``sandbox_id``: the sandbox it ran in, which two compared runs must not share;
    - ``answers``: what each step was answered with (requirement 24.1);
    - ``diff``: the bytes of the diff against ``initial``, with the value of ``sandbox_id``
      replaced by :data:`SANDBOX_PLACEHOLDER` (requirement 24.2);
    - ``trace``: the events of the sandbox, in the order they were written (req 24.3).
    """

    sandbox_id: str
    answers: Answers
    diff: bytes
    trace: tuple[dict[str, Any], ...]

    @property
    def calls(self) -> tuple[Answered, ...]:
        """The tools/call answers of the run, the tools/list left out."""
        return tuple(answer for answer in self.answers if isinstance(answer.step, Call))

    @property
    def changes(self) -> list[Mapping[str, Any]]:
        """The changes of the diff, in the order it wrote them (requirement 18.6)."""
        payload = json.loads(self.diff)
        return [dict(change) for change in payload[CHANGES]]


@dataclass(frozen=True, slots=True)
class Recorded:
    """The run the module records once, and the sequence it recorded (module docstring)."""

    steps: tuple[Step, ...]
    run: Run
    anchor_date: str

    @property
    def sandbox_id(self) -> str:
        """The sandbox the recording run used, which requirement 24.4 resets."""
        return self.run.sandbox_id


@asynccontextmanager
async def http_session(
    service: Service, sandbox_id: str, *, record: bool = True
) -> AsyncIterator[Caller]:
    """One session on ``/s/<sandbox_id>/mcp`` of ``service`` (requirement 15.3).

    The transport is given the address and nothing else. Leaving the block terminates the
    session (the transport's DELETE), which unbinds it from the prebuilt sandbox and leaves
    the sandbox as it is, so the next session of a test reaches the same state
    (requirement 15.8) and the trace of the sandbox ends with a ``session_end``, which is
    the condition requirement 24.3 puts on the traces it compares.
    """
    with anyio.fail_after(SESSION_TIMEOUT_S):
        async with streamable_http_client(service.mcp_url(sandbox_id)) as (read, write, _):
            async with ClientSession(read, write, client_info=CLIENT_INFO) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == SERVER_NAME
                yield Caller(client=client, record=record)


async def over_http(
    service: Service, sandbox_id: str, steps: Sequence[Step] | None, anchor_date: str
) -> tuple[tuple[Step, ...], Answers]:
    """Run the sequence in the sandbox ``sandbox_id`` of ``service``, in one session.

    ``steps`` of None runs the script, which is what records the sequence; a list of steps
    replays exactly those requests (:class:`Caller`).
    """
    async with http_session(service, sandbox_id) as caller:
        if steps is None:
            await run_script(caller, anchor_date)
        else:
            await caller.replay(steps)
    return tuple(caller.steps), tuple(caller.answers)


def read_http_run(service: Service, sandbox_id: str, answers: Answers) -> Run:
    """The diff and the trace of a sandbox of the serve process, after its session ended.

    Both are read through the Admin API, which reads them off a sandbox whatever state it is
    in and without changing it (requirements 19.7 and 19.8). The trace is read once the
    ``session_end`` of the session is in the file (:func:`wait_for_session_end`).
    """
    events = wait_for_session_end(service, sandbox_id)
    return Run(
        sandbox_id=sandbox_id,
        answers=answers,
        diff=replaced(service.diff(sandbox_id), sandbox_id),
        trace=events,
    )


def wait_for_session_end(service: Service, sandbox_id: str) -> tuple[dict[str, Any], ...]:
    """Poll the trace of ``sandbox_id`` until its last event is a ``session_end``.

    The DELETE of the transport is answered before the event is written, so the file may be
    one event short for a moment after a session has gone; requirement 24.3 compares the
    traces of two *finished* sessions, so the wait is for that event rather than for a
    fixed span of time.
    """
    deadline = time.monotonic() + TRACE_TIMEOUT_S
    events: tuple[dict[str, Any], ...] = ()
    while time.monotonic() < deadline:
        events = service.trace(sandbox_id)
        if events and events[-1]["event"] == EventName.SESSION_END:
            return events
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"等待沙箱 {sandbox_id} 的 session_end 事件超时（{TRACE_TIMEOUT_S} 秒），"
        f"trace 的最后一个事件是 {events[-1]['event'] if events else '（没有事件）'}；"
        f"服务的日志：\n{_short(service.stderr)}"
    )


def stdio_args(data_dir: Path, anchor_date: str) -> list[str]:
    """The command line of one stdio run over p2p-demo (requirement 22.2).

    ``--anchor-date`` is the date the serve process reported for its own sandbox, which is
    how the two processes of requirement 24.1 come to work on one anchor date without any
    test reading a clock (module docstring).
    """
    return [
        str(COMMAND),
        "stdio",
        "--instance",
        str(INSTANCE_DIR),
        "--packs-dir",
        str(PACKS_DIR),
        "--data-dir",
        str(data_dir),
        "--anchor-date",
        anchor_date,
    ]


async def over_stdio(workdir: Path, recorded: Recorded, *, hash_seed: str | None = None) -> Run:
    """Replay the sequence in a stdio process of its own, below ``workdir``.

    The process is spawned by the official ``stdio_client`` and is gone after the block:
    it closes its stdin, which is the end of file of requirement 22.4, and waits for the
    process to end, so the ``diff.json`` and the ``trace.jsonl`` of its sandbox - the one
    sandbox a stdio process has (requirement 15.2) - are complete once the replay returns.
    Its stderr goes to a file below ``workdir``, and its environment is built from nothing
    (:func:`process_env`), with ``PYTHONHASHSEED`` added where the test names one.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    data_dir = workdir / "var"
    extra = {} if hash_seed is None else {HASH_SEED_VAR: hash_seed}
    args = stdio_args(data_dir, recorded.anchor_date)
    params = StdioServerParameters(
        command=args[0], args=args[1:], cwd=str(workdir), env=process_env(**extra)
    )
    with (workdir / "stderr.txt").open("w", encoding="utf-8") as errlog:
        with anyio.fail_after(SESSION_TIMEOUT_S):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, client_info=CLIENT_INFO) as client:
                    initialized = await client.initialize()
                    assert initialized.serverInfo.name == SERVER_NAME
                    caller = Caller(client=client)
                    answers = await caller.replay(recorded.steps)
    sandbox = one_sandbox(data_dir)
    return Run(
        sandbox_id=sandbox.name,
        answers=answers,
        diff=replaced((sandbox / DIFF_FILENAME).read_bytes(), sandbox.name),
        trace=events_of((sandbox / TRACE_FILENAME).read_text(encoding="utf-8")),
    )


async def over_new_http_sandbox(service: Service, recorded: Recorded) -> Run:
    """Replay the sequence in a sandbox the initialize itself creates (requirement 15.1).

    The sandbox requirement 25.4 means by "created over Streamable HTTP": a session on
    ``/mcp`` with no sandbox named, which makes a sandbox of the default profile and the
    default initial dataset at the anchor date the clock of the instance resolves then.
    The sandbox is the one the Admin API reports the session id of the transport for, and
    it turns frozen once the session ends - its diff and its trace are still read, which is
    what requirements 15.12 and 19.7 are about.

    The anchor date is the one thing this run cannot be told: ``serve`` resolves it per
    sandbox from the system clock in mode today (requirement 16.1), so a module that
    straddled midnight would get a different date here than the recording run did, and the
    replayed requests - whose dates are the recording's - would no longer be the sequence of
    requirement 24.1. The date is therefore asserted rather than assumed.
    """
    with anyio.fail_after(SESSION_TIMEOUT_S):
        async with streamable_http_client(service.mcp_url()) as (read, write, session_id):
            async with ClientSession(read, write, client_info=CLIENT_INFO) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == SERVER_NAME
                bound = session_id()
                assert bound is not None, "initialize 没有分配 Mcp-Session-Id"
                caller = Caller(client=client)
                answers = await caller.replay(recorded.steps)
    metadata = service.sandbox_of_session(bound)
    assert metadata[ANCHOR_DATE_KEY] == recorded.anchor_date, (
        f"会话自动创建的沙箱的锚点日期是 {metadata[ANCHOR_DATE_KEY]}，"
        f"记录那次是 {recorded.anchor_date}：本模块跨过了自然日，请重跑"
    )
    assert metadata["dataset"] == DATASET_REF, metadata
    return read_http_run(service, str(metadata["sandbox_id"]), answers)


def one_sandbox(data_dir: Path) -> Path:
    """The single sandbox directory of a stdio process, which serves one (req 15.2)."""
    root = data_dir / SANDBOXES_DIR
    assert root.is_dir(), f"{root} 不存在，说明进程没有创建沙箱"
    found = sorted(path for path in root.iterdir() if path.is_dir())
    assert len(found) == 1, f"期望恰好一个沙箱目录，实际 {found}"
    return found[0]


# --- the one run the module records ------------------------------------------------------


@pytest.fixture(scope="module")
def recorded(service: Service) -> Recorded:
    """The script, run once over Streamable HTTP, and everything it left behind.

    The baseline of the four tests that compare against a run of the serve process, and -
    nothing else of this module having executed a request in that process yet - the run
    without any other sandbox beside it that requirement 24.10 asks about. The sandbox is
    prebuilt through the Admin API, so its id is known before the session starts and its
    ``anchor_date`` is the date every other sandbox of the module is created at
    (requirement 19.3).
    """
    sandbox_id, anchor_date = service.prebuilt()
    steps, answers = anyio.run(over_http, service, sandbox_id, None, anchor_date)
    first = Recorded(
        steps=steps,
        run=read_http_run(service, sandbox_id, answers),
        anchor_date=anchor_date,
    )
    check_sequence(first)
    return first


def check_sequence(recorded: Recorded) -> None:
    """Assert the recorded sequence is one the comparisons can say something about.

    It writes (the diff of the run holds changes), it fails twice and it lists twice, and it
    holds fewer tools/call than the call rate of one session allows per second
    (requirement 23.12), so no call of it is ever refused for calling too fast - the
    condition requirement 24.1 puts on a Request_Sequence. A sequence that is extended past
    that bound has to raise ``ERP_MOCK_MAX_CALLS_PER_SECOND`` for every process of this
    file, or the comparisons would start comparing refusals.
    """
    run = recorded.run
    assert len(run.answers) == len(recorded.steps)
    calls = run.calls
    assert len(calls) < MAX_CALLS_PER_SECOND, (
        f"序列有 {len(calls)} 个 tools/call，已达到一个会话每秒 {MAX_CALLS_PER_SECOND} 次的上限"
    )
    assert len(run.answers) - len(calls) == EXPECTED_LISTS
    failed = [answer for answer in calls if answer.is_error]
    assert len(failed) == EXPECTED_FAILURES, [answer.code for answer in failed]
    assert run.changes, "序列没有留下任何净变化，diff 的比较没有意义"

    # The premises requirement 24.1 holds fixed, as the diff of the run writes them down:
    # one instance configuration version, one initial dataset version, one anchor date
    # (requirement 18.1). Every other sandbox of the module is created from these.
    payload = json.loads(run.diff)
    assert payload["instance"] == INSTANCE_REF, payload
    assert payload["dataset"] == DATASET_REF, payload
    assert payload[ANCHOR_DATE_KEY] == recorded.anchor_date, payload


# --- comparing two runs ------------------------------------------------------------------


def canonical(value: Any) -> str:
    """``value`` as one line of JSON, every key in the order it is in (requirement 13.4).

    Two values serialize to the same text exactly when they are equal *and* their keys are
    in the same order, at every level, which is what requirements 24.1 and 24.3 compare.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def events_of(text: str) -> tuple[dict[str, Any], ...]:
    """The trace events of ``text``, one per line (requirement 17.1)."""
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line:
            continue
        parsed = json.loads(line)
        assert isinstance(parsed, dict), f"trace 行不是 JSON 对象：{line}"
        events.append(parsed)
    return tuple(events)


def stable_event(event: Mapping[str, Any]) -> str:
    """One trace event without the four keys requirement 24.3 drops, as one line of JSON."""
    return canonical({key: value for key, value in event.items() if key not in VOLATILE_KEYS})


def replaced(diff: bytes, sandbox_id: str) -> bytes:
    """``diff`` with the value of ``sandbox_id`` replaced by one string (req 24.2).

    That the replacement happened at all is asserted here: a diff that named no sandbox
    would make the comparison of two of them pass for the wrong reason.
    """
    assert sandbox_id.encode() in diff, f"diff 中没有沙箱 {sandbox_id} 的 id：{_short(diff)}"
    return diff.replace(sandbox_id.encode(), SANDBOX_PLACEHOLDER.encode())


def without_causes(diff: bytes) -> str:
    """``diff`` with the ``cause_seqs`` of every change dropped (requirement 24.4)."""
    payload = json.loads(diff)
    payload[CHANGES] = [
        {key: value for key, value in change.items() if key != CAUSE_SEQS}
        for change in payload[CHANGES]
    ]
    return canonical(payload)


def cause_seqs(diff: bytes) -> tuple[int, ...]:
    """Every ``cause_seqs`` entry of ``diff``, in the order the changes hold them (18.3)."""
    payload = json.loads(diff)
    return tuple(int(seq) for change in payload[CHANGES] for seq in change[CAUSE_SEQS])


def reset_seq(events: Sequence[Mapping[str, Any]]) -> int:
    """The ``seq`` of the one ``sandbox_reset`` event of a trace (requirement 17.7)."""
    found = [int(item["seq"]) for item in events if item["event"] == EventName.SANDBOX_RESET]
    assert len(found) == 1, f"trace 中有 {len(found)} 个 sandbox_reset 事件，期望 1 个"
    return found[0]


def compare_run(label: str, first: Run, second: Run) -> None:
    """Assert two runs of the sequence agree on all three yardsticks (24.1 to 24.3)."""
    assert first.sandbox_id != second.sandbox_id, (
        f"{label}：两次执行用的是同一个沙箱 {first.sandbox_id}，比较没有意义"
    )
    compare_answers(label, first.answers, second.answers)
    compare_diffs(label, first.diff, second.diff)
    compare_traces(label, first.trace, second.trace)


def compare_answers(label: str, first: Answers, second: Answers) -> None:
    """Assert two runs were answered the same way, step by step (requirement 24.1).

    The ``isError``, the ``text`` byte for byte, the ``structuredContent`` with its keys in
    the same order, and the payload of a tools/list (requirement 24.9). No answer of either
    run may be a call the sandbox refused for a resource limit, which is the premise
    requirement 24.1 puts on the sequence.
    """
    assert len(first) == len(second), (
        f"{label}：两次执行的步骤数不同，{len(first)} 与 {len(second)}"
    )
    for answers in (first, second):
        refused = [(_step_text(a.step), a.refused_limit) for a in answers if a.refused_limit]
        assert not refused, f"{label}：有调用被资源上限拒绝，不是需求 24 第 1 条的序列：{refused}"
    for index, (one, other) in enumerate(zip(first, second, strict=True), start=1):
        assert type(one.step) is type(other.step), (
            f"{label}：第 {index} 步的请求种类不同：{_step_text(one.step)} 与"
            f" {_step_text(other.step)}"
        )
        assert one.step == other.step, (
            f"{label}：第 {index} 步发出的请求不同：{_step_text(one.step)} 与"
            f" {_step_text(other.step)}"
        )
        for key, left, right in zip(
            ("isError", "text", "structuredContent", "tools/list"),
            one.compared,
            other.compared,
            strict=True,
        ):
            assert left == right, _mismatch(label, index, one.step, key, left, right)


def compare_diffs(label: str, first: bytes, second: bytes) -> None:
    """Assert two diffs are the same bytes once the sandbox id is replaced (req 24.2)."""
    assert first == second, f"{label}：{_difference('diff', first, second)}"


def compare_traces(
    label: str, first: Sequence[Mapping[str, Any]], second: Sequence[Mapping[str, Any]]
) -> None:
    """Assert two traces hold the same events, volatile keys aside (requirement 24.3)."""
    assert len(first) == len(second), (
        f"{label}：两个沙箱的 trace 事件数不同，{len(first)} 与 {len(second)}；"
        f"两侧的事件名分别是 {[item['event'] for item in first]} 与"
        f" {[item['event'] for item in second]}"
    )
    for index, (one, other) in enumerate(zip(first, second, strict=True), start=1):
        assert stable_event(one) == stable_event(other), "\n".join(
            [
                f"{label}：第 {index} 个 trace 事件去掉易变键后不同：",
                f"  基准：{_short(stable_event(one))}",
                f"  本次：{_short(stable_event(other))}",
            ]
        )


def _mismatch(label: str, index: int, step: Step, key: str, one: object, other: object) -> str:
    """The failure message of one field of one step: where, what, and both values."""
    return "\n".join(
        [
            f"{label}：第 {index} 步（{_step_text(step)}）的 {key} 不同：",
            f"  基准：{_short(one)}",
            f"  本次：{_short(other)}",
        ]
    )


def _step_text(step: Step) -> str:
    """One step of the sequence as a report prints it."""
    if isinstance(step, ListStep):
        return "tools/list"
    return f"{step.tool} {step.args}"


def _short(value: object) -> str:
    """``value`` as text, cut short at :data:`REPORTED_CHARS` characters."""
    text = str(value)
    if len(text) <= REPORTED_CHARS:
        return text
    return f"{text[:REPORTED_CHARS]}…（共 {len(text)} 个字符）"


def _difference(name: str, first: bytes, second: bytes) -> str:
    """Where two versions of ``name`` differ, as a message about them."""
    offset = next(
        (
            index
            for index, (one, other) in enumerate(zip(first, second, strict=False))
            if one != other
        ),
        min(len(first), len(second)),
    )
    return (
        f"{name} 的两份内容不同：长度 {len(first)} 与 {len(second)}，"
        f"第一个不同的字节在 {offset}；两侧各自的上下文："
        f"\n  {_short(first[max(offset - 40, 0) : offset + 80])}"
        f"\n  {_short(second[max(offset - 40, 0) : offset + 80])}"
    )


# --- two transports, two processes, two data directories (requirements 24.1 to 24.3) -----


@pytest.mark.anyio
@pytest.mark.req("24.1", "24.2", "24.3", "25.4")
async def test_the_same_sequence_over_two_transports_in_two_processes_agrees(
    tmp_path: Path, service: Service, recorded: Recorded
) -> None:
    """The sequence over Streamable HTTP and over stdio, in two processes of their own.

    The two sandboxes of requirement 24.1 at their furthest apart, which is the case
    requirement 25.4 asks for by name: one created by an initialize on ``/mcp`` of the serve
    process (:func:`over_new_http_sandbox`), one by the first initialize of a stdio process
    below a ``--data-dir`` of its own - two processes, two data directories, two Snapshot
    caches, and the same instance configuration, profile, initial dataset and anchor date,
    which is what the requirement holds fixed. The results, the diff and the trace all have
    to agree, the diff read once from the Admin API and once from the ``diff.json`` the
    stdio process wrote as it exited: the same bytes either way (requirement 24.2).

    The prebuilt sandbox of the recording run is compared with the stdio run as well, so the
    two ways a sandbox of a serve process comes about - prebuilt through the Admin API and
    created by an initialize - are both in it (requirements 15.1 and 19.3).
    """
    created = await over_new_http_sandbox(service, recorded)
    run = await over_stdio(tmp_path, recorded)
    compare_run("经 /mcp 自动创建的沙箱与 stdio 进程", created, run)
    compare_run("经 Admin API 预建的沙箱与 stdio 进程", recorded.run, run)

    # The premise of the comparison, which the processes reached differently: the stdio
    # process was told the date, the serve process read it off its own clock (req 16.1).
    assert json.loads(run.diff)[ANCHOR_DATE_KEY] == recorded.anchor_date


# --- two hash seeds (requirement 24.9) ---------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("24.9")
async def test_the_same_sequence_under_two_hash_seeds_agrees(
    tmp_path: Path, recorded: Recorded
) -> None:
    """``PYTHONHASHSEED`` 0 and 12345: the same tools/list, results, diff and trace.

    Two stdio processes, each below a ``--data-dir`` of its own and at the anchor date of
    the recording run, so the only thing that differs between them is the seed the
    interpreter hashes strings with. Both tools/list of the sequence are compared with their
    whole payload, which is where an iteration order that leaked into a result would show
    (requirement 11.10), and the diff and the trace are compared as everywhere else.
    """
    first_seed, second_seed = HASH_SEEDS
    first = await over_stdio(tmp_path / f"seed-{first_seed}", recorded, hash_seed=first_seed)
    second = await over_stdio(tmp_path / f"seed-{second_seed}", recorded, hash_seed=second_seed)
    compare_run(f"PYTHONHASHSEED 为 {first_seed} 与 {second_seed} 的两个进程", first, second)

    # Both runs listed the tools of the profile, and that payload is part of what was
    # compared rather than something this test asserts the shape of (requirement 24.9).
    listed = [answer.listed for answer in first.answers if answer.listed is not None]
    assert len(listed) == EXPECTED_LISTS, listed


# --- other sandboxes executing at the same time (requirement 24.10) ----------------------


async def keep_busy(
    service: Service,
    sandbox_id: str,
    steps: Sequence[Step],
    ready: anyio.Event,
    stop: anyio.Event,
) -> None:
    """Send the sequence in ``sandbox_id`` over and over until ``stop`` is set.

    One neighbour of the test of requirement 24.10: a session of its own on a sandbox of its
    own, sending requests into the same service process as the run under test, so the two
    sandboxes are being worked on at the same time (requirement 15.4). ``ready`` is set
    after its first request, which is how the test knows the neighbour is really running
    before it starts the run it compares.

    Nothing of what a neighbour is answered is compared, so a repeat that fails - a create
    whose ids are taken, a call the sandbox refuses for a resource limit - is of no concern
    here: the request still queued on that sandbox, wrote its trace event and competed for
    the same thread pool, which is all this neighbour is for.
    """
    async with http_session(service, sandbox_id, record=False) as caller:
        while not stop.is_set():
            for step in steps:
                if stop.is_set():
                    return
                await caller.send(step)
                ready.set()


@pytest.mark.anyio
@pytest.mark.req("24.10")
async def test_other_sandboxes_running_at_the_same_time_change_nothing(
    service: Service, recorded: Recorded
) -> None:
    """The sequence with neighbours at work, against the run that had the process to itself.

    The recording run is the baseline requirement 24.10 asks for: it was the first thing
    this module did with the serve process, so nothing else was executing a request while it
    ran. Here the same sequence runs in a prebuilt sandbox of the same process while
    :data:`NEIGHBOURS` other sandboxes keep sending the same requests, and the results, the
    diff and the trace all have to be what they were - the sandboxes of one process share
    its thread pool and its tools layer, and nothing else (requirement 15.13).
    """
    busy, anchor_date = service.prebuilt(recorded.anchor_date)
    assert anchor_date == recorded.anchor_date
    neighbours = [service.prebuilt(recorded.anchor_date)[0] for _ in range(NEIGHBOURS)]
    stop = anyio.Event()
    ready = [anyio.Event() for _ in neighbours]

    async with anyio.create_task_group() as tasks:
        for sandbox_id, started in zip(neighbours, ready, strict=True):
            tasks.start_soon(keep_busy, service, sandbox_id, recorded.steps, started, stop)
        try:
            # Every neighbour has answered at least one request before the run starts, so
            # the comparison is really about a sandbox that had company.
            with anyio.fail_after(SESSION_TIMEOUT_S):
                for started in ready:
                    await started.wait()
            _, answers = await over_http(service, busy, recorded.steps, recorded.anchor_date)
        finally:
            stop.set()

    compare_run("与其他沙箱并发执行", recorded.run, read_http_run(service, busy, answers))

    # And the neighbours really did work while that ran: each of them has a trace of its
    # own with calls in it, which is what their requests left behind (requirement 15.13).
    for sandbox_id in neighbours:
        events = service.trace(sandbox_id)
        performed = [item for item in events if item["event"] == EventName.TOOLS_CALL]
        assert performed, f"并发沙箱 {sandbox_id} 没有执行任何调用：{[i['event'] for i in events]}"


# --- a reset and a replay (requirement 24.4) ---------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("24.4")
async def test_the_replay_after_a_reset_agrees_with_the_first_run(
    service: Service, recorded: Recorded
) -> None:
    """The recorded sandbox, reset through the Admin API and replaying its own sequence.

    The reset puts the database back to the Snapshot of the initial dataset, counters
    included, and leaves the anchor date, the trace and the Named_Snapshots alone
    (requirement 19.4). The replay therefore has to be answered exactly as the first run
    was, and its diff has to equal the diff taken before the reset in everything but
    ``cause_seqs``, which now names the calls of the replay alone - every one of them
    written after the ``sandbox_reset`` event (requirements 18.3 and 18.10).
    """
    sandbox_id = recorded.sandbox_id
    service.reset(sandbox_id)
    _, answers = await over_http(service, sandbox_id, recorded.steps, recorded.anchor_date)
    compare_answers("重置后重放", recorded.run.answers, answers)

    run = read_http_run(service, sandbox_id, answers)
    assert without_causes(run.diff) == without_causes(recorded.run.diff), (
        "重置后重放的 diff 除 cause_seqs 外与重置前不同：\n"
        f"  重置前：{_short(without_causes(recorded.run.diff))}\n"
        f"  重放后：{_short(without_causes(run.diff))}"
    )
    after = reset_seq(run.trace)
    replayed = cause_seqs(run.diff)
    assert replayed, f"重放后的 diff 没有 cause_seqs：{_short(run.diff)}"
    assert all(seq > after for seq in replayed), (
        f"重放后的 cause_seqs {replayed} 中有不晚于 sandbox_reset（seq {after}）的调用序号"
    )


# --- a Named_Snapshot, a restore and a replay (requirement 24.11) ------------------------


@pytest.mark.anyio
@pytest.mark.req("24.11")
async def test_the_replay_after_restoring_a_named_snapshot_agrees(
    service: Service, recorded: Recorded
) -> None:
    """A Named_Snapshot in the middle of the sequence, and the tail replayed twice.

    The sequence is split before the push (:func:`split_at`): a prebuilt sandbox runs the
    head, the Admin API saves the Named_Snapshot of the state that left (requirement 19.5),
    the tail runs for the first time, the sandbox is restored to that snapshot and the tail
    runs again. Requirement 24.11 is that the second time is answered as the first was,
    which it only can be if the restore put the records, the counters and the effective
    calls back as they were (requirement 19.6) - the numbers of the receipt and its lines
    would differ otherwise.
    """
    sandbox_id, _ = service.prebuilt(recorded.anchor_date)
    split = split_at(recorded.steps)
    head, tail = recorded.steps[:split], recorded.steps[split:]
    assert head and tail, f"序列在第 {split} 步切分后有一侧为空"

    await over_http(service, sandbox_id, head, recorded.anchor_date)
    service.save(sandbox_id, NAMED)
    _, first = await over_http(service, sandbox_id, tail, recorded.anchor_date)
    service.restore(sandbox_id, NAMED)
    _, second = await over_http(service, sandbox_id, tail, recorded.anchor_date)

    compare_answers(f"恢复 {NAMED} 后重放第 {split + 1} 步起", first, second)
    assert not any(answer.is_error for answer in first), (
        f"快照之后的那段序列本应全部成功：{[answer.code for answer in first if answer.is_error]}"
    )
