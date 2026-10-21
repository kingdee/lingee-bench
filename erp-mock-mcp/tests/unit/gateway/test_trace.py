"""erp_mock.gateway.trace: the trace file of one sandbox (requirement 17).

What a trace event is checked here as text, not as a parsed object: the expected line of
each test is built character by character from :func:`common_fields`, so a key in another
place, a space between two tokens or a missing newline fails the test even where the
parsed object would still be equal. That is what requirement 24.3 compares two sandboxes
by, and what a reader of the file splits into events (requirement 17.1).

Every writer here reads :func:`stepping_clock`, a clock over instants written into this
file, so no test reads the system clock (requirement 25.10) and the ``ts`` of the nth
event of a writer is ``TS[n - 1]``. The first of them is the instant of design.md
component 8, read in the time zone of :data:`HEADER`.

The names in :data:`HEADER` and in the arguments and results below are of no business
pack: what the module does with them is to copy them into a line.
"""

import errno
import itertools
import json
import os
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.jsonio import JsonValue
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.trace import (
    BODY_KEYS,
    COMMON_KEYS,
    TOOLS_CALL_KEYS,
    TRACE_FILENAME,
    WRITE_KEYS,
    EventName,
    TraceHeader,
    TraceWriteError,
    TraceWriter,
    snapshot_restore_body,
    tools_call_body,
    unknown_tool_body,
    write_item,
)

# The sandbox every event below belongs to. The time zone is the one of design.md
# component 8, so the ts of the first event is the instant of that example.
HEADER: Final = TraceHeader(
    sandbox_id="sb_3f9c1e7a0b2d4c6e",
    instance="fx-trace@1.0.0",
    profile="semantic@1.0.0",
    dataset="fx-basic@1.0.0",
    anchor_date=date(2026, 10, 9),
    timezone="Asia/Shanghai",
)

# The session that triggered an event; an event of the Admin API has None instead.
SESSION: Final = "9d2c8f14b7a3e05c"

# The instant the first reading of a clock from `stepping_clock` returns; the microseconds
# are not a whole millisecond, so a ts that was rounded rather than truncated shows.
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_999, tzinfo=UTC)

# The ts of the first four events of one writer: a second apart, in HEADER's time zone.
TS: Final = (
    "2026-10-09T10:15:32.481+08:00",
    "2026-10-09T10:15:33.481+08:00",
    "2026-10-09T10:15:34.481+08:00",
    "2026-10-09T10:15:35.481+08:00",
)

# The body of one tools_call, the example of design.md data model 4 in the small: the
# arguments are the raw values of the request (a decimal string and a float among them),
# one of them was coerced, and the call inserted one record.
TOOL: Final = "fx_doc_create"
ARGS: Final[Mapping[str, JsonValue]] = {
    "partner_id": "px_000107",
    "lines": [{"qty": "2000", "price": 0.85}],
}
RESULT: Final[Mapping[str, JsonValue]] = {"id": "dc_000001", "status": "draft"}
WRITES: Final = (write_item(entity="fx_doc", op="insert", id="dc_000001", cause="direct"),)
COERCED: Final = ("lines[0].qty",)

# The JSON-RPC error of a tool name that is not in the session's profile (requirement 14.6);
# the handler composes it, the event carries it as its result (requirement 17.10).
UNKNOWN_ERROR: Final[Mapping[str, JsonValue]] = {
    "code": -32602,
    "message": "未知工具：fx_doc_destroy",
}


def stepping_clock() -> SystemClock:
    """A clock whose nth reading is :data:`INSTANT` plus n - 1 seconds."""
    readings = itertools.count()
    return SystemClock(now=lambda: INSTANT + timedelta(seconds=next(readings)))


def common_fields(seq: int, ts: str, session_id: str | None, event: EventName) -> str:
    """The text an event starts with, up to and including ``event``.

    Written out here rather than taken from the module, so a change of the key order of
    requirement 17.2 fails every test of this file.
    """
    session = "null" if session_id is None else f'"{session_id}"'
    return (
        f'{{"seq":{seq},"ts":"{ts}","sandbox_id":"{HEADER.sandbox_id}","session_id":{session},'
        f'"instance":"{HEADER.instance}","profile":"{HEADER.profile}",'
        f'"dataset":"{HEADER.dataset}","anchor_date":"2026-10-09","event":"{event}"'
    )


def lines_of(path: Path) -> list[str]:
    """The lines of a trace file, each without its newline; the file must end with one."""
    text = path.read_text("utf-8")
    assert text == "" or text.endswith("\n"), "每个事件以换行符结尾"
    return text.split("\n")[:-1]


def keys_of(line: str) -> list[str]:
    """The keys of one event, in the order the line lists them."""
    return [key for key, _ in json.loads(line, object_pairs_hook=list)]


@pytest.fixture
def new_writer(tmp_path: Path) -> Iterator[Callable[[], TraceWriter]]:
    """Open a writer on ``<tmp_path>/trace.jsonl``; called twice it reopens that file."""
    opened: list[TraceWriter] = []

    def open_writer() -> TraceWriter:
        writer = TraceWriter(tmp_path / TRACE_FILENAME, HEADER, stepping_clock())
        opened.append(writer)
        return writer

    yield open_writer
    for writer in opened:
        writer.close()


# --- the common fields of every event (requirements 17.1, 17.2) --------------------------


@pytest.mark.req("17.1", "17.2")
def test_an_event_is_one_line_of_the_nine_common_fields(
    new_writer: Callable[[], TraceWriter],
) -> None:
    writer = new_writer()

    seq = writer.write(EventName.SESSION_START, SESSION)

    expected = common_fields(1, TS[0], SESSION, EventName.SESSION_START) + "}"
    assert seq == 1
    assert lines_of(writer.path) == [expected]
    assert keys_of(expected) == list(COMMON_KEYS)


@pytest.mark.req("17.1", "17.2")
@pytest.mark.parametrize(
    "event",
    [
        EventName.SESSION_START,
        EventName.TOOLS_LIST,
        EventName.SANDBOX_RESET,
        EventName.SESSION_END,
    ],
)
def test_an_event_that_is_not_tools_call_carries_no_body(
    new_writer: Callable[[], TraceWriter], event: EventName
) -> None:
    writer = new_writer()

    writer.write(event, SESSION)

    assert lines_of(writer.path) == [common_fields(1, TS[0], SESSION, event) + "}"]


@pytest.mark.req("17.2")
def test_an_event_of_the_admin_api_has_no_session(new_writer: Callable[[], TraceWriter]) -> None:
    writer = new_writer()

    writer.write(EventName.SANDBOX_RESET, None)

    line = lines_of(writer.path)[0]
    assert line == common_fields(1, TS[0], None, EventName.SANDBOX_RESET) + "}"
    assert json.loads(line)["session_id"] is None


@pytest.mark.req("17.1", "17.2")
def test_each_event_gets_its_own_timestamp(new_writer: Callable[[], TraceWriter]) -> None:
    writer = new_writer()

    writer.write(EventName.SESSION_START, SESSION)
    writer.write(EventName.TOOLS_LIST, SESSION)
    writer.write(EventName.SESSION_END, SESSION)

    assert [json.loads(line)["ts"] for line in lines_of(writer.path)] == list(TS[:3])


# --- seq (requirements 17.2, 17.7) -------------------------------------------------------


@pytest.mark.req("17.2")
def test_seq_starts_at_one_and_rises_by_one(new_writer: Callable[[], TraceWriter]) -> None:
    writer = new_writer()

    assert writer.next_seq == 1, "没有事件的沙箱的下一个 seq 为 1"
    written = [
        writer.write(EventName.SESSION_START, SESSION),
        writer.write(EventName.TOOLS_LIST, SESSION),
        writer.write(EventName.TOOLS_LIST, SESSION),
    ]

    assert written == [1, 2, 3], "write 返回本次事件的 seq"
    assert writer.next_seq == 4
    assert [json.loads(line)["seq"] for line in lines_of(writer.path)] == [1, 2, 3]


@pytest.mark.req("17.2")
def test_next_seq_is_the_seq_the_next_event_gets(new_writer: Callable[[], TraceWriter]) -> None:
    # How a tools/call uses the two steps: the seq is read as the call starts - and is
    # written into the database as the seq of the effective call - and the event is written
    # with it when the call ends.
    writer = new_writer()
    writer.write(EventName.SESSION_START, SESSION)

    call_seq = writer.next_seq

    assert call_seq == 2
    assert writer.next_seq == call_seq, "读取 next_seq 不消耗序号"
    assert writer.write(EventName.TOOLS_LIST, SESSION) == call_seq


@pytest.mark.req("17.2", "17.7")
def test_reopening_the_file_continues_the_numbering(
    new_writer: Callable[[], TraceWriter],
) -> None:
    first = new_writer()
    first.write(EventName.SESSION_START, SESSION)
    first.write(EventName.TOOLS_LIST, SESSION)
    first.close()

    reopened = new_writer()

    assert reopened.next_seq == 3, "重开已有的 trace 文件后 seq 接着原有事件"
    assert reopened.write(EventName.SESSION_END, SESSION) == 3
    assert [json.loads(line)["seq"] for line in lines_of(reopened.path)] == [1, 2, 3]


@pytest.mark.req("17.2", "17.7")
def test_a_reset_and_a_restore_append_to_the_events_already_written(
    new_writer: Callable[[], TraceWriter],
) -> None:
    # A reset and a restore only replace the content of the database files; the trace file
    # is in the sandbox directory beside them and is neither deleted nor rewritten, which
    # the writer notices in nothing but a rising seq.
    writer = new_writer()
    writer.write(EventName.SESSION_START, SESSION)
    before = writer.path.read_bytes()

    writer.write(EventName.SANDBOX_RESET, None)
    writer.write(EventName.SNAPSHOT_RESTORE, None, snapshot_restore_body("nightly"))
    writer.write(EventName.TOOLS_LIST, SESSION)

    lines = lines_of(writer.path)
    assert writer.path.read_bytes().startswith(before), "已写入的事件不被改写"
    assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3, 4]
    assert [json.loads(line)["event"] for line in lines] == [
        "session_start",
        "sandbox_reset",
        "snapshot_restore",
        "tools_list",
    ]


@pytest.mark.req("17.1", "17.2")
def test_a_line_left_half_written_is_not_counted_as_an_event(
    new_writer: Callable[[], TraceWriter], tmp_path: Path
) -> None:
    # Only a process that died in the middle of a write can leave such a fragment behind:
    # a write that fails freezes the sandbox, which writes no further event. The fragment
    # is no event, so it takes no seq, and it keeps a line of its own.
    writer = new_writer()
    writer.write(EventName.SESSION_START, SESSION)
    writer.close()
    fragment = '{"seq":2,"ts":"2026-'
    with (tmp_path / TRACE_FILENAME).open("ab") as broken:
        broken.write(fragment.encode())

    reopened = new_writer()

    assert reopened.next_seq == 2
    assert reopened.write(EventName.TOOLS_LIST, SESSION) == 2
    lines = lines_of(reopened.path)
    assert lines[1] == fragment, "未写完的文本保持原样"
    assert lines[2] == common_fields(2, TS[0], SESSION, EventName.TOOLS_LIST) + "}"


# --- a tools_call event (requirements 17.3, 17.10) ---------------------------------------


@pytest.mark.req("17.3")
def test_a_tools_call_event_carries_the_eleven_fields_of_the_requirement(
    new_writer: Callable[[], TraceWriter],
) -> None:
    writer = new_writer()
    body = tools_call_body(
        tool=TOOL,
        args=ARGS,
        result=RESULT,
        is_error=False,
        latency_ms=4,
        coerced=COERCED,
        writes=WRITES,
    )

    writer.write(EventName.TOOLS_CALL, SESSION, body)

    expected = common_fields(1, TS[0], SESSION, EventName.TOOLS_CALL) + (
        f',"tool":"{TOOL}","args":{{"partner_id":"px_000107","lines":[{{"qty":"2000","price":0.85}}]}},'
        '"coerced":["lines[0].qty"],"known_tool":true,"is_error":false,"error_code":null,'
        '"rule_id":null,"result":{"id":"dc_000001","status":"draft"},'
        '"writes":[{"entity":"fx_doc","op":"insert","id":"dc_000001","cause":"direct"}],'
        '"flags":[],"latency_ms":4}'
    )
    assert lines_of(writer.path) == [expected]
    assert keys_of(expected) == [*COMMON_KEYS, *TOOLS_CALL_KEYS]
    assert keys_of(expected)[-1] == "latency_ms", "事件体在公共字段之后"


@pytest.mark.req("17.3")
def test_a_failed_call_carries_the_error_object_and_its_code(
    new_writer: Callable[[], TraceWriter],
) -> None:
    writer = new_writer()
    error: Mapping[str, JsonValue] = {
        "code": "VALIDATION_FAILED",
        "message": "数量必须大于 0",
        "rule_id": "R-GEN-001",
        "details": [{"field": "lines[0].qty", "reason": "not_positive"}],
    }
    body = tools_call_body(
        tool=TOOL,
        args=ARGS,
        result=error,
        is_error=True,
        latency_ms=0,
        error_code="VALIDATION_FAILED",
        rule_id="R-GEN-001",
    )

    writer.write(EventName.TOOLS_CALL, SESSION, body)

    event = json.loads(lines_of(writer.path)[0])
    assert event["is_error"] is True
    assert event["error_code"] == "VALIDATION_FAILED"
    assert event["rule_id"] == "R-GEN-001"
    assert event["result"] == error, "出错时 result 为错误对象"
    assert event["known_tool"] is True
    assert event["writes"] == [], "出错的调用没有写入项"
    assert event["coerced"] == []
    assert event["flags"] == []
    assert event["latency_ms"] == 0


@pytest.mark.req("17.3")
def test_a_call_without_arguments_has_an_empty_object(
    new_writer: Callable[[], TraceWriter],
) -> None:
    writer = new_writer()
    body = tools_call_body(tool=TOOL, args=None, result=RESULT, is_error=False, latency_ms=1)

    writer.write(EventName.TOOLS_CALL, SESSION, body)

    assert '"args":{},' in lines_of(writer.path)[0], "未提供 arguments 时 args 为空对象"


@pytest.mark.req("17.3")
def test_the_keys_of_a_writes_item_are_entity_op_id_cause() -> None:
    item = write_item(entity="fx_doc_line", op="update", id="dc_000001-1", cause="lk_stock_in")

    assert list(item) == list(WRITE_KEYS)
    assert json.dumps(item, ensure_ascii=False, separators=(",", ":")) == (
        '{"entity":"fx_doc_line","op":"update","id":"dc_000001-1","cause":"lk_stock_in"}'
    )


@pytest.mark.req("17.10")
def test_an_unknown_tool_event_says_so_and_carries_the_json_rpc_error(
    new_writer: Callable[[], TraceWriter],
) -> None:
    writer = new_writer()
    body = unknown_tool_body(tool="fx_doc_destroy", args=ARGS, result=UNKNOWN_ERROR, latency_ms=0)

    writer.write(EventName.TOOLS_CALL, SESSION, body)

    line = lines_of(writer.path)[0]
    event = json.loads(line)
    assert keys_of(line) == [*COMMON_KEYS, *TOOLS_CALL_KEYS], "未知工具同样是 tools_call 事件"
    assert event["known_tool"] is False
    assert event["is_error"] is True
    assert event["error_code"] is None
    assert event["rule_id"] is None
    assert event["result"] == {"code": -32602, "message": "未知工具：fx_doc_destroy"}
    assert event["coerced"] == []
    assert event["writes"] == []
    assert event["flags"] == []
    assert event["tool"] == "fx_doc_destroy", "事件记录请求的工具名"
    assert event["args"] == ARGS


# --- a snapshot_restore event (design.md 7.6) --------------------------------------------


@pytest.mark.req("17.2")
def test_a_snapshot_restore_event_names_the_snapshot(
    new_writer: Callable[[], TraceWriter],
) -> None:
    writer = new_writer()

    writer.write(EventName.SNAPSHOT_RESTORE, None, snapshot_restore_body("before-月结"))

    line = lines_of(writer.path)[0]
    assert line == common_fields(1, TS[0], None, EventName.SNAPSHOT_RESTORE) + (
        ',"name":"before-月结"}'
    )
    assert keys_of(line) == [*COMMON_KEYS, "name"]


# --- the body a write accepts (requirement 24.3) -----------------------------------------


@pytest.mark.req("17.1")
def test_every_event_name_has_the_keys_of_its_body() -> None:
    assert set(BODY_KEYS) == set(EventName), "每个事件名都要声明事件体的键"
    assert BODY_KEYS[EventName.TOOLS_CALL] == TOOLS_CALL_KEYS
    assert BODY_KEYS[EventName.SNAPSHOT_RESTORE] == ("name",)
    assert BODY_KEYS[EventName.SESSION_START] == ()


@pytest.mark.req("17.3")
@pytest.mark.parametrize(
    ("event", "body"),
    [
        pytest.param(EventName.TOOLS_CALL, {}, id="tools-call-without-a-body"),
        pytest.param(
            EventName.TOOLS_CALL,
            dict(
                reversed(
                    list(
                        tools_call_body(
                            tool=TOOL, args=None, result=RESULT, is_error=False, latency_ms=1
                        ).items()
                    )
                )
            ),
            id="tools-call-in-another-key-order",
        ),
        pytest.param(EventName.SNAPSHOT_RESTORE, {}, id="restore-without-a-name"),
        pytest.param(EventName.SESSION_START, {"name": "nightly"}, id="session-start-with-a-body"),
    ],
)
def test_a_body_that_is_not_the_events_is_refused(
    new_writer: Callable[[], TraceWriter], event: EventName, body: Mapping[str, JsonValue]
) -> None:
    # The key order of an event is the module's, not the caller's: two sandboxes that ran
    # the same requests must list the same keys in the same order (requirement 24.3).
    writer = new_writer()

    with pytest.raises(ValueError, match="事件体的键"):
        writer.write(event, SESSION, body)

    assert writer.next_seq == 1, "被拒绝的事件不消耗 seq"
    assert writer.path.read_bytes() == b"", "被拒绝的事件不写入文件"


# --- the bytes of the file (requirements 17.1, 17.6) -------------------------------------


@pytest.mark.req("17.1")
def test_one_write_is_exactly_one_line_whatever_the_strings_hold(
    new_writer: Callable[[], TraceWriter],
) -> None:
    # Requirement 17.8 is a property of its own (task 21.3); this is the one example that
    # keeps the writer from putting a raw line break into the file.
    writer = new_writer()
    args: Mapping[str, JsonValue] = {
        "remark": 'one\ntwo\r\nthree\u2028four\u2029five\x85six "quoted" \\ 结束 𝄞',
    }
    body = tools_call_body(tool=TOOL, args=args, result=RESULT, is_error=False, latency_ms=2)

    writer.write(EventName.TOOLS_CALL, SESSION, body)

    raw = writer.path.read_bytes()
    text = raw.decode("utf-8")
    assert raw == text.encode("utf-8"), "trace 文件是 UTF-8"
    assert text.endswith("\n")
    assert len(text.split("\n")) == 2, "一次写入恰好一行"
    assert len(text.splitlines()) == 1, "任何行分隔符都不出现在行内"
    assert json.loads(text)["args"] == args, "该行按 JSON 解析后等于原事件"


@pytest.mark.req("17.6")
def test_an_event_is_in_the_file_as_soon_as_write_returns(
    new_writer: Callable[[], TraceWriter],
) -> None:
    # No fsync is called, and nothing is buffered in the process: a reader that opened the
    # file before the event was written reads it right after write returns, which is what
    # a client that got its response needs (requirement 17.6).
    writer = new_writer()

    with writer.path.open("rb") as reader:
        assert reader.read() == b""
        writer.write(EventName.SESSION_START, SESSION)
        first = reader.read()
        writer.write(EventName.TOOLS_LIST, SESSION)
        second = reader.read()

    assert first.decode() == common_fields(1, TS[0], SESSION, EventName.SESSION_START) + "}\n"
    assert second.decode() == common_fields(2, TS[1], SESSION, EventName.TOOLS_LIST) + "}\n"


@pytest.mark.req("17.6")
def test_every_byte_is_written_even_when_os_write_takes_them_in_pieces(
    new_writer: Callable[[], TraceWriter], monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = new_writer()
    real_write = os.write
    calls = 0

    def in_pieces(fd: int, data: memoryview) -> int:
        nonlocal calls
        calls += 1
        return real_write(fd, data[:7])

    monkeypatch.setattr(os, "write", in_pieces)
    try:
        writer.write(EventName.SESSION_START, SESSION)
    finally:
        monkeypatch.undo()

    assert calls > 1, "每次只写入一部分，循环到写完"
    assert lines_of(writer.path) == [
        common_fields(1, TS[0], SESSION, EventName.SESSION_START) + "}"
    ]


@pytest.mark.req("17.6")
def test_a_write_that_fails_is_reported_and_keeps_its_seq(
    new_writer: Callable[[], TraceWriter], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The caller turns the sandbox frozen and answers the request with -32603; what this
    # module owes it is an exception it can recognise, with the file and the event in it.
    writer = new_writer()
    writer.write(EventName.SESSION_START, SESSION)

    def refuse(fd: int, data: memoryview) -> int:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(os, "write", refuse)
    try:
        with pytest.raises(TraceWriteError) as caught:
            writer.write(EventName.TOOLS_LIST, SESSION)
    finally:
        monkeypatch.undo()

    assert caught.value.seq == 2
    assert caught.value.path == writer.path
    assert isinstance(caught.value.__cause__, OSError)
    assert str(writer.path) in str(caught.value)
    assert writer.next_seq == 2, "写入失败的 seq 不被下一个事件跳过"


@pytest.mark.req("17.1")
def test_closing_is_repeatable_and_a_closed_writer_writes_nothing(
    new_writer: Callable[[], TraceWriter],
) -> None:
    writer = new_writer()
    writer.write(EventName.SESSION_START, SESSION)

    writer.close()
    writer.close()

    with pytest.raises(ValueError, match="已关闭"):
        writer.write(EventName.SESSION_END, SESSION)
    assert len(lines_of(writer.path)) == 1


@pytest.mark.req("17.1")
def test_the_trace_file_is_created_where_there_is_none(tmp_path: Path) -> None:
    path = tmp_path / TRACE_FILENAME
    assert not path.exists()

    writer = TraceWriter(path, HEADER, stepping_clock())
    try:
        assert path.read_bytes() == b"", "打开即创建文件，还没有事件"
        assert writer.next_seq == 1
    finally:
        writer.close()
