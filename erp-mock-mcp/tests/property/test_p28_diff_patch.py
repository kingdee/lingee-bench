"""Property 28: the diff is the net patch from the basis to the current state.

**Validates: Requirements 18.2, 18.5, 18.6, 18.9**

For any sequence of record-level writes that starts at the basis, the ``changes`` of the
diff are exactly the patch that turns the basis into the current state: applying them to
the basis - an insert adds its ``after``, a delete removes the record, an update writes its
``after`` over the fields it names - gives the content of every table the diff covers, and
nothing more is reported than the records whose non-internal fields differ
(requirement 18.2). On top of that patch, each change is pinned down:

- ``changes`` is ordered by entity and then by id, and the fields of ``before`` and
  ``after`` by field name (requirement 18.6);
- an insert has ``after`` alone and a delete ``before`` alone, each holding every
  non-internal field but ``id``; an update has both, with the same keys, holding exactly
  the fields whose values differ (requirement 18.9);
- every value is written as its field type has it, a decimal as its canonical text, a bool
  as a JSON boolean, an absent value as null (requirement 18.5).

Each example runs on real files: a sandbox created from the Snapshot of
:data:`~tests.property.models.state_ops.BASE_CONTENT`, which is built once for the module,
with its ``base.sqlite`` linked beside it, so the diff reads what it reads in production.
The plan writes to five tables of the fixture with a handful of ids and small value pools
(tests.property.models.state_ops), which keeps an example cheap and makes the cases
requirement 18.2 reports as no change - a record created and deleted again, one changed
back, one whose decimal was rewritten with other text, one whose internal column alone was
written - come up by themselves. The diff still compares every table of the instance.

The current state is read back through the unit of work and the basis from its Snapshot
file, both written the way requirement 18.5 writes values, so the comparison is against
what the two databases hold rather than against what the diff itself read.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given, note, settings
from hypothesis import strategies as st

from erp_mock.common.jsonio import JsonValue
from erp_mock.state.diff import Change, compute_diff
from erp_mock.state.schema import ID_COLUMN, StorageSchema, TableSchema, build_schema
from erp_mock.state.snapshot import BASE_DB_FILENAME
from tests.helpers.fixtures import fixture_domain
from tests.property.models.state_ops import (
    JSON_TYPES,
    Fields,
    State,
    build_basis,
    current_state,
    plans,
    run_plan,
    sandbox_from,
    snapshot_state,
)

# The keys every change starts with, in the order of design.md data model 5; ``before`` and
# ``after`` follow as the op has them (requirements 18.6 and 18.9).
CHANGE_KEYS: Final = ["entity", "op", "id", "cause_seqs"]


@pytest.fixture(scope="module")
def schema() -> StorageSchema:
    return build_schema(fixture_domain())


@pytest.fixture(scope="module")
def basis(tmp_path_factory: pytest.TempPathFactory, schema: StorageSchema) -> Path:
    """The Snapshot every example creates its sandbox from, built once."""
    return build_basis(tmp_path_factory.mktemp("basis"), schema)


@pytest.fixture(scope="module")
def base_state(basis: Path, schema: StorageSchema) -> State:
    """What the basis holds; the same for every example, so it is read once."""
    return snapshot_state(basis, schema)


@pytest.fixture(scope="module")
def sandboxes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandbox of each example is created below."""
    return tmp_path_factory.mktemp("sandboxes")


# --- helpers -------------------------------------------------------------------------------


def patched(base: State, changes: Sequence[Change]) -> State:
    """Return ``base`` with every change applied: the state the diff says the sandbox is in.

    The patch of requirement 18.2: an insert adds the record its ``after`` describes, a
    delete removes the record, and an update writes its ``after`` over the fields it names.
    An insert of a record the basis holds, or an update or a delete of one it does not, is
    no patch at all and fails here.
    """
    found = {table: dict(records) for table, records in base.items()}
    for change in changes:
        records = found[change.entity]
        held = change.id in records
        if change.op == "delete":
            assert held, f"the basis has no {change.entity} {change.id} to delete"
            del records[change.id]
            continue
        assert change.after is not None, f"{change.op} of {change.id} has no after"
        if change.op == "insert":
            assert not held, f"the basis holds {change.entity} {change.id} already"
            records[change.id] = dict(change.after)
        else:
            assert held, f"the basis has no {change.entity} {change.id} to update"
            records[change.id] = {**records[change.id], **change.after}
    return found


def differing(base: State, current: State) -> set[tuple[str, str]]:
    """Every (entity, id) whose non-internal fields differ between the two sides (req 18.2).

    A record only one side holds differs, and so does one both sides hold whose fields are
    not all equal; a record with equal fields - the internal columns may well differ - does
    not, and must not be reported.
    """
    found: set[tuple[str, str]] = set()
    for one, other in ((base, current), (current, base)):
        for table, records in one.items():
            for record_id, fields in records.items():
                if other[table].get(record_id) != fields:
                    found.add((table, record_id))
    return found


def assert_shape(
    change: Change, table: TableSchema, before: Fields | None, after: Fields | None
) -> None:
    """Pin the keys and the values of one change (requirements 18.5, 18.6 and 18.9).

    ``before`` and ``after`` are what the two sides hold for the record, None when that
    side does not hold it.
    """
    public = sorted(column.name for column in table.public_columns if column.name != ID_COLUMN)
    keys = list(change.as_json())
    if change.op == "insert":
        assert (before, change.before) == (None, None)
        assert after is not None and change.after is not None
        assert keys == [*CHANGE_KEYS, "after"]
        # Every non-internal field but id, in field name order, as the sandbox holds them.
        assert list(change.after) == public
        assert change.after == after
    elif change.op == "delete":
        assert (after, change.after) == (None, None)
        assert before is not None and change.before is not None
        assert keys == [*CHANGE_KEYS, "before"]
        assert list(change.before) == public
        assert change.before == before
    else:
        assert before is not None and after is not None
        assert change.before is not None and change.after is not None
        assert keys == [*CHANGE_KEYS, "before", "after"]
        # Only the fields whose values differ, the same keys in both, in field name order.
        changed = sorted(name for name in public if before[name] != after[name])
        assert changed, f"{table.name} {change.id} has no differing field"
        assert list(change.before) == changed
        assert list(change.after) == changed
        assert change.before == {name: before[name] for name in changed}
        assert change.after == {name: after[name] for name in changed}
    assert_json_types(table, change.before)
    assert_json_types(table, change.after)


def assert_json_types(table: TableSchema, fields: Mapping[str, JsonValue] | None) -> None:
    """Every value is of the JSON type of its field type (requirement 18.5).

    A bool is a JSON boolean and an int a JSON integer, which equality alone does not
    separate: ``True == 1``. An absent value is null.
    """
    for name, value in (fields or {}).items():
        expected = JSON_TYPES[table.columns[name].type]
        assert value is None or type(value) is expected, (
            f"{table.name}.{name} is a {table.columns[name].type} field"
            f" and is written as {type(value).__name__}: {value!r}"
        )


# --- the property --------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 28: diff 是从基准到当前状态的净补丁
@pytest.mark.req("18.2", "18.5", "18.6", "18.9")
@settings(max_examples=100)
@given(data=st.data())
def test_the_changes_patch_the_basis_into_the_current_state(
    schema: StorageSchema,
    basis: Path,
    base_state: State,
    sandboxes: Path,
    data: st.DataObject,
) -> None:
    plan = data.draw(plans(schema), label="plan")

    with sandbox_from(basis, sandboxes, schema) as store:
        run_plan(store, plan)
        body = compute_diff(store.db_path, store.sandbox_dir / BASE_DB_FILENAME, schema)
        current = current_state(store)
    note(f"changes: {[(change.entity, change.op, change.id) for change in body.changes]}")

    # The changes are the patch from the basis to the current state (requirement 18.2).
    assert patched(base_state, body.changes) == current
    # Nothing else is reported: the records whose non-internal fields are equal, the ones
    # created and deleted again among them, do not appear (requirement 18.2).
    reported = {(change.entity, change.id) for change in body.changes}
    assert reported == differing(base_state, current)
    # Ordered by entity, then by id (requirement 18.6).
    keys = [change.sort_key for change in body.changes]
    assert keys == sorted(keys)
    for change in body.changes:
        table = schema.tables[change.entity]
        assert_shape(
            change,
            table,
            base_state[change.entity].get(change.id),
            current[change.entity].get(change.id),
        )
