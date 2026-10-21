"""The procure-to-receipt flow of requirement 25.5, over both transports, on p2p-demo.

The other integration tests of this directory serve the engine test fixture, which loads in
milliseconds and holds the few records one call needs. This one serves
``instances/p2p-demo`` as it ships - the three example business packs, the semantic profile
and the ``purchase-basic`` dataset - and runs the twelve calls requirement 25.5 asks for
with the official MCP Python SDK client, once over the ``/mcp`` endpoint of a serve
subprocess and once over the stdin and stdout of a stdio subprocess (requirement 14.1)::

    business_context_get  supplier_search  material_search  warehouse_search
    purchase_order_create  purchase_order_submit  purchase_order_approve
    purchase_receipt_create_from_order  purchase_receipt_submit
    purchase_receipt_approve  purchase_order_get  inventory_balance_query

:func:`run_flow` is that sequence and nothing else, and what it leaves behind is checked
twice over, once in the results the calls returned and once in the diff of the sandbox
(:func:`check_flow`, :func:`check_diff`): every call answered with ``isError`` false, every
line of the order has ``linked_qty`` equal to its ``qty_base``, the order is ``closed``, and
the stock of each (material, warehouse) the receipt fed is the initial dataset's plus the
``qty_base`` of the receipt lines on it. The two bills got there through the states the four
submit and approve calls answered with, and the closing is the writeback's doing rather than
an operation the flow asked for (requirement 8.13). The order is created for every enabled
material of the dataset, one of which is purchased in a unit that is not its base unit
(requirement 21.8), which the results show themselves: that line's ``qty_base`` is not its
``qty``, so the engine folded it through a conversion, where the other line's two values are
equal.

Nothing is written down that the service can say itself. The internal ids of the supplier,
the materials and the warehouse come from the three searches, as a client's would
(requirement 11.7); the order and receipt ids from what created them; the source line ids
from the order's own lines; and the quantity the receipt asks for is the text the order line
came back as, which requirement 12.5 lets a client send back as it got it. What is written
down is the business configuration: the keyword the dataset's records carry, the warehouse
code of core's shared basic data and the stock that warehouse starts with
(:data:`DEMO_KEYWORD`, :data:`WAREHOUSE_CODE`, :data:`INITIAL_STOCK`), the records of which
tests/packs/test_p2p_demo.py checks against the Snapshot itself.

The anchor date, which the two transports reach differently (requirement 16.1):

===========  =======================================================================
``serve``    has no ``--anchor-date``, and the clock of p2p-demo is in mode today, so
             the anchor date is the day the process runs. No test reads the system
             clock (requirement 25.10), so every date of the HTTP test comes from the
             service: the ``business_date`` of the first call, the ``anchor_date`` of
             the Admin metadata and the ``anchor_date`` of the diff, which have to
             agree with each other, and the delivery date of the order is the
             business date the service reported.
``stdio``    has ``--anchor-date``, so the test fixes it (:data:`STDIO_ANCHOR_DATE`)
             and the service's three dates have to agree with that one as well.
===========  =======================================================================

Where the diff comes from, also differently (requirement 25.5): over HTTP from
``GET /admin/sandboxes/{id}/diff`` of the Admin port, the sandbox being the one whose
``session_ids`` hold the session id the SDK's transport reports; over stdio from the
``diff.json`` the process writes into its sandbox directory as it exits
(requirement 22.4). Both are read after the session has ended - an auto-created sandbox
turns frozen then, and the Admin API still reads the diff of a frozen sandbox
(requirements 15.7 and 15.12) - and the two are checked by the same function.

The processes, as in tests/integration/test_cli_serve.py and
tests/integration/test_cli_stdio.py: the console script of ``[project.scripts]`` beside the
interpreter running the tests (:data:`COMMAND`), with a data directory below ``tmp_path`` -
named or, in the test of a command that configures nothing, the default ``./var`` of the
working directory, which is a ``tmp_path`` as well - so each one builds the Snapshot of
``purchase-basic`` in a cache of its own and writes its sandboxes where pytest removes
them. p2p-demo takes a few hundred milliseconds to load and
to build, on top of starting an interpreter, so the serve process is started once for the
module and both tests that speak HTTP share it; a stdio process belongs to one session by
definition and is started per test. Every wait is a poll against a deadline
(:func:`wait_for`), and a poll that finds a process gone fails the test with what it wrote.

The requests of the flow are sent with :meth:`ClientSession.send_request` rather than with
``call_tool``, which issues a tools/list of its own to validate ``structuredContent`` on the
client side: the twelve calls of requirement 25.5 are then the twelve requests the session
makes and no more. The two tests of requirement 14.9 are the other way round - they are
about a client that was configured with nothing but the address or the command, so they take
the plain path of the SDK, ``call_tool`` and ``list_tools``, and call the tool before the
listing to show that neither has to come first.
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
from datetime import date
from decimal import Decimal
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
from erp_mock.domain.inventory import balance_id, txn_id
from erp_mock.gateway.handlers import SERVER_NAME
from erp_mock.gateway.sandbox import SANDBOXES_DIR
from erp_mock.gateway.serve import ADMIN_HOST, bind_socket
from erp_mock.gateway.stdio import DIFF_FILENAME
from erp_mock.state.diff import INITIAL_BASE
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET

# The example instance and the example business packs of the repository, which both
# transports are served from (requirement 25.5).
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INSTANCE_DIR: Final = REPO_ROOT / "instances" / "p2p-demo"
PACKS_DIR: Final = REPO_ROOT / "packs"

# What the instance and its one initial dataset are called where the service writes each as
# ``<名称>@<版本>`` (requirements 18.1 and 19.12).
INSTANCE_REF: Final = "p2p-demo@1.0.0"
DATASET_REF: Final = "purchase-basic@1.0.0"

# The installed command, beside the interpreter that runs the tests (module docstring).
COMMAND: Final = Path(sys.executable).parent / "erp-mock-mcp"

# The variables a serve subprocess is given beyond what a test sets: enough to run, and not
# one of the ``ERP_MOCK_*`` the environment of the test process may carry
# (requirement 23.9). A stdio subprocess gets the SDK's own ``get_default_environment()``,
# which passes on a list of the same kind.
PASSED_THROUGH: Final = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")

# The deadlines. Starting a service loads p2p-demo and builds the Snapshot of
# ``purchase-basic``, which together take a few hundred milliseconds, and a session is
# twelve calls; a test that reaches one of these has already failed.
START_TIMEOUT_S: Final = 30.0
SESSION_TIMEOUT_S: Final = 45.0
STOP_TIMEOUT_S: Final = 15.0
ADMIN_TIMEOUT_S: Final = 10.0
POLL_SECONDS: Final = 0.05

# The loopback address both ports of the serve process of this module are on: the only
# address the Admin port is ever on (requirement 19.1), and a ``--host`` that needs no MCP
# endpoint token, which is what requirement 14.9 is about.
LOOPBACK: Final = ADMIN_HOST

# The addresses of the service this module uses (requirements 14.2, 19.8, 19.9 and 19.11).
MCP_PATH: Final = "/mcp"
HEALTH_PATH: Final = "/admin/health"
SANDBOXES_PATH: Final = "/admin/sandboxes"

# What the client calls itself in the handshake of the two flow tests; the two tests of
# requirement 14.9 configure nothing at all and let the SDK name itself.
CLIENT_INFO: Final = types.Implementation(name="protocol-flow-test", version="0.0.0")

# The anchor date of the stdio runs that ask for one (module docstring). Any date of
# 2000-01-01 to 2099-12-31 would do - that is what requirement 21.12 is about - so this one
# is picked for being nothing special, as in tests/packs/test_p2p_demo.py: a Friday in the
# middle of a 31 day month of a non-leap year.
STDIO_ANCHOR_DATE: Final = date(2026, 10, 9)


# --- what the twelve calls are, and what the dataset they run on holds -------------------

# The eleven tool names of requirement 25.5 beyond the built-in context tool, as p2p-demo's
# packs generate them (requirement 21.4).
SUPPLIER_SEARCH: Final = "supplier_search"
MATERIAL_SEARCH: Final = "material_search"
WAREHOUSE_SEARCH: Final = "warehouse_search"
ORDER_CREATE: Final = "purchase_order_create"
ORDER_SUBMIT: Final = "purchase_order_submit"
ORDER_APPROVE: Final = "purchase_order_approve"
RECEIPT_CREATE: Final = "purchase_receipt_create_from_order"
RECEIPT_SUBMIT: Final = "purchase_receipt_submit"
RECEIPT_APPROVE: Final = "purchase_receipt_approve"
ORDER_GET: Final = "purchase_order_get"
BALANCE_QUERY: Final = "inventory_balance_query"

# The twelve calls of requirement 25.5, in the order the requirement writes them;
# :attr:`Session.called` has to hold exactly this afterwards.
FLOW_TOOLS: Final[tuple[str, ...]] = (
    BUSINESS_CONTEXT_GET,
    SUPPLIER_SEARCH,
    MATERIAL_SEARCH,
    WAREHOUSE_SEARCH,
    ORDER_CREATE,
    ORDER_SUBMIT,
    ORDER_APPROVE,
    RECEIPT_CREATE,
    RECEIPT_SUBMIT,
    RECEIPT_APPROVE,
    ORDER_GET,
    BALANCE_QUERY,
)

# The facts of the business configuration this file depends on. The dataset and core's
# shared basic data write each of them down, and tests/packs/test_p2p_demo.py checks the
# records they produce against the Snapshot itself:
#
# - every record purchase-basic writes carries 演示 in its name and the records of ``bulk``
#   do not, so this keyword selects the dataset's own suppliers and materials
#   (instances/p2p-demo/datasets/purchase-basic.yaml);
# - the enabled records of the two searches are what a client may write into a new bill:
#   the disabled supplier and the disabled material of the dataset would break R-GEN-006
#   (requirement 5.7), and the filter keeps them out of the result a line is picked from;
# - WH01 is the first warehouse of core's shared basic data
#   (packs/core/seed/shared/05-warehouse.yaml), and the dataset's historical receipt left
#   144 base units of one material in it and nothing of the other, which is the stock the
#   flow adds to (requirement 21.7).
DEMO_KEYWORD: Final = "演示"
ENABLED: Final = "enabled"
WAREHOUSE_CODE: Final = "WH01"
INITIAL_STOCK: Final = Decimal(144)

# What the order of the flow asks for, per line: the same quantity and the same unit price
# for every material, so that the one line whose ``qty_base`` is not its ``qty`` is the
# material whose purchase unit is not its base unit and not a line that was written
# differently (requirement 25.5). Both are JSON numbers, as the input schema declares them
# (requirement 11.6); a number of at most 15 significant digits reaches the engine as the
# decimal it was written as (requirement 12.4).
ORDER_QTY: Final = 3
ORDER_PRICE: Final = 10.08

# The three money fields of a bill header, which the tool result and the diff have to agree
# on (requirements 13.4 and 18.5).
MONEY_KEYS: Final = ("amount", "tax_amount", "amount_with_tax")

# The tables the flow writes, as the diff names them (requirement 18.4).
ORDER_ENTITY: Final = "purchase_order"
ORDER_LINE_ENTITY: Final = "purchase_order_line"
RECEIPT_ENTITY: Final = "purchase_receipt"
RECEIPT_LINE_ENTITY: Final = "purchase_receipt_line"
BALANCE_ENTITY: Final = "stock_balance"
TXN_ENTITY: Final = "stock_txn"

# The states a bill of the flow goes through (requirement 7.1): both bills are created as
# drafts and submitted and approved, and the order is then closed by the writeback of the
# receipt (requirement 8.13), which no call of the flow asks for.
DRAFT: Final = "draft"
SUBMITTED: Final = "submitted"
APPROVED: Final = "approved"
CLOSED: Final = "closed"

# The direction of the stock transactions a receipt writes (requirement 18.8).
IN: Final = "in"


@pytest.fixture(scope="module", autouse=True)
def installed_command() -> None:
    """Every test here runs the console script; a run without it would be meaningless."""
    assert COMMAND.is_file(), f"没有找到安装好的命令：{COMMAND}"
    assert os.access(COMMAND, os.X_OK)


@pytest.fixture
def anyio_backend() -> str:
    """The one event loop these tests run on, as every async test of the suite does."""
    return "asyncio"


def process_env(**extra: str) -> dict[str, str]:
    """The environment of one serve process: those of :data:`PASSED_THROUGH` and ``extra``."""
    inherited = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    return {**inherited, **extra}


# --- one session of the official client --------------------------------------------------


@dataclass(slots=True)
class Session:
    """A live session of the official SDK client, and the calls it has made.

    :meth:`call` sends one tools/call, asserts the result is no error and hands back its
    ``structuredContent``; ``called`` collects the tool names in the order they went out, so
    a test asserts the sequence of requirement 25.5 as a whole rather than call by call.
    """

    client: ClientSession
    called: list[str] = field(default_factory=list)

    async def call(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Send ``tool`` with ``args`` and return the structured result (requirement 13.1).

        ``ClientSession.call_tool`` is avoided on purpose (module docstring): it issues a
        tools/list of its own, which would put a thirteenth request into the twelve of
        requirement 25.5.
        """
        request = types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(name=tool, arguments=dict(args))
            )
        )
        result = await self.client.send_request(request, types.CallToolResult)
        assert result.isError is False, f"{tool} 的调用结果 isError 为 true：{result_text(result)}"
        assert result.structuredContent is not None, f"{tool} 的成功结果没有 structuredContent"
        self.called.append(tool)
        return dict(result.structuredContent)


def result_text(result: types.CallToolResult) -> str:
    """The text content of ``result``, which is where an error object is (req 13.6)."""
    return "".join(item.text for item in result.content if isinstance(item, types.TextContent))


# --- the serve process and its two ports (requirements 19.1 and 22.1) --------------------


@dataclass(slots=True)
class Service:
    """The serve process of this module, and what a test asks of it."""

    process: subprocess.Popen[bytes]
    data_dir: Path
    port: int
    admin_port: int
    out: Path
    err: Path
    token: str = ""

    @property
    def stderr(self) -> str:
        """What the process has written to stderr: its log (design.md 7.3)."""
        return self.err.read_text(encoding="utf-8")

    @property
    def token_file(self) -> Path:
        """``<data-dir>/admin-token``, which serve writes before it listens (req 23.5)."""
        return self.data_dir / TOKEN_FILENAME

    @property
    def mcp_url(self) -> str:
        """The address of the MCP endpoint a client is given (requirement 14.2)."""
        return f"http://{LOOPBACK}:{self.port}{MCP_PATH}"

    def admin_get(self, path: str) -> httpx.Response | None:
        """``GET path`` on the Admin port, None where it did not answer yet (req 19.2)."""
        url = f"http://{LOOPBACK}:{self.admin_port}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            return httpx.get(url, headers=headers, timeout=ADMIN_TIMEOUT_S)
        except httpx.TransportError:
            return None

    def admin_json(self, path: str) -> dict[str, Any]:
        """``GET path`` on the Admin port, which has to answer 200 and one JSON object."""
        response = self.admin_get(path)
        assert response is not None, f"Admin 端口没有回应 {path}：\n{self.stderr}"
        assert response.status_code == httpx.codes.OK, response.text
        payload = response.json()
        assert isinstance(payload, dict), payload
        return payload

    def serving(self) -> bool:
        """Whether the Admin port answers the token of the process with 200 (req 19.9)."""
        response = self.admin_get(HEALTH_PATH)
        return response is not None and response.status_code == httpx.codes.OK

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
            pytest.fail(f"serve 进程在{what}之前以 {code} 退出：\n{service.stderr}")
        time.sleep(POLL_SECONDS)
    pytest.fail(f"等待{what}超时（{START_TIMEOUT_S} 秒）：\n{service.stderr}")


@contextmanager
def serve_process(workdir: Path) -> Iterator[Service]:
    """Start one serve process over p2p-demo on two free ports and yield it, serving.

    ``--host`` is the loopback address and no MCP endpoint token is configured, which is the
    service requirement 14.9 speaks of; the Admin token is the one serve generated and wrote
    to its token file (requirement 23.5), which is how this file reaches the Admin port.
    stdout and stderr go to files below ``workdir`` rather than to pipes, so a test reads
    them while the process runs. Whatever is still running when the block ends is stopped,
    so no test leaves a service behind.
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
    service = Service(
        process=process,
        data_dir=data_dir,
        port=port,
        admin_port=admin_port,
        out=out,
        err=err,
    )
    try:
        wait_for(service, service.token_file.is_file, "写出 Admin 令牌文件")
        service.token = service.token_file.read_text(encoding="utf-8")
        wait_for(service, service.serving, "两个端口开始监听")
        yield service
    finally:
        service.terminate()


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    """One serve process for the whole module (module docstring).

    Loading p2p-demo and building the Snapshot of ``purchase-basic`` happens once, and the
    two tests that speak Streamable HTTP each create a sandbox of their own on ``/mcp``.
    """
    with serve_process(tmp_path_factory.mktemp("serve")) as started:
        yield started


@asynccontextmanager
async def http_session(service: Service) -> AsyncIterator[tuple[Session, str]]:
    """One session on ``/mcp`` of ``service``, with the session id the transport reports.

    The transport is given the address and nothing else; the client names itself in the
    handshake, which is what a client does and what the two tests of requirement 14.9
    deliberately leave out as well. Leaving the block terminates the session (the
    transport's DELETE), after which the sandbox that was created for it turns frozen and
    its diff is still readable (requirements 15.7 and 15.12).
    """
    with anyio.fail_after(SESSION_TIMEOUT_S):
        async with streamable_http_client(service.mcp_url) as (read, write, session_id):
            async with ClientSession(read, write, client_info=CLIENT_INFO) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == SERVER_NAME
                bound = session_id()
                assert bound is not None, "initialize 没有分配 Mcp-Session-Id"
                yield Session(client=client), bound


def sandbox_of_session(service: Service, session_id: str) -> dict[str, Any]:
    """The metadata of the sandbox the session ``session_id`` is bound to (req 19.11).

    The listing of the Admin port holds every sandbox of the process, each with the sessions
    that were ever bound to it (requirement 19.12), so the sandbox of one session is found
    without the test having to know which sandboxes the other tests of the module left.
    """
    listed = service.admin_json(SANDBOXES_PATH)
    found = [item for item in listed["items"] if session_id in item["session_ids"]]
    assert len(found) == 1, f"会话 {session_id} 没有对应唯一的沙箱：{listed}"
    return found[0]


# --- the stdio process (requirements 22.2 and 22.4) --------------------------------------


def stdio_args(data_dir: Path | None = None, anchor_date: date | None = None) -> list[str]:
    """The command line of one stdio run over p2p-demo (requirement 22.2).

    ``data_dir`` of None leaves ``--data-dir`` at its default, which is ``./var`` of the
    working directory of the process (requirement 22.1); ``anchor_date`` of None leaves the
    anchor date to the clock of the instance, which is in mode today (requirement 16.1).
    """
    args = [
        str(COMMAND),
        "stdio",
        "--instance",
        str(INSTANCE_DIR),
        "--packs-dir",
        str(PACKS_DIR),
    ]
    if data_dir is not None:
        args += ["--data-dir", str(data_dir)]
    if anchor_date is not None:
        args += ["--anchor-date", anchor_date.isoformat()]
    return args


@asynccontextmanager
async def stdio_session(
    workdir: Path, args: Sequence[str], *, client_info: types.Implementation | None = None
) -> AsyncIterator[Session]:
    """Spawn ``args`` through the official ``stdio_client`` and hand over a live session.

    The process is gone after the block: ``stdio_client`` closes its stdin, which is the end
    of file of requirement 22.4, and waits for it to end, so the ``diff.json`` of its
    sandbox is there once the block has left. Its stderr goes to a file below ``workdir``
    rather than to the stderr of the test, and its environment is the SDK's own
    ``get_default_environment()``, which carries no ``ERP_MOCK_*`` (requirement 23.9).
    """
    params = StdioServerParameters(command=args[0], args=list(args[1:]), cwd=str(workdir))
    with (workdir / "stderr.txt").open("w", encoding="utf-8") as errlog:
        with anyio.fail_after(SESSION_TIMEOUT_S):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, client_info=client_info) as client:
                    initialized = await client.initialize()
                    assert initialized.serverInfo.name == SERVER_NAME
                    yield Session(client=client)


def one_sandbox(data_dir: Path) -> Path:
    """The single sandbox directory of a stdio process, which serves one (req 15.2)."""
    root = data_dir / SANDBOXES_DIR
    assert root.is_dir(), f"{root} 不存在，说明进程没有创建沙箱"
    found = sorted(path for path in root.iterdir() if path.is_dir())
    assert len(found) == 1, f"期望恰好一个沙箱目录，实际 {found}"
    return found[0]


# --- the twelve calls (requirement 25.5) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Flow:
    """What the twelve calls of one session returned.

    ``order`` is the order as ``purchase_order_create`` answered, ``order_closed`` the same
    order as ``purchase_order_get`` answered after the receipt was approved, and ``receipt``
    the receipt the push generated; ``balances`` is the inventory balance query of the
    warehouse the receipt fed. ``statuses`` is the status each of the four state machine
    calls answered with, by tool name (requirement 7.1). ``business_date`` is the date the
    service reported for itself, which is the anchor date of the sandbox
    (requirement 16.10).
    """

    business_date: str
    warehouse_id: str
    order: Mapping[str, Any]
    receipt: Mapping[str, Any]
    order_closed: Mapping[str, Any]
    balances: Mapping[str, Any]
    statuses: Mapping[str, str]

    @property
    def order_id(self) -> str:
        """The internal id of the order the flow created."""
        return str(self.order["id"])

    @property
    def receipt_id(self) -> str:
        """The internal id of the receipt the push generated."""
        return str(self.receipt["id"])

    @property
    def order_lines(self) -> list[Mapping[str, Any]]:
        """The lines of the closed order, in ``line_no`` order (requirement 5.12)."""
        return [dict(line) for line in self.order_closed["lines"]]

    @property
    def receipt_lines(self) -> list[Mapping[str, Any]]:
        """The lines of the receipt, in ``line_no`` order."""
        return [dict(line) for line in self.receipt["lines"]]

    @property
    def received(self) -> dict[tuple[str, str], Decimal]:
        """The base units the receipt took in, per (material, warehouse) (req 9.2)."""
        totals: dict[tuple[str, str], Decimal] = {}
        for line in self.receipt_lines:
            place = (str(line["material"]["id"]), str(line["warehouse"]["id"]))
            totals[place] = totals.get(place, Decimal(0)) + Decimal(str(line["qty_base"]))
        return totals

    @property
    def on_hand(self) -> dict[tuple[str, str], Decimal]:
        """The stock the balance query reported, per (material, warehouse) (req 9.12)."""
        return {
            (str(item["material"]["id"]), str(item["warehouse"]["id"])): Decimal(
                str(item["qty_base"])
            )
            for item in self.balances["items"]
        }


async def run_flow(session: Session) -> Flow:
    """Run the twelve calls of requirement 25.5 in one session and return what they said.

    Each call asserts ``isError`` is false (:meth:`Session.call`), and every id it needs
    comes from a call before it: the supplier, the materials and the warehouse from the
    three searches, the order from its creation, the source lines from the order's own lines
    and the receipt from the push. The order is created for every enabled material of the
    dataset and the receipt takes every line of it in full, so the writeback closes the order
    (requirement 8.13).
    """
    context = await session.call(BUSINESS_CONTEXT_GET, {})
    business_date = str(context["business_date"])

    suppliers = await session.call(SUPPLIER_SEARCH, {"keyword": DEMO_KEYWORD, "status": ENABLED})
    materials = await session.call(MATERIAL_SEARCH, {"keyword": DEMO_KEYWORD, "status": ENABLED})
    warehouses = await session.call(WAREHOUSE_SEARCH, {"status": ENABLED})

    # What a model would pick out of the three results: the first supplier, every material
    # of the dataset and the warehouse of code WH01 (module docstring).
    supplier_id = str(enabled_items(suppliers, SUPPLIER_SEARCH)[0]["id"])
    material_ids = [str(item["id"]) for item in enabled_items(materials, MATERIAL_SEARCH)]
    warehouse_id = str(by_code(enabled_items(warehouses, WAREHOUSE_SEARCH), WAREHOUSE_CODE)["id"])

    # The delivery date is the business date the service reported, which R-PUR-001 allows
    # (requirement 21.6) and which no test had to compute itself (requirement 25.10).
    order = await session.call(
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
    order_id = str(order["id"])
    statuses = {
        ORDER_SUBMIT: str((await session.call(ORDER_SUBMIT, {"id": order_id}))["status"]),
        ORDER_APPROVE: str((await session.call(ORDER_APPROVE, {"id": order_id}))["status"]),
    }

    # Every line of the order, in full and into the one warehouse: the quantity is the text
    # the order line came back as, which requirement 12.5 lets a client send back as a
    # decimal string, and the unit of it is the order line's (requirement 8.1).
    receipt = await session.call(
        RECEIPT_CREATE,
        {
            "order_id": order_id,
            "lines": [
                {
                    "source_line_id": line["id"],
                    "qty": line["qty"],
                    "warehouse_id": warehouse_id,
                }
                for line in order["lines"]
            ],
        },
    )
    receipt_id = str(receipt["id"])
    statuses[RECEIPT_SUBMIT] = str(
        (await session.call(RECEIPT_SUBMIT, {"id": receipt_id}))["status"]
    )
    statuses[RECEIPT_APPROVE] = str(
        (await session.call(RECEIPT_APPROVE, {"id": receipt_id}))["status"]
    )

    order_closed = await session.call(ORDER_GET, {"id": order_id})
    balances = await session.call(BALANCE_QUERY, {"warehouse_id": warehouse_id})

    assert session.called == list(FLOW_TOOLS), session.called
    return Flow(
        business_date=business_date,
        warehouse_id=warehouse_id,
        order=order,
        receipt=receipt,
        order_closed=order_closed,
        balances=balances,
        statuses=statuses,
    )


def enabled_items(result: Mapping[str, Any], tool: str) -> list[Mapping[str, Any]]:
    """The items of a search that filtered by status, every one of them enabled.

    A search that returned nothing would make the rest of the flow meaningless, and a
    disabled record among the items would be one a create refuses (R-GEN-006,
    requirement 5.7): the dataset holds a disabled supplier and a disabled material, and
    the ``status`` of the request is what keeps them out of what a line is picked from.
    """
    items: list[Mapping[str, Any]] = [dict(item) for item in result["items"]]
    assert items, f"{tool} 没有返回任何记录：{result}"
    assert {str(item["status"]) for item in items} == {ENABLED}, items
    return items


def by_code(items: Sequence[Mapping[str, Any]], code: str) -> Mapping[str, Any]:
    """The one item of ``items`` whose ``code`` is ``code`` (requirement 13.3)."""
    found = [item for item in items if item["code"] == code]
    assert len(found) == 1, f"检索结果中编码为 {code} 的记录不唯一：{items}"
    return found[0]


# --- what the twelve calls have to have left behind (requirement 25.5) -------------------


def check_flow(flow: Flow) -> None:
    """Check the results of the twelve calls against requirement 25.5.

    Every line of the order is linked in full, the order is closed, and the stock of each
    (material, warehouse) the receipt fed grew by the ``qty_base`` of the receipt lines on
    it. The quantity on hand before the flow is not written down per pair but taken from the
    diff, which reports it as the ``before`` of the balance it changed (:func:`check_diff`);
    what is written down is their sum, which is the stock the initial dataset left in that
    warehouse (:data:`INITIAL_STOCK`).
    """
    # The order was created for every material of the search, and one of its lines is
    # measured in a unit that is not the material's base unit: the engine folded that line's
    # quantity through a conversion, so its ``qty_base`` is not its ``qty``
    # (requirements 6.3 and 25.5).
    lines = flow.order_lines
    assert len(lines) == len(flow.order["lines"])
    assert len(lines) >= 2, f"数据集至少要有两个启用物料，这个订单只有 {len(lines)} 行"
    folded = [line for line in lines if Decimal(str(line["qty_base"])) != Decimal(str(line["qty"]))]
    assert folded, f"订单中没有采购单位与基本单位不同的物料：{lines}"
    assert len(folded) < len(lines), f"订单中没有以基本单位下单的物料：{lines}"

    # Every line is linked in full and the order is closed (requirements 8.8 and 8.13).
    for line in lines:
        assert Decimal(str(line["linked_qty"])) == Decimal(str(line["qty_base"])), line
    assert flow.order_closed["status"] == CLOSED, flow.order_closed
    assert flow.order_closed["id"] == flow.order_id

    # The two bills were created as drafts and each submit and approve answered with the
    # state it put its bill in (requirement 7.1); the order being closed is the writeback's
    # doing, not an operation the flow asked for.
    assert flow.order["status"] == flow.receipt["status"] == DRAFT
    assert flow.statuses == {
        ORDER_SUBMIT: SUBMITTED,
        ORDER_APPROVE: APPROVED,
        RECEIPT_SUBMIT: SUBMITTED,
        RECEIPT_APPROVE: APPROVED,
    }

    # The receipt took in every line of the order, in full (requirement 8.1).
    assert flow.receipt["source_bill"]["id"] == flow.order_id
    assert len(flow.receipt_lines) == len(lines)
    for line, source in zip(flow.receipt_lines, lines, strict=True):
        assert line["source_line_id"] == source["id"]
        assert Decimal(str(line["qty_base"])) == Decimal(str(source["qty_base"])), line

    # And the stock of the warehouse the receipt fed is what the query reports for each
    # (material, warehouse) of its lines (requirement 9.12).
    on_hand = flow.on_hand
    for place, received in flow.received.items():
        assert place in on_hand, f"即时库存查询没有返回 {place}：{flow.balances}"
        assert received > 0
    assert {place[1] for place in on_hand} == {flow.warehouse_id}


def check_diff(flow: Flow, payload: Mapping[str, Any], sandbox_id: str) -> None:
    """Check the diff of the sandbox the flow ran in against requirement 25.5.

    The header of requirement 18.1, the net change of the order and the receipt with their
    lines, the stock transactions of the receipt and the balance of each (material,
    warehouse) it fed - and nothing else, the change list being exactly these records in the
    order of requirement 18.6. The quantity on hand the initial dataset left is read here,
    from the ``before`` of each balance the flow changed, and the stock requirement 25.5
    asks about is checked against it: a pair the dataset had no record for starts at 0
    (requirement 9.5).
    """
    # The six keys of requirement 18.1, with the anchor date the service itself reported as
    # its business date (module docstring).
    assert list(payload) == ["sandbox_id", "instance", "dataset", "anchor_date", "base", "changes"]
    assert payload["sandbox_id"] == sandbox_id
    assert payload["instance"] == INSTANCE_REF
    assert payload["dataset"] == DATASET_REF
    assert payload["anchor_date"] == flow.business_date
    assert payload["base"] == INITIAL_BASE

    diff = Diff(payload)
    received = flow.received

    # Exactly the records the flow wrote, ordered by entity and then by id (req 18.6).
    assert diff.keys == [
        (ORDER_ENTITY, flow.order_id),
        *((ORDER_LINE_ENTITY, str(line["id"])) for line in flow.order_lines),
        (RECEIPT_ENTITY, flow.receipt_id),
        *((RECEIPT_LINE_ENTITY, str(line["id"])) for line in flow.receipt_lines),
        *(
            (BALANCE_ENTITY, balance_id(material_id, warehouse_id))
            for material_id, warehouse_id in sorted(received)
        ),
        *(
            (TXN_ENTITY, txn_id(str(line["id"]), flow.warehouse_id, IN))
            for line in flow.receipt_lines
        ),
    ]

    # The order: one record that was not there before, closed, with the header values the
    # tool result reported (requirements 18.2 and 18.5).
    order = diff.after(ORDER_ENTITY, flow.order_id, "insert")
    assert order["status"] == CLOSED
    assert order["number"] == flow.order_closed["number"]
    assert order["supplier_id"] == flow.order_closed["supplier"]["id"]
    assert order["biz_date"] == flow.order_closed["biz_date"] == flow.business_date
    assert order["delivery_date"] == flow.business_date
    assert order["source_bill_id"] is None
    assert [order[key] for key in MONEY_KEYS] == [flow.order_closed[key] for key in MONEY_KEYS]

    # Its lines, each linked in full, with the quantity in the line's own unit beside the
    # one in base units (requirements 18.5 and 18.9).
    for line in flow.order_lines:
        after = diff.after(ORDER_LINE_ENTITY, str(line["id"]), "insert")
        assert after["line_no"] == line["line_no"]
        assert after["material_id"] == line["material"]["id"]
        assert after["unit_id"] == line["unit"]["id"]
        assert after["qty"] == line["qty"]
        assert after["qty_base"] == line["qty_base"]
        assert after["linked_qty"] == line["qty_base"]

    # The receipt and its lines, which name the order and its lines as their source
    # (requirement 8.1).
    receipt = diff.after(RECEIPT_ENTITY, flow.receipt_id, "insert")
    assert receipt["status"] == APPROVED
    assert receipt["number"] == flow.receipt["number"]
    assert receipt["source_bill_type"] == ORDER_ENTITY
    assert receipt["source_bill_id"] == flow.order_id
    assert [receipt[key] for key in MONEY_KEYS] == [flow.receipt[key] for key in MONEY_KEYS]
    for line in flow.receipt_lines:
        after = diff.after(RECEIPT_LINE_ENTITY, str(line["id"]), "insert")
        assert after["source_line_id"] == line["source_line_id"]
        assert after["warehouse_id"] == flow.warehouse_id
        assert after["qty"] == line["qty"]
        assert after["qty_base"] == line["qty_base"]

    # One stock transaction per receipt line, taking its base units in (requirement 9.2).
    for line in flow.receipt_lines:
        identifier = txn_id(str(line["id"]), flow.warehouse_id, IN)
        after = diff.after(TXN_ENTITY, identifier, "insert")
        assert after["material_id"] == line["material"]["id"]
        assert after["warehouse_id"] == flow.warehouse_id
        assert after["source_bill_type"] == RECEIPT_ENTITY
        assert after["source_line_id"] == line["id"]
        assert Decimal(str(after["qty_delta"])) == Decimal(str(line["qty_base"]))

    # And the balance of each pair: what the dataset left plus what the receipt took in,
    # which is also what the balance query answered (requirement 25.5).
    on_hand = flow.on_hand
    initial: dict[tuple[str, str], Decimal] = {}
    for place, quantity in received.items():
        change = diff.change(BALANCE_ENTITY, balance_id(*place))
        before = change.get("before")
        initial[place] = Decimal(0) if before is None else Decimal(str(before["qty_base"]))
        assert (change["op"] == "insert") == (before is None), change
        balance = Decimal(str(change["after"]["qty_base"]))
        assert balance == initial[place] + quantity, change
        assert balance == on_hand[place], change

    # The one quantity of the initial dataset this file writes down (module docstring):
    # 144 base units of the one material the historical receipt took into that warehouse,
    # and no record at all for the other, so the sum of what the pairs started with is it.
    assert sum(initial.values()) == INITIAL_STOCK, initial


@dataclass(frozen=True, slots=True)
class Diff:
    """One diff, read by (entity, id) (requirement 18.2)."""

    payload: Mapping[str, Any]

    @property
    def changes(self) -> list[Mapping[str, Any]]:
        """The changes of the diff, in the order they were written (requirement 18.6)."""
        return [dict(change) for change in self.payload["changes"]]

    @property
    def keys(self) -> list[tuple[str, str]]:
        """The (entity, id) of every change, in that order."""
        return [(str(change["entity"]), str(change["id"])) for change in self.changes]

    def change(self, entity: str, record_id: str) -> Mapping[str, Any]:
        """The one change of the record ``record_id`` of ``entity``; it has to be there."""
        found = [
            change
            for change in self.changes
            if change["entity"] == entity and change["id"] == record_id
        ]
        assert len(found) == 1, f"diff 中 {entity} {record_id} 的变化不唯一：{self.keys}"
        return found[0]

    def after(self, entity: str, record_id: str, op: str) -> Mapping[str, Any]:
        """The ``after`` of that change, whose ``op`` has to be ``op`` (requirement 18.9)."""
        change = self.change(entity, record_id)
        assert change["op"] == op, change
        after = change["after"]
        assert isinstance(after, dict), change
        return after


# --- the flow over Streamable HTTP (requirements 14.1 and 25.5) --------------------------


@pytest.mark.anyio
@pytest.mark.req("14.1", "25.5")
async def test_the_flow_over_streamable_http_closes_the_order_and_feeds_the_stock(
    service: Service,
) -> None:
    """The twelve calls on ``/mcp`` of a serve process, with the diff from the Admin API.

    The anchor date is the day the process runs, serve having no ``--anchor-date`` and the
    clock of p2p-demo being in mode today, so the three dates the service reports - the
    business date of the first call, the ``anchor_date`` of the sandbox metadata and the
    ``anchor_date`` of the diff - are compared with each other and with nothing a test
    computed (requirements 16.1 and 25.10).
    """
    async with http_session(service) as (session, session_id):
        flow = await run_flow(session)

    check_flow(flow)

    # The sandbox the session created, which is the one the Admin API reports that session
    # for (requirements 15.1 and 19.12).
    metadata = sandbox_of_session(service, session_id)
    assert metadata["dataset"] == DATASET_REF
    assert metadata["anchor_date"] == flow.business_date

    # And its diff, read after the session ended: the sandbox is frozen by then and the
    # Admin API reads the diff of a frozen sandbox (requirements 15.12 and 19.8).
    sandbox_id = str(metadata["sandbox_id"])
    payload = service.admin_json(f"{SANDBOXES_PATH}/{sandbox_id}/diff")
    check_diff(flow, payload, sandbox_id)


# --- the flow over stdio (requirements 14.1 and 25.5) ------------------------------------


@pytest.mark.anyio
@pytest.mark.req("14.1", "25.5")
async def test_the_flow_over_stdio_closes_the_order_and_feeds_the_stock(
    tmp_path: Path,
) -> None:
    """The twelve calls over the stdin and stdout of a stdio process, with its ``diff.json``.

    stdio has ``--anchor-date``, so this run fixes it: the business date the service reports
    and the ``anchor_date`` of the diff it writes both have to be that date, where the HTTP
    run can only compare the service's dates with each other (module docstring).
    """
    data_dir = tmp_path / "var"
    args = stdio_args(data_dir, anchor_date=STDIO_ANCHOR_DATE)

    async with stdio_session(tmp_path, args, client_info=CLIENT_INFO) as session:
        flow = await run_flow(session)

    check_flow(flow)
    assert flow.business_date == STDIO_ANCHOR_DATE.isoformat()

    # The process has ended, so the diff of its sandbox is in the sandbox directory
    # (requirement 22.4).
    sandbox = one_sandbox(data_dir)
    payload = json.loads((sandbox / DIFF_FILENAME).read_text(encoding="utf-8"))
    assert isinstance(payload, dict), payload
    check_diff(flow, payload, sandbox.name)


# --- a client that was configured with the address or the command alone (req 14.9) -------


@pytest.mark.anyio
@pytest.mark.req("14.9")
async def test_an_address_alone_is_enough_for_the_context_tool_and_the_tool_list(
    service: Service,
) -> None:
    """An address, default everything else: the tool call may come before the listing.

    The service is the one of this module: ``--host`` is a loopback address and no MCP
    endpoint token is configured, which is the ``serve`` case of requirement 14.9. The
    client is ``streamable_http_client(url)`` and ``ClientSession`` with no arguments beyond
    the two streams - no header, no parameter, no client info - and the two calls go out
    through the plain methods of the SDK, the tool first, so that neither call depends on
    the other having happened.
    """
    with anyio.fail_after(SESSION_TIMEOUT_S):
        async with streamable_http_client(service.mcp_url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()

                # ``call_tool`` validates the structured result against the output schema it
                # fetches, so this one call exercises the client's own view of the contract
                # as well (requirement 13.1).
                called = await client.call_tool(BUSINESS_CONTEXT_GET, {})
                listed = await client.list_tools()

    assert called.isError is False, result_text(called)
    assert called.structuredContent is not None
    assert called.structuredContent["business_date"]

    # The tools of the session's profile, in one response and with no cursor
    # (requirement 14.5); the twelve of the flow are among them.
    names = [tool.name for tool in listed.tools]
    assert listed.nextCursor is None
    assert len(names) == len(set(names))
    assert set(FLOW_TOOLS) <= set(names), names


@pytest.mark.anyio
@pytest.mark.req("14.9")
async def test_a_start_command_alone_is_enough_for_the_context_tool_and_the_tool_list(
    tmp_path: Path,
) -> None:
    """A start command, default everything else: the same two calls over stdio.

    The command names the instance configuration and the business packs and nothing more:
    the profile, the initial dataset, the anchor date and the data directory are all left to
    their defaults (requirements 14.2 and 22.1), the last of which is ``./var`` of the
    working directory the client starts the process in. The client passes no environment and
    no client info, so the SDK's own defaults are what the process is given
    (:func:`~mcp.client.stdio.get_default_environment`).
    """
    async with stdio_session(tmp_path, stdio_args()) as session:
        called = await session.client.call_tool(BUSINESS_CONTEXT_GET, {})
        listed = await session.client.list_tools()

    assert called.isError is False, result_text(called)
    assert called.structuredContent is not None
    assert called.structuredContent["business_date"]

    names = [tool.name for tool in listed.tools]
    assert listed.nextCursor is None
    assert set(FLOW_TOOLS) <= set(names), names

    # The defaults reached the sandbox: it was created from the default dataset of the
    # instance configuration, below the default data directory (requirement 15.1).
    sandbox = one_sandbox(tmp_path / "var")
    payload = json.loads((sandbox / DIFF_FILENAME).read_text(encoding="utf-8"))
    assert payload["dataset"] == DATASET_REF
    assert payload["changes"] == []
