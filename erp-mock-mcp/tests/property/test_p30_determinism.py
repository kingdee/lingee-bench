"""Property 30: the determinism and the replay of a request sequence.

**Validates: Requirements 24.1, 24.2, 24.3, 24.4, 24.11**

For any Request_Sequence of ``instances/p2p-demo`` - tools/list calls and failing
tools/call among it - the sequence decides everything the service hands out, and nothing
else does:

- two sandboxes created from the same initial dataset at the same anchor date answer every
  tools/call with the same ``isError``, the same ``text`` byte for byte and the same
  ``structuredContent``, values and key order alike (requirement 24.1);
- their diffs against ``initial`` are byte for byte the same once the value of
  ``sandbox_id`` is replaced by one string, ``cause_seqs`` included (requirement 24.2);
- their traces hold the same number of events, and the corresponding events are equal and
  carry their keys in the same order once ``ts``, ``latency_ms``, ``sandbox_id`` and
  ``session_id`` are dropped (requirement 24.3);
- a sandbox that is reset and replays the sequence answers each call as it did the first
  time, and its diff equals the one taken before the reset in everything but
  ``cause_seqs``, which now names the replay's calls alone (requirement 24.4);
- a sandbox that is restored to a Named_Snapshot taken in the middle of the sequence and
  replays the part after it answers each of those calls as it did the first time
  (requirement 24.11).

The sequence. The operations are drawn by :mod:`tests.property.opseq`, which reads the
state the earlier ones left behind before each draw and makes three in ten wrong on
purpose, so the sequence reaches submitted, approved and pushed bills and both the
successful and the failing path of an operation come up. The generator works against a
sandbox of its own through the :class:`~erp_mock.tools.layer.ToolLayer` and that sandbox is
thrown away: what the property runs is the list of calls it drew, as a Request_Sequence
through the gateway. One to :data:`MAX_CALLS` tools/call, with one or two tools/list drawn
into the sequence at any position - a tools/list changes nothing but takes a trace event
and a sequence number, which is exactly what makes it worth having in the comparisons.

The execution path. Every call of every sandbox goes through
:class:`~erp_mock.gateway.handlers.SessionHandlers` and the SDK's memory streams, with the
official ``ClientSession`` on the other end (:func:`mcp_session`): a real low-level
``Server`` per session, requests queued on the sandbox lock by
:class:`~erp_mock.gateway.sandbox.SandboxManager` and events written by the sandbox's own
trace writer. What the two transports own - the session id, the ``session_start`` of an
accepted initialize and the ``session_end`` of the exit - is done here the way the stdio
runtime does it (design.md 7.3), so the traces that are compared are the traces of two
finished sessions (requirement 24.3). The diff is taken the way the stdio runtime takes it
(:func:`diff_of`): :func:`~erp_mock.state.diff.compute_diff` against ``base.sqlite``, the
four identifying values of the sandbox, and
:func:`~erp_mock.common.jsonio.dumps_pretty`, which is the serialization the Admin API's
diff endpoint sends as well (design.md 7.7).

The two sandboxes are created in one process, below one data directory, by one
Sandbox_Manager: two addresses, two transports and two processes with different
``--data-dir`` are the integration test of requirement 24.1 (task 26.4). Their session ids
differ on purpose - ``se_...a`` and ``se_...b`` - so an event that carried the session or
the sandbox into a compared field would be caught rather than cancelled out.

The Named_Snapshot is saved in the middle of the first sandbox's one session, at a drawn
position, through :meth:`~erp_mock.gateway.sandbox.SandboxManager.save_named`. It writes no
trace event and does not touch the database (requirement 19.5), which is why the second
sandbox can run the same sequence without saving one and the two traces and diffs still
have to match.

The anchor date is passed in. The clock of p2p-demo is in mode today, so it configures
none: the date reaches the loader and every
:meth:`~erp_mock.gateway.sandbox.SandboxManager.create` as :data:`ANCHOR_DATE`, and the
``ts``, the ``latency_ms`` and the timing of a sandbox come from a monotonic source and an
instant source the test steps itself (:class:`Ticker`). No test of this file reads the
system clock (requirement 25.10).

The resource limits are the ones a plain process has (requirement 23.9), and no call of a
sequence is refused by them, which is the condition requirement 24.1 puts on a
Request_Sequence. A sandbox sees at most :data:`MAX_CALLS` calls per run and three runs,
and a reset puts the count back to 0 (requirement 19.4), so the 500 calls of a sandbox are
far off; the monotonic source moves 0.25 s per reading and a tools/call takes three
readings, which leaves consecutive calls 0.75 s apart and the 20 calls per second of a
session out of reach. The property asserts that no answer was a LIMIT_EXCEEDED of one of
the three resource limits rather than trusting the arithmetic, so a change that brings a
sequence up against a bound fails here instead of quietly comparing refusals.

The cost. One example creates two sandboxes and runs the sequence four times - once in
each sandbox, once after the reset and once more over the tail after the restore - plus the
drawing run through the tools layer and three diffs over every table of p2p-demo. That is
7 to 130 ms per example on the development machine, so the :data:`SEQUENCES` examples of
this module take about 26 seconds of wall clock, the 0.2 seconds of loading p2p-demo and
building the Snapshot of its initial dataset included. The example count is three times the
100 a property of this suite declares by default (tests/conftest.py), which buys back some
of what the short sequences give up: 1 to :data:`MAX_CALLS` calls rather than the 50 of
requirement 25.2, about what one MCP session of a model does between two diffs. What this
property needs from a sequence is that it writes, fails and leaves a state to compare, not
that it is long - the properties that need long sequences (17 to 21, 27) run them through
the tools layer, where a call costs a fraction of one that goes over a transport. Of the
sequences a run draws, about three in four leave a net change behind and three in five hold
a call that failed, which ``--hypothesis-show-statistics`` reports per run.

Reporting a failure. The assertion messages name the step of the sequence, the field that
differs and both values, and print the sequence as it was shrunk - every call with its
arguments - so a counterexample is replayable. The draws shrink towards a short sequence of
sensible operations and towards a Named_Snapshot at the very start, which makes the tail of
requirement 24.11 the whole sequence.
"""

import json
from collections import Counter
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import anyio
import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from erp_mock.common.errors import ErrorCode
from erp_mock.common.jsonio import dumps_pretty, encode_utf8
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import INIT_OPTIONS, McpSessionContext, SessionHandlers
from erp_mock.gateway.sandbox import SandboxManager, SandboxRuntime
from erp_mock.gateway.trace import TRACE_FILENAME, EventName
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.loader.loader import Loader
from erp_mock.seed.builder import SNAPSHOT_FILENAME
from erp_mock.state.diff import compute_diff
from erp_mock.state.schema import StorageSchema, build_schema
from erp_mock.tools.layer import LIMIT_KEY, LIMITS
from erp_mock.tools.results import CODE, DETAILS, ERROR_KEY
from tests.helpers.fixtures import ANCHOR_DATE
from tests.property.opseq import Call, Start, next_call, open_sandbox, start_from

# How many sequences the property runs (module docstring).
SEQUENCES: Final = 300

# How many tools/call one sequence holds, and how many tools/list are drawn into it.
MIN_CALLS: Final = 1
MAX_CALLS: Final = 12
MIN_LISTS: Final = 1
MAX_LISTS: Final = 2

# The example instance of the repository, its business packs and its initial dataset, which
# every sandbox of this file is created from (requirement 21.2).
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
P2P_INSTANCE_DIR: Final = REPO_ROOT / "instances" / "p2p-demo"
P2P_PACKS_DIR: Final = REPO_ROOT / "packs"
P2P_DATASET: Final = "purchase-basic"

# The sessions of one example, one per run of the sequence. The two that run the sequence
# for the first time differ in their last character, which is what requirement 24.3 asks to
# be dropped from the comparison of the traces.
SESSION_A: Final = "se_0123456789abcdea"
SESSION_B: Final = "se_0123456789abcdeb"
SESSION_RESET: Final = "se_0123456789abcdec"
SESSION_RESTORED: Final = "se_0123456789abcded"

# The Named_Snapshot the middle of the first run saves (requirement 19.20).
NAMED: Final = "mid"

# What the value of ``sandbox_id`` is replaced by before two diffs are compared
# (requirement 24.2). The ids of the records of a sandbox carry no sandbox id, so replacing
# the id in the text replaces exactly the one value the key holds.
SANDBOX_PLACEHOLDER: Final = "sb_0000000000000000"

# The keys requirement 24.3 drops from a trace event before two of them are compared.
VOLATILE_KEYS: Final[frozenset[str]] = frozenset({"ts", "latency_ms", "sandbox_id", "session_id"})

# The key of a diff change requirement 24.4 leaves out of the comparison after a reset.
CAUSE_SEQS: Final = "cause_seqs"
CHANGES: Final = "changes"

# The two readings the service takes of the clock, as this file drives them (module
# docstring): 0.25 s of the monotonic source per reading, which keeps the call rate of a
# session far below its bound, and 125 ms of the instant source, so two events have two
# different ``ts``.
MONOTONIC_STEP: Final = 0.25
MONOTONIC_BASE: Final = 1000.0
NOW_STEP_MS: Final = 125
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# How many threads the SQLite work of the examples runs in. One example runs one job at a
# time - two sandboxes, never at once - so the pool of the service (up to 32 threads,
# :func:`~erp_mock.gateway.sandbox.pool_size`) would be idle capacity.
POOL_THREADS: Final = 4

# Upper bound for one session; a hang fails the example instead of blocking the run.
TIMEOUT_S: Final = 60

# How much of a value an assertion message prints before it is cut short.
REPORTED_CHARS: Final = 240


# --- the clock of the service (requirement 25.10) ----------------------------------------


@dataclass(slots=True)
class Ticker:
    """The two time sources of the service, stepped by the readings themselves.

    ``monotonic`` moves :data:`MONOTONIC_STEP` seconds per reading and ``now``
    :data:`NOW_STEP_MS` milliseconds from :data:`INSTANT`, so every reading differs from the
    one before it and none of them is the system time. One per example, which is one
    Sandbox_Manager.
    """

    monotonic_reads: int = 0
    now_reads: int = 0

    def monotonic(self) -> float:
        value = MONOTONIC_BASE + MONOTONIC_STEP * self.monotonic_reads
        self.monotonic_reads += 1
        return value

    def now(self) -> datetime:
        moment = INSTANT + timedelta(milliseconds=NOW_STEP_MS * self.now_reads)
        self.now_reads += 1
        return moment

    def clock(self) -> SystemClock:
        """The clock of one Sandbox_Manager over these two sources."""
        return SystemClock(now=self.now, monotonic=self.monotonic)


# --- the instance under test --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Snapshots:
    """A :class:`~erp_mock.gateway.sandbox.SnapshotSource` over the one Snapshot file.

    The double of the adapter the service builds over the Snapshot cache (design.md 6.2):
    every sandbox of this file is copied from the Snapshot loading step 8 took up for
    ``purchase-basic``. What the cache would have been looked up with is asserted rather
    than recorded, since it is the premise of requirement 24.1 that the two sandboxes are
    created from one initial dataset version at one anchor date.
    """

    db: Path
    dataset_name: str

    @contextmanager
    def snapshot(self, dataset: DatasetModel, anchor: date) -> Iterator[Path]:
        assert dataset.name == self.dataset_name, f"创建沙箱时取用的数据集是 {dataset.name}"
        assert anchor == ANCHOR_DATE, f"创建沙箱时的锚点日期是 {anchor}，不是 {ANCHOR_DATE}"
        yield self.db


@dataclass(frozen=True, slots=True)
class Service:
    """What every example of this file starts from, assembled once for the module.

    - ``loaded``: the validated configuration of ``instances/p2p-demo`` (design.md 2.4);
    - ``schema``: its tables, which every sandbox database and every diff holds;
    - ``start``: the starting point of the drawing runs, over the same Snapshot file; its
      :class:`~erp_mock.tools.layer.ToolLayer` is the one the gateway performs the calls of
      the property with as well, there being one tools layer per service process;
    - ``snapshots``: where a new sandbox is copied from (:class:`Snapshots`);
    - ``data_dir``: ``--data-dir`` of the service, whose ``sandboxes/`` the sandboxes of
      every example go below;
    - ``work``: where the sandbox of a drawing run is created;
    - ``executor``: the thread pool every SQLite statement of the examples runs in.
    """

    loaded: LoadedInstance
    schema: StorageSchema
    start: Start
    snapshots: Snapshots
    data_dir: Path
    work: Path
    executor: ThreadPoolExecutor

    @property
    def profile(self) -> ProfileModel:
        """The tool profile every session of this file sees (requirement 14.2)."""
        return self.loaded.default_profile

    @property
    def dataset(self) -> DatasetModel:
        """The initial dataset every sandbox is created from (requirement 15.1)."""
        return self.loaded.default_dataset

    @property
    def instance_ref(self) -> str:
        """The instance as ``<name>@<version>``, which a diff names (requirement 18.1)."""
        return f"{self.loaded.instance.instance}@{self.loaded.instance.version}"


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    """The example instance, loaded once, and the Snapshot of its initial dataset.

    Every loading step runs over the real instance directory, step 8 included, which builds
    the Snapshot of ``purchase-basic`` below the module's temporary directory and checks the
    invariants over it (requirement 2.14). :data:`ANCHOR_DATE` is handed to the loader
    because the clock of this instance is in mode today (module docstring). All of it is a
    fixture of the module rather than of a test: it costs about as much as two examples do,
    and an example only copies sandboxes from what it produced.

    Raises :class:`~erp_mock.loader.errors.ConfigErrors` where the instance does not
    validate and :class:`~erp_mock.seed.resolve.SeedError` where the dataset cannot be
    built at that date, both of which are tested by :mod:`tests.packs.test_p2p_demo`.
    """
    root = tmp_path_factory.mktemp("p30")
    loaded = Loader(P2P_INSTANCE_DIR, P2P_PACKS_DIR, ANCHOR_DATE, root / "build").load()
    dataset = loaded.default_dataset
    assert dataset.name == P2P_DATASET, f"默认初始数据集是 {dataset.name}，不是 {P2P_DATASET}"
    directory = dataset.snapshot
    assert directory is not None, f"第 8 步没有为 {P2P_DATASET} 取用快照"
    snapshot = directory / SNAPSHOT_FILENAME
    data_dir = root / "data"
    data_dir.mkdir()
    work = root / "work"
    work.mkdir()
    executor = ThreadPoolExecutor(max_workers=POOL_THREADS, thread_name_prefix="p30-sandbox")
    try:
        yield Service(
            loaded=loaded,
            schema=build_schema(loaded.domain),
            start=start_from(
                snapshot,
                loaded,
                name=P2P_DATASET,
                anchor_date=ANCHOR_DATE,
                current_user_id=dataset.current_user,
            ),
            snapshots=Snapshots(db=snapshot, dataset_name=P2P_DATASET),
            data_dir=data_dir,
            work=work,
            executor=executor,
        )
    finally:
        executor.shutdown(wait=True)


# --- one Request_Sequence ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListStep:
    """One tools/list of the sequence (requirement 14.5).

    It changes nothing of the sandbox and returns the frozen payload of the profile, and it
    is part of a Request_Sequence all the same: it takes the next sequence number of the
    sandbox and writes a ``tools_list`` event, both of which the comparisons of
    requirements 24.2 and 24.3 cover.
    """


# The one tools/list step; a sequence holds the same object at every list position.
LIST: Final = ListStep()

# One step of a Request_Sequence: a tools/call of a tool with its arguments
# (:class:`~tests.property.opseq.session.Call`), or a tools/list.
type Step = Call | ListStep


def draw_sequence(data: st.DataObject, service: Service) -> tuple[Step, ...]:
    """Draw one Request_Sequence against the state its own operations build up.

    The operations are drawn one at a time through
    :func:`~tests.property.opseq.generate.next_call` and performed in a sandbox of the
    drawing run, so the next draw reads the state the earlier ones left (requirement 25.2);
    that sandbox is removed when the block leaves and nothing of it is compared. The
    tools/list steps are drawn afterwards, as positions in the resulting list, there being
    nothing about a tools/list that depends on the state.
    """
    with open_sandbox(service.start, service.work) as session:
        count = data.draw(st.integers(min_value=MIN_CALLS, max_value=MAX_CALLS), label="调用个数")
        calls: list[Call] = []
        for _ in range(count):
            call = next_call(data.draw, session)
            if call is None:
                break
            session.perform(call)
            calls.append(call)
    positions = data.draw(
        st.lists(
            st.integers(min_value=0, max_value=len(calls)),
            min_size=MIN_LISTS,
            max_size=MAX_LISTS,
        ),
        label="tools/list 的位置",
    )
    return _interleave(calls, positions)


def _interleave(calls: Sequence[Call], positions: Sequence[int]) -> tuple[Step, ...]:
    """``calls`` with a tools/list before the call at each of ``positions``.

    A position of ``len(calls)`` puts the tools/list after the last call, and two equal
    positions put two of them in a row.
    """
    at = Counter(positions)
    steps: list[Step] = []
    for index, call in enumerate(calls):
        steps += [LIST] * at[index]
        steps.append(call)
    steps += [LIST] * at[len(calls)]
    return tuple(steps)


# --- what one step of the sequence got back -----------------------------------------------


@dataclass(frozen=True, slots=True)
class Answered:
    """What one tools/call of the sequence was answered with (requirement 24.1).

    - ``call``: the tool and the arguments of the request, for the failure report;
    - ``is_error``: the ``isError`` of the result (requirement 13.6);
    - ``text``: the one text content item, as it came - the comparison of two of these is
      the byte for byte comparison requirement 24.1 asks for, both being Python strings
      that the SDK decoded from the same UTF-8;
    - ``structured``: the ``structuredContent`` as one line of JSON, so two of them differ
      as soon as a value or the order of a key does (requirements 13.4 and 24.1); None for
      a failed call, which carries no ``structuredContent`` at all.
    """

    call: Call
    is_error: bool
    text: str
    structured: str | None

    @property
    def error(self) -> Mapping[str, Any] | None:
        """The error object of a failed call, None for one that went through (req 13.6)."""
        if not self.is_error:
            return None
        payload = json.loads(self.text)
        found = payload[ERROR_KEY]
        assert isinstance(found, dict), f"错误结果的 {ERROR_KEY} 不是对象：{found}"
        return found

    @property
    def refused_limit(self) -> str | None:
        """The resource limit that refused this call, None where none did (req 23.12).

        The condition requirement 24.1 puts on a Request_Sequence is that no call of it was
        refused for calling too fast; the three limits are reported the same way
        (:func:`~erp_mock.tools.layer.limit_error`), so this names whichever of them bit.
        A LIMIT_EXCEEDED of a parameter over its bound - a ``page_size`` above its maximum,
        a ``lines`` above its ``maxItems`` (requirement 12.3) - carries no such detail and
        is no refusal of the sandbox.
        """
        error = self.error
        if error is None or error[CODE] != ErrorCode.LIMIT_EXCEEDED.value:
            return None
        details = error.get(DETAILS) or []
        names = [item[LIMIT_KEY] for item in details if LIMIT_KEY in item]
        return next((str(name) for name in names if name in LIMITS), None)


# What one run of the sequence was answered with, step by step: one :class:`Answered` per
# tools/call and None at the position of every tools/list, so two runs of one sequence are
# compared position by position.
type Answers = tuple[Answered | None, ...]


@dataclass(frozen=True, slots=True)
class Collected:
    """Everything one example read off the service, before anything is compared.

    The four runs of the sequence (module docstring) and what each of them left behind:
    ``first`` and ``second`` are the two new sandboxes, ``after_reset`` the replay of the
    whole sequence after the reset and ``after_restore`` the replay of the tail after the
    restore. ``trace_*`` are the trace events of a sandbox, ``diff_*`` the bytes of its
    diff against ``initial``, and ``reset_seq`` the sequence number of the
    ``sandbox_reset`` event, which every ``cause_seqs`` of the replay has to be above
    (requirements 18.3, 18.10 and 24.4).
    """

    first: Answers
    second: Answers
    after_reset: Answers
    after_restore: Answers
    trace_first: tuple[dict[str, Any], ...]
    trace_second: tuple[dict[str, Any], ...]
    diff_first: bytes
    diff_second: bytes
    diff_after_reset: bytes
    reset_seq: int


# --- one MCP session over the SDK's memory streams ---------------------------------------


@asynccontextmanager
async def mcp_session(
    manager: SandboxManager, handlers: SessionHandlers, sb: SandboxRuntime, session_id: str
) -> AsyncIterator[ClientSession]:
    """One initialized session on ``sb``, with the official client on the other end.

    What the transports do around a session, done here (module docstring): the session is
    bound to the sandbox, its ``session_start`` is written inside the sandbox lock before
    the first request, a low-level ``Server`` of
    :class:`~erp_mock.gateway.handlers.SessionHandlers` is run over the SDK's memory
    streams with :data:`~erp_mock.gateway.handlers.INIT_OPTIONS`, and the ``session_end``
    of requirement 22.4 is written once the session is over. ``raise_exceptions`` stays
    false, as in both transports (design.md 7.2).
    """
    sb.bind(session_id)
    await manager.run(sb, lambda runtime: runtime.trace.write(EventName.SESSION_START, session_id))
    try:
        with anyio.fail_after(TIMEOUT_S):
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                client_read, client_write = client_streams
                server_read, server_write = server_streams
                server = handlers.build_server(McpSessionContext.of(session_id, sb))
                async with anyio.create_task_group() as tg:
                    tg.start_soon(server.run, server_read, server_write, INIT_OPTIONS)
                    try:
                        async with ClientSession(client_read, client_write) as client:
                            await client.initialize()
                            yield client
                    finally:
                        tg.cancel_scope.cancel()
    finally:
        await manager.run(
            sb, lambda runtime: runtime.trace.write(EventName.SESSION_END, session_id)
        )


async def perform(client: ClientSession, call: Call) -> Answered:
    """Send one tools/call of ``call`` and read the result off the response.

    ``ClientSession.call_tool`` is avoided on purpose: it issues a tools/list of its own,
    which would be a request of the sequence that nothing drew, and validates
    ``structuredContent`` against the outputSchema on the client side, which is no part of
    what the server answered.
    """
    request = types.ClientRequest(
        types.CallToolRequest(
            params=types.CallToolRequestParams(
                name=call.tool, arguments=None if call.args is None else dict(call.args)
            )
        )
    )
    result = await client.send_request(request, types.CallToolResult)
    assert len(result.content) == 1, f"{call.tool} 的结果有 {len(result.content)} 个内容项"
    item = result.content[0]
    assert isinstance(item, types.TextContent), f"{call.tool} 的结果不是文本内容：{item}"
    return Answered(
        call=call,
        is_error=bool(result.isError),
        text=item.text,
        structured=None
        if result.structuredContent is None
        else canonical(result.structuredContent),
    )


async def run_sequence(
    manager: SandboxManager,
    handlers: SessionHandlers,
    sb: SandboxRuntime,
    session_id: str,
    steps: Sequence[Step],
    *,
    save_named_at: int | None = None,
) -> Answers:
    """Run ``steps`` as one session of ``sb`` and return what each step was answered with.

    ``save_named_at`` is the step the Named_Snapshot :data:`NAMED` is saved before, None
    saving none; it names a step of ``steps``, so the part of the sequence that follows the
    snapshot is never empty. Saving it in the middle of the session is what requirement
    24.11 describes - a snapshot of the state the sequence had reached - and it writes no
    trace event and changes no record (requirement 19.5), so the trace and the diff of this
    run are the ones a run without it would have produced.
    """
    answers: list[Answered | None] = []
    async with mcp_session(manager, handlers, sb, session_id) as client:
        for index, step in enumerate(steps):
            if index == save_named_at:
                await manager.save_named(sb, NAMED)
            if isinstance(step, ListStep):
                await client.list_tools()
                answers.append(None)
            else:
                answers.append(await perform(client, step))
    return tuple(answers)


# --- what a sandbox leaves behind ---------------------------------------------------------


def read_trace(sb: SandboxRuntime) -> tuple[dict[str, Any], ...]:
    """The events of the trace file of ``sb``, one per line (requirement 17.1).

    Read off the file without taking the sandbox lock, as the Admin API's trace endpoint
    reads it (requirement 19.7); the caller has awaited every job of the sandbox, so the
    file holds every event of it.
    """
    text = (sb.directory / TRACE_FILENAME).read_text(encoding="utf-8")
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line:
            continue
        parsed = json.loads(line)
        assert isinstance(parsed, dict), f"trace 行不是 JSON 对象：{line}"
        events.append(parsed)
    return tuple(events)


async def diff_of(manager: SandboxManager, service: Service, sb: SandboxRuntime) -> bytes:
    """The diff of ``sb`` against ``initial``, with the sandbox id replaced (req 24.2).

    The bytes the stdio runtime writes into ``diff.json`` and the Admin API's diff endpoint
    sends (design.md 7.7): :func:`~erp_mock.state.diff.compute_diff` against
    ``base.sqlite``, the four identifying values of the sandbox, and
    :func:`~erp_mock.common.jsonio.dumps_pretty` encoded as UTF-8. The value of
    ``sandbox_id`` becomes :data:`SANDBOX_PLACEHOLDER`, which is the one replacement
    requirement 24.2 allows before the two are compared.

    It runs through :meth:`~erp_mock.gateway.sandbox.SandboxManager.run`, so the comparison
    waits for a call that is still in flight and its statements run in the thread pool, and
    with ``activity=False``, a diff being no call of the sandbox.
    """
    return await manager.run(sb, lambda runtime: _diff_bytes(service, runtime), activity=False)


def _diff_bytes(service: Service, sb: SandboxRuntime) -> bytes:
    """The diff of ``sb`` as the service serializes it (worker thread, :func:`diff_of`)."""
    body = compute_diff(sb.store.db_path, sb.base_db, service.schema)
    payload = body.as_json(
        sandbox_id=sb.sandbox_id,
        instance=service.instance_ref,
        dataset=sb.dataset_ref,
        anchor_date=sb.anchor_date,
    )
    return encode_utf8(dumps_pretty(payload).replace(sb.sandbox_id, SANDBOX_PLACEHOLDER))


# --- the four runs of one sequence --------------------------------------------------------


async def collect(service: Service, steps: Sequence[Step], split: int) -> Collected:
    """Run ``steps`` in two new sandboxes, then replay it in the first one twice.

    The order of the runs, which is what makes the comparisons meaningful: the first
    sandbox runs the sequence and saves the Named_Snapshot after ``split`` steps, and its
    trace and its diff are read before anything else touches it; the second sandbox runs
    the same sequence; the first is then reset and replays the whole sequence, and restored
    to the Named_Snapshot and replays the part of the sequence that followed it.

    One Sandbox_Manager per example, over the shared thread pool and the stepped clock of
    :class:`Ticker`; both sandboxes are deleted and the manager is closed on the way out,
    so an example leaves no sandbox directory behind (requirement 19.9).
    """
    manager = SandboxManager(
        data_dir=service.data_dir,
        loaded=service.loaded,
        schema=service.schema,
        snapshots=service.snapshots,
        clock=Ticker().clock(),
        executor=service.executor,
    )
    handlers = SessionHandlers(manager, service.start.layer)
    created: list[SandboxRuntime] = []
    try:
        for _ in range(2):
            created.append(
                await manager.create(service.profile, service.dataset, anchor=ANCHOR_DATE)
            )
        one, other = created

        first = await run_sequence(manager, handlers, one, SESSION_A, steps, save_named_at=split)
        trace_first = read_trace(one)
        diff_first = await diff_of(manager, service, one)

        second = await run_sequence(manager, handlers, other, SESSION_B, steps)
        trace_second = read_trace(other)
        diff_second = await diff_of(manager, service, other)

        await manager.reset(one)
        after_reset = await run_sequence(manager, handlers, one, SESSION_RESET, steps)
        diff_after_reset = await diff_of(manager, service, one)
        reset_seq = _reset_seq(read_trace(one))

        await manager.restore(one, NAMED)
        after_restore = await run_sequence(manager, handlers, one, SESSION_RESTORED, steps[split:])
        return Collected(
            first=first,
            second=second,
            after_reset=after_reset,
            after_restore=after_restore,
            trace_first=trace_first,
            trace_second=trace_second,
            diff_first=diff_first,
            diff_second=diff_second,
            diff_after_reset=diff_after_reset,
            reset_seq=reset_seq,
        )
    finally:
        for sb in created:
            await manager.delete(sb)
        manager.close()


def _reset_seq(events: Sequence[Mapping[str, Any]]) -> int:
    """The ``seq`` of the one ``sandbox_reset`` event of the trace (requirement 17.7)."""
    found = [int(item["seq"]) for item in events if item["event"] == EventName.SANDBOX_RESET]
    assert len(found) == 1, f"trace 中有 {len(found)} 个 sandbox_reset 事件，期望 1 个"
    return found[0]


# --- comparing two runs, two traces and two diffs -----------------------------------------


def canonical(value: Any) -> str:
    """``value`` as one line of JSON, every key in the order it is in (requirement 13.4).

    Two values serialize to the same text exactly when they are equal *and* their keys are
    in the same order, at every level, which is what requirements 24.1 and 24.3 compare.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def stable_event(item: Mapping[str, Any]) -> str:
    """One trace event without the four keys requirement 24.3 drops, as one line of JSON."""
    return canonical({key: value for key, value in item.items() if key not in VOLATILE_KEYS})


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


def compare_answers(label: str, steps: Sequence[Step], first: Answers, second: Answers) -> None:
    """Assert that two runs of one sequence were answered the same way (requirement 24.1).

    Step by step: the ``isError``, the ``text`` byte for byte and the ``structuredContent``
    with its keys in the same order. A tools/list is compared as a position and nothing
    else, its result being the frozen payload of the profile (requirement 11.10).
    """
    assert len(first) == len(second), (
        f"{label}：两次执行的步骤数不同，{len(first)} 与 {len(second)}\n{_sequence_report(steps)}"
    )
    for index, (one, other) in enumerate(zip(first, second, strict=True), start=1):
        if one is None or other is None:
            assert one is None and other is None, (
                f"{label}：第 {index} 步一次是 tools/call 一次是 tools/list"
                f"\n{_sequence_report(steps)}"
            )
            continue
        assert one.is_error == other.is_error, _mismatch(
            label, index, steps, "isError", one.is_error, other.is_error
        )
        assert one.text == other.text, _mismatch(label, index, steps, "text", one.text, other.text)
        assert one.structured == other.structured, _mismatch(
            label, index, steps, "structuredContent", one.structured, other.structured
        )


def _mismatch(
    label: str, index: int, steps: Sequence[Step], key: str, one: object, other: object
) -> str:
    """The failure message of one field of one step: where, what, and both values."""
    tool = _step_text(steps[index - 1])
    return "\n".join(
        [
            f"{label}：第 {index} 步（{tool}）的 {key} 不同：",
            f"  第一次：{_short(one)}",
            f"  第二次：{_short(other)}",
            _sequence_report(steps),
        ]
    )


def _sequence_report(steps: Sequence[Step]) -> str:
    """The sequence as the report prints it: every step with its arguments, in order.

    The counterexample of requirement 25.2: by the time Hypothesis prints the message it
    has replayed the shrunk example, so this is the short way to the failure rather than
    the sequence it was found on.
    """
    lines = [f"  {index}. {_step_text(step)}" for index, step in enumerate(steps, start=1)]
    return "\n".join(["请求序列（已收缩）：", *lines])


def _step_text(step: Step) -> str:
    """One step of the sequence as the report prints it."""
    if isinstance(step, ListStep):
        return "tools/list"
    return f"{step.tool} {step.args}"


def _short(value: object) -> str:
    """``value`` as text, cut short at :data:`REPORTED_CHARS` characters."""
    text = str(value)
    if len(text) <= REPORTED_CHARS:
        return text
    return f"{text[:REPORTED_CHARS]}…（共 {len(text)} 个字符）"


def _bucket(count: int) -> str:
    """``count`` as the range a Hypothesis event reports it in (:func:`event`)."""
    if count == 0:
        return "0"
    return "1 到 9" if count < 10 else "10 及以上"


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


# --- the property --------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 30: 请求序列的确定性与重放
@pytest.mark.req("24.1", "24.2", "24.3", "24.4", "24.11")
@settings(
    max_examples=SEQUENCES,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_a_request_sequence_decides_the_results_the_diff_and_the_trace(
    data: st.DataObject, service: Service
) -> None:
    steps = draw_sequence(data, service)
    # Where the Named_Snapshot of requirement 24.11 is taken: never after the last step, so
    # the tail that is replayed holds at least one step; 0 - the shrunk value - makes that
    # tail the whole sequence.
    split = data.draw(st.integers(min_value=0, max_value=len(steps) - 1), label="快照位置")
    collected = anyio.run(collect, service, steps, split)
    calls = [answer for answer in collected.first if answer is not None]
    event(f"失败的调用：{'有' if any(answer.is_error for answer in calls) else '没有'}")
    event(f"tools/list 的个数：{len(collected.first) - len(calls)}")
    event(f"diff 的变化条数：{_bucket(len(json.loads(collected.diff_first)[CHANGES]))}")

    # The condition requirement 24.1 puts on a Request_Sequence: no call of it was refused
    # by a resource limit of the sandbox or by the call rate of the session.
    refused = [(answer.call.tool, answer.refused_limit) for answer in calls if answer.refused_limit]
    assert not refused, f"序列中有调用被资源上限拒绝，不是需求 24 第 1 条的序列：{refused}"

    # Two new sandboxes, one sequence: the same isError, the same text byte for byte and the
    # same structuredContent, values and key order alike (requirement 24.1).
    compare_answers("两个新建沙箱", steps, collected.first, collected.second)

    # Their diffs against initial, byte for byte once the sandbox id is replaced by one
    # string - cause_seqs included (requirement 24.2). That the replacement happened at all
    # is asserted first: a diff that named no sandbox would make the comparison pass for
    # the wrong reason.
    assert SANDBOX_PLACEHOLDER.encode() in collected.diff_first, (
        f"diff 中没有 sandbox_id 的值，无法按需求 24 第 2 条替换：{_short(collected.diff_first)}"
    )
    assert collected.diff_first == collected.diff_second, (
        _difference("diff", collected.diff_first, collected.diff_second)
        + f"\n{_sequence_report(steps)}"
    )

    # Their traces: the same number of events, and the corresponding events equal with their
    # keys in the same order once ts, latency_ms, sandbox_id and session_id are dropped
    # (requirement 24.3). Both sessions ended before the traces were read.
    first_events, second_events = collected.trace_first, collected.trace_second
    assert len(first_events) == len(second_events), (
        f"两个沙箱的 trace 事件数不同：{len(first_events)} 与 {len(second_events)}"
        f"\n{_sequence_report(steps)}"
    )
    for index, (one, other) in enumerate(zip(first_events, second_events, strict=True), start=1):
        assert stable_event(one) == stable_event(other), "\n".join(
            [
                f"两个沙箱的第 {index} 个 trace 事件去掉易变键后不同：",
                f"  第一个沙箱：{_short(stable_event(one))}",
                f"  第二个沙箱：{_short(stable_event(other))}",
                _sequence_report(steps),
            ]
        )

    # Reset and replayed: every call answered as it was the first time, and the diff equal
    # to the one taken before the reset in everything but cause_seqs (requirement 24.4).
    compare_answers("重置后重放", steps, collected.first, collected.after_reset)
    assert without_causes(collected.diff_after_reset) == without_causes(collected.diff_first), (
        "重置后重放的 diff 除 cause_seqs 外与重置前不同：\n"
        f"  重置前：{_short(without_causes(collected.diff_first))}\n"
        f"  重放后：{_short(without_causes(collected.diff_after_reset))}\n"
        f"{_sequence_report(steps)}"
    )
    # And cause_seqs names the calls of the replay alone: every one of them was written
    # after the sandbox_reset event (requirements 18.3 and 18.10).
    replayed = cause_seqs(collected.diff_after_reset)
    assert all(seq > collected.reset_seq for seq in replayed), (
        f"重放后的 cause_seqs {replayed} 中有不晚于 sandbox_reset（seq "
        f"{collected.reset_seq}）的调用序号\n{_sequence_report(steps)}"
    )

    # Restored to the Named_Snapshot and replayed from there: the part of the sequence that
    # followed the snapshot is answered as it was the first time (requirement 24.11).
    compare_answers(
        f"恢复 {NAMED} 后重放第 {split + 1} 步起",
        steps[split:],
        collected.first[split:],
        collected.after_restore,
    )
