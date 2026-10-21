"""The serve command as a deployer runs it: a real process, two real ports, real signals.

tests/integration/test_serve_inprocess.py starts the gateway on the event loop of the test
with every dependency replaced; this file starts the installed ``erp-mock-mcp serve`` as a
subprocess and asks of it what only a process can show (task 24.3):

- the five steps of :mod:`erp_mock.cli.serve` happen in the order the requirements fix, and
  each one's failure is its own exit code - 2 for a non-loopback ``--host`` without an MCP
  endpoint token, before anything is loaded (requirement 23.2), 1 for a validation that
  failed with no port bound and no token file written (requirement 22.7), 1 for a port that
  could not be bound;
- the Admin token: generated and written to ``<data-dir>/admin-token`` with mode 0600 and
  nothing but the token in it, its absolute path on stderr and its value nowhere
  (requirements 23.5 and 23.6), or taken from ``ERP_MOCK_ADMIN_TOKEN`` with no file written
  at all (requirement 23.4);
- the two ports are served over real sockets - the Admin API on the loopback address and
  behind the token of that file, the MCP endpoint on ``--host`` and ``--port`` and serving
  no Admin address - and a sandbox created through them is copied from the Snapshot the
  CLI's cache adapter built during loading (:class:`~erp_mock.cli.serve.CachedSnapshots`,
  requirement 22.1);
- SIGTERM ends the process with exit code 0, which is the signal a supervisor stops a
  service with and the one path an in-process test cannot take.

The process is the console script of ``[project.scripts]`` beside the interpreter running
the tests (:data:`COMMAND`) rather than ``python -m``: it is the command a deployer types,
shebang and entry point included, and :mod:`erp_mock.cli.main` has no ``__main__`` to run.
Its environment is built from nothing (:func:`process_env`): the few variables a process
needs plus whatever the test sets, so no ``ERP_MOCK_*`` of the machine the tests run on
reaches a service and nothing a test sets reaches the test process.

What the services run on: the engine fixture (:mod:`tests.helpers.fixtures`), whose clock is
in mode fixed - serve has no ``--anchor-date``, so a configuration in mode today would read
the current date (requirement 25.10) - and which loads and builds in well under a second,
where the example instance takes longer. ``--data-dir`` is always below ``tmp_path``, so
each service builds the Snapshot of the fixture dataset in a cache of its own and writes its
token file, its sandboxes and its trace where the test can read them and pytest removes
them.

Every wait is a poll against a deadline (:func:`wait_for`), and a poll that finds the
process gone fails the test with what it wrote; no test sleeps for a fixed duration, and the
whole file stays a few seconds.
"""

import os
import signal
import stat
import string
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
import pytest

from erp_mock.cli.options import ADMIN_TOKEN_VAR, EXIT_FAILED, EXIT_OK, EXIT_USAGE, MCP_TOKEN_VAR
from erp_mock.cli.serve import TOKEN_FILENAME, TOKEN_MODE
from erp_mock.gateway.serve import ADMIN_HOST, bind_socket
from erp_mock.seed.cache import SNAPSHOTS_DIR
from tests.helpers.fixtures import ANCHOR_DATE, DATASET, INSTANCE_DIR, INSTANCE_NAME, PACKS_DIR

# The installed command, beside the interpreter that runs the tests (module docstring).
COMMAND: Final = Path(sys.executable).parent / "erp-mock-mcp"

# The variables a subprocess is given beyond what a test sets: enough to run, and not one of
# the ``ERP_MOCK_*`` the environment of the test process may carry (requirement 23.9).
PASSED_THROUGH: Final = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")

# The deadlines. Starting a service loads the fixture and builds the Snapshot of its
# dataset, which takes well under a second; the stop is the five steps of the serve
# docstring. A test that reaches either of these has already failed.
START_TIMEOUT_S: Final = 20.0
STOP_TIMEOUT_S: Final = 15.0
RUN_TIMEOUT_S: Final = 30.0
POLL_SECONDS: Final = 0.05

# The one address the Admin port is ever on (requirement 19.1), which is also the loopback
# host every service of this file serves its MCP port on.
LOOPBACK: Final = ADMIN_HOST

# An address of TEST-NET-3 (RFC 5737): not an address of this machine and not a loopback
# address, so a ``--host`` requirement 23.2 refuses without an MCP endpoint token.
NON_LOOPBACK: Final = "203.0.113.1"

# The two addresses of the Admin API these tests use (requirements 19.9 and 19.3).
HEALTH_PATH: Final = "/admin/health"
SANDBOXES_PATH: Final = "/admin/sandboxes"

# What ``secrets.token_urlsafe(32)`` produces: 43 characters of the URL-safe base64
# alphabet, which is 256 bits of randomness over the characters requirement 23.5 allows.
TOKEN_LENGTH: Final = 43
TOKEN_CHARS: Final = frozenset(string.ascii_letters + string.digits + "-_")

# The two tokens a test configures: the Admin token of requirement 23.4, which leaves no
# token file behind, and the MCP endpoint token requirement 23.2 asks for.
GIVEN_ADMIN_TOKEN: Final = "given-admin-token-0123456789"
GIVEN_MCP_TOKEN: Final = "given-mcp-token-0123456789"

# What the fixture instance and its one initial dataset are called in the Admin API, which
# writes both as ``<名称>@<版本>`` (requirements 19.9 and 19.12).
VERSION: Final = "1.0.0"
INSTANCE_REF: Final = f"{INSTANCE_NAME}@{VERSION}"
DATASET_REF: Final = f"{DATASET}@{VERSION}"

# The directories of a service below ``--data-dir`` (design.md data model 2).
SANDBOXES_DIR: Final = "sandboxes"
DB_FILENAME: Final = "db.sqlite"

# The first error of a loading that failed, as the loader formats its list (req 2.2).
STEP1_PREFIX: Final = "[step 1]"


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
    """The Authorization header the Admin port checks every request against (req 19.2)."""
    return {"Authorization": f"Bearer {token}"}


def free_port() -> int:
    """A port of the loopback address that nothing held a moment ago.

    Bound the way serve binds its sockets and released again, so a test can name it in
    ``--port`` or ``--admin-port`` (:func:`~erp_mock.gateway.serve.bind_socket`).
    """
    with closing(bind_socket(LOOPBACK, 0)) as sock:
        return int(sock.getsockname()[1])


# --- running the command -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finished:
    """A serve run that has ended: its exit code and the two streams it wrote."""

    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class Service:
    """A serve process of this test, and what a test asks of it."""

    process: subprocess.Popen[bytes]
    data_dir: Path
    port: int
    admin_port: int
    out: Path
    err: Path

    @property
    def stdout(self) -> str:
        """What the process has written to stdout; requirement 22.1 puts nothing there."""
        return self.out.read_text(encoding="utf-8")

    @property
    def stderr(self) -> str:
        """What the process has written to stderr: its log (design.md 7.3)."""
        return self.err.read_text(encoding="utf-8")

    @property
    def token_file(self) -> Path:
        """``<data-dir>/admin-token``, whether or not it was written (requirement 23.5)."""
        return self.data_dir / TOKEN_FILENAME

    def admin_url(self, path: str) -> str:
        """The address ``path`` of the Admin API of this process (requirement 19.1)."""
        return f"http://{LOOPBACK}:{self.admin_port}{path}"

    def mcp_url(self, path: str) -> str:
        """The address ``path`` of the MCP port of this process (requirement 14.2)."""
        return f"http://{LOOPBACK}:{self.port}{path}"

    def health(self, token: str | None) -> httpx.Response | None:
        """``GET /admin/health`` with ``token``, None where the port did not answer yet."""
        headers = {} if token is None else bearer(token)
        try:
            return httpx.get(self.admin_url(HEALTH_PATH), headers=headers, timeout=2.0)
        except httpx.TransportError:
            return None

    def serving(self, token: str) -> bool:
        """Whether the Admin port answers ``token`` with 200 (requirement 19.9)."""
        response = self.health(token)
        return response is not None and response.status_code == httpx.codes.OK

    def terminate(self) -> Finished:
        """Send SIGTERM and return the exit code and the output of the finished process.

        The signal a supervisor stops a service with, which serve answers with the five
        steps of its stop and the exit code of a service that was asked to stop
        (requirement 22.1).
        """
        self.process.send_signal(signal.SIGTERM)
        code = self.process.wait(timeout=STOP_TIMEOUT_S)
        return Finished(code=code, stdout=self.stdout, stderr=self.stderr)


def serve_args(
    data_dir: Path,
    *,
    port: int,
    admin_port: int,
    host: str = LOOPBACK,
    packs_dir: Path = PACKS_DIR,
    instance_dir: Path = INSTANCE_DIR,
) -> list[str]:
    """The command line of one serve run over the engine fixture (requirement 22.1)."""
    return [
        str(COMMAND),
        "serve",
        "--instance",
        str(instance_dir),
        "--packs-dir",
        str(packs_dir),
        "--data-dir",
        str(data_dir),
        "--host",
        host,
        "--port",
        str(port),
        "--admin-port",
        str(admin_port),
    ]


def run_until_exit(tmp_path: Path, args: list[str], env: Mapping[str, str]) -> Finished:
    """Run ``args`` to completion and return its exit code and output.

    For the runs that end by themselves: a usage error, a validation that failed, a port
    that could not be bound. The working directory is ``tmp_path``, so a relative path this
    did not pass could not reach the repository.
    """
    completed = subprocess.run(
        args,
        cwd=tmp_path,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=RUN_TIMEOUT_S,
        check=False,
    )
    return Finished(
        code=completed.returncode,
        stdout=completed.stdout.decode("utf-8"),
        stderr=completed.stderr.decode("utf-8"),
    )


@contextmanager
def serve_process(
    tmp_path: Path,
    *,
    host: str = LOOPBACK,
    packs_dir: Path = PACKS_DIR,
    env: Mapping[str, str] | None = None,
) -> Iterator[Service]:
    """Start a serve process on two free ports and yield it; it is gone after the block.

    A test about the stop ends the process itself (:meth:`Service.terminate`); whatever is
    still running when the block ends is killed, so no test leaves a service behind. stdout
    and stderr go to files below ``tmp_path`` rather than to pipes: a test reads them while
    the process runs without having to drain a pipe to keep it going.
    """
    data_dir = tmp_path / "var"
    out, err = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    port, admin_port = free_port(), free_port()
    args = serve_args(data_dir, port=port, admin_port=admin_port, host=host, packs_dir=packs_dir)
    with out.open("wb") as out_file, err.open("wb") as err_file:
        process = subprocess.Popen(
            args,
            cwd=tmp_path,
            env=dict(process_env() if env is None else env),
            stdin=subprocess.DEVNULL,
            stdout=out_file,
            stderr=err_file,
        )
    try:
        yield Service(
            process=process,
            data_dir=data_dir,
            port=port,
            admin_port=admin_port,
            out=out,
            err=err,
        )
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
            pytest.fail(f"serve 进程在{what}之前以 {code} 退出：\n{service.stderr}")
        time.sleep(POLL_SECONDS)
    pytest.fail(f"等待{what}超时（{START_TIMEOUT_S} 秒）：\n{service.stderr}")


def wait_until_serving(service: Service, token: str) -> None:
    """Wait until the Admin port of ``service`` answers ``token`` with 200."""
    wait_for(service, lambda: service.serving(token), "两个端口开始监听")


def written_token(service: Service) -> str:
    """Wait until the token file is there and return the token in it (requirement 23.5)."""
    wait_for(service, service.token_file.is_file, "写出 Admin 令牌文件")
    return service.token_file.read_text(encoding="utf-8")


# --- the token file and the two ports (requirements 22.1, 23.5, 23.6) --------------------


@pytest.mark.req("22.1", "23.5", "23.6")
def test_a_serve_process_writes_the_token_file_and_serves_the_two_ports(tmp_path: Path) -> None:
    with serve_process(tmp_path) as service:
        token = written_token(service)

        # The file holds the token and nothing else, under 0600 (requirement 23.5).
        assert stat.S_IMODE(service.token_file.stat().st_mode) == TOKEN_MODE == 0o600
        assert "\n" not in token
        assert len(token) == TOKEN_LENGTH
        assert set(token) <= TOKEN_CHARS

        wait_until_serving(service, token)

        # The Admin API on its own port, and that token is what it accepts
        # (requirements 19.1, 19.2 and 19.9).
        health = service.health(token)
        assert health is not None
        assert health.status_code == httpx.codes.OK
        body = health.json()
        assert body["instance"] == INSTANCE_REF
        assert body["datasets"] == [DATASET_REF]

        anonymous = service.health(None)
        assert anonymous is not None
        assert anonymous.status_code == httpx.codes.UNAUTHORIZED

        # The MCP endpoint listens on --host and --port, and no Admin address is served
        # there (requirement 19.1).
        on_mcp = httpx.get(service.mcp_url(HEALTH_PATH), timeout=2.0)
        assert on_mcp.status_code == httpx.codes.NOT_FOUND

        # Loading built the Snapshot of the initial dataset below the data directory
        # (requirement 20.11), which is the cache the sandboxes of the service come from.
        assert (service.data_dir / SNAPSHOTS_DIR).is_dir()

        finished = service.terminate()

    # SIGTERM is a stop that was asked for (requirement 22.1).
    assert finished.code == EXIT_OK

    # Nothing on stdout, the absolute path of the token file on stderr, and the token itself
    # in neither (requirements 22.1 and 23.6).
    assert finished.stdout == ""
    assert str(service.token_file) in finished.stderr
    assert token not in finished.stderr
    assert token not in finished.stdout


@pytest.mark.req("22.1")
def test_a_sandbox_of_the_service_comes_from_the_injected_snapshot_cache(tmp_path: Path) -> None:
    """The Snapshot source of the service is the CLI's cache adapter (task 24.3)."""
    with serve_process(tmp_path) as service:
        token = written_token(service)
        wait_until_serving(service, token)

        created = httpx.post(
            service.admin_url(SANDBOXES_PATH),
            headers=bearer(token),
            json={},
            timeout=STOP_TIMEOUT_S,
        )
        assert created.status_code == httpx.codes.CREATED, created.text
        body = created.json()
        # The dataset and the anchor date of the configuration: the clock of the fixture is
        # in mode fixed and serve has no --anchor-date (requirement 16.1).
        assert body["dataset"] == DATASET_REF
        assert body["anchor_date"] == ANCHOR_DATE.isoformat()

        # The sandbox was copied from the Snapshot the cache holds for that dataset and
        # that anchor date, which is what :class:`CachedSnapshots` hands the gateway.
        sandbox_dir = service.data_dir / SANDBOXES_DIR / body["sandbox_id"]
        assert (sandbox_dir / DB_FILENAME).is_file()
        assert (service.data_dir / SNAPSHOTS_DIR / DATASET_REF).is_dir()

        finished = service.terminate()

    assert finished.code == EXIT_OK
    assert finished.stdout == ""


@pytest.mark.req("23.4", "23.6")
def test_an_admin_token_from_the_environment_is_used_and_writes_no_file(tmp_path: Path) -> None:
    env = process_env(**{ADMIN_TOKEN_VAR: GIVEN_ADMIN_TOKEN})
    with serve_process(tmp_path, env=env) as service:
        wait_until_serving(service, GIVEN_ADMIN_TOKEN)

        # That value is the Admin token of the process, and no token file was written
        # (requirement 23.4).
        assert service.token_file.exists() is False
        finished = service.terminate()

    assert finished.code == EXIT_OK
    assert finished.stdout == ""
    assert service.token_file.exists() is False
    # The variable is named on stderr, its value is nowhere (requirement 23.6).
    assert ADMIN_TOKEN_VAR in finished.stderr
    assert GIVEN_ADMIN_TOKEN not in finished.stderr
    assert GIVEN_ADMIN_TOKEN not in finished.stdout


# --- the host and the MCP endpoint token (requirement 23.2) ------------------------------


@pytest.mark.req("23.2")
def test_a_non_loopback_host_without_the_mcp_token_is_refused_before_loading(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "var"
    args = serve_args(data_dir, port=free_port(), admin_port=free_port(), host=NON_LOOPBACK)
    finished = run_until_exit(tmp_path, args, process_env())

    # A usage error naming the option, its value and the variable to configure
    # (requirements 22.8 and 23.2).
    assert finished.code == EXIT_USAGE
    assert finished.stdout == ""
    assert "--host" in finished.stderr
    assert NON_LOOPBACK in finished.stderr
    assert MCP_TOKEN_VAR in finished.stderr

    # Before the loading: the data directory was never created, so nothing was read, built
    # or written - no Snapshot, no token file, no sandbox.
    assert data_dir.exists() is False


@pytest.mark.req("23.2", "22.7")
def test_a_non_loopback_host_with_the_mcp_token_reaches_the_loading(tmp_path: Path) -> None:
    """The check of requirement 23.2 is about the token: with one, the host is accepted."""
    data_dir = tmp_path / "var"
    args = serve_args(
        data_dir,
        port=free_port(),
        admin_port=free_port(),
        host=NON_LOOPBACK,
        packs_dir=tmp_path / "no-packs",
    )
    finished = run_until_exit(tmp_path, args, process_env(**{MCP_TOKEN_VAR: GIVEN_MCP_TOKEN}))

    # Not the usage error of requirement 23.2 but the error list of the loading: the run got
    # past the host check and failed at validation step 1 (requirement 22.7).
    assert finished.code == EXIT_FAILED
    assert finished.stdout == ""
    assert finished.stderr.startswith(STEP1_PREFIX)
    assert "--host" not in finished.stderr
    assert GIVEN_MCP_TOKEN not in finished.stderr


# --- a start that could not happen (requirements 22.7 and 22.8) --------------------------


@pytest.mark.req("22.7", "23.5")
def test_a_validation_that_failed_binds_no_port_and_writes_no_token_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    port, admin_port = free_port(), free_port()
    args = serve_args(data_dir, port=port, admin_port=admin_port, packs_dir=tmp_path / "no-packs")
    finished = run_until_exit(tmp_path, args, process_env())

    # The error list of requirement 2.2 on stderr, exit code 1, nothing on stdout.
    assert finished.code == EXIT_FAILED
    assert finished.stdout == ""
    assert finished.stderr.startswith(STEP1_PREFIX)

    # No port was bound and no token file was written: the Admin token is settled after the
    # loading and before the sockets (requirements 22.7 and 23.5).
    assert data_dir.exists() is False
    for taken in (port, admin_port):
        with closing(bind_socket(LOOPBACK, taken)) as sock:
            assert int(sock.getsockname()[1]) == taken


@pytest.mark.req("22.1", "23.5")
def test_a_port_that_is_taken_ends_the_process_with_the_failure_code(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    with closing(bind_socket(LOOPBACK, 0)) as held:
        held.listen(1)
        port = int(held.getsockname()[1])
        args = serve_args(data_dir, port=port, admin_port=free_port())
        finished = run_until_exit(tmp_path, args, process_env())

    # The bind of the MCP socket failed, which is the 1 of a service that could not start
    # (requirement 22.8); the port it could not have is named on stderr.
    assert finished.code == EXIT_FAILED
    assert finished.stdout == ""
    assert str(port) in finished.stderr

    # The token file was written before the sockets were bound, which is the order
    # requirement 23.5 asks for, and its content is in no message (requirement 23.6).
    token_file = data_dir / TOKEN_FILENAME
    assert token_file.is_file()
    assert token_file.read_text(encoding="utf-8") not in finished.stderr


@pytest.mark.req("22.1")
def test_a_port_outside_the_range_is_a_usage_error(tmp_path: Path) -> None:
    """``--port`` is a TCP port number; anything else is the 2 of requirement 22.8."""
    data_dir = tmp_path / "var"
    args = serve_args(data_dir, port=free_port(), admin_port=free_port())
    args[args.index("--port") + 1] = "65536"
    finished = run_until_exit(tmp_path, args, process_env())

    assert finished.code == EXIT_USAGE
    assert finished.stdout == ""
    assert "--port" in finished.stderr
    assert data_dir.exists() is False
