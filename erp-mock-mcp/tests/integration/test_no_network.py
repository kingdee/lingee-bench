"""The flow of requirement 25.5 in a process that cannot reach beyond the loopback address.

Requirement 24.5 says the service computes its results from the business configuration, the
sandbox database, the anchor date and the request parameters alone: at run time it calls no
LLM and opens no connection to any host but the loopback one. A test cannot prove that from
the outside - a connection that was never made leaves no trace - so this file turns it
around and takes the network away from the service: the subprocess is started with a
``sitecustomize`` on its ``PYTHONPATH`` that replaces the four ways a Python process reaches
a host (:data:`INTERCEPTOR_SOURCE`) and raises instead of letting a non-loopback target
through. A service that depended on anything out there would fail in front of the client.

What the interceptor does, at interpreter startup and before the command it was started
with runs (``site.execsitecustomize``):

===========================  ============================================================
``socket.socket.connect``    every outbound TCP or UDP connection, including the ones
``socket.socket.connect_ex`` ``asyncio`` makes through ``loop.sock_connect`` and the ones
                             an ``ssl`` socket makes, which inherit these two
``socket.create_connection`` what ``http.client``, ``httpx`` and ``urllib`` connect with
``socket.getaddrinfo``       every name resolution, ``loop.getaddrinfo`` included, so a
                             host name is refused before a DNS packet is sent
===========================  ============================================================

A target of the loopback interface - ``127.0.0.0/8``, ``::1``, the unspecified address and
the names that resolve to them - is let through, and so is an ``AF_UNIX`` socket, which is a
path in the file system rather than a host; the ``serve`` process binds and listens on the
loopback address and has to keep working. Everything else is refused with
``ExternalNetworkBlocked``, and every decision, with the call it came from and the address
it was about, is appended to the ``network.jsonl`` beside the interceptor
(:class:`Interceptor`), so a test reads afterwards what the process did rather than guessing.

The three tests, each of which has to hold for the file to mean anything:

==========================================  ==============================================
:func:`test_the_interceptor_refuses_every_non_loopback_target_and_lets_the_loopback_through`
                                            the interceptor itself, in a process of its
                                            own: the four calls above are refused for a
                                            non-loopback address and a name that does not
                                            resolve, and a connection to a listening
                                            loopback socket and a resolution of
                                            ``localhost`` go through. Without this the two
                                            tests below would pass just as well with an
                                            interceptor that was never installed
:func:`test_the_flow_over_stdio_reaches_nothing_but_the_loopback_address`
                                            the twelve calls of requirement 25.5 over the
                                            stdin and stdout of an intercepted ``stdio``
                                            process, checked in the results and in the
                                            ``diff.json`` it writes as it exits
:func:`test_the_flow_over_streamable_http_reaches_nothing_but_the_loopback_address`
                                            the same twelve calls on the ``/mcp`` endpoint
                                            of an intercepted ``serve`` process, whose two
                                            ports are on the loopback address the
                                            interceptor lets through, with the diff from
                                            the Admin API
==========================================  ==============================================

The two flow tests assert both directions. The flow went through - every call answered with
``isError`` false, the order ended ``closed`` and the stock grew by what the receipt took in
(:func:`~tests.integration.test_protocol_flow.check_flow`,
:func:`~tests.integration.test_protocol_flow.check_diff`) - and the interceptor was in that
very process while it did: its log holds exactly one install record, written after the four
replacements were in place, whose ``argv`` is the command that was started, whose ``wrapped``
says the four names it had replaced were still its own and whose ``rejects`` and ``allows``
say the guard it installed refuses a non-loopback target and admits a loopback one. Not one
record of a refusal means the service never asked for anything beyond the loopback address.

What runs: ``instances/p2p-demo`` as it ships, the twelve calls and the checks of
tests/integration/test_protocol_flow.py, imported rather than written again - this file is
about the network and nothing else. The processes are the console script beside the
interpreter running the tests, with a data directory below ``tmp_path``; the environment of
each is built from nothing (:func:`~tests.integration.test_protocol_flow.process_env`) plus
the ``PYTHONPATH`` that carries the interceptor, so no ``ERP_MOCK_*`` of the machine reaches
a service (requirement 23.9). Every wait is a poll against a deadline and the module as a
whole is kept under :data:`MODULE_BUDGET_S`.

The anchor date, as in tests/integration/test_protocol_flow.py: the ``stdio`` run fixes it
with ``--anchor-date`` and ``serve`` has no such option, so the HTTP run compares the dates
the service reports with each other and with nothing a test computed (requirement 25.10).
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from erp_mock.cli.options import EXIT_OK
from erp_mock.gateway.handlers import SERVER_NAME
from erp_mock.gateway.serve import bind_socket
from erp_mock.gateway.stdio import DIFF_FILENAME
from tests.integration.test_protocol_flow import (
    CLIENT_INFO,
    COMMAND,
    DATASET_REF,
    INSTANCE_DIR,
    LOOPBACK,
    PACKS_DIR,
    SANDBOXES_PATH,
    SESSION_TIMEOUT_S,
    STDIO_ANCHOR_DATE,
    Service,
    Session,
    check_diff,
    check_flow,
    free_port,
    http_session,
    one_sandbox,
    process_env,
    run_flow,
    sandbox_of_session,
    stdio_args,
    wait_for,
)

# --- the interceptor a subprocess is started with ----------------------------------------

# The module name the interpreter imports at startup from a directory of its ``PYTHONPATH``,
# the log it appends every decision to beside itself, and the exception a refusal raises.
INTERCEPTOR_FILENAME: Final = "sitecustomize.py"
LOG_FILENAME: Final = "network.jsonl"
BLOCKED_ERROR: Final = "ExternalNetworkBlocked"

# The three kinds of record the log holds (module docstring).
INSTALL: Final = "install"
ALLOWED: Final = "allowed"
BLOCKED: Final = "blocked"

INTERCEPTOR_SOURCE: Final = '''"""Refuse every connection of this process that is not to the loopback interface.

Written by tests/integration/test_no_network.py into a directory it puts on the
``PYTHONPATH`` of a subprocess, so the interpreter imports it at startup
(``site.execsitecustomize``) before the command runs. Every decision is appended to the
``network.jsonl`` beside this file, which is what the test reads afterwards.
"""

import ipaddress
import json
import os
import socket
import sys

_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "network.jsonl")

# The names a resolver would answer with a loopback address.
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})

# What the install record reports the guard's own decisions over: an address of TEST-NET-3
# (RFC 5737) and a name under the reserved .invalid top level domain (RFC 2606), neither of
# which is touched - only the predicate is asked about them.
_SAMPLE_HOST = "203.0.113.1"
_SAMPLE_NAME = "no-such-host.invalid"

_BLOCKED = "不得向回环地址以外的主机发起网络连接：{call} {address!r}（需求 24 第 5 条）"

# A socket of this family is a path in the file system rather than a host.
_AF_UNIX = getattr(socket, "AF_UNIX", None)


class ExternalNetworkBlocked(OSError):
    """Raised in place of connecting to or resolving anything but the loopback interface."""


def _record(kind, call, host, address, **extra):
    """Append one decision to the log; a log that cannot be written is not fatal."""
    entry = {
        "kind": kind,
        "call": call,
        "host": "" if host is None else str(host),
        "address": repr(address),
        "pid": os.getpid(),
        "argv": list(sys.argv),
    }
    entry.update(extra)
    try:
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\\n")
    except OSError:
        pass


def _is_local(host):
    """Whether ``host`` is the loopback interface, by name or by address.

    None and the empty string are the host of a bind rather than of a connection
    (``getaddrinfo(None, port)`` with AI_PASSIVE), and the unspecified address reaches this
    machine alone as well.
    """
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    name = host.strip("[]").split("%")[0]
    if name == "" or name.lower() in _LOCAL_NAMES:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _guard_host(call, host, address):
    """Let ``host`` through if it is the loopback interface, and raise if it is not."""
    if _is_local(host):
        _record("allowed", call, host, address)
        return
    _record("blocked", call, host, address)
    raise ExternalNetworkBlocked(_BLOCKED.format(call=call, address=address))


def _guard_socket(call, sock, address):
    """The same for one socket, whose family says how to read ``address``."""
    if _AF_UNIX is not None and getattr(sock, "family", None) == _AF_UNIX:
        _record("allowed", call, "unix", address)
        return
    host = address[0] if isinstance(address, tuple) and address else address
    _guard_host(call, host, address)


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection
_real_getaddrinfo = socket.getaddrinfo


def _connect(self, address):
    _guard_socket("connect", self, address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _guard_socket("connect_ex", self, address)
    return _real_connect_ex(self, address)


def _create_connection(address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) and address else address
    _guard_host("create_connection", host, address)
    return _real_create_connection(address, *args, **kwargs)


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    _guard_host("getaddrinfo", host, (host, port))
    return _real_getaddrinfo(host, port, family, type, proto, flags)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.create_connection = _create_connection
socket.getaddrinfo = _getaddrinfo

# Written last, so a record of it means the four replacements above are in place: what this
# process was started as, that the four names are still this module's, and that the guard
# refuses a non-loopback target and admits a loopback one.
_record(
    "install",
    "sitecustomize",
    None,
    _LOG,
    wrapped=(
        socket.socket.connect is _connect
        and socket.socket.connect_ex is _connect_ex
        and socket.create_connection is _create_connection
        and socket.getaddrinfo is _getaddrinfo
    ),
    rejects=not _is_local(_SAMPLE_HOST) and not _is_local(_SAMPLE_NAME),
    allows=_is_local("127.0.0.1") and _is_local("::1") and _is_local("localhost"),
)
'''

# --- the probe that shows the interceptor works (module docstring) -----------------------

PROBE_FILENAME: Final = "probe.py"
PROBE_RESULT_FILENAME: Final = "probe.json"

# The six attempts of the probe: the four calls the interceptor replaced against a
# non-loopback target, and two of them against the loopback interface.
PROBE_ATTEMPTS: Final = ("connect", "connect_ex", "create_connection", "getaddrinfo")
PROBE_LOOPBACK: Final = ("loopback-connect", "loopback-getaddrinfo")

PROBE_SOURCE: Final = '''"""Try the four calls against a non-loopback target and two against the loopback one.

Run by tests/integration/test_no_network.py with the interceptor on its ``PYTHONPATH``. Each
attempt is recorded as the exception it raised, or as "ok" where it went through, and the
whole list is written as JSON to the path of the second argument; the first argument is the
port of a loopback socket the test has listening.

A refusal happens before any syscall, so nothing leaves this machine. Were the interceptor
missing, the non-loopback attempts would end in a timeout or a resolver error instead, which
is a different answer rather than a hang.
"""

import json
import socket
import sys

# TEST-NET-3 (RFC 5737) and the reserved .invalid top level domain (RFC 2606).
EXTERNAL_HOST = "203.0.113.1"
EXTERNAL_NAME = "no-such-host.invalid"
EXTERNAL_PORT = 80
TIMEOUT_S = 0.5


def attempt(name, call):
    try:
        call()
    except BaseException as error:
        return {"name": name, "outcome": type(error).__name__, "message": str(error)}
    return {"name": name, "outcome": "ok", "message": ""}


def connect_external():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(TIMEOUT_S)
        sock.connect((EXTERNAL_HOST, EXTERNAL_PORT))


def connect_ex_external():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(TIMEOUT_S)
        sock.connect_ex((EXTERNAL_HOST, EXTERNAL_PORT))


def create_connection_external():
    socket.create_connection((EXTERNAL_HOST, EXTERNAL_PORT), timeout=TIMEOUT_S).close()


def getaddrinfo_external():
    socket.getaddrinfo(EXTERNAL_NAME, EXTERNAL_PORT)


def main():
    port, out = int(sys.argv[1]), sys.argv[2]

    def connect_loopback():
        socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT_S).close()

    def getaddrinfo_loopback():
        socket.getaddrinfo("localhost", port, socket.AF_INET, socket.SOCK_STREAM)

    results = [
        attempt("connect", connect_external),
        attempt("connect_ex", connect_ex_external),
        attempt("create_connection", create_connection_external),
        attempt("getaddrinfo", getaddrinfo_external),
        attempt("loopback-connect", connect_loopback),
        attempt("loopback-getaddrinfo", getaddrinfo_loopback),
    ]
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False)


main()
'''

# --- the deadlines -----------------------------------------------------------------------

# What this module may take as a whole: three processes over p2p-demo, two of which run the
# twelve calls of requirement 25.5. A run that reaches this has already failed.
MODULE_BUDGET_S: Final = 300.0

# The probe neither resolves nor connects to anything outside, so it ends within a second.
PROBE_TIMEOUT_S: Final = 30.0

# The data directory below the working directory of each process, as ``--data-dir`` names it.
DATA_DIR_NAME: Final = "var"

# The two subcommands of the service, which the install record of each process has to carry.
SERVE: Final = "serve"
STDIO: Final = "stdio"


@pytest.fixture(scope="module", autouse=True)
def installed_command() -> None:
    """Two tests here run the console script; a run without it would be meaningless."""
    assert COMMAND.is_file(), f"没有找到安装好的命令：{COMMAND}"
    assert os.access(COMMAND, os.X_OK)


@pytest.fixture
def anyio_backend() -> str:
    """The one event loop these tests run on, as every async test of the suite does."""
    return "asyncio"


@pytest.fixture(scope="module")
def module_deadline() -> float:
    """When this module has used up :data:`MODULE_BUDGET_S` (module docstring)."""
    return time.monotonic() + MODULE_BUDGET_S


@pytest.fixture(autouse=True)
def within_budget(module_deadline: float) -> Iterator[None]:
    """Fail readably where the module has run longer than its wall clock budget."""
    assert module_deadline > time.monotonic(), (
        f"模块的墙钟上限 {MODULE_BUDGET_S} 秒在这个用例开始前已用尽"
    )
    yield
    assert module_deadline > time.monotonic(), (
        f"模块的墙钟上限 {MODULE_BUDGET_S} 秒在这个用例结束时已用尽"
    )


# --- the interceptor and its log ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Interceptor:
    """One directory holding the interceptor of a process, and what that process did.

    ``pythonpath`` is what the subprocess is given as its ``PYTHONPATH``, and ``log`` the
    file the interceptor appends its decisions to. One directory per process, so the records
    of one test are never another's.
    """

    directory: Path

    @property
    def pythonpath(self) -> str:
        """The one entry of the ``PYTHONPATH`` the interpreter imports the module from."""
        return str(self.directory)

    @property
    def log(self) -> Path:
        """``network.jsonl`` beside the interceptor; missing until something is recorded."""
        return self.directory / LOG_FILENAME

    def records(self) -> list[dict[str, Any]]:
        """Every decision the interceptor recorded, in the order it made them."""
        if not self.log.is_file():
            return []
        text = self.log.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line]

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        """The records of one kind: :data:`INSTALL`, :data:`ALLOWED` or :data:`BLOCKED`."""
        return [record for record in self.records() if record["kind"] == kind]


def interceptor(directory: Path) -> Interceptor:
    """Write the interceptor into ``directory`` and hand back the handle to it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / INTERCEPTOR_FILENAME).write_text(INTERCEPTOR_SOURCE, encoding="utf-8")
    return Interceptor(directory=directory)


def check_interceptor(guard: Interceptor, subcommand: str, context: str) -> None:
    """The interceptor was in the service process, and it refused nothing that process did.

    The install record is written after the four replacements are in place (module
    docstring), so one record of the command that was started - with its three self reported
    flags - is what makes the absence of refusals mean something: the service asked for
    nothing beyond the loopback address, in a process where asking would have failed.
    """
    installs = guard.of_kind(INSTALL)
    assert len(installs) == 1, f"拦截器没有恰好装到一个进程里：{installs}\n{context}"
    install = installs[0]
    assert install["argv"][0] == str(COMMAND), install
    assert subcommand in install["argv"], install
    assert install["wrapped"] is True, install
    assert install["rejects"] is True, install
    assert install["allows"] is True, install

    blocked = guard.of_kind(BLOCKED)
    assert blocked == [], f"服务进程试图连接回环地址以外的主机：{blocked}\n{context}"


# --- the two intercepted processes -------------------------------------------------------


@contextmanager
def intercepted_serve(workdir: Path, guard: Interceptor) -> Iterator[Service]:
    """Start one intercepted serve process over p2p-demo on two free ports, and serve.

    The same process tests/integration/test_protocol_flow.py starts - the console script,
    both ports on the loopback address, no MCP endpoint token, a data directory below
    ``workdir``, stdout and stderr in files beside it - with the interceptor on its
    ``PYTHONPATH``: binding and listening on the loopback address is what the interceptor
    lets through, so the service has to come up as it always does. Whatever is still running
    when the block ends is stopped.
    """
    data_dir = workdir / DATA_DIR_NAME
    out, err = workdir / "stdout.txt", workdir / "stderr.txt"
    port, admin_port = free_port(), free_port()
    args = [
        str(COMMAND),
        SERVE,
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
            env=process_env(PYTHONPATH=guard.pythonpath),
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


@asynccontextmanager
async def intercepted_stdio(workdir: Path, guard: Interceptor) -> AsyncIterator[Session]:
    """Spawn one intercepted stdio process through the official client and serve a session.

    The process is gone after the block: ``stdio_client`` closes its stdin, which is the end
    of file of requirement 22.4, and waits for it to end, so the ``diff.json`` of its sandbox
    is there once the block has left. Its environment is built from nothing plus the
    ``PYTHONPATH`` that carries the interceptor, which is why the SDK's own
    ``get_default_environment()`` is not used: it passes no ``PYTHONPATH`` on.
    """
    args = stdio_args(workdir / DATA_DIR_NAME, STDIO_ANCHOR_DATE)
    params = StdioServerParameters(
        command=args[0],
        args=list(args[1:]),
        cwd=str(workdir),
        env=process_env(PYTHONPATH=guard.pythonpath),
    )
    with (workdir / "stderr.txt").open("w", encoding="utf-8") as errlog:
        with anyio.fail_after(SESSION_TIMEOUT_S):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, client_info=CLIENT_INFO) as client:
                    initialized = await client.initialize()
                    assert initialized.serverInfo.name == SERVER_NAME
                    yield Session(client=client)


# --- the interceptor itself (module docstring) -------------------------------------------


@pytest.mark.req("24.5")
def test_the_interceptor_refuses_every_non_loopback_target_and_lets_the_loopback_through(
    tmp_path: Path,
) -> None:
    """A process of its own, the six attempts of the probe, and the log of all six.

    The four calls the interceptor replaced are refused for an address of TEST-NET-3 and a
    name under ``.invalid``, each with the one exception the interceptor raises, and the two
    loopback attempts - a connection to a socket this test has listening and a resolution of
    ``localhost`` - go through. The flow tests below read the same log for the absence of
    refusals, so this is what gives that absence its meaning.
    """
    guard = interceptor(tmp_path / "intercept")
    probe = tmp_path / PROBE_FILENAME
    probe.write_text(PROBE_SOURCE, encoding="utf-8")
    result = tmp_path / PROBE_RESULT_FILENAME

    with closing(bind_socket(LOOPBACK, 0)) as listener:
        listener.listen(1)
        port = int(listener.getsockname()[1])
        completed = subprocess.run(
            [sys.executable, str(probe), str(port), str(result)],
            cwd=tmp_path,
            env=process_env(PYTHONPATH=guard.pythonpath),
            capture_output=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr.decode("utf-8")
    attempts = json.loads(result.read_text(encoding="utf-8"))
    outcomes = {str(item["name"]): str(item["outcome"]) for item in attempts}
    assert outcomes == {
        **dict.fromkeys(PROBE_ATTEMPTS, BLOCKED_ERROR),
        **dict.fromkeys(PROBE_LOOPBACK, "ok"),
    }, attempts

    # The interceptor was installed in that process and says so itself.
    installs = guard.of_kind(INSTALL)
    assert len(installs) == 1, installs
    assert installs[0]["argv"][0] == str(probe), installs[0]
    assert [installs[0][key] for key in ("wrapped", "rejects", "allows")] == [True, True, True]

    # And it recorded the four refusals, in the order the probe tried them, each about the
    # target the probe named.
    blocked = guard.of_kind(BLOCKED)
    assert [record["call"] for record in blocked] == list(PROBE_ATTEMPTS), blocked
    assert {record["host"] for record in blocked} == {"203.0.113.1", "no-such-host.invalid"}

    # The two loopback attempts went through the same guard: the resolution of each and the
    # connection of the first are the calls it let through.
    allowed = guard.of_kind(ALLOWED)
    assert {record["call"] for record in allowed} == {
        "create_connection",
        "getaddrinfo",
        "connect",
    }, allowed
    assert {record["host"] for record in allowed} == {"127.0.0.1", "localhost"}, allowed


# --- the flow over stdio, with no network (requirements 24.5 and 25.5) -------------------


@pytest.mark.anyio
@pytest.mark.req("24.5")
async def test_the_flow_over_stdio_reaches_nothing_but_the_loopback_address(
    tmp_path: Path,
) -> None:
    """The twelve calls of requirement 25.5 in an intercepted stdio process.

    stdio needs no socket at all, so this is the service with the network taken away
    entirely: the order is created, submitted, approved, received and closed, and the stock
    grows by what the receipt took in, out of the business packs, the sandbox database, the
    anchor date and the request parameters alone (requirement 24.5). ``--anchor-date`` fixes
    the date, so the business date the service reports and the ``anchor_date`` of the diff
    it writes are compared with that one.
    """
    guard = interceptor(tmp_path / "intercept")

    async with intercepted_stdio(tmp_path, guard) as session:
        flow = await run_flow(session)

    check_flow(flow)
    assert flow.business_date == STDIO_ANCHOR_DATE.isoformat()

    # The process has ended, so the diff of its sandbox is in the sandbox directory
    # (requirement 22.4).
    sandbox = one_sandbox(tmp_path / DATA_DIR_NAME)
    payload = json.loads((sandbox / DIFF_FILENAME).read_text(encoding="utf-8"))
    assert isinstance(payload, dict), payload
    check_diff(flow, payload, sandbox.name)

    # And it did all of that without asking for one host beyond the loopback address.
    check_interceptor(guard, STDIO, (tmp_path / "stderr.txt").read_text(encoding="utf-8"))


# --- the flow over Streamable HTTP, with no network (requirements 24.5 and 25.5) ---------


@pytest.mark.anyio
@pytest.mark.req("24.5")
async def test_the_flow_over_streamable_http_reaches_nothing_but_the_loopback_address(
    tmp_path: Path,
) -> None:
    """The same twelve calls on ``/mcp`` of an intercepted serve process.

    The transport the service is reached over is a socket of its own, which is what makes
    this run worth having beside the stdio one: the two ports bind and listen on the
    loopback address the interceptor admits, the client of the test connects to them from a
    process that is not intercepted, and the service still reaches no host of its own - no
    resolution, no outbound connection, for the whole session and the five Admin requests
    around it.

    serve has no ``--anchor-date`` and the clock of p2p-demo is in mode today, so the dates
    here are the service's own: the business date of the first call, the ``anchor_date`` of
    the sandbox metadata and the ``anchor_date`` of the diff have to agree with each other
    (requirement 25.10).
    """
    guard = interceptor(tmp_path / "intercept")

    with intercepted_serve(tmp_path, guard) as service:
        async with http_session(service) as (session, session_id):
            flow = await run_flow(session)

        check_flow(flow)

        # The sandbox of that session, and its diff, read after the session ended: the
        # sandbox is frozen by then and the Admin API still reads its diff (req 15.12).
        metadata = sandbox_of_session(service, session_id)
        assert metadata["dataset"] == DATASET_REF
        assert metadata["anchor_date"] == flow.business_date
        sandbox_id = str(metadata["sandbox_id"])
        payload = service.admin_json(f"{SANDBOXES_PATH}/{sandbox_id}/diff")
        check_diff(flow, payload, sandbox_id)

        # SIGTERM, and the process ends the way it ends without the interceptor
        # (requirement 22.1).
        assert service.terminate() == EXIT_OK, service.stderr

    check_interceptor(guard, SERVE, service.stderr)
