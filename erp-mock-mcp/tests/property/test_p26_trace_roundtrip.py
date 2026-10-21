"""Property 26: the round trip of a trace line.

**Validates: Requirements 17.8**

For any trace event - the strings of it free to carry line breaks, quotes, backslashes,
non-ASCII characters, characters beyond the BMP and lone surrogate code points - one
:meth:`~erp_mock.gateway.trace.TraceWriter.write` appends exactly one line to the trace
file; the file is UTF-8 and the line ends with a newline and holds no line break of its
own; and the line parsed as JSON is the event that was written, the nine common fields of
requirement 17.2 followed by the body of the event, the same keys in the same order and
the same values at every level of nesting.

One example writes 1 to 5 events into one file, so what a reader of the file needs is
covered as well: after n writes the file has n whole lines, the nth of them is the nth
event, and no event lands inside another.

What the expected object is compared against the parsed line. The event is built a second
time here, by :func:`expected_event`, rather than read back out of the module under test,
so the key order of requirement 17.2 is written down in this file; and the comparison goes
through :func:`shape`, which turns either side into nested tuples that carry the kind of
each value, so a differing key order, a bool that came back as the integer 1 or an object
that came back as an array is a failure rather than a Python equality that happens to hold.

What the generators draw:

- strings (:data:`STRINGS`) over an alphabet that is every character class requirement 17.8
  names, each written out in :data:`NAMED_CHARS` so it is really drawn: the five line
  terminators ``\\n``, ``\\r``, U+0085, U+2028 and U+2029, the quote, the backslash and the
  solidus, control characters (U+0000, the tab, U+000B, U+001F), a CJK and a Latin-1
  character, U+1D11E (``𝄞``) and U+10FFFF beyond the BMP - plus any other character UTF-8
  can encode
  (``st.characters(codec="utf-8")``, which draws no surrogate) and any single surrogate code
  point of U+D800 to U+DFFF;
- every name of :class:`~erp_mock.gateway.trace.EventName`, with the body its name takes
  (:data:`~erp_mock.gateway.trace.BODY_KEYS`): the body of a tools_call from
  :func:`~erp_mock.gateway.trace.tools_call_body` or
  :func:`~erp_mock.gateway.trace.unknown_tool_body` and the body of a snapshot_restore from
  :func:`~erp_mock.gateway.trace.snapshot_restore_body`, the other four events carrying
  none. The values are drawn freely, so an ``is_error`` false beside an ``error_code`` and
  an empty ``tool`` occur: which combinations a handler may write is requirements 17.3 and
  17.10, not this property;
- ``args`` and ``result`` as arbitrary JSON objects over :data:`JSON_VALUES`, a recursive
  generator of nulls, booleans, integers, floats, strings, arrays and objects whose keys are
  :data:`STRINGS` as well;
- the sandbox of the event (:data:`HEADERS`): ``sandbox_id``, ``instance``, ``profile`` and
  ``dataset`` are :data:`STRINGS` too, since the event repeats them verbatim, and the anchor
  date is any date of the calendar.

Lone surrogates round trip, and a surrogate pair is excluded. A lone surrogate is the one
code point UTF-8 cannot encode, and
:func:`~erp_mock.common.jsonio.encode_utf8` writes it as the JSON escape ``\\udXXX``; the
escape is inside a string literal, so ``json.loads`` reads it back as the same code point
and the round trip holds. Two surrogates directly after one another inside one string are
the exception: JSON spells a character beyond the BMP as exactly that pair of escapes, so a
high surrogate followed by a low one reads back as one supplementary character and not as
the two code points that were written. :func:`separate_surrogate_pairs` therefore puts
U+FFFD between such a pair while the string is being built, which keeps both surrogates in
the string, breaks no other draw and rejects no example. Only that one order is affected: a
low surrogate followed by a high one, and two surrogates in two different strings - which
the ``","`` or ``":"`` between the two literals keeps apart - round trip unchanged.

What else is left out. NaN and the infinities, which JSON cannot spell and
:func:`~erp_mock.common.jsonio.dumps_compact` refuses with ValueError, are not drawn
(:data:`JSON_SCALARS`); every finite float is, since the text json writes for one is
``repr`` and reads back as the same value. The time zone is drawn from :data:`TIMEZONES`
rather than from arbitrary strings: a name that is no IANA zone is not an event the service
could write, and :meth:`~erp_mock.gateway.clock.SystemClock.now_iso_ms` raises on it. The
zone decides the ``ts`` alone and nothing this property is about.

The clock is a source over :data:`INSTANT`, a fixed instant, so no test reads the system
clock (requirement 25.10) and the ``ts`` of every event of an example is the same text,
which :func:`expected_event` is given.

The cost. One example opens a writer and writes at most five short lines, which is well
under a millisecond: the :data:`EXAMPLES` examples of the one test of this module run in
about 0.4 seconds of wall clock on the development machine, the count being the one a
property of this suite declares by default (tests/conftest.py).

Each example writes into a :class:`tempfile.TemporaryDirectory` taken in the test body, the
way :func:`tests.property.test_p31_seed_determinism.build_at` takes one: the trace file
starts out missing, so the writer numbers from 1 and counts the lines of this example
alone, and the directory is gone by the end of the example. A function-scoped ``tmp_path``
fixture would be shared by every example of the test and would trip Hypothesis'
``function_scoped_fixture`` health check.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Final

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from erp_mock.common.jsonio import JsonValue
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.trace import (
    BODY_KEYS,
    TRACE_FILENAME,
    EventName,
    TraceHeader,
    TraceWriter,
    snapshot_restore_body,
    tools_call_body,
    unknown_tool_body,
    write_item,
)

# How many events the property draws, and how many of them one example writes into one file.
EXAMPLES: Final = 100
MOST_EVENTS_PER_FILE: Final = 5

# The instant the clock of every example returns. The microseconds are not a whole
# millisecond, so a ``ts`` that was rounded rather than truncated shows up in the parsed
# line as readily as in the text.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_999, tzinfo=UTC)


# --- the strings of an event -------------------------------------------------------------

# The characters requirement 17.8 names, each written out so that the alphabet really draws
# it: the five line terminators ``str.splitlines()`` and JavaScript break a line at and json
# does not escape by itself (U+0085, U+2028 and U+2029 among them), the two characters JSON
# escapes inside a string, three control characters, a CJK and a Latin-1 character, and two
# characters beyond the BMP.
NAMED_CHARS: Final = (
    "\n",
    "\r",
    "\x85",
    "\u2028",
    "\u2029",
    '"',
    "\\",
    "/",
    "\t",
    "\x00",
    "\x0b",
    "\x1f",
    "结",
    "é",
    "\U0001d11e",
    "\U0010ffff",
)

# The surrogate code points, the only ones UTF-8 cannot encode, which a client argument can
# carry into an event through a JSON escape such as ``"\ud800"``.
FIRST_SURROGATE: Final = 0xD800
LAST_SURROGATE: Final = 0xDFFF
HIGH_SURROGATES: Final = range(FIRST_SURROGATE, 0xDC00)
LOW_SURROGATES: Final = range(0xDC00, LAST_SURROGATE + 1)

# What :func:`separate_surrogate_pairs` puts between a high surrogate and a low one that
# would otherwise be adjacent; any character that is no surrogate would do.
PAIR_BREAKER: Final = "\ufffd"

# How long a drawn string may get. Short strings keep an example cheap and shrink to a
# readable counterexample; what the property is about is which characters a string holds.
LONGEST_STRING: Final = 10


def separate_surrogate_pairs(chars: Sequence[str]) -> str:
    """Join ``chars`` into a string in which no low surrogate follows a high one directly.

    JSON spells a character beyond the BMP as a high surrogate escape followed by a low one,
    so such a pair would read back as one supplementary character rather than as the two
    code points that were written (module docstring). A :data:`PAIR_BREAKER` is put between
    the two, which keeps both surrogates in the string and drops no draw.
    """
    out: list[str] = []
    for char in chars:
        if out and ord(out[-1]) in HIGH_SURROGATES and ord(char) in LOW_SURROGATES:
            out.append(PAIR_BREAKER)
        out.append(char)
    return "".join(out)


# One character of a drawn string: one of the classes requirement 17.8 names, any other
# character UTF-8 can encode, or any single surrogate code point.
CHARS: Final = st.one_of(
    st.sampled_from(NAMED_CHARS),
    st.characters(codec="utf-8"),
    st.integers(min_value=FIRST_SURROGATE, max_value=LAST_SURROGATE).map(chr),
)

# Any string of an event, the empty one included.
STRINGS: Final = st.lists(CHARS, max_size=LONGEST_STRING).map(separate_surrogate_pairs)


# --- the values of an event --------------------------------------------------------------

# A JSON value that is not a container. NaN and the infinities are left out: JSON cannot
# spell them and ``dumps_compact`` refuses them (module docstring).
JSON_SCALARS: Final = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    STRINGS,
)

# How wide and how deep a drawn JSON value gets.
MOST_ITEMS: Final = 3
MOST_LEAVES: Final = 6

# Any JSON value: a scalar, or an array or an object of them, the keys of an object drawn
# from the same alphabet as any other string.
JSON_VALUES: Final[st.SearchStrategy[JsonValue]] = st.recursive(
    JSON_SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=MOST_ITEMS),
        st.dictionaries(STRINGS, children, max_size=MOST_ITEMS),
    ),
    max_leaves=MOST_LEAVES,
)

# The ``args`` and the ``result`` of a tools_call, which are JSON objects; ``args`` is None
# for a request that carried no ``arguments`` and ``result`` None for a call that produced
# nothing (requirement 17.3).
JSON_OBJECTS: Final = st.dictionaries(STRINGS, JSON_VALUES, max_size=MOST_ITEMS)
OPTIONAL_OBJECTS: Final = st.none() | JSON_OBJECTS

# The remaining values of a tools_call body (requirement 17.3): whole milliseconds, the
# parameter paths of the lenient conversion, and one item per changed (record, cause).
LATENCIES: Final = st.integers(min_value=0, max_value=10**6)
COERCED: Final = st.lists(STRINGS, max_size=MOST_ITEMS)
WRITES: Final = st.lists(
    st.builds(write_item, entity=STRINGS, op=STRINGS, id=STRINGS, cause=STRINGS),
    max_size=MOST_ITEMS,
)


# --- the sandbox an event belongs to -----------------------------------------------------

# The time zone the ``ts`` of an event is read in: an IANA name or the local zone of the
# process (module docstring).
TIMEZONES: Final[tuple[str | None, ...]] = (None, "UTC", "Asia/Shanghai", "America/New_York")

# The sandbox of an example. Every string of it is repeated verbatim by each event, so all
# four are drawn from the same alphabet as any other string.
HEADERS: Final = st.builds(
    TraceHeader,
    sandbox_id=STRINGS,
    instance=STRINGS,
    profile=STRINGS,
    dataset=STRINGS,
    anchor_date=st.dates(),
    timezone=st.sampled_from(TIMEZONES),
)


# --- one event ---------------------------------------------------------------------------

# The body of an event that has none, which is four of the six event names (requirement
# 17.9); read-only, so the one object every such event is built with cannot be changed.
NO_BODY: Final[Mapping[str, JsonValue]] = MappingProxyType({})

# The event names whose body is empty, read off the module rather than written out again.
BODYLESS_EVENTS: Final = tuple(name for name, keys in BODY_KEYS.items() if not keys)


@dataclass(frozen=True, slots=True)
class Event:
    """One trace event as :meth:`~erp_mock.gateway.trace.TraceWriter.write` takes it.

    ``name`` is the event name, ``session_id`` the session that triggered it - None for an
    event of the Admin API (requirement 17.2) - and ``body`` what follows the common fields,
    whose keys are the ones :data:`~erp_mock.gateway.trace.BODY_KEYS` has for ``name``.
    """

    name: EventName
    session_id: str | None
    body: Mapping[str, JsonValue]


# The session that triggered an event, None for one the Admin API triggered.
SESSIONS: Final = st.none() | STRINGS

# The body of a tools_call: one of a call that was served, with every value drawn freely,
# and one of a tool name that is not in the session's profile (requirement 17.10).
TOOLS_CALL_BODIES: Final = st.one_of(
    st.builds(
        tools_call_body,
        tool=STRINGS,
        args=OPTIONAL_OBJECTS,
        result=OPTIONAL_OBJECTS,
        is_error=st.booleans(),
        latency_ms=LATENCIES,
        coerced=COERCED,
        error_code=st.none() | STRINGS,
        rule_id=st.none() | STRINGS,
        writes=WRITES,
        known_tool=st.booleans(),
    ),
    st.builds(
        unknown_tool_body,
        tool=STRINGS,
        args=OPTIONAL_OBJECTS,
        result=JSON_OBJECTS,
        latency_ms=LATENCIES,
    ),
)

# Any event: a name whose body is empty, a tools_call, or a snapshot_restore that names a
# Named_Snapshot.
EVENTS: Final = st.one_of(
    st.builds(
        Event,
        name=st.sampled_from(BODYLESS_EVENTS),
        session_id=SESSIONS,
        body=st.just(NO_BODY),
    ),
    st.builds(
        Event,
        name=st.just(EventName.TOOLS_CALL),
        session_id=SESSIONS,
        body=TOOLS_CALL_BODIES,
    ),
    st.builds(
        Event,
        name=st.just(EventName.SNAPSHOT_RESTORE),
        session_id=SESSIONS,
        body=st.builds(snapshot_restore_body, STRINGS),
    ),
)


def expected_event(
    header: TraceHeader, timestamp: str, seq: int, drawn: Event
) -> Mapping[str, JsonValue]:
    """The object the line of ``drawn`` has to parse back to.

    The nine common fields in the order of requirement 17.2, followed by the body of the
    event. Written out here rather than read back out of the module under test, so a change
    of that order fails this property as well.
    """
    return {
        "seq": seq,
        "ts": timestamp,
        "sandbox_id": header.sandbox_id,
        "session_id": drawn.session_id,
        "instance": header.instance,
        "profile": header.profile,
        "dataset": header.dataset,
        "anchor_date": header.anchor_date.isoformat(),
        "event": drawn.name.value,
        **drawn.body,
    }


# --- what the file holds -----------------------------------------------------------------

_NOT_UTF8: Final = "trace 文件不是 UTF-8 文本：{reason}"
_NO_FINAL_NEWLINE: Final = "trace 文件的最后一个事件没有以换行符结尾：{tail!r}"


def lines_of(path: Path) -> list[str]:
    """The whole lines of the trace file at ``path``, each without its newline.

    The bytes are decoded as UTF-8 strictly, so a file that is not UTF-8 fails here
    (requirement 17.8), and the text has to end with a newline, so a line is one whole
    event. Splitting on ``"\\n"`` alone is on purpose: whether a line holds another line
    terminator is asserted by the property, with ``str.splitlines()``.
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AssertionError(_NOT_UTF8.format(reason=error)) from error
    if text == "":
        return []
    assert text.endswith("\n"), _NO_FINAL_NEWLINE.format(tail=text[-20:])
    return text[:-1].split("\n")


# --- comparing a parsed line with the event ----------------------------------------------

# One value of a JSON document as :func:`shape` writes it: the kind of the value and the
# value itself, containers turned into tuples of the same.
type Shape = tuple[str, object]

_NOT_JSON = "第 {seq} 个事件不是 JSON 值：{reason}\n  行：{line!r}"
_DIFFERENT = (
    "第 {seq} 个事件按 JSON 解析后与写入的事件不同\n"
    "  行：{line!r}\n"
    "  写入的事件：{event!r}\n"
    "  解析的结果：{parsed!r}"
)
_NO_JSON_VALUE: Final = "{value!r} 不是 JSON 值，无法参与比较"


def shape(value: object) -> Shape:
    """``value`` as nested tuples that carry the kind of each value.

    Comparing two shapes compares the values, the kind of each one and the key order of
    every object, which plain Python equality does not: ``True == 1`` and ``{"a": 1}`` with
    its keys the other way round would both pass as equal. Arrays are tuples and objects
    tuples of (key, shape) pairs, so a tuple a caller passed for an array and a read-only
    mapping it passed for an object compare as the list and the dict they are written as.

    Raises TypeError for a value that is no JSON value, which no parsed line and no drawn
    event holds.
    """
    if value is None:
        return "null", None
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, int):
        return "int", value
    if isinstance(value, float):
        return "float", value
    if isinstance(value, str):
        return "str", value
    if isinstance(value, Mapping):
        return "object", tuple((key, shape(item)) for key, item in value.items())
    if isinstance(value, Sequence):
        return "array", tuple(shape(item) for item in value)
    raise TypeError(_NO_JSON_VALUE.format(value=value))


def assert_parses_back(line: str, seq: int, written: Mapping[str, JsonValue]) -> None:
    """Assert that ``line`` parses as JSON and is the event ``written`` (requirement 17.8).

    ``seq`` is the number of the event in the file, which the message of a failure names.
    The parse keeps the order the line lists the keys in - ``object_pairs_hook`` is not
    needed, since a dict keeps its insertion order and :func:`shape` reads it off - so a
    key in another place is a failure.
    """
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as error:
        raise AssertionError(_NOT_JSON.format(seq=seq, reason=error, line=line)) from error
    assert shape(parsed) == shape(written), _DIFFERENT.format(
        seq=seq, line=line, event=dict(written), parsed=parsed
    )


# --- the property ------------------------------------------------------------------------

# One event whose strings hold every character class requirement 17.8 names at once, so the
# classes are covered at any example count and a failure of one of them is reported on a
# line that is readable. The lone surrogates are the last two characters, kept apart by the
# low one coming first: a high surrogate followed by a low one is a pair (module docstring).
HAIRY: Final = 'one\ntwo\r\nthree\u2028four\u2029five\x85six "七" \\ 结束 \U0001d11e \udfff\ud800'
HAIRY_HEADER: Final = TraceHeader(
    sandbox_id=HAIRY,
    instance=HAIRY,
    profile=HAIRY,
    dataset=HAIRY,
    anchor_date=date(2026, 10, 9),
    timezone="Asia/Shanghai",
)
HAIRY_EVENTS: Final = [
    Event(
        name=EventName.TOOLS_CALL,
        session_id=HAIRY,
        body=tools_call_body(
            tool=HAIRY,
            args={HAIRY: [HAIRY, {HAIRY: None}, 0.5]},
            result={HAIRY: HAIRY},
            is_error=True,
            latency_ms=0,
            coerced=[HAIRY],
            error_code=HAIRY,
            rule_id=HAIRY,
            writes=[write_item(entity=HAIRY, op=HAIRY, id=HAIRY, cause=HAIRY)],
        ),
    ),
    Event(name=EventName.SNAPSHOT_RESTORE, session_id=None, body=snapshot_restore_body(HAIRY)),
]


# Feature: erp-mock-mcp-m0, Property 26: trace 行的往返
@pytest.mark.req("17.8")
@settings(max_examples=EXAMPLES)
@given(
    header=HEADERS,
    events=st.lists(EVENTS, min_size=1, max_size=MOST_EVENTS_PER_FILE),
)
@example(header=HAIRY_HEADER, events=HAIRY_EVENTS)
def test_every_event_is_one_line_that_parses_back_to_the_event(
    header: TraceHeader, events: Sequence[Event]
) -> None:
    clock = SystemClock(now=lambda: INSTANT)
    # The one ``ts`` every event of this example carries: the source is a fixed instant, so
    # no system clock is read (requirement 25.10) and the expected text is known here.
    timestamp = clock.now_iso_ms(header.timezone)

    with TemporaryDirectory() as sandbox_dir:
        path = Path(sandbox_dir) / TRACE_FILENAME
        writer = TraceWriter(path, header, clock)
        try:
            for written_so_far, drawn in enumerate(events, start=1):
                assert writer.write(drawn.name, drawn.session_id, drawn.body) == written_so_far
                # One write appends exactly one line, whatever the strings of the event
                # hold: the file has one line per event written so far.
                assert len(lines_of(path)) == written_so_far, (
                    f"写入第 {written_so_far} 个事件后 trace 文件有"
                    f" {len(lines_of(path))} 行，不是 {written_so_far} 行"
                )
        finally:
            writer.close()

        lines = lines_of(path)

    # Each line is one whole event: it carries no line terminator of its own, so a reader
    # that splits the file into lines gets the events back one by one, and parsing it gives
    # the event that was written (requirement 17.8).
    for seq, (line, drawn) in enumerate(zip(lines, events, strict=True), start=1):
        assert line.splitlines() == [line], f"第 {seq} 个事件的行内出现了行分隔符：{line!r}"
        assert_parses_back(line, seq, expected_event(header, timestamp, seq, drawn))
