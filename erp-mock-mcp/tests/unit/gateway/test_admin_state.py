"""What the Admin API's state and export interfaces answer, and what they refuse.

The six interfaces of this file are the ones that change a sandbox or read what it has
become: POST .../reset, POST .../snapshots and POST .../restore go through the
Sandbox_Manager and therefore through the sandbox lock, while GET .../trace, GET .../diff
and POST /admin/exports only read and take no lock at all (admin docstring). Each test
speaks HTTP to :mod:`erp_mock.gateway.admin` through :class:`httpx.ASGITransport` - no
socket, no uvicorn - and then looks at the sandbox behind the answer: the rows of its
database, the lines of its trace, the files below ``named/`` and the timing fields the
janitor reads. That is where requirements 19.4 to 19.8 live, the answer being one object
and the change being everything else.

The service under test is the real thing short of the middlewares of task 23.1 and the two
sockets of task 23.4, assembled the way tests/unit/gateway/test_admin_lifecycle.py
assembles it: a real :class:`~erp_mock.gateway.sandbox.SandboxManager` over a data
directory of the test, a real :class:`~erp_mock.gateway.router.McpRouter` in front of it
and the :class:`~erp_mock.gateway.admin.AdminRouter` over both. A session of these tests is
a real SDK transport, so the ``tools/call`` that gives a sandbox something to reset, to
snapshot and to diff is the call a client makes (:meth:`Session.call`).

What is replaced, and why:

- the Snapshot source (:class:`Snapshots`): one Snapshot file built once for the module,
  from the standard basic data of :mod:`tests.helpers.engine_env`, so no test builds a
  dataset;
- the clock (:class:`Ticker`, :func:`stepping_clock`): no test reads the system clock
  (requirement 25.10). The monotonic source is moved by the test alone, which is what makes
  "a read does not change the idle timing" observable (requirements 19.7 and 19.8), and
  every instant is one second after the previous one, which gives each sandbox a
  ``created_at`` of its own and so an order for the export of requirement 19.14;
- the initial dataset of the loaded instance: loading step 8 is not run here, so the
  dataset it would leave behind is built by hand (:func:`loaded_with_dataset`).

The byte-for-byte promise of requirement 24.2 is checked against the other producer of a
diff rather than against a copy of the shape: :func:`stdio_diff` lets
:class:`~erp_mock.gateway.stdio.StdioRuntime` write the ``diff.json`` of the same sandbox
and the test compares the bytes with the response. Reaching into its ``_diff_job`` is
deliberate - the alternative is running a whole stdio process for one file.

The business configuration is the engine test fixture (:mod:`tests.helpers.fixtures`), not
the example instance: it exposes the same kinds of tools and loads in milliseconds, and the
example instance belongs to the integration tests of task 26.2.
"""

import json
import zipfile
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Final

import anyio
import anyio.to_thread
import httpx
import pytest

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.instance import DatasetDef
from erp_mock.gateway.admin import DIFF_FILENAME, AdminError, AdminRouter
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import SessionHandlers
from erp_mock.gateway.router import SESSION_ID_HEADER, McpRouter, sandbox_path
from erp_mock.gateway.sandbox import CALLS_KEY, SandboxLimits, SandboxManager, SandboxRuntime
from erp_mock.gateway.stdio import DIFF_FILENAME as STDIO_DIFF_FILENAME
from erp_mock.gateway.stdio import StdioOptions, StdioRuntime
from erp_mock.gateway.trace import TRACE_FILENAME, EventName
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.loader.yaml_io import load_yaml
from erp_mock.state.schema import StorageSchema, build_schema
from erp_mock.state.snapshot import build_snapshot_db, finalize_snapshot
from erp_mock.state.store import Value
from erp_mock.tools.layer import ToolLayer
from tests.helpers.engine_env import CURRENT_USER, create_basic_data
from tests.helpers.fixtures import (
    ANCHOR_DATE,
    DATASET,
    INSTANCE_DIR,
    fixture_loaded,
)

# Upper bound for every test: a hang fails the test instead of blocking the run.
TIMEOUT_S: Final = 10

# Where the httpx clients send their requests; the ASGI transport never opens a socket.
BASE_URL: Final = "http://127.0.0.1:8000"

# The definition file of the initial dataset of the fixture, relative to its instance
# directory, as loading step 4 reads it.
DATASET_FILE: Final = f"datasets/{DATASET}.yaml"

# The instant the first reading of the test clock returns; the nth reading is this plus
# n - 1 seconds, so two sandboxes get ``created_at`` values a second apart.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# The thresholds every service of this file runs with: seconds of its own monotonic source
# rather than the five minutes of the service.
LIMITS: Final = SandboxLimits(
    idle_seconds=60, freeze_seconds=300, retention_seconds=900, max_sandboxes=8
)

# The interfaces this file covers, as paths below one sandbox.
SANDBOXES: Final = "/admin/sandboxes"
EXPORTS: Final = "/admin/exports"

# The media types of the two answers that are not JSON objects (requirements 19.7, 19.14).
NDJSON: Final = "application/x-ndjson"
ZIP: Final = "application/zip"

# The keys of each answer, in the order the requirements fix them.
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
CHANGE_KEYS: Final = ("entity", "op", "id", "cause_seqs")
ERROR_KEYS: Final = ("error", "message")

# A sandbox id of the right shape that no service of this file has.
MISSING_ID: Final = "sb_0000000000000000"

# The basis of a diff that is no Named_Snapshot but the initial dataset (requirement 19.8).
INITIAL: Final = "initial"

# The tool of the fixture every test calls and the records of the basic data it names
# (:data:`~tests.helpers.engine_env.BASIC_DATA`): the ids are fixed by the order the
# records are created in. One call inserts one header and one line, which is a diff with
# something in it and a trace with a tools_call event in it.
COUNT_CREATE: Final = "count_sheet_create"
COUNT_SHEET: Final = "count_sheet"
COUNT_SHEET_LINE: Final = "count_sheet_line"
DEPOT: Final = "dep_000001"
WASHER: Final = "art_000002"
NEW_COUNT: Final[Mapping[str, Any]] = {
    "depot_id": DEPOT,
    "count_kind": "gain",
    "lines": [{"article_id": WASHER, "diff_qty": "3"}],
}

# The headers the official client puts on every request (mcp.client.streamable_http) and
# the protocol version it announces.
JSON_AND_SSE: Final = "application/json, text/event-stream"
POST_HEADERS: Final[Mapping[str, str]] = {
    "content-type": "application/json",
    "accept": JSON_AND_SSE,
}
PROTOCOL_VERSION: Final = "2025-11-25"

# A Named_Snapshot name the tests save under, and one nobody saves.
SAVED: Final = "after_setup"
UNSAVED: Final = "never_saved"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- the test clock (requirement 25.10) --------------------------------------------------


@dataclass(slots=True)
class Ticker:
    """A monotonic source the test moves itself (requirement 25.10).

    Every reading returns :attr:`seconds`, which :meth:`advance` raises. The Sandbox_Manager
    reads it for the idle timing of every job it runs, so a reading taken after
    :meth:`advance` is what a job would have written into ``last_done`` - and what an
    interface that takes no lock must not write (requirements 19.7 and 19.8).
    """

    seconds: float = 1000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> float:
        """Move the clock forward and return the new reading."""
        self.seconds += seconds
        return self.seconds


def stepping_clock(ticker: Ticker) -> SystemClock:
    """A clock whose nth instant is :data:`INSTANT` plus n - 1 seconds, over ``ticker``."""
    readings = iter(range(1 << 20))
    return SystemClock(now=lambda: INSTANT + timedelta(seconds=next(readings)), monotonic=ticker)


# --- the instance configuration and its Snapshot -----------------------------------------


@dataclass(frozen=True, slots=True)
class Env:
    """What every test of this file starts from, built once for the module."""

    loaded: LoadedInstance
    schema: StorageSchema
    layer: ToolLayer
    snapshot: Path

    @property
    def profile(self) -> ProfileModel:
        """The one profile of the fixture instance, whose tools a session sees."""
        return self.loaded.default_profile

    @property
    def dataset(self) -> DatasetModel:
        """The one initial dataset of the fixture instance."""
        return self.loaded.default_dataset


def loaded_with_dataset() -> LoadedInstance:
    """The fixture configuration with the initial dataset loading step 8 would leave.

    The steps before step 8 leave no :class:`~erp_mock.loader.instance.DatasetModel`
    behind, and the Admin API looks a dataset name up in exactly that map
    (requirement 19.3), so the model is built here the way step 8 builds it, short of the
    cache entry: no interface of this file reads one.
    """
    loaded = fixture_loaded()
    definition = DatasetDef.model_validate(load_yaml(INSTANCE_DIR / DATASET_FILE))
    dataset = DatasetModel(definition=definition, file=DATASET_FILE, current_user=CURRENT_USER)
    return replace(loaded, datasets=FrozenMap({dataset.name: dataset}))


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Env:
    """The fixture instance, its tools layer and one Snapshot of its initial dataset."""
    loaded = loaded_with_dataset()
    layer = ToolLayer(loaded)
    schema = build_schema(loaded.domain)
    return Env(
        loaded=loaded,
        schema=schema,
        layer=layer,
        snapshot=build_snapshot(tmp_path_factory.mktemp("snapshot"), schema, layer),
    )


def build_snapshot(directory: Path, env_schema: StorageSchema, layer: ToolLayer) -> Path:
    """Build the one Snapshot of this file below ``directory`` and return its path."""
    build = directory / "build.sqlite"
    snapshot = directory / "snapshot.sqlite"
    store = build_snapshot_db(build, env_schema)
    try:
        create_basic_data(layer.engine, store, anchor_date=ANCHOR_DATE)
    finally:
        store.close()
    finalize_snapshot(build, snapshot)
    build.unlink()
    return snapshot


@dataclass(slots=True)
class Snapshots:
    """A :class:`~erp_mock.gateway.sandbox.SnapshotSource` over one Snapshot file."""

    db: Path
    asked: list[tuple[str, date]] = field(default_factory=list)

    @contextmanager
    def snapshot(self, dataset: DatasetModel, anchor: date) -> Iterator[Path]:
        self.asked.append((dataset.name, anchor))
        yield self.db


# --- the service under test --------------------------------------------------------------


@dataclass(slots=True)
class Service:
    """The Admin API with the MCP endpoint, the sandboxes and two HTTP clients."""

    admin: AdminRouter
    mcp: McpRouter
    manager: SandboxManager
    client: httpx.AsyncClient
    mcp_client: httpx.AsyncClient
    ticker: Ticker
    env: Env

    async def prebuilt(self) -> str:
        """Prebuild one sandbox through the API and return its id (requirement 19.3)."""
        response = await self.client.post(SANDBOXES)
        assert response.status_code == httpx.codes.CREATED, response.text
        sandbox_id = response.json()["sandbox_id"]
        assert isinstance(sandbox_id, str)
        return sandbox_id

    async def metadata(self, sandbox_id: str) -> httpx.Response:
        """GET /admin/sandboxes/{id} (requirement 19.12)."""
        return await self.client.get(f"{SANDBOXES}/{sandbox_id}")

    async def post(self, path: str, body: Any | None = None) -> httpx.Response:
        """POST ``path`` with ``body`` as JSON, or with no body at all for None."""
        if body is None:
            return await self.client.post(path)
        content = body if isinstance(body, str) else json.dumps(body)
        return await self.client.post(
            path, content=content.encode(), headers={"content-type": "application/json"}
        )

    async def reset(self, sandbox_id: str, body: Any | None = None) -> httpx.Response:
        """POST /admin/sandboxes/{id}/reset (requirement 19.4)."""
        return await self.post(f"{SANDBOXES}/{sandbox_id}/reset", body)

    async def save(self, sandbox_id: str, name: Any = SAVED) -> httpx.Response:
        """POST /admin/sandboxes/{id}/snapshots (requirement 19.5)."""
        return await self.post(f"{SANDBOXES}/{sandbox_id}/snapshots", {"name": name})

    async def restore(self, sandbox_id: str, name: Any = SAVED) -> httpx.Response:
        """POST /admin/sandboxes/{id}/restore (requirement 19.6)."""
        return await self.post(f"{SANDBOXES}/{sandbox_id}/restore", {"name": name})

    async def trace(self, sandbox_id: str) -> httpx.Response:
        """GET /admin/sandboxes/{id}/trace (requirement 19.7)."""
        return await self.client.get(f"{SANDBOXES}/{sandbox_id}/trace")

    async def diff(self, sandbox_id: str, **query: str) -> httpx.Response:
        """GET /admin/sandboxes/{id}/diff (requirement 19.8)."""
        return await self.client.get(f"{SANDBOXES}/{sandbox_id}/diff", params=query)

    async def exports(self, body: Any | None = None) -> httpx.Response:
        """POST /admin/exports (requirement 19.14)."""
        return await self.post(EXPORTS, body)

    async def freeze(self, sandbox_id: str) -> httpx.Response:
        """POST /admin/sandboxes/{id}/freeze (requirement 19.13)."""
        response = await self.client.post(f"{SANDBOXES}/{sandbox_id}/freeze")
        assert response.status_code == httpx.codes.OK, response.text
        return response

    def sandbox(self, sandbox_id: str) -> SandboxRuntime:
        """The runtime of the sandbox ``sandbox_id``, which must be one of this service."""
        found = self.manager.get(sandbox_id)
        assert found is not None
        return found

    async def worked(self, calls: int = 1) -> "Session":
        """One prebuilt sandbox with a session on it that made ``calls`` tool calls.

        What most tests of this file start from: a sandbox whose database holds records the
        initial dataset does not have, whose trace holds a ``session_start`` and one
        ``tools_call`` per call, and whose call count is therefore ``calls``
        (requirement 23.12).
        """
        session = await self.open_session(await self.prebuilt())
        for request_id in range(calls):
            response = await session.call(request_id + 2)
            assert response.json()["result"]["isError"] is False, response.text
        return session

    async def open_session(self, sandbox_id: str) -> "Session":
        """Open an MCP session on ``/s/<sandbox_id>/mcp`` (requirement 15.3)."""
        path = sandbox_path(sandbox_id)
        response = await self.mcp_client.post(
            path, content=json.dumps(initialize_body()).encode(), headers=dict(POST_HEADERS)
        )
        assert response.status_code == httpx.codes.OK, response.text
        return Session(service=self, session_id=response.headers[SESSION_ID_HEADER], path=path)


@dataclass(slots=True)
class Session:
    """One open MCP session, and the one tool call a test of this file makes in it."""

    service: Service
    session_id: str
    path: str

    @property
    def sandbox(self) -> SandboxRuntime:
        """The sandbox the session is bound to (requirement 15.3)."""
        return self.service.sandbox(self.sandbox_id)

    @property
    def sandbox_id(self) -> str:
        """The id of that sandbox, which the path of the session carries."""
        return self.path.split("/")[2]

    async def call(self, request_id: int) -> httpx.Response:
        """Send one ``count_sheet_create`` inside this session (requirement 17.3)."""
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": COUNT_CREATE, "arguments": dict(NEW_COUNT)},
        }
        headers = {
            **POST_HEADERS,
            SESSION_ID_HEADER: self.session_id,
            "mcp-protocol-version": PROTOCOL_VERSION,
        }
        return await self.service.mcp_client.post(
            self.path, content=json.dumps(body).encode(), headers=headers
        )


def initialize_body(request_id: int = 1) -> dict[str, Any]:
    """The initialize request the official client opens a session with."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.0.0"},
        },
    }


@asynccontextmanager
async def service_over(
    env: Env, data_dir: Path, limits: SandboxLimits = LIMITS
) -> AsyncIterator[Service]:
    """The Admin API over an MCP router and a Sandbox_Manager in ``data_dir``.

    What task 23.4 will assemble, short of the middlewares and the two uvicorn servers: one
    Sandbox_Manager, the MCP endpoint over it with its session tasks in a task group of this
    block, and the Admin API over that endpoint, which is where its sandboxes come from
    (admin docstring). The schema is passed in rather than built by the Admin API, as the
    serve command passes the one its sandboxes were created with. Every session is ended and
    every sandbox closed when the block returns.
    """
    ticker = Ticker()
    manager = SandboxManager(
        data_dir=data_dir,
        loaded=env.loaded,
        schema=env.schema,
        snapshots=Snapshots(env.snapshot),
        clock=stepping_clock(ticker),
        limits=limits,
    )
    mcp = McpRouter(SessionHandlers(manager, env.layer), env.loaded)
    admin = AdminRouter(mcp, env.loaded, env.schema)
    try:
        with anyio.fail_after(TIMEOUT_S):
            async with anyio.create_task_group() as tg:
                mcp.attach(tg)
                async with (
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=admin), base_url=BASE_URL
                    ) as client,
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=mcp), base_url=BASE_URL
                    ) as mcp_client,
                ):
                    # httpx adds "Accept: */*" to every request by default; drop it on the
                    # MCP client so that each request carries exactly its own headers.
                    del mcp_client.headers["accept"]
                    try:
                        yield Service(
                            admin=admin,
                            mcp=mcp,
                            manager=manager,
                            client=client,
                            mcp_client=mcp_client,
                            ticker=ticker,
                            env=env,
                        )
                    finally:
                        await mcp.shutdown()
                        tg.cancel_scope.cancel()
    finally:
        manager.close()


# --- what a sandbox holds ----------------------------------------------------------------


def rows(sandbox: SandboxRuntime, table: str) -> list[dict[str, Value]]:
    """Every record of ``table`` of the sandbox database; no request is in flight."""
    with sandbox.store.transaction() as uow:
        return [dict(row) for row in uow.select(table)]


def sheet_ids(sandbox: SandboxRuntime) -> list[str]:
    """The ids of the count sheets of the sandbox, which the calls of a test inserted."""
    return sorted(str(row["id"]) for row in rows(sandbox, COUNT_SHEET))


def trace_bytes(sandbox: SandboxRuntime) -> bytes:
    """The bytes of the trace file of the sandbox, as a reader would see them."""
    return (sandbox.directory / TRACE_FILENAME).read_bytes()


def events(payload: bytes) -> list[dict[str, Any]]:
    """The events of a trace body, one per line (requirement 17.1)."""
    return [json.loads(line) for line in payload.decode("utf-8").splitlines() if line]


def named_files(sandbox: SandboxRuntime) -> dict[str, bytes]:
    """The files below ``named/`` of the sandbox, by name, with their bytes.

    What a 409 and a refused request must leave exactly as they were
    (requirement 19.16).
    """
    directory = sandbox.named_dir
    if not directory.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def saved_calls(sandbox: SandboxRuntime, name: str) -> int:
    """The call count the side file of the Named_Snapshot ``name`` holds (req 23.12)."""
    payload = json.loads(sandbox.named_meta(name).read_bytes())
    count = payload[CALLS_KEY]
    assert isinstance(count, int)
    return count


async def stdio_diff(service: Service, sandbox: SandboxRuntime) -> bytes:
    """The bytes the stdio runtime writes into ``<sandbox>/diff.json`` (req 24.2).

    The other producer of a diff, run on the same sandbox: its exit path is a
    ``session_end``, a freeze and this one job (requirement 22.4), and only the job is
    wanted here. ``_diff_job`` is reached directly because the alternative is a whole stdio
    process for one file; what it writes is what the Admin API's diff interface must send,
    byte for byte.
    """
    runtime = StdioRuntime(
        loaded=service.env.loaded,
        schema=service.env.schema,
        manager=service.manager,
        handlers=SessionHandlers(service.manager, service.env.layer),
        options=StdioOptions(profile=service.env.profile, dataset=service.env.dataset),
        signals=(),
    )
    await anyio.to_thread.run_sync(lambda: runtime._diff_job(sandbox))
    return (sandbox.directory / DIFF_FILENAME).read_bytes()


# --- what an answer looks like -----------------------------------------------------------


def assert_json(response: httpx.Response, status: int) -> Any:
    """Assert ``response`` is JSON of ``status`` and return its parsed body."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    return response.json()


def assert_error(response: httpx.Response, status: int, code: AdminError) -> str:
    """Assert ``response`` is the error object of ``status`` and return its message.

    The shape is design.md 7.7's and is the same whatever went wrong: ``error`` with a
    short code and ``message`` with a Chinese explanation, in that order and nothing else.
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


def entries(payload: bytes) -> dict[str, bytes]:
    """The entries of a zip, by name, in the order the archive lists them (req 19.14)."""
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# --- POST /admin/sandboxes/{id}/reset (requirement 19.4) ---------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.4")
async def test_a_reset_answers_the_metadata_and_puts_the_database_back(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        sandbox = session.sandbox
        assert sheet_ids(sandbox) != []
        assert sandbox.sandbox_calls == 1

        body = assert_json(await service.reset(session.sandbox_id), httpx.codes.OK)

        # The answer is the object the metadata interface answers with (requirement 19.4).
        assert tuple(body) == METADATA_KEYS
        assert body == assert_json(await service.metadata(session.sandbox_id), httpx.codes.OK)
        # The database is the Snapshot again - the records of the call are gone, the lines
        # with them - and the call count is 0 (requirement 23.12, design.md data model 2).
        assert sheet_ids(sandbox) == []
        assert rows(sandbox, COUNT_SHEET_LINE) == []
        assert sandbox.sandbox_calls == 0
        # The anchor date, the status and the session bindings are untouched (req 16.2).
        assert body["anchor_date"] == ANCHOR_DATE.isoformat()
        assert body["status"] == "active"
        assert body["session_ids"] == [session.session_id]


@pytest.mark.anyio
@pytest.mark.req("19.4")
async def test_a_reset_appends_its_event_and_keeps_the_trace_continuous(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        before = events(trace_bytes(session.sandbox))

        assert (await service.reset(session.sandbox_id)).status_code == httpx.codes.OK

        # The trace written so far is kept and a sandbox_reset is appended to it, its seq
        # continuing the numbering (requirements 19.4 and 17.7).
        after = events(trace_bytes(session.sandbox))
        assert after[: len(before)] == before
        assert [event["event"] for event in after] == [
            EventName.SESSION_START,
            EventName.TOOLS_CALL,
            EventName.SANDBOX_RESET,
        ]
        assert [event["seq"] for event in after] == [1, 2, 3]


@pytest.mark.anyio
@pytest.mark.req("19.4", "19.6")
async def test_the_counters_come_back_with_the_database(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        sandbox = session.sandbox
        first = sheet_ids(sandbox)

        assert (await service.reset(session.sandbox_id)).status_code == httpx.codes.OK
        assert (await session.call(98)).status_code == httpx.codes.OK

        # The id, code and number counters live in the sandbox database and come back with
        # it (design.md data model 2), so the call after a reset writes the records the
        # first call wrote - which is what makes a replay reproducible (requirement 24.4).
        assert sheet_ids(sandbox) == first
        # And the same holds for a restore: a Named_Snapshot taken now and restored after
        # another call puts both the records and the counters back.
        assert (await service.save(session.sandbox_id)).status_code == httpx.codes.CREATED
        assert (await session.call(97)).status_code == httpx.codes.OK
        second = sheet_ids(sandbox)
        assert second != first
        assert (await service.restore(session.sandbox_id)).status_code == httpx.codes.OK
        assert (await session.call(96)).status_code == httpx.codes.OK
        assert sheet_ids(sandbox) == second


@pytest.mark.anyio
@pytest.mark.req("19.4")
async def test_a_reset_keeps_the_named_snapshots_of_the_sandbox(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        assert (await service.save(session.sandbox_id)).status_code == httpx.codes.CREATED
        saved = named_files(session.sandbox)

        assert (await service.reset(session.sandbox_id)).status_code == httpx.codes.OK

        # A reset replaces the working database and nothing else (requirement 19.4), so
        # the sandbox can be restored to what it was before it.
        assert named_files(session.sandbox) == saved
        assert (await service.restore(session.sandbox_id)).status_code == httpx.codes.OK
        assert sheet_ids(session.sandbox) != []


# --- POST /admin/sandboxes/{id}/snapshots (requirement 19.5) -----------------------------


@pytest.mark.anyio
@pytest.mark.req("19.5")
async def test_saving_a_named_snapshot_answers_201_and_leaves_the_sandbox_alone(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        sandbox = session.sandbox
        before = (sheet_ids(sandbox), trace_bytes(sandbox))

        body = assert_json(await service.save(session.sandbox_id), httpx.codes.CREATED)

        # 201 and the two keys of requirement 19.5, in its order.
        assert tuple(body) == SAVED_KEYS
        assert body == {"sandbox_id": session.sandbox_id, "name": SAVED}
        # The two files of the Named_Snapshot, the side one holding the call count of this
        # moment (requirement 23.12); the sandbox database and its trace are unchanged.
        assert sorted(named_files(sandbox)) == [f"{SAVED}.json", f"{SAVED}.sqlite"]
        assert sandbox.named_names() == (SAVED,)
        assert saved_calls(sandbox, SAVED) == 1
        assert (sheet_ids(sandbox), trace_bytes(sandbox)) == before


@pytest.mark.anyio
@pytest.mark.req("19.5")
async def test_two_named_snapshots_of_one_sandbox_live_side_by_side(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()

        assert (await service.save(session.sandbox_id, "first")).status_code == (
            httpx.codes.CREATED
        )
        assert (await service.save(session.sandbox_id, "second")).status_code == (
            httpx.codes.CREATED
        )

        # Each name is a snapshot of its own, in code point order (requirement 19.5).
        assert session.sandbox.named_names() == ("first", "second")


# --- POST /admin/sandboxes/{id}/restore (requirement 19.6) -------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.6")
async def test_restoring_puts_the_database_and_the_call_count_back(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        sandbox = session.sandbox
        assert (await service.save(session.sandbox_id)).status_code == httpx.codes.CREATED
        at_snapshot = sheet_ids(sandbox)
        assert (await session.call(99)).status_code == httpx.codes.OK
        assert sheet_ids(sandbox) != at_snapshot
        assert sandbox.sandbox_calls == 2

        body = assert_json(await service.restore(session.sandbox_id), httpx.codes.OK)

        # The answer is the metadata object again, and the database is the snapshot's
        # content: the records of the call after it are gone (requirement 19.6).
        assert tuple(body) == METADATA_KEYS
        assert body == assert_json(await service.metadata(session.sandbox_id), httpx.codes.OK)
        assert sheet_ids(sandbox) == at_snapshot
        # The call count is the one the snapshot was saved with (requirement 23.12).
        assert sandbox.sandbox_calls == 1
        # The anchor date, the status, the bindings and the Named_Snapshots stay as they
        # are, so the same snapshot can be restored again (requirements 16.2 and 19.6).
        assert (body["anchor_date"], body["status"]) == (ANCHOR_DATE.isoformat(), "active")
        assert body["session_ids"] == [session.session_id]
        assert sandbox.named_names() == (SAVED,)


@pytest.mark.anyio
@pytest.mark.req("19.6")
async def test_restoring_appends_an_event_that_names_the_snapshot(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        assert (await service.save(session.sandbox_id)).status_code == httpx.codes.CREATED

        assert (await service.restore(session.sandbox_id)).status_code == httpx.codes.OK

        # The trace is kept and a snapshot_restore naming the snapshot is appended, its seq
        # continuing the numbering (requirements 19.6 and 17.7).
        recorded = events(trace_bytes(session.sandbox))
        assert [event["event"] for event in recorded] == [
            EventName.SESSION_START,
            EventName.TOOLS_CALL,
            EventName.SNAPSHOT_RESTORE,
        ]
        assert [event["seq"] for event in recorded] == [1, 2, 3]
        assert recorded[-1]["name"] == SAVED


@pytest.mark.anyio
@pytest.mark.req("19.15")
async def test_restoring_a_snapshot_the_sandbox_does_not_have_is_not_found(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        before = (sheet_ids(session.sandbox), trace_bytes(session.sandbox))

        response = await service.restore(session.sandbox_id, UNSAVED)

        # A name that is no Named_Snapshot of this sandbox is a 404, and nothing of the
        # sandbox was touched (requirement 19.15).
        message = assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)
        assert UNSAVED in message
        assert (sheet_ids(session.sandbox), trace_bytes(session.sandbox)) == before


# --- the 400 of requirement 19.20 --------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.20")
@pytest.mark.parametrize("interface", ["snapshots", "restore"])
@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("Upper", id="upper-case"),
        pytest.param("_leading", id="leading-underscore"),
        pytest.param("-leading", id="leading-hyphen"),
        pytest.param("with space", id="space"),
        pytest.param("with/slash", id="slash"),
        pytest.param("../escape", id="parent-directory"),
        pytest.param("a" * 65, id="too-long"),
        pytest.param(INITIAL, id="reserved-name"),
        pytest.param(1, id="not-a-string"),
        pytest.param(None, id="null"),
    ],
)
async def test_a_name_that_is_not_a_snapshot_name_is_a_bad_request(
    env: Env, tmp_path: Path, interface: str, name: Any
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        assert (await service.save(session.sandbox_id)).status_code == httpx.codes.CREATED
        before = (sheet_ids(session.sandbox), trace_bytes(session.sandbox))
        saved = named_files(session.sandbox)

        response = await service.post(
            f"{SANDBOXES}/{session.sandbox_id}/{interface}", {"name": name}
        )

        # A name of another shape and the reserved ``initial`` are each a 400; the sandbox
        # and the Named_Snapshot it has are untouched (requirement 19.20).
        assert_error(response, httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)
        assert (sheet_ids(session.sandbox), trace_bytes(session.sandbox)) == before
        assert named_files(session.sandbox) == saved


@pytest.mark.anyio
@pytest.mark.req("19.20")
@pytest.mark.parametrize("interface", ["snapshots", "restore"])
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="name-missing"),
        pytest.param({"snapshot": SAVED}, id="another-key"),
        pytest.param({"name": SAVED, "extra": 1}, id="undefined-key-beside-name"),
        pytest.param("[1, 2]", id="not-an-object"),
        pytest.param("{", id="not-json"),
    ],
)
async def test_a_body_that_does_not_fit_the_model_is_a_bad_request(
    env: Env, tmp_path: Path, interface: str, body: Any
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        before = named_files(session.sandbox)

        response = await service.post(f"{SANDBOXES}/{session.sandbox_id}/{interface}", body)

        # ``name`` is required and the model forbids every other key (requirement 19.20).
        assert_error(response, httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)
        assert named_files(session.sandbox) == before


@pytest.mark.anyio
@pytest.mark.req("19.17")
async def test_a_body_on_the_reset_interface_is_a_bad_request(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        before = sheet_ids(session.sandbox)

        response = await service.reset(session.sandbox_id, {"name": SAVED})

        # A reset defines no parameters, so every key of a body is an undefined key and
        # nothing is reset (requirement 19.17).
        assert_error(response, httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)
        assert sheet_ids(session.sandbox) == before


@pytest.mark.anyio
@pytest.mark.req("19.17")
async def test_an_undefined_query_parameter_of_the_diff_is_a_bad_request(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()

        response = await service.diff(session.sandbox_id, basis=INITIAL)

        # ``base`` is the one parameter of the interface; a parameter that was asked for
        # and would be silently dropped is a 400 instead (admin docstring).
        assert_error(response, httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)


# --- the 409 of requirement 19.16 --------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.16")
@pytest.mark.parametrize("interface", ["reset", "snapshots", "restore"])
async def test_changing_a_frozen_sandbox_is_a_conflict(
    env: Env, tmp_path: Path, interface: str
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        sandbox = session.sandbox
        assert (await service.save(session.sandbox_id)).status_code == httpx.codes.CREATED
        await service.freeze(session.sandbox_id)
        # Read after the freeze, which checkpointed the WAL and closed the database: from
        # here on the three files are what they will be until the sandbox is deleted.
        before = (trace_bytes(sandbox), named_files(sandbox))
        database = sandbox.store.db_path.read_bytes()

        body = None if interface == "reset" else {"name": SAVED}
        response = await service.post(f"{SANDBOXES}/{session.sandbox_id}/{interface}", body)

        # A frozen sandbox stays read-only until it is deleted (requirement 15.12), so the
        # three interfaces that change one answer 409 and change nothing of it: not its
        # database, not its trace, not its state and not its Named_Snapshots (req 19.16).
        assert session.sandbox_id in assert_error(
            response, httpx.codes.CONFLICT, AdminError.CONFLICT
        )
        assert (trace_bytes(sandbox), named_files(sandbox)) == before
        assert sandbox.store.db_path.read_bytes() == database
        assert sandbox.status == "frozen"


@pytest.mark.anyio
@pytest.mark.req("19.16")
async def test_a_named_snapshot_whose_name_is_taken_is_a_conflict(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        sandbox = session.sandbox
        assert (await service.save(session.sandbox_id)).status_code == httpx.codes.CREATED
        saved = named_files(sandbox)
        # One more call, so a snapshot taken now would hold other records than the one of
        # that name: the 409 must keep the snapshot that is there.
        assert (await session.call(99)).status_code == httpx.codes.OK

        response = await service.save(session.sandbox_id)

        message = assert_error(response, httpx.codes.CONFLICT, AdminError.CONFLICT)
        assert SAVED in message and session.sandbox_id in message
        assert named_files(sandbox) == saved
        assert sandbox.named_names() == (SAVED,)


# --- GET /admin/sandboxes/{id}/trace (requirement 19.7) ----------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.7")
async def test_the_trace_is_the_bytes_of_the_events_written_so_far(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()

        payload = assert_bytes(await service.trace(session.sandbox_id), NDJSON)

        # The response body is the trace file byte for byte: the session_start of the
        # initialize and the tools_call of the call, one complete line each, ending with a
        # newline (requirement 19.7).
        assert payload == trace_bytes(session.sandbox)
        assert payload.endswith(b"\n")
        recorded = events(payload)
        assert [event["event"] for event in recorded] == [
            EventName.SESSION_START,
            EventName.TOOLS_CALL,
        ]
        assert [event["seq"] for event in recorded] == [1, 2]
        assert {event["sandbox_id"] for event in recorded} == {session.sandbox_id}


@pytest.mark.anyio
@pytest.mark.req("19.7")
async def test_a_sandbox_without_events_answers_an_empty_body(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()

        # A prebuilt sandbox nobody has connected to has written no event
        # (requirement 19.3), which is an empty body and not a 404 (requirement 19.7).
        assert assert_bytes(await service.trace(sandbox_id), NDJSON) == b""


@pytest.mark.anyio
@pytest.mark.req("19.7")
async def test_a_line_that_is_half_written_is_not_in_the_trace(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        complete = assert_bytes(await service.trace(session.sandbox_id), NDJSON)

        # What a reader sees of an event that is being written: a prefix without its
        # newline. The answer ends at the last newline, so the fragment is not in it and
        # every line of it parses (requirement 19.7).
        with (session.sandbox.directory / TRACE_FILENAME).open("ab") as handle:
            handle.write(b'{"seq": 3, "ts": "2026-10-09T10')

        payload = assert_bytes(await service.trace(session.sandbox_id), NDJSON)
        assert payload == complete
        assert len(events(payload)) == 2


@pytest.mark.anyio
@pytest.mark.req("19.7")
async def test_the_trace_of_a_frozen_sandbox_is_answered_too(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        before = assert_bytes(await service.trace(session.sandbox_id), NDJSON)
        await service.freeze(session.sandbox_id)

        # A frozen sandbox writes no more events - the freeze closed its trace writer -
        # and its file stays until the sandbox is deleted, so the interface answers the
        # same bytes as before the freeze (requirements 15.12 and 19.7).
        payload = assert_bytes(await service.trace(session.sandbox_id), NDJSON)
        assert payload == before
        assert [event["event"] for event in events(payload)] == [
            EventName.SESSION_START,
            EventName.TOOLS_CALL,
        ]


# --- GET /admin/sandboxes/{id}/diff (requirement 19.8) -----------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.8")
async def test_the_diff_against_the_initial_dataset_reports_what_the_calls_wrote(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()

        payload = assert_bytes(await service.diff(session.sandbox_id), "application/json")

        # The six keys of design.md data model 5, in that order, with ``initial`` as the
        # basis the parameter defaults to (requirement 19.8).
        body = json.loads(payload)
        assert tuple(body) == DIFF_KEYS
        assert body["sandbox_id"] == session.sandbox_id
        assert body["dataset"] == session.sandbox.dataset_ref
        assert body["anchor_date"] == ANCHOR_DATE.isoformat()
        assert body["base"] == INITIAL
        # The records of the one call, each an insert the initial dataset does not have,
        # with the trace seq of the call that wrote it (requirements 18.2 and 18.3).
        changed = {(change["entity"], change["op"]) for change in body["changes"]}
        assert (COUNT_SHEET, "insert") in changed
        assert (COUNT_SHEET_LINE, "insert") in changed
        for change in body["changes"]:
            assert tuple(change)[: len(CHANGE_KEYS)] == CHANGE_KEYS
            assert change["cause_seqs"] == [2]


@pytest.mark.anyio
@pytest.mark.req("19.8")
async def test_the_diff_is_the_same_bytes_as_the_file_the_stdio_runtime_writes(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()

        payload = assert_bytes(await service.diff(session.sandbox_id), "application/json")

        # One serialization for both producers, so an upper layer comparing the diff of an
        # HTTP sandbox with the diff.json of a stdio run compares equal texts (req 24.2).
        assert payload == await stdio_diff(service, session.sandbox)


@pytest.mark.anyio
@pytest.mark.req("19.8")
async def test_the_diff_can_be_taken_against_a_named_snapshot(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        assert (await service.save(session.sandbox_id)).status_code == httpx.codes.CREATED
        assert (await session.call(99)).status_code == httpx.codes.OK

        against_snapshot = json.loads(
            assert_bytes(await service.diff(session.sandbox_id, base=SAVED), "application/json")
        )
        against_initial = json.loads(
            assert_bytes(await service.diff(session.sandbox_id), "application/json")
        )

        # The basis is the Named_Snapshot and the output names it (requirement 19.8): the
        # changes are the ones of the call after it was saved, while the diff against the
        # initial dataset holds the records of both calls.
        assert against_snapshot["base"] == SAVED
        assert {change["id"] for change in against_snapshot["changes"]} < {
            change["id"] for change in against_initial["changes"]
        }
        assert {change["cause_seqs"][0] for change in against_snapshot["changes"]} == {3}


@pytest.mark.anyio
@pytest.mark.req("19.8")
async def test_the_diff_of_a_sandbox_that_changed_nothing_has_no_changes(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        sandbox_id = await service.prebuilt()

        body = json.loads(assert_bytes(await service.diff(sandbox_id), "application/json"))

        # An empty array and the other keys unchanged (requirement 18.7).
        assert tuple(body) == DIFF_KEYS
        assert body["changes"] == []


@pytest.mark.anyio
@pytest.mark.req("19.15")
@pytest.mark.parametrize(
    "base",
    [
        pytest.param(UNSAVED, id="no-such-snapshot"),
        pytest.param("../../etc/passwd", id="outside-the-sandbox"),
        pytest.param("Initial", id="case-sensitive"),
        pytest.param("", id="empty"),
    ],
)
async def test_a_diff_base_that_is_no_named_snapshot_is_not_found(
    env: Env, tmp_path: Path, base: str
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        assert (await service.save(session.sandbox_id)).status_code == httpx.codes.CREATED

        response = await service.diff(session.sandbox_id, base=base)

        # Every basis but ``initial`` is looked up among the Named_Snapshots the sandbox
        # has, so a name it does not have - whatever that name looks like - is a 404 and
        # reaches no file (requirement 19.15).
        assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)


@pytest.mark.anyio
@pytest.mark.req("19.8")
async def test_the_diff_of_a_frozen_sandbox_is_answered_too(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        before = assert_bytes(await service.diff(session.sandbox_id), "application/json")
        await service.freeze(session.sandbox_id)

        # Freezing closes the database, and the diff opens a read connection of its own,
        # so a frozen sandbox answers the same diff as before (requirements 15.12, 19.8).
        assert assert_bytes(await service.diff(session.sandbox_id), "application/json") == before


# --- the two reads take no lock and no time (requirements 19.7 and 19.8) -----------------


@pytest.mark.anyio
@pytest.mark.req("19.7", "19.8")
async def test_reading_the_trace_and_the_diff_does_not_change_the_idle_timing(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        sandbox = session.sandbox
        # The monotonic source moves on, so a job of the Sandbox_Manager would write a
        # later reading into ``last_done`` and the sandbox would never turn idle.
        service.ticker.advance(30)
        before = (sandbox.last_done, sandbox.status, sandbox.idle_since)

        assert (await service.trace(session.sandbox_id)).status_code == httpx.codes.OK
        assert (await service.diff(session.sandbox_id)).status_code == httpx.codes.OK
        assert (await service.exports()).status_code == httpx.codes.OK

        # Neither read went through the sandbox lock, so the idle timing and the state of
        # the sandbox are what they were (requirements 19.7 and 19.8).
        assert (sandbox.last_done, sandbox.status, sandbox.idle_since) == before
        # And a reset, which does go through the lock, moves the timing on - which is what
        # makes the assertion above mean something.
        assert (await service.reset(session.sandbox_id)).status_code == httpx.codes.OK
        assert sandbox.last_done == service.ticker.seconds


# --- POST /admin/exports (requirement 19.14) ---------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.14")
async def test_the_export_holds_the_trace_and_the_diff_of_every_sandbox_in_order(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        first = await service.worked()
        second = await service.worked()

        payload = assert_bytes(await service.exports(), ZIP)

        # Two entries per sandbox, named after it, in the order of the listing of
        # requirement 19.11: by creation time, then by sandbox_id (requirement 19.14).
        archived = entries(payload)
        assert list(archived) == [
            f"{first.sandbox_id}/{TRACE_FILENAME}",
            f"{first.sandbox_id}/{DIFF_FILENAME}",
            f"{second.sandbox_id}/{TRACE_FILENAME}",
            f"{second.sandbox_id}/{DIFF_FILENAME}",
        ]
        # And the content of each entry is what the two interfaces answer for that sandbox.
        for session in (first, second):
            trace = await service.trace(session.sandbox_id)
            diff = await service.diff(session.sandbox_id)
            assert archived[f"{session.sandbox_id}/{TRACE_FILENAME}"] == trace.content
            assert archived[f"{session.sandbox_id}/{DIFF_FILENAME}"] == diff.content


@pytest.mark.anyio
@pytest.mark.req("19.14")
async def test_the_export_keeps_the_sandboxes_created_inside_the_range(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        made = [await service.prebuilt() for _ in range(3)]
        times = [
            assert_json(await service.metadata(sandbox_id), httpx.codes.OK)["created_at"]
            for sandbox_id in made
        ]

        async def exported(**body: str) -> list[str]:
            payload = assert_bytes(await service.exports(body), ZIP)
            return sorted({name.split("/")[0] for name in entries(payload)})

        # Both ends are inclusive and an end that was not given does not limit
        # (requirement 19.14); a range that holds no sandbox gives a zip with no entries.
        assert await exported() == sorted(made)
        assert await exported(created_from=times[1]) == sorted(made[1:])
        assert await exported(created_to=times[1]) == sorted(made[:2])
        assert await exported(created_from=times[1], created_to=times[1]) == [made[1]]
        assert await exported(created_from=times[2], created_to=times[0]) == []


@pytest.mark.anyio
@pytest.mark.req("19.14")
async def test_an_export_of_no_sandbox_is_a_zip_without_entries(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        payload = assert_bytes(await service.exports(), ZIP)

        # A service with no sandbox at all: a zip file, and an empty one
        # (requirement 19.14).
        assert entries(payload) == {}
        assert payload.startswith(b"PK")


@pytest.mark.anyio
@pytest.mark.req("19.14")
async def test_two_exports_of_the_same_sandboxes_are_the_same_bytes(
    env: Env, tmp_path: Path
) -> None:
    async with service_over(env, tmp_path) as service:
        await service.worked()
        await service.worked()

        first = assert_bytes(await service.exports(), ZIP)
        second = assert_bytes(await service.exports(), ZIP)

        # An export of unchanged sandboxes is the same file twice (requirement 24.2 read
        # for the zip of requirement 19.14).
        assert first == second
        # Which holds because nothing of an entry is taken from the machine: the timestamp
        # is the earliest the format can write, the mode and the host system are fixed and
        # every entry is deflated at one level. A zip entry written from an arcname instead
        # would carry the local time of the moment it was written.
        with zipfile.ZipFile(BytesIO(first)) as archive:
            for info in archive.infolist():
                assert info.date_time == (1980, 1, 1, 0, 0, 0)
                assert info.create_system == 3
                assert info.external_attr >> 16 == 0o644
                assert info.compress_type == zipfile.ZIP_DEFLATED


@pytest.mark.anyio
@pytest.mark.req("19.17")
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"created_from": "2026-10-09"}, id="time-without-offset"),
        pytest.param({"created_to": "nonsense"}, id="not-a-time"),
        pytest.param({"status": "active"}, id="undefined-key"),
        pytest.param({"created_from": 1}, id="wrong-type"),
    ],
)
async def test_an_export_body_that_does_not_fit_the_model_is_a_bad_request(
    env: Env, tmp_path: Path, body: Mapping[str, Any]
) -> None:
    async with service_over(env, tmp_path) as service:
        await service.prebuilt()

        response = await service.exports(body)

        # The bounds are instants with a UTC offset and nothing else is a parameter of the
        # interface (requirement 19.17).
        assert_error(response, httpx.codes.BAD_REQUEST, AdminError.BAD_REQUEST)


# --- the 404 of requirement 19.15 --------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.15")
@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        pytest.param("POST", "reset", None, id="reset"),
        pytest.param("POST", "snapshots", {"name": SAVED}, id="snapshots"),
        pytest.param("POST", "restore", {"name": SAVED}, id="restore"),
        pytest.param("GET", "trace", None, id="trace"),
        pytest.param("GET", "diff", None, id="diff"),
    ],
)
async def test_a_sandbox_id_that_is_not_a_sandbox_is_not_found(
    env: Env, tmp_path: Path, method: str, suffix: str, body: Any
) -> None:
    async with service_over(env, tmp_path) as service:
        kept = await service.prebuilt()

        response = await service.client.request(
            method,
            f"{SANDBOXES}/{MISSING_ID}/{suffix}",
            content=None if body is None else json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )

        assert MISSING_ID in assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)
        assert service.sandbox(kept).status == "active"


@pytest.mark.anyio
@pytest.mark.req("19.15")
async def test_a_deleted_sandbox_is_not_found_from_then_on(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        sandbox_id = session.sandbox_id
        assert (await service.client.delete(f"{SANDBOXES}/{sandbox_id}")).status_code == (
            httpx.codes.NO_CONTENT
        )

        # The sandbox, its trace and its diff are gone with its directory (requirement
        # 19.13), so every interface of this file answers 404 (requirement 19.15).
        for response in (
            await service.reset(sandbox_id),
            await service.save(sandbox_id),
            await service.restore(sandbox_id),
            await service.trace(sandbox_id),
            await service.diff(sandbox_id),
        ):
            assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)
        # The export of a service without sandboxes is an empty zip, not an error.
        assert entries(assert_bytes(await service.exports(), ZIP)) == {}


@pytest.mark.anyio
@pytest.mark.req("19.15")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", "reset", id="reset-is-post-only"),
        pytest.param("GET", "snapshots", id="snapshots-is-post-only"),
        pytest.param("GET", "restore", id="restore-is-post-only"),
        pytest.param("POST", "trace", id="trace-is-get-only"),
        pytest.param("POST", "diff", id="diff-is-get-only"),
        pytest.param("DELETE", "reset", id="reset-is-not-deletable"),
        pytest.param("GET", "trace/extra", id="extra-segment"),
        pytest.param("GET", "snapshots/x", id="no-snapshot-of-its-own"),
    ],
)
async def test_a_method_and_path_of_no_interface_are_not_found(
    env: Env, tmp_path: Path, method: str, path: str
) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        before = sheet_ids(session.sandbox)

        response = await service.client.request(method, f"{SANDBOXES}/{session.sandbox_id}/{path}")

        # 404 and not the 405 a framework router would write for a path that exists with
        # another method (requirement 19.15); nothing of the sandbox was touched.
        assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)
        assert sheet_ids(session.sandbox) == before


@pytest.mark.anyio
@pytest.mark.req("19.15")
@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_the_export_is_post_only(env: Env, tmp_path: Path, method: str) -> None:
    async with service_over(env, tmp_path) as service:
        response = await service.client.request(method, EXPORTS)

        assert_error(response, httpx.codes.NOT_FOUND, AdminError.NOT_FOUND)


# --- the six interfaces of this task are all served (requirement 19.1) -------------------


@pytest.mark.anyio
@pytest.mark.req("19.4", "19.5", "19.6", "19.7", "19.8", "19.14")
async def test_the_six_state_and_export_interfaces_are_served(env: Env, tmp_path: Path) -> None:
    async with service_over(env, tmp_path) as service:
        session = await service.worked()
        sandbox_id = session.sandbox_id
        name: Any = {"name": SAVED}
        served: list[tuple[str, str, Any, int]] = [
            ("POST", f"{SANDBOXES}/{sandbox_id}/snapshots", name, httpx.codes.CREATED),
            ("POST", f"{SANDBOXES}/{sandbox_id}/reset", None, httpx.codes.OK),
            ("POST", f"{SANDBOXES}/{sandbox_id}/restore", name, httpx.codes.OK),
            ("GET", f"{SANDBOXES}/{sandbox_id}/trace", None, httpx.codes.OK),
            ("GET", f"{SANDBOXES}/{sandbox_id}/diff", None, httpx.codes.OK),
            ("POST", EXPORTS, None, httpx.codes.OK),
        ]

        # Each pair answers its own status, in an order one sandbox allows: a snapshot
        # exists before it is restored, and the reset in between gives the restore
        # something to put back (requirement 19.1).
        for method, path, body, status in served:
            response = await service.client.request(
                method,
                path,
                content=None if body is None else json.dumps(body).encode(),
                headers={"content-type": "application/json"},
            )
            assert response.status_code == status, f"{method} {path}: {response.text}"


@pytest.mark.req("19.14")
def test_the_diff_file_name_is_the_one_the_stdio_runtime_writes() -> None:
    # The Admin API writes the diff of an export under the name the stdio runtime writes
    # its own file under, and the name is written in both places (admin docstring).
    assert DIFF_FILENAME == STDIO_DIFF_FILENAME
