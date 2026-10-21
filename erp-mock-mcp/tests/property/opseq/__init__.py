"""Random operation sequences through the tools layer (requirement 25.2).

Properties 17 to 21 and 27 are all about what the engine does over a *sequence* of
operations: 1 to 50 of create, push, update, delete, submit, unsubmit, approve, unapprove,
close and unclose, drawn against the state the earlier operations left behind and with
about three in ten of them wrong on purpose, so the successful and the failing path of
every operation come up. This package is that sequence, once, for every property that
needs one:

====================================  =====================================================
module                                what it is
====================================  =====================================================
:mod:`~tests.property.opseq.start`    the starting point: a snapshot file plus the loaded
                                      instance, and the write tools of its surface
:mod:`~tests.property.opseq.state`    what the generator reads off the sandbox before it
                                      draws: the bills by state, the referable records and
                                      the quota of each source line
:mod:`~tests.property.opseq.session`  the sandbox of one example and the calls made in it,
                                      each call's outcome and the tables around it
:mod:`~tests.property.opseq.generate` the generator: 70 against 30, sensible against noise
====================================  =====================================================

One example, start to finish::

    import pytest
    from hypothesis import given, settings, strategies as st
    from tests.property.opseq import Start, fixture_start, open_sandbox, walk

    @pytest.fixture(scope="module")
    def start(tmp_path_factory: pytest.TempPathFactory) -> Start:
        return fixture_start(tmp_path_factory.mktemp("p18"))   # one snapshot per module

    @settings(max_examples=100)
    @given(data=st.data())
    def test_a_property(data: st.DataObject, start: Start, tmp_path: Path) -> None:
        with open_sandbox(start, tmp_path) as session:
            for performed in walk(data, session):
                ...                                            # the property goes here

What each property of task 16 reads, all of it off :class:`~tests.property.opseq.session.Performed`
and :class:`~tests.property.opseq.session.Session`:

- **property 18, numbering and counters** (task 16.2): ``session.counters()`` for the four
  counter kinds and ``session.records(<table>)`` for the id, the number and the line number
  of every record; ``performed.outcome.structured`` for what a call returned. The second
  half of the property - the successful calls get the same ids with the failed ones left out
  - is ``session.calls`` filtered down to the successful calls and
  :meth:`~tests.property.opseq.session.Session.replay` of those in a second sandbox from
  the same start.
- **property 19, atomicity and deterministic errors** (task 16.3): open the sandbox with
  ``watch=True`` and compare ``performed.before`` with ``performed.after`` around every
  failed call - both hold every table, the internal ones included. For the error bytes,
  :func:`~tests.property.opseq.session.forks` gives two sandboxes in exactly the state the
  session is in, and the same call in both must return the same ``outcome.text``.
- **property 20, the shape of a successful result** (task 16.4):
  ``performed.outcome.structured`` against ``performed.spec.output_schema``, and the get of
  the same record right after through ``session.call(...)``.
- **property 21, the shape of an error result** (task 16.5): ``performed.outcome.error``,
  with ``outcome.error_code`` and ``outcome.rule_id`` for the two keys the property is
  about.
- **property 27, writes against the net change** (task 16.6): ``performed.writes`` against
  :func:`~tests.property.opseq.session.public_contents` of ``performed.before`` and
  ``performed.after``, which drops the internal columns the property leaves out.

Two starting points are asked for (requirement 25.2). This task provides the first,
:func:`~tests.property.opseq.start.fixture_start`, which writes the standard basic data of
:mod:`tests.helpers.engine_env` out as a snapshot file; task 19.8 adds the
``purchase-basic`` dataset and a test business pack through
:func:`~tests.property.opseq.start.start_from`, which takes any snapshot file and the
instance it belongs to. Nothing but ``fixture_start`` knows the fixture.
"""

from tests.property.opseq.generate import (
    MAX_DRAWN_LINES,
    MAX_OPS,
    MIN_OPS,
    OPERATIONS,
    SENSIBLE,
    Draw,
    Flaw,
    Kind,
    Params,
    Values,
    next_call,
    params_of,
    walk,
)
from tests.property.opseq.session import (
    BlankProbe,
    Call,
    Contents,
    Performed,
    Session,
    Tables,
    forks,
    open_sandbox,
    public_contents,
    read_tables,
)
from tests.property.opseq.start import (
    FIXTURE_START,
    BillTools,
    PushTools,
    Start,
    fixture_start,
    start_from,
)
from tests.property.opseq.state import (
    Held,
    Refs,
    Source,
    SourceLine,
    missing_id,
    read_bills,
    read_refs,
    read_source,
)

__all__ = [
    "FIXTURE_START",
    "MAX_DRAWN_LINES",
    "MAX_OPS",
    "MIN_OPS",
    "OPERATIONS",
    "SENSIBLE",
    "BillTools",
    "BlankProbe",
    "Call",
    "Contents",
    "Draw",
    "Flaw",
    "Held",
    "Kind",
    "Params",
    "Performed",
    "PushTools",
    "Refs",
    "Session",
    "Source",
    "SourceLine",
    "Start",
    "Tables",
    "Values",
    "fixture_start",
    "forks",
    "missing_id",
    "next_call",
    "open_sandbox",
    "params_of",
    "public_contents",
    "read_bills",
    "read_refs",
    "read_source",
    "read_tables",
    "start_from",
    "walk",
]
