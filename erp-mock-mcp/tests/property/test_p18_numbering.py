"""Property 18: the ids, the numbers and the operation sequence numbers of a sequence.

**Validates: Requirements 4.1, 4.2, 4.4, 4.5, 4.11, 4.12**

For any random operation sequence (:mod:`tests.property.opseq`), every successful create
and push gets ``<id_prefix>_<n>`` with n one above the last id of that bill type
(requirement 4.1) and ``<prefix><YYYYMMDD><4 digits>`` with the sequence of its (number
prefix, business date) (requirement 4.4), the lines of a bill are ``<bill id>-<line
number>`` numbered from 1 without a gap (requirement 4.2), and every successful write
operation takes the next operation sequence number and writes it into ``created_seq`` and
``updated_seq`` the way requirement 4.5 says. A read-only call takes none
(requirement 4.11), and with the failed calls left out the successful ones get exactly the
same ids, numbers and sequence numbers (requirement 4.12).

The reference model (:class:`Numbering`) keeps the counters of requirement 4.6 itself: it
starts from the state the sandbox inherits from its Snapshot - the counters and the bills
that are already there - and raises a counter per operation of the sequence the way the
requirements say it should be raised, one id and one number per generated bill and one
operation sequence number per successful write operation. No counter of the engine is read
to decide an expected value: every expectation comes off the model, and the ``_counters``
table of the sandbox is only ever compared against it. The one value the model takes off a
call is the business date of the bill it generated, which is the date requirement 4.4
builds the number from - a create sends it or takes the default and a conversion may map it
from the source bill - while the sequence of that (prefix, date) is the model's own.

What each call is checked against, all of it off the result the call returned, which is the
record the sandbox holds (requirement 5.16):

- a create and a push: the id, the number and the two sequence numbers of the bill they
  generated, and that neither the id nor the number was ever handed out before, a deleted
  bill's included (requirements 4.1, 4.4 and 4.12);
- an update, a delete and a state operation: the bill they were called on keeps its number
  and its ``created_seq`` and takes the operation's sequence number in ``updated_seq``; a
  delete returns the record as it was and writes its sequence number nowhere
  (requirement 4.5);
- the lines of every result: ``<bill id>-<line number>``, the numbers running from 1 in
  result order (requirement 4.2);
- the source bills a writeback, an automatic close or an automatic unclose changed, which
  the ``writes`` of the call report (requirement 17.4): their ``updated_seq`` becomes the
  operation's sequence number while their ``created_seq`` stays (requirement 4.5).

Once the sequence is over, the counters of the sandbox and the ``number``, ``created_seq``
and ``updated_seq`` of every bill it holds must equal the model's - which is where a call
that ended with an error is caught having numbered something (requirement 4.12) and where
a bill the operations never named is caught having been stamped (requirement 4.5) - a
search, a query, a get and the business context of the sandbox must leave every counter
where it is (requirement 4.11), and the successful calls alone, replayed in a second
sandbox copied from the same snapshot, must return the same ids, numbers and sequence
numbers and leave the same counters behind (requirement 4.12).

A hundred examples, which is what a property that is not 3 or 17 draws (tests/conftest.py);
each one runs its sequence of up to 50 operations in a sandbox of its own and the
successful part of it in a second one, and reads the state of the sandbox twice rather than
once per call, so the examples stay quick.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, given, note, settings
from hypothesis import strategies as st

from erp_mock.common.jsonio import JsonValue
from erp_mock.config.model import BillModel
from erp_mock.domain.numbering import (
    CREATED_SEQ,
    FIRST_LINE_NO,
    OP_COUNTER_KEY,
    UPDATED_SEQ,
    format_id,
    format_number,
    line_id,
    number_key,
)
from erp_mock.domain.views import LINES
from erp_mock.state.schema import ID_COLUMN
from erp_mock.state.store import Record
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET, PUSH_ACTION
from erp_mock.tools.input_schema import ID_PARAM
from tests.property.opseq import (
    Call,
    Performed,
    Session,
    Start,
    fixture_start,
    open_sandbox,
    walk,
)

# How many sequences the property draws: properties 3 and 17 draw 500, every other one 100
# (tests/conftest.py).
EXAMPLES: Final = 100

# The standard fields of a bill this property reads (meta-model.md section 4): its number,
# the business date the number is built from (requirement 4.4) and the line number of one
# line (requirement 4.2). ``created_seq`` and ``updated_seq`` come from the numbering
# primitive itself.
_NUMBER: Final = "number"
_BIZ_DATE: Final = "biz_date"
_LINE_NO: Final = "line_no"

# The counter kinds of requirement 4.6 a sequence of bill operations raises: the id counter
# of a bill type, the number counter of a (number prefix, date) and the one operation
# sequence number counter of the sandbox. The ``code`` counters belong to the create of an
# entity record, which no operation of a sequence performs (:data:`~tests.property.opseq.OPERATIONS`).
_ID_KIND: Final = "id"
_NUMBER_KIND: Final = "number"
_OP_KIND: Final = "op"

# The two operations that generate a bill, and so take an id and a number (requirements
# 4.1 and 4.4): the create of a bill and a push.
_GENERATING: Final[tuple[str, ...]] = ("create", PUSH_ACTION)
# The operation that takes a sequence number and writes it into no bill at all
# (requirement 4.5).
_DELETE: Final = "delete"

# One bill of the sandbox: the name of its bill type, which is its table, and its id - the
# (entity, id) of a write as well (requirement 17.4).
type BillKey = tuple[str, str]


# --- the reference model (requirement 4.6) -----------------------------------------------


@dataclass(frozen=True, slots=True)
class Stamp:
    """What one bill of the sandbox carries of the numbering (requirements 4.4 and 4.5).

    The number it was given when it was created, which never changes afterwards, and the
    two operation sequence numbers of its header.
    """

    number: str | None
    created_seq: int | None
    updated_seq: int | None


@dataclass(slots=True)
class Numbering:
    """The counters and the stamps the sequence should have produced (requirement 4.6).

    - ``counters``: the counters of the sandbox by (kind, key), the ones the Snapshot was
      inherited with raised by what each operation takes;
    - ``bills``: the stamp of every bill the sandbox holds, by (bill type, id);
    - ``ids`` and ``numbers``: every internal id and every bill number that was ever handed
      out, the ones of deleted records included, so a reuse is caught (requirements 4.1,
      4.4 and 4.12).
    """

    counters: dict[tuple[str, str], int] = field(default_factory=dict)
    bills: dict[BillKey, Stamp] = field(default_factory=dict)
    ids: set[str] = field(default_factory=set)
    numbers: set[str] = field(default_factory=set)

    def bump(self, kind: str, key: str) -> int:
        """Raise the counter (``kind``, ``key``) by one and return its new value.

        A counter the sandbox has no row for counts as 0, which is what a Snapshot that
        never raised it leaves behind (requirement 4.6).
        """
        value = self.counters.get((kind, key), 0) + 1
        self.counters[(kind, key)] = value
        return value

    def generated(self, table: str, record_id: str, number: str, seq: int) -> None:
        """Record the bill ``record_id`` a create or a push generated (requirement 4.5)."""
        self.ids.add(record_id)
        self.numbers.add(number)
        self.bills[(table, record_id)] = Stamp(number=number, created_seq=seq, updated_seq=seq)

    def changed(self, table: str, record_id: str, seq: int) -> None:
        """Record that the operation ``seq`` changed the bill ``record_id`` (req 4.5)."""
        stamp = self.bills[(table, record_id)]
        self.bills[(table, record_id)] = replace(stamp, updated_seq=seq)

    def dropped(self, table: str, record_id: str) -> None:
        """Record that the bill ``record_id`` was deleted; its id and number stay used."""
        del self.bills[(table, record_id)]


def baseline(session: Session) -> Numbering:
    """The model of the state the sandbox starts in, before the first call (req 4.6).

    A sandbox continues the counters of its Snapshot and holds the records that Snapshot
    was built with, so the model starts from both: the counters as they are and one stamp
    per bill that is already there. Called before the first operation of the sequence; from
    there on the model is raised by the operations alone.
    """
    model = Numbering(counters=dict(session.counters()))
    for name, table in session.schema.tables.items():
        if table.kind not in ("entity", "bill"):
            continue
        for record in session.records(name):
            record_id = str(record[ID_COLUMN])
            model.ids.add(record_id)
            if table.kind != "bill":
                continue
            # The bills of the starting point: the fixture dataset has none, and the
            # dataset Snapshots of task 19.8 do, whose bills the sequences then operate on.
            stamp = _stamp_of(record)
            if stamp.number is not None:
                model.numbers.add(stamp.number)
            model.bills[(name, record_id)] = stamp
    return model


# --- one call of the sequence ------------------------------------------------------------


def check_call(model: Numbering, start: Start, performed: Performed) -> None:
    """Check one call of the sequence against the model (requirements 4.1 to 4.5).

    A call that ended with an error numbers nothing at all, so the model is left as it is
    (requirements 4.11 and 4.12); what such a call left the counters at is checked once the
    sequence is over (:func:`check_state`) and by the replay (:func:`check_replay`).
    """
    if performed.failed:
        return
    where = f"call {performed.seq} ({performed.tool})"
    result = performed.outcome.structured
    assert result is not None, f"{where} went through with no structuredContent"
    # Requirement 4.11: every successful write operation takes the next sequence number,
    # and every operation of a sequence is one (requirement 25.2).
    seq = model.bump(_OP_KIND, OP_COUNTER_KEY)
    bill = _bill_of(start, performed)
    if performed.action in _GENERATING:
        _check_generated(model, bill, result, seq, where)
    else:
        _check_called_on(model, performed, bill, result, seq, where)
    _check_lines(result, where)
    _stamp_changed(model, start, performed, seq, where)


def _check_generated(
    model: Numbering, bill: BillModel, result: Mapping[str, JsonValue], seq: int, where: str
) -> None:
    """The id, the number and the two seqs of a generated bill (4.1, 4.4, 4.5 and 4.12)."""
    expected_id = format_id(bill.id_prefix, model.bump(_ID_KIND, bill.name))
    record_id = _as_text(result.get(ID_COLUMN))
    assert record_id == expected_id, f"{where} returned the id {record_id}, expected {expected_id}"
    assert record_id not in model.ids, f"{where} was given the id {record_id} a second time"

    biz_date = _as_date(result.get(_BIZ_DATE))
    assert biz_date is not None, f"{where} generated a bill with no business date"
    prefix = bill.number_prefix
    expected_number = format_number(
        prefix, biz_date, model.bump(_NUMBER_KIND, number_key(prefix, biz_date))
    )
    number = _as_text(result.get(_NUMBER))
    assert number == expected_number, (
        f"{where} returned the number {number}, expected {expected_number}"
    )
    assert number not in model.numbers, f"{where} was given the number {number} a second time"

    assert _as_int(result.get(CREATED_SEQ)) == seq, f"{where} wrote another created_seq than {seq}"
    assert _as_int(result.get(UPDATED_SEQ)) == seq, f"{where} wrote another updated_seq than {seq}"
    model.generated(bill.name, expected_id, expected_number, seq)


def _check_called_on(
    model: Numbering,
    performed: Performed,
    bill: BillModel,
    result: Mapping[str, JsonValue],
    seq: int,
    where: str,
) -> None:
    """The bill an update, a delete or a state operation was called on (4.4 and 4.5)."""
    args = performed.call.args if performed.call.args is not None else {}
    record_id = _as_text(args.get(ID_PARAM))
    assert record_id is not None, f"{where} named no record under {ID_PARAM}"
    before = model.bills.get((bill.name, record_id))
    assert before is not None, f"{where} worked on {record_id}, which the model does not hold"
    assert _as_text(result.get(ID_COLUMN)) == record_id, f"{where} returned another record"
    # Requirement 4.4: a bill keeps the number it was created with.
    assert _as_text(result.get(_NUMBER)) == before.number, (
        f"{where} returned the number {result.get(_NUMBER)}, expected {before.number}"
    )
    # Requirement 4.5: created_seq stays as it is whatever the operation.
    assert _as_int(result.get(CREATED_SEQ)) == before.created_seq, (
        f"{where} changed created_seq from {before.created_seq}"
    )
    if performed.action == _DELETE:
        # The result of a delete is the record as it was, so it carries the updated_seq of
        # the operation before it; the delete itself writes its own into no bill.
        assert _as_int(result.get(UPDATED_SEQ)) == before.updated_seq, (
            f"{where} returned another updated_seq than the {before.updated_seq} it had"
        )
        model.dropped(bill.name, record_id)
        return
    assert _as_int(result.get(UPDATED_SEQ)) == seq, f"{where} wrote another updated_seq than {seq}"
    model.changed(bill.name, record_id, seq)


def _check_lines(result: Mapping[str, JsonValue], where: str) -> None:
    """The line ids of a result: ``<bill id>-<line number>``, from 1 on (requirement 4.2).

    The lines of a result are in line number order (requirement 5.12), so the position of a
    line in the array is its line number minus one as long as the numbers run from 1 without
    a gap.
    """
    record_id = _as_text(result.get(ID_COLUMN))
    assert record_id is not None, f"{where} returned a record with no id"
    for index, line in enumerate(_lines_of(result)):
        line_no = FIRST_LINE_NO + index
        assert _as_int(line.get(_LINE_NO)) == line_no, (
            f"{where} returned the line number {line.get(_LINE_NO)} at position {index}"
        )
        assert _as_text(line.get(ID_COLUMN)) == line_id(record_id, line_no), (
            f"{where} returned the line id {line.get(ID_COLUMN)},"
            f" expected {line_id(record_id, line_no)}"
        )


def _stamp_changed(
    model: Numbering, start: Start, performed: Performed, seq: int, where: str
) -> None:
    """The other bills the operation changed take its sequence number (requirement 4.5).

    A writeback, an automatic close and an automatic unclose change a source bill, which
    the ``writes`` of the call report as an update of a bill table (requirement 17.4); its
    ``updated_seq`` becomes the sequence number of the operation while its ``created_seq``
    stays as it is. The bill the operation generated or deleted is accounted for already,
    and the write of it is checked to be that bill and no other.
    """
    for write in performed.writes:
        if write.entity not in start.domain.bills:
            continue
        key = (write.entity, write.id)
        match write.op:
            case "update":
                assert key in model.bills, (
                    f"{where} changed the bill {write.id}, which the model does not hold"
                )
                model.changed(write.entity, write.id, seq)
            case "insert":
                assert key in model.bills, (
                    f"{where} created the bill {write.id}, which is not the one it returned"
                )
            case "delete":
                assert key not in model.bills, (
                    f"{where} deleted the bill {write.id}, which is not the one it returned"
                )


def _bill_of(start: Start, performed: Performed) -> BillModel:
    """The bill the call works on: its own, or the target bill of a push (req 8.2)."""
    if performed.spec.kind == "push":
        conversion = start.domain.conversions[performed.spec.target]
        return start.domain.bills[conversion.target]
    bill = start.domain.bills.get(performed.spec.target)
    assert bill is not None, (
        f"{performed.tool} writes {performed.spec.target}, which is no bill;"
        " this property follows the id, number and operation counters of bills alone"
    )
    return bill


# --- the sandbox once the sequence is over -----------------------------------------------


def check_state(session: Session, model: Numbering) -> None:
    """The sandbox against the model (requirements 4.4, 4.5, 4.11 and 4.12).

    The counters hold what the operations took and nothing more, so a call that ended with
    an error raised none of them (requirement 4.12), and every bill the sandbox holds
    carries the number and the two sequence numbers the model gives it, so no bill the
    operations never named was stamped (requirement 4.5).
    """
    assert session.counters() == model.counters, "the counters of the sandbox are not the model's"
    for bill in session.start.domain.bills.values():
        held = {str(record[ID_COLUMN]): _stamp_of(record) for record in session.records(bill.name)}
        expected = {
            record_id: stamp
            for (table, record_id), stamp in model.bills.items()
            if table == bill.name
        }
        assert held == expected, f"the {bill.name} bills of the sandbox are not the model's"


def check_reads(session: Session) -> None:
    """A read-only call takes no operation sequence number (requirement 4.11).

    One call of each kind requirement 4.11 names is made on the state the sequence left
    behind, and no counter of the sandbox may move. A declarative query whose parameters
    the profile declares required ends with VALIDATION_FAILED instead, which takes no
    counter either (requirement 4.12), so the calls are not required to go through one by
    one - but at least one of them is, or the check would say nothing.
    """
    before = session.counters()
    performed = [session.perform(call) for call in _read_calls(session)]
    assert any(not step.failed for step in performed), "no read-only call went through"
    assert session.counters() == before, "a read-only call changed a counter of the sandbox"


def _read_calls(session: Session) -> tuple[Call, ...]:
    """One read-only call of each kind of requirement 4.11 the surface exposes.

    The business context of the sandbox, a search, a declarative query and the get of a
    bill the sandbox holds, which a sequence whose bills were all deleted again has none
    of.
    """
    start = session.start
    calls: list[Call] = []
    if BUSINESS_CONTEXT_GET in start.tools:
        calls.append(Call(BUSINESS_CONTEXT_GET, {}))
    for action in ("search", "query"):
        spec = next((found for found in start.tools.values() if found.action == action), None)
        if spec is not None:
            calls.append(Call(spec.name, {}))
    for tools in start.bills:
        if tools.get is None:
            continue
        records = session.records(tools.name)
        if records:
            calls.append(Call(tools.get.name, {ID_PARAM: str(records[0][ID_COLUMN])}))
            break
    return tuple(calls)


def check_replay(session: Session, steps: Sequence[Performed], sandboxes: Path) -> None:
    """The successful calls alone get the same numbering (requirement 4.12).

    The sequence with every failed call left out, run in a second sandbox copied from the
    same snapshot: every call must go through again and return the same id, number and
    sequence numbers it returned in the sequence, and the counters of the two sandboxes
    must end up equal - which is requirement 4.12 itself, that a call ending in an error
    leaves the next successful one exactly the numbering it would have had.
    """
    passed = tuple(step for step in steps if not step.failed)
    with open_sandbox(session.start, sandboxes) as twin:
        again = twin.replay([step.call for step in passed])
        for before, after in zip(passed, again, strict=True):
            assert not after.failed, (
                f"call {before.seq} ({before.tool}) went through in the sequence"
                f" but failed with the failed calls left out: {after.outcome.error}"
            )
            assert _numbering_of(after) == _numbering_of(before), (
                f"call {before.seq} ({before.tool}) was numbered"
                f" {_numbering_of(after)} rather than {_numbering_of(before)}"
            )
        assert twin.counters() == session.counters(), (
            "the counters differ once the failed calls are left out"
        )


def _numbering_of(performed: Performed) -> tuple[object, ...]:
    """What one successful call returned of the numbering (requirements 4.1, 4.2 and 4.5)."""
    result: Mapping[str, JsonValue] = performed.outcome.structured or {}
    return (
        performed.tool,
        result.get(ID_COLUMN),
        result.get(_NUMBER),
        result.get(CREATED_SEQ),
        result.get(UPDATED_SEQ),
        tuple((line.get(ID_COLUMN), line.get(_LINE_NO)) for line in _lines_of(result)),
    )


# --- reading a record and a result -------------------------------------------------------


def _stamp_of(record: Record) -> Stamp:
    """The number and the two sequence numbers of one stored bill (requirement 4.5)."""
    return Stamp(
        number=_as_text(record.get(_NUMBER)),
        created_seq=_as_int(record.get(CREATED_SEQ)),
        updated_seq=_as_int(record.get(UPDATED_SEQ)),
    )


def _lines_of(result: Mapping[str, JsonValue]) -> tuple[Mapping[str, JsonValue], ...]:
    """The ``lines`` of a result, empty for one that carries none (requirement 13.2)."""
    lines = result.get(LINES)
    if not isinstance(lines, Sequence) or isinstance(lines, str):
        return ()
    return tuple(line for line in lines if isinstance(line, Mapping))


def _as_text(value: object) -> str | None:
    """``value`` as the string it is, None for anything else."""
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    """``value`` as the integer it is, None for anything else; a bool is no integer here."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_date(value: object) -> date | None:
    """The ``YYYY-MM-DD`` of a result as the date it is, None for anything else (req 13.2)."""
    text = _as_text(value)
    try:
        return None if text is None else date.fromisoformat(text)
    except ValueError:
        return None


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def start(tmp_path_factory: pytest.TempPathFactory) -> Start:
    """The fixture starting point, built once for the module (requirement 25.2)."""
    return fixture_start(tmp_path_factory.mktemp("p18"))


@pytest.fixture(scope="module")
def sandboxes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandboxes of each example are created below."""
    return tmp_path_factory.mktemp("p18-sandboxes")


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 18: 编号与计数器
@pytest.mark.req("4.1", "4.2", "4.4", "4.5", "4.11", "4.12")
@settings(
    max_examples=EXAMPLES,
    # A sequence of up to 50 operations, run twice, draws a lot and takes its time; neither
    # says anything about the property (the smoke test of the generator suppresses the same
    # two).
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_the_numbering_of_a_sequence_follows_the_counters(
    data: st.DataObject, start: Start, sandboxes: Path
) -> None:
    with open_sandbox(start, sandboxes) as session:
        model = baseline(session)
        steps = walk(data, session)
        note("sequence: " + ", ".join(f"{s.seq} {s.tool}{'!' if s.failed else ''}" for s in steps))
        for performed in steps:
            check_call(model, start, performed)
        check_state(session, model)
        check_reads(session)
        check_replay(session, steps, sandboxes)
