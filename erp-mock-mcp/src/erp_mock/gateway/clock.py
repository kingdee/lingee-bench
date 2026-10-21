"""The system time of the service: anchor dates, trace stamps and timing (req 16.1, 17.2).

This is the one place in erp_mock that reads the current date, the current instant and a
monotonic clock (requirement 24.7): domain, tools, state, loader and seed take dates as
arguments, and the static scan of tests/static/test_no_clock.py keeps them from reading a
clock of their own. Three readings are needed, and :class:`SystemClock` has one method
each:

====================  ==============  =================================================
what                  method          who reads it
====================  ==============  =================================================
the current date      today           the anchor date of a sandbox (requirement 16.1)
the current instant   now_iso_ms      the ``ts`` of a trace event (requirement 17.2)
elapsed seconds       monotonic       the janitor and the resource limits (req 15.5-15.9)
====================  ==============  =================================================

A date and an instant are always taken in a named time zone - ``clock.timezone`` of the
instance configuration, the local time zone of the service process when it is not set -
so the two methods take that name and convert; ``monotonic`` counts seconds that have no
time zone and no relation to the wall clock, which is why idle, freeze and retention
timing uses it rather than dates.

:func:`resolve_anchor` puts the three sources of an anchor date in the order of
requirement 16.1. It is called once per sandbox, when the sandbox is created: the
Sandbox_Manager writes the date it returns into the sandbox metadata and keeps it there
for the life of the sandbox, over a reset, a restore and the turn of the day
(requirement 16.2). Loading has its own anchor date, which the CLI resolves through this
module before it loads and passes in
(:func:`~erp_mock.loader.step8.anchor_of`, requirement 24.7); both agree on a fixed clock,
whose ``anchor_date`` they read the same way.
"""

import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Final
from zoneinfo import ZoneInfo

from erp_mock.config.instance import ClockDef

__all__ = [
    "MonotonicSource",
    "NowSource",
    "SystemClock",
    "resolve_anchor",
]

# The current instant as an aware datetime. Its time zone does not matter: every result is
# converted to the time zone that was asked for.
type NowSource = Callable[[], datetime]
# Seconds of a clock that only moves forward, counted from an arbitrary point.
type MonotonicSource = Callable[[], float]

_NAIVE_INSTANT: Final = "时间源返回的时刻 {moment} 没有时区信息，无法换算为日期或时间文本"
_NO_FIXED_DATE: Final = "clock.mode 为 fixed 时必须有 clock.anchor_date（需求 16 第 3 条）"


def _system_now() -> datetime:
    """The current instant of the operating system, as an aware datetime in UTC."""
    return datetime.now(UTC)


class SystemClock:
    """The system time, read in the time zone the caller names (requirements 16.1, 17.2).

    ``now`` and ``monotonic`` are the two sources this reads, and both may be replaced:
    ``now`` returns the current instant as an aware datetime, in any time zone, and
    ``monotonic`` the seconds of a monotonic clock. Left out, they are ``datetime.now``
    in UTC and ``time.monotonic``, which is what the service runs with; a test passes
    doubles of its own - a source over a list of instants, a counter it steps itself -
    and so reads no clock (requirement 25.10). Converting and formatting stay with this
    class whichever source it was given, so a double decides the instant and nothing
    else.

    One object per process is enough: it holds its two sources and nothing else, and
    every method is a reading, so the object is safe to share between threads and
    sandboxes as long as the sources are.
    """

    __slots__ = ("_monotonic", "_now")

    def __init__(
        self, *, now: NowSource | None = None, monotonic: MonotonicSource | None = None
    ) -> None:
        self._now: NowSource = _system_now if now is None else now
        self._monotonic: MonotonicSource = time.monotonic if monotonic is None else monotonic

    def today(self, timezone: str | None) -> date:
        """The current date in ``timezone`` (requirement 16.1).

        ``timezone`` is an IANA time zone name the configuration has already been checked
        against (requirement 16.3), None the local time zone of the service process. The
        instant is read once, so two dates of one reading are impossible.
        """
        return self._moment(timezone).date()

    def now_iso_ms(self, timezone: str | None) -> str:
        """The current instant in ``timezone``, to the millisecond, with its offset.

        The ``ts`` of a trace event (requirement 17.2): ISO 8601 with exactly three
        fractional digits and the UTC offset of ``timezone`` at that instant, as in
        ``2026-10-09T10:15:32.481+08:00``. Microseconds are dropped, not rounded, and an
        offset of zero is written ``+00:00``, never ``Z``.
        """
        return self._moment(timezone).isoformat(timespec="milliseconds")

    def monotonic(self) -> float:
        """Seconds of a monotonic clock, for measuring how long something has lasted.

        The value has no meaning on its own - only differences of two readings do - and
        is unaffected by a change of the system time, which is what the idle, freeze and
        retention timing of the Sandbox_Manager needs (requirements 15.5 to 15.9).
        """
        return self._monotonic()

    def _moment(self, timezone: str | None) -> datetime:
        """The current instant, converted to ``timezone`` or to the local time zone.

        Raises ValueError where the source returned a datetime without a time zone: the
        instant it stands for would depend on where the process runs, which is a mistake
        in the double that returned it.
        """
        moment = self._now()
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise ValueError(_NAIVE_INSTANT.format(moment=moment.isoformat()))
        return moment.astimezone() if timezone is None else moment.astimezone(ZoneInfo(timezone))


def resolve_anchor(explicit: date | None, clock_cfg: ClockDef, sysclock: SystemClock) -> date:
    """The anchor date of a sandbox being created, by the priority of requirement 16.1.

    ``explicit`` is the date given for this one sandbox - the ``anchor_date`` of an Admin
    API request, the ``--anchor-date`` of stdio - which wins over the configuration;
    without one, a clock in mode fixed gives its ``anchor_date``, and a clock in mode
    today the current date of ``sysclock`` in ``clock_cfg.timezone``, read once here. A
    clock in mode today may carry an ``anchor_date`` as well, which nothing reads: the
    mode decides.

    ``clock_cfg`` is the ``clock`` of the instance configuration, which loading step 1 has
    checked (requirement 16.3), so mode fixed has a date and that date is a date of the
    calendar; a mode fixed without one is a sandbox without an anchor date and raises
    AssertionError.
    """
    if explicit is not None:
        return explicit
    if clock_cfg.mode == "fixed":
        if clock_cfg.anchor_date is None:
            raise AssertionError(_NO_FIXED_DATE)
        # The same reading as loading step 8 (:func:`~erp_mock.loader.step8.anchor_of`):
        # the text is YYYY-MM-DD and names a date of the calendar.
        return date.fromisoformat(clock_cfg.anchor_date)
    return sysclock.today(clock_cfg.timezone)
