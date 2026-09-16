"""Delivery times, moved out of app.yaml and into the bot.

What breaks in production without these: a time an admin typed that is stored and never
applied, a reset that leaves yesterday's override behind, or a Tempo reminder that tells
people to spend their last two hours on it.
"""

from __future__ import annotations

from datetime import time

import pytest

from tabelshchik.adapters.db.repositories import SqlAuditLog, SqlSettingsStore
from tabelshchik.application.manage_times import (
    TimeError,
    parse_extend,
    parse_time,
    reminder_times,
    set_extend,
    set_time,
)
from tabelshchik.application.policy import ReminderTimes
from tabelshchik.application.ports import ScheduleTime

WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


class CountingJobs:
    def __init__(self) -> None:
        self.rescheduled = 0

    def add_office(self, office_id: str) -> None: ...
    def drop_office(self, office_id: str) -> None: ...

    def reschedule(self) -> None:
        self.rescheduled += 1


# ---------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("17:50", time(17, 50)),
        ("9:30", time(9, 30)),
        ("9.30", time(9, 30)),
        (" 07:05 ", time(7, 5)),
        ("00:00", time(0, 0)),
        ("23:59", time(23, 59)),
    ],
)
def test_a_time_of_day_is_read(raw: str, expected: time) -> None:
    assert parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "1750", "24:00", "12:60", "12:5", "-1:00", "12:00:00"])
def test_anything_else_is_refused(raw: str) -> None:
    assert parse_time(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("чт 10:00", (3, time(10, 0))),
        ("Пт 9:15", (4, time(9, 15))),
        ("пн. 08:00", (0, time(8, 0))),
        ("10:30", (None, time(10, 30))),
    ],
)
def test_the_extension_takes_a_day_and_a_time(raw: str, expected) -> None:
    assert parse_extend(raw, WEEKDAYS_SHORT) == expected


@pytest.mark.parametrize("raw", ["четверг 10:00", "чт", "чт 25:00", "xx 10:00", ""])
def test_an_unreadable_extension_is_refused(raw: str) -> None:
    assert parse_extend(raw, WEEKDAYS_SHORT) is None


# ---------------------------------------------------------------------------- storing


def test_nothing_stored_means_the_defaults(sessions) -> None:
    assert reminder_times(SqlSettingsStore(sessions)) == ReminderTimes()


def test_the_defaults_are_the_ones_asked_for() -> None:
    times = ReminderTimes()
    assert times.tempo == time(17, 50)
    assert times.workday_end == time(18, 0)
    assert times.attendance == time(15, 30)
    assert (times.extend_weekday, times.extend) == (3, time(10, 0))


def test_a_stored_time_is_what_is_in_force(sessions) -> None:
    settings = SqlSettingsStore(sessions)
    jobs = CountingJobs()

    set_time(which=ScheduleTime.TEMPO, value=time(17, 20), actor_id=1, settings=settings, jobs=jobs)

    assert reminder_times(settings).tempo == time(17, 20)
    assert reminder_times(settings).attendance == ReminderTimes().attendance
    assert jobs.rescheduled == 1


def test_a_reset_goes_back_to_the_default(sessions) -> None:
    settings = SqlSettingsStore(sessions)
    set_time(which=ScheduleTime.HOLIDAY, value=time(12, 0), actor_id=1, settings=settings)

    set_time(which=ScheduleTime.HOLIDAY, value=None, actor_id=1, settings=settings)

    assert reminder_times(settings).holiday == ReminderTimes().holiday


def test_a_change_is_audited(sessions) -> None:
    from sqlalchemy import select

    from tabelshchik.adapters.db import models
    from tabelshchik.adapters.db.engine import session_scope

    set_time(
        which=ScheduleTime.ATTENDANCE,
        value=time(16, 0),
        actor_id=7,
        settings=SqlSettingsStore(sessions),
        audit=SqlAuditLog(sessions),
    )

    with session_scope(sessions) as session:
        [row] = session.scalars(select(models.AuditLog)).all()
        assert (row.actor_id, row.action) == (7, "schedule.time")
        assert row.payload == {"which": "time.attendance", "value": "16:00"}


def test_a_time_alone_keeps_the_extension_day(sessions) -> None:
    settings = SqlSettingsStore(sessions)
    set_extend(weekday=0, value=time(9, 0), actor_id=1, settings=settings)

    set_extend(weekday=None, value=time(11, 0), actor_id=1, settings=settings)

    times = reminder_times(settings)
    assert (times.extend_weekday, times.extend) == (0, time(11, 0))


def test_resetting_the_extension_resets_the_day_too(sessions) -> None:
    settings = SqlSettingsStore(sessions)
    set_extend(weekday=0, value=time(9, 0), actor_id=1, settings=settings)

    set_extend(weekday=None, value=None, actor_id=1, settings=settings)

    times = reminder_times(settings)
    assert (times.extend_weekday, times.extend) == (3, time(10, 0))


def test_there_is_no_eighth_weekday(sessions) -> None:
    with pytest.raises(TimeError):
        set_extend(weekday=7, value=time(9, 0), actor_id=1, settings=SqlSettingsStore(sessions))


def test_a_corrupt_stored_value_reads_as_the_default(sessions) -> None:
    """Not written by this code, but a crash in every job that reads it is worse."""
    settings = SqlSettingsStore(sessions)
    settings._set(ScheduleTime.TEMPO.value, 99999)
    settings._set(settings.EXTEND_WEEKDAY, 12)

    times = reminder_times(settings)
    assert times.tempo == ReminderTimes().tempo
    assert times.extend_weekday == ReminderTimes().extend_weekday


# ------------------------------------------------------------------ the tempo countdown


@pytest.mark.parametrize(
    ("tempo", "end", "gap"),
    [
        (time(17, 50), time(18, 0), 10),
        (time(17, 30), time(18, 0), 30),
        (time(17, 59), time(18, 0), 1),
        (time(17, 20), time(18, 0), None),  # too early to count down
        (time(18, 0), time(18, 0), None),  # the day is already over
        (time(18, 10), time(18, 0), None),
        (time(8, 45), time(9, 0), 15),
    ],
)
def test_the_countdown_only_applies_in_the_last_half_hour(tempo, end, gap) -> None:
    assert ReminderTimes(tempo=tempo, workday_end=end).tempo_gap_minutes == gap
