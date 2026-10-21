"""The sandboxes of one service process: their files, their order and their life.

A sandbox is one working database with one anchor date, created from the Snapshot of an
initial dataset and reached by the sessions bound to it (requirement 15.1).
:class:`SandboxRuntime` is everything the service knows about one of them and
:class:`SandboxManager` the registry of them all: it creates a sandbox, runs every request
of a sandbox one after another, resets and restores it, saves and reads back its
Named_Snapshots, freezes it and deletes it (design.md 7.5)::

    manager = SandboxManager(data_dir=data_dir, loaded=loaded, schema=schema,
                             snapshots=snapshots, clock=clock)
    sb = await manager.create(loaded.default_profile, loaded.default_dataset)
    outcome = await manager.run(sb, lambda sb: handle(sb, request))
    await manager.reset(sb)                      # Admin POST .../reset
    await manager.save_named(sb, "after_setup")  # Admin POST .../snapshots
    await manager.restore(sb, "after_setup")     # Admin POST .../restore
    await manager.freeze(sb)                     # Admin POST .../freeze
    await manager.delete(sb)                     # Admin DELETE

One sandbox is one directory below ``<data-dir>/sandboxes/`` (design.md data model 2):
``sandbox.json`` with what the sandbox is, ``base.sqlite`` linked to the Snapshot it was
created from, ``db.sqlite`` as its working database, ``trace.jsonl`` for its events and
``named/<name>.sqlite`` plus ``named/<name>.json`` per Named_Snapshot. The ``diff.json``
of design.md data model 2 is written by the stdio runtime when it exits, not here.

One request at a time, and no request blocking another sandbox (requirement 15.4).
:meth:`SandboxManager.run` is the one way a request touches a sandbox: it takes the
sandbox's :class:`asyncio.Lock`, which CPython wakes in arrival order and lets nobody jump,
hands the job to the shared thread pool - every SQLite statement of the service runs there,
never on the event loop - and releases the lock when the job is done, the trace event of
the request included. The whole of it is one task and the caller awaits it through
:func:`asyncio.shield`, so a caller that is cancelled (a client that hung up) does not stop
the job half way and does not release the lock in the middle of a transaction. Locks are
per sandbox, so two sandboxes run at the same time, each in its own pool thread.

What a reset and a restore change, and what they leave alone, is the table of design.md
data model 2. Everything a sandbox remembers inside its database - records, counters, the
effective calls of requirement 18.10 and the writeback ledger - moves with the whole file
(:meth:`~erp_mock.state.store.SandboxStore.restore_from`), so neither this module nor the
Admin API names any of it. Three things live outside the database and are handled here:

=========================  ==============  ==================  ==================  =======
what                       after a reset   after a restore     after a freeze      req
=========================  ==============  ==================  ==================  =======
the trace and its ``seq``  continues       continues           ends                17.7
the sandbox calls          0               the saved value     unchanged           23.12
the anchor date            unchanged       unchanged           unchanged            16.2
the status and bindings    unchanged       unchanged           frozen              19.4
=========================  ==============  ==================  ==================  =======

The saved value of the call count is the one the Named_Snapshot was taken with, which is
what its ``named/<name>.json`` holds.

The Snapshot a new sandbox is copied from comes from the injected
:class:`SnapshotSource`, a protocol of one method: the gateway asks for the Snapshot file
of a dataset at an anchor date and copies it while the source holds whatever it needs to
hold. The service passes an adapter over the Snapshot cache, whose ``get_or_build`` builds
a Snapshot the cache does not have (requirement 20.11) and whose ``shared`` lock keeps the
cache entry from being replaced under the copy (design.md 6.2); a test passes a source over
a file it built itself. That keeps the gateway free of the seed layer and makes a sandbox
creatable without a cache.

The concurrent sandboxes of requirement 15.10 are counted as the sandboxes that are not
frozen, and :meth:`SandboxManager.create` checks that count and takes its place in it
before its first ``await``, so two creations that arrive at once cannot both take the last
place; a creation that fails - a Snapshot that cannot be built above all - gives the place
back and leaves no directory behind (requirement 19.19).

A trace event that cannot be written freezes its sandbox (design.md error handling). The
database transaction of a call is committed before its event is written, so a sandbox whose
event was lost would keep answering with a trace that is behind its database;
:meth:`SandboxManager.run` turns such a sandbox frozen and lets the
:class:`~erp_mock.gateway.trace.TraceWriteError` through, and the caller answers that one
request with JSON-RPC -32603.

The three transitions a sandbox makes without being asked are the janitor's
(:meth:`SandboxManager.janitor`, started and stopped with
:meth:`SandboxManager.start_janitor` and :meth:`SandboxManager.stop_janitor`): every
:data:`JANITOR_INTERVAL_SECONDS` it takes one reading of the monotonic clock of the
injected :class:`~erp_mock.gateway.clock.SystemClock` - so a test drives the transitions
with a source it moves itself - and compares that reading with the timing fields of
:class:`SandboxRuntime`:

==============  ================  ========================================  ====
transition      measured from     what it does                              req
==============  ================  ========================================  ====
active to idle  ``last_done``     closes the database, sets ``idle_since``  15.5
idle to frozen  ``idle_since``    closes the trace too, writes the status   15.7
frozen to gone  ``frozen_since``  removes the directory and the entry       15.9
==============  ================  ========================================  ====

``inflight`` is the fourth: a sandbox with a request in flight is left alone. A sandbox
makes at most one of the three transitions per scan, and that needs no rule of its own:
every threshold is measured from the moment of the transition before it, and a transition
records the reading of the scan it was made in, so the next threshold cannot have run out
by the time that same scan looks again.

Every transition goes through :meth:`SandboxManager.run`, which is what makes it safe: the
sandbox lock keeps it out of the way of a request, and the job checks its condition once
more under that lock, a request that arrived between the scan and the lock having moved
``last_done`` on - such a sandbox is to stay active. The reverse transition belongs to
:meth:`SandboxManager.run` itself, which reopens the database of an idle sandbox and turns
it active again before the job (requirement 15.6). Turning idle writes no ``sandbox.json``,
unlike freezing: the status of an idle sandbox is the one thing about it that changes back
on the request path, and the file would be stale the moment a call reopened it.

A transition that fails - a directory that cannot be removed above all - is logged, and
the loop goes on with the next sandbox and the next scan (design.md error handling); a
sandbox whose files could not be removed stays in the registry and is tried again.

M0 simplification: the registry is this process's. A sandbox directory that is already
below the data directory when the service starts is neither registered again nor removed,
which is recorded in the open questions of the design.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from erp_mock.common.jsonio import JsonValue, dumps_compact, encode_utf8
from erp_mock.gateway.clock import SystemClock, resolve_anchor
from erp_mock.gateway.trace import (
    TRACE_FILENAME,
    EventName,
    TraceHeader,
    TraceWriteError,
    TraceWriter,
    snapshot_restore_body,
)
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.state.diff import INITIAL_BASE
from erp_mock.state.schema import StorageSchema
from erp_mock.state.snapshot import BASE_DB_FILENAME, copy_snapshot, link_snapshot
from erp_mock.state.store import DB_FILENAME, SandboxStore
from erp_mock.tools.layer import LimitProbe

__all__ = [
    "CALLS_KEY",
    "DEFAULT_SANDBOX_LIMITS",
    "FREEZE_SECONDS",
    "IDLE_SECONDS",
    "JANITOR_INTERVAL_SECONDS",
    "MAX_SANDBOXES",
    "METADATA_FILENAME",
    "METADATA_KEYS",
    "NAMED_DIR",
    "NAMED_NAME_PATTERN",
    "RATE_WINDOW_SECONDS",
    "RETENTION_SECONDS",
    "SANDBOXES_DIR",
    "SANDBOX_ID_PREFIX",
    "NamedSnapshotExists",
    "NamedSnapshotMissing",
    "Origin",
    "SandboxFrozen",
    "SandboxLimitReached",
    "SandboxLimits",
    "SandboxManager",
    "SandboxProbe",
    "SandboxRuntime",
    "SnapshotSource",
    "Status",
    "pool_size",
]

# Where a background failure of this module goes: the service log on stderr, never stdout,
# which carries MCP messages alone in stdio mode (design.md 7.3).
_LOG: Final = logging.getLogger(__name__)

# The directory of the sandboxes below ``--data-dir`` and the files of one sandbox
# (design.md data model 2). The working database, the basis and the trace are named by the
# modules that own them (:data:`~erp_mock.state.store.DB_FILENAME`,
# :data:`~erp_mock.state.snapshot.BASE_DB_FILENAME`,
# :data:`~erp_mock.gateway.trace.TRACE_FILENAME`).
SANDBOXES_DIR: Final = "sandboxes"
METADATA_FILENAME: Final = "sandbox.json"
NAMED_DIR: Final = "named"
_NAMED_DB_SUFFIX: Final = ".sqlite"
_NAMED_META_SUFFIX: Final = ".json"

# The keys of ``sandbox.json``, in the order design.md data model 2 lists them. The file
# says what the sandbox is; nothing reads it back in M0 (module docstring), and a sandbox
# that is created, reset, restored or frozen writes it so that a person who looks into the
# data directory sees the state the service has.
METADATA_KEYS: Final = (
    "id",
    "origin",
    "profile",
    "dataset",
    "anchor_date",
    "created_at",
    "status",
)

# The one key of ``named/<name>.json``: how many calls the sandbox had passed the limit
# check with when the Named_Snapshot was saved (requirement 23.12, design.md 7.7).
CALLS_KEY: Final = "sandbox_calls"

# A sandbox id: this prefix and 16 hexadecimal digits, drawn from the operating system's
# secure random source (requirement 24.6). The id reaches a URL path and a directory name,
# and hexadecimal digits need no escaping in either.
SANDBOX_ID_PREFIX: Final = "sb_"
_ID_BYTES: Final = 8

# The name of a Named_Snapshot (requirement 19.20). The name becomes a file name below
# ``named/``, so the pattern is what keeps a name from reaching outside that directory;
# :data:`~erp_mock.state.diff.INITIAL_BASE` is the reserved name of the initial dataset,
# which is no Named_Snapshot of a sandbox.
NAMED_NAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# How long the window of the call rate is (requirement 23.12): the calls of a session that
# passed the check within the second before this request arrived.
RATE_WINDOW_SECONDS: Final = 1.0

# The thresholds of a service whose environment configured none (requirement 15.11): five
# minutes to idle, half an hour to frozen, seven days of retention and 256 sandboxes that
# are not frozen. The CLI reads ``ERP_MOCK_IDLE_SECONDS``, ``ERP_MOCK_FREEZE_SECONDS``,
# ``ERP_MOCK_RETENTION_SECONDS`` and ``ERP_MOCK_MAX_SANDBOXES`` once at startup
# (requirement 23.9) and the values stay what they were for as long as it runs.
IDLE_SECONDS: Final = 300
FREEZE_SECONDS: Final = 1800
RETENTION_SECONDS: Final = 604800
MAX_SANDBOXES: Final = 256

# How often the janitor looks at the sandboxes (design.md 7.5): four times a second, so
# that one interval plus the transition itself stays well inside the one second
# requirements 15.5, 15.7 and 15.9 allow between a condition and its transition. A scan of
# a service whose sandboxes are all busy reads four numbers per sandbox and awaits nothing.
JANITOR_INTERVAL_SECONDS: Final = 0.25
_JANITOR_TASK: Final = "sandbox-janitor"

# The largest thread pool the SQLite work of the service runs in (design.md 7.5). Four
# threads per core, because a job waits on the disk rather than on the CPU, and never more
# than this, because every thread holds a connection of its sandbox while it runs.
_MAX_POOL: Final = 32
_THREADS_PER_CPU: Final = 4
_POOL_PREFIX: Final = "erp-mock-sandbox"

# What the manager refuses, one reason per class; the Admin API and the transports turn
# each of them into their own status code (requirements 19.16, 19.18, 23.8).
_AT_CAPACITY: Final = "非 frozen 沙箱的数量已达上限 {max} 个，无法创建新沙箱"
_FROZEN: Final = "沙箱 {sandbox_id} 已冻结，不能再执行写入、重置、恢复或创建 Named_Snapshot"
_NAME_EXISTS: Final = "沙箱 {sandbox_id} 中已存在名为 {name} 的 Named_Snapshot"
_NAME_MISSING: Final = "沙箱 {sandbox_id} 中不存在名为 {name} 的 Named_Snapshot"
_BAD_NAME: Final = "Named_Snapshot 的名称 {name!r} 不匹配 ^[a-z0-9][a-z0-9_-]{{0,63}}$"
_RESERVED_NAME: Final = f"{INITIAL_BASE} 是初始数据集基准的保留名称，不能用作 Named_Snapshot"

# What the side file of a Named_Snapshot said, where it cannot be read; the snapshot itself
# is there, so the restore goes through and the call count starts over (requirement 23.12).
_BAD_SIDE_FILE: Final = "无法读取 Named_Snapshot 侧文件 %s，沙箱调用次数按 0 计：%s"

# What a failure of the janitor says, one line per sandbox and one for a scan that could
# not even be made; the timing loop goes on either way (design.md error handling).
_TRANSITION_FAILED: Final = "沙箱 %s 的计时转换失败，下一轮扫描重试：%s"
_SWEEP_FAILED: Final = "沙箱计时扫描失败，计时循环继续：%s"


# --- what a sandbox is -------------------------------------------------------------------

# The state of a sandbox (requirement 19.12): taking calls, with its database closed after
# the idle threshold, or read-only until it is deleted (requirements 15.5, 15.7, 15.12).
type Status = Literal["active", "idle", "frozen"]

# Where a sandbox came from: an initialize that got one of its own (requirements 15.1 and
# 15.2) or the Admin API (requirement 19.3). The difference shows when a session ends: the
# sandbox of a session is frozen with it, a prebuilt one only loses that binding
# (requirements 15.7 and 15.8).
type Origin = Literal["auto", "prebuilt"]


class SnapshotSource(Protocol):
    """Where the initial content of a new sandbox comes from (design.md 6.2).

    One method, because that is all the gateway needs of the Snapshot cache: give me the
    Snapshot file of this dataset at this anchor date, and keep it there while I copy it.
    The service passes an adapter over :class:`~erp_mock.seed.cache.SnapshotCache`::

        @contextmanager
        def snapshot(self, dataset: DatasetModel, anchor: date) -> Iterator[Path]:
            ref = cache.get_or_build(dataset.definition, anchor, file=dataset.file)
            with cache.shared(ref):          # the entry cannot be replaced under us
                yield ref.db

    so a Snapshot the cache does not hold is built on the way (requirement 20.11) and the
    gateway imports nothing of the seed layer. A build that fails raises out of the method
    - :class:`~erp_mock.seed.resolve.SeedError` for the cache - and
    :meth:`SandboxManager.create` lets it through so that the caller can name the dataset
    and the anchor date in its error (requirements 15.14 and 19.19).

    The method is called in a worker thread, since building a Snapshot replays a whole
    dataset through the engine and must not run on the event loop.
    """

    def snapshot(self, dataset: DatasetModel, anchor: date) -> AbstractContextManager[Path]:
        """The Snapshot file of ``dataset`` at ``anchor``, readable for the block."""
        ...


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """The thresholds the Sandbox_Manager runs with (requirement 15.11).

    ``idle_seconds``, ``freeze_seconds`` and ``retention_seconds`` are the three timings of
    requirements 15.5, 15.7 and 15.9, in seconds of the monotonic clock, and
    ``max_sandboxes`` the number of sandboxes that are not frozen that the service holds at
    once (requirement 15.10). One per service process, fixed at startup from the
    environment (requirement 23.9); the defaults are the four of requirement 15.11.
    """

    idle_seconds: int = IDLE_SECONDS
    freeze_seconds: int = FREEZE_SECONDS
    retention_seconds: int = RETENTION_SECONDS
    max_sandboxes: int = MAX_SANDBOXES


# The thresholds of a service whose environment configured none (requirement 15.11).
DEFAULT_SANDBOX_LIMITS: Final = SandboxLimits()


class SandboxLimitReached(Exception):
    """The service already holds as many sandboxes as it may (requirement 15.10).

    Raised by :meth:`SandboxManager.create` instead of creating one; nothing was created
    and no existing sandbox was touched. An initialize that asked for a sandbox of its own
    is answered with HTTP 503 and a POST /admin/sandboxes likewise (requirement 19.18).
    """

    __slots__ = ("maximum",)

    maximum: int

    def __init__(self, maximum: int) -> None:
        super().__init__(_AT_CAPACITY.format(max=maximum))
        self.maximum = maximum


class SandboxFrozen(Exception):
    """The sandbox is frozen and stays read-only until it is deleted (req 15.12).

    Raised by :meth:`SandboxManager.run` and therefore by the reset, the restore and the
    saving of a Named_Snapshot; the Admin API answers 409 (requirement 19.16) and a request
    of a session 404, the sessions of a frozen sandbox having been terminated with it.
    """

    __slots__ = ("sandbox_id",)

    sandbox_id: str

    def __init__(self, sandbox_id: str) -> None:
        super().__init__(_FROZEN.format(sandbox_id=sandbox_id))
        self.sandbox_id = sandbox_id


class NamedSnapshotExists(Exception):
    """The sandbox already has a Named_Snapshot of that name (requirement 19.16).

    Raised by :meth:`SandboxManager.save_named`; the sandbox, its trace and its existing
    Named_Snapshots are as they were, and the Admin API answers 409.
    """

    __slots__ = ("name", "sandbox_id")

    sandbox_id: str
    name: str

    def __init__(self, sandbox_id: str, name: str) -> None:
        super().__init__(_NAME_EXISTS.format(sandbox_id=sandbox_id, name=name))
        self.sandbox_id = sandbox_id
        self.name = name


class NamedSnapshotMissing(Exception):
    """The sandbox has no Named_Snapshot of that name (requirement 19.15).

    Raised by :meth:`SandboxManager.restore`; the sandbox is unchanged and the Admin API
    answers 404, which is also what a diff against a basis that is not there gets.
    """

    __slots__ = ("name", "sandbox_id")

    sandbox_id: str
    name: str

    def __init__(self, sandbox_id: str, name: str) -> None:
        super().__init__(_NAME_MISSING.format(sandbox_id=sandbox_id, name=name))
        self.sandbox_id = sandbox_id
        self.name = name


@dataclass(slots=True)
class SandboxProbe:
    """The resource readings of one call (:class:`~erp_mock.tools.layer.LimitProbe`).

    One per tools/call, built by :meth:`SandboxRuntime.probe` when the request arrives:
    ``session_id`` is the session it arrived on and ``arrival`` the reading of the
    monotonic clock at that moment, which the rate window is measured against and which
    :meth:`mark_passed` records (requirement 23.12).

    The call count and the arrival times live on the runtime, outside the sandbox database,
    so a call that passed the check stays counted however it ends: an error rolls its
    transaction back and leaves the count where it is (design.md issue 4). The database
    size is read off the open database, which is where the call is about to run.
    """

    runtime: "SandboxRuntime"
    session_id: str
    arrival: float

    def sandbox_calls(self) -> int:
        """How many calls of this sandbox have passed the check (requirement 23.12)."""
        return self.runtime.sandbox_calls

    def db_bytes(self) -> int:
        """The size of the sandbox database, its page count times its page size."""
        return self.runtime.store.db_bytes()

    def recent_session_calls(self) -> int:
        """The passed calls of this session in the second before arrival (req 23.12).

        The arrival times older than that second are dropped as they are counted, so the
        window holds the calls of one second and no more: a reading is the number of calls
        whose arrival is after ``arrival`` minus :data:`RATE_WINDOW_SECONDS`, the one that
        is exactly a second old not among them.
        """
        passed = self.runtime.passed_at.get(self.session_id)
        if passed is None:
            return 0
        edge = self.arrival - RATE_WINDOW_SECONDS
        while passed and passed[0] <= edge:
            passed.popleft()
        return len(passed)

    def mark_passed(self) -> None:
        """Record that this call passed the check (requirement 23.12)."""
        self.runtime.sandbox_calls += 1
        self.runtime.passed_at.setdefault(self.session_id, deque()).append(self.arrival)


@dataclass(slots=True)
class SandboxRuntime:
    """One sandbox of this process: its files, its state and its timing (design.md 7.5).

    What it is, which no request changes (requirements 16.2 and 19.12):

    - ``sandbox_id``: :data:`SANDBOX_ID_PREFIX` and 16 hexadecimal digits;
    - ``directory``: the sandbox directory, below ``<data-dir>/sandboxes/``;
    - ``profile`` and ``dataset``: the tool profile its sessions see and the initial
      dataset it was created from;
    - ``anchor_date``: the business date of every call of it, fixed at creation and
      unchanged by a reset, a restore and the turn of the day (requirement 16.2);
    - ``origin``: ``auto`` for the sandbox of a session, ``prebuilt`` for one of the
      Admin API (requirements 15.1 and 19.3);
    - ``created_at``: the instant it was created, ISO 8601 with offset and milliseconds
      (requirement 19.12).

    What changes while it lives:

    - ``status``: active, idle or frozen (requirements 15.5, 15.7, 15.12);
    - ``session_ids``: the sessions that were ever bound to it, in first-binding order
      (requirement 19.12); a session that ends is not taken off, the Admin API reporting
      the sessions that were bound rather than the ones that still are;
    - ``store`` and ``trace``: the working database, closed while the sandbox is idle
      (requirement 15.5), and the writer of its trace file;
    - ``lock``: the lock every request of this sandbox takes, in arrival order
      (requirement 15.4, :meth:`SandboxManager.run`);
    - ``sandbox_calls`` and ``passed_at``: the calls that passed the limit check, kept
      outside the database so that a failed call stays counted, and the arrival times of
      those calls per session (requirement 23.12, :class:`SandboxProbe`);
    - ``inflight``, ``last_done``, ``idle_since``, ``frozen_since``: what the janitor of
      task 21.5 reads - how many requests are running, when the last one finished, and
      since when the sandbox has been idle or frozen, all on the monotonic clock
      (requirements 15.5, 15.7, 15.9).

    Only :class:`SandboxManager` builds one, and every field but the three identifying
    ones is read and written under :attr:`lock`.
    """

    sandbox_id: str
    directory: Path
    profile: ProfileModel
    dataset: DatasetModel
    anchor_date: date
    origin: Origin
    created_at: str
    store: SandboxStore
    trace: TraceWriter
    lock: asyncio.Lock
    status: Status = "active"
    session_ids: list[str] = field(default_factory=list)
    sandbox_calls: int = 0
    passed_at: dict[str, deque[float]] = field(default_factory=dict)
    inflight: int = 0
    last_done: float = 0.0
    idle_since: float | None = None
    frozen_since: float | None = None

    @property
    def profile_ref(self) -> str:
        """The profile as ``<name>@<version>``, as a trace event and the Admin API name it."""
        return _ref(self.profile.name, self.profile.definition.version)

    @property
    def dataset_ref(self) -> str:
        """The dataset as ``<name>@<version>`` (requirements 17.2 and 19.12)."""
        return _ref(self.dataset.name, self.dataset.version)

    @property
    def base_db(self) -> Path:
        """``base.sqlite``: the Snapshot of its creation, which a reset restores from."""
        return self.directory / BASE_DB_FILENAME

    @property
    def named_dir(self) -> Path:
        """``named/``: the directory of the Named_Snapshots of this sandbox."""
        return self.directory / NAMED_DIR

    def named_db(self, name: str) -> Path:
        """The database file of the Named_Snapshot ``name``."""
        return self.named_dir / f"{name}{_NAMED_DB_SUFFIX}"

    def named_meta(self, name: str) -> Path:
        """The side file of the Named_Snapshot ``name``, which holds its call count."""
        return self.named_dir / f"{name}{_NAMED_META_SUFFIX}"

    def named_names(self) -> tuple[str, ...]:
        """The Named_Snapshots of this sandbox, by code point of their names.

        Read off the directory, which is where they are: a restore and a diff look up a
        basis by name (requirement 19.15) and nothing else keeps the list.
        """
        if not self.named_dir.is_dir():
            return ()
        return tuple(
            sorted(
                path.name.removesuffix(_NAMED_DB_SUFFIX)
                for path in self.named_dir.iterdir()
                if path.name.endswith(_NAMED_DB_SUFFIX)
            )
        )

    def bind(self, session_id: str) -> None:
        """Record that ``session_id`` is bound to this sandbox (requirement 19.12).

        The first binding of a session is kept, a session that binds again changes
        nothing; several sessions may share a prebuilt sandbox (requirement 15.3).
        """
        if session_id not in self.session_ids:
            self.session_ids.append(session_id)

    def mark_activity(self, now: float) -> None:
        """Record ``now`` as the latest activity of this sandbox (requirement 15.5).

        ``now`` is a reading of
        :meth:`~erp_mock.gateway.clock.SystemClock.monotonic`, taken when a request of a
        session bound to this sandbox arrived. It moves the moment the idle threshold is
        measured from, which is the same field :meth:`SandboxManager.run` moves when a job
        of the sandbox finishes: a request that does not run a job - a ping, which needs
        neither the lock nor the database (:mod:`erp_mock.gateway.handlers`) - is an MCP
        request all the same and keeps the sandbox from turning idle (requirement 15.5).
        The caller is the transport of the session, through the ``activity`` hook of
        :class:`~erp_mock.gateway.message_filter.FilterHooks`.

        What it does not do, deliberately: it does not reopen the database and does not
        change the status. Turning an idle sandbox active again belongs to
        :meth:`SandboxManager.run`, which reopens the database before the job
        (requirement 15.6), and a request that needs no database has no use for an open
        one. The freeze threshold therefore stays measured from ``idle_since``, which this
        does not touch: a sandbox that has already turned idle freezes on schedule
        (requirement 15.7, design.md 7.5).
        """
        self.last_done = now

    def probe(self, session_id: str, arrival: float) -> LimitProbe:
        """The resource readings of a call of ``session_id`` that arrived at ``arrival``.

        ``arrival`` is a reading of
        :meth:`~erp_mock.gateway.clock.SystemClock.monotonic` taken when the request
        arrived, which is what the rate window of requirement 23.12 is measured against.
        """
        return SandboxProbe(runtime=self, session_id=session_id, arrival=arrival)

    def metadata(self) -> dict[str, JsonValue]:
        """The object ``sandbox.json`` holds, its keys in the order of :data:`METADATA_KEYS`."""
        return {
            "id": self.sandbox_id,
            "origin": self.origin,
            "profile": self.profile_ref,
            "dataset": self.dataset_ref,
            "anchor_date": self.anchor_date.isoformat(),
            "created_at": self.created_at,
            "status": self.status,
        }


def pool_size() -> int:
    """How many threads the SQLite work of the service runs in (design.md 7.5)."""
    return min(_MAX_POOL, (os.cpu_count() or 1) * _THREADS_PER_CPU)


class SandboxManager:
    """The sandboxes of one service process, and the order their requests run in.

    - ``data_dir``: ``--data-dir``, whose ``sandboxes/`` the directories go below;
    - ``loaded``: the validated instance configuration, which gives the ``instance`` of a
      trace event and the ``clock`` an anchor date is resolved against (requirement 16.1);
    - ``schema``: the tables of that instance, which every sandbox database holds;
    - ``snapshots``: where the Snapshot of a new sandbox comes from
      (:class:`SnapshotSource`);
    - ``clock``: the one clock of the service, read for the ``created_at`` of a sandbox,
      the ``ts`` of its events and every timing of its life;
    - ``limits``: the thresholds of requirement 15.11;
    - ``executor``: the shared thread pool every SQLite statement runs in, one of
      :func:`pool_size` threads when none is given; the manager shuts down the pool it
      made itself and leaves a given one alone.

    One manager per service process. Its registry and its placeholders are read and
    written on the event loop alone, so no lock of its own is needed: the methods that
    change them do so between two ``await``s (:meth:`create`, :meth:`delete`).
    """

    __slots__ = (
        "_clock",
        "_executor",
        "_instance_ref",
        "_janitor",
        "_limits",
        "_loaded",
        "_owns_executor",
        "_reserved",
        "_root",
        "_sandboxes",
        "_schema",
        "_snapshots",
    )

    def __init__(
        self,
        *,
        data_dir: Path,
        loaded: LoadedInstance,
        schema: StorageSchema,
        snapshots: SnapshotSource,
        clock: SystemClock,
        limits: SandboxLimits = DEFAULT_SANDBOX_LIMITS,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._root = data_dir / SANDBOXES_DIR
        self._loaded = loaded
        self._instance_ref = f"{loaded.instance.instance}@{loaded.instance.version}"
        self._schema = schema
        self._snapshots = snapshots
        self._clock = clock
        self._limits = limits
        self._owns_executor = executor is None
        self._executor = (
            ThreadPoolExecutor(max_workers=pool_size(), thread_name_prefix=_POOL_PREFIX)
            if executor is None
            else executor
        )
        self._sandboxes: dict[str, SandboxRuntime] = {}
        # Creations that have taken their place among the concurrent sandboxes and have not
        # reached the registry yet (:meth:`create`).
        self._reserved = 0
        # The timing loop, while one is running (:meth:`start_janitor`).
        self._janitor: asyncio.Task[None] | None = None

    @property
    def root(self) -> Path:
        """``<data-dir>/sandboxes/``, the directory the sandbox directories are below."""
        return self._root

    @property
    def limits(self) -> SandboxLimits:
        """The thresholds this manager runs with (requirement 15.11)."""
        return self._limits

    @property
    def clock(self) -> SystemClock:
        """The clock every timing of a sandbox is read from (requirement 24.7)."""
        return self._clock

    @property
    def executor(self) -> ThreadPoolExecutor:
        """The shared thread pool the SQLite work runs in (design.md 7.5)."""
        return self._executor

    # --- the registry ------------------------------------------------------------------

    def get(self, sandbox_id: str) -> SandboxRuntime | None:
        """The sandbox ``sandbox_id``, or None where this process has none of that id.

        None is what a request for a sandbox that was deleted gets, which the Admin API
        answers with 404 (requirement 19.15).
        """
        return self._sandboxes.get(sandbox_id)

    def sandboxes(self) -> tuple[SandboxRuntime, ...]:
        """Every sandbox of this process, in creation order.

        The Admin API orders its own listing by created_at and sandbox_id
        (requirement 19.11); this is the set it orders.
        """
        return tuple(self._sandboxes.values())

    def non_frozen_count(self) -> int:
        """How many sandboxes count against the concurrency limit (requirement 15.10).

        The sandboxes that are not frozen, idle and prebuilt ones included, plus the
        creations that have taken their place and are still running (:meth:`create`).
        """
        return sum(1 for sb in self._sandboxes.values() if sb.status != "frozen") + self._reserved

    # --- creating a sandbox (requirements 15.1, 19.3) ----------------------------------

    async def create(
        self,
        profile: ProfileModel,
        dataset: DatasetModel,
        *,
        anchor: date | None = None,
        origin: Origin = "auto",
    ) -> SandboxRuntime:
        """Create an active sandbox of ``dataset`` and return it (requirement 15.1).

        ``anchor`` is the anchor date asked for this one sandbox - the ``anchor_date`` of
        an Admin request, the ``--anchor-date`` of stdio - and None leaves it to the clock
        configuration, which is the priority of requirement 16.1 resolved once, here. The
        sandbox gets a directory of its own below :attr:`root` with ``db.sqlite`` copied
        from the Snapshot of ``dataset`` at that date, ``base.sqlite`` linked to the same
        Snapshot (requirement 15.13), a trace writer on ``trace.jsonl`` and a
        ``sandbox.json`` of what it is; no session is bound, which the caller does with
        :meth:`SandboxRuntime.bind`.

        The copy and the Snapshot the source may have to build run in the thread pool. The
        place among the concurrent sandboxes is taken before that, without an ``await`` in
        between, so two creations that arrive at once cannot both take the last one
        (requirement 15.10).

        Raises :class:`SandboxLimitReached` where the service already holds as many
        sandboxes as it may, and whatever the Snapshot source raises where the Snapshot
        cannot be had - :class:`~erp_mock.seed.resolve.SeedError` for the cache
        (requirements 15.14 and 19.19). Either way nothing is created, no directory is
        left behind and the place is given back.
        """
        anchor_date = resolve_anchor(anchor, self._loaded.instance.clock, self._clock)
        if self.non_frozen_count() >= self._limits.max_sandboxes:
            raise SandboxLimitReached(self._limits.max_sandboxes)
        self._reserved += 1
        try:
            sandbox_id = self._new_id()
            created_at = self._clock.now_iso_ms(self._timezone)
            started = self._clock.monotonic()
            # The lock is made on the event loop, which is where it is taken and released.
            lock = asyncio.Lock()
            runtime = await self._in_thread(
                lambda: self._make(
                    sandbox_id=sandbox_id,
                    profile=profile,
                    dataset=dataset,
                    anchor_date=anchor_date,
                    origin=origin,
                    created_at=created_at,
                    started=started,
                    lock=lock,
                )
            )
            self._sandboxes[sandbox_id] = runtime
        finally:
            self._reserved -= 1
        return runtime

    def _make(
        self,
        *,
        sandbox_id: str,
        profile: ProfileModel,
        dataset: DatasetModel,
        anchor_date: date,
        origin: Origin,
        created_at: str,
        started: float,
        lock: asyncio.Lock,
    ) -> SandboxRuntime:
        """Build the sandbox ``sandbox_id`` on disk and return its runtime (worker thread).

        Everything that touches the file system is here: the directory, the two databases,
        the trace writer and the metadata file. A step that fails removes what the previous
        ones made, so a creation that raises leaves no sandbox directory (requirement
        19.19).
        """
        directory = self._root / sandbox_id
        directory.mkdir(parents=True)
        store: SandboxStore | None = None
        trace: TraceWriter | None = None
        try:
            with self._snapshots.snapshot(dataset, anchor_date) as snapshot_db:
                copy_snapshot(snapshot_db, directory / DB_FILENAME)
                link_snapshot(snapshot_db, directory / BASE_DB_FILENAME)
            store = SandboxStore(directory, self._schema)
            store.open()
            header = TraceHeader(
                sandbox_id=sandbox_id,
                instance=self._instance_ref,
                profile=_ref(profile.name, profile.definition.version),
                dataset=_ref(dataset.name, dataset.version),
                anchor_date=anchor_date,
                timezone=self._timezone,
            )
            trace = TraceWriter(directory / TRACE_FILENAME, header, self._clock)
            runtime = SandboxRuntime(
                sandbox_id=sandbox_id,
                directory=directory,
                profile=profile,
                dataset=dataset,
                anchor_date=anchor_date,
                origin=origin,
                created_at=created_at,
                store=store,
                trace=trace,
                lock=lock,
                last_done=started,
            )
            _write_json(directory / METADATA_FILENAME, runtime.metadata())
            return runtime
        except BaseException:
            if trace is not None:
                _close_quietly(trace.close, directory)
            if store is not None:
                _close_quietly(store.close, directory)
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def _new_id(self) -> str:
        """A sandbox id this process does not use yet (requirement 24.6)."""
        while True:
            sandbox_id = SANDBOX_ID_PREFIX + secrets.token_hex(_ID_BYTES)
            if sandbox_id not in self._sandboxes:
                return sandbox_id

    # --- running one request of one sandbox (requirement 15.4) -------------------------

    async def run[T](
        self,
        sb: SandboxRuntime,
        job: Callable[[SandboxRuntime], T],
        *,
        activity: bool = True,
        allow_frozen: bool = False,
    ) -> T:
        """Run ``job`` on ``sb`` alone, in arrival order, and return what it returns.

        Every MCP request of a sandbox and every change the Admin API makes to it goes
        through here (requirement 15.4): the sandbox lock is taken, the database of an idle
        sandbox is opened again and the sandbox turned active (requirement 15.6), ``job``
        runs in the shared thread pool - it is the job that reads and writes the database
        and writes the trace event of the request - and the lock is released when it is
        done. The lock wakes its waiters in arrival order, so the requests of a sandbox run
        in the order the service received them, and a sandbox with a request in flight does
        not hold up any other sandbox.

        ``activity`` says whether this counts as a call for the idle timing of
        requirement 15.5, which it does for a request of a session and for the reset, the
        restore and the Named_Snapshot of the Admin API, and does not for a transition the
        janitor makes; the timing is recorded whether ``job`` returned or raised.
        ``allow_frozen`` lets the job run on a frozen sandbox, which only freezing and
        deleting it may (requirement 15.12); such a job needs no database, so an idle
        sandbox is not opened again for it.

        The whole of it is one task, awaited through :func:`asyncio.shield`: a caller that
        is cancelled - a client that hung up, a request that timed out - raises
        :class:`asyncio.CancelledError` here while the job runs on to its end and the lock
        is released after it, never in the middle of a transaction.

        Raises :class:`SandboxFrozen` where the sandbox is frozen and ``allow_frozen`` is
        false, and whatever ``job`` raises. A job that raises
        :class:`~erp_mock.gateway.trace.TraceWriteError` freezes the sandbox first
        (design.md error handling): its database is committed and its event is not, so it
        answers nothing else.
        """
        task = asyncio.create_task(
            self._locked(sb, job, activity, allow_frozen), name=f"run-{sb.sandbox_id}"
        )
        # A caller that is cancelled stops awaiting the task; this keeps the exception of
        # such a task from being reported as never retrieved.
        task.add_done_callback(_retrieve)
        return await asyncio.shield(task)

    async def _locked[T](
        self,
        sb: SandboxRuntime,
        job: Callable[[SandboxRuntime], T],
        activity: bool,
        allow_frozen: bool,
    ) -> T:
        """Hold the sandbox lock while ``job`` runs in the pool (:meth:`run`)."""
        async with sb.lock:
            if sb.status == "frozen" and not allow_frozen:
                raise SandboxFrozen(sb.sandbox_id)
            sb.inflight += 1
            try:
                # Freezing and deleting are the only jobs that may run on a frozen sandbox
                # and neither needs the database; every other job gets the database of an
                # idle sandbox opened again and the sandbox turned active first, so the
                # call is handled exactly as it would have been on an active sandbox
                # (requirement 15.6).
                if sb.status == "idle" and not allow_frozen:
                    await self._in_thread(sb.store.open)
                    sb.status = "active"
                    sb.idle_since = None
                return await self._in_thread(lambda: job(sb))
            except TraceWriteError:
                await self._in_thread(lambda: self._freeze(sb))
                raise
            finally:
                sb.inflight -= 1
                if activity:
                    sb.last_done = self._clock.monotonic()

    # --- what the Admin API does to a sandbox (requirement 19.4 to 19.6, 19.13) --------

    async def reset(self, sb: SandboxRuntime) -> None:
        """Reset ``sb`` to the Snapshot it was created from (requirement 19.4).

        The whole database becomes the content of ``base.sqlite`` - its records, its
        counters and its effective calls (requirement 18.10) - the call count goes back to
        0 (requirement 23.12) and a ``sandbox_reset`` event is written; the anchor date,
        the trace written so far, the Named_Snapshots and the bindings are untouched
        (requirement 16.2). The rate window of each session is kept: how fast a session
        called is about the session, not about the content of the sandbox.

        Raises :class:`SandboxFrozen` for a frozen sandbox (requirement 19.16).
        """
        await self.run(sb, self._reset_job)

    def _reset_job(self, sb: SandboxRuntime) -> None:
        """Restore the basis, zero the call count and write the event (worker thread).

        ``sandbox.json`` is not written again: a reset changes nothing it holds, the
        anchor date and the status among it (requirements 19.4 and 16.2).
        """
        sb.store.restore_from(sb.base_db)
        sb.sandbox_calls = 0
        sb.trace.write(EventName.SANDBOX_RESET, None)

    async def save_named(self, sb: SandboxRuntime, name: str) -> None:
        """Save the content of ``sb`` as the Named_Snapshot ``name`` (requirement 19.5).

        ``named/<name>.sqlite`` gets the whole database, the counters and the effective
        calls among it, and ``named/<name>.json`` the call count of this moment, which a
        restore of this snapshot takes up again (requirement 23.12). The sandbox database
        and its trace stay exactly as they are, and no event is written.

        Raises ValueError for a ``name`` that is not a Named_Snapshot name
        (:data:`NAMED_NAME_PATTERN`, requirement 19.20), :class:`NamedSnapshotExists`
        where the sandbox has one of that name already and :class:`SandboxFrozen` for a
        frozen sandbox (requirement 19.16).
        """
        _check_name(name)
        await self.run(sb, lambda sb: self._save_job(sb, name))

    def _save_job(self, sb: SandboxRuntime, name: str) -> None:
        """Write the two files of the Named_Snapshot ``name`` (worker thread)."""
        database = sb.named_db(name)
        meta = sb.named_meta(name)
        if database.exists() or meta.exists():
            raise NamedSnapshotExists(sb.sandbox_id, name)
        sb.store.save_to(database)
        try:
            _write_json(meta, {CALLS_KEY: sb.sandbox_calls})
        except BaseException:
            # Either both files are there or neither is: a snapshot without its call count
            # would restore to a count that is not the one it was saved with.
            database.unlink(missing_ok=True)
            raise

    async def restore(self, sb: SandboxRuntime, name: str) -> None:
        """Restore ``sb`` to the Named_Snapshot ``name`` (requirement 19.6).

        The whole database becomes the content of that snapshot, its counters and its
        effective calls included, and the call count becomes the one the snapshot was saved
        with (requirements 18.10 and 23.12); a ``snapshot_restore`` event naming it is
        written. The anchor date, the trace written so far, the bindings and every
        Named_Snapshot of the sandbox are untouched (requirement 16.2).

        Raises ValueError for a ``name`` that is not a Named_Snapshot name,
        :class:`NamedSnapshotMissing` where the sandbox has none of that name
        (requirement 19.15) and :class:`SandboxFrozen` for a frozen sandbox
        (requirement 19.16).
        """
        _check_name(name)
        await self.run(sb, lambda sb: self._restore_job(sb, name))

    def _restore_job(self, sb: SandboxRuntime, name: str) -> None:
        """Restore the Named_Snapshot ``name`` and write the event (worker thread)."""
        database = sb.named_db(name)
        if not database.is_file():
            raise NamedSnapshotMissing(sb.sandbox_id, name)
        sb.store.restore_from(database)
        sb.sandbox_calls = _read_calls(sb.named_meta(name))
        sb.trace.write(EventName.SNAPSHOT_RESTORE, None, snapshot_restore_body(name))

    async def freeze(self, sb: SandboxRuntime) -> None:
        """Turn ``sb`` frozen, which makes it read-only (requirements 15.7 and 15.12).

        The database and the trace writer are closed and the status is written to
        ``sandbox.json``; the files themselves - the database, the trace and every
        Named_Snapshot - stay as they are until the sandbox is deleted, and the Admin API
        goes on reading its metadata, its trace and its diff. Freezing a frozen sandbox
        changes nothing, which is what requirement 19.13 asks of a second freeze.
        """
        await self.run(sb, self._freeze, activity=False, allow_frozen=True)

    def _freeze(self, sb: SandboxRuntime) -> None:
        """Turn ``sb`` frozen and close what it holds open (worker thread)."""
        if sb.status == "frozen":
            return
        sb.status = "frozen"
        sb.frozen_since = self._clock.monotonic()
        sb.idle_since = None
        _close_quietly(sb.store.close, sb.directory)
        _close_quietly(sb.trace.close, sb.directory)
        try:
            _write_json(sb.directory / METADATA_FILENAME, sb.metadata())
        except OSError as error:
            # The sandbox is frozen whether the file could be written or not; nothing reads
            # the file back in M0 (module docstring).
            _LOG.warning("无法写入沙箱元数据 %s：%s", sb.directory / METADATA_FILENAME, error)

    async def delete(self, sb: SandboxRuntime) -> None:
        """Delete ``sb``: its database, trace, Named_Snapshots and metadata (req 19.9).

        The sandbox leaves the registry first, so nothing finds it any more and the Admin
        API answers 404 for its id from here on (requirement 19.15); its directory is then
        removed under its own lock, which lets a request that is still running finish
        first. A sandbox in any state may be deleted, a frozen one included.
        """
        self._sandboxes.pop(sb.sandbox_id, None)
        await self.run(sb, self._delete_job, activity=False, allow_frozen=True)

    def _delete_job(self, sb: SandboxRuntime) -> None:
        """Close the sandbox and remove its directory (worker thread)."""
        _close_quietly(sb.store.close, sb.directory)
        _close_quietly(sb.trace.close, sb.directory)
        shutil.rmtree(sb.directory, ignore_errors=True)

    # --- the timing of a sandbox's life (requirements 15.5, 15.7, 15.9) ----------------

    def start_janitor(self) -> None:
        """Start the timing loop of this manager, if it is not running already.

        What the service does once it has an event loop: from here on an active sandbox
        that nobody calls turns idle, an idle one frozen and a frozen one is deleted, each
        within a second of its threshold (module docstring). Starting a running janitor
        changes nothing, so a caller need not keep track. Must be called from the event
        loop the manager's requests run on, since the loop is a task of it.
        """
        if self._janitor is None or self._janitor.done():
            self._janitor = asyncio.create_task(self.janitor(), name=_JANITOR_TASK)

    async def stop_janitor(self) -> None:
        """Stop the timing loop and wait until it is gone; no sandbox is changed.

        What the service does when it stops, before :meth:`close`. The loop is cancelled
        between two scans or in the middle of one; a transition that has reached its job is
        shielded by :meth:`run` and runs to its end, so no sandbox is left half way
        through one. Stopping a manager with no janitor does nothing.
        """
        task, self._janitor = self._janitor, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def janitor(self) -> None:
        """Scan the sandboxes every :data:`JANITOR_INTERVAL_SECONDS` until cancelled.

        The timing loop of requirements 15.5, 15.7 and 15.9 (design.md 7.5), normally run
        as the task of :meth:`start_janitor`. It sleeps first, so a sandbox created just
        now gets a whole interval before anybody looks at it, and it ends only by
        cancellation: a scan that raises at all - which :meth:`sweep` already keeps to the
        sandbox it happened on - is logged and the next one is made all the same, because
        a service whose timing stopped would hold every sandbox it ever made
        (design.md error handling).
        """
        while True:
            await asyncio.sleep(JANITOR_INTERVAL_SECONDS)
            try:
                await self.sweep()
            except Exception as error:
                _LOG.warning(_SWEEP_FAILED, error)

    async def sweep(self) -> None:
        """Make every transition that is due, in one reading of the clock (:meth:`janitor`).

        One scan: the registry is taken as it is now, so a sandbox created or deleted while
        the scan runs neither disturbs it nor is looked at twice, and every sandbox is
        measured against the one reading this took - a transition therefore records the
        moment the scan saw the condition, not the moment its lock came free. The sandboxes
        that are due are then transitioned at the same time, each waiting for its own lock
        alone, so one busy sandbox cannot keep another past its second (requirement 15.4).

        Public because it is one scan of the loop and nothing else: a test that drives the
        thresholds with a monotonic source of its own calls it instead of waiting.
        """
        now = self._clock.monotonic()
        due = tuple(sb for sb in self.sandboxes() if self._due(sb, now))
        if due:
            transitions = asyncio.gather(*(self._transition(sb, now) for sb in due))
            try:
                await asyncio.shield(transitions)
            except asyncio.CancelledError:
                # run() shields each worker job. Drain the scan as well, so stopping
                # the janitor does not return before those jobs finish removing files.
                await transitions
                raise

    def _due(self, sb: SandboxRuntime, now: float) -> bool:
        """Whether ``sb`` is due for a transition at ``now`` (:meth:`sweep`).

        The reading of the three thresholds off the timing fields, on the event loop and
        without a lock: it says which sandboxes are worth taking a lock for, and the job of
        each transition reads the same condition again under that lock. A threshold counts
        as reached once it is over - "连续超过" of requirements 15.5, 15.7 and 15.9 - so a
        sandbox exactly at its threshold stays where it is and the scan 250 ms later moves
        it. A sandbox whose timing field is missing is never due; only a sandbox state this
        module did not make itself can be in that position.
        """
        limits = self._limits
        if sb.status == "active":
            return sb.inflight == 0 and now - sb.last_done > limits.idle_seconds
        if sb.status == "idle":
            return sb.idle_since is not None and now - sb.idle_since > limits.freeze_seconds
        return sb.frozen_since is not None and now - sb.frozen_since > limits.retention_seconds

    async def _transition(self, sb: SandboxRuntime, now: float) -> None:
        """Make the one transition ``sb`` is due for at ``now`` (:meth:`sweep`).

        Each of the three goes through :meth:`run` with ``activity=False``, a transition
        being no call of the sandbox (requirement 15.5), and with ``allow_frozen=True``:
        freezing and deleting may touch a frozen sandbox anyway, and turning one idle must
        not have :meth:`run` reopen the database it is about to close - nor fail on a
        sandbox that was frozen between the scan and the lock, which the job of the
        transition sees for itself.

        A transition that raises is logged and left at that: the sandbox keeps the state it
        had, the other sandboxes of this scan are unaffected and the next scan tries again
        (design.md error handling).
        """
        try:
            if sb.status == "active":
                await self.run(
                    sb, lambda sb: self._idle_job(sb, now), activity=False, allow_frozen=True
                )
            elif sb.status == "idle":
                await self.run(
                    sb, lambda sb: self._freeze_job(sb, now), activity=False, allow_frozen=True
                )
            else:
                await self._retire(sb, now)
        except Exception as error:
            _LOG.warning(_TRANSITION_FAILED, sb.sandbox_id, error)

    def _idle_job(self, sb: SandboxRuntime, now: float) -> None:
        """Close the database of ``sb`` and turn it idle (worker thread, req 15.5).

        The condition of the scan, read again under the sandbox lock: a request that
        arrived in between has moved ``last_done`` to a later reading than ``now`` and the
        sandbox stays active, which is the whole point of making the transition through
        :meth:`run`. ``inflight`` is not read here - it counts this job itself - and needs
        no reading, the lock being held: a request of this sandbox has either finished,
        and then moved ``last_done``, or has not started.

        What is left as it is: the database file, the trace and its writer, the
        Named_Snapshots, the session bindings and the anchor date (requirement 15.5).
        ``idle_since`` becomes ``now``, from which the freeze threshold is counted.
        """
        if sb.status != "active" or now - sb.last_done <= self._limits.idle_seconds:
            return
        # A database that cannot be closed is still a database nobody is going to use; the
        # sandbox turns idle and :meth:`run` opens it again for the next call, as it would
        # have had to anyway (requirement 15.6).
        _close_quietly(sb.store.close, sb.directory)
        sb.status = "idle"
        sb.idle_since = now

    def _freeze_job(self, sb: SandboxRuntime, now: float) -> None:
        """Freeze ``sb``, which has been idle past the freeze threshold (req 15.7).

        The condition read again under the lock: a call of the sandbox has turned it active
        again by now (requirement 15.6) and its freeze threshold starts over when it next
        turns idle. The freeze itself is :meth:`_freeze`, the same one the Admin API and a
        session that ends reach, so a sandbox frozen by its threshold is frozen in exactly
        the same state as any other.
        """
        if sb.status != "idle" or sb.idle_since is None:
            return
        if now - sb.idle_since <= self._limits.freeze_seconds:
            return
        self._freeze(sb)

    async def _retire(self, sb: SandboxRuntime, now: float) -> None:
        """Delete ``sb``, whose retention period is over (requirement 15.9).

        The sandbox leaves the registry before its files go, as in :meth:`delete`, so that
        nothing finds it while the directory is being removed and the Admin API answers 404
        for its id from here on (requirement 19.15). A sandbox the job did not delete goes
        back into the registry and is looked at again on the next scan: that is a directory
        that could not be removed, which is the one thing here that fails in practice, and
        - for completeness - a sandbox whose retention period turned out not to be over
        after all, which a frozen sandbox's cannot, frozen being final
        (requirement 15.12).
        """
        self._sandboxes.pop(sb.sandbox_id, None)
        try:
            retired = await self.run(
                sb, lambda sb: self._retire_job(sb, now), activity=False, allow_frozen=True
            )
        except asyncio.CancelledError:
            # The janitor was stopped while this was waiting for the lock. The job is
            # shielded by :meth:`run` and removes the directory anyway, so the sandbox
            # stays out of the registry, which is where a deleted sandbox belongs.
            raise
        except Exception:
            self._sandboxes.setdefault(sb.sandbox_id, sb)
            raise
        if not retired:
            self._sandboxes.setdefault(sb.sandbox_id, sb)

    def _retire_job(self, sb: SandboxRuntime, now: float) -> bool:
        """Remove what ``sb`` is made of and say whether it is gone (worker thread).

        The database, the trace, the Named_Snapshots and the metadata are the sandbox
        directory, so removing it is all of requirement 15.9. Returns False where the
        retention period is not over after all, the condition of the scan read again under
        the lock (:meth:`_retire`), and raises what the removal raised otherwise: unlike
        :meth:`_delete_job`, which answers an Admin DELETE and has nobody to tell, this one
        is retried on the next scan and must not report a sandbox gone that is still there.
        A directory that is already gone counts as removed.
        """
        if sb.frozen_since is None or now - sb.frozen_since <= self._limits.retention_seconds:
            return False
        _close_quietly(sb.store.close, sb.directory)
        _close_quietly(sb.trace.close, sb.directory)
        with suppress(FileNotFoundError):
            shutil.rmtree(sb.directory)
        return True

    # --- shutting the manager down -----------------------------------------------------

    def close(self) -> None:
        """Close every sandbox and the thread pool; the sandbox files stay on disk.

        What the service runs when it stops: the timing loop is cancelled, the databases
        and the trace writers are closed, so no file descriptor is left open, and the pool
        this manager made itself is shut down. The registry is kept, since nothing is
        deleted - a sandbox directory outlives the process (module docstring) - and the
        manager runs no job afterwards. This is not a coroutine, so it cannot wait for a
        scan that is in flight; a service awaits :meth:`stop_janitor` first.
        """
        self._cancel_janitor()
        for sb in self._sandboxes.values():
            _close_quietly(sb.store.close, sb.directory)
            _close_quietly(sb.trace.close, sb.directory)
        if self._owns_executor:
            self._executor.shutdown(wait=True)

    # --- internals ---------------------------------------------------------------------

    def _cancel_janitor(self) -> None:
        """Cancel the timing loop without waiting for it (:meth:`close`)."""
        task, self._janitor = self._janitor, None
        if task is not None and not task.done():
            task.cancel()

    @property
    def _timezone(self) -> str | None:
        """The time zone every instant of a sandbox is read in (requirement 16.1)."""
        return self._loaded.instance.clock.timezone

    async def _in_thread[T](self, job: Callable[[], T]) -> T:
        """Run ``job`` in the shared pool, where every SQLite statement runs."""
        return await asyncio.get_running_loop().run_in_executor(self._executor, job)


def _ref(name: str, version: str) -> str:
    """``<name>@<version>``, how a trace event and the Admin API name a document."""
    return f"{name}@{version}"


def _check_name(name: str) -> None:
    """Check that ``name`` can be a Named_Snapshot of a sandbox (requirement 19.20).

    Raises ValueError where it does not match :data:`NAMED_NAME_PATTERN` or is the
    reserved name of the initial dataset; the Admin API checks the same two things itself
    and answers 400, so this is the guard that keeps a name out of the file system that
    never passed through it.
    """
    if NAMED_NAME_PATTERN.match(name) is None:
        raise ValueError(_BAD_NAME.format(name=name))
    if name == INITIAL_BASE:
        raise ValueError(_RESERVED_NAME)


def _read_calls(meta: Path) -> int:
    """The call count the side file ``meta`` holds, 0 where it cannot be read.

    The number a restore takes up (requirement 23.12). The file is written beside the
    snapshot it belongs to, so the only way it is missing or unreadable is a sandbox
    directory somebody edited; the restore itself goes through and the count starts over,
    which is the safe side of the limit.
    """
    try:
        payload = json.loads(meta.read_bytes())
    except (OSError, ValueError) as error:
        _LOG.warning(_BAD_SIDE_FILE, meta, error)
        return 0
    found = payload.get(CALLS_KEY) if isinstance(payload, dict) else None
    if isinstance(found, int) and not isinstance(found, bool) and found >= 0:
        return found
    _LOG.warning(_BAD_SIDE_FILE, meta, f"{CALLS_KEY} 不是非负整数：{found!r}")
    return 0


def _write_json(path: Path, payload: Mapping[str, JsonValue]) -> None:
    """Write ``payload`` to ``path`` as one line of compact JSON (design.md 7.7)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_utf8(dumps_compact(payload) + "\n"))


def _close_quietly(close: Callable[[], None], directory: Path) -> None:
    """Close something of the sandbox ``directory``, logging a failure instead of raising.

    Closing happens while a sandbox is being frozen or deleted and while a creation that
    failed is being cleaned up, none of which has anything left to do about an error; the
    reason belongs in the service log.
    """
    try:
        close()
    except Exception as error:
        _LOG.warning("关闭沙箱 %s 的资源失败：%s", directory, error)


def _retrieve(task: "asyncio.Future[Any]") -> None:
    """Retrieve the exception of a finished job (:meth:`SandboxManager.run`)."""
    if not task.cancelled():
        task.exception()
