"""Working-day arithmetic.

The domain never computes holidays itself — that needs a data source. Instead an adapter
hands it a :class:`CalendarSpec` of concrete dates, which keeps this module deterministic
and trivially testable, and makes the holiday source swappable per office.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from tabelshchik.domain.entities import FRIDAY, MONDAY

DEFAULT_WORKWEEK: frozenset[int] = frozenset(range(MONDAY, FRIDAY + 1))


class DayKind(StrEnum):
    WORKDAY = "workday"
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class CalendarSpec:
    """Which days an office is open.

    ``extra_workdays`` is not optional for Kazakhstan: public holidays are routinely
    transferred onto an otherwise-free Saturday, which then *is* a working day.
    It overrides everything else.
    """

    holidays: frozenset[date] = frozenset()
    closed: frozenset[date] = frozenset()
    extra_workdays: frozenset[date] = frozenset()
    workweek: frozenset[int] = field(default=DEFAULT_WORKWEEK)
    #: Human-readable reason per skipped date, for the "Пропущенные дни" report sheet.
    holiday_names: dict[date, str] = field(default_factory=dict)

    def kind(self, day: date) -> DayKind:
        if day in self.extra_workdays:
            return DayKind.WORKDAY
        if day in self.closed:
            return DayKind.CLOSED
        if day in self.holidays:
            return DayKind.HOLIDAY
        if day.weekday() not in self.workweek:
            return DayKind.WEEKEND
        return DayKind.WORKDAY

    def is_working_day(self, day: date) -> bool:
        return self.kind(day) is DayKind.WORKDAY

    def reason(self, day: date) -> str:
        kind = self.kind(day)
        if kind is DayKind.HOLIDAY:
            return self.holiday_names.get(day, "Праздничный день")
        if kind is DayKind.CLOSED:
            return self.holiday_names.get(day, "Офис закрыт")
        if kind is DayKind.WEEKEND:
            return "Выходной"
        return ""


def next_working_day(spec: CalendarSpec, after: date, *, limit: int = 30) -> date | None:
    """The first working day strictly after ``after``.

    This single rule is what makes the reminder schedule free of weekday special-casing:
    Mon→Tue and Fri→Mon both fall out of it, and a holiday chain is skipped correctly.
    """
    day = after
    for _ in range(limit):
        day += timedelta(days=1)
        if spec.is_working_day(day):
            return day
    return None


def working_days(spec: CalendarSpec, start: date, end: date) -> list[date]:
    """Working days in the inclusive range ``[start, end]``."""
    return [day for day in date_range(start, end) if spec.is_working_day(day)]


def date_range(start: date, end: date) -> Iterator[date]:
    """Every date in the inclusive range ``[start, end]``."""
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def start_of_week(day: date) -> date:
    """The Monday of ``day``'s ISO week. Horizons are always whole weeks."""
    return day - timedelta(days=day.weekday())


def week_index(anchor: date, day: date) -> int:
    """Which week of the horizon ``day`` falls in, counting from ``anchor``'s week."""
    return (start_of_week(day) - start_of_week(anchor)).days // 7


def last_working_day_of_month(spec: CalendarSpec, day: date) -> date | None:
    """The final working day of ``day``'s month — when the Tempo month-end nag fires."""
    if day.month == 12:
        first_of_next = date(day.year + 1, 1, 1)
    else:
        first_of_next = date(day.year, day.month + 1, 1)
    cursor = first_of_next - timedelta(days=1)
    while cursor.month == day.month:
        if spec.is_working_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return None


def last_working_day_of_week(spec: CalendarSpec, day: date) -> date | None:
    """The final working day of ``day``'s ISO week — when the weekly Tempo nag fires.

    Not "Friday": a Friday holiday makes it Thursday, and a Saturday worked in exchange
    for a holiday makes it that Saturday. None for a week with no working day at all.
    """
    monday = start_of_week(day)
    for offset in range(6, -1, -1):
        cursor = monday + timedelta(days=offset)
        if spec.is_working_day(cursor):
            return cursor
    return None


def roll_forward_to_working_day(spec: CalendarSpec, day: date, *, limit: int = 30) -> date | None:
    """``day`` itself if it works, else the next working day."""
    if spec.is_working_day(day):
        return day
    return next_working_day(spec, day, limit=limit)
