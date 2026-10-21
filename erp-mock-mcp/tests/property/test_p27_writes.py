"""Property 27: the writes of a call are its net change, attributed to what made it.

**Validates: Requirements 17.4**

For every call of a random operation sequence (:mod:`tests.property.opseq`), the ``writes``
the call reports are exactly the net change it made to the records of the sandbox, one item
per (record, cause):

- the (entity, id) of the items are exactly the records whose non-internal content differs
  between the state right before the call and the state right after it;
- the ``op`` of an item is the net change of its record: ``insert`` for a record only the
  state after the call holds, ``delete`` for one only the state before it holds, and
  ``update`` for one both hold with a different content;
- a record nothing but an internal column changed on is no change of the record and no item
  of ``writes``, and no record of an internal table is one either (requirement 18.4);
- the items are ordered by entity, id and cause by code point and no (entity, id, cause) is
  listed twice;
- the ``cause`` of an item is ``direct`` for the record the call worked on and its lines -
  the bill a push generated and its lines included - the id of an inventory linkage of that
  bill for a record of the two stock tables (requirement 9.1), and the id of a writeback of
  the conversion the operated bill came from for a record of its source bill
  (requirement 8.8);
- a call that ended with an error reports no writes and changed nothing (requirement 17.5).

The reference model is the net change itself (:class:`Change`). The sandbox is opened with
``watch=True``, so every call records every table of it before and after itself, and
:func:`~tests.property.opseq.session.public_contents` drops the internal columns and the
internal tables from both readings - the key of what it returns is exactly the (entity, id)
of a write (requirement 18.4). The difference between the two readings is what the
``writes`` are compared against; nothing of the ``writes`` decides what is expected of them.

The causes come from the configuration (:class:`BillCauses`, :func:`causes_of`): which
inventory linkages are declared on the operated bill, and which writebacks belong to a
conversion whose target it is, split by whether they change the header of the source bill or
its lines - a ``source.<field>`` target against a ``source_line.<field>`` one
(requirement 8.8) - with a writeback that closes or reopens the source bill by itself
changing its header as well (requirements 8.13 and 8.14). Which records of the source bill
those are is decided by the ``source_bill_id`` of the operated record, so a write that
reaches some other bill of the sandbox is caught rather than taken for a writeback. The ids
of the conversions themselves are the cause of nothing: the bill a push generates is the
record that call worked on, and therefore ``direct``.

The one cause requirement 17.4 does not spell out. The engine writes the operation sequence
number of the call into the ``updated_seq`` of a source bill that a writeback, an automatic
close or an automatic unclose changed (requirement 4.5), and that write is the engine's own
rather than the writeback's: it carries ``direct`` on a record that is not the one the call
worked on. :meth:`Operated.allowed` accepts it on the header of such a source bill, and only
where ``updated_seq`` is among the fields that changed, so no other direct write may reach a
record of another bill.

A hundred examples, which is what a property that is not 3 or 17 draws (tests/conftest.py);
each one walks a sequence of up to 50 operations in a sandbox of its own. Every successful
call reports the ops and the causes of its writes as Hypothesis events, so
``--hypothesis-show-statistics`` shows which of the three ops and which of the configured
linkages and writebacks a run covered.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, event, given, note, settings
from hypothesis import strategies as st

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import BillModel, DomainModel
from erp_mock.domain.conversion import SOURCE_BILL_ID
from erp_mock.domain.numbering import LINE_ID_SEPARATOR, UPDATED_SEQ
from erp_mock.state.schema import ID_COLUMN, STOCK_BALANCE_TABLE, STOCK_TXN_TABLE, StorageSchema
from erp_mock.state.store import Record, Value, Write, WriteOp
from erp_mock.tools.generator import ToolSpec
from tests.property.opseq import (
    Performed,
    Start,
    Tables,
    fixture_start,
    open_sandbox,
    public_contents,
    walk,
)

# How many sequences the property draws (design.md, property testing).
EXAMPLES: Final = 100

# The cause of a write the engine made itself, written the way requirement 17.4 writes it.
DIRECT: Final = "direct"
_DIRECT_ONLY: Final[frozenset[str]] = frozenset({DIRECT})

# The two tables only an inventory linkage writes (requirement 9.1).
STOCK_TABLES: Final[frozenset[str]] = frozenset({STOCK_TXN_TABLE, STOCK_BALANCE_TABLE})

# The level a writeback names the lines of its source bill with; anything else is a field of
# its header (requirement 8.8, :class:`~erp_mock.config.model.WritebackModel`).
LINE_LEVEL: Final = "line"

# How many records a failing comparison prints.
REPORTED: Final = 10

# One record of the sandbox: the (entity, id) of a write and of a change (requirement 17.4).
type Place = tuple[str, str]


# --- the net change of one call (the reference model) ------------------------------------


@dataclass(frozen=True, slots=True)
class Change:
    """The net change of one call, read off the records before and after it.

    - ``ops``: the net change of every record whose non-internal content the call changed,
      by (entity, id), which is what the (entity, id) and the ``op`` of the ``writes`` are
      compared against (requirement 17.4);
    - ``fields``: the non-internal fields that differ, per record, which a failure prints
      and the sequence number stamp of requirement 4.5 is recognised by;
    - ``internal_only``: the records the call changed in an internal column alone, which is
      no change of the record and no write of the call (requirement 18.4).
    """

    ops: Mapping[Place, WriteOp]
    fields: Mapping[Place, frozenset[str]]
    internal_only: frozenset[Place]


def net_change(performed: Performed, schema: StorageSchema) -> Change:
    """The net change ``performed`` made, from the tables it recorded around itself.

    Both readings hold every table of the sandbox; the internal columns and the internal
    tables are dropped from them first (:func:`~tests.property.opseq.session.public_contents`),
    so what is compared is the records and the fields requirement 18.4 covers.
    """
    before, after = performed.before, performed.after
    assert before is not None and after is not None, "the sandbox is not watching its tables"
    one, two = public_contents(before, schema), public_contents(after, schema)
    ops: dict[Place, WriteOp] = {}
    fields: dict[Place, frozenset[str]] = {}
    for place in sorted(one.keys() | two.keys()):
        was, now = one.get(place), two.get(place)
        if was is None and now is not None:
            ops[place], fields[place] = "insert", frozenset(now)
        elif now is None and was is not None:
            ops[place], fields[place] = "delete", frozenset(was)
        elif was is not None and now is not None and was != now:
            ops[place], fields[place] = "update", _differing(was, now)
    return Change(
        ops=ops,
        fields=fields,
        internal_only=_internal_only(before, after, schema, changed=ops.keys()),
    )


def _differing(was: Mapping[str, Value], now: Mapping[str, Value]) -> frozenset[str]:
    """The fields two contents of one record differ in (requirement 18.2)."""
    return frozenset(name for name in was.keys() | now.keys() if was.get(name) != now.get(name))


def _internal_only(
    before: Tables, after: Tables, schema: StorageSchema, *, changed: Iterable[Place]
) -> frozenset[Place]:
    """The records of the diffable tables an internal column alone changed on (req 18.4).

    The whole record is compared here, the internal columns included, and the records whose
    non-internal content changed are taken out again: what is left changed in ``_close_mode``,
    ``_conversion`` or ``_bill_id`` and nowhere else, which no result and no diff of
    requirement 18 shows and which therefore is no write of the call.
    """
    one, two = _records(before, schema), _records(after, schema)
    public = set(changed)
    return frozenset(
        place
        for place in one.keys() & two.keys()
        if one[place] != two[place] and place not in public
    )


def _records(tables: Tables, schema: StorageSchema) -> dict[Place, Record]:
    """Every record of the tables the diff covers, internal columns and all, by (entity, id)."""
    return {
        (table.name, str(record[ID_COLUMN])): record
        for table in schema.diffable_tables
        for record in tables.get(table.name, [])
    }


# --- what the configuration lets one operation change (requirements 8.8 and 9.1) ---------


@dataclass(frozen=True, slots=True)
class BillCauses:
    """What a linkage or a writeback may change when an operation of one bill runs.

    Read off the configuration once per starting point (:func:`causes_of`):

    - ``linkages``: the ids of the inventory linkages declared on the bill, which every
      write of the two stock tables carries (requirement 9.1);
    - ``source_headers``: per source bill table, the ids of the writebacks that change the
      header of that bill - a ``source.<field>`` target, and the automatic close and unclose
      of a writeback that declares a condition (requirements 8.8, 8.13 and 8.14). A source
      bill whose writebacks all target its lines is a key with an empty set, since the
      engine still stamps its sequence number (requirement 4.5);
    - ``source_lines``: per source line table, the ids of the writebacks whose target is a
      ``source_line.<field>`` (requirement 8.8).
    """

    linkages: frozenset[str] = frozenset()
    source_headers: FrozenMap[str, frozenset[str]] = FrozenMap()
    source_lines: FrozenMap[str, frozenset[str]] = FrozenMap()


def causes_of(domain: DomainModel) -> FrozenMap[str, BillCauses]:
    """The :class:`BillCauses` of every bill of ``domain``, by the name of the bill.

    The linkages of a bill are the ones declared on it, and the writebacks of a bill are the
    ones of the conversions whose target it is: those run on the approve and the unapprove of
    that bill and change the source bill it was pushed from (requirements 8.8 and 8.9).
    """
    linkages: dict[str, set[str]] = {}
    for linkage in domain.linkages.values():
        linkages.setdefault(linkage.bill, set()).add(linkage.id)
    headers: dict[str, dict[str, set[str]]] = {}
    lines: dict[str, dict[str, set[str]]] = {}
    for writeback in domain.writebacks.values():
        conversion = domain.conversions.get(writeback.conversion)
        source = None if conversion is None else domain.bills.get(conversion.source)
        target = None if conversion is None else domain.bills.get(conversion.target)
        if source is None or target is None:
            continue
        of_header = headers.setdefault(target.name, {}).setdefault(source.name, set())
        if writeback.target.level == LINE_LEVEL:
            lines.setdefault(target.name, {}).setdefault(source.line_name, set()).add(writeback.id)
        else:
            of_header.add(writeback.id)
        if writeback.auto_close_source_when is not None:
            of_header.add(writeback.id)
    return FrozenMap(
        (
            name,
            BillCauses(
                linkages=frozenset(linkages.get(name, ())),
                source_headers=_frozen(headers.get(name, {})),
                source_lines=_frozen(lines.get(name, {})),
            ),
        )
        for name in domain.bills
    )


def _frozen(found: Mapping[str, set[str]]) -> FrozenMap[str, frozenset[str]]:
    """One table of :func:`causes_of` as the read-only map a :class:`BillCauses` holds."""
    return FrozenMap((table, frozenset(ids)) for table, ids in found.items())


# --- what one call worked on -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Operated:
    """The record one call worked on, and the causes its writes may carry (req 17.4).

    ``bill`` and ``id`` are the bill and the record the call operated on, which for a push
    is the bill it generated (requirement 8.1); ``source`` is the id of the bill that record
    was pushed from, read off its ``source_bill_id``, None for a record no push generated;
    ``causes`` is what a linkage or a writeback of this bill may change
    (:class:`BillCauses`).
    """

    bill: BillModel
    id: str
    source: str | None
    causes: BillCauses

    def allowed(self, write: Write, changed: frozenset[str]) -> frozenset[str]:
        """The causes requirement 17.4 allows on the record of ``write``.

        An empty set is a record the call had no business changing at all. ``changed`` is
        the non-internal fields of that record that differ, which decides whether the
        sequence number stamp of requirement 4.5 may have reached a source bill (module
        docstring).
        """
        entity, record = write.entity, write.id
        if entity == self.bill.name and record == self.id:
            return _DIRECT_ONLY
        if entity == self.bill.line_name and record.startswith(self.id + LINE_ID_SEPARATOR):
            return _DIRECT_ONLY
        if entity in STOCK_TABLES:
            return self.causes.linkages
        source = self.source
        if source is None:
            return frozenset()
        if record == source and entity in self.causes.source_headers:
            stamp = _DIRECT_ONLY if UPDATED_SEQ in changed else frozenset()
            return self.causes.source_headers[entity] | stamp
        if record.startswith(source + LINE_ID_SEPARATOR) and entity in self.causes.source_lines:
            return self.causes.source_lines[entity]
        return frozenset()


def operated_of(start: Start, causes: Mapping[str, BillCauses], performed: Performed) -> Operated:
    """What the successful ``performed`` worked on, read off the result it returned.

    The result of a create, an update, a push and a state operation is the record as the
    sandbox now holds it and the result of a delete the record as it was, so the ``id`` of
    either names the record the call operated on (requirements 5.16, 13.2 and 13.9).
    """
    bill = bill_of(start.domain, performed.spec)
    assert bill is not None, f"{where(performed)} is no operation of a bill"
    record_id = result_id(performed)
    return Operated(
        bill=bill,
        id=record_id,
        source=source_id(performed, bill, record_id),
        causes=causes.get(bill.name, BillCauses()),
    )


def bill_of(domain: DomainModel, spec: ToolSpec) -> BillModel | None:
    """The bill the call of ``spec`` operates on, None for a tool that works on no bill.

    An operation of a bill names the bill itself and a push the target bill of its
    conversion, which is the bill it generates (requirements 8.1 and 13.2).
    """
    match spec.kind:
        case "entity_op":
            return domain.bills.get(spec.target)
        case "push":
            conversion = domain.conversions.get(spec.target)
            return None if conversion is None else domain.bills.get(conversion.target)
        case _:
            return None


def result_id(performed: Performed) -> str:
    """The id of the record the result of ``performed`` names (requirement 13.2)."""
    structured = performed.outcome.structured
    assert structured is not None, f"{where(performed)} returned no result"
    # The key of the id in a result is the name of the column it is stored under (req 13.3).
    found = structured.get(ID_COLUMN)
    assert isinstance(found, str) and found, f"{where(performed)} returned no id"
    return found


def source_id(performed: Performed, bill: BillModel, record_id: str) -> str | None:
    """The bill the operated record was pushed from, None for a record no push generated.

    The ``source_bill_id`` of the record, read from the state after the call and, for a
    delete, from the state before it (requirement 8.2).
    """
    for tables in (performed.after, performed.before):
        record = _held(tables, bill.name, record_id)
        if record is None:
            continue
        found = record.get(SOURCE_BILL_ID)
        return found if isinstance(found, str) and found else None
    return None


def _held(tables: Tables | None, table: str, record_id: str) -> Record | None:
    """The record ``record_id`` of ``table`` in one reading, None where it holds none."""
    if tables is None:
        return None
    return next((found for found in tables.get(table, []) if found[ID_COLUMN] == record_id), None)


# --- the checks --------------------------------------------------------------------------


def check_call(
    start: Start, schema: StorageSchema, causes: Mapping[str, BillCauses], performed: Performed
) -> None:
    """One call of a sequence against its net change (module docstring)."""
    change = net_change(performed, schema)
    if performed.failed:
        check_failed(performed, change)
        return
    report(performed)
    check_entities(performed, schema)
    check_records(performed, change)
    check_internal(performed, change)
    check_order(performed)
    check_causes(performed, change, operated_of(start, causes, performed))


def check_failed(performed: Performed, change: Change) -> None:
    """A failed call reports no writes and changed no record (requirement 17.5)."""
    assert performed.writes == (), (
        f"{where(performed)} failed with {performed.error_code}"
        f" and reported {shown(performed.writes)}"
    )
    assert not change.ops, (
        f"{where(performed)} failed with {performed.error_code} and changed {places(change.ops)}"
    )


def check_entities(performed: Performed, schema: StorageSchema) -> None:
    """Every write names a table the diff covers, so no internal one (requirement 18.4)."""
    covered = {table.name for table in schema.diffable_tables}
    unknown = sorted({write.entity for write in performed.writes} - covered)
    assert not unknown, f"{where(performed)} reported writes of tables no diff covers: {unknown}"


def check_records(performed: Performed, change: Change) -> None:
    """The records and the ops of the writes are the net change of the call (req 17.4)."""
    held = {(write.entity, write.id) for write in performed.writes}
    unchanged = sorted(held - set(change.ops))
    assert not unchanged, (
        f"{where(performed)} reported writes of records it did not change: {unchanged[:REPORTED]}"
    )
    unreported = sorted(set(change.ops) - held)
    assert not unreported, (
        f"{where(performed)} changed records it reported no write of:"
        f" {[(place, sorted(change.fields[place])) for place in unreported[:REPORTED]]}"
    )
    for write in performed.writes:
        expected = change.ops[(write.entity, write.id)]
        assert write.op == expected, (
            f"{where(performed)} reported {write.op} of {write.entity} {write.id},"
            f" whose net change is a {expected}"
        )


def check_internal(performed: Performed, change: Change) -> None:
    """A change of an internal column alone is no write of the call (requirement 18.4)."""
    if change.internal_only:
        event("an internal column changed on its own")
    reported = sorted(
        {(write.entity, write.id) for write in performed.writes} & change.internal_only
    )
    assert not reported, (
        f"{where(performed)} reported writes of records whose internal columns alone"
        f" changed: {reported[:REPORTED]}"
    )


def check_order(performed: Performed) -> None:
    """The items are ordered by entity, id and cause, each of them once (req 17.4)."""
    keys = [(write.entity, write.id, write.cause) for write in performed.writes]
    assert keys == sorted(keys), (
        f"{where(performed)} reported {shown(performed.writes)} out of order"
    )
    assert len(set(keys)) == len(keys), (
        f"{where(performed)} reported an (entity, id, cause) twice: {shown(performed.writes)}"
    )


def check_causes(performed: Performed, change: Change, operated: Operated) -> None:
    """Every write is attributed to what the configuration says made it (req 17.4)."""
    for write in performed.writes:
        changed = change.fields.get((write.entity, write.id), frozenset())
        allowed = operated.allowed(write, changed)
        assert write.cause in allowed, (
            f"{where(performed)} on {operated.bill.name} {operated.id} attributed the"
            f" {write.op} of {write.entity} {write.id} to {write.cause!r};"
            f" {'nothing' if not allowed else sorted(allowed)} may have written that record"
        )


# --- what a run and a failure report -----------------------------------------------------


def report(performed: Performed) -> None:
    """Report the ops and the causes of one successful call as Hypothesis events."""
    if not performed.writes:
        event(f"a successful {performed.action} wrote nothing")
        return
    for op in sorted({write.op for write in performed.writes}):
        event(f"{performed.action}: a net {op}")
    for cause in sorted({write.cause for write in performed.writes}):
        event(f"{performed.action}: written by {cause}")


def where(performed: Performed) -> str:
    """The call a failure names: its sequence number and the tool it called."""
    return f"call {performed.seq}, {performed.tool},"


def shown(writes: tuple[Write, ...]) -> str:
    """The writes of a call as a failure prints them (requirement 17.4)."""
    return str([(write.entity, write.op, write.id, write.cause) for write in writes[:REPORTED]])


def places(ops: Mapping[Place, WriteOp]) -> str:
    """The records of a net change as a failure prints them."""
    return str([(place, op) for place, op in list(ops.items())[:REPORTED]])


# --- the starting point and the sandboxes of the run -------------------------------------


@pytest.fixture(scope="module")
def start(tmp_path_factory: pytest.TempPathFactory) -> Start:
    """The fixture starting point, built once for the module (requirement 25.2)."""
    return fixture_start(tmp_path_factory.mktemp("p27"))


@pytest.fixture(scope="module")
def sandboxes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandbox of each example is created below."""
    return tmp_path_factory.mktemp("p27-sandboxes")


@pytest.fixture(scope="module")
def causes(start: Start) -> FrozenMap[str, BillCauses]:
    """What a linkage or a writeback of each bill may change (:func:`causes_of`)."""
    return causes_of(start.domain)


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 27: writes 与净变化一致
@pytest.mark.req("17.4")
@settings(
    max_examples=EXAMPLES,
    # A sequence of up to 50 operations that reads every table of the sandbox around every
    # call draws a lot and takes its time; neither says anything about the property.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_the_writes_of_a_call_are_its_net_change_per_cause(
    data: st.DataObject, start: Start, causes: FrozenMap[str, BillCauses], sandboxes: Path
) -> None:
    with open_sandbox(start, sandboxes, watch=True) as session:
        steps = walk(data, session)
        note("sequence: " + ", ".join(f"{s.seq} {s.tool}{'!' if s.failed else ''}" for s in steps))
        for performed in steps:
            check_call(start, session.schema, causes, performed)
