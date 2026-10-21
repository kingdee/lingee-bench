"""Property 29: cause_seqs is the symmetric difference of the effective calls.

**Validates: Requirements 18.3, 18.10**

For any call sequence with Named_Snapshots, restores and resets mixed into it, the
``cause_seqs`` of every change of the diff - against ``initial`` or against any of those
Named_Snapshots - is the ascending list of the seqs of the effective calls that belong to
only one of the two sides and whose ``writes`` hold that record (requirement 18.3).

The reference model is the effective call set of requirement 18.10, kept beside the
sandbox: empty when the sandbox is created, a call added when its ``writes`` are not
empty, emptied by a reset, and taken from the Named_Snapshot by a restore, each snapshot
holding the set the sandbox had when it was saved. What each call wrote comes from
:meth:`~erp_mock.state.store.UnitOfWork.net_writes`, the ``writes`` of requirement 17.4,
which property 27 pins; this property takes it as given and only asks which of those
calls the diff names. The expected value of one record is then read straight off the
model: the seqs of the symmetric difference of the two sets that wrote the record, sorted.

The sequences are drawn from four operations - a call, a Named_Snapshot, a restore of one
that is there and a reset - and every operation consumes a seq, because the snapshot and
reset events take one as well (requirement 17.2), so the seqs of the calls have gaps. A
call writes fields of a few records of two entity tables and deletes a few, drawn from a
pool of three ids the two tables share and of four values, two of which the basis holds
as well: a small pool is what makes the interesting cases frequent - a record written by
calls on both sides of a basis, a call that writes a value back to what it was and is
therefore no effective call at all, and a record whose id is in both tables, which the
model keys by (entity, id). The first operation is a call that writes, so no sequence is
one that leaves the sandbox untouched. Sequences stay short and the basis Snapshot is
built once for the module: every example copies it, runs its operations on real SQLite
files and computes one diff per basis, which is what keeps a hundred examples quick.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from typing import Final, assert_never

import pytest
from hypothesis import given, note, settings
from hypothesis import strategies as st

from erp_mock.state.diff import INITIAL_BASE, compute_diff
from erp_mock.state.schema import StorageSchema, build_schema
from erp_mock.state.snapshot import (
    BASE_DB_FILENAME,
    build_snapshot_db,
    copy_snapshot,
    finalize_snapshot,
    link_snapshot,
)
from erp_mock.state.store import DB_FILENAME, SandboxStore, UnitOfWork, Value
from tests.helpers.fixtures import fixture_domain

# --- what the sequences write to -----------------------------------------------------------

# Two entity tables of the engine test fixture, both with a ``code`` and a ``name`` field.
VENDOR: Final = "vendor"
DEPOT: Final = "depot"
TABLES: Final = (VENDOR, DEPOT)

# The records a call may write. An id is any text in storage, and the two tables share
# this pool on purpose: a record of the same id in both tables shows that an effective
# call is attributed per (entity, id) rather than per id (requirements 18.3 and 18.4).
RECORD_IDS: Final = ("a1", "a2", "a3")
FIELDS: Final = ("code", "name")
# The values written: the two the basis holds, so writing a value back to what the basis
# has - a call with no net write - comes up often (requirement 17.4), one it does not hold
# and the absent one.
VALUES: Final[tuple[str | None, ...]] = ("甲", "乙", "丙", None)

# What the basis holds: two records of each table, one id in both of them and one id in
# neither, so a call can insert, update and delete. An initial dataset has no effective
# call (requirement 18.10), so the build below records none.
BASE_CONTENT: Final[tuple[tuple[str, Mapping[str, Value]], ...]] = (
    (VENDOR, {"id": "a1", "code": "甲", "name": "乙"}),
    (VENDOR, {"id": "a2", "code": "乙"}),
    (DEPOT, {"id": "a1", "code": "甲"}),
    (DEPOT, {"id": "a3", "name": "甲"}),
)

# The length of a sequence and how many Named_Snapshots it may create.
MAX_OPS: Final = 10
MAX_SNAPSHOTS: Final = 3
# How many records one call writes.
MAX_RECORD_OPS: Final = 3


# --- the operations of a sequence ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetValue:
    """Write ``value`` to the field of a record, inserting the record when it is not there."""

    table: str
    id: str
    field: str
    value: str | None


@dataclass(frozen=True, slots=True)
class DeleteRecord:
    """Delete a record; an id that is not there changes nothing."""

    table: str
    id: str


type RecordOp = SetValue | DeleteRecord


@dataclass(frozen=True, slots=True)
class Call:
    """One tools/call: its record writes run in one transaction (requirement 12.7)."""

    writes: tuple[RecordOp, ...]


@dataclass(frozen=True, slots=True)
class SaveSnapshot:
    """Save the sandbox as the Named_Snapshot ``name`` (requirement 19.5)."""

    name: str


@dataclass(frozen=True, slots=True)
class RestoreSnapshot:
    """Restore the Named_Snapshot ``name`` (requirement 19.6)."""

    name: str


@dataclass(frozen=True, slots=True)
class ResetToBasis:
    """Reset the sandbox to the Snapshot it was created from (requirement 19.4)."""


type Op = Call | SaveSnapshot | RestoreSnapshot | ResetToBasis

RECORD_OPS: Final[st.SearchStrategy[RecordOp]] = st.one_of(
    st.builds(
        SetValue,
        st.sampled_from(TABLES),
        st.sampled_from(RECORD_IDS),
        st.sampled_from(FIELDS),
        st.sampled_from(VALUES),
    ),
    st.builds(DeleteRecord, st.sampled_from(TABLES), st.sampled_from(RECORD_IDS)),
)


def calls(min_writes: int = 0) -> st.SearchStrategy[Call]:
    """Calls writing ``min_writes`` to :data:`MAX_RECORD_OPS` records.

    A call with no record op at all is one that only read, which is no effective call
    either (requirement 18.10).
    """
    writes = st.lists(RECORD_OPS, min_size=min_writes, max_size=MAX_RECORD_OPS)
    return st.builds(Call, writes.map(tuple))


@st.composite
def op_sequences(draw: st.DrawFn) -> tuple[Op, ...]:
    """Draw a call sequence with Named_Snapshots, restores and resets mixed into it.

    A restore names a Named_Snapshot the sequence created before it, and no two of them
    have the same name, which is what the Admin API accepts (requirements 19.5 and 19.6).
    Calls are drawn more often than the other three operations, because they are what
    makes a change and a cause for it; the first operation is one that writes.
    """
    ops: list[Op] = [draw(calls(min_writes=1))]
    saved: list[str] = []
    for _ in range(draw(st.integers(min_value=0, max_value=MAX_OPS - 1))):
        kinds = ["call", "call", "reset"]
        if len(saved) < MAX_SNAPSHOTS:
            kinds.append("save")
        if saved:
            kinds.append("restore")
        match draw(st.sampled_from(kinds)):
            case "save":
                name = f"s{len(saved) + 1}"
                saved.append(name)
                ops.append(SaveSnapshot(name))
            case "restore":
                ops.append(RestoreSnapshot(draw(st.sampled_from(saved))))
            case "reset":
                ops.append(ResetToBasis())
            case _:
                ops.append(draw(calls()))
    return tuple(ops)


# --- the reference model -------------------------------------------------------------------

# One record of the sandbox: the entity of its table and its id (requirement 18.4).
type RecordKey = tuple[str, str]


@dataclass(slots=True)
class Reference:
    """The effective calls of the sandbox and of its Named_Snapshots (requirement 18.10)."""

    # The effective calls of the current state, empty when the sandbox is created.
    effective: frozenset[int] = frozenset()
    # The effective calls each Named_Snapshot holds: the set at the moment it was saved.
    snapshots: dict[str, frozenset[int]] = field(default_factory=dict)
    # What each effective call wrote, the ``writes`` of requirement 17.4 per seq.
    writes: dict[int, frozenset[RecordKey]] = field(default_factory=dict)

    def called(self, seq: int, wrote: frozenset[RecordKey]) -> None:
        """Record the call ``seq``; it is effective when its writes are not empty."""
        if wrote:
            self.writes[seq] = wrote
            self.effective |= {seq}

    def expected_cause_seqs(self, base: frozenset[int]) -> dict[RecordKey, tuple[int, ...]]:
        """The cause_seqs of every record against the basis ``base`` (requirement 18.3).

        The calls that belong to only one of the two sides are the symmetric difference of
        the two sets; a record's causes are the ones of them that wrote it, each once and
        in ascending order.
        """
        found: dict[RecordKey, list[int]] = {}
        for seq in sorted(self.effective ^ base):
            for record in self.writes[seq]:
                found.setdefault(record, []).append(seq)
        return {record: tuple(seqs) for record, seqs in found.items()}


# --- running a sequence --------------------------------------------------------------------


def run_call(store: SandboxStore, seq: int, writes: Sequence[RecordOp]) -> frozenset[RecordKey]:
    """Run ``writes`` as the call ``seq`` and return the records it wrote (req 17.4, 18.10)."""
    with store.transaction() as uow:
        for write in writes:
            apply_write(uow, write)
        net_writes = uow.net_writes()
        uow.record_effective_call(seq, net_writes)
    return frozenset((write.entity, write.id) for write in net_writes)


def apply_write(uow: UnitOfWork, write: RecordOp) -> None:
    """Apply one record op, inserting a record a :class:`SetValue` does not find."""
    match write:
        case SetValue(table=table, id=record_id, field=name, value=value):
            if uow.get(table, record_id) is None:
                uow.insert(table, {"id": record_id, name: value})
            else:
                uow.update(table, record_id, {name: value})
        case DeleteRecord(table=table, id=record_id):
            uow.delete(table, record_id)
        case _:
            assert_never(write)


def run_sequence(
    ops: Sequence[Op], store: SandboxStore, named_dir: Path, reference: Reference
) -> None:
    """Run ``ops`` on ``store``, keeping ``reference`` beside it (requirement 18.10).

    Every operation takes one seq, the snapshot and reset events included, so the seqs of
    the calls are not contiguous (requirement 17.2).
    """
    for seq, op in enumerate(ops, start=1):
        match op:
            case Call(writes=writes):
                reference.called(seq, run_call(store, seq, writes))
            case SaveSnapshot(name=name):
                store.save_to(named_dir / f"{name}.sqlite")
                reference.snapshots[name] = reference.effective
            case RestoreSnapshot(name=name):
                store.restore_from(named_dir / f"{name}.sqlite")
                reference.effective = reference.snapshots[name]
            case ResetToBasis():
                store.restore_from(store.sandbox_dir / BASE_DB_FILENAME)
                reference.effective = frozenset()
            case _:
                assert_never(op)


# --- fixtures ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema() -> StorageSchema:
    return build_schema(fixture_domain())


@pytest.fixture(scope="module")
def basis(tmp_path_factory: pytest.TempPathFactory, schema: StorageSchema) -> Path:
    """The Snapshot every example starts from: the basis of an ``initial`` diff."""
    root = tmp_path_factory.mktemp("p29-basis")
    build_db = root / "build" / "build.sqlite"
    store = build_snapshot_db(build_db, schema)
    try:
        with store.transaction() as uow:
            for table, record in BASE_CONTENT:
                uow.insert(table, record)
    finally:
        store.close()
    out = root / "snapshot.sqlite"
    finalize_snapshot(build_db, out)
    return out


@pytest.fixture(scope="module")
def sandbox_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandbox of each example is created below."""
    return tmp_path_factory.mktemp("p29-sandboxes")


# --- the property --------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 29: cause_seqs 是生效调用的对称差
@pytest.mark.req("18.3", "18.10")
@settings(max_examples=100)
@given(ops=op_sequences())
def test_cause_seqs_are_the_symmetric_difference_of_the_effective_calls(
    schema: StorageSchema, basis: Path, sandbox_root: Path, ops: tuple[Op, ...]
) -> None:
    note(f"operations: {ops}")
    # A space in the directory name: both databases are read through a URI (design.md 3.4).
    sandbox_dir = Path(mkdtemp(dir=sandbox_root, prefix="sb "))
    copy_snapshot(basis, sandbox_dir / DB_FILENAME)
    link_snapshot(basis, sandbox_dir / BASE_DB_FILENAME)
    store = SandboxStore(sandbox_dir, schema)
    store.open()
    reference = Reference()
    try:
        run_sequence(ops, store, sandbox_dir / "named", reference)
        note(f"effective calls: {sorted(reference.effective)}, writes: {reference.writes}")

        bases: list[tuple[Path, str, frozenset[int]]] = [
            (sandbox_dir / BASE_DB_FILENAME, INITIAL_BASE, frozenset())
        ]
        bases += [
            (sandbox_dir / "named" / f"{name}.sqlite", name, effective)
            for name, effective in reference.snapshots.items()
        ]
        for path, label, base_effective in bases:
            body = compute_diff(store.db_path, path, schema, label)
            expected = reference.expected_cause_seqs(base_effective)
            note(f"base {label}: {[(c.entity, c.id, c.cause_seqs) for c in body.changes]}")
            for change in body.changes:
                record = (change.entity, change.id)
                assert change.cause_seqs == expected.get(record, ())
                # Ascending, each seq once (requirement 18.3).
                assert list(change.cause_seqs) == sorted(set(change.cause_seqs))
    finally:
        store.close()
        rmtree(sandbox_dir, ignore_errors=True)
