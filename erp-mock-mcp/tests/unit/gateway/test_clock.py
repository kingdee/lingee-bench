"""erp_mock.gateway.clock: the system time of the service (requirements 16.1, 17.2).

Every clock here is driven by :class:`_Instants`, a stand-in source over instants written
into this file, so no test reads the system clock or the current date (requirement 25.10);
what each one checks is the converting and the formatting the clock does over the instant
it was handed, and how often it reads it. The one real reading is of the monotonic clock,
which counts seconds and tells nothing of the date.

The instant of :data:`INSTANT` is the example of design.md component 8, and the time zones
are chosen so that a result gives away where it came from: ``Asia/Shanghai`` and
``America/New_York`` put it on two different dates, ``Asia/Kolkata`` and
``Pacific/Marquesas`` have half-hour offsets, and ``UTC`` is the one zone whose offset a
reading that ignored its argument would also show. The local time zone of the process is
set by the ``local_zone`` fixture, so ``None`` has a result this file can name.
"""

import re
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from typing import Final

import pytest

from erp_mock.config.instance import ClockDef, InstanceDef
from erp_mock.gateway.clock import SystemClock, resolve_anchor
from erp_mock.loader.step8 import anchor_of

# The instant every test reads: 2026-10-09T10:15:32.481+08:00, the example of design.md
# component 8. The microseconds are not a whole number of milliseconds, so a result shows
# whether they were dropped (.481) or rounded (.482).
INSTANT: Final = datetime(2026, 10, 9, 2, 15, 32, 481_999, tzinfo=UTC)

# The local time zone of the process while a test that asks for it runs (+05:30): a
# half-hour offset, so a reading that fell back to UTC or to a whole hour is recognised.
LOCAL_ZONE: Final = "Asia/Kolkata"

# The date given for one sandbox (an Admin API request, ``--anchor-date``) and the
# ``anchor_date`` of a fixed clock: three different dates, so the priority of
# requirement 16.1 shows in the result.
EXPLICIT: Final = date(2031, 2, 28)
FIXED_TEXT: Final = "2044-07-01"
FIXED: Final = date(2044, 7, 1)

# requirement 17.2: ISO 8601, three fractional digits, the offset written +HH:MM or -HH:MM.
TS_FORMAT: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}")


class _Instants:
    """A stand-in time source over the instants it was given, counting the readings.

    One reading per instant, in order; once the last one is reached it is returned again,
    so a test that only wants one instant passes one. ``reads`` is how often the clock
    read the source, which is what "reads the system time once" is checked by.
    """

    def __init__(self, *moments: datetime) -> None:
        self._moments: Final = moments
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self._moments[min(self.reads, len(self._moments)) - 1]


def clock_over(*moments: datetime) -> tuple[SystemClock, _Instants]:
    """A clock reading ``moments``, with the source so a test can count the readings."""
    source = _Instants(*moments)
    return SystemClock(now=source), source


def instance_with(clock: Mapping[str, str]) -> InstanceDef:
    """An instance.yaml whose ``clock`` is ``clock``, for the comparison with step 8."""
    document = {
        "instance": "fx-clock",
        "version": "1.0.0",
        "title": {"zh": "时钟单测实例"},
        "packs": {"fx_base": "1.0.0"},
        "profiles": ["semantic"],
        "default_profile": "semantic",
        "datasets": ["fx-basic"],
        "default_dataset": "fx-basic",
        "clock": dict(clock),
    }
    return InstanceDef.model_validate(document)


@pytest.fixture
def local_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Run the test with :data:`LOCAL_ZONE` as the local time zone of the process."""
    monkeypatch.setenv("TZ", LOCAL_ZONE)
    time.tzset()
    yield LOCAL_ZONE
    monkeypatch.undo()
    time.tzset()


# --- reading the current date (requirement 16.1) -----------------------------------------


@pytest.mark.req("16.1")
@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("Asia/Shanghai", date(2026, 10, 9)),
        ("UTC", date(2026, 10, 9)),
        ("America/New_York", date(2026, 10, 8)),
        ("Asia/Kolkata", date(2026, 10, 9)),
        ("Pacific/Kiritimati", date(2026, 10, 9)),
    ],
)
def test_today_is_the_date_of_the_instant_in_the_named_zone(zone: str, expected: date) -> None:
    clock, source = clock_over(INSTANT)

    assert clock.today(zone) == expected, f"{zone} 的当天日期换算错误"
    assert source.reads == 1, "一次 today 只读一次时间源"


@pytest.mark.req("16.1")
def test_one_instant_falls_on_two_dates_in_two_zones() -> None:
    clock, _ = clock_over(INSTANT)

    shanghai = clock.today("Asia/Shanghai")
    new_york = clock.today("America/New_York")

    assert shanghai != new_york, "同一时刻在东八区与美东区应落在不同日期"
    assert (shanghai - new_york).days == 1, "东八区比美东区早一天"


@pytest.mark.req("16.1", "17.2")
def test_the_date_turns_over_at_midnight_of_the_named_zone() -> None:
    # One millisecond apart: the last instant of 2026-10-09 in Asia/Shanghai and the first
    # of 2026-10-10. Both are still 2026-10-09 in UTC, so the turn is the zone's own.
    before = datetime(2026, 10, 9, 15, 59, 59, 999_000, tzinfo=UTC)
    after = datetime(2026, 10, 9, 16, 0, 0, tzinfo=UTC)
    clock, _ = clock_over(before, after)

    assert clock.today("Asia/Shanghai") == date(2026, 10, 9), "午夜之前仍是当天"
    assert clock.today("Asia/Shanghai") == date(2026, 10, 10), "午夜之后是次日"

    clock, _ = clock_over(before, after)
    assert clock.now_iso_ms("Asia/Shanghai") == "2026-10-09T23:59:59.999+08:00"
    assert clock.now_iso_ms("Asia/Shanghai") == "2026-10-10T00:00:00.000+08:00"
    assert clock.today("UTC") == date(2026, 10, 9), "同两个时刻在 UTC 都还是 10-09"


@pytest.mark.req("16.1", "17.2")
def test_no_zone_means_the_local_time_zone_of_the_process(local_zone: str) -> None:
    clock, _ = clock_over(INSTANT)

    assert clock.today(None) == date(2026, 10, 9), f"未设置时区应取进程本地时区 {local_zone}"
    assert clock.now_iso_ms(None) == "2026-10-09T07:45:32.481+05:30"
    assert clock.today(None) == clock.today(local_zone), "未设置时区与本地时区名的结果相同"
    assert clock.now_iso_ms(None) != clock.now_iso_ms("UTC"), "未设置时区不等于 UTC"


# --- the ts of a trace event (requirement 17.2) ------------------------------------------


@pytest.mark.req("17.2")
@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("Asia/Shanghai", "2026-10-09T10:15:32.481+08:00"),
        ("UTC", "2026-10-09T02:15:32.481+00:00"),
        ("America/New_York", "2026-10-08T22:15:32.481-04:00"),
        ("Asia/Kolkata", "2026-10-09T07:45:32.481+05:30"),
        ("Pacific/Marquesas", "2026-10-08T16:45:32.481-09:30"),
    ],
)
def test_now_iso_ms_writes_milliseconds_and_the_offset(zone: str, expected: str) -> None:
    clock, source = clock_over(INSTANT)

    written = clock.now_iso_ms(zone)

    assert written == expected, f"{zone} 的时间文本错误"
    assert TS_FORMAT.fullmatch(written) is not None, "ts 必须是带时区偏移、毫秒精度的 ISO 8601"
    assert "Z" not in written, "零偏移写作 +00:00，不写 Z"
    assert source.reads == 1, "一次 now_iso_ms 只读一次时间源"


@pytest.mark.req("17.2")
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # A whole second still has three fractional digits, and a microsecond below the
        # millisecond is dropped, not rounded up.
        (datetime(2026, 10, 9, 2, 15, 32, tzinfo=UTC), "2026-10-09T10:15:32.000+08:00"),
        (datetime(2026, 10, 9, 2, 15, 32, 999, tzinfo=UTC), "2026-10-09T10:15:32.000+08:00"),
        (datetime(2026, 10, 9, 2, 15, 32, 1_000, tzinfo=UTC), "2026-10-09T10:15:32.001+08:00"),
        (datetime(2026, 10, 9, 2, 15, 32, 999_999, tzinfo=UTC), "2026-10-09T10:15:32.999+08:00"),
    ],
)
def test_the_milliseconds_are_three_digits_and_truncated(moment: datetime, expected: str) -> None:
    clock, _ = clock_over(moment)

    assert clock.now_iso_ms("Asia/Shanghai") == expected, "毫秒应截断为三位"


@pytest.mark.req("17.2")
def test_the_offset_is_the_one_of_the_instant_not_of_the_zone() -> None:
    # Summer time: the same zone has -04:00 in October and -05:00 in January.
    january = datetime(2026, 1, 9, 2, 15, 32, 481_999, tzinfo=UTC)
    clock, _ = clock_over(INSTANT, january)

    assert clock.now_iso_ms("America/New_York") == "2026-10-08T22:15:32.481-04:00"
    assert clock.now_iso_ms("America/New_York") == "2026-01-08T21:15:32.481-05:00"


@pytest.mark.req("16.1", "17.2")
def test_an_instant_without_a_time_zone_is_refused() -> None:
    clock, _ = clock_over(datetime(2026, 10, 9, 2, 15, 32, 481_000))

    with pytest.raises(ValueError, match="没有时区信息"):
        clock.today("Asia/Shanghai")
    with pytest.raises(ValueError, match="没有时区信息"):
        clock.now_iso_ms(None)


# --- the monotonic clock (requirement 24.7) ----------------------------------------------
#
# What it is for - the idle, freeze and retention timing of requirements 15.5 to 15.9 - is
# the Sandbox_Manager's, and is checked there; here it is the reading itself.


@pytest.mark.req("24.7")
def test_monotonic_comes_from_the_injected_source() -> None:
    steps = iter([1.0, 1.25, 4.0])
    clock = SystemClock(monotonic=lambda: next(steps))

    assert [clock.monotonic() for _ in range(3)] == [1.0, 1.25, 4.0], "单调时钟来自注入的时间源"


@pytest.mark.req("24.7")
def test_the_default_monotonic_clock_does_not_go_back() -> None:
    clock = SystemClock()

    first = clock.monotonic()
    second = clock.monotonic()

    assert second >= first, "缺省单调时钟不回退"


# --- the anchor date of a sandbox (requirement 16.1) -------------------------------------


@pytest.mark.req("16.1")
@pytest.mark.parametrize(
    "clock_cfg",
    [
        pytest.param(ClockDef(), id="today-default"),
        pytest.param(ClockDef(mode="today", timezone="Asia/Shanghai"), id="today-zone"),
        pytest.param(ClockDef(mode="fixed", anchor_date=FIXED_TEXT), id="fixed"),
    ],
)
def test_the_date_given_for_the_sandbox_wins_over_the_configuration(clock_cfg: ClockDef) -> None:
    clock, source = clock_over(INSTANT)

    assert resolve_anchor(EXPLICIT, clock_cfg, clock) == EXPLICIT, "沙箱级指定的日期优先"
    assert source.reads == 0, "有指定日期时不读系统时间"


@pytest.mark.req("16.1")
def test_a_fixed_clock_gives_its_anchor_date() -> None:
    clock, source = clock_over(INSTANT)
    instance = instance_with({"mode": "fixed", "anchor_date": FIXED_TEXT})

    assert resolve_anchor(None, instance.clock, clock) == FIXED, "mode 为 fixed 时取 anchor_date"
    assert source.reads == 0, "mode 为 fixed 时不读系统时间"
    assert resolve_anchor(None, instance.clock, clock) == anchor_of(instance, None), (
        "fixed 分支必须与加载第 8 步的锚点日期一致"
    )


@pytest.mark.req("16.1")
@pytest.mark.parametrize(
    ("clock_cfg", "expected"),
    [
        pytest.param(
            ClockDef(mode="today", timezone="Asia/Shanghai"), date(2026, 10, 9), id="shanghai"
        ),
        pytest.param(
            ClockDef(mode="today", timezone="America/New_York"), date(2026, 10, 8), id="new-york"
        ),
        pytest.param(
            # mode today with an anchor_date is a legal configuration: the mode decides,
            # and the declared date is only there for a sandbox that asks for it.
            ClockDef(mode="today", anchor_date=FIXED_TEXT, timezone="UTC"),
            date(2026, 10, 9),
            id="today-with-anchor-date",
        ),
    ],
)
def test_a_clock_in_mode_today_reads_the_system_time_once(
    clock_cfg: ClockDef, expected: date
) -> None:
    clock, source = clock_over(INSTANT)

    assert resolve_anchor(None, clock_cfg, clock) == expected, "mode 为 today 时取系统时间的日期"
    assert source.reads == 1, "mode 为 today 时只读一次系统时间"


@pytest.mark.req("16.1")
def test_a_clock_without_a_mode_takes_today_in_the_local_time_zone(local_zone: str) -> None:
    clock, source = clock_over(INSTANT)
    instance = instance_with({})

    assert instance.clock == ClockDef(), "未设置 clock 时为 mode today、无时区"
    assert resolve_anchor(None, instance.clock, clock) == date(2026, 10, 9), (
        f"未设置时区应取进程本地时区 {local_zone} 的日期"
    )
    assert source.reads == 1, "未设置 clock 时同样只读一次系统时间"


@pytest.mark.req("16.1")
def test_mode_fixed_without_an_anchor_date_is_an_error() -> None:
    # The configuration cannot be written this way - ClockDef reports it, and loading
    # step 1 reports it at clock.anchor_date (requirement 16.3) - so the guard is reached
    # only by skipping validation, as nothing in the service does.
    clock, _ = clock_over(INSTANT)
    unchecked = ClockDef.model_construct(mode="fixed", anchor_date=None, timezone=None)

    with pytest.raises(AssertionError, match="clock.anchor_date"):
        resolve_anchor(None, unchecked, clock)
