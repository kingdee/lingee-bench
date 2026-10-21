"""Property 19: a failed call changes nothing and its error is always the same.

**Validates: Requirements 7.10, 9.4, 12.7, 12.9**

For any sandbox state s and any tool name with arguments a that make the call fail, the
contents of every table of the sandbox, the internal ones included, are the same after the
call as before it; and in two sandboxes both in state s, a returns the same error result
byte for byte, the order of the details included (requirements 12.7 and 12.9).

Both halves are read off one random operation sequence per example
(:mod:`tests.property.opseq`): 1 to 50 operations drawn against the state the earlier ones
left behind, about three in ten of them wrong on purpose, which is where the failed calls
come from. Every failed call of the sequence is a state s and an a - what the engine was
asked to do, and the state it was in when it refused - so the sequence is not a setup for
the property but the supply of its cases:

- the first half, for every failed call: ``performed.before`` against ``performed.after``,
  the sandbox opened with ``watch=True`` so each call records every table around itself.
  Both readings hold every table of the schema - ``_counters``, ``_effective_writes``,
  ``_writeback_ledger``, ``stock_txn`` and ``stock_balance`` as much as the bills and the
  basic data - and the comparison goes record by record and column by column
  (:func:`_changes`). :func:`_check_every_table_is_watched` asserts that the readings
  really do hold the five tables a rolled back write would leak into first, so the half
  cannot pass by comparing a shrinking set of tables.
- the second half, for every failed call: :func:`~tests.property.opseq.session.forks`
  writes the state the session is in out to a snapshot file and copies two sandboxes from
  that one file, so the two start out identical down to the counters; the same
  :class:`~tests.property.opseq.session.Call` runs in both and the two results are compared
  as UTF-8 bytes (:func:`_check_same_error`). Every failure of every sequence is compared,
  the shallow ones of input validation and the ones a sequence has to build a state up to
  reach alike - the overdrawn source lines of R-GEN-005 and the warehouses of R-GEN-007,
  whose details are what the order in requirement 12.9 is about.

The second half builds on the first. The twins are copied right after the failed call, out
of a sandbox the call left in the state it was refused in - which is the first half, and
which is why the sequence here is walked one operation at a time rather than through
:func:`~tests.property.opseq.generate.walk`: the fork has to happen while the sandbox is
still in that state. The tables of each twin are then read before its call runs and compared
with the session's (:func:`_check_forked_in_state`), so a fork that held some other state
cannot let the comparison pass.

The session's own result is compared with the twins' as well, which makes the comparison
transitive over all three. It was produced in the same state by the same arguments and
differs in one thing only: the call sequence number, which is 1 in a fresh twin and the
length of the sequence so far in the session
(:meth:`~tests.property.opseq.session.Session.context`). That number reaches the records of
an effective call and nothing else (``Engine.run``), so a failed call, which has no
effective writes, must return the same bytes whatever the number was - and the comparison
is where that shows.

Which errors come up is the generator's business, not this module's: the deliberate flaws
reach VALIDATION_FAILED, NOT_FOUND and INVALID_STATE, and the sensible calls that break a
rule reach BUSINESS_RULE_VIOLATION - R-GEN-005 for a push over the quota of its source line
and R-GEN-007 for an approve that would take a warehouse below zero, which is the rollback
of requirement 9.4 and the one requirement 7.10 is about, a whole approve with its linkage
and its writebacks undone. Each failed call reports its error code and rule as a Hypothesis
event, so ``--hypothesis-show-statistics`` shows what a run covered; that the generator
reaches both paths of every operation is asserted in :mod:`tests.property.test_opseq_smoke`
and not again here.

Two error codes requirement 12.9 cannot be shown with here. LIMIT_EXCEEDED over the call
rate is the exception the requirement itself makes, and the sequences are let through by
:class:`~tests.property.opseq.session.BlankProbe` anyway; INTERNAL_ERROR is a defect of the
framework, which a passing suite has none of.
"""

from collections.abc import Mapping, Sequence
from itertools import zip_longest
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, event, given, note, settings
from hypothesis import strategies as st

from erp_mock.state.schema import (
    COUNTERS_TABLE,
    EFFECTIVE_WRITES_TABLE,
    STOCK_BALANCE_TABLE,
    STOCK_TXN_TABLE,
    WRITEBACK_LEDGER_TABLE,
)
from erp_mock.state.store import Record, Value
from tests.property.opseq import (
    MAX_OPS,
    MIN_OPS,
    Performed,
    Session,
    Start,
    Tables,
    fixture_start,
    forks,
    next_call,
    open_sandbox,
)

# How many sandboxes in one state a failed call is compared in: the two of requirement 12.9.
TWINS: Final = 2

# How many differences a failing comparison prints (:func:`_report`).
REPORTED: Final = 10

# The tables a rolled back write would leak into first, which every reading around a call
# must hold: the three internal ones (requirements 4.6 and 18.10, design.md 4.2) and the two
# an inventory linkage writes (requirement 9.1).
WATCHED: Final[frozenset[str]] = frozenset(
    {
        COUNTERS_TABLE,
        EFFECTIVE_WRITES_TABLE,
        WRITEBACK_LEDGER_TABLE,
        STOCK_TXN_TABLE,
        STOCK_BALANCE_TABLE,
    }
)


@pytest.fixture(scope="module")
def start(tmp_path_factory: pytest.TempPathFactory) -> Start:
    """The fixture starting point, built once for the module (requirement 25.2)."""
    return fixture_start(tmp_path_factory.mktemp("p19"))


@pytest.fixture(scope="module")
def sandboxes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandbox of each example and its forks are created below."""
    return tmp_path_factory.mktemp("p19-sandboxes")


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 19: 失败调用的原子性与错误确定性
@pytest.mark.req("7.10", "9.4", "12.7", "12.9")
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_a_failed_call_changes_nothing_and_returns_the_same_error_in_two_sandboxes(
    data: st.DataObject, start: Start, sandboxes: Path
) -> None:
    with open_sandbox(start, sandboxes, watch=True) as session:
        steps = data.draw(st.integers(min_value=MIN_OPS, max_value=MAX_OPS), label="操作个数")
        for _ in range(steps):
            call = next_call(data.draw, session)
            if call is None:
                break
            performed = session.perform(call)
            if not performed.failed:
                continue
            rule = f" ({performed.rule_id})" if performed.rule_id else ""
            event(f"{performed.error_code}{rule} from a {performed.action}")
            note(f"call {performed.seq}: {performed.tool} {performed.call.args}")

            # The first half: the call left every table of the sandbox as it found it,
            # the counters and the stock of an approve included (requirements 7.10 and 12.7).
            _check_unchanged(performed)

            # The second half: the same call in two sandboxes in the state it was refused
            # in - the state it left behind - returns the same error (requirement 12.9).
            with forks(session, sandboxes, count=TWINS) as twins:
                for twin in twins:
                    _check_forked_in_state(performed, twin)
                again = tuple(twin.perform(performed.call) for twin in twins)
            _check_same_error((performed, *again))


# --- the first half: nothing changed (requirements 7.10, 9.4 and 12.7) -------------------


def _check_unchanged(performed: Performed) -> None:
    """Assert that the failed ``performed`` left every table of the sandbox as it was."""
    before, after = performed.before, performed.after
    assert before is not None and after is not None, "the sandbox is not watching its tables"
    _check_every_table_is_watched(before)
    _check_every_table_is_watched(after)
    changes = _changes(before, after)
    assert not changes, (
        f"{performed.tool} failed with {performed.error_code}"
        f" and changed the sandbox: {_report(changes)}"
    )
    # A failed call reports no writes either, which is the same statement in the trace
    # (requirement 17.5); a difference between the two would be a hole in this property.
    assert performed.writes == (), f"{performed.tool} failed and reported {performed.writes}"


def _check_forked_in_state(performed: Performed, twin: Session) -> None:
    """Assert that ``twin`` starts in the state the failed ``performed`` left behind.

    The premise of the second half: the two sandboxes the error is compared in are in the
    state s the call was refused in, which is the state the call left behind. A fork that
    held the state the sequence started from, or an empty database, would answer the same
    in both of its sandboxes and say nothing about s.
    """
    assert performed.after is not None, "the sandbox is not watching its tables"
    changes = _changes(performed.after, twin.tables())
    assert not changes, (
        f"the fork of {performed.tool} did not start in the state of the session:"
        f" {_report(changes)}"
    )


def _check_every_table_is_watched(tables: Tables) -> None:
    """Assert that ``tables`` holds the internal and the stock tables (module docstring)."""
    missing = WATCHED - tables.keys()
    assert not missing, f"tables left out of the comparison: {sorted(missing)}"


def _changes(before: Tables, after: Tables) -> list[str]:
    """Every difference between two readings of the tables of a sandbox, as report lines.

    An empty list is what the first half of the property asserts: the two readings hold the
    same tables, each with the same records in the same order - :func:`read_tables
    <tests.property.opseq.session.read_tables>` reads them in primary key order - and every
    record the same columns with the same values.
    """
    lines = [
        f"{name}: table only read {'before' if name in before else 'after'} the call"
        for name in sorted(before.keys() ^ after.keys())
    ]
    for name in sorted(before.keys() & after.keys()):
        lines.extend(_record_changes(name, before[name], after[name]))
    return lines


def _record_changes(name: str, before: Sequence[Record], after: Sequence[Record]) -> list[str]:
    """Every difference between the records of the table ``name``, as report lines."""
    lines: list[str] = []
    for index, (one, other) in enumerate(zip_longest(before, after)):
        if one == other:
            continue
        if one is None:
            lines.append(f"{name}: record {index} appeared: {_shown(other)}")
        elif other is None:
            lines.append(f"{name}: record {index} disappeared: {_shown(one)}")
        else:
            lines.append(f"{name}: record {index} changed: {_columns(one, other)}")
    return lines


def _columns(one: Record, other: Record) -> dict[str, tuple[Value, Value]]:
    """The columns ``one`` and ``other`` differ in, each with the two values."""
    return {
        column: (one.get(column), other.get(column))
        for column in sorted(one.keys() | other.keys())
        if one.get(column) != other.get(column)
    }


def _shown(record: Record | None) -> str:
    """One record as a failure reports it."""
    return "none" if record is None else repr(dict(record))


def _report(changes: Sequence[str]) -> str:
    """The differences a failure prints: the first :data:`REPORTED` of them and a count.

    One inserted record shifts every record of its table behind it, since the comparison
    goes by position (:func:`_record_changes`), so a single write can produce as many lines
    as the table has records; the point of the message is the first few.
    """
    rest = len(changes) - REPORTED
    shown = "; ".join(changes[:REPORTED])
    return shown if rest <= 0 else f"{shown}; and {rest} more"


# --- the second half: the same error, byte for byte (requirement 12.9) -------------------


def _check_same_error(runs: Sequence[Performed]) -> None:
    """Assert that the same call in the same state produced the same error in every run.

    ``runs`` is the call as the session made it followed by the same call in each of the
    twins :func:`~tests.property.opseq.session.forks` copied out of that state; comparing
    every twin with the session's run compares the twins with each other too. The text is
    compared as the bytes the one text item of the content carries (requirement 13.1) and
    the error object as its keys in order and then as its items, so a difference in the
    order of the details is reported as the order and not as a wall of JSON.
    """
    first, *rest = runs
    for other in rest:
        where = f"call {first.seq}, {first.tool},"
        assert other.failed, f"{where} failed in the session and went through in a fork"
        assert other.outcome.structured is None, f"{where} returned structuredContent in a fork"
        one, two = first.outcome.error, other.outcome.error
        assert one is not None and two is not None, f"{where} returned no error object"
        assert _keys(one) == _keys(two), (
            f"{where} returned the error keys in another order: {_keys(one)} against {_keys(two)}"
        )
        assert one == two, f"{where} returned another error: {one} against {two}"
        assert first.outcome.text.encode() == other.outcome.text.encode(), (
            f"{where} returned another text: {first.outcome.text!r} against {other.outcome.text!r}"
        )


def _keys(error: Mapping[str, object]) -> tuple[str, ...]:
    """The keys of an error object in the order it holds them (requirement 13.6)."""
    return tuple(error)
