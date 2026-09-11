"""Clocks.

The only source of "now" in the system. Everything that cares about time takes one, so a
test can place the bot at 15:30 on a Friday without waiting for Friday.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo


class SystemClock:
    """Wall-clock time in the office's timezone, as naive local values.

    Naive on purpose: the whole system reasons in one configured timezone, and mixing
    aware and naive values is a reliable source of off-by-one-day bugs at midnight.
    """

    __slots__ = ("_zone",)

    def __init__(self, timezone: str = "Asia/Almaty") -> None:
        self._zone = ZoneInfo(timezone)

    @property
    def timezone(self) -> ZoneInfo:
        return self._zone

    def now(self) -> datetime:
        return datetime.now(self._zone).replace(tzinfo=None)

    def today(self) -> date:
        return self.now().date()

    def time_of_day(self) -> time:
        return self.now().time()


class FixedClock:
    """A clock that stays where it is put, and moves only when told."""

    __slots__ = ("_now",)

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()

    def time_of_day(self) -> time:
        return self._now.time()

    def set(self, now: datetime) -> None:
        self._now = now

    def advance(self, **delta: float) -> None:
        from datetime import timedelta

        self._now += timedelta(**delta)
