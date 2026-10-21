"""The net change of a sandbox against a basis (design.md 3.4).

:func:`compute_diff` compares the working database of one sandbox with the Snapshot it was
created from or with one of its Named_Snapshots and reports what the calls in between
changed, per (entity, id): a record only the sandbox has is an insert, one only the basis
has a delete, and a record both sides hold whose non-internal fields differ an update of
exactly those fields (requirement 18.2). Intermediate states do not survive the
comparison - a record created and deleted again, or changed and changed back, is not a
change at all - and neither do the internal columns and the engine's internal tables
(requirement 18.4)::

    body = compute_diff(sandbox.db_path, sandbox_dir / BASE_DB_FILENAME, schema)
    dumps_pretty(body.as_json(sandbox_id=..., instance=..., dataset=..., anchor_date=...))

Reading. The current state is read through a plain connection of its own with
``PRAGMA query_only=1``, the basis is attached read-only and immutable, and every
statement runs in one plain BEGIN (V8, tests/probes/test_sqlite.py):

- a plain connection rather than ``mode=ro``, because an idle sandbox has no ``-shm``
  file left and a read-only connection cannot create one;
- ``query_only`` instead of a transaction of the store, so the diff takes no write lock
  and waits for none: in WAL mode the read transaction sees exactly the state committed
  when it started, which is every call that finished and no part of one in flight
  (requirement 19.8). ``BEGIN IMMEDIATE`` would fail on such a connection, so the reader
  uses plain BEGIN;
- the basis is only read, through :func:`~erp_mock.state.store.immutable_uri`, so its
  bytes stay as they are and no sidecar file appears next to it (requirement 15.13).

Comparing. Both databases were created from the same instance configuration, so they have
the same tables and columns, and every value is stored in its canonical form: equal
decimals have equal text (design.md 3.1). Stored values are therefore compared directly,
without converting them to typed values first, and the comparison is the comparison of
the formatted values requirement 18.2 asks for. Output values are the stored ones as well,
a 0 or 1 of a bool column turned into a JSON boolean (requirement 18.5).

Determinism. The tables are compared in the order of the schema, which is the code point
order of their names, and the changed ids of one table come back ordered by ``id``, whose
BINARY collation on UTF-8 is that same code point order; ``changes`` is therefore ordered
by entity and then by id (requirement 18.6) without a sort of its own. The key order of
the output is design.md data model 5: :meth:`Change.as_json` and :meth:`DiffBody.as_json`
build each object with its keys in that order, and the fields inside ``before`` and
``after`` by field name. Two sandboxes that ran the same requests therefore produce
diffs that are identical byte for byte (requirement 24.2).
"""

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, Literal

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.state.schema import (
    EFFECTIVE_WRITES_TABLE,
    ID_COLUMN,
    ColumnSchema,
    StorageSchema,
    TableSchema,
)
from erp_mock.state.store import StoredValue, immutable_uri

__all__ = [
    "INITIAL_BASE",
    "Change",
    "DiffBody",
    "DiffOp",
    "Fields",
    "compute_diff",
]

# The ``base`` of a diff against the initial dataset, and the value the Admin API's base
# parameter defaults to (requirements 18.1 and 19.8); every other basis is a
# Named_Snapshot and carries its own name.
INITIAL_BASE: Final = "initial"

# The names the two databases are read under: the connection's own database is the current
# state, the basis is attached beside it.
_BASE_DATABASE: Final = "base"
_MAIN_DATABASE: Final = "main"

# The net change of one record (requirement 18.2).
type DiffOp = Literal["insert", "update", "delete"]

# The values of one record as the diff reports them, by field name (requirement 18.5).
type Fields = FrozenMap[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Change:
    """One net change of one record, an item of a diff's ``changes`` (requirement 18.2).

    ``entity`` is the name of the record's table - an entity name, a bill name,
    ``<bill>_line``, stock_txn or stock_balance - and ``id`` the record's id
    (requirement 18.4). ``cause_seqs`` holds the trace seq of every effective call that
    wrote the record and belongs to only one of the two sides, in ascending order
    (requirement 18.3).

    An insert has ``after`` alone and a delete ``before`` alone, each holding every
    non-internal field but ``id``; an update has both, holding the fields whose values
    differ (requirement 18.9). The fields are keyed by storage name, a ref field keeping
    its ``_id`` suffix, and ordered by that name (requirements 18.5 and 18.6).
    """

    entity: str
    op: DiffOp
    id: str
    cause_seqs: tuple[int, ...] = ()
    before: Fields | None = None
    after: Fields | None = None

    @property
    def sort_key(self) -> tuple[str, str]:
        """The order of requirement 18.6: entity, then id, by code point."""
        return (self.entity, self.id)

    def as_json(self) -> dict[str, JsonValue]:
        """The change as a JSON object, keys in the order of design.md data model 5.

        ``entity``, ``op``, ``id``, ``cause_seqs``, then ``before`` and ``after`` as the
        op has them (requirements 18.6 and 18.9).
        """
        item: dict[str, JsonValue] = {
            "entity": self.entity,
            "op": self.op,
            "id": self.id,
            "cause_seqs": self.cause_seqs,
        }
        if self.before is not None:
            item["before"] = self.before
        if self.after is not None:
            item["after"] = self.after
        return item


@dataclass(frozen=True, slots=True)
class DiffBody:
    """The result of one comparison: the basis it ran against and the changes it found.

    ``base`` is :data:`INITIAL_BASE` or the name of the Named_Snapshot the sandbox was
    compared with, and ``changes`` is ordered by entity and id, empty when the sandbox
    holds what the basis holds (requirements 18.1, 18.6 and 18.7). The four values that
    say which sandbox this is belong to the sandbox rather than to its databases, so
    :meth:`as_json` takes them.
    """

    base: str
    changes: tuple[Change, ...] = ()

    def as_json(
        self, *, sandbox_id: str, instance: str, dataset: str, anchor_date: date
    ) -> dict[str, JsonValue]:
        """The diff as a JSON object, keys in the order of design.md data model 5.

        ``sandbox_id``, ``instance``, ``dataset``, ``anchor_date``, ``base``, ``changes``
        (requirement 18.1); ``instance`` and ``dataset`` are written ``<name>@<version>``
        by the caller and the anchor date as ``YYYY-MM-DD`` here. ``changes`` is an empty
        array when there is no net change, the other keys unchanged (requirement 18.7).
        """
        return {
            "sandbox_id": sandbox_id,
            "instance": instance,
            "dataset": dataset,
            "anchor_date": anchor_date.isoformat(),
            "base": self.base,
            "changes": [change.as_json() for change in self.changes],
        }


def compute_diff(
    current: Path, base: Path, schema: StorageSchema, base_label: str = INITIAL_BASE
) -> DiffBody:
    """Return the net change of the database ``current`` against ``base`` (requirement 18.1).

    ``current`` is the working database of a sandbox, open or idle, and ``base`` the
    Snapshot it was created from (its ``base.sqlite``) or one of its Named_Snapshots;
    ``base_label`` is what the output names it: :data:`INITIAL_BASE` or the name of that
    Named_Snapshot. Both are read and neither is changed (see the module docstring).

    Raises FileNotFoundError when either file is not there, and
    ``sqlite3.OperationalError`` when the basis holds other tables than ``schema``, which
    means it was built from another instance configuration.
    """
    if not current.is_file():
        raise FileNotFoundError(f"no such sandbox database: {current}")
    if not base.is_file():
        raise FileNotFoundError(f"no such snapshot: {base}")
    with _reader(current, base) as connection:
        causes = _cause_seqs(connection)
        changes = tuple(
            change
            for table in schema.diffable_tables
            for change in _table_changes(connection, table, causes)
        )
    return DiffBody(base=base_label, changes=changes)


@contextmanager
def _reader(current: Path, base: Path) -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection to ``current`` with ``base`` attached, in one read transaction.

    The pragma, the ATTACH and the BEGIN of design.md 3.4, in that order: ATTACH cannot
    run inside a transaction, and the transaction holds one read snapshot of the current
    state for every statement of the block. Leaving the block commits; an error closes
    the connection, which ends the read transaction as well.
    """
    # as_uri() percent-encodes what a URI cannot carry, so a data directory whose name
    # holds a space or a '#' works; a path is never taken for a URI of its own.
    uri = current.resolve().as_uri()
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
        connection.execute("PRAGMA query_only=1")
        connection.execute(f"ATTACH ? AS {_BASE_DATABASE}", (immutable_uri(base),))
        connection.execute("BEGIN")
        yield connection
        connection.execute("COMMIT")


def _table_changes(
    connection: sqlite3.Connection,
    table: TableSchema,
    causes: Mapping[tuple[str, str], tuple[int, ...]],
) -> Iterator[Change]:
    """The changes of one table, ordered by id (requirements 18.2 and 18.6)."""
    compared = tuple(column for column in table.public_columns if column.name != ID_COLUMN)
    for record_id in _changed_ids(connection, table):
        after = _row(connection, _MAIN_DATABASE, table, record_id)
        before = _row(connection, _BASE_DATABASE, table, record_id)
        cause_seqs = causes.get((table.name, record_id), ())
        change = _change(table.name, record_id, compared, before, after, cause_seqs)
        if change is not None:
            yield change


def _change(
    entity: str,
    record_id: str,
    compared: Sequence[ColumnSchema],
    before: Mapping[str, StoredValue] | None,
    after: Mapping[str, StoredValue] | None,
    cause_seqs: tuple[int, ...],
) -> Change | None:
    """The net change of one record, None when there is none (requirements 18.2 and 18.9)."""
    if after is None:
        if before is None:
            # Not reached: an id comes from one of the two sides.
            return None
        return Change(
            entity=entity,
            op="delete",
            id=record_id,
            cause_seqs=cause_seqs,
            before=_fields(compared, before),
        )
    if before is None:
        return Change(
            entity=entity,
            op="insert",
            id=record_id,
            cause_seqs=cause_seqs,
            after=_fields(compared, after),
        )
    differing = tuple(column for column in compared if before[column.name] != after[column.name])
    if not differing:
        # Only internal columns differ, so the record has no net change (requirement 18.4).
        return None
    return Change(
        entity=entity,
        op="update",
        id=record_id,
        cause_seqs=cause_seqs,
        before=_fields(differing, before),
        after=_fields(differing, after),
    )


def _fields(columns: Sequence[ColumnSchema], row: Mapping[str, StoredValue]) -> Fields:
    """The values of ``columns`` in ``row``, by field name in code point order (req 18.6)."""
    return FrozenMap(
        (column.name, _json_value(column, row[column.name]))
        for column in sorted(columns, key=lambda column: column.name)
    )


def _json_value(column: ColumnSchema, stored: StoredValue) -> JsonValue:
    """The JSON value of ``stored``, as its field type is written (requirement 18.5).

    A decimal, a date, a period, an enum code and a reference are stored as the text the
    diff reports, so they pass through; an int column holds a JSON integer already and an
    absent value is null. Only a bool needs its 0 or 1 turned into a JSON boolean.
    """
    if stored is None:
        return None
    if column.type == "bool":
        return bool(stored)
    return stored


def _changed_ids(connection: sqlite3.Connection, table: TableSchema) -> list[str]:
    """The ids of ``table`` whose non-internal columns differ between the two sides.

    One compound statement per table: the rows of either side that the other side does not
    hold, projected on the non-internal columns, give the ids that were inserted, deleted
    or changed, each once. Ordered by ``id``, whose BINARY collation on UTF-8 is the code
    point order of requirement 18.6.
    """
    projection = ", ".join(_quoted(column.name) for column in table.public_columns)
    main = f"SELECT {projection} FROM {_MAIN_DATABASE}.{_quoted(table.name)}"
    base = f"SELECT {projection} FROM {_BASE_DATABASE}.{_quoted(table.name)}"
    key = _quoted(ID_COLUMN)
    statement = (
        f"SELECT {key} FROM ({main} EXCEPT {base})"
        f" UNION SELECT {key} FROM ({base} EXCEPT {main})"
        f" ORDER BY {key}"
    )
    return [str(record_id) for (record_id,) in connection.execute(statement)]


def _row(
    connection: sqlite3.Connection, database: str, table: TableSchema, record_id: str
) -> dict[str, StoredValue] | None:
    """The non-internal columns of the record ``record_id``, or None when it is not there."""
    names = [column.name for column in table.public_columns]
    projection = ", ".join(_quoted(name) for name in names)
    statement = (
        f"SELECT {projection} FROM {database}.{_quoted(table.name)} WHERE {_quoted(ID_COLUMN)} = ?"
    )
    row = connection.execute(statement, (record_id,)).fetchone()
    return None if row is None else dict(zip(names, row, strict=True))


def _cause_seqs(connection: sqlite3.Connection) -> dict[tuple[str, str], tuple[int, ...]]:
    """The cause_seqs of every record, keyed by (entity, id) (requirement 18.3).

    An effective call counts when it belongs to only one of the two sides: a call both
    sides remember ran before the basis was taken, so what it wrote is part of the basis
    (requirement 18.10). The seqs of the symmetric difference that wrote a record are its
    cause_seqs, each once and in ascending order.
    """
    current = _effective_writes(connection, _MAIN_DATABASE)
    basis = _effective_writes(connection, _BASE_DATABASE)
    only = current.keys() ^ basis.keys()
    found: dict[tuple[str, str], set[int]] = {}
    for calls in (current, basis):
        for seq in only & calls.keys():
            for record in calls[seq]:
                found.setdefault(record, set()).add(seq)
    return {record: tuple(sorted(seqs)) for record, seqs in found.items()}


def _effective_writes(
    connection: sqlite3.Connection, database: str
) -> dict[int, list[tuple[str, str]]]:
    """The records each effective call of ``database`` wrote, by seq (requirement 18.10)."""
    statement = (
        f"SELECT {_quoted('seq')}, {_quoted('entity')}, {_quoted(ID_COLUMN)}"
        f" FROM {database}.{_quoted(EFFECTIVE_WRITES_TABLE)}"
    )
    calls: dict[int, list[tuple[str, str]]] = {}
    for seq, entity, record_id in connection.execute(statement):
        calls.setdefault(int(seq), []).append((str(entity), str(record_id)))
    return calls


def _quoted(identifier: str) -> str:
    """``identifier`` as a quoted SQL name, so a table or column may be any word."""
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'
