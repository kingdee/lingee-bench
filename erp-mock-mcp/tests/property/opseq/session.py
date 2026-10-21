"""One sandbox of one example, and the calls a sequence made in it (requirement 25.2).

A :class:`Session` is a sandbox copied from the snapshot of a :class:`~tests.property.opseq.start.Start`
plus the client side of it: it performs tools/call through the
:class:`~erp_mock.tools.layer.ToolLayer` of the start, numbers the calls the way the
gateway numbers the events of a session (requirement 18.10) and keeps what each call
produced (:class:`Performed`)::

    with open_sandbox(start, parent, watch=True) as session:
        performed = session.call("supply_order_create", {"vendor_id": ..., "lines": [...]})
        performed.outcome.structured, performed.before, performed.after

What a property test reads off it:

- :attr:`Performed.outcome` - the :class:`~erp_mock.tools.layer.ToolOutcome` of the call:
  ``structured`` and ``spec.output_schema`` for property 20, ``error`` for property 21,
  ``writes`` for property 27;
- :attr:`Performed.before` and :attr:`Performed.after` - every table of the sandbox, the
  internal ones included, as it was right before and right after the call, for properties
  19 and 27; both are None unless the sandbox was opened with ``watch=True``, since
  reading every table around every call costs more than the call itself;
- :meth:`Session.tables`, :meth:`Session.counters` and :meth:`Session.records` - the
  sandbox at any point, for the ids, numbers and counters of property 18;
- :attr:`Session.calls` - the calls of the sequence as they were made, which
  :meth:`Session.replay` runs again in another sandbox: property 18 replays the successful
  calls alone and compares what they got.

Two sandboxes in one state. :func:`forks` writes the state a session is in out to a
snapshot file and copies that file into as many fresh sandboxes as a test asks for, so
property 19 can run one failing call in two sandboxes that are byte for byte the same and
compare the error results (task 16.3).

The resource limits. A sequence of 50 calls, times 500 examples, is far past the 500 calls
a sandbox is allowed (requirement 23.12), so the limits are answered by
:class:`BlankProbe`, which reports no calls, no bytes and no recent calls at all. The
bounds of the layer stay the defaults of a plain process, so the three checks run in the
order and at the moment they do in production (design.md 5.3 step 1) and none of them ever
bites; ``BlankProbe.passed`` counts the calls that got through the check, which is every
call a session made.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.state.schema import COUNTERS_TABLE, ID_COLUMN, StorageSchema
from erp_mock.state.snapshot import BASE_DB_FILENAME, copy_snapshot, link_snapshot
from erp_mock.state.store import DB_FILENAME, Record, SandboxStore, Value, Write
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.layer import CallContext, ToolLayer, ToolOutcome
from tests.property.opseq.start import Start
from tests.property.opseq.state import Refs, read_refs

__all__ = [
    "SESSION_ID",
    "SNAPSHOT_NAME",
    "Arguments",
    "BlankProbe",
    "Call",
    "Contents",
    "Performed",
    "Session",
    "Tables",
    "forks",
    "open_sandbox",
    "public_contents",
    "read_tables",
]

# The session every call of a sequence arrives on; nothing of the pipeline depends on the
# value (:class:`~erp_mock.tools.layer.CallContext`).
SESSION_ID: Final = "se_0123456789abcdef"

# The file :func:`forks` writes the state of a session out to.
SNAPSHOT_NAME: Final = "state.sqlite"

# The arguments of one call: the ``arguments`` of a tools/call request, parsed JSON and
# nothing else, which is what the tools layer takes (requirement 12.1).
type Arguments = Mapping[str, JsonValue]
# Every table of a sandbox with its records, by table name and in primary key order; the
# internal tables are part of it, which is what property 19 compares.
type Tables = dict[str, list[Record]]
# The non-internal fields of every record of the tables the diff covers, by (entity, id),
# which is what property 27 compares a call's ``writes`` against (requirement 17.4).
type Contents = dict[tuple[str, str], FrozenMap[str, Value]]


# --- the limit probe of a sequence (requirement 23.12) -----------------------------------


@dataclass(slots=True)
class BlankProbe:
    """A :class:`~erp_mock.tools.layer.LimitProbe` that is never over any bound.

    The three counts are reported as 0 whatever the sequence did, so no call of a sequence
    is refused with LIMIT_EXCEEDED and the bounds of the layer stay the ones a plain
    process has (module docstring). ``passed`` counts the calls that reached
    ``mark_passed``, which is every call that got through the check.
    """

    passed: int = 0

    def sandbox_calls(self) -> int:
        return 0

    def db_bytes(self) -> int:
        return 0

    def recent_session_calls(self) -> int:
        return 0

    def mark_passed(self) -> None:
        self.passed += 1


# --- one call and what it produced -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Call:
    """One tools/call of a sequence: the tool it names and the arguments it carries.

    ``args`` is a plain dict, the way the ``arguments`` of a request parses, and None for a
    call that carried none. A :class:`Call` is what :meth:`Session.replay` runs again, so a
    test may drop calls from a sequence and run the rest in another sandbox (property 18).
    """

    tool: str
    args: Arguments | None = None


@dataclass(frozen=True, slots=True)
class Performed:
    """One call of a sequence as it ran: what was called, what came back, and the sandbox.

    - ``seq``: the trace sequence number of the call, 1 for the first call of the session
      (requirement 18.10);
    - ``call`` and ``spec``: the call that was made and the tool it named, whose
      ``output_schema`` a successful result validates against (requirement 11.7);
    - ``outcome``: what the tools layer returned, successful or not (requirement 12.1);
    - ``before`` and ``after``: every table of the sandbox right before and right after the
      call, None unless the sandbox watches them (module docstring).
    """

    seq: int
    call: Call
    spec: ToolSpec
    outcome: ToolOutcome
    before: Tables | None = None
    after: Tables | None = None

    @property
    def tool(self) -> str:
        """The name of the tool the call named (requirement 11.9)."""
        return self.call.tool

    @property
    def action(self) -> str:
        """The operation the tool performs: ``create``, ``submit``, ``create_from``, ..."""
        return self.spec.action

    @property
    def failed(self) -> bool:
        """Whether the call ended with an error code (requirement 13.6)."""
        return self.outcome.is_error

    @property
    def error_code(self) -> str | None:
        """The error code of a failed call, None for a successful one (req 17.3)."""
        return self.outcome.error_code

    @property
    def rule_id(self) -> str | None:
        """The rule that decided the error, None where none did (requirement 13.8)."""
        return self.outcome.rule_id

    @property
    def writes(self) -> tuple[Write, ...]:
        """The net change the call made to each (record, cause) (requirement 17.4)."""
        return self.outcome.writes


# --- the sandbox of one example ----------------------------------------------------------


@dataclass(slots=True)
class Session:
    """A sandbox of one example and the calls made in it (module docstring).

    Built by :func:`open_sandbox`, which copies the sandbox from the snapshot of ``start``
    and closes it again. ``watch`` is whether every call records the tables around it, and
    ``probe`` the limit probe of every call (:class:`BlankProbe`).
    """

    start: Start
    store: SandboxStore
    watch: bool = False
    probe: BlankProbe = field(default_factory=BlankProbe)
    performed: list[Performed] = field(default_factory=list)
    seq: int = 0
    ref_pool: Refs | None = None

    @property
    def layer(self) -> ToolLayer:
        """The tools layer every call goes through (design.md 5.3)."""
        return self.start.layer

    @property
    def schema(self) -> StorageSchema:
        """The tables of the sandbox (design.md 3.1)."""
        return self.store.schema

    @property
    def calls(self) -> tuple[Call, ...]:
        """The calls of the sequence, in the order they were made."""
        return tuple(performed.call for performed in self.performed)

    def context(self) -> CallContext:
        """The context of the next call: the business date, the user and a fresh seq.

        Every call of the session gets the next trace sequence number, reads and failed
        calls included, which is what the gateway does with the events of a session
        (requirement 18.10).
        """
        self.seq += 1
        return CallContext(
            session_id=SESSION_ID,
            anchor_date=self.start.anchor_date,
            current_user_id=self.start.current_user_id,
            call_seq=self.seq,
            limits=self.probe,
        )

    def perform(self, call: Call) -> Performed:
        """Perform ``call`` against the sandbox and record what it produced.

        The tables around the call are read when the session watches them, so
        ``before`` and ``after`` hold the state of every table, the internal ones included
        (properties 19 and 27). The outcome is returned whatever it is: the tools layer
        raises nothing, so a sequence goes on after a call that failed
        (requirement 12.8).
        """
        spec = self.start.spec(call.tool)
        before = read_tables(self.store) if self.watch else None
        outcome = self.layer.call(self.store, self.context(), spec, call.args)
        after = read_tables(self.store) if self.watch else None
        performed = Performed(
            seq=self.seq, call=call, spec=spec, outcome=outcome, before=before, after=after
        )
        self.performed.append(performed)
        if _wrote_an_entity(self.start, outcome):
            # The ids a reference may name have changed, so the pool is read again.
            self.ref_pool = None
        return performed

    def call(self, tool: str, args: Arguments | None = None) -> Performed:
        """Perform one tools/call of ``tool`` with the arguments ``args``."""
        return self.perform(Call(tool, args))

    def replay(self, calls: Sequence[Call]) -> tuple[Performed, ...]:
        """Perform ``calls`` in order and return what each of them produced.

        What property 18 runs in a second sandbox: the calls of a sequence with the failed
        ones left out get the same ids, numbers and operation sequence numbers as they did
        in the first.
        """
        return tuple(self.perform(call) for call in calls)

    def refs(self) -> Refs:
        """The records a reference may name, read once and kept (:class:`~tests.property.opseq.state.Refs`).

        The pool is read on the first call and again after a call that wrote an entity
        record, so a generated reference names a record that is there
        (:func:`~tests.property.opseq.state.read_refs`).
        """
        if self.ref_pool is None:
            self.ref_pool = read_refs(self.store, self.start.domain)
        return self.ref_pool

    def tables(self) -> Tables:
        """Every table of the sandbox with its records, the internal ones included."""
        return read_tables(self.store)

    def contents(self) -> Contents:
        """The non-internal fields of every record the diff covers, by (entity, id)."""
        return public_contents(self.tables(), self.schema)

    def records(self, table: str) -> list[Record]:
        """Every record of ``table``, in primary key order; raises KeyError when unknown."""
        with self.store.transaction() as uow:
            return uow.select(table)

    def record(self, table: str, record_id: str) -> Record | None:
        """The record ``record_id`` of ``table``, None when the sandbox holds none."""
        with self.store.transaction() as uow:
            return uow.get(table, record_id)

    def counters(self) -> dict[tuple[str, str], int]:
        """The counters of the sandbox, keyed by (kind, key) (requirement 4.6)."""
        rows = self.records(COUNTERS_TABLE)
        return {(str(row["kind"]), str(row["key"])): int(str(row["value"])) for row in rows}

    def save_state(self, dest: Path) -> None:
        """Write the state the sandbox is in now out to the new snapshot file ``dest``.

        The backup API copies every page, so the records, the counters and the effective
        calls are all part of it and a sandbox copied from the file starts in exactly this
        state (:meth:`~erp_mock.state.store.SandboxStore.save_to`). Used by :func:`forks`.
        """
        self.store.save_to(dest)


@contextmanager
def open_sandbox(
    start: Start, parent: Path, *, snapshot: Path | None = None, watch: bool = False
) -> Iterator[Session]:
    """Yield a sandbox copied from the snapshot of ``start``, closed again on the way out.

    What requirement 15.1 gives a new sandbox: ``db.sqlite`` copied from the Snapshot with
    the backup API and ``base.sqlite`` linked beside it, in a directory of its own below
    ``parent`` that is removed when the block leaves - a run of 500 examples keeps one
    sandbox at a time. ``snapshot`` copies from another file of the same instance instead,
    which is how :func:`forks` opens two sandboxes in one state; ``watch`` makes every call
    record the tables around it (module docstring).
    """
    with TemporaryDirectory(dir=parent, prefix="sb ") as made:
        sandbox_dir = Path(made)
        source = snapshot if snapshot is not None else start.snapshot
        copy_snapshot(source, sandbox_dir / DB_FILENAME)
        link_snapshot(source, sandbox_dir / BASE_DB_FILENAME)
        store = SandboxStore(sandbox_dir, start.schema)
        store.open()
        try:
            yield Session(start=start, store=store, watch=watch)
        finally:
            store.close()


@contextmanager
def forks(
    session: Session, parent: Path, *, count: int = 2, watch: bool = False
) -> Iterator[tuple[Session, ...]]:
    """Yield ``count`` fresh sandboxes, each in the state ``session`` is in right now.

    The state is written out to a snapshot file of its own and every sandbox is copied from
    that one file, so the databases start out identical down to the counters and the
    effective calls. Property 19 runs one failing call in two of them and compares the
    error results byte for byte (task 16.3). The session itself is left as it is.
    """
    with TemporaryDirectory(dir=parent, prefix="fork ") as made:
        state = Path(made) / SNAPSHOT_NAME
        session.save_state(state)
        with ExitStack() as stack:
            yield tuple(
                stack.enter_context(
                    open_sandbox(session.start, Path(made), snapshot=state, watch=watch)
                )
                for _ in range(count)
            )


# --- reading a sandbox -------------------------------------------------------------------


def read_tables(store: SandboxStore) -> Tables:
    """Every table of ``store`` with its records, the internal ones included (req 12.7).

    One transaction, so the tables are read as one state of the sandbox; the records come
    back as typed values in primary key order (:meth:`~erp_mock.state.store.UnitOfWork.select`).
    What property 19 compares around a failed call, counters and effective calls included.
    """
    with store.transaction() as uow:
        return {name: uow.select(name) for name in store.schema.tables}


def public_contents(tables: Tables, schema: StorageSchema) -> Contents:
    """The non-internal fields of every record of the diffable tables, by (entity, id).

    The ``entity`` of a :class:`~erp_mock.state.store.Write` is the name of the table and
    its ``id`` the record's, so a key of this map is exactly the (entity, id) of
    requirement 17.4; the internal columns are dropped, since a change of one of them is no
    change of the record (requirement 18.4). What property 27 compares the ``writes`` of a
    call against.
    """
    contents: Contents = {}
    for table in schema.diffable_tables:
        names = [column.name for column in table.public_columns if column.name != ID_COLUMN]
        for record in tables.get(table.name, []):
            contents[(table.name, str(record[ID_COLUMN]))] = FrozenMap(
                (name, record[name]) for name in names
            )
    return contents


def _wrote_an_entity(start: Start, outcome: ToolOutcome) -> bool:
    """Whether the call wrote a record of an entity, whose ids a reference may name.

    The sequences write bills, so the pool of referable ids is read once per session and
    kept; a profile that exposes the create of an entity makes the pool stale, and this
    tells the session to read it again (:attr:`Session.references`).
    """
    return any(write.entity in start.domain.entities for write in outcome.writes)
