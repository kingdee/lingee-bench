"""Property 17: the four invariants hold after every operation of a random sequence.

**Validates: Requirements 4.10, 6.4, 8.11, 9.8, 25.2**

For any sequence of 1 to 50 operations drawn from create, push, update, delete, submit,
unsubmit, approve, unapprove, close and unclose - the operations that failed included - the
sandbox satisfies INV-1, INV-2, INV-3 and INV-6 after every one of them:

- ``INV-1``: the quantity on hand of each (material, warehouse) is the sum of the
  ``qty_delta`` of its stock transactions and is not below 0 (requirement 9.8);
- ``INV-2``: the link field of each source line - of the source header for a push limited
  by amount - is the sum of what the approve of every downstream bill that is approved or
  closed added to it, and no source line of a push limited by quantity is occupied beyond
  its own ``qty_base`` (requirement 8.11);
- ``INV-3``: the three amounts of a bill header are the sums of the amounts of its lines
  (requirement 6.4);
- ``INV-6``: the internal ids, the line ids and the bill numbers are pairwise distinct and
  no id or number that was handed out will be handed out again (requirement 4.10).

The four are decided by :func:`~erp_mock.domain.invariants.check_invariants`, which reads
the records alone and returns one
:class:`~erp_mock.domain.invariants.InvariantViolation` per place that does not hold; the
property is that it returns nothing, whatever the sequence did. Nothing of the engine's
answer to a call takes part in the decision - no ``writes``, no result - so a write that
went in behind the amounts, the quota or the counters is caught by the records themselves.

What the sequences are is :mod:`tests.property.opseq`: every step reads the state the
earlier operations left behind and draws against it, seven draws in ten a sensible
operation and the rest with one deliberate flaw, so the successful and the failing path of
every operation come up (requirement 25.2). The failing ones matter here as much as the
others: a refused call must leave the state it found (requirement 12.7), and an approve
refused halfway - R-GEN-007 over a warehouse that would go below zero - has stock
transactions, writebacks and a state change to roll back, any of which would show up as a
broken invariant on the next check. Which is why the sequence is walked one operation at a
time through :func:`~tests.property.opseq.generate.next_call` rather than through
:func:`~tests.property.opseq.generate.walk`: the check belongs between two operations, not
after the sequence.

The two starting points of requirement 25.2, each a run of its own
(:data:`STARTS`, 500 sequences each):

- ``purchase-basic`` of ``instances/p2p-demo``, taken up by a
  :class:`~erp_mock.loader.loader.Loader` over the real instance directory, so the Snapshot
  the sequences start from is the one a client of that instance gets (requirement 21.2).
  The clock of p2p-demo is in mode today, which configures no anchor date, so the date is
  passed to the loader and to the starting point instead of read from the configuration: a
  test never reads the system clock (requirement 25.10).
- ``fx-basic`` of the engine test fixture, the test business pack requirement 25.2 asks
  for: a ``simple`` state machine bill (``fee_note``) with a push limited by amount that
  writes ``linked_amount`` back onto the source header (``pay_slip``), next to the
  ``standard`` machine, a push limited by quantity with an automatic close, and all four
  inventory directions. Its Snapshot is taken up the same way, by running the loading steps
  over ``tests/fixtures``, so both starting points are what a build produced rather than
  records created by a test: already approved bills, a closed one, a partly received order,
  a half paid fee note and stock in two warehouses, which is a state the operations of one
  sequence would take dozens of calls to reach.

Both are built once per run through :func:`~tests.property.opseq.start.start_from`, and
every example copies a sandbox of its own from the one Snapshot file
(:func:`~tests.property.opseq.session.open_sandbox`). The sandbox does not watch its tables:
this property reads the state after each call, not the difference around it, so the readings
properties 19 and 27 need would be paid for and thrown away.

Reporting a failure. The sequence is kept as it runs, and the assertion message of a broken
invariant holds the violations and then every call of the sequence with its arguments and
its outcome - which is the shrunk sequence, since Hypothesis replays the shrunk example to
produce the message it prints (requirement 25.2). The draws shrink towards a short sequence
of sensible operations: the step count towards 1 and :data:`~tests.property.opseq.generate.SENSIBLE`
towards the sensible branch, so what a failure prints is the short way to it rather than the
50 operations it was found on.

Each call reports its operation and its outcome as a Hypothesis event, so
``--hypothesis-show-statistics`` shows what a run covered. That both paths of every
operation are reached is asserted in :mod:`tests.property.test_opseq_smoke` and not again
here. Which of the ten operations a starting point offers at all is the surface's business
(requirement 11.10): the M0 tool list of p2p-demo leaves out every ``delete`` and the
``unsubmit``, ``unapprove`` and ``unclose`` of its bills (requirement 21.4), so those four
are drawn on the ``fx-basic`` starting point, which exposes all ten - the second reason
requirement 25.2 asks for two.

The cost. Four invariants over every table of the sandbox after every one of 1 to 50
operations, times 500 sequences, times two starting points: the two runs together take a
few minutes, about half of it in the invariants themselves and the rest in the calls and
the state the generator reads before each draw. The sample count is what requirement 25.2
asks for and is not what to turn down; a run that walks fewer operations is a run that
checks fewer states.
"""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from erp_mock.domain.invariants import (
    INV_1,
    INV_2,
    INV_3,
    INV_6,
    INVARIANTS,
    InvariantViolation,
    check_invariants,
)
from erp_mock.loader.instance import LoadedInstance
from erp_mock.loader.loader import Loader
from erp_mock.seed.builder import SNAPSHOT_FILENAME
from tests.helpers.fixtures import ANCHOR_DATE, DATASET, fixture_loaded
from tests.property.opseq import (
    MAX_OPS,
    MIN_OPS,
    Performed,
    Session,
    Start,
    next_call,
    open_sandbox,
    start_from,
)

# How many sequences each starting point gets (design.md, property testing): the 500 of
# requirement 25.2, per starting point rather than over both of them.
SEQUENCES: Final = 500

# How many violations a failure prints (:func:`_violation_lines`); the sequence itself is
# printed whole (:func:`_report`).
REPORTED: Final = 10

# The example instance of the repository and its business packs, which the first starting
# point is the initial dataset of (requirement 21.2).
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
P2P_INSTANCE_DIR: Final = REPO_ROOT / "instances" / "p2p-demo"
P2P_PACKS_DIR: Final = REPO_ROOT / "packs"
P2P_DATASET: Final = "purchase-basic"


# --- the two starting points (requirement 25.2) ------------------------------------------


def purchase_basic_start(workdir: Path) -> Start:
    """The ``purchase-basic`` Snapshot of ``instances/p2p-demo`` as a starting point.

    Every loading step runs over the real instance directory, step 8 included, which builds
    the Snapshot of the dataset below ``workdir`` and checks the four invariants over it
    (requirement 2.14). :data:`ANCHOR_DATE` is handed to the loader because the clock of
    this instance is in mode today (module docstring).

    Raises :class:`~erp_mock.loader.errors.ConfigErrors` where the instance does not
    validate and :class:`~erp_mock.seed.resolve.SeedError` where the dataset cannot be
    built at that date, both of which are tested by :mod:`tests.packs.test_p2p_demo`.
    """
    loaded = Loader(P2P_INSTANCE_DIR, P2P_PACKS_DIR, ANCHOR_DATE, workdir / "data").load()
    return _dataset_start(loaded, P2P_DATASET)


def fixture_basic_start(workdir: Path) -> Start:
    """The ``fx-basic`` Snapshot of the engine test fixture as a starting point.

    The same steps over ``tests/fixtures``, which is the test business pack of requirement
    25.2 (module docstring); its clock is fixed at :data:`ANCHOR_DATE`, the date the loader
    is given here as well, so both starting points run every call on one business date.
    """
    loaded = fixture_loaded(ANCHOR_DATE, workdir / "data")
    return _dataset_start(loaded, DATASET)


def _dataset_start(loaded: LoadedInstance, name: str) -> Start:
    """The starting point over the Snapshot step 8 took up for the dataset ``name``."""
    dataset = loaded.default_dataset
    assert dataset.name == name, f"the default dataset is {dataset.name}, not {name}"
    directory = dataset.snapshot
    assert directory is not None, f"step 8 took up no Snapshot for {name}"
    return start_from(directory / SNAPSHOT_FILENAME, loaded, name=name, anchor_date=ANCHOR_DATE)


# The two starting points by the name they are reported under, which is the name of the
# initial dataset each of them is the Snapshot of (design.md, property 17).
STARTS: Final[Mapping[str, Callable[[Path], Start]]] = {
    P2P_DATASET: purchase_basic_start,
    DATASET: fixture_basic_start,
}


@pytest.fixture(scope="module", params=tuple(STARTS), ids=tuple(STARTS))
def start(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Start:
    """One starting point of :data:`STARTS`, built once for the run it belongs to."""
    name = str(request.param)
    return STARTS[name](tmp_path_factory.mktemp(f"p17-{name}"))


@pytest.fixture(scope="module")
def sandboxes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandbox of each example is created below."""
    return tmp_path_factory.mktemp("p17-sandboxes")


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 17: 操作序列中的不变量
@pytest.mark.req("4.10", "6.4", "8.11", "9.8", "25.2")
@settings(
    max_examples=SEQUENCES,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_the_four_invariants_hold_after_every_operation_of_a_sequence(
    data: st.DataObject, start: Start, sandboxes: Path
) -> None:
    with open_sandbox(start, sandboxes) as session:
        steps = data.draw(st.integers(min_value=MIN_OPS, max_value=MAX_OPS), label="操作个数")
        done: list[Performed] = []
        for _ in range(steps):
            call = next_call(data.draw, session)
            if call is None:
                break
            performed = session.perform(call)
            done.append(performed)
            event(_outcome(performed))

            # INV-1, INV-2, INV-3 and INV-6 over the state this operation left behind,
            # whether it went through or was refused (module docstring).
            violations = _violations(session)
            assert not violations, _report(start, done, violations)

        # Every surface that has a write tool has something to draw at every step, so a
        # sequence that stopped early would be an example that checked fewer states than it
        # was drawn for (:func:`~tests.property.opseq.generate.next_call`).
        assert len(done) == steps, f"{start.name}: drew {steps} operations, performed {len(done)}"


@pytest.mark.req("4.10", "6.4", "8.11", "9.8", "25.2")
def test_the_starting_point_satisfies_the_four_invariants_before_any_operation(
    start: Start, sandboxes: Path
) -> None:
    # The baseline of the property: a sequence starts from a state in which the four hold,
    # so a violation after an operation is that operation's and not the Snapshot's. Loading
    # step 8 checks every Snapshot it takes up (requirement 2.14); this is the same check on
    # the sandbox copied from it, which is the state the first operation of a sequence runs
    # against (requirement 15.1).
    assert tuple(INVARIANTS) == (INV_1, INV_2, INV_3, INV_6), (
        f"the invariants of M0 are {INVARIANTS}, so the property checks something else"
    )
    with open_sandbox(start, sandboxes) as session:
        violations = _violations(session)
    assert not violations, "\n".join(
        [
            f"{start.name} starts in a state the invariants do not hold in:",
            *_violation_lines(violations),
        ]
    )


# --- the check and the report ------------------------------------------------------------


def _violations(session: Session) -> tuple[InvariantViolation, ...]:
    """Every place one of the four invariants does not hold in the sandbox right now.

    One transaction of its own, which reads the records and writes nothing
    (:func:`~erp_mock.domain.invariants.check_invariants`); the call it follows has
    committed whatever it wrote, so this is the state a next call would find.
    """
    with session.store.transaction() as uow:
        return tuple(check_invariants(uow, session.start.domain))


def _report(
    start: Start, done: Sequence[Performed], violations: Sequence[InvariantViolation]
) -> str:
    """The failure message: what did not hold, and the sequence that got the sandbox there.

    The second half is the counterexample of requirement 25.2: every call as it was made,
    with its arguments and its outcome, in order, which is the shrunk sequence by the time
    Hypothesis prints the message (module docstring).
    """
    lines = [
        f"{start.name}: after operation {len(done)} of the sequence,"
        f" {len(violations)} invariant violation(s):",
        *_violation_lines(violations),
        "the sequence, as it was shrunk:",
        # Every call of it, however long it is: the sequence *is* the counterexample, and a
        # shrunk one is short - 50 calls at the very most (requirement 25.2).
        *(f"  {_call_text(performed)}" for performed in done),
    ]
    return "\n".join(lines)


def _violation_lines(violations: Sequence[InvariantViolation]) -> list[str]:
    """The violations a failure prints, one per line: the first :data:`REPORTED` and a count.

    One violation names the invariant, what about it failed, the records involved and the
    two values (:attr:`~erp_mock.domain.invariants.InvariantViolation.text`); a state that
    broke INV-6 can hold as many of them as it has records, and the point of the message is
    the first few.
    """
    lines = [f"  {violation.text}" for violation in violations[:REPORTED]]
    rest = len(violations) - REPORTED
    if rest > 0:
        lines.append(f"  and {rest} more violation(s)")
    return lines


def _call_text(performed: Performed) -> str:
    """One call of the sequence as the report prints it: what it was and what it got."""
    return f"{performed.seq}. {performed.tool} {performed.call.args} -> {_outcome(performed)}"


def _outcome(performed: Performed) -> str:
    """What one call ended with, as a Hypothesis event and as a line of the report."""
    if not performed.failed:
        return f"{performed.action} went through"
    rule = f" ({performed.rule_id})" if performed.rule_id else ""
    return f"{performed.action} failed with {performed.error_code}{rule}"
