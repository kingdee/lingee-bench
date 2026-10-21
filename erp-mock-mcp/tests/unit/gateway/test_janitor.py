"""The janitor of the Sandbox_Manager: when a sandbox turns idle, freezes and is deleted.

What is checked here is the timing of a sandbox's life, the three transitions nobody asks
for (design.md 7.5): an active sandbox that has had no call for the idle threshold turns
idle and its database is closed (requirement 15.5), an idle one that stays idle for the
freeze threshold turns frozen (requirement 15.7), a frozen one is deleted once its
retention period is over (requirement 15.9), and the call that reaches an idle sandbox in
between is served exactly as an active sandbox would have served it (requirement 15.6).
The reverse of those conditions matters as much and is checked beside each of them: a
sandbox with a request in flight, one that was called a moment ago and one that is exactly
at its threshold stay where they are.

Almost every test here drives the thresholds with :class:`Ticker`, a monotonic source it
moves itself, and makes the scan itself with :meth:`~erp_mock.gateway.sandbox.
SandboxManager.sweep`, which is one scan of the timing loop: a threshold of five minutes
is then a line of the test rather than five minutes of waiting, the transitions happen
where the test put them, and no test reads the system clock (requirement 25.10).

Two tests run the loop instead of calling the scan, because the loop is what the service
starts:

- ``test_the_janitor_scans_until_it_is_stopped`` runs it over the same :class:`Ticker`, so
  what it shows is the loop - that it keeps scanning and that it stops when it is stopped -
  and not the timing;
- ``test_every_transition_happens_within_a_second_of_its_condition`` is the one test of
  this file that reads a real monotonic clock, and it must: requirements 15.5, 15.7 and
  15.9 give the service one second between a condition and its transition, which is a
  statement about elapsed time and cannot be made by a test that decides what the clock
  says. Its thresholds are one second each, the smallest
  :class:`~erp_mock.gateway.sandbox.SandboxLimits` takes, so the whole life of its sandbox
  passes in about four seconds. The instants of the sandbox - its ``created_at``, the
  ``ts`` of its events - come from the same fixed source as everywhere else here; only the
  monotonic source is the real one.

The Snapshot every sandbox is created from is the cheap one of
tests/unit/gateway/test_sandbox.py, built once per module: the tables of the engine test
fixture and one counter in them, which is enough to tell a database that survived its
sandbox turning idle from one that did not.
"""

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.jsonio import JsonValue
from erp_mock.config.instance import DatasetDef
from erp_mock.gateway.clock import MonotonicSource, SystemClock
from erp_mock.gateway.sandbox import (
    JANITOR_INTERVAL_SECONDS,
    METADATA_FILENAME,
    SandboxLimits,
    SandboxManager,
    SandboxRuntime,
)
from erp_mock.gateway.trace import TRACE_FILENAME, EventName
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.loader.yaml_io import load_yaml
from erp_mock.state.schema import (
    COUNTERS_TABLE,
    EFFECTIVE_WRITES_TABLE,
    StorageSchema,
    build_schema,
)
from erp_mock.state.snapshot import build_snapshot_db, finalize_snapshot
from erp_mock.state.store import DB_FILENAME, DIRECT_CAUSE, SandboxStore, Value, Write
from tests.helpers.fixtures import ANCHOR_DATE, DATASET, INSTANCE_DIR, fixture_loaded

# The counter the Snapshot brings with it and how far it was raised; the two names are of
# no business pack - what this file does with them is to write them into a row.
COUNTER_KIND: Final = "id"
COUNTER_KEY: Final = "fx_widget"
SNAPSHOT_COUNTER: Final = 5

# The record an effective call of this file says it wrote (requirement 18.10).
ENTITY: Final = "fx_widget"

# The definition file of the initial dataset of the engine test fixture, relative to its
# instance directory, as loading step 4 reads it.
DATASET_FILE: Final = f"datasets/{DATASET}.yaml"

# The session the calls of this file arrive on.
SESSION: Final = "se_6678a1b20c3d4e5f"

# The instant the first reading of the test clock returns; the nth reading is this plus
# n - 1 seconds, so the created_at of two sandboxes differ.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# The thresholds the tests that drive the clock themselves run with: seconds, because the
# :class:`Ticker` moves in whatever steps the test wants and three different numbers make
# it plain which threshold a transition was made on.
LIMITS: Final = SandboxLimits(idle_seconds=60, freeze_seconds=300, retention_seconds=900)

# The thresholds of the one test that waits for the real clock: one second each, the
# smallest SandboxLimits takes, and the whole life of a sandbox is about four seconds.
LIVE_LIMITS: Final = SandboxLimits(idle_seconds=1, freeze_seconds=1, retention_seconds=1)

# How long that test gives a transition once its condition holds: the one second of
# requirements 15.5, 15.7 and 15.9, polled this often. The poll is what decides how early a
# transition would have to be for the test to see it as early.
ALLOWED_DELAY: Final = 1.0
POLL_SECONDS: Final = 0.02

# How long a job of this file waits for the event that lets it go, and how long a test
# waits for a condition of the running loop: both are far above what the operations here
# take and are only reached by a test that is about to fail anyway.
BLOCK_TIMEOUT: Final = 5.0
SPIN_SECONDS: Final = 0.01
SPINS: Final = 200

# How many turns of the event loop a queued transition gets to reach the sandbox lock;
# it needs a handful, and a turn costs nothing.
LOCK_TURNS: Final = 50


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- the test clock ----------------------------------------------------------------------


@dataclass(slots=True)
class Ticker:
    """A monotonic source the test moves itself (requirement 25.10).

    Every reading returns :attr:`seconds`, which :meth:`advance` raises. The janitor reads
    it once per scan and measures every threshold against that reading, so a test says how
    much time has passed by saying how far to move this.
    """

    seconds: float = 1000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> float:
        """Move the clock forward and return the new reading."""
        self.seconds += seconds
        return self.seconds


def stepping_clock(ticker: Ticker, monotonic: MonotonicSource | None = None) -> SystemClock:
    """A clock whose nth instant is :data:`INSTANT` plus n - 1 seconds, over ``ticker``.

    ``monotonic`` replaces the ticker for the one test that waits for the real clock; the
    instants stay the fixed ones either way, since nothing of the janitor reads them.
    """
    readings = iter(range(1 << 20))
    return SystemClock(
        now=lambda: INSTANT + timedelta(seconds=next(readings)),
        monotonic=ticker if monotonic is None else monotonic,
    )


# --- the Snapshot every sandbox is created from ------------------------------------------


def build_snapshot(directory: Path, schema: StorageSchema) -> Path:
    """Build the one Snapshot of this file below ``directory`` and return its path.

    A build database with the tables of ``schema``, one counter raised
    :data:`SNAPSHOT_COUNTER` times and nothing else, written out as a Snapshot the way the
    Seed_Builder writes one (:func:`~erp_mock.state.snapshot.finalize_snapshot`).
    """
    build = directory / "build.sqlite"
    snapshot = directory / "snapshot.sqlite"
    store = build_snapshot_db(build, schema)
    try:
        with store.transaction() as uow:
            for _ in range(SNAPSHOT_COUNTER):
                uow.bump_counter(COUNTER_KIND, COUNTER_KEY)
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


# --- the instance configuration and the manager under test -------------------------------


@dataclass(frozen=True, slots=True)
class Env:
    """What every test of this file starts from, built once for the module."""

    loaded: LoadedInstance
    schema: StorageSchema
    dataset: DatasetModel
    snapshot: Path

    @property
    def profile(self) -> ProfileModel:
        """The one profile of the fixture instance, which every sandbox here gets."""
        return self.loaded.default_profile


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Env:
    """The fixture instance, its tables and one Snapshot of its initial dataset."""
    loaded = fixture_loaded()
    schema = build_schema(loaded.domain)
    definition = DatasetDef.model_validate(load_yaml(INSTANCE_DIR / DATASET_FILE))
    return Env(
        loaded=loaded,
        schema=schema,
        dataset=DatasetModel(definition=definition, file=DATASET_FILE),
        snapshot=build_snapshot(tmp_path_factory.mktemp("snapshot"), schema),
    )


@dataclass(slots=True)
class Harness:
    """A manager over a data directory of its own, and what a test needs around it."""

    manager: SandboxManager
    ticker: Ticker
    env: Env

    async def create(self) -> SandboxRuntime:
        """Create one sandbox of the fixture's profile and dataset."""
        return await self.manager.create(self.env.profile, self.env.dataset)

    @asynccontextmanager
    async def janitor(self) -> AsyncIterator[None]:
        """Run the timing loop of the manager for the block and stop it on the way out."""
        self.manager.start_janitor()
        try:
            yield
        finally:
            await self.manager.stop_janitor()


@contextmanager
def harness_over(
    env: Env,
    data_dir: Path,
    *,
    limits: SandboxLimits = LIMITS,
    monotonic: MonotonicSource | None = None,
) -> Iterator[Harness]:
    """Yield a harness over ``data_dir``, closing the manager and its pool on the way out."""
    ticker = Ticker()
    manager = SandboxManager(
        data_dir=data_dir,
        loaded=env.loaded,
        schema=env.schema,
        snapshots=Snapshots(env.snapshot),
        clock=stepping_clock(ticker, monotonic),
        limits=limits,
    )
    try:
        yield Harness(manager=manager, ticker=ticker, env=env)
    finally:
        manager.close()


@pytest.fixture
def harness(env: Env, tmp_path: Path) -> Iterator[Harness]:
    """A manager with the thresholds of :data:`LIMITS` over a data directory of this test."""
    with harness_over(env, tmp_path) as made:
        yield made


# --- jobs and readings -------------------------------------------------------------------


def call(seq: int) -> Callable[[SandboxRuntime], int]:
    """A job that writes what a request of a session writes: the counter, the call, an event.

    One transaction and one trace event, so that a sandbox which turns idle has something
    in its database and in its trace that the transition must leave alone. The event is a
    ``tools_list``, the one with no body of its own; what the janitor does to a trace is
    the same for every event of it (requirement 17.7).
    """

    def job(sb: SandboxRuntime) -> int:
        with sb.store.transaction() as uow:
            value = uow.bump_counter(COUNTER_KIND, COUNTER_KEY)
            uow.record_effective_call(
                seq, [Write(entity=ENTITY, op="insert", id=f"wd_{seq:06d}", cause=DIRECT_CAUSE)]
            )
        sb.trace.write(EventName.TOOLS_LIST, SESSION)
        return value

    return job


def blocking(entered: threading.Event, gate: threading.Event) -> Callable[[SandboxRuntime], str]:
    """A job that reports it started and then waits for ``gate`` before it returns."""

    def job(sb: SandboxRuntime) -> str:
        entered.set()
        if not gate.wait(BLOCK_TIMEOUT):  # pragma: no cover - a test that is failing
            raise AssertionError("作业等待超时")
        return sb.sandbox_id

    return job


@dataclass(frozen=True, slots=True)
class Content:
    """What a sandbox database holds, as much of it as this file writes."""

    counter: int
    calls: tuple[int, ...]


def content_of(directory: Path, schema: StorageSchema) -> Content:
    """The content of the sandbox database in ``directory``, read on a store of our own.

    A sandbox that has turned idle has its database closed, which is the point of
    requirement 15.5, so its content is read through a second store over the same file -
    what the next call of the sandbox will find when :meth:`SandboxManager.run` opens it
    again (requirement 15.6).
    """
    store = SandboxStore(directory, schema)
    store.open()
    try:
        with store.transaction() as uow:
            counters = [
                dict(row)
                for row in uow.select(COUNTERS_TABLE, {"kind": COUNTER_KIND, "key": COUNTER_KEY})
            ]
            calls = [as_int(dict(row)["seq"]) for row in uow.select(EFFECTIVE_WRITES_TABLE, {})]
        return Content(
            counter=as_int(counters[0]["value"]) if counters else 0, calls=tuple(sorted(calls))
        )
    finally:
        store.close()


def as_int(value: Value) -> int:
    """``value`` as the int a counter and a sequence number are stored as."""
    assert isinstance(value, int), value
    return value


def events(sb: SandboxRuntime) -> list[dict[str, JsonValue]]:
    """The events of the trace file of ``sb``, one per line (requirement 17.1)."""
    text = (sb.directory / TRACE_FILENAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def comparable(sb: SandboxRuntime) -> list[dict[str, JsonValue]]:
    """The events of ``sb`` without the two fields that are of this sandbox and this moment.

    ``sandbox_id`` names the sandbox and ``ts`` the instant the event was written, which
    two sandboxes cannot share; everything else of an event is decided by the call alone,
    so this is what requirement 15.6 compares (design.md 10, requirement 24.1).
    """
    return [
        {key: value for key, value in event.items() if key not in ("sandbox_id", "ts")}
        for event in events(sb)
    ]


def metadata_of(sb: SandboxRuntime) -> dict[str, JsonValue]:
    """The ``sandbox.json`` of ``sb``, as it is on disk (design.md data model 2)."""
    payload = json.loads((sb.directory / METADATA_FILENAME).read_bytes())
    assert isinstance(payload, dict)
    return payload


async def until(predicate: Callable[[], bool], what: str) -> None:
    """Spin the event loop until ``predicate`` holds; fail the test if it never does."""
    for _ in range(SPINS):
        if predicate():
            return
        await asyncio.sleep(SPIN_SECONDS)
    raise AssertionError(f"条件始终不成立：{what}")


async def turn_loop(turns: int = LOCK_TURNS) -> None:
    """Let every task that is ready run, so a queued transition reaches the sandbox lock."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def observed(predicate: Callable[[], bool], deadline: float, what: str) -> float:
    """Poll ``predicate`` on the real clock and return when it was first seen to hold.

    Fails once ``deadline`` has passed. The clock is read after the condition was seen to
    hold, never before, so the reading is never earlier than the transition itself and at
    most :data:`POLL_SECONDS` later: a transition that was too early shows as a reading
    before its condition and one that was too late as a reading past the deadline.
    """
    while True:
        if predicate():
            return time.monotonic()
        if time.monotonic() > deadline:
            raise AssertionError(f"条件未在期限内成立：{what}")
        await asyncio.sleep(POLL_SECONDS)


# --- active to idle (requirement 15.5) ---------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.5")
async def test_an_active_sandbox_turns_idle_when_nobody_calls_it(harness: Harness) -> None:
    sb = await harness.create()
    sb.bind(SESSION)
    assert await harness.manager.run(sb, call(seq=1)) == SNAPSHOT_COUNTER + 1
    await harness.manager.save_named(sb, "one")
    before = content_of(sb.directory, harness.env.schema)
    trace = (sb.directory / TRACE_FILENAME).read_bytes()
    named = sb.named_db("one").read_bytes()

    # Exactly at the threshold is not over it: "连续超过" of requirement 15.5.
    harness.ticker.advance(LIMITS.idle_seconds)
    await harness.manager.sweep()
    assert (sb.status, sb.store.is_open) == ("active", True)

    idle_at = harness.ticker.advance(JANITOR_INTERVAL_SECONDS)
    await harness.manager.sweep()

    assert (sb.status, sb.idle_since, sb.frozen_since) == ("idle", idle_at, None)
    assert not sb.store.is_open
    # The database file, the trace, the Named_Snapshot and the bindings are untouched, and
    # so is the anchor date; what the database holds is read on a store of our own, the
    # sandbox's being closed - comparing the bytes of the file would compare the WAL that
    # closing it checkpointed, which is no change of content.
    assert content_of(sb.directory, harness.env.schema) == before
    assert (sb.directory / TRACE_FILENAME).read_bytes() == trace
    assert sb.named_db("one").read_bytes() == named
    assert (sb.session_ids, sb.named_names()) == ([SESSION], ("one",))
    assert sb.anchor_date == ANCHOR_DATE
    assert metadata_of(sb)["anchor_date"] == ANCHOR_DATE.isoformat()
    # An idle sandbox is a sandbox of the service like any other: it is still in the
    # registry and it still takes its place among the concurrent ones (requirement 15.10).
    assert harness.manager.get(sb.sandbox_id) is sb
    assert harness.manager.non_frozen_count() == 1
    # Its sandbox.json still says active, which is the choice of design: the status of an
    # idle sandbox changes back on the request path (requirement 15.6) and the file would
    # be stale the moment a call reopened it. Nothing reads it back in M0.
    assert metadata_of(sb)["status"] == "active"


@pytest.mark.anyio
@pytest.mark.req("15.5")
async def test_a_sandbox_that_is_busy_or_was_just_called_stays_active(harness: Harness) -> None:
    sb = await harness.create()
    gate = threading.Event()
    entered = threading.Event()
    held = asyncio.create_task(harness.manager.run(sb, blocking(entered, gate)))
    await until(entered.is_set, "请求开始执行")

    # A sandbox with a request in flight is left alone however long the request takes.
    harness.ticker.advance(LIMITS.idle_seconds * 2)
    await harness.manager.sweep()
    assert (sb.status, sb.inflight) == ("active", 1)

    gate.set()
    assert await held == sb.sandbox_id
    # The call that has just finished starts the idle threshold over, so the scan right
    # after it changes nothing (requirement 15.5).
    await harness.manager.sweep()
    assert (sb.status, sb.last_done) == ("active", harness.ticker.seconds)

    harness.ticker.advance(LIMITS.idle_seconds + 1)
    await harness.manager.sweep()
    assert sb.status == "idle"


@pytest.mark.anyio
@pytest.mark.req("15.5", "15.4")
async def test_a_call_that_arrives_during_the_scan_keeps_the_sandbox_active(
    harness: Harness,
) -> None:
    sb = await harness.create()
    harness.ticker.advance(LIMITS.idle_seconds + 1)

    # The lock, held the way a request that is being handled holds it. The scan finds the
    # sandbox due - no request had started when it looked - and its transition queues
    # behind that lock, which is where a call that arrives inside a scan puts it.
    await sb.lock.acquire()
    scan = asyncio.create_task(harness.manager.sweep())
    await turn_loop()
    assert not scan.done(), "扫描应当在等待沙箱锁"

    # What the request leaves behind is the reading of the moment it finished, which is
    # run()'s own last step; the transition reads the condition again and finds it gone.
    sb.last_done = harness.ticker.advance(1.0)
    sb.lock.release()
    await scan

    assert (sb.status, sb.idle_since) == ("active", None)
    assert sb.store.is_open
    assert await harness.manager.run(sb, call(seq=1)) == SNAPSHOT_COUNTER + 1


@pytest.mark.anyio
@pytest.mark.req("15.6")
async def test_an_idle_sandbox_serves_its_next_call_as_an_active_one_would(
    harness: Harness,
) -> None:
    reopened = await harness.create()
    reference = await harness.create()
    for sb in (reopened, reference):
        assert await harness.manager.run(sb, call(seq=1)) == SNAPSHOT_COUNTER + 1
    # The reference takes its second call while it is still active, before the clock moves,
    # so nothing of it was ever closed; the other one takes the same call after the janitor
    # has turned it idle (requirement 15.6).
    expected = await harness.manager.run(reference, call(seq=2))

    harness.ticker.advance(LIMITS.idle_seconds + 1)
    await harness.manager.sweep()
    assert reopened.status == "idle"
    assert not reopened.store.is_open

    assert await harness.manager.run(reopened, call(seq=2)) == expected

    assert (reopened.status, reopened.idle_since) == ("active", None)
    assert reopened.store.is_open
    # The call is served as it would have been on a sandbox that never turned idle: the
    # same result, the same content of the database and the same trace event.
    assert content_of(reopened.directory, harness.env.schema) == content_of(
        reference.directory, harness.env.schema
    )
    assert comparable(reopened) == comparable(reference)


# --- idle to frozen (requirement 15.7) ---------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.7")
async def test_an_idle_sandbox_turns_frozen_when_the_freeze_threshold_is_over(
    harness: Harness,
) -> None:
    sb = await harness.create()
    await harness.manager.run(sb, call(seq=1))
    await harness.manager.save_named(sb, "one")
    harness.ticker.advance(LIMITS.idle_seconds + 1)
    await harness.manager.sweep()
    assert sb.status == "idle"
    trace = (sb.directory / TRACE_FILENAME).read_bytes()

    harness.ticker.advance(LIMITS.freeze_seconds)
    await harness.manager.sweep()
    assert sb.status == "idle"

    frozen_at = harness.ticker.advance(JANITOR_INTERVAL_SECONDS)
    await harness.manager.sweep()

    assert (sb.status, sb.frozen_since, sb.idle_since) == ("frozen", frozen_at, None)
    assert metadata_of(sb)["status"] == "frozen"
    # A frozen sandbox is read-only and keeps its files until it is deleted (req 15.12):
    # its trace is closed where it stands and the Admin API goes on reading all of it.
    with pytest.raises(ValueError):
        sb.trace.write(EventName.TOOLS_LIST, SESSION)
    assert (sb.directory / TRACE_FILENAME).read_bytes() == trace
    assert (sb.directory / DB_FILENAME).is_file()
    assert sb.named_names() == ("one",)
    assert harness.manager.get(sb.sandbox_id) is sb
    assert harness.manager.non_frozen_count() == 0


@pytest.mark.anyio
@pytest.mark.req("15.5", "15.7", "15.9")
async def test_one_scan_moves_a_sandbox_one_step(harness: Harness) -> None:
    sb = await harness.create()
    # Longer than all three thresholds together: every one of them would be over if they
    # were counted from here, and still the sandbox goes one step at a time, each threshold
    # being counted from the transition before it.
    harness.ticker.advance(
        LIMITS.idle_seconds + LIMITS.freeze_seconds + LIMITS.retention_seconds + 1
    )

    await harness.manager.sweep()
    assert sb.status == "idle"
    await harness.manager.sweep()
    assert sb.status == "idle"

    harness.ticker.advance(LIMITS.freeze_seconds + 1)
    await harness.manager.sweep()
    assert sb.status == "frozen"
    await harness.manager.sweep()
    assert (sb.status, harness.manager.get(sb.sandbox_id)) == ("frozen", sb)

    harness.ticker.advance(LIMITS.retention_seconds + 1)
    await harness.manager.sweep()
    assert harness.manager.get(sb.sandbox_id) is None


# --- frozen to gone (requirement 15.9) ---------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.9")
async def test_a_frozen_sandbox_is_deleted_when_its_retention_period_is_over(
    harness: Harness,
) -> None:
    sb = await harness.create()
    await harness.manager.run(sb, call(seq=1))
    await harness.manager.save_named(sb, "one")
    frozen_at = harness.ticker.advance(1.0)
    await harness.manager.freeze(sb)
    assert sb.frozen_since == frozen_at

    harness.ticker.advance(LIMITS.retention_seconds)
    await harness.manager.sweep()
    assert harness.manager.get(sb.sandbox_id) is sb
    assert sb.directory.is_dir()

    harness.ticker.advance(JANITOR_INTERVAL_SECONDS)
    await harness.manager.sweep()

    # The database, the trace, the Named_Snapshots and the metadata are the directory, and
    # it is gone; the Admin API answers 404 for the id from here on (requirement 19.15).
    assert not sb.directory.exists()
    assert list(harness.manager.root.iterdir()) == []
    assert harness.manager.get(sb.sandbox_id) is None
    assert harness.manager.sandboxes() == ()
    assert harness.manager.non_frozen_count() == 0


@pytest.mark.anyio
@pytest.mark.req("15.9")
async def test_a_deletion_that_fails_leaves_the_other_sandboxes_and_the_next_scan_alone(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    kept = await harness.create()
    undeletable = await harness.create()
    await harness.manager.run(undeletable, call(seq=1))
    for sb in (kept, undeletable):
        await harness.manager.freeze(sb)
    harness.ticker.advance(LIMITS.retention_seconds + 1)
    # A directory whose entries cannot be unlinked: the removal fails on a real file system
    # error, which is the one thing about this transition that fails in practice.
    undeletable.directory.chmod(0o500)
    try:
        with caplog.at_level("WARNING"):
            await harness.manager.sweep()

        # The sandbox that could be removed is gone, the one that could not is still a
        # sandbox of the service, and the failure is in the log (design.md error handling).
        assert harness.manager.get(kept.sandbox_id) is None
        assert not kept.directory.exists()
        assert harness.manager.get(undeletable.sandbox_id) is undeletable
        assert undeletable.directory.is_dir()
        assert undeletable.sandbox_id in caplog.text
    finally:
        undeletable.directory.chmod(0o700)

    # The next scan tries it again, and this time there is nothing in the way.
    await harness.manager.sweep()

    assert harness.manager.get(undeletable.sandbox_id) is None
    assert not undeletable.directory.exists()
    assert harness.manager.sandboxes() == ()


# --- the loop itself ---------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.5")
async def test_the_janitor_scans_until_it_is_stopped(harness: Harness) -> None:
    first = await harness.create()
    harness.ticker.advance(LIMITS.idle_seconds + 1)

    async with harness.janitor():
        # Starting a janitor that runs changes nothing, so a caller need not keep track.
        harness.manager.start_janitor()
        await until(lambda: first.status == "idle", "计时循环把第一个沙箱转为 idle")

    # The loop is stopped: a second sandbox whose threshold is long over stays active over
    # several scan intervals.
    second = await harness.create()
    harness.ticker.advance(LIMITS.idle_seconds + 1)
    await asyncio.sleep(JANITOR_INTERVAL_SECONDS * 3)

    assert second.status == "active"
    assert first.status == "idle"


@pytest.mark.anyio
@pytest.mark.req("15.5", "15.7", "15.9")
async def test_stopping_the_janitor_waits_for_retirement_files(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    sb = await harness.create()
    await harness.manager.freeze(sb)
    harness.ticker.advance(LIMITS.retention_seconds + 1)
    entered, release = threading.Event(), threading.Event()
    original = SandboxManager._retire_job

    def blocked_retire(manager: SandboxManager, sandbox: SandboxRuntime, now: float) -> bool:
        entered.set()
        assert release.wait(5), "retirement test did not release the worker"
        return original(manager, sandbox, now)

    monkeypatch.setattr(SandboxManager, "_retire_job", blocked_retire)
    harness.manager.start_janitor()
    stop = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        stop = asyncio.create_task(harness.manager.stop_janitor())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not stop.done()
    finally:
        release.set()
        if stop is not None:
            await stop
        else:
            await harness.manager.stop_janitor()
    assert not sb.directory.exists()


@pytest.mark.anyio
@pytest.mark.req("15.5", "15.7", "15.9")
async def test_every_transition_happens_within_a_second_of_its_condition(
    env: Env, tmp_path: Path
) -> None:
    # The one test of this file that reads a real monotonic clock (module docstring): the
    # second requirements 15.5, 15.7 and 15.9 allow between a condition and its transition
    # is elapsed time, which a test that decides what the clock says cannot measure.
    with harness_over(env, tmp_path, limits=LIVE_LIMITS, monotonic=time.monotonic) as live:
        sb = await live.create()
        async with live.janitor():
            idle_due = sb.last_done + LIVE_LIMITS.idle_seconds
            seen = await observed(
                lambda: sb.status == "idle", idle_due + ALLOWED_DELAY, "沙箱转为 idle"
            )
            assert idle_due <= seen <= idle_due + ALLOWED_DELAY
            assert not sb.store.is_open

            frozen_due = (sb.idle_since or 0.0) + LIVE_LIMITS.freeze_seconds
            seen = await observed(
                lambda: sb.status == "frozen", frozen_due + ALLOWED_DELAY, "沙箱转为 frozen"
            )
            assert frozen_due <= seen <= frozen_due + ALLOWED_DELAY

            gone_due = (sb.frozen_since or 0.0) + LIVE_LIMITS.retention_seconds
            seen = await observed(
                lambda: live.manager.get(sb.sandbox_id) is None,
                gone_due + ALLOWED_DELAY,
                "沙箱被删除",
            )
            assert gone_due <= seen <= gone_due + ALLOWED_DELAY

        assert not sb.directory.exists()
        assert live.manager.sandboxes() == ()
