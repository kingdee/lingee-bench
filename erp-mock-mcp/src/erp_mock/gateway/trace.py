"""The trace of one sandbox: one JSON event per line of its trace.jsonl (req 17.1).

Everything an upper layer can learn about what happened in a sandbox without reading the
client's log goes through this module (design.md 7.6). The Sandbox_Manager creates one
:class:`TraceWriter` per sandbox when the sandbox is created and closes it when the
sandbox is deleted; the session handlers, the stdio command and the Admin API then write
one event per request they serve (requirement 17.9), and the Admin trace endpoint and the
export read the file back.

What this module owns, and what it leaves to its callers:

- it owns the shape of an event - which keys there are and in which order - so that the
  corresponding events of two sandboxes that ran the same requests list the same keys in
  the same order (requirement 24.3). Callers hand over values, never a key order: the
  common fields come from the :class:`TraceHeader` of the sandbox, the ``seq`` and the
  ``ts`` from this module, and an event body from :func:`tools_call_body`,
  :func:`unknown_tool_body` or :func:`snapshot_restore_body`;
- it owns the bytes of a line: :func:`~erp_mock.common.jsonio.dumps_compact` and
  :func:`~erp_mock.common.jsonio.encode_utf8` make one event exactly one line of UTF-8,
  whatever its strings contain (requirement 17.8);
- it leaves the timing of a write to the caller, which writes the event before it answers
  the request it belongs to (requirement 17.6). No ``fsync`` is called: the bytes are in
  the file once ``os.write`` returns, so a reader - another process included - that reads
  the file after the response sees the event;
- it leaves serialization to the caller. The writer has no lock of its own, and the
  Sandbox_Manager's lock around a request is what keeps two events of one sandbox from
  interleaving (requirement 15.4, design.md 7.5).

``seq`` and the sandbox lock. A tools/call needs its ``seq`` twice: the records it writes
are recorded under that number inside the database (requirement 18.10), and the event
carries it. The caller therefore reads :attr:`TraceWriter.next_seq` when the call starts,
passes it to the tools layer as the ``call_seq`` of the call, and writes the event when the
call ends; :meth:`TraceWriter.write` hands out exactly that number and returns it. For the
two to agree, no other event of this sandbox may be written in between, which is the
caller's responsibility and holds because every request on a sandbox runs under its lock.

seq across a reopen (requirement 17.2). The number is not kept anywhere but in the file:
``seq`` starts at 1 and rises by 1 per event, and an event is exactly one line, so the
number of complete lines already in the file is the last ``seq`` written, and the next one
is that plus one. Opening a writer on a trace file that already has events therefore
continues the numbering, and a reset, a restore and a Named_Snapshot - all of which only
replace the content of the database files, never this one (requirement 17.7) - keep it
continuous without doing anything at all. The alternative, keeping the counter in the
sandbox database, would lose it on every reset.

When a write fails. The database transaction of a call is committed before its event is
written, so a failed write would leave the trace behind the content of the database for
good. :meth:`TraceWriter.write` raises :class:`TraceWriteError`, and the caller - the
Sandbox_Manager - turns the sandbox frozen, records the error and answers that one request
with JSON-RPC -32603 (design.md error handling). The message of the exception names the
file and belongs in the service log; what the client gets is the preset INTERNAL_ERROR
message, which carries no path (requirement 13.10).
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

from erp_mock.common.jsonio import JsonValue, dumps_compact, encode_utf8
from erp_mock.gateway.clock import SystemClock

__all__ = [
    "BODY_KEYS",
    "COMMON_KEYS",
    "TOOLS_CALL_KEYS",
    "TRACE_FILENAME",
    "WRITE_KEYS",
    "EventName",
    "TraceHeader",
    "TraceWriteError",
    "TraceWriter",
    "snapshot_restore_body",
    "tools_call_body",
    "unknown_tool_body",
    "write_item",
]

# The trace file of a sandbox, in the sandbox directory and outside its database files
# (design.md data model 2, requirement 17.7).
TRACE_FILENAME: Final = "trace.jsonl"

# Append-only, created where it is not there yet; O_APPEND puts every write at the end of
# the file whatever else has been written to it, so a line never lands inside another.
_OPEN_FLAGS: Final = os.O_WRONLY | os.O_APPEND | os.O_CREAT
_FILE_MODE: Final = 0o644

# How much of an existing trace file is read at a time while its lines are counted.
_READ_CHUNK: Final = 1 << 20

_CLOSED: Final = "trace 文件 {path} 的写入器已关闭，无法再写入事件"
_WRITE_FAILED: Final = "无法写入 trace 文件 {path} 的第 {seq} 个事件：{reason}"
_WRONG_BODY: Final = "{event} 事件体的键必须依次为 {expected}，收到的是 {found}"


class EventName(StrEnum):
    """The ``event`` of a trace event; each value equals the member name in lower case.

    The six of requirement 17.1, in the order it lists them, and nothing else: what
    writes each one is requirement 17.9 - an accepted initialize writes ``session_start``,
    tools/list writes ``tools_list``, tools/call writes ``tools_call``, the Admin API's
    reset and restore write ``sandbox_reset`` and ``snapshot_restore``, and the end of a
    session writes ``session_end``.

    A StrEnum member is a str, so it serializes as its value in JSON.
    """

    SESSION_START = "session_start"
    TOOLS_LIST = "tools_list"
    TOOLS_CALL = "tools_call"
    SANDBOX_RESET = "sandbox_reset"
    SNAPSHOT_RESTORE = "snapshot_restore"
    SESSION_END = "session_end"


# The keys every event starts with, in the order of requirement 17.2.
COMMON_KEYS: Final = (
    "seq",
    "ts",
    "sandbox_id",
    "session_id",
    "instance",
    "profile",
    "dataset",
    "anchor_date",
    "event",
)

# The keys a tools_call event carries after the common ones, in the order of
# requirement 17.3 (design.md data model 4).
TOOLS_CALL_KEYS: Final = (
    "tool",
    "args",
    "coerced",
    "known_tool",
    "is_error",
    "error_code",
    "rule_id",
    "result",
    "writes",
    "flags",
    "latency_ms",
)

# The keys of one item of a tools_call event's ``writes``, in the order of
# requirement 17.4.
WRITE_KEYS: Final = ("entity", "op", "id", "cause")

# The one key a snapshot_restore event carries after the common ones (design.md 7.6).
_RESTORE_KEYS: Final = ("name",)

# The keys of the body of each event, which :meth:`TraceWriter.write` checks a body
# against: a tools_call and a snapshot_restore carry one, every other event is the common
# fields alone (design.md 7.6).
BODY_KEYS: Final[Mapping[EventName, tuple[str, ...]]] = MappingProxyType(
    {
        EventName.SESSION_START: (),
        EventName.TOOLS_LIST: (),
        EventName.TOOLS_CALL: TOOLS_CALL_KEYS,
        EventName.SANDBOX_RESET: (),
        EventName.SNAPSHOT_RESTORE: _RESTORE_KEYS,
        EventName.SESSION_END: (),
    }
)

# The body of an event that has none; a read-only mapping, so the default cannot be filled.
_NO_BODY: Final[Mapping[str, JsonValue]] = MappingProxyType({})

# M0 raises no flag on a call (requirement 17.3).
_NO_FLAGS: Final[tuple[str, ...]] = ()


@dataclass(frozen=True, slots=True)
class TraceHeader:
    """What every event of one sandbox repeats, fixed for the life of the sandbox.

    The five values of requirement 17.2 that do not depend on the event, plus the time
    zone the ``ts`` of an event is read in:

    - ``sandbox_id``: the id of the sandbox this trace belongs to;
    - ``instance``, ``profile``, ``dataset``: ``<name>@<version>`` each, the instance
      configuration of the service and the profile and initial dataset the sandbox was
      created with;
    - ``anchor_date``: the anchor date of the sandbox, which a reset and a restore leave
      alone (requirement 16.2); written as ``YYYY-MM-DD``, so the date is kept as a date
      and formatted in one place;
    - ``timezone``: an IANA time zone name, None the local time zone of the service
      process. It is ``clock.timezone`` of the instance configuration, the same name the
      anchor date of a sandbox in mode today is read in
      (:func:`~erp_mock.gateway.clock.resolve_anchor`).

    The time zone is here rather than an argument of :meth:`TraceWriter.write` on purpose:
    it belongs to the service, not to the request that triggered an event. As an argument
    it would let two events of one sandbox be stamped in two different zones, and a reader
    of the file could then no longer read the order of the events off their ``ts``.

    Every field is a value the Sandbox_Manager reads once, when it creates the sandbox,
    and the header is frozen, so it is safe to share between threads.
    """

    sandbox_id: str
    instance: str
    profile: str
    dataset: str
    anchor_date: date
    timezone: str | None = None


class TraceWriteError(Exception):
    """A trace event that could not be written to the file (design.md error handling).

    ``path`` is the trace file and ``seq`` the event that was being written, and the
    OSError that caused it is the ``__cause__`` of this one. The database transaction of
    the request is committed by the time its event is written, so the caller turns the
    sandbox frozen: its trace would otherwise stay behind the content of its database.
    """

    __slots__ = ("path", "seq")

    path: Path
    seq: int

    def __init__(self, path: Path, seq: int, cause: OSError) -> None:
        super().__init__(_WRITE_FAILED.format(path=path, seq=seq, reason=cause.strerror or cause))
        self.path = path
        self.seq = seq


class TraceWriter:
    """Appends the events of one sandbox to its trace file (requirements 17.1, 17.2).

    The file at ``path`` is opened for appending and created where it is not there yet;
    ``header`` is what every event of this sandbox repeats and ``clock`` where the ``ts``
    of an event comes from. Opening an existing file continues its numbering (see the
    module docstring), so the writer of a sandbox may be closed and opened again - after a
    reset, a restore or a reopen of the sandbox - without the sandbox's ``seq`` repeating
    or skipping a number.

    One writer per sandbox and one write at a time: the writer holds a file descriptor, the
    next ``seq``, and nothing else, and it takes no lock. Every write of a sandbox runs
    under the sandbox's lock (design.md 7.5), which is what makes the two steps of a
    tools/call - reading :attr:`next_seq`, then writing the event - one unit.

    Raises OSError from the constructor where the file cannot be opened, the sandbox
    directory missing among the reasons; the Sandbox_Manager creates that directory before
    the writer. A write that fails raises :class:`TraceWriteError`.
    """

    __slots__ = ("_clock", "_fd", "_header", "_next_seq", "_path", "_pending_newline")

    def __init__(self, path: Path, header: TraceHeader, clock: SystemClock) -> None:
        self._path = path
        self._header = header
        self._clock = clock
        written, complete = _count_lines(path)
        self._next_seq = written + 1
        # A trace file whose last line has no newline is one a write left half-finished,
        # which only a process that died mid-write can leave behind (a failed write
        # freezes the sandbox). The fragment is not counted as an event and the next event
        # starts on a line of its own, so every other line stays one whole event.
        self._pending_newline = not complete
        self._fd = os.open(path, _OPEN_FLAGS, _FILE_MODE)

    @property
    def path(self) -> Path:
        """The trace file this writer appends to."""
        return self._path

    @property
    def next_seq(self) -> int:
        """The ``seq`` the next event will carry; 1 for a sandbox with no event yet.

        Reading it reserves nothing. A tools/call reads it as it starts, records the
        records it writes under that number (requirement 18.10) and lets
        :meth:`write` hand out the same number when the call ends; the sandbox lock is
        what keeps another event from taking it in between (requirement 17.2).
        """
        return self._next_seq

    def write(
        self,
        event: EventName,
        session_id: str | None,
        body: Mapping[str, JsonValue] = _NO_BODY,
    ) -> int:
        """Append one ``event`` to the trace file and return the ``seq`` it was given.

        ``session_id`` is the session that triggered the event, None for an event the Admin
        API triggered (requirement 17.2). ``body`` is what follows the common fields: the
        result of :func:`tools_call_body` or :func:`unknown_tool_body` for a tools_call, of
        :func:`snapshot_restore_body` for a snapshot_restore, and nothing for every other
        event. Its keys must be the ones :data:`BODY_KEYS` has for ``event``, in that
        order, so that the events of two sandboxes compare key by key (requirement 24.3).

        The event is one line of UTF-8, written in full before this returns: the ``ts`` is
        read from the clock here, the object is built in the key order of
        :data:`COMMON_KEYS` followed by ``body``, and ``os.write`` is repeated until every
        byte is in the file (requirement 17.6). No ``fsync`` follows, so the bytes are
        visible to a reader but not promised to survive a crash of the machine.

        Raises ValueError where the keys of ``body`` are not the ones of ``event`` or the
        writer is closed, TypeError or ValueError where a value of ``body`` is not a JSON
        value (:func:`~erp_mock.common.jsonio.dumps_compact`), and
        :class:`TraceWriteError` where the file could not be written. Nothing but a
        complete line is ever appended by a write that returns, and a write that raises
        leaves the ``seq`` it was given unused.
        """
        expected = BODY_KEYS[event]
        found = tuple(body)
        if found != expected:
            raise ValueError(_WRONG_BODY.format(event=event, expected=expected, found=found))
        if self._fd < 0:
            raise ValueError(_CLOSED.format(path=self._path))
        seq = self._next_seq
        header = self._header
        line = dumps_compact(
            {
                "seq": seq,
                "ts": self._clock.now_iso_ms(header.timezone),
                "sandbox_id": header.sandbox_id,
                "session_id": session_id,
                "instance": header.instance,
                "profile": header.profile,
                "dataset": header.dataset,
                "anchor_date": header.anchor_date.isoformat(),
                "event": event,
                **body,
            }
        )
        self._append(encode_utf8(line + "\n"), seq)
        self._next_seq = seq + 1
        return seq

    def close(self) -> None:
        """Close the file descriptor; writing afterwards raises ValueError.

        Called when the sandbox is deleted, and when its directory is about to be removed.
        Closing an already closed writer does nothing, so it may be called twice.
        """
        if self._fd >= 0:
            fd, self._fd = self._fd, -1
            os.close(fd)

    def _append(self, data: bytes, seq: int) -> None:
        """Write every byte of ``data`` to the file, repeating ``os.write`` as needed.

        ``os.write`` may write fewer bytes than it was given, so what is left is written
        again until nothing is; a memoryview slices the bytes without copying them.
        """
        if self._pending_newline:
            data = b"\n" + data
        view = memoryview(data)
        try:
            while view:
                view = view[os.write(self._fd, view) :]
        except OSError as error:
            raise TraceWriteError(self._path, seq, error) from error
        self._pending_newline = False


def tools_call_body(
    *,
    tool: str,
    args: Mapping[str, JsonValue] | None,
    result: Mapping[str, JsonValue] | None,
    is_error: bool,
    latency_ms: int,
    coerced: Sequence[str] = (),
    error_code: str | None = None,
    rule_id: str | None = None,
    writes: Sequence[Mapping[str, JsonValue]] = (),
    known_tool: bool = True,
) -> dict[str, JsonValue]:
    """The body of a tools_call event, its keys in the order of requirement 17.3.

    - ``tool``: the tool name of the request, known or not;
    - ``args``: the ``arguments`` of the request as they arrived, before the lenient
      conversion and the rounding of requirement 12.5; None where the request carried none,
      which is written as an empty object;
    - ``result``: the ``structuredContent`` of a successful call, the error object of a
      failed one (requirement 13.6) and the JSON-RPC error object of an unknown tool;
    - ``is_error``: the ``isError`` of the tool result;
    - ``latency_ms``: whole milliseconds from the arrival of the request to just before
      the event is written, by the monotonic clock;
    - ``coerced``: the parameter paths that arrived as decimal strings, in the order of
      requirement 12.12, empty for a call that ended before input validation
      (requirement 17.11);
    - ``error_code`` and ``rule_id``: the ``code`` and the ``rule_id`` of the error object,
      both None for a successful call;
    - ``writes``: the net change of each (record, cause) of the call, built with
      :func:`write_item` and sorted by requirement 17.4; empty for a read and for every
      call that ended with an error (requirement 17.5);
    - ``known_tool``: whether the tool is one of the session's profile.

    ``flags`` is an empty array, which is every call of M0 (requirement 17.3), and is not
    an argument. The values are used as they are, not copied, and must be JSON values.
    """
    return {
        "tool": tool,
        "args": {} if args is None else args,
        "coerced": coerced,
        "known_tool": known_tool,
        "is_error": is_error,
        "error_code": error_code,
        "rule_id": rule_id,
        "result": result,
        "writes": writes,
        "flags": _NO_FLAGS,
        "latency_ms": latency_ms,
    }


def unknown_tool_body(
    *,
    tool: str,
    args: Mapping[str, JsonValue] | None,
    result: Mapping[str, JsonValue],
    latency_ms: int,
) -> dict[str, JsonValue]:
    """The body of the tools_call event of an unknown tool name (requirement 17.10).

    Everything requirement 17.10 fixes is fixed here: ``known_tool`` is false,
    ``is_error`` true, ``error_code`` and ``rule_id`` null, and ``coerced``, ``writes`` and
    ``flags`` empty arrays. ``result`` is the JSON-RPC error object the client is given,
    whose ``code`` is -32602 and whose ``message`` names the tool (requirement 14.6);
    composing it belongs to the handler that answers the call, not here.
    """
    return tools_call_body(
        tool=tool,
        args=args,
        result=result,
        is_error=True,
        latency_ms=latency_ms,
        known_tool=False,
    )


def snapshot_restore_body(name: str) -> dict[str, JsonValue]:
    """The body of a snapshot_restore event: the name of the Named_Snapshot (7.6)."""
    return {"name": name}


def write_item(*, entity: str, op: str, id: str, cause: str) -> dict[str, JsonValue]:
    """One item of a tools_call event's ``writes``, in the order of requirement 17.4.

    ``entity`` is the table of the record, ``op`` the net change (insert, update or
    delete), ``id`` the id of the record and ``cause`` ``direct`` or the id of the linkage
    or writeback configuration that changed it. The caller sorts the items
    (``Write.sort_key`` of :class:`~erp_mock.state.store.Write`) before passing them to
    :func:`tools_call_body`; this function only decides the keys.
    """
    return {"entity": entity, "op": op, "id": id, "cause": cause}


def _count_lines(path: Path) -> tuple[int, bool]:
    """How many whole lines ``path`` holds, and whether it ends with one.

    A file that is not there yet holds no line and needs no newline before its first
    event; so does an empty one. An event never contains a raw newline - the three line
    terminators json leaves alone are escaped by
    :func:`~erp_mock.common.jsonio.dumps_compact` (requirement 17.8) - so the newlines of
    the file are exactly the ends of its events.
    """
    lines = 0
    last = b""
    try:
        with path.open("rb") as existing:
            while chunk := existing.read(_READ_CHUNK):
                lines += chunk.count(b"\n")
                last = chunk[-1:]
    except FileNotFoundError:
        return 0, True
    return lines, last in (b"", b"\n")
