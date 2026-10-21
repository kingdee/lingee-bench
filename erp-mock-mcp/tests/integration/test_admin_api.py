"""The Admin API of a real serve process: the 13 interfaces and the 9 status codes (25.6).

tests/unit/gateway/test_admin_lifecycle.py and tests/unit/gateway/test_admin_state.py speak
to :mod:`erp_mock.gateway.admin` through an ASGI transport, with the Snapshot source, the
clock and the initial dataset of the loaded instance replaced; this file asks the same
questions of the thing a deployer runs (task 26.2): the installed ``erp-mock-mcp serve``
command, its Admin port on a real socket, its token read from ``<data-dir>/admin-token``,
its sandboxes on disk and its Snapshots built by the cache of the CLI. What that adds over
the unit tests is everything between the request and the handler - uvicorn, the middleware
chain of requirement 23.11, the thread pool a read runs in - and everything behind it: a
Snapshot that is really built, a trace file that is really written, a zip that really
arrives over a socket.

What requirement 25.6 asks for, and which test answers for it:

=============================  ==========================================================
what                           where
=============================  ==========================================================
the 13 interfaces of           ``..._the_thirteen_interfaces_...``, a table of the 13
requirement 19.1, one          pairs in the order of that requirement, run over one
success case each              sandbox from its prebuild to its delete; the tests after it
                               say what each answer holds (requirements 19.3 to 19.14)
201                            the create of that table, and ``..._a_prebuilt_sandbox_...``
                               for the five fields of requirement 19.3
204                            the delete of that table, and
                               ``..._freezing_and_then_deleting_...``
400                            ``..._does_not_fit_the_model_...`` for a body and
                               ``..._query_parameter_...`` for a parameter (19.17, 19.20)
401                            ``..._admin_token_does_not_match_...`` for seven header
                               values and ``..._path_of_no_interface_without_the_token_...``
                               for a path that is no interface (requirement 19.2)
404                            ``..._method_path_or_name_of_no_interface_...`` and
                               ``..._a_name_the_sandbox_does_not_have_...`` (req 19.15)
409                            ``..._a_frozen_sandbox_and_a_name_that_is_taken_...``
                               (requirement 19.16)
413                            ``..._a_body_over_the_limit_...`` (requirement 23.7)
503                            ``..._the_concurrency_limit_...`` (requirement 19.18)
500                            ``..._a_snapshot_that_cannot_be_built_...`` (req 19.19)
=============================  ==========================================================

The three processes, and why there are three. Most tests share one (:func:`service`): a
serve process over the engine fixture (:mod:`tests.helpers.fixtures`), whose clock is in
mode fixed - serve has no ``--anchor-date``, so a configuration in mode today would read
the current date (requirement 25.10) - and which loads and builds its one Snapshot in a
couple of seconds. Every one of those tests creates the sandboxes it needs through the API
and leaves the service otherwise as it found it, so none of them depends on another's
sandboxes. The two that need another environment and the one that needs another instance
configuration get a process of their own:

- :func:`limited_service`: ``ERP_MOCK_MAX_BODY_BYTES`` of :data:`SMALL_BODY_LIMIT` and
  ``ERP_MOCK_MAX_SANDBOXES`` of 1, which is what makes the 413 of requirement 23.7 and the
  503 of requirement 19.18 reachable without sending 256 KB or prebuilding 256 sandboxes;
- :func:`fragile_service`: the instance configuration of :func:`write_fragile_instance`,
  whose one dataset holds a date expression that is in the calendar at the anchor date the
  service starts with and outside it at another - ``year(-2026).start`` is the year 1 at
  2027-06-15 and the year 0 at 2026-06-15, which no date is. The build therefore succeeds
  during loading, so the process starts, and fails for the ``anchor_date`` the Admin
  request names, which is the 500 of requirement 19.19. A dataset that cannot be built at
  all would be refused by validation step 8 and the process would exit with 1 before it
  ever listened (requirement 22.7), which is why the failure has to depend on the date.

How a process is run, as in tests/integration/test_cli_serve.py: the console script beside
the interpreter of the tests (:data:`COMMAND`), an environment built from nothing
(:func:`process_env`) so no ``ERP_MOCK_*`` of the machine reaches a service, stdout and
stderr redirected to files below ``tmp_path``, every wait a poll against a deadline
(:func:`wait_for`), and SIGTERM to stop. ``--data-dir`` is below ``tmp_path``, so each
service builds its Snapshot in a cache of its own and writes its token file, its sandboxes
and its traces where the test can read them and pytest removes them.

No MCP session is opened here. The sandboxes of this file are prebuilt ones
(requirement 19.3) and the events of their traces are the ones the Admin API writes itself -
``sandbox_reset`` and ``snapshot_restore`` with a ``session_id`` of null (requirement 17.2);
what a client's calls do to a sandbox, its trace and its diff is the protocol flow of
task 26.1.
"""

import json
import os
import signal
import string
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from erp_mock import FRAMEWORK_VERSION, META_MODEL_VERSION
from erp_mock.cli.options import MAX_BODY_BYTES_VAR, MAX_SANDBOXES_VAR
from erp_mock.cli.serve import TOKEN_FILENAME
from erp_mock.gateway.admin import ALIASES_FILENAME, DIFF_FILENAME, AdminError
from erp_mock.gateway.middleware import PAYLOAD_TOO_LARGE, UNAUTHORIZED
from erp_mock.gateway.router import sandbox_path
from erp_mock.gateway.serve import ADMIN_HOST, bind_socket
from erp_mock.gateway.trace import COMMON_KEYS, TRACE_FILENAME, EventName
from erp_mock.seed.cache import SNAPSHOTS_DIR
from erp_mock.state.diff import INITIAL_BASE
from tests.helpers.fixtures import (
    ANCHOR_DATE,
    DATASET,
    INSTANCE_DIR,
    INSTANCE_NAME,
    PACK_NAMES,
    PACKS_DIR,
    PROFILE,
)
from tests.helpers.tree import (
    CONTEXT_FILES,
    dataset_doc,
    entity_doc,
    profile_doc,
    write_instance,
    write_pack,
)

# Every test of this file is the Admin API test of requirement 25.6; each one also names
# the items of requirement 19 or 23 it is about.
pytestmark = pytest.mark.req("25.6")

# The installed command, beside the interpreter that runs the tests: the command a deployer
# types, shebang and entry point included (tests/integration/test_cli_serve.py).
COMMAND: Final = Path(sys.executable).parent / "erp-mock-mcp"

# The variables a subprocess is given beyond what a test sets: enough to run, and not one
# of the ``ERP_MOCK_*`` the environment of the test process may carry (requirement 23.9).
PASSED_THROUGH: Final = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")

# The deadlines. Starting a service loads a configuration and builds the Snapshot of its
# one dataset, which takes a few seconds at most; a test that reaches either of these has
# already failed.
START_TIMEOUT_S: Final = 30.0
STOP_TIMEOUT_S: Final = 15.0
REQUEST_TIMEOUT_S: Final = 20.0
POLL_SECONDS: Final = 0.05

# The one address the Admin port is ever on (requirement 19.1), which is also where the MCP
# port of every service of this file listens.
LOOPBACK: Final = ADMIN_HOST

# The addresses of the Admin API (requirement 19.1 and design.md 7.7).
HEALTH: Final = "/admin/health"
SANDBOXES: Final = "/admin/sandboxes"
EXPORTS: Final = "/admin/exports"
ALIASES: Final = f"/admin/datasets/{DATASET}/aliases"

# What the fixture instance, its profile and its one initial dataset are called in the
# Admin API, which writes each as ``<名称>@<版本>`` (requirements 19.9 and 19.12).
VERSION: Final = "1.0.0"
INSTANCE_REF: Final = f"{INSTANCE_NAME}@{VERSION}"
PROFILE_REF: Final = f"{PROFILE}@{VERSION}"
DATASET_REF: Final = f"{DATASET}@{VERSION}"

# The keys of each answer, in the order the requirements fix them.
HEALTH_KEYS: Final = (
    "framework_version",
    "meta_model_version",
    "instance",
    "packs",
    "profiles",
    "datasets",
)
CREATED_KEYS: Final = ("sandbox_id", "mcp_url", "profile", "dataset", "anchor_date")
METADATA_KEYS: Final = (
    "sandbox_id",
    "session_ids",
    "profile",
    "dataset",
    "anchor_date",
    "status",
    "created_at",
)
SAVED_KEYS: Final = ("sandbox_id", "name")
DIFF_KEYS: Final = ("sandbox_id", "instance", "dataset", "anchor_date", "base", "changes")
ALIASES_KEYS: Final = ("dataset", "aliases")
ERROR_KEYS: Final = ("error", "message")

# The media types of the two answers that are not JSON objects (requirements 19.7, 19.14).
JSON_MEDIA_TYPE: Final = "application/json"
NDJSON_MEDIA_TYPE: Final = "application/x-ndjson"
ZIP_MEDIA_TYPE: Final = "application/zip"

# Where the directories of the sandboxes of a process are, below ``--data-dir``: what a
# delete has to leave nothing of (design.md data model 2, requirement 19.13). The two files
# of one sandbox in an export are :data:`TRACE_FILENAME` and :data:`DIFF_FILENAME`.
SANDBOXES_DIR: Final = "sandboxes"

# A sandbox id of the right shape that no service of this file has, and a Named_Snapshot
# name no sandbox of this file saves (requirement 19.15).
MISSING_ID: Final = "sb_0000000000000000"
MISSING_NAME: Final = "never-saved"

# The Named_Snapshot names the tests save under (requirement 19.5).
SAVED: Final = "after_prebuild"
OTHER_SAVED: Final = "second-point"

# A token that is not the one of any service here, and the authentication scheme the Admin
# port accepts (requirement 19.2).
WRONG_TOKEN: Final = "not-the-admin-token"
BEARER: Final = "Bearer"

# What ``secrets.token_urlsafe(32)`` produces, which is what the token file holds
# (requirement 23.5).
TOKEN_CHARS: Final = frozenset(string.ascii_letters + string.digits + "-_")

# The environment of :func:`limited_service`: a body limit that the bodies of this file are
# well under and a request of a kilobyte is over, and room for one sandbox at a time.
SMALL_BODY_LIMIT: Final = 512
ONE_SANDBOX: Final = 1

# The instance configuration of :func:`fragile_service` (module docstring): its name, its
# one dataset, the date expression whose value depends on the anchor date, the date the
# service starts at - where that expression is the year 1 - and the date an Admin request
# names, where it would be the year 0.
FRAGILE_INSTANCE: Final = "fragile-demo"
FRAGILE_DATASET: Final = "fragile-basic"
FRAGILE_DATASET_FILE: Final = f"datasets/{FRAGILE_DATASET}.yaml"
FRAGILE_EXPR: Final = "year(-2026).start"
FRAGILE_ANCHOR: Final = "2027-06-15"
BREAKING_ANCHOR: Final = "2026-06-15"

# The one pack of that instance and the entity whose date field carries the expression; the
# ``sup`` prefix and the two context entities are in the default manifest of
# :mod:`tests.helpers.tree`.
FRAGILE_PACK: Final = "core"
FRAGILE_ENTITY: Final = "supplier"
FRAGILE_FIELD: Final = "signed_on"

# The current user of that dataset: the internal id its one employee record gets, the
# records being created in the order they are written (requirement 20.17).
FRAGILE_USER: Final = "emp_000001"


# --- one serve process, and what a test asks of it ---------------------------------------


@dataclass(frozen=True, slots=True)
class Service:
    """A serve process of this file, its Admin port and the token that opens it.

    ``client`` carries the Authorization header of :attr:`token` on every request
    (requirement 19.2) and is addressed with the paths of the API; the tests that are about
    a token that does not match send their requests without it (:func:`raw`).
    """

    process: subprocess.Popen[bytes]
    data_dir: Path
    admin_port: int
    token: str
    client: httpx.Client
    err: Path

    @property
    def stderr(self) -> str:
        """What the process has written to stderr: its log (design.md 7.3)."""
        return self.err.read_text(encoding="utf-8")

    def url(self, path: str) -> str:
        """The address ``path`` of the Admin API of this process (requirement 19.1)."""
        return f"http://{LOOPBACK}:{self.admin_port}{path}"

    # --- the 13 interfaces, as a test calls them ---------------------------------------

    def health(self) -> httpx.Response:
        """GET /admin/health (requirement 19.9)."""
        return self.client.get(HEALTH)

    def aliases(self, name: str = DATASET) -> httpx.Response:
        """GET /admin/datasets/{name}/aliases (requirement 19.10)."""
        return self.client.get(f"/admin/datasets/{name}/aliases")

    def create(self, body: Any | None = None) -> httpx.Response:
        """POST /admin/sandboxes with ``body``, or with no body at all (req 19.3)."""
        return self.post(SANDBOXES, body)

    def listing(self, **query: str) -> httpx.Response:
        """GET /admin/sandboxes with ``query`` as its query string (requirement 19.11)."""
        return self.client.get(SANDBOXES, params=query)

    def metadata(self, sandbox_id: str) -> httpx.Response:
        """GET /admin/sandboxes/{id} (requirement 19.12)."""
        return self.client.get(f"{SANDBOXES}/{sandbox_id}")

    def reset(self, sandbox_id: str, body: Any | None = None) -> httpx.Response:
        """POST /admin/sandboxes/{id}/reset (requirement 19.4)."""
        return self.post(f"{SANDBOXES}/{sandbox_id}/reset", body)

    def save(self, sandbox_id: str, name: Any = SAVED) -> httpx.Response:
        """POST /admin/sandboxes/{id}/snapshots (requirement 19.5)."""
        return self.post(f"{SANDBOXES}/{sandbox_id}/snapshots", {"name": name})

    def restore(self, sandbox_id: str, name: Any = SAVED) -> httpx.Response:
        """POST /admin/sandboxes/{id}/restore (requirement 19.6)."""
        return self.post(f"{SANDBOXES}/{sandbox_id}/restore", {"name": name})

    def trace(self, sandbox_id: str) -> httpx.Response:
        """GET /admin/sandboxes/{id}/trace (requirement 19.7)."""
        return self.client.get(f"{SANDBOXES}/{sandbox_id}/trace")

    def diff(self, sandbox_id: str, **query: str) -> httpx.Response:
        """GET /admin/sandboxes/{id}/diff (requirement 19.8)."""
        return self.client.get(f"{SANDBOXES}/{sandbox_id}/diff", params=query)

    def freeze(self, sandbox_id: str, body: Any | None = None) -> httpx.Response:
        """POST /admin/sandboxes/{id}/freeze (requirement 19.13)."""
        return self.post(f"{SANDBOXES}/{sandbox_id}/freeze", body)

    def remove(self, sandbox_id: str) -> httpx.Response:
        """DELETE /admin/sandboxes/{id} (requirement 19.13)."""
        return self.client.delete(f"{SANDBOXES}/{sandbox_id}")

    def exports(self, body: Any | None = None) -> httpx.Response:
        """POST /admin/exports (requirement 19.14)."""
        return self.post(EXPORTS, body)

    # --- what the calls above are built from -------------------------------------------

    def post(self, path: str, body: Any | None = None) -> httpx.Response:
        """POST ``path`` with ``body`` as JSON, or with no body at all for None."""
        if body is None:
            return self.client.post(path)
        content = body if isinstance(body, str) else json.dumps(body)
        return self.client.post(
            path, content=content.encode(), headers={"content-type": JSON_MEDIA_TYPE}
        )

    def prebuilt(self, body: Any | None = None) -> str:
        """Prebuild one sandbox through the API and return its id (requirement 19.3)."""
        response = self.create(body)
        assert response.status_code == httpx.codes.CREATED, response.text
        sandbox_id = response.json()["sandbox_id"]
        assert isinstance(sandbox_id, str)
        return sandbox_id

    def sandbox_ids(self) -> list[str]:
        """The ids of the sandboxes the service has, in the order it lists them."""
        body = assert_json(self.listing(), httpx.codes.OK)
        return [str(item["sandbox_id"]) for item in body["items"]]

    def created_at(self, sandbox_id: str) -> str:
        """The ``created_at`` of ``sandbox_id``, as the metadata interface writes it."""
        return str(assert_json(self.metadata(sandbox_id), httpx.codes.OK)["created_at"])

    def terminate(self) -> int:
        """Send SIGTERM and return the exit code of the finished process (req 22.1)."""
        self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=STOP_TIMEOUT_S)


def process_env(**extra: str) -> dict[str, str]:
    """The environment of one serve process: those of :data:`PASSED_THROUGH` and ``extra``."""
    inherited = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    return {**inherited, **extra}


def bearer(token: str) -> dict[str, str]:
    """The Authorization header the Admin port checks every request against (19.2)."""
    return {"Authorization": f"{BEARER} {token}"}


def free_port() -> int:
    """A port of the loopback address that nothing held a moment ago."""
    with closing(bind_socket(LOOPBACK, 0)) as sock:
        return int(sock.getsockname()[1])


def raw(service: Service, method: str, path: str, header: str | None = None) -> httpx.Response:
    """One request to the Admin port outside :attr:`Service.client` and its token.

    ``header`` is the whole value of the Authorization header, None sending no such header
    at all, which is what the 401 of requirement 19.2 is about.
    """
    headers = {} if header is None else {"Authorization": header}
    return httpx.request(method, service.url(path), headers=headers, timeout=REQUEST_TIMEOUT_S)


def serve_args(
    data_dir: Path, *, port: int, admin_port: int, instance_dir: Path, packs_dir: Path
) -> list[str]:
    """The command line of one serve run over ``instance_dir`` (requirement 22.1)."""
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
        LOOPBACK,
        "--port",
        str(port),
        "--admin-port",
        str(admin_port),
    ]


@contextmanager
def serve_process(
    tmp_path: Path,
    *,
    instance_dir: Path = INSTANCE_DIR,
    packs_dir: Path = PACKS_DIR,
    env: Mapping[str, str] | None = None,
) -> Iterator[Service]:
    """Start a serve process on two free ports, wait until it answers, and yield it.

    The token of the process is read from ``<data-dir>/admin-token``, which is where serve
    writes the one it generated (requirement 23.5), and the client of the yielded service
    carries it. The process is stopped with SIGTERM when the block ends, and killed where
    it does not go, so no test leaves a service behind. stdout and stderr go to files below
    ``tmp_path`` rather than to pipes: a test reads them while the process runs without
    having to drain a pipe to keep it going.
    """
    data_dir = tmp_path / "var"
    out, err = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    port, admin_port = free_port(), free_port()
    args = serve_args(
        data_dir,
        port=port,
        admin_port=admin_port,
        instance_dir=instance_dir,
        packs_dir=packs_dir,
    )
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
        token = wait_for_token(process, data_dir / TOKEN_FILENAME, err)
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{admin_port}",
            headers=bearer(token),
            timeout=REQUEST_TIMEOUT_S,
        ) as client:
            service = Service(
                process=process,
                data_dir=data_dir,
                admin_port=admin_port,
                token=token,
                client=client,
                err=err,
            )
            wait_until_serving(service)
            yield service
            assert service.terminate() == 0, f"serve 进程未以 0 退出：\n{service.stderr}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=STOP_TIMEOUT_S)


def wait_for(
    process: subprocess.Popen[bytes], err: Path, condition: Callable[[], bool], what: str
) -> None:
    """Poll ``condition`` until it holds, failing the test where it does not in time.

    A process that ended while the wait was on fails the test at once, with what it wrote
    on stderr: a service that could not start has already said why.
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


def wait_for_token(process: subprocess.Popen[bytes], token_file: Path, err: Path) -> str:
    """The token of ``<data-dir>/admin-token`` once serve has written it (req 23.5)."""
    wait_for(process, err, token_file.is_file, "写出 Admin 令牌文件")
    token = token_file.read_text(encoding="utf-8")
    assert token and set(token) <= TOKEN_CHARS, "令牌文件只含令牌本身"
    return token


def wait_until_serving(service: Service) -> None:
    """Wait until the Admin port answers the token of the service with 200 (19.9)."""

    def answered() -> bool:
        try:
            return service.health().status_code == httpx.codes.OK
        except httpx.TransportError:
            return False

    wait_for(service.process, service.err, answered, "Admin 端口开始监听")


# --- the three services of this file (module docstring) ----------------------------------


@pytest.fixture(scope="module", autouse=True)
def installed_command() -> None:
    """Every test here runs the console script; a run without it would be meaningless."""
    assert COMMAND.is_file(), f"没有找到安装好的命令：{COMMAND}"
    assert os.access(COMMAND, os.X_OK)


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    """One serve process over the engine fixture, shared by most tests of this file."""
    with serve_process(tmp_path_factory.mktemp("admin-api")) as started:
        yield started


@pytest.fixture(scope="module")
def limited_service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    """A serve process whose body limit and sandbox limit are small (req 23.9)."""
    env = process_env(
        **{MAX_BODY_BYTES_VAR: str(SMALL_BODY_LIMIT), MAX_SANDBOXES_VAR: str(ONE_SANDBOX)}
    )
    with serve_process(tmp_path_factory.mktemp("admin-limits"), env=env) as started:
        yield started


@pytest.fixture(scope="module")
def fragile_service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    """A serve process whose dataset builds at its own anchor date and at no other."""
    root = tmp_path_factory.mktemp("admin-fragile")
    instance_dir, packs_dir = write_fragile_instance(root)
    with serve_process(root, instance_dir=instance_dir, packs_dir=packs_dir) as started:
        yield started


def write_fragile_instance(root: Path) -> tuple[Path, Path]:
    """Write the instance configuration of :func:`fragile_service`; return its directories.

    One pack with the two context entities of :data:`~tests.helpers.tree.CONTEXT_FILES` and
    an entity with a date field, and one dataset of three records: the default
    organization, the employee that is its current user, and the record whose date field
    holds :data:`FRAGILE_EXPR`. The clock is in mode fixed at :data:`FRAGILE_ANCHOR`
    (requirement 16.1), where that expression is the first day of the year 1; at
    :data:`BREAKING_ANCHOR` it would be the year 0, which is outside the calendar, and the
    build fails there and nowhere else (module docstring).
    """
    records = (
        "{ 'org_unit:main': { kind: org_unit, name: 甲组织, is_default: true },"
        " 'employee:clerk': { kind: employee, name: 甲员工 },"
        f" '{FRAGILE_ENTITY}:fragile': {{ kind: {FRAGILE_ENTITY}, name: 甲供应商,"
        f" {FRAGILE_FIELD}: '{FRAGILE_EXPR}' }} }}"
    )
    instance_dir = write_instance(
        root / "instance",
        {
            "profiles/semantic.yaml": profile_doc(),
            FRAGILE_DATASET_FILE: dataset_doc(
                dataset=FRAGILE_DATASET, current_user=FRAGILE_USER, entities=records
            ),
        },
        instance=FRAGILE_INSTANCE,
        datasets=f"[{FRAGILE_DATASET}]",
        default_dataset=FRAGILE_DATASET,
        clock=f"{{ mode: fixed, anchor_date: {FRAGILE_ANCHOR}, timezone: UTC }}",
    )
    packs_dir = root / "packs"
    write_pack(
        packs_dir,
        FRAGILE_PACK,
        files={
            **CONTEXT_FILES,
            f"entities/{FRAGILE_ENTITY}.yaml": entity_doc(
                fields=f"{{ name: {{ type: string }}, {FRAGILE_FIELD}: {{ type: date }} }}"
            ),
        },
    )
    return instance_dir, packs_dir


# --- what an answer looks like -----------------------------------------------------------


def assert_json(response: httpx.Response, status: int) -> Any:
    """Assert ``response`` is JSON of ``status`` and return its parsed body."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == JSON_MEDIA_TYPE
    return response.json()


def assert_error(response: httpx.Response, status: int, code: str) -> str:
    """Assert ``response`` is the error object of ``status`` and return its message.

    The shape is design.md 7.7's and is the same whatever went wrong - the refusals of the
    middleware chain included: ``error`` with a short code and ``message`` with a Chinese
    explanation, in that order and nothing else.
    """
    body = assert_json(response, status)
    assert tuple(body) == ERROR_KEYS
    assert body["error"] == code
    message = body["message"]
    assert isinstance(message, str) and message
    return message


def assert_bytes(response: httpx.Response, media_type: str) -> bytes:
    """Assert ``response`` is 200 of ``media_type`` and return its body as it arrived."""
    assert response.status_code == httpx.codes.OK, response.text
    assert response.headers["content-type"] == media_type
    return response.content


def events(payload: bytes) -> list[dict[str, Any]]:
    """The events of a trace answer, one per line (requirement 19.7)."""
    return [json.loads(line) for line in payload.splitlines()]


def entries(payload: bytes) -> dict[str, bytes]:
    """The entries of a zip, by name, in the order the archive lists them (19.14)."""
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# --- the 13 interfaces of requirement 19.1 -----------------------------------------------


@dataclass(frozen=True, slots=True)
class Served:
    """One interface of requirement 19.1 and the success it answers with."""

    method: str
    path: str
    status: int
    body: Any | None = None


@pytest.mark.req("19.1")
def test_the_thirteen_interfaces_each_answer_their_own_success_status(service: Service) -> None:
    sandbox_id = service.prebuilt()
    served = (
        Served("GET", HEALTH, httpx.codes.OK),
        Served("GET", ALIASES, httpx.codes.OK),
        Served("POST", SANDBOXES, httpx.codes.CREATED),
        Served("GET", SANDBOXES, httpx.codes.OK),
        Served("GET", f"{SANDBOXES}/{sandbox_id}", httpx.codes.OK),
        Served("POST", f"{SANDBOXES}/{sandbox_id}/reset", httpx.codes.OK),
        Served(
            "POST",
            f"{SANDBOXES}/{sandbox_id}/snapshots",
            httpx.codes.CREATED,
            {"name": SAVED},
        ),
        Served("POST", f"{SANDBOXES}/{sandbox_id}/restore", httpx.codes.OK, {"name": SAVED}),
        Served("GET", f"{SANDBOXES}/{sandbox_id}/trace", httpx.codes.OK),
        Served("GET", f"{SANDBOXES}/{sandbox_id}/diff", httpx.codes.OK),
        Served("POST", f"{SANDBOXES}/{sandbox_id}/freeze", httpx.codes.OK),
        Served("DELETE", f"{SANDBOXES}/{sandbox_id}", httpx.codes.NO_CONTENT),
        Served("POST", EXPORTS, httpx.codes.OK),
    )

    # The 13 interfaces of requirement 19.1, each answering its own status over a real
    # socket; the order is the one a sandbox allows - prebuilt, worked on, frozen, deleted.
    assert len(served) == 13
    for call in served:
        if call.method == "POST":
            response = service.post(call.path, call.body)
        else:
            response = service.client.request(call.method, call.path)
        assert response.status_code == call.status, f"{call.method} {call.path}: {response.text}"


@pytest.mark.req("19.9")
def test_health_reports_the_versions_of_what_the_service_loaded(service: Service) -> None:
    body = assert_json(service.health(), httpx.codes.OK)

    # The six keys of requirement 19.9, in its order: the two engine versions, the instance
    # as <name>@<version>, and one <name>@<version> per pack, profile and initial dataset,
    # each in the order instance.yaml lists them.
    assert tuple(body) == HEALTH_KEYS
    assert body["framework_version"] == FRAMEWORK_VERSION
    assert body["meta_model_version"] == META_MODEL_VERSION
    assert body["instance"] == INSTANCE_REF
    assert [ref.split("@")[0] for ref in body["packs"]] == list(PACK_NAMES)
    assert body["profiles"] == [PROFILE_REF]
    assert body["datasets"] == [DATASET_REF]


@pytest.mark.req("19.10")
def test_the_alias_map_is_the_file_the_service_built_during_loading(service: Service) -> None:
    body = assert_json(service.aliases(), httpx.codes.OK)

    # The two keys of requirement 19.10, the aliases sorted by code point, and the content
    # of the alias map file the cache of this service holds for the dataset at the anchor
    # date its clock configuration fixes (requirement 20.10).
    assert tuple(body) == ALIASES_KEYS
    assert body["dataset"] == DATASET_REF
    assert list(body["aliases"]) == sorted(body["aliases"])
    entry = service.data_dir / SNAPSHOTS_DIR / DATASET_REF / ANCHOR_DATE.isoformat()
    assert body == json.loads((entry / ALIASES_FILENAME).read_text(encoding="utf-8"))


@pytest.mark.req("19.3", "19.11", "19.12")
def test_a_prebuilt_sandbox_is_listed_with_the_fields_of_the_requirement(
    service: Service,
) -> None:
    created = assert_json(service.create(), httpx.codes.CREATED)

    # The five keys of requirement 19.3, in its order: the defaults of the instance
    # configuration decide the profile and the dataset, its clock the anchor date
    # (requirement 16.1), and mcp_url is the address of that sandbox.
    assert tuple(created) == CREATED_KEYS
    sandbox_id = created["sandbox_id"]
    assert created["mcp_url"] == sandbox_path(sandbox_id)
    assert created["profile"] == PROFILE_REF
    assert created["dataset"] == DATASET_REF
    assert created["anchor_date"] == ANCHOR_DATE.isoformat()

    # The seven keys of requirement 19.12, in its order: a prebuilt sandbox is active and
    # bound to no session, and its database is a copy of the Snapshot of its dataset
    # (requirement 15.1).
    metadata = assert_json(service.metadata(sandbox_id), httpx.codes.OK)
    assert tuple(metadata) == METADATA_KEYS
    assert metadata["sandbox_id"] == sandbox_id
    assert metadata["session_ids"] == []
    assert metadata["status"] == "active"
    assert metadata["anchor_date"] == ANCHOR_DATE.isoformat()

    # The listing holds items and nothing else, each item what the metadata interface
    # answers for that sandbox, and the one just created is among them (requirement 19.11).
    listed = assert_json(service.listing(), httpx.codes.OK)
    assert tuple(listed) == ("items",)
    assert metadata in listed["items"]
    # Every parameter that was given has to be satisfied: the status and the dataset of
    # this sandbox select it, a status no sandbox of it has does not.
    assert sandbox_id in service.sandbox_ids()
    narrowed = assert_json(
        service.listing(status="active", dataset=DATASET, created_from=metadata["created_at"]),
        httpx.codes.OK,
    )
    assert sandbox_id in [item["sandbox_id"] for item in narrowed["items"]]
    frozen = assert_json(service.listing(status="frozen"), httpx.codes.OK)
    assert sandbox_id not in [item["sandbox_id"] for item in frozen["items"]]


@pytest.mark.req("19.4", "19.5", "19.6", "19.7")
def test_a_reset_and_a_restore_append_their_events_to_the_trace(service: Service) -> None:
    sandbox_id = service.prebuilt()
    # A prebuilt sandbox nobody has connected to has no events at all (requirement 19.7).
    assert assert_bytes(service.trace(sandbox_id), NDJSON_MEDIA_TYPE) == b""

    reset = assert_json(service.reset(sandbox_id), httpx.codes.OK)
    saved = assert_json(service.save(sandbox_id), httpx.codes.CREATED)
    restored = assert_json(service.restore(sandbox_id), httpx.codes.OK)

    # The reset and the restore each answer with the object the metadata interface answers
    # with, and the Named_Snapshot with its sandbox and its name (requirements 19.4 to 19.6).
    assert tuple(reset) == METADATA_KEYS
    assert tuple(restored) == METADATA_KEYS
    assert restored == assert_json(service.metadata(sandbox_id), httpx.codes.OK)
    assert tuple(saved) == SAVED_KEYS
    assert saved == {"sandbox_id": sandbox_id, "name": SAVED}
    # The anchor date and the state of the sandbox came through all three untouched
    # (requirements 16.2 and 19.4).
    assert restored["anchor_date"] == ANCHOR_DATE.isoformat()
    assert restored["status"] == "active"

    # The trace is the bytes of the events written so far, in seq order: the reset wrote
    # one and the restore the next, the save none, and both are the API's own and so carry
    # no session (requirements 17.2, 17.9 and 19.7).
    written = events(assert_bytes(service.trace(sandbox_id), NDJSON_MEDIA_TYPE))
    assert [event["event"] for event in written] == [
        EventName.SANDBOX_RESET,
        EventName.SNAPSHOT_RESTORE,
    ]
    assert [event["seq"] for event in written] == [1, 2]
    for event in written:
        assert tuple(event)[: len(COMMON_KEYS)] == COMMON_KEYS
        assert event["sandbox_id"] == sandbox_id
        assert event["session_id"] is None
        assert event["anchor_date"] == ANCHOR_DATE.isoformat()
    assert written[1]["name"] == SAVED


@pytest.mark.req("19.8")
def test_the_diff_is_answered_against_the_initial_dataset_and_against_a_snapshot(
    service: Service,
) -> None:
    sandbox_id = service.prebuilt()
    assert service.save(sandbox_id, OTHER_SAVED).status_code == httpx.codes.CREATED

    against_initial = assert_json(service.diff(sandbox_id), httpx.codes.OK)
    against_named = assert_json(service.diff(sandbox_id, base=OTHER_SAVED), httpx.codes.OK)

    # The six keys of requirement 18.1, in its order; the basis is the initial dataset
    # where the parameter is missing and the Named_Snapshot where it names one.
    for body in (against_initial, against_named):
        assert tuple(body) == DIFF_KEYS
        assert body["sandbox_id"] == sandbox_id
        assert body["instance"] == INSTANCE_REF
        assert body["dataset"] == DATASET_REF
        assert body["anchor_date"] == ANCHOR_DATE.isoformat()
    assert against_initial["base"] == INITIAL_BASE
    assert against_named["base"] == OTHER_SAVED
    # Nothing has been written in this sandbox, so neither basis finds a change
    # (requirement 18.7); what a call changes is the protocol flow of task 26.1.
    assert against_initial["changes"] == []
    assert against_named["changes"] == []
    # Reading twice answers the same thing: a diff is a function of the two databases, and
    # reading one does not change the sandbox (requirement 19.8).
    assert assert_json(service.diff(sandbox_id), httpx.codes.OK) == against_initial


@pytest.mark.req("19.14")
def test_the_export_holds_the_trace_and_the_diff_of_every_sandbox_in_range(
    service: Service,
) -> None:
    sandbox_id = service.prebuilt()
    assert service.reset(sandbox_id).status_code == httpx.codes.OK
    created_at = service.created_at(sandbox_id)

    archive = entries(assert_bytes(service.exports({"created_from": created_at}), ZIP_MEDIA_TYPE))

    # Two entries per sandbox of the range, named after it, and nothing else in the zip
    # (requirement 19.14).
    assert all(name.count("/") == 1 for name in archive)
    assert all(name.split("/")[1] in (TRACE_FILENAME, DIFF_FILENAME) for name in archive)
    assert f"{sandbox_id}/{TRACE_FILENAME}" in archive
    assert f"{sandbox_id}/{DIFF_FILENAME}" in archive
    # Each entry is what the trace interface and the diff interface of that sandbox answer,
    # the diff against the initial dataset (requirement 19.14).
    assert archive[f"{sandbox_id}/{TRACE_FILENAME}"] == assert_bytes(
        service.trace(sandbox_id), NDJSON_MEDIA_TYPE
    )
    assert archive[f"{sandbox_id}/{DIFF_FILENAME}"] == assert_bytes(
        service.diff(sandbox_id), JSON_MEDIA_TYPE
    )
    # A range no sandbox was created in is a zip without entries, not an error.
    empty = service.exports({"created_to": "1999-01-01T00:00:00.000+00:00"})
    assert entries(assert_bytes(empty, ZIP_MEDIA_TYPE)) == {}


@pytest.mark.req("19.13", "19.15")
def test_freezing_and_then_deleting_leaves_no_sandbox_behind(service: Service) -> None:
    sandbox_id = service.prebuilt()

    frozen = assert_json(service.freeze(sandbox_id), httpx.codes.OK)

    # The freeze answers the object the metadata interface answers with, the sandbox being
    # frozen before the answer (requirement 19.13), and a frozen sandbox stays readable
    # (requirement 15.12).
    assert tuple(frozen) == METADATA_KEYS
    assert frozen["status"] == "frozen"
    assert frozen == assert_json(service.metadata(sandbox_id), httpx.codes.OK)
    assert assert_bytes(service.trace(sandbox_id), NDJSON_MEDIA_TYPE) == b""
    assert service.diff(sandbox_id).status_code == httpx.codes.OK

    removed = service.remove(sandbox_id)

    # 204 and nothing at all in the body, and the sandbox and its directory are gone
    # (requirements 19.13 and 15.9).
    assert removed.status_code == httpx.codes.NO_CONTENT
    assert removed.content == b""
    assert sandbox_id not in service.sandbox_ids()
    assert not (service.data_dir / SANDBOXES_DIR / sandbox_id).exists()
    # Every interface of that id answers 404 from then on (requirement 19.15).
    for response in (service.metadata(sandbox_id), service.trace(sandbox_id)):
        assert sandbox_id in assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)
    assert service.remove(sandbox_id).status_code == httpx.codes.NOT_FOUND


# --- 401: the Admin token (requirement 19.2) ---------------------------------------------


@pytest.mark.req("19.2")
@pytest.mark.parametrize(
    "template",
    [
        pytest.param(None, id="no-header"),
        pytest.param("", id="empty-header"),
        pytest.param(BEARER, id="scheme-without-token"),
        pytest.param("{token}", id="token-without-scheme"),
        pytest.param("bearer {token}", id="scheme-in-lower-case"),
        pytest.param(f"{BEARER} {{token}}x", id="token-with-a-character-too-many"),
        pytest.param(f"{BEARER} {WRONG_TOKEN}", id="another-token"),
    ],
)
def test_a_request_whose_admin_token_does_not_match_is_unauthorized(
    service: Service, template: str | None
) -> None:
    sandbox_id = service.prebuilt()
    before = service.sandbox_ids()
    header = None if template is None else template.format(token=service.token)

    refused = raw(service, "POST", SANDBOXES, header)

    # 401 and the error object of design.md 7.7, whatever the path and the method
    # (requirement 19.2); the token value is in no message.
    message = assert_error(refused, httpx.codes.UNAUTHORIZED, UNAUTHORIZED)
    assert service.token not in message
    assert refused.headers["WWW-Authenticate"] == BEARER
    # The request was not executed: no sandbox was created and none was changed.
    assert service.sandbox_ids() == before
    assert assert_json(service.metadata(sandbox_id), httpx.codes.OK)["status"] == "active"


@pytest.mark.req("19.2")
def test_a_path_of_no_interface_without_the_token_is_unauthorized(service: Service) -> None:
    # Requirement 19.2 holds for every request of the port, a path that is no interface
    # included: the token is checked before the route table, so this is 401 and not the 404
    # the same path answers with a token (requirement 23.11).
    assert_error(raw(service, "GET", "/admin/nope"), httpx.codes.UNAUTHORIZED, UNAUTHORIZED)
    assert_error(raw(service, "GET", "/nope"), httpx.codes.UNAUTHORIZED, UNAUTHORIZED)
    assert_error(service.client.get("/admin/nope"), httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)


# --- 404: a method, a path or a name of no interface (requirement 19.15) -----------------


@pytest.mark.req("19.15")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", f"{SANDBOXES}/{MISSING_ID}", id="sandbox-that-is-not-there"),
        pytest.param("POST", f"{SANDBOXES}/{MISSING_ID}/reset", id="reset-of-no-sandbox"),
        pytest.param("DELETE", f"{SANDBOXES}/{MISSING_ID}", id="delete-of-no-sandbox"),
        pytest.param("GET", "/admin/datasets/nope/aliases", id="dataset-not-configured"),
        pytest.param("POST", HEALTH, id="health-is-get-only"),
        pytest.param("GET", EXPORTS, id="exports-is-post-only"),
        pytest.param("DELETE", SANDBOXES, id="the-listing-is-not-deletable"),
        pytest.param("GET", "/admin/health/extra", id="extra-segment"),
        pytest.param("GET", "/admin", id="the-prefix-alone"),
    ],
)
def test_a_method_path_or_name_of_no_interface_is_not_found(
    service: Service, method: str, path: str
) -> None:
    sandbox_id = service.prebuilt()

    response = service.client.request(method, path)

    # 404 and not the 405 a framework router would write for a path that exists with
    # another method (requirement 19.15), and nothing of any sandbox was changed.
    assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)
    assert assert_json(service.metadata(sandbox_id), httpx.codes.OK)["status"] == "active"


@pytest.mark.req("19.15")
def test_a_name_the_sandbox_does_not_have_is_not_found(service: Service) -> None:
    sandbox_id = service.prebuilt()

    # A Named_Snapshot the sandbox never saved, as the name of a restore and as the basis
    # of a diff (requirement 19.15).
    for response in (
        service.restore(sandbox_id, MISSING_NAME),
        service.diff(sandbox_id, base=MISSING_NAME),
    ):
        message = assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)
        assert MISSING_NAME in message


# --- 400: a body or a parameter that does not fit (requirements 19.17 and 19.20) ---------


@pytest.mark.req("19.17", "19.20")
@pytest.mark.parametrize(
    ("interface", "body"),
    [
        pytest.param("create", {"extra": 1}, id="create-with-an-undefined-key"),
        pytest.param("create", {"profile": 1}, id="create-with-a-wrong-type"),
        pytest.param("create", {"dataset": "nope"}, id="create-with-no-such-dataset"),
        pytest.param("create", {"anchor_date": "2026-02-30"}, id="create-with-no-such-date"),
        pytest.param("create", "[1, 2]", id="create-with-a-body-that-is-no-object"),
        pytest.param("freeze", {"name": "x"}, id="freeze-takes-no-parameters"),
        pytest.param("snapshots", {"name": "Not_Lower_Case"}, id="name-of-the-wrong-shape"),
        pytest.param("snapshots", {"name": INITIAL_BASE}, id="name-that-is-reserved"),
        pytest.param("snapshots", {"nome": "typo"}, id="name-that-is-missing"),
        pytest.param("restore", {"name": INITIAL_BASE}, id="restore-of-the-reserved-name"),
    ],
)
def test_a_request_that_does_not_fit_the_model_is_a_bad_request(
    service: Service, interface: str, body: Any
) -> None:
    sandbox_id = service.prebuilt()
    before = service.sandbox_ids()
    paths = {
        "create": SANDBOXES,
        "freeze": f"{SANDBOXES}/{sandbox_id}/freeze",
        "snapshots": f"{SANDBOXES}/{sandbox_id}/snapshots",
        "restore": f"{SANDBOXES}/{sandbox_id}/restore",
    }

    response = service.post(paths[interface], body)

    # 400 and the error object, and nothing was created or changed: no new sandbox, the
    # existing one still active and no Named_Snapshot saved (requirements 19.17 and 19.20).
    assert_error(response, httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)
    assert service.sandbox_ids() == before
    assert assert_json(service.metadata(sandbox_id), httpx.codes.OK)["status"] == "active"
    assert service.diff(sandbox_id, base=INITIAL_BASE).status_code == httpx.codes.OK


@pytest.mark.req("19.17")
def test_a_query_parameter_that_does_not_fit_the_model_is_a_bad_request(
    service: Service,
) -> None:
    sandbox_id = service.prebuilt()

    # A status that is not one of the three, a time without an offset and a parameter the
    # model does not define, each a 400 of the listing or the diff (requirement 19.17).
    assert_error(service.listing(status="gone"), httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)
    assert_error(
        service.listing(created_from="2026-10-09"),
        httpx.codes.BAD_REQUEST,
        AdminError.BAD_REQUEST,
    )
    assert_error(
        service.diff(sandbox_id, nope="1"), httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST
    )


# --- 409: a frozen sandbox and a name that is taken (requirement 19.16) ------------------


@pytest.mark.req("19.16")
def test_a_frozen_sandbox_and_a_name_that_is_taken_are_conflicts(service: Service) -> None:
    sandbox_id = service.prebuilt()
    assert service.save(sandbox_id).status_code == httpx.codes.CREATED

    # A name that is already a Named_Snapshot of this sandbox (requirement 19.16).
    taken = service.save(sandbox_id)
    message = assert_error(taken, httpx.codes.CONFLICT, AdminError.CONFLICT)
    assert SAVED in message and sandbox_id in message

    assert service.freeze(sandbox_id).status_code == httpx.codes.OK

    # The three interfaces that change a sandbox, on a frozen one (requirement 19.16).
    for response in (
        service.reset(sandbox_id),
        service.save(sandbox_id, OTHER_SAVED),
        service.restore(sandbox_id),
    ):
        assert sandbox_id in assert_error(response, httpx.codes.CONFLICT, AdminError.CONFLICT)
    # The sandbox is still frozen and its Named_Snapshot is still the only one there
    # (requirement 19.16): the diff against it is answered, the other name is not a basis.
    assert assert_json(service.metadata(sandbox_id), httpx.codes.OK)["status"] == "frozen"
    assert service.diff(sandbox_id, base=SAVED).status_code == httpx.codes.OK
    assert service.diff(sandbox_id, base=OTHER_SAVED).status_code == httpx.codes.NOT_FOUND


# --- 413 and 503: the limits of the environment (requirements 23.7 and 19.18) ------------


@pytest.mark.req("23.7")
def test_a_body_over_the_limit_is_refused_before_the_interface(limited_service: Service) -> None:
    before = limited_service.sandbox_ids()
    oversized = json.dumps({"profile": "x" * (SMALL_BODY_LIMIT * 2)})
    assert len(oversized) > SMALL_BODY_LIMIT

    refused = limited_service.post(SANDBOXES, oversized)

    # 413 naming the limit the environment set, and the interface behind the guard never
    # ran: no sandbox was created and none was changed (requirements 23.7 and 23.9).
    message = assert_error(refused, httpx.codes.REQUEST_ENTITY_TOO_LARGE, PAYLOAD_TOO_LARGE)
    assert str(SMALL_BODY_LIMIT) in message
    assert limited_service.sandbox_ids() == before
    # The port is unharmed: a request within the limit is answered as before.
    assert limited_service.health().status_code == httpx.codes.OK


@pytest.mark.req("19.18")
def test_the_concurrency_limit_answers_service_unavailable(limited_service: Service) -> None:
    # This service has room for one sandbox and no test before this one created any.
    assert limited_service.sandbox_ids() == []
    sandbox_id = limited_service.prebuilt()

    refused = limited_service.create()

    # 503, no sandbox created and the one that is there untouched (requirement 19.18).
    assert_error(refused, httpx.codes.SERVICE_UNAVAILABLE, AdminError.AT_CAPACITY)
    assert limited_service.sandbox_ids() == [sandbox_id]
    assert assert_json(limited_service.metadata(sandbox_id), httpx.codes.OK)["status"] == "active"
    # A sandbox that is frozen does not count against the limit (requirement 15.10).
    assert limited_service.freeze(sandbox_id).status_code == httpx.codes.OK
    assert limited_service.create().status_code == httpx.codes.CREATED


# --- 500: a Snapshot that cannot be built (requirement 19.19) ----------------------------


@pytest.mark.req("19.19")
def test_a_snapshot_that_cannot_be_built_answers_internal_server_error(
    fragile_service: Service,
) -> None:
    # The service started, so the dataset builds at the anchor date of its clock
    # configuration, and a sandbox at that date is created from the cache entry loading
    # left behind (requirements 20.11 and 22.7).
    at_its_own_date = assert_json(fragile_service.create(), httpx.codes.CREATED)
    assert at_its_own_date["anchor_date"] == FRAGILE_ANCHOR
    assert at_its_own_date["dataset"] == f"{FRAGILE_DATASET}@{VERSION}"
    before = fragile_service.sandbox_ids()

    refused = fragile_service.create({"anchor_date": BREAKING_ANCHOR})

    # 500 whose message names the initial dataset and the anchor date that were tried, and
    # nothing of the failure itself (requirement 19.19).
    message = assert_error(refused, httpx.codes.INTERNAL_SERVER_ERROR, AdminError.SNAPSHOT_FAILED)
    assert FRAGILE_DATASET in message
    assert BREAKING_ANCHOR in message
    assert FRAGILE_EXPR not in message
    # What really failed is on the log of the service and nowhere else: the date expression
    # of the dataset went outside the calendar at that date, which is the build this test
    # engineered and not some other failure of the create (module docstring).
    assert FRAGILE_EXPR in fragile_service.stderr
    # No sandbox was created and the existing one is untouched (requirement 19.19).
    assert fragile_service.sandbox_ids() == before
    assert assert_json(fragile_service.metadata(before[0]), httpx.codes.OK)["status"] == "active"
    # The service is still serving: the next request at its own date is answered as before.
    assert fragile_service.create().status_code == httpx.codes.CREATED
