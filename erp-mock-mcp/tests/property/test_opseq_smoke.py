"""The random operation sequences of requirement 25.2 reach both paths of every operation.

The generator of :mod:`tests.property.opseq` is what properties 17 to 21 and 27 are drawn
from, so it gets a test of its own before those properties use it: the sequences it draws
must hold successful *and* failed calls - a generator that only ever gets through, or only
ever fails, would let every one of those properties pass without covering anything
(requirement 25.2).

That is a property of the generator over many sequences, not of one sequence: a sequence of
sixteen operations may happen to succeed throughout. The first test therefore draws a run
of sequences with an inner ``@given`` function, counts what the calls of all of them did
and asserts on the counts once the run is over:

- successful and failed calls both came up, which is the 70 against 30 of the generator
  (design.md property 17);
- all five kinds of operation were drawn - a create, a push, an update, a delete and a
  state operation - so no kind is missing from what the generator offers;
- a create and a state operation went through, so the sequences do reach the states the
  later operations need rather than failing from the first call on;
- the failures spread over more than one error code, so the noise is not all input
  validation.

Each example gets a sandbox of its own, copied from the one snapshot the module builds, and
runs a short sequence in it; the counts are per run of the test, which is what the
assertions read. A failed call must carry an error object and no structuredContent and a
successful one the other way around (requirement 13.6), which is checked per call as the
run goes.

The second test is about the ten operations requirement 25.2 names one by one: that the
surface of the starting point exposes every one of them, so the candidate lists the
generator draws from offer all ten. How often each of them comes up is a matter of the
length of the run - an unclose needs a bill that was created, submitted, approved and
closed first - and property 17 draws 500 sequences of up to 50 operations per starting
point for that (design.md); a run of this size is not the place to pin it.
"""

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from erp_mock.tools.generator import PUSH_ACTION
from tests.property.opseq import (
    OPERATIONS,
    Performed,
    Start,
    fixture_start,
    open_sandbox,
    walk,
)

# How many sequences the run draws and how many operations each of them holds: enough calls
# that every kind of operation is drawn and the states the later ones need are reached, and
# few enough that the run stays a smoke test (seconds, not minutes).
SEQUENCES: Final = 20
STEPS: Final = 16

# The six state operations of requirement 7.2, the ones of :data:`OPERATIONS` that move a
# bill from one state to the next.
STATE_ACTIONS: Final[tuple[str, ...]] = (
    "submit",
    "unsubmit",
    "approve",
    "unapprove",
    "close",
    "unclose",
)

# Which kind of operation each action belongs to (tests.property.opseq.generate.Kind); the
# five kinds are what one step of the generator draws between.
KINDS: Final[Mapping[str, str]] = {
    "create": "create",
    PUSH_ACTION: "push",
    "update": "update",
    "delete": "delete",
    **{action: "state" for action in STATE_ACTIONS},
}
EVERY_KIND: Final = frozenset(KINDS.values())


@pytest.fixture(scope="module")
def start(tmp_path_factory: pytest.TempPathFactory) -> Start:
    """The fixture starting point, built once for the module (requirement 25.2)."""
    return fixture_start(tmp_path_factory.mktemp("opseq-smoke"))


@pytest.fixture(scope="module")
def sandboxes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandbox of each example is created below."""
    return tmp_path_factory.mktemp("opseq-smoke-sandboxes")


# Feature: erp-mock-mcp-m0, the generator of the random operation sequences (req 25.2)
@pytest.mark.req("25.2")
def test_the_drawn_sequences_hold_successful_and_failed_calls(
    start: Start, sandboxes: Path
) -> None:
    actions: Counter[str] = Counter()
    passed: Counter[str] = Counter()
    codes: Counter[str] = Counter()

    @settings(
        max_examples=SEQUENCES,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(data=st.data())
    def run_one_sequence(data: st.DataObject) -> None:
        with open_sandbox(start, sandboxes) as session:
            performed = walk(data, session, steps=STEPS)
            assert len(performed) == STEPS, "every step of the sequence had an operation to draw"
            assert session.probe.passed == len(performed), "no call was refused by a limit"
            for call in performed:
                _check_outcome(call)
                actions[call.action] += 1
                if call.failed:
                    codes[str(call.error_code)] += 1
                else:
                    passed[call.action] += 1

    run_one_sequence()

    # Both paths came up, which is what the generator is for (requirement 25.2).
    assert sum(passed.values()) > 0, f"no call went through: {codes}"
    assert sum(codes.values()) > 0, f"no call failed: {passed}"
    # Every kind of operation was drawn, successfully or not (module docstring).
    drawn = {KINDS[action] for action in actions}
    assert drawn == EVERY_KIND, f"kinds never drawn: {EVERY_KIND - drawn}, drawn: {dict(actions)}"
    # The sequences build a state up: a create and a state operation went through.
    assert passed["create"] > 0, f"no create went through: {codes}"
    assert any(passed[action] > 0 for action in STATE_ACTIONS), (
        f"no state operation went through: {dict(passed)}"
    )
    # The noise reaches further than input validation (module docstring).
    assert len(codes) > 1, f"every failure ended with the same error code: {dict(codes)}"


@pytest.mark.req("25.2")
def test_the_starting_point_exposes_every_operation_of_the_sequences(start: Start) -> None:
    offered: set[str] = {action for tools in start.bills for action in tools.states}
    offered |= {tools.create.action for tools in start.bills if tools.create is not None}
    offered |= {tools.update.action for tools in start.bills if tools.update is not None}
    offered |= {tools.delete.action for tools in start.bills if tools.delete is not None}
    offered |= {push.spec.action for push in start.pushes}

    # The ten operations of requirement 25.2, which the generator's candidates are built
    # from: every one of them is a tool of this starting point.
    assert offered >= set(OPERATIONS), f"operations the surface has no tool for: {offered}"


def _check_outcome(call: Performed) -> None:
    """One call of a sequence is either a result or an error, never both (req 13.6)."""
    outcome = call.outcome
    assert outcome.text, f"{call.tool} returned no text"
    if call.failed:
        assert outcome.error is not None and outcome.structured is None, call.tool
        assert outcome.error_code, f"{call.tool} failed with no error code"
        assert outcome.writes == (), f"{call.tool} failed and reported writes"
    else:
        assert outcome.structured is not None and outcome.error is None, call.tool
