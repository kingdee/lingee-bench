"""The Sandbox_Manager: the order of the requests of a sandbox and the life of a sandbox.

What is checked here is what :mod:`erp_mock.gateway.sandbox` promises an upper layer, in
the four groups of design.md 7.5: the requests of one sandbox run in arrival order and one
sandbox does not hold up another (requirement 15.4), a reset and a restore put back the
right counters and the right call count (requirements 19.4, 19.6, 18.10 and 23.12), a
frozen sandbox changes nothing any more (requirement 15.12), and the anchor date of a
sandbox outlives both (requirement 16.2).

The Snapshot every sandbox here is created from is a real SQLite Snapshot, built once per
module by :func:`build_snapshot` with the tables of the engine test fixture and nothing in
it but a counter that has been raised :data:`SNAPSHOT_COUNTER` times. That is the cheap
road: the state a reset and a restore are about - the counters, the effective calls of
requirement 18.10 and the call count beside the database - needs no business record to
exist, and building a dataset through the engine would cost a second per anchor date
without changing a single assertion of this file. The records of a dataset are what
tests/property/test_p30_determinism.py and the pack level tests work on.

The Snapshot reaches the manager through :class:`Snapshots`, a double of the
:class:`~erp_mock.gateway.sandbox.SnapshotSource` the service builds over the Snapshot
cache: it hands out the one file it was given and records what it was asked for, so a test
sees which dataset and which anchor date a creation asked for without a cache, a data
directory of built entries or a build. :class:`BrokenSnapshots` is the same double for a
Snapshot that cannot be built, which is the one way a creation fails (requirement 19.19).

No test reads the system clock (requirement 25.10): the manager is given a
:class:`~erp_mock.gateway.clock.SystemClock` over :data:`INSTANT` and a :class:`Ticker`
the test moves itself, so the ``created_at`` of a sandbox and the ``ts`` of its events are
fixed and the monotonic readings are the ones the test made.

A job of this file blocks on a :class:`threading.Event` rather than on a sleep, and the
test waits for a condition by spinning the event loop (:func:`until`), so the file runs in
well under a second and still shows which job was where when.
"""

import asyncio
import errno
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.jsonio import JsonValue
from erp_mock.config.instance import DatasetDef
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.sandbox import (
    CALLS_KEY,
    METADATA_FILENAME,
    METADATA_KEYS,
    SANDBOX_ID_PREFIX,
    SANDBOXES_DIR,
    NamedSnapshotExists,
    NamedSnapshotMissing,
    Origin,
    SandboxFrozen,
    SandboxLimitReached,
    SandboxLimits,
    SandboxManager,
    SandboxRuntime,
)
from erp_mock.gateway.trace import TRACE_FILENAME, EventName, TraceWriteError
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.loader.yaml_io import load_yaml
from erp_mock.state.schema import (
    COUNTERS_TABLE,
    EFFECTIVE_WRITES_TABLE,
    StorageSchema,
    build_schema,
)
from erp_mock.state.snapshot import BASE_DB_FILENAME, build_snapshot_db, finalize_snapshot
from erp_mock.state.store import DB_FILENAME, DIRECT_CAUSE, SandboxStore, Value, Write
from tests.helpers.fixtures import ANCHOR_DATE, DATASET, INSTANCE_DIR, fixture_loaded

# The counter the Snapshot brings with it and how far it was raised: a sandbox created from
# it continues that counter (requirement 4.6), a reset puts it back here and a restore puts
# it back to where the Named_Snapshot had it. The two names are of no business pack - what
# this file does with them is to write them into a row.
COUNTER_KIND: Final = "id"
COUNTER_KEY: Final = "fx_widget"
SNAPSHOT_COUNTER: Final = 5

# The record an effective call of this file says it wrote (requirement 18.10); the table it
# names is never read, the effective calls being rows of their own.
ENTITY: Final = "fx_widget"

# The definition file of the initial dataset of the engine test fixture, relative to its
# instance directory, as loading step 4 reads it.
DATASET_FILE: Final = f"datasets/{DATASET}.yaml"

# The session a call of this file arrives on, and a second one for the rate window, which
# is counted per session (requirement 23.12).
SESSION: Final = "se_6678a1b20c3d4e5f"
OTHER_SESSION: Final = "se_1122334455667788"

# The instant the first reading of the test clock returns; the nth reading is this plus
# n - 1 seconds, so the created_at of two sandboxes differ.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_000, tzinfo=UTC)

# How long a job of this file waits for the event that lets it go, and how long a test
# waits for a condition: both are far above what the operations here take and are only
# reached by a test that is about to fail anyway.
BLOCK_TIMEOUT: Final = 5.0
SPIN_SECONDS: Final = 0.005
SPINS: Final = 200

# How many turns of the event loop the queued requests of one sandbox get to reach its
# lock; every one of them needs a handful, and a turn costs nothing.
LOCK_TURNS: Final = 50


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- the test clock ----------------------------------------------------------------------


@dataclass(slots=True)
class Ticker:
    """A monotonic source the test moves itself (requirement 25.10).

    Every reading returns :attr:`seconds`, which :meth:`advance` raises; the manager reads
    it for the ``last_done`` of a request and for the moment a sandbox was frozen, and the
    janitor of task 21.5 will read it for its thresholds.
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


# --- the Snapshot every sandbox is created from ------------------------------------------


def build_snapshot(directory: Path, schema: StorageSchema) -> Path:
    """Build the one Snapshot of this file below ``directory`` and return its path.

    A build database with the tables of ``schema``, one counter raised
    :data:`SNAPSHOT_COUNTER` times and nothing else, written out as a Snapshot the way the
    Seed_Builder writes one (:func:`~erp_mock.state.snapshot.finalize_snapshot`). The build
    database is removed, so the directory holds the Snapshot alone.
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
    """A :class:`~erp_mock.gateway.sandbox.SnapshotSource` over one Snapshot file.

    ``asked`` records the (dataset, anchor date) of every creation, in order, which is what
    the service's own source would have looked up in the cache or built.
    """

    db: Path
    asked: list[tuple[str, date]] = field(default_factory=list)

    @contextmanager
    def snapshot(self, dataset: DatasetModel, anchor: date) -> Iterator[Path]:
        self.asked.append((dataset.name, anchor))
        yield self.db


@dataclass(slots=True)
class BrokenSnapshots:
    """A Snapshot source whose Snapshot cannot be built (requirements 15.14 and 19.19)."""

    reason: str = "初始数据集构建失败"
    asked: list[tuple[str, date]] = field(default_factory=list)

    @contextmanager
    def snapshot(self, dataset: DatasetModel, anchor: date) -> Iterator[Path]:
        self.asked.append((dataset.name, anchor))
        raise RuntimeError(f"{self.reason}：{dataset.name} {anchor.isoformat()}")
        yield Path()  # pragma: no cover - never reached; keeps this a generator


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
    snapshots: Snapshots | BrokenSnapshots
    ticker: Ticker
    env: Env

    async def create(
        self, *, anchor: date | None = None, origin: Origin = "auto"
    ) -> SandboxRuntime:
        """Create one sandbox of the fixture's profile and dataset."""
        return await self.manager.create(
            self.env.profile, self.env.dataset, anchor=anchor, origin=origin
        )


@contextmanager
def harness_over(
    env: Env,
    data_dir: Path,
    *,
    limits: SandboxLimits | None = None,
    snapshots: Snapshots | BrokenSnapshots | None = None,
) -> Iterator[Harness]:
    """Yield a harness over ``data_dir``, closing the manager and its pool on the way out."""
    ticker = Ticker()
    source = Snapshots(env.snapshot) if snapshots is None else snapshots
    manager = SandboxManager(
        data_dir=data_dir,
        loaded=env.loaded,
        schema=env.schema,
        snapshots=source,
        clock=stepping_clock(ticker),
        limits=SandboxLimits() if limits is None else limits,
    )
    try:
        yield Harness(manager=manager, snapshots=source, ticker=ticker, env=env)
    finally:
        manager.close()


@pytest.fixture
def harness(env: Env, tmp_path: Path) -> Iterator[Harness]:
    """A manager with the default thresholds over a data directory of this test."""
    with harness_over(env, tmp_path) as made:
        yield made


# --- jobs and readings -------------------------------------------------------------------


def bump(seq: int, times: int = 1) -> Callable[[SandboxRuntime], int]:
    """A job that raises the counter ``times`` and records the writes of call ``seq``.

    One transaction, the way a tools/call runs: the counter and the effective call of
    requirement 18.10 are written together, so a reset and a restore move both at once.
    """

    def job(sb: SandboxRuntime) -> int:
        with sb.store.transaction() as uow:
            value = 0
            for _ in range(times):
                value = uow.bump_counter(COUNTER_KIND, COUNTER_KEY)
            uow.record_effective_call(
                seq, [Write(entity=ENTITY, op="insert", id=f"wd_{seq:06d}", cause=DIRECT_CAUSE)]
            )
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


def failing_trace(sb: SandboxRuntime) -> None:
    """A job whose trace event cannot be written (design.md error handling).

    What the Sandbox_Manager sees when the database of a call is committed and its event is
    not: :meth:`~erp_mock.gateway.trace.TraceWriter.write` raises
    :class:`~erp_mock.gateway.trace.TraceWriteError`, which is this exactly.
    """
    raise TraceWriteError(
        sb.trace.path, sb.trace.next_seq, OSError(errno.ENOSPC, "No space left on device")
    )


def counter_of(sb: SandboxRuntime) -> int:
    """The counter the sandbox database holds for :data:`COUNTER_KEY` (requirement 4.6)."""
    rows = select(sb.store, COUNTERS_TABLE, {"kind": COUNTER_KIND, "key": COUNTER_KEY})
    return as_int(rows[0]["value"]) if rows else 0


def effective_seqs(sb: SandboxRuntime) -> list[int]:
    """The call sequence numbers the effective calls of the sandbox name (req 18.10)."""
    return sorted(as_int(row["seq"]) for row in select(sb.store, EFFECTIVE_WRITES_TABLE))


def select(
    store: SandboxStore, table: str, where: dict[str, Value] | None = None
) -> list[dict[str, Value]]:
    """Read ``table`` of an open sandbox database; no job of the sandbox is running."""
    with store.transaction() as uow:
        return [dict(row) for row in uow.select(table, where or {})]


def as_int(value: Value) -> int:
    """``value`` as the int a counter and a sequence number are stored as."""
    assert isinstance(value, int), value
    return value


def events(sb: SandboxRuntime) -> list[dict[str, JsonValue]]:
    """The events of the trace file of ``sb``, one per line (requirement 17.1)."""
    text = (sb.directory / TRACE_FILENAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


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
    """Let every task that is ready run, so queued requests reach the sandbox lock."""
    for _ in range(turns):
        await asyncio.sleep(0)


# --- creating a sandbox ------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.13")
async def test_create_gives_the_sandbox_its_own_files(harness: Harness) -> None:
    sb = await harness.create(origin="prebuilt")

    assert sb.sandbox_id.startswith(SANDBOX_ID_PREFIX)
    assert sb.directory == harness.manager.root / sb.sandbox_id
    assert harness.manager.root.name == SANDBOXES_DIR
    assert sb.store.db_path == sb.directory / DB_FILENAME
    assert sb.base_db == sb.directory / BASE_DB_FILENAME
    assert sb.base_db.is_file()
    assert sb.trace.path == sb.directory / TRACE_FILENAME
    # The sandbox starts from the content of the Snapshot of its dataset, counters included.
    assert harness.snapshots.asked == [(harness.env.dataset.name, ANCHOR_DATE)]
    assert counter_of(sb) == SNAPSHOT_COUNTER
    assert effective_seqs(sb) == []
    assert (sb.status, sb.sandbox_calls, sb.session_ids, sb.named_names()) == (
        "active",
        0,
        [],
        (),
    )
    assert harness.manager.get(sb.sandbox_id) is sb
    assert harness.manager.non_frozen_count() == 1


@pytest.mark.anyio
@pytest.mark.req("15.13")
async def test_create_writes_what_the_sandbox_is(harness: Harness) -> None:
    sb = await harness.create(origin="prebuilt")

    payload = metadata_of(sb)
    assert tuple(payload) == METADATA_KEYS
    assert payload == {
        "id": sb.sandbox_id,
        "origin": "prebuilt",
        "profile": f"{harness.env.profile.name}@{harness.env.profile.definition.version}",
        "dataset": f"{DATASET}@{harness.env.dataset.version}",
        "anchor_date": ANCHOR_DATE.isoformat(),
        "created_at": "2026-10-09T10:15:32.481+08:00",
        "status": "active",
    }
    assert sb.created_at == payload["created_at"]
    assert events(sb) == []


@pytest.mark.anyio
@pytest.mark.req("15.13")
async def test_two_sandboxes_keep_their_own_database_and_trace(harness: Harness) -> None:
    before = harness.env.snapshot.read_bytes()
    first = await harness.create()
    second = await harness.create()

    assert await harness.manager.run(first, bump(seq=1, times=2)) == SNAPSHOT_COUNTER + 2
    await harness.manager.run(first, lambda sb: sb.trace.write(EventName.TOOLS_LIST, SESSION))

    assert counter_of(first) == SNAPSHOT_COUNTER + 2
    assert counter_of(second) == SNAPSHOT_COUNTER
    assert effective_seqs(first) == [1]
    assert effective_seqs(second) == []
    assert [event["event"] for event in events(first)] == [EventName.TOOLS_LIST]
    assert events(second) == []
    # The Snapshot the two were copied from is only read (requirement 15.13).
    assert harness.env.snapshot.read_bytes() == before
    assert not list(harness.env.snapshot.parent.glob("snapshot.sqlite-*"))


@pytest.mark.anyio
@pytest.mark.req("15.11")
async def test_a_creation_that_fails_leaves_no_sandbox_behind(env: Env, tmp_path: Path) -> None:
    with harness_over(env, tmp_path, snapshots=BrokenSnapshots()) as broken:
        with pytest.raises(RuntimeError, match="初始数据集构建失败"):
            await broken.create()

        assert broken.manager.sandboxes() == ()
        assert broken.manager.non_frozen_count() == 0
        assert list(broken.manager.root.iterdir()) == []


# --- the order requests run in (requirement 15.4) ----------------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.4")
async def test_requests_of_one_sandbox_run_in_arrival_order(harness: Harness) -> None:
    sb = await harness.create()
    order: list[int] = []
    gate = threading.Event()
    entered = threading.Event()

    def job(index: int) -> Callable[[SandboxRuntime], int]:
        def run_job(sb: SandboxRuntime) -> int:
            if index == 0:
                entered.set()
                assert gate.wait(BLOCK_TIMEOUT)
            order.append(index)
            return index

        return run_job

    arrivals = [asyncio.create_task(harness.manager.run(sb, job(index))) for index in range(5)]
    await until(entered.is_set, "第一个请求开始执行")
    # Every later request is now waiting for the lock the first one holds.
    await turn_loop()
    assert order == []
    gate.set()

    assert await asyncio.gather(*arrivals) == [0, 1, 2, 3, 4]
    assert order == [0, 1, 2, 3, 4]
    assert sb.inflight == 0


@pytest.mark.anyio
@pytest.mark.req("15.4")
async def test_a_busy_sandbox_does_not_hold_up_another(harness: Harness) -> None:
    first = await harness.create()
    second = await harness.create()
    gate = threading.Event()
    entered = threading.Event()

    held = asyncio.create_task(harness.manager.run(first, blocking(entered, gate)))
    await until(entered.is_set, "第一个沙箱的请求开始执行")

    # The other sandbox is served while the first one is still inside its job.
    assert await harness.manager.run(second, bump(seq=1)) == SNAPSHOT_COUNTER + 1
    assert first.lock.locked()
    assert first.inflight == 1

    gate.set()
    assert await held == first.sandbox_id
    assert not first.lock.locked()


@pytest.mark.anyio
@pytest.mark.req("15.4")
async def test_a_cancelled_caller_does_not_interrupt_the_job(harness: Harness) -> None:
    sb = await harness.create()
    gate = threading.Event()
    entered = threading.Event()

    def writing(sb: SandboxRuntime) -> int:
        entered.set()
        assert gate.wait(BLOCK_TIMEOUT)
        return bump(seq=1, times=2)(sb)

    waiting = asyncio.create_task(harness.manager.run(sb, writing))
    await until(entered.is_set, "请求开始执行")
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    # The job is still running and still holds the lock: a transaction is never left half
    # way and the lock is never handed on in the middle of one.
    assert sb.lock.locked()
    assert sb.inflight == 1
    gate.set()
    # The next request gets the lock, which the job released itself when it was done, and
    # finds everything the cancelled call wrote.
    assert await harness.manager.run(sb, bump(seq=2)) == SNAPSHOT_COUNTER + 3
    assert effective_seqs(sb) == [1, 2]
    assert sb.inflight == 0
    assert sb.last_done == harness.ticker.seconds


@pytest.mark.anyio
@pytest.mark.req("15.6")
async def test_an_idle_sandbox_is_opened_again_before_its_request(harness: Harness) -> None:
    sb = await harness.create()
    assert await harness.manager.run(sb, bump(seq=1)) == SNAPSHOT_COUNTER + 1
    # What the janitor of task 21.5 does when the idle threshold is over: close the
    # database and leave everything else as it is (requirement 15.5).
    sb.store.close()
    sb.status = "idle"
    sb.idle_since = harness.ticker.advance(1.0)

    assert await harness.manager.run(sb, bump(seq=2)) == SNAPSHOT_COUNTER + 2

    assert (sb.status, sb.idle_since) == ("active", None)
    assert sb.store.is_open
    assert effective_seqs(sb) == [1, 2]


# --- the resource limits of a sandbox (requirement 23.12) --------------------------------


@pytest.mark.anyio
@pytest.mark.req("23.12")
async def test_the_call_count_lives_outside_the_database(harness: Harness) -> None:
    sb = await harness.create()

    def passing_then_failing(sb: SandboxRuntime) -> None:
        probe = sb.probe(SESSION, harness.ticker.seconds)
        assert probe.sandbox_calls() == 0
        assert probe.db_bytes() > 0
        probe.mark_passed()
        with pytest.raises(RuntimeError, match="业务错误"):
            with sb.store.transaction() as uow:
                uow.bump_counter(COUNTER_KIND, COUNTER_KEY)
                raise RuntimeError("业务错误")

    await harness.manager.run(sb, passing_then_failing)

    # The call passed the check, so it stays counted; its transaction rolled back.
    assert sb.sandbox_calls == 1
    assert counter_of(sb) == SNAPSHOT_COUNTER
    assert sb.probe(SESSION, harness.ticker.seconds).sandbox_calls() == 1


@pytest.mark.anyio
@pytest.mark.req("23.12")
async def test_the_rate_window_counts_the_second_before_arrival(harness: Harness) -> None:
    sb = await harness.create()
    start = harness.ticker.seconds

    first = sb.probe(SESSION, start)
    assert first.recent_session_calls() == 0
    first.mark_passed()
    second = sb.probe(SESSION, start + 0.5)
    assert second.recent_session_calls() == 1
    second.mark_passed()

    # Both calls arrived within the second before this one, the sandbox count is both.
    assert sb.probe(SESSION, start + 0.9).recent_session_calls() == 2
    assert sb.sandbox_calls == 2
    # A call a second later sees neither, and one of another session sees none of them.
    assert sb.probe(SESSION, start + 2.0).recent_session_calls() == 0
    assert sb.probe(OTHER_SESSION, start + 0.9).recent_session_calls() == 0
    assert sb.probe(OTHER_SESSION, start + 0.9).sandbox_calls() == 2


# --- reset, Named_Snapshot and restore (requirements 19.4 to 19.6) -----------------------


@pytest.mark.anyio
@pytest.mark.req("19.4", "18.10", "23.12")
async def test_reset_puts_back_the_snapshot_and_zeroes_the_call_count(harness: Harness) -> None:
    sb = await harness.create()
    await harness.manager.run(sb, bump(seq=1, times=3))
    await harness.manager.run(sb, lambda sb: sb.probe(SESSION, 1000.0).mark_passed())
    await harness.manager.save_named(sb, "keep_me")

    await harness.manager.reset(sb)

    assert counter_of(sb) == SNAPSHOT_COUNTER
    assert effective_seqs(sb) == []
    assert sb.sandbox_calls == 0
    # The trace goes on where it was, and the Named_Snapshot and the bindings are untouched.
    assert [event["event"] for event in events(sb)] == [EventName.SANDBOX_RESET]
    assert [event["seq"] for event in events(sb)] == [1]
    assert events(sb)[0]["session_id"] is None
    assert sb.named_names() == ("keep_me",)
    assert sb.status == "active"
    assert metadata_of(sb)["status"] == "active"


@pytest.mark.anyio
@pytest.mark.req("19.5", "23.12")
async def test_save_named_writes_the_snapshot_and_its_call_count(harness: Harness) -> None:
    sb = await harness.create()
    await harness.manager.run(sb, bump(seq=1, times=2))
    await harness.manager.run(sb, lambda sb: sb.probe(SESSION, 1000.0).mark_passed())
    before = (sb.directory / DB_FILENAME).read_bytes()

    await harness.manager.save_named(sb, "after_setup")

    assert sb.named_names() == ("after_setup",)
    assert json.loads(sb.named_meta("after_setup").read_bytes()) == {CALLS_KEY: 1}
    # The sandbox database and its trace are what they were (requirement 19.5).
    assert (sb.directory / DB_FILENAME).read_bytes() == before
    assert events(sb) == []
    assert counter_of(sb) == SNAPSHOT_COUNTER + 2

    with pytest.raises(NamedSnapshotExists):
        await harness.manager.save_named(sb, "after_setup")
    with pytest.raises(ValueError, match="保留名称"):
        await harness.manager.save_named(sb, "initial")
    with pytest.raises(ValueError, match="名称"):
        await harness.manager.save_named(sb, "../escape")


@pytest.mark.anyio
@pytest.mark.req("19.6", "18.10", "23.12")
async def test_restore_takes_up_the_snapshot_and_its_call_count(harness: Harness) -> None:
    sb = await harness.create()
    await harness.manager.run(sb, bump(seq=1, times=2))
    await harness.manager.run(sb, lambda sb: sb.probe(SESSION, 1000.0).mark_passed())
    await harness.manager.save_named(sb, "after_setup")
    saved = counter_of(sb)
    await harness.manager.run(sb, bump(seq=2, times=4))
    await harness.manager.run(sb, lambda sb: sb.probe(SESSION, 1001.0).mark_passed())

    await harness.manager.restore(sb, "after_setup")

    assert counter_of(sb) == saved
    assert effective_seqs(sb) == [1]
    assert sb.sandbox_calls == 1
    assert sb.named_names() == ("after_setup",)
    body = events(sb)[-1]
    assert (body["event"], body["name"], body["session_id"]) == (
        EventName.SNAPSHOT_RESTORE,
        "after_setup",
        None,
    )

    with pytest.raises(NamedSnapshotMissing):
        await harness.manager.restore(sb, "never_saved")


@pytest.mark.anyio
@pytest.mark.req("16.2")
async def test_the_anchor_date_outlives_a_reset_and_a_restore(harness: Harness) -> None:
    anchor = date(2031, 2, 28)
    sb = await harness.create(anchor=anchor)
    await harness.manager.run(sb, bump(seq=1))
    await harness.manager.save_named(sb, "one")

    await harness.manager.reset(sb)
    await harness.manager.restore(sb, "one")

    assert sb.anchor_date == anchor
    assert harness.snapshots.asked == [(harness.env.dataset.name, anchor)]
    assert metadata_of(sb)["anchor_date"] == anchor.isoformat()
    # Every event of the sandbox carries that date, the ones after the two operations too
    # (requirements 16.2 and 17.2), and the seq runs on without a gap (requirement 17.7).
    written = events(sb)
    assert [event["anchor_date"] for event in written] == [anchor.isoformat()] * 2
    assert [event["seq"] for event in written] == [1, 2]
    assert [event["event"] for event in written] == [
        EventName.SANDBOX_RESET,
        EventName.SNAPSHOT_RESTORE,
    ]


# --- a frozen sandbox (requirement 15.12) ------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("15.12")
async def test_a_frozen_sandbox_changes_nothing_any_more(harness: Harness) -> None:
    sb = await harness.create()
    await harness.manager.run(sb, bump(seq=1))
    await harness.manager.save_named(sb, "one")
    frozen_at = harness.ticker.advance(7.0)

    await harness.manager.freeze(sb)

    assert (sb.status, sb.frozen_since) == ("frozen", frozen_at)
    assert not sb.store.is_open
    assert metadata_of(sb)["status"] == "frozen"
    database = (sb.directory / DB_FILENAME).read_bytes()
    trace = (sb.directory / TRACE_FILENAME).read_bytes()
    named = sb.named_db("one").read_bytes()

    # Nothing a request could ask of it goes through, and nothing of it changes.
    for refused in (
        harness.manager.run(sb, bump(seq=2)),
        harness.manager.reset(sb),
        harness.manager.restore(sb, "one"),
        harness.manager.save_named(sb, "two"),
    ):
        with pytest.raises(SandboxFrozen):
            await refused
    assert (sb.directory / DB_FILENAME).read_bytes() == database
    assert (sb.directory / TRACE_FILENAME).read_bytes() == trace
    assert sb.named_db("one").read_bytes() == named
    assert sb.named_names() == ("one",)
    assert sb.status == "frozen"
    # A second freeze is no change either (requirement 19.13), and the Admin API goes on
    # reading the metadata of the sandbox.
    await harness.manager.freeze(sb)
    assert (sb.status, sb.frozen_since) == ("frozen", frozen_at)
    assert harness.manager.get(sb.sandbox_id) is sb
    assert harness.manager.non_frozen_count() == 0


@pytest.mark.anyio
@pytest.mark.req("15.12")
async def test_a_deleted_sandbox_leaves_nothing_behind(harness: Harness) -> None:
    sb = await harness.create()
    await harness.manager.run(sb, bump(seq=1))
    await harness.manager.save_named(sb, "one")

    await harness.manager.delete(sb)

    assert not sb.directory.exists()
    assert harness.manager.get(sb.sandbox_id) is None
    assert harness.manager.sandboxes() == ()
    assert harness.manager.non_frozen_count() == 0


@pytest.mark.anyio
@pytest.mark.req("15.12")
async def test_a_trace_event_that_cannot_be_written_freezes_the_sandbox(
    harness: Harness,
) -> None:
    sb = await harness.create()
    await harness.manager.run(sb, bump(seq=1))

    with pytest.raises(TraceWriteError):
        await harness.manager.run(sb, failing_trace)

    assert sb.status == "frozen"
    assert sb.frozen_since == harness.ticker.seconds
    assert not sb.store.is_open
    assert metadata_of(sb)["status"] == "frozen"
    with pytest.raises(SandboxFrozen):
        await harness.manager.run(sb, bump(seq=2))


# --- the concurrent sandboxes of the service (requirement 15.11) -------------------------


@pytest.mark.anyio
@pytest.mark.req("15.11")
async def test_the_limit_counts_the_sandboxes_that_are_not_frozen(env: Env, tmp_path: Path) -> None:
    with harness_over(env, tmp_path, limits=SandboxLimits(max_sandboxes=2)) as small:
        assert small.manager.limits.max_sandboxes == 2

        # Four creations at once take the two places between them, and no more.
        results = await asyncio.gather(*(small.create() for _ in range(4)), return_exceptions=True)
        made = [found for found in results if isinstance(found, SandboxRuntime)]
        refused = [found for found in results if isinstance(found, SandboxLimitReached)]
        assert len(made) == 2
        assert len(refused) == 2
        assert all(error.maximum == 2 for error in refused)
        assert small.manager.non_frozen_count() == 2
        assert len(list(small.manager.root.iterdir())) == 2

        # A frozen sandbox gives its place back; the one that fills it is a new sandbox.
        await small.manager.freeze(made[0])
        replacement = await small.create()
        assert replacement.sandbox_id not in {sb.sandbox_id for sb in made}
        assert small.manager.non_frozen_count() == 2
        with pytest.raises(SandboxLimitReached):
            await small.create()


# --- what a sandbox remembers about its sessions (requirement 19.12) --------------------


@pytest.mark.anyio
@pytest.mark.req("15.4")
async def test_a_sandbox_keeps_the_sessions_that_bound_it(harness: Harness) -> None:
    sb = await harness.create(origin="prebuilt")

    sb.bind(SESSION)
    sb.bind(OTHER_SESSION)
    sb.bind(SESSION)

    assert sb.session_ids == [SESSION, OTHER_SESSION]
