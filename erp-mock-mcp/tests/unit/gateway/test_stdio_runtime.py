"""The life of one stdio process: its sandbox, its trace and the diff its exit writes.

:mod:`erp_mock.gateway.stdio` is exercised the way a client reaches it, with memory
streams in place of stdin and stdout (:func:`stdio_over`): ``run_stdio`` is given the two
streams ``stdio_server`` would have handed it, the official ``ClientSession`` talks on the
other end, and a tap records every message the service wrote - which is exactly what the
stdout writer of the real transport would have written (requirement 22.2). Everything a
test then asserts is on disk or on the wire: the events of the sandbox's trace.jsonl, the
``diff.json`` of the exit, the status of the sandbox and the notice on stderr.

The situations of task 22.4, one test each:

- the whole life of a session: initialize, tools/list, one write call, stdin EOF, and the
  four events the trace holds afterwards (requirements 17.9 and 22.4);
- the exit: the sandbox frozen and ``<sandbox>/diff.json`` holding the net change of the
  run against the initial dataset, with the anchor date ``--anchor-date`` asked for
  (requirements 18.1 and 22.4);
- the notice on stderr, and stdout carrying MCP messages and nothing else
  (requirements 22.2 and 22.3);
- a second initialize: the same sandbox and one more ``session_start``
  (requirement 15.2);
- a Snapshot that will not build: JSON-RPC -32603, no sandbox, no diff
  (requirement 15.14);
- the two ways out besides EOF, :meth:`~erp_mock.gateway.stdio.StdioRuntime.stop` and a
  real SIGTERM, and a process that never received an initialize at all
  (requirement 22.4);
- :func:`~erp_mock.gateway.stdio.run_stdio` itself, which is what the command calls and
  which gives back the sandbox of the process (design.md 7.3).

The business configuration is the engine test fixture (:mod:`tests.helpers.fixtures`), not
the example instance, and the Snapshot its sandboxes are created from is built once for the
module from the standard basic data of :mod:`tests.helpers.engine_env`, so the one write
call of this file has a depot and an article to point at. It reaches the manager through
:class:`Snapshots`, a double of the
:class:`~erp_mock.gateway.sandbox.SnapshotSource` the service builds over the Snapshot
cache, and :class:`BrokenSnapshots` is the same double with the build of
requirement 15.14 failing.

No test reads the system clock (requirement 25.10): the manager is given a
:class:`~erp_mock.gateway.clock.SystemClock` over :data:`INSTANT` and :class:`Ticker`, a
monotonic source that advances by a fixed step per reading.

Why the signal path is reached twice. ``anyio.open_signal_receiver`` registers with
``loop.add_signal_handler`` and so needs the main thread (tests/probes/test_uvicorn.py),
and a SIGINT sent here would be a SIGINT sent to pytest, which is why the runtime has
:meth:`~erp_mock.gateway.stdio.StdioRuntime.stop` - the door the watcher itself calls - and
why the one test that does send a signal sends SIGTERM and only after an initialize has
been answered, by which time the watcher is installed (``serve`` reports the watcher
started before it reads a message). Every other test leaves the handlers of the runner
alone by passing no signals at all.
"""

import json
import signal
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Final

import anyio
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage

from erp_mock.common.jsonio import dumps_pretty
from erp_mock.config.instance import DatasetDef
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import SessionHandlers
from erp_mock.gateway.sandbox import SandboxManager, SandboxRuntime
from erp_mock.gateway.stdio import (
    DIFF_FILENAME,
    SANDBOX_NOTICE,
    SESSION_ID_PREFIX,
    SNAPSHOT_FAILED_MESSAGE,
    StdioOptions,
    StdioRuntime,
    StdioStreams,
    run_stdio,
)
from erp_mock.gateway.trace import TRACE_FILENAME, EventName
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.loader.yaml_io import load_yaml
from erp_mock.state.diff import INITIAL_BASE
from erp_mock.state.schema import StorageSchema, build_schema
from erp_mock.state.snapshot import build_snapshot_db, finalize_snapshot
from erp_mock.tools.layer import ToolLayer
from tests.helpers.engine_env import CURRENT_USER, create_basic_data
from tests.helpers.fixtures import ANCHOR_DATE, DATASET, INSTANCE_DIR, fixture_loaded

# Upper bound for every test: a hang fails the test instead of blocking the run.
TIMEOUT_S: Final = 20

# The instant the first reading of the test clock returns; the nth reading is this plus
# n - 1 seconds, so the ``ts`` of two events differ and neither is the system time.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# How far the monotonic source moves per reading (requirement 25.10).
STEP_SECONDS: Final = 0.25

# The definition file of the initial dataset of the fixture, relative to its instance
# directory, as loading step 4 reads it.
DATASET_FILE: Final = f"datasets/{DATASET}.yaml"

# The date ``--anchor-date`` asks for in the one test that gives it: not the fixed
# ``anchor_date`` of the fixture's clock configuration, so the priority of
# requirement 16.1 is visible in the sandbox and in its diff.
ASKED_ANCHOR: Final = date(2026, 11, 20)

# The tool the one write call of this file calls, the tables it writes and the records of
# the basic data it names (:data:`~tests.helpers.engine_env.BASIC_DATA`).
COUNT_CREATE: Final = "count_sheet_create"
COUNT_SHEET: Final = "count_sheet"
COUNT_SHEET_LINE: Final = "count_sheet_line"
DEPOT: Final = "dep_000001"
WASHER: Final = "art_000002"

# That call: the two required header fields and one line (requirement 12.5 accepts the
# quantity as a decimal string).
NEW_COUNT: Final[Mapping[str, Any]] = {
    "depot_id": DEPOT,
    "count_kind": "gain",
    "lines": [{"article_id": WASHER, "diff_qty": "3"}],
}

# The keys of one change of a diff and of the diff itself, in the order of
# design.md data model 5 (requirements 18.1 and 18.6).
INSERT_KEYS: Final = ["entity", "op", "id", "cause_seqs", "after"]
DIFF_KEYS: Final = ["sandbox_id", "instance", "dataset", "anchor_date", "base", "changes"]

# What the double of the Snapshot source says when the build of requirement 15.14 fails;
# the real one is a :class:`~erp_mock.seed.resolve.SeedError` of the Snapshot cache, and
# the runtime treats either the same way: what it reports is that the creation failed, not
# which exception said so.
BROKEN_REASON: Final = "INV-1 不成立：夹具快照构建失败"

# The JSON-RPC code an initialize whose sandbox cannot be created is answered with
# (requirement 15.14).
INTERNAL_ERROR: Final = -32603


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- the test clock (requirement 25.10) --------------------------------------------------


@dataclass(slots=True)
class Ticker:
    """A monotonic source that advances by ``step`` per reading, driven by the test."""

    step: float = STEP_SECONDS
    base: float = 1000.0
    reads: int = 0

    def __call__(self) -> float:
        value = self.base + self.step * self.reads
        self.reads += 1
        return value


def stepping_clock(ticker: Ticker) -> SystemClock:
    """A clock whose nth instant is :data:`INSTANT` plus n - 1 seconds, over ``ticker``."""
    readings = iter(range(1 << 20))
    return SystemClock(now=lambda: INSTANT + timedelta(seconds=next(readings)), monotonic=ticker)


# --- the Snapshot every sandbox is created from ------------------------------------------


class SnapshotBroken(Exception):
    """What the Snapshot source of :class:`BrokenSnapshots` raises (requirement 15.14)."""


@dataclass(slots=True)
class Snapshots:
    """A :class:`~erp_mock.gateway.sandbox.SnapshotSource` over one Snapshot file.

    ``asked`` records the (dataset, anchor date) of every creation, which is what the
    service's own source would have looked up in the Snapshot cache or built.
    """

    db: Path
    asked: list[tuple[str, date]] = field(default_factory=list)

    @contextmanager
    def snapshot(self, dataset: DatasetModel, anchor: date) -> Iterator[Path]:
        self.asked.append((dataset.name, anchor))
        yield self.db


@dataclass(slots=True)
class BrokenSnapshots:
    """A Snapshot source whose build fails, as requirement 15.14 describes.

    The failure is raised by the call itself rather than inside the block, which is where
    a build that cannot be carried out fails: the Sandbox_Manager calls this while it is
    making the sandbox directory and removes that directory again on the way out.
    """

    asked: list[tuple[str, date]] = field(default_factory=list)

    def snapshot(self, dataset: DatasetModel, anchor: date) -> AbstractContextManager[Path]:
        self.asked.append((dataset.name, anchor))
        raise SnapshotBroken(BROKEN_REASON)


def build_snapshot(directory: Path, schema: StorageSchema, layer: ToolLayer) -> Path:
    """Build the one Snapshot of this file below ``directory`` and return its path.

    A build database with the tables of the fixture and the standard basic data of
    :mod:`tests.helpers.engine_env` in it, created through the engine the way a seed build
    creates them (requirement 20.17), and written out as a Snapshot.
    """
    build = directory / "build.sqlite"
    snapshot = directory / "snapshot.sqlite"
    store = build_snapshot_db(build, schema)
    try:
        create_basic_data(layer.engine, store, anchor_date=ANCHOR_DATE)
    finally:
        store.close()
    finalize_snapshot(build, snapshot)
    build.unlink()
    return snapshot


# --- the instance configuration under test -----------------------------------------------


@dataclass(frozen=True, slots=True)
class Env:
    """What every test of this file starts from, built once for the module."""

    loaded: LoadedInstance
    schema: StorageSchema
    layer: ToolLayer
    dataset: DatasetModel
    snapshot: Path

    @property
    def profile(self) -> ProfileModel:
        """The one profile of the fixture instance, whose tools a session sees."""
        return self.loaded.default_profile

    @property
    def instance_ref(self) -> str:
        """The instance as ``<name>@<version>``, as the diff names it (requirement 18.1)."""
        return f"{self.loaded.instance.instance}@{self.loaded.instance.version}"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Env:
    """The fixture instance, its tools layer and one Snapshot of its initial dataset."""
    loaded = fixture_loaded()
    layer = ToolLayer(loaded)
    schema = build_schema(loaded.domain)
    definition = DatasetDef.model_validate(load_yaml(INSTANCE_DIR / DATASET_FILE))
    dataset = DatasetModel(definition=definition, file=DATASET_FILE, current_user=CURRENT_USER)
    return Env(
        loaded=loaded,
        schema=schema,
        layer=layer,
        dataset=dataset,
        snapshot=build_snapshot(tmp_path_factory.mktemp("snapshot"), schema, layer),
    )


# --- the service one stdio process runs on ------------------------------------------------


@dataclass(slots=True)
class Service:
    """A Sandbox_Manager over a data directory of its own, and what it was given."""

    env: Env
    manager: SandboxManager
    snapshots: Snapshots | BrokenSnapshots

    def runtime(
        self,
        streams: StdioStreams,
        *,
        anchor_date: date | None = None,
        notices: StringIO | None = None,
        signals: Sequence[signal.Signals] = (),
    ) -> StdioRuntime:
        """The runtime of one stdio process over ``streams`` (task 24.4 builds this)."""
        return StdioRuntime(
            loaded=self.env.loaded,
            schema=self.env.schema,
            manager=self.manager,
            handlers=SessionHandlers(self.manager, self.env.layer),
            options=StdioOptions(
                profile=self.env.profile, dataset=self.env.dataset, anchor_date=anchor_date
            ),
            streams=streams,
            notices=notices,
            signals=signals,
        )


@contextmanager
def service_over(env: Env, data_dir: Path, *, broken: bool = False) -> Iterator[Service]:
    """Yield a manager over ``data_dir``, closing it and its thread pool on the way out.

    ``broken`` takes the Snapshot source whose build fails (requirement 15.14).
    """
    snapshots = BrokenSnapshots() if broken else Snapshots(env.snapshot)
    manager = SandboxManager(
        data_dir=data_dir,
        loaded=env.loaded,
        schema=env.schema,
        snapshots=snapshots,
        clock=stepping_clock(Ticker()),
    )
    try:
        yield Service(env=env, manager=manager, snapshots=snapshots)
    finally:
        manager.close()


# --- one stdio process, over memory streams instead of stdin and stdout -------------------


@dataclass(slots=True)
class Live:
    """A stdio process that is running, and what a test reads off it."""

    client: ClientSession
    runtime: StdioRuntime
    sent: list[SessionMessage]

    async def call(self, tool: str, args: Mapping[str, Any]) -> types.CallToolResult:
        """Send one tools/call and parse its result.

        ``ClientSession.call_tool`` is avoided on purpose: it issues a tools/list of its
        own and validates structuredContent on the client side, neither of which is part
        of what the service answered.
        """
        request = types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(name=tool, arguments=dict(args))
            )
        )
        return await self.client.send_request(request, types.CallToolResult)

    def wire_messages(self) -> list[dict[str, Any]]:
        """Every message the service wrote, as the stdio transport serializes it."""
        return [
            json.loads(message.message.model_dump_json(by_alias=True, exclude_none=True))
            for message in self.sent
        ]


async def tap(
    source: MemoryObjectReceiveStream[SessionMessage],
    sink: MemoryObjectSendStream[SessionMessage | Exception],
    record: list[SessionMessage],
) -> None:
    """Forward every message from ``source`` to ``sink``, recording it first."""
    async with source, sink:
        async for message in source:
            record.append(message)
            await sink.send(message)


@asynccontextmanager
async def stdio_over(
    service: Service,
    *,
    anchor_date: date | None = None,
    notices: StringIO | None = None,
    signals: Sequence[signal.Signals] = (),
    initialize: bool = True,
) -> AsyncIterator[Live]:
    """One stdio process over memory streams, with the official client on the other end.

    The streams are the pair ``stdio_server`` hands over, made here so that the test owns
    the client's end: leaving the block ends the client session, which closes both of its
    streams, and that is stdin reaching EOF for the service (requirement 22.4). The block
    returns once ``run_stdio`` has written its exit, so a test reads the trace and the diff
    after it.

    ``initialize`` says whether the handshake is done before the block body - a process
    that never received an initialize is one of the cases of requirement 22.4.
    """
    to_service, service_read = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    to_client, client_read = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    service_write, tapped = anyio.create_memory_object_stream[SessionMessage](1)
    recorded: list[SessionMessage] = []
    runtime = service.runtime(
        (service_read, service_write), anchor_date=anchor_date, notices=notices, signals=signals
    )
    with anyio.fail_after(TIMEOUT_S):
        async with anyio.create_task_group() as recorder:
            recorder.start_soon(tap, tapped, to_client, recorded)
            async with anyio.create_task_group() as process:
                process.start_soon(runtime.serve)
                async with ClientSession(client_read, to_service) as client:
                    if initialize:
                        await client.initialize()
                    yield Live(client=client, runtime=runtime, sent=recorded)
                # The client session closed its write stream on the way out, which is the
                # EOF the service exits on; closing it again is a no-op and says so here.
                await to_service.aclose()
            # The process has written its exit. The streams are the test's, so the test
            # closes the one the service wrote to, which ends the tap; the service closes
            # it as well, whichever of the two gets there first.
            await service_write.aclose()


def events_of(sandbox: SandboxRuntime) -> list[dict[str, Any]]:
    """The events of the sandbox's trace file, one per line (requirement 17.1)."""
    path = sandbox.directory / TRACE_FILENAME
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def diff_of(sandbox: SandboxRuntime) -> dict[str, Any]:
    """The ``diff.json`` the exit wrote, parsed (requirements 18.1 and 22.4)."""
    payload = json.loads((sandbox.directory / DIFF_FILENAME).read_bytes())
    assert isinstance(payload, dict), payload
    return payload


def sandbox_dirs(manager: SandboxManager) -> list[Path]:
    """The sandbox directories below the data directory, none where there are none.

    What requirement 15.14 asks after a creation that failed: no directory left behind,
    whether the manager got as far as making ``sandboxes/`` or not.
    """
    root = manager.root
    return sorted(root.iterdir()) if root.is_dir() else []


def sandbox_of(live: Live) -> SandboxRuntime:
    """The sandbox of the process; fails the test where there is none."""
    sandbox = live.runtime.sandbox
    assert sandbox is not None
    return sandbox


def only[T](items: Sequence[T]) -> T:
    """The one item of ``items``; fails the test when there is not exactly one."""
    assert len(items) == 1, items
    return items[0]


def text_of(result: types.CallToolResult) -> dict[str, Any]:
    """The one text item of a tool result, parsed (requirements 13.1 and 13.6)."""
    item = only(result.content)
    assert isinstance(item, types.TextContent), item
    payload = json.loads(item.text)
    assert isinstance(payload, dict), payload
    return payload


# --- the whole life of one session (requirements 17.9 and 22.4) ---------------------------


@pytest.mark.anyio
@pytest.mark.req("22.4")
async def test_the_life_of_a_session_is_its_start_its_calls_and_its_end(
    env: Env, tmp_path: Path
) -> None:
    with service_over(env, tmp_path) as service:
        async with stdio_over(service) as live:
            listed = await live.client.list_tools()
            result = await live.call(COUNT_CREATE, NEW_COUNT)
            sandbox = sandbox_of(live)

            assert tuple(tool.name for tool in listed.tools) == env.profile.tool_names
            assert result.isError is False
            # The process asked its Snapshot source for the dataset of its options at the
            # anchor date of the fixture's fixed clock (requirements 15.2 and 16.1).
            assert service.snapshots.asked == [(env.dataset.name, ANCHOR_DATE)]

        # The four events of the run, in the order they happened: the initialize wrote
        # session_start, the two calls their own events, and the EOF wrote session_end
        # (requirements 17.9 and 22.4).
        events = events_of(sandbox)
        assert [event["event"] for event in events] == [
            EventName.SESSION_START,
            EventName.TOOLS_LIST,
            EventName.TOOLS_CALL,
            EventName.SESSION_END,
        ]
        # One sandbox, one session: every event carries the process's session id and the
        # seq rises by one per event (requirement 17.2).
        assert [event["seq"] for event in events] == [1, 2, 3, 4]
        assert {event["session_id"] for event in events} == {live.runtime.session_id}
        assert live.runtime.session_id.startswith(SESSION_ID_PREFIX)
        assert {event["sandbox_id"] for event in events} == {sandbox.sandbox_id}
        assert sandbox.session_ids == [live.runtime.session_id]


# --- the exit (requirements 18.1 and 22.4) ------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("18.1", "22.4")
async def test_the_exit_freezes_the_sandbox_and_writes_the_diff_of_the_run(
    env: Env, tmp_path: Path
) -> None:
    with service_over(env, tmp_path) as service:
        # The anchor date of the sandbox is the one ``--anchor-date`` asked for, which wins
        # over the fixed date of the clock configuration (requirement 16.1).
        async with stdio_over(service, anchor_date=ASKED_ANCHOR) as live:
            result = await live.call(COUNT_CREATE, NEW_COUNT)
            sandbox = sandbox_of(live)
            sheet_id = text_of(result)["id"]
            # The seq of the call that wrote the records, which their ``cause_seqs`` names
            # (requirement 18.3); session_start took the seq before it.
            written = events_of(sandbox)
            call_seq = only([e for e in written if e["event"] == EventName.TOOLS_CALL])["seq"]

        # The session of an automatically created sandbox has ended, so the sandbox is
        # frozen and stays read-only (requirements 15.7 and 15.12).
        assert sandbox.status == "frozen"
        assert sandbox.anchor_date == ASKED_ANCHOR

        # The diff of requirement 18.1: the six keys in order, the basis of the initial
        # dataset, and the instance, dataset and anchor date of this sandbox.
        payload = diff_of(sandbox)
        assert list(payload) == DIFF_KEYS
        assert payload["sandbox_id"] == sandbox.sandbox_id
        assert payload["instance"] == env.instance_ref
        assert payload["dataset"] == sandbox.dataset_ref
        assert payload["anchor_date"] == ASKED_ANCHOR.isoformat()
        assert payload["base"] == INITIAL_BASE

        # The net change of the one call: the bill and its line, each an insert caused by
        # the call that wrote it (requirements 18.2, 18.3 and 18.6).
        changes = payload["changes"]
        assert [(change["entity"], change["op"], change["id"]) for change in changes] == [
            (COUNT_SHEET, "insert", sheet_id),
            (COUNT_SHEET_LINE, "insert", f"{sheet_id}-1"),
        ]
        for change in changes:
            assert list(change) == INSERT_KEYS
            assert change["cause_seqs"] == [call_seq]
            # The fields of an insert are ordered by name and carry no id of their own
            # (requirements 18.5, 18.6 and 18.9).
            assert list(change["after"]) == sorted(change["after"])
            assert "id" not in change["after"]
        header = changes[0]["after"]
        assert header["depot_id"] == DEPOT
        # The business date of the call is the anchor date of the sandbox (req 16.2).
        assert header["biz_date"] == ASKED_ANCHOR.isoformat()

        # The text is the one the Admin API's diff endpoint sends: pretty JSON, no
        # trailing newline, the key order of the object as it was built (requirement 24.2).
        written_diff = (sandbox.directory / DIFF_FILENAME).read_text(encoding="utf-8")
        assert written_diff == dumps_pretty(payload)


# --- what the process says, and where (requirements 22.2 and 22.3) ------------------------


@pytest.mark.anyio
@pytest.mark.req("22.3")
async def test_the_new_sandbox_is_reported_on_stderr_and_stdout_stays_clean(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with service_over(env, tmp_path) as service:
        # No ``notices`` stream, so the runtime writes to the process's stderr, which is
        # what the command does (requirement 22.3).
        async with stdio_over(service) as live:
            await live.client.send_ping()
            sandbox = sandbox_of(live)

    captured = capsys.readouterr()
    # The notice names the sandbox and the absolute path of its directory, which is where
    # its trace.jsonl and its diff.json are.
    assert captured.err.splitlines() == [
        SANDBOX_NOTICE.format(sandbox_id=sandbox.sandbox_id, directory=sandbox.directory.resolve())
    ]
    assert sandbox.sandbox_id in captured.err
    assert str(sandbox.directory.resolve()) in captured.err
    assert (sandbox.directory / DIFF_FILENAME).is_file()

    # Nothing of the service reached the process's stdout: the only writer of it is the
    # SDK's stdout writer, which here is the injected stream (requirement 22.2).
    assert captured.out == ""
    # And what went to that stream are MCP messages alone: a response per request, each
    # carrying the id it answers and no method of its own (requirement 14.11).
    written = live.wire_messages()
    assert [message["jsonrpc"] for message in written] == ["2.0"] * 2
    assert [sorted(message) for message in written] == [["id", "jsonrpc", "result"]] * 2


# --- a second initialize (requirement 15.2) -----------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.2")
async def test_a_second_initialize_keeps_the_sandbox_and_writes_one_more_start(
    env: Env, tmp_path: Path
) -> None:
    with service_over(env, tmp_path) as service:
        async with stdio_over(service) as live:
            sandbox = sandbox_of(live)

            # The handshake again, on the same process: the second initialize is answered
            # like the first one.
            again = await live.client.initialize()

            # And it creates no second sandbox: the Snapshot source was asked once, the
            # sandbox of the process is the one the first initialize made, and this
            # process holds no other (requirement 15.2).
            assert again.instructions is not None
            assert service.snapshots.asked == [(env.dataset.name, ANCHOR_DATE)]
            assert live.runtime.sandbox is sandbox
            assert only(service.manager.sandboxes()) is sandbox

        # One session_start per accepted initialize, both on that sandbox and under the
        # one session id of the process, and one session_end at the exit.
        assert [event["event"] for event in events_of(sandbox)] == [
            EventName.SESSION_START,
            EventName.SESSION_START,
            EventName.SESSION_END,
        ]
        assert sandbox.session_ids == [live.runtime.session_id]


# --- a Snapshot that will not build (requirement 15.14) -----------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.14")
async def test_an_initialize_whose_snapshot_will_not_build_is_answered_with_32603(
    env: Env, tmp_path: Path
) -> None:
    with service_over(env, tmp_path, broken=True) as service:
        async with stdio_over(service, initialize=False) as live:
            with pytest.raises(McpError) as raised:
                await live.client.initialize()

            # The code is the filter's and the message names the initial dataset and the
            # anchor date, and nothing of the reason (requirement 15.14).
            assert raised.value.error.code == INTERNAL_ERROR
            assert raised.value.error.message == SNAPSHOT_FAILED_MESSAGE.format(
                dataset=env.dataset.name, anchor_date=ANCHOR_DATE.isoformat()
            )
            assert BROKEN_REASON not in raised.value.error.message

            # No session and no sandbox: the registry is empty and the directory the
            # creation had started is gone again (requirements 15.14 and 19.19).
            assert live.runtime.sandbox is None
            assert service.manager.sandboxes() == ()
            assert sandbox_dirs(service.manager) == []

    # And the exit of a process without a sandbox writes neither a trace nor a diff.
    assert sandbox_dirs(service.manager) == []


# --- the two ways out besides EOF (requirement 22.4) --------------------------------------


@pytest.mark.anyio
@pytest.mark.req("22.4")
async def test_stopping_the_process_writes_the_same_exit_as_an_eof(
    env: Env, tmp_path: Path
) -> None:
    with service_over(env, tmp_path) as service:
        async with stdio_over(service) as live:
            await live.call(COUNT_CREATE, NEW_COUNT)
            sandbox = sandbox_of(live)

            # The door the signal watcher itself calls (module docstring): it cancels the
            # session, and the exit runs afterwards, outside the cancelled scope.
            live.runtime.stop()

        assert [event["event"] for event in events_of(sandbox)][-1] == EventName.SESSION_END
        assert sandbox.status == "frozen"
        assert diff_of(sandbox)["base"] == INITIAL_BASE
        assert len(diff_of(sandbox)["changes"]) == 2


@pytest.mark.anyio
@pytest.mark.req("22.4")
async def test_a_sigterm_ends_the_session_through_the_signal_watcher(
    env: Env, tmp_path: Path
) -> None:
    with service_over(env, tmp_path) as service:
        # SIGTERM alone: the handler of SIGINT belongs to the test runner (module
        # docstring). The initialize has been answered by the time the body runs, so the
        # watcher is installed - ``serve`` starts it before it reads a message.
        async with stdio_over(service, signals=(signal.SIGTERM,)) as live:
            sandbox = sandbox_of(live)

            signal.raise_signal(signal.SIGTERM)

        assert [event["event"] for event in events_of(sandbox)] == [
            EventName.SESSION_START,
            EventName.SESSION_END,
        ]
        assert sandbox.status == "frozen"
        # Nothing was called, so the sandbox holds what its Snapshot held
        # (requirement 18.7).
        assert diff_of(sandbox)["changes"] == []


@pytest.mark.anyio
@pytest.mark.req("22.4")
async def test_a_process_that_never_received_an_initialize_writes_nothing(
    env: Env, tmp_path: Path
) -> None:
    with service_over(env, tmp_path) as service:
        notices = StringIO()
        # A client that connects and hangs up again: no initialize, so no sandbox and no
        # session, and the service has nothing of its own to answer (module docstring).
        async with stdio_over(service, notices=notices, initialize=False) as live:
            assert live.runtime.sandbox is None

        # The exit has nothing to end, to freeze or to compare, and it says nothing.
        assert live.runtime.sandbox is None
        assert service.manager.sandboxes() == ()
        assert sandbox_dirs(service.manager) == []
        assert notices.getvalue() == ""


# --- what the command calls (design.md 7.3) -----------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.2")
async def test_run_stdio_serves_one_session_and_returns_its_sandbox(
    env: Env, tmp_path: Path
) -> None:
    with service_over(env, tmp_path) as service:
        to_service, service_read = anyio.create_memory_object_stream[SessionMessage | Exception](1)
        to_client, client_read = anyio.create_memory_object_stream[SessionMessage | Exception](1)
        service_write, tapped = anyio.create_memory_object_stream[SessionMessage](1)
        notices = StringIO()
        served: list[SandboxRuntime | None] = []

        async def serve() -> None:
            served.append(
                await run_stdio(
                    loaded=env.loaded,
                    schema=env.schema,
                    manager=service.manager,
                    handlers=SessionHandlers(service.manager, env.layer),
                    options=StdioOptions(profile=env.profile, dataset=env.dataset),
                    streams=(service_read, service_write),
                    notices=notices,
                    signals=(),
                )
            )

        with anyio.fail_after(TIMEOUT_S):
            async with anyio.create_task_group() as tg:
                tg.start_soon(tap, tapped, to_client, [])
                tg.start_soon(serve)
                async with ClientSession(client_read, to_service) as client:
                    await client.initialize()
                await to_service.aclose()

        # The one sandbox of the process, which the command reports and the exit froze.
        sandbox = only(served)
        assert sandbox is not None
        assert only(service.manager.sandboxes()) is sandbox
        assert sandbox.sandbox_id in notices.getvalue()
        assert sandbox.status == "frozen"
        assert [event["event"] for event in events_of(sandbox)] == [
            EventName.SESSION_START,
            EventName.SESSION_END,
        ]
        assert diff_of(sandbox)["sandbox_id"] == sandbox.sandbox_id
