"""The stdio command as a client starts it: a real process, real pipes, a real client.

tests/unit/gateway/test_stdio_runtime.py drives :func:`~erp_mock.gateway.stdio.run_stdio`
over memory streams with every dependency replaced; this file starts the installed
``erp-mock-mcp stdio`` as a subprocess and asks of it what only a process can show
(task 24.4):

- the official MCP Python SDK client spawns the command, completes the handshake, lists the
  tools of the profile and calls ``business_context_get`` over the real stdin and stdout of
  that process (requirement 22.2); closing stdin ends it with exit code 0 and leaves the
  ``diff.json`` and the ``trace.jsonl`` of its sandbox in the sandbox directory
  (requirements 22.3 and 22.4);
- stdout carries MCP messages and nothing else: the sandbox notice, the log of the service
  and every other line the process writes are on stderr (requirement 22.2);
- the two names of requirement 14.12 are checked after validation step 1: a ``--profile`` or
  ``--dataset`` the instance configuration does not have ends the process with exit code 2,
  an empty stdout and no sandbox anywhere;
- a loading that failed is exit code 1 with the error list of requirement 2.2 on stderr, an
  empty stdout and no sandbox (requirement 22.7).

The process is the console script of ``[project.scripts]`` beside the interpreter running
the tests (:data:`COMMAND`), as in tests/integration/test_cli_serve.py: it is the command a
client's configuration names, shebang and entry point included.

Two ways of talking to it, because they show different things:

======================================  ================================================
:func:`client_over`                     the official ``stdio_client``, which spawns the
                                        process, awaits each response and closes stdin on
                                        the way out - the conversation a client has
:func:`run_until_exit`                  ``subprocess.run`` with the JSON-RPC lines as
                                        stdin, which hands back the raw bytes of stdout
======================================  ================================================

``stdio_client`` consumes stdout itself, so the one test that reads those bytes writes the
messages by hand. Such a run sends everything and closes stdin at once, and the SDK's stdio
session closes its writer when its read stream ends: a request still being handled loses its
response, which is why that test asserts over the shape of every line it did get and over
the trace the process wrote rather than over a response per request. The conversation of a
client, where each response is awaited before the next request goes out, is the
:func:`client_over` test.

What ``stdio_client`` does not hand back is the exit code: it closes stdin, waits for the
process and tells no one what it exited with. The command is therefore wrapped in
:data:`RECORD_EXIT`, a shell line that runs it with the same three streams and writes its
status into a file of the test (:func:`wrapped`), which is how requirement 22.4's exit code
0 is read.

The environment of every process here is built from nothing - the few variables a process
needs plus whatever the test sets - so no ``ERP_MOCK_*`` of the machine the tests run on
reaches a service (requirement 23.9); the SDK's own ``get_default_environment`` passes the
same kind of short list on.

What the processes run on: the engine test fixture (:mod:`tests.helpers.fixtures`), which
loads and builds in well under a second, with ``--data-dir`` below ``tmp_path``, so each
process builds the Snapshot of the fixture dataset in a cache of its own and writes its
sandbox where the test can read it and pytest removes it. Every run of the module together
stays a few seconds.
"""

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

import anyio
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from erp_mock.cli.options import EXIT_FAILED, EXIT_OK, EXIT_USAGE
from erp_mock.cli.stdio import DATASET_OPTION, PROFILE_OPTION
from erp_mock.gateway.handlers import SERVER_NAME
from erp_mock.gateway.sandbox import SANDBOX_ID_PREFIX, SANDBOXES_DIR
from erp_mock.gateway.stdio import DIFF_FILENAME, SANDBOX_NOTICE
from erp_mock.gateway.trace import TRACE_FILENAME, EventName
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET
from tests.helpers.fixtures import (
    ANCHOR_DATE,
    DATASET,
    INSTANCE_DIR,
    INSTANCE_NAME,
    PACKS_DIR,
    PROFILE,
)

# The installed command, beside the interpreter that runs the tests (module docstring).
COMMAND: Final = Path(sys.executable).parent / "erp-mock-mcp"

# The variables a subprocess is given beyond what a test sets: enough to run, and not one of
# the ``ERP_MOCK_*`` the environment of the test process may carry (requirement 23.9).
PASSED_THROUGH: Final = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")

# The shell that records the exit code of the process the SDK client spawns (module
# docstring): ``sh -c '"$@"; printf %s "$?" > "$0"' <file> <command> <args...>`` runs the
# command with the stdin, stdout and stderr the client gave the shell and writes the status
# it ended with into ``<file>``.
SHELL: Final = "/bin/sh"
RECORD_EXIT: Final = '"$@"; printf %s "$?" > "$0"'
EXIT_CODE_FILE: Final = "exit-code.txt"
STDERR_FILE: Final = "stderr.txt"

# The deadlines. A process loads the fixture and builds the Snapshot of its dataset in well
# under a second, and the conversation is three requests; a test that reaches either of
# these has already failed.
SESSION_TIMEOUT_S: Final = 20.0
RUN_TIMEOUT_S: Final = 30.0

# What the client calls itself in the handshake, and the protocol version it announces.
CLIENT_INFO: Final = types.Implementation(name="cli-stdio-test", version="0.0.0")
PROTOCOL_VERSION: Final = "2025-11-25"

# The messages the one test that reads the bytes of stdout writes by hand (module
# docstring).
JSONRPC_VERSION: Final = "2.0"
INITIALIZE: Final = "initialize"
INITIALIZED: Final = "notifications/initialized"
TOOLS_LIST: Final = "tools/list"
TOOLS_CALL: Final = "tools/call"
INITIALIZE_PARAMS: Final[Mapping[str, Any]] = {
    "protocolVersion": PROTOCOL_VERSION,
    "capabilities": {},
    "clientInfo": {"name": CLIENT_INFO.name, "version": CLIENT_INFO.version},
}

# The keys of a JSON-RPC response, which is all a well-behaved stdout holds (req 22.2).
RESPONSE_KEYS: Final = ("id", "jsonrpc", "result")

# The six keys of a diff and the basis of the one the exit writes (requirement 18.1).
DIFF_KEYS: Final = ["sandbox_id", "instance", "dataset", "anchor_date", "base", "changes"]
INITIAL_BASE: Final = "initial"

# What the fixture instance, its one profile and its one initial dataset are called where
# the service writes each as ``<名称>@<版本>`` (requirements 17.2 and 18.1).
VERSION: Final = "1.0.0"
INSTANCE_REF: Final = f"{INSTANCE_NAME}@{VERSION}"
PROFILE_REF: Final = f"{PROFILE}@{VERSION}"
DATASET_REF: Final = f"{DATASET}@{VERSION}"

# A name no profile and no dataset of the fixture instance carries (requirement 14.12).
UNKNOWN_NAME: Final = "no-such-name"

# The first error of a loading that failed, as the loader formats its list (req 2.2).
STEP1_PREFIX: Final = "[step 1]"


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
    """The environment of one stdio process: those of :data:`PASSED_THROUGH` and ``extra``."""
    inherited = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    return {**inherited, **extra}


def stdio_args(
    data_dir: Path,
    *,
    profile: str | None = None,
    dataset: str | None = None,
    anchor_date: date | None = None,
    instance_dir: Path = INSTANCE_DIR,
    packs_dir: Path = PACKS_DIR,
) -> list[str]:
    """The command line of one stdio run over the engine fixture (requirement 22.2)."""
    args = [
        str(COMMAND),
        "stdio",
        "--instance",
        str(instance_dir),
        "--packs-dir",
        str(packs_dir),
        "--data-dir",
        str(data_dir),
    ]
    if profile is not None:
        args += [PROFILE_OPTION, profile]
    if dataset is not None:
        args += [DATASET_OPTION, dataset]
    if anchor_date is not None:
        args += ["--anchor-date", anchor_date.isoformat()]
    return args


def wrapped(exit_file: Path, args: list[str]) -> list[str]:
    """``args`` behind the shell that records what it exited with (module docstring)."""
    return [SHELL, "-c", RECORD_EXIT, str(exit_file), *args]


# --- what a run leaves behind ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finished:
    """A stdio run that has ended: its exit code, its two streams and its data directory."""

    code: int
    stdout: str
    stderr: str
    data_dir: Path

    @property
    def sandboxes(self) -> tuple[Path, ...]:
        """The sandbox directories below ``<data-dir>/sandboxes``, by name; none is none."""
        root = self.data_dir / SANDBOXES_DIR
        if not root.is_dir():
            return ()
        return tuple(sorted(path for path in root.iterdir() if path.is_dir()))

    @property
    def sandbox(self) -> Path:
        """The one sandbox of the process, which serves exactly one (requirement 15.2)."""
        found = self.sandboxes
        assert len(found) == 1, f"期望恰好一个沙箱目录，实际 {found}"
        return found[0]

    def diff(self) -> dict[str, Any]:
        """The ``diff.json`` the exit wrote into the sandbox directory (req 22.4)."""
        payload = json.loads((self.sandbox / DIFF_FILENAME).read_text(encoding="utf-8"))
        assert isinstance(payload, dict), payload
        return payload

    def events(self) -> list[dict[str, Any]]:
        """The trace of the sandbox, one object per line (requirement 22.3)."""
        text = (self.sandbox / TRACE_FILENAME).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line]

    def stdout_messages(self) -> list[dict[str, Any]]:
        """Every line of stdout as JSON; a line that is no JSON fails the test (22.2)."""
        messages: list[dict[str, Any]] = []
        for line in self.stdout.splitlines():
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                pytest.fail(f"stdout 上出现了非 MCP 消息的行：{line!r}（{error}）")
            assert isinstance(payload, dict), payload
            messages.append(payload)
        return messages


def run_until_exit(
    tmp_path: Path, args: list[str], env: Mapping[str, str], data_dir: Path, lines: str = ""
) -> Finished:
    """Run ``args`` with ``lines`` as its stdin and return its exit code and output.

    ``lines`` is what the process reads before stdin reaches end of file, empty for the runs
    that end before they would serve anything. The working directory is ``tmp_path``, so a
    relative path this did not pass could not reach the repository.
    """
    completed = subprocess.run(
        args,
        cwd=tmp_path,
        env=dict(env),
        input=lines.encode("utf-8"),
        capture_output=True,
        timeout=RUN_TIMEOUT_S,
        check=False,
    )
    return Finished(
        code=completed.returncode,
        stdout=completed.stdout.decode("utf-8"),
        stderr=completed.stderr.decode("utf-8"),
        data_dir=data_dir,
    )


def jsonrpc_line(request_id: int, method: str, params: Mapping[str, Any] | None = None) -> str:
    """One JSON-RPC request as the stdio transport carries it: one line of JSON."""
    body: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        body["params"] = dict(params)
    return json.dumps(body)


# --- the conversation of the official client ---------------------------------------------


@dataclass(slots=True)
class Live:
    """A stdio process the SDK client is talking to, and what the test asks of it."""

    client: ClientSession
    initialized: types.InitializeResult

    async def call(self, tool: str, args: Mapping[str, Any]) -> types.CallToolResult:
        """Send one tools/call and parse its result.

        ``ClientSession.call_tool`` is avoided on purpose: it issues a tools/list of its own
        and validates structuredContent on the client side, neither of which is part of what
        the service answered.
        """
        request = types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(name=tool, arguments=dict(args))
            )
        )
        return await self.client.send_request(request, types.CallToolResult)


@asynccontextmanager
async def client_over(tmp_path: Path, args: list[str]) -> AsyncIterator[Live]:
    """Spawn ``args`` through the official ``stdio_client`` and hand over a live session.

    The process is gone after the block: ``stdio_client`` closes its stdin, which is the end
    of file of requirement 22.4, and waits for it to end. Its stderr goes to a file below
    ``tmp_path`` rather than to the stderr of the test, so a test reads the notice of
    requirement 22.3 off it.
    """
    argv = wrapped(tmp_path / EXIT_CODE_FILE, args)
    params = StdioServerParameters(command=argv[0], args=argv[1:], cwd=str(tmp_path))
    with (tmp_path / STDERR_FILE).open("w", encoding="utf-8") as errlog:
        with anyio.fail_after(SESSION_TIMEOUT_S):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, client_info=CLIENT_INFO) as client:
                    initialized = await client.initialize()
                    yield Live(client=client, initialized=initialized)


def finished_client_run(tmp_path: Path, data_dir: Path) -> Finished:
    """What the process the SDK client spawned left behind, once it has ended.

    The exit code comes from the file the shell wrapper wrote (module docstring) and the
    stderr from the file ``stdio_client`` was given; stdout belongs to the client, which
    read it as MCP messages.
    """
    exit_file = tmp_path / EXIT_CODE_FILE
    assert exit_file.is_file(), "stdio 进程没有写出退出码，说明它没有正常结束"
    return Finished(
        code=int(exit_file.read_text(encoding="utf-8").strip()),
        stdout="",
        stderr=(tmp_path / STDERR_FILE).read_text(encoding="utf-8"),
        data_dir=data_dir,
    )


# --- one session over the real stdin and stdout (requirements 22.2, 22.3, 22.4) ----------


@pytest.mark.anyio
@pytest.mark.req("22.2", "22.3", "22.4")
async def test_the_official_client_serves_a_session_and_the_exit_writes_the_diff(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "var"
    args = stdio_args(data_dir, anchor_date=ANCHOR_DATE)

    async with client_over(tmp_path, args) as live:
        # The handshake went through the real stdin and stdout of the process, and the
        # server that answered it is this service (requirement 22.2).
        assert live.initialized.serverInfo.name == SERVER_NAME

        listed = await live.client.list_tools()
        assert BUSINESS_CONTEXT_GET in [tool.name for tool in listed.tools]

        result = await live.call(BUSINESS_CONTEXT_GET, {})
        assert result.isError is False, result

    finished = finished_client_run(tmp_path, data_dir)

    # stdin reached end of file, so the process wrote its exit and ended with 0
    # (requirement 22.4).
    assert finished.code == EXIT_OK

    # The sandbox id and the absolute path of the sandbox directory are on stderr, and that
    # directory holds the trace of the run (requirement 22.3).
    sandbox = finished.sandbox
    assert sandbox.name.startswith(SANDBOX_ID_PREFIX)
    assert (
        SANDBOX_NOTICE.format(sandbox_id=sandbox.name, directory=sandbox.resolve())
        in finished.stderr.splitlines()
    )
    assert (sandbox / TRACE_FILENAME).is_file()

    # The events of the session, in the order they happened: the initialize wrote
    # session_start, the two requests their own events, and the end of file wrote
    # session_end before the exit (requirements 17.9 and 22.4).
    events = finished.events()
    assert [event["event"] for event in events] == [
        EventName.SESSION_START,
        EventName.TOOLS_LIST,
        EventName.TOOLS_CALL,
        EventName.SESSION_END,
    ]
    assert {event["sandbox_id"] for event in events} == {sandbox.name}
    assert {event["instance"] for event in events} == {INSTANCE_REF}

    # And the diff of the run against the initial dataset, under the anchor date
    # ``--anchor-date`` asked for; the session only read, so nothing changed
    # (requirements 18.1 and 22.4).
    payload = finished.diff()
    assert list(payload) == DIFF_KEYS
    assert payload["sandbox_id"] == sandbox.name
    assert payload["instance"] == INSTANCE_REF
    assert payload["dataset"] == DATASET_REF
    assert payload["anchor_date"] == ANCHOR_DATE.isoformat()
    assert payload["base"] == INITIAL_BASE
    assert payload["changes"] == []


# --- what reaches stdout, and what does not (requirement 22.2) ---------------------------


@pytest.mark.req("22.2", "22.3")
def test_the_stdout_of_the_process_carries_mcp_messages_alone(tmp_path: Path) -> None:
    """Every line of stdout is a JSON-RPC response; the notices are on stderr.

    The run names its ``--profile`` and its ``--dataset``, which is the other side of
    requirement 14.12: a name the instance configuration does have is accepted, and the
    sandbox of the session is the one of those two names.
    """
    data_dir = tmp_path / "var"
    lines = (
        "\n".join(
            [
                jsonrpc_line(1, INITIALIZE, INITIALIZE_PARAMS),
                json.dumps({"jsonrpc": JSONRPC_VERSION, "method": INITIALIZED}),
                jsonrpc_line(2, TOOLS_LIST),
                jsonrpc_line(3, TOOLS_CALL, {"name": BUSINESS_CONTEXT_GET, "arguments": {}}),
            ]
        )
        + "\n"
    )
    args = stdio_args(data_dir, profile=PROFILE, dataset=DATASET)
    finished = run_until_exit(tmp_path, args, process_env(), data_dir, lines=lines)

    assert finished.code == EXIT_OK

    # Nothing but MCP messages: every line parses as a JSON-RPC response, the first of them
    # being the one to the initialize (requirement 22.2).
    messages = finished.stdout_messages()
    assert messages, f"stdio 进程没有在 stdout 上回应任何消息：\n{finished.stderr}"
    for message in messages:
        assert message["jsonrpc"] == JSONRPC_VERSION
        assert tuple(sorted(message)) == RESPONSE_KEYS
    assert messages[0]["id"] == 1
    assert messages[0]["result"]["serverInfo"]["name"] == SERVER_NAME

    # The process served the session and wrote its trace in the sandbox directory, so the
    # requests above were answered from a sandbox of this run (requirement 22.3), and that
    # sandbox is the profile and the dataset the two options named.
    events = finished.events()
    assert [event["event"] for event in events][0] == EventName.SESSION_START
    assert {event["profile"] for event in events} == {PROFILE_REF}
    assert {event["dataset"] for event in events} == {DATASET_REF}
    assert (finished.sandbox / DIFF_FILENAME).is_file()

    # What the process had to say about itself went to stderr and to nowhere else
    # (requirements 22.2 and 22.3).
    assert finished.sandbox.name in finished.stderr
    assert str(finished.sandbox.resolve()) in finished.stderr
    assert finished.sandbox.name not in finished.stdout
    assert str(finished.sandbox.resolve()) not in finished.stdout


# --- a profile or a dataset the instance does not have (requirement 14.12) ---------------


@pytest.mark.req("14.12")
@pytest.mark.parametrize("option", [PROFILE_OPTION, DATASET_OPTION])
def test_a_name_the_instance_does_not_have_is_a_usage_error_with_no_sandbox(
    tmp_path: Path, option: str
) -> None:
    data_dir = tmp_path / "var"
    args = stdio_args(data_dir)
    args += [option, UNKNOWN_NAME]
    finished = run_until_exit(tmp_path, args, process_env(), data_dir)

    # A usage error naming the option and its value, nothing on stdout
    # (requirements 14.12 and 22.8).
    assert finished.code == EXIT_USAGE
    assert finished.stdout == ""
    assert option in finished.stderr
    assert UNKNOWN_NAME in finished.stderr

    # The check happens after validation step 1 and before anything is built or created:
    # no sandbox, and the data directory was never written in (requirement 14.12).
    assert finished.sandboxes == ()
    assert data_dir.exists() is False


# --- a loading that failed (requirement 22.7) --------------------------------------------


@pytest.mark.req("22.7")
def test_a_validation_that_failed_writes_the_error_list_and_creates_no_sandbox(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "var"
    args = stdio_args(data_dir, packs_dir=tmp_path / "no-packs")
    finished = run_until_exit(tmp_path, args, process_env(), data_dir)

    # The error list of requirement 2.2 on stderr, exit code 1, and stdout stays empty
    # (requirement 22.7).
    assert finished.code == EXIT_FAILED
    assert finished.stdout == ""
    assert finished.stderr.startswith(STEP1_PREFIX)

    assert finished.sandboxes == ()
    assert data_dir.exists() is False
