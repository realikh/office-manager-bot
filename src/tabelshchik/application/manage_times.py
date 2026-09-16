"""When the scheduled messages go out, changed from the bot.

These used to be `app.yaml` keys, so moving a reminder by ten minutes took a commit and a
deploy. They are rows in `setting` now. A missing row means the default in
`ReminderTimes`, so a default changed in a release still reaches every deployment that
has not deliberately moved away from it.

A change is applied to the running scheduler straight away. Moving a time on a day it
has already fired is safe: every job either checks whether its work is already done
(the attendance fingerprint, the regeneration diff) or records that it is (`PostStore`).
"""

from __future__ import annotations

import re
from datetime import time

from tabelshchik.application.policy import ReminderTimes
from tabelshchik.application.ports import (
    AuditLog,
    OfficeJobs,
    ScheduleTime,
    SettingsStore,
)

_TIME = re.compile(r"^\s*(\d{1,2})\s*[:.]\s*(\d{2})\s*$")
_EXTEND = re.compile(r"^\s*(?:([^\d\s]+)\s+)?(.+?)\s*$")


class TimeError(ValueError):
    """The requested change makes no sense. The message is shown to the admin."""


def reminder_times(settings: SettingsStore, defaults: ReminderTimes | None = None) -> ReminderTimes:
    """What is in force: stored overrides over the defaults. Read uncached, on purpose."""
    base = defaults or ReminderTimes()

    def pick(which: ScheduleTime, default: time) -> time:
        stored = settings.schedule_time(which)
        return stored if stored is not None else default

    weekday = settings.extend_weekday()
    return ReminderTimes(
        attendance=pick(ScheduleTime.ATTENDANCE, base.attendance),
        tempo=pick(ScheduleTime.TEMPO, base.tempo),
        holiday=pick(ScheduleTime.HOLIDAY, base.holiday),
        extend=pick(ScheduleTime.EXTEND, base.extend),
        extend_weekday=weekday if weekday is not None else base.extend_weekday,
        workday_end=pick(ScheduleTime.WORKDAY_END, base.workday_end),
    )


def parse_time(raw: str) -> time | None:
    """`18:00`, `9:30` or `9.30`. None for anything else."""
    match = _TIME.match(raw)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def parse_extend(raw: str, weekdays_short: tuple[str, ...]) -> tuple[int | None, time] | None:
    """`чт 10:00`, or just `10:00` to keep the day. None if either half is unreadable.

    ``weekdays_short`` is the catalog's `пн`…`вс`, so the accepted spelling is whatever
    the bot itself shows.
    """
    match = _EXTEND.match(raw)
    if match is None:
        return None
    day_text, time_text = match.group(1), match.group(2)
    moment = parse_time(time_text)
    if moment is None:
        return None
    if day_text is None:
        return None, moment
    names = [name.casefold() for name in weekdays_short]
    day = day_text.casefold().rstrip(".")
    if day not in names:
        return None
    return names.index(day), moment


def set_time(
    *,
    which: ScheduleTime,
    value: time | None,
    actor_id: int,
    settings: SettingsStore,
    jobs: OfficeJobs | None = None,
    audit: AuditLog | None = None,
) -> None:
    """Store one time, or `None` to go back to the default, and reschedule."""
    settings.set_schedule_time(which, value)
    _after_change(
        jobs,
        audit,
        actor_id,
        {"which": which.value, "value": value.strftime("%H:%M") if value else None},
    )


def set_extend(
    *,
    weekday: int | None,
    value: time | None,
    actor_id: int,
    settings: SettingsStore,
    jobs: OfficeJobs | None = None,
    audit: AuditLog | None = None,
) -> None:
    """The horizon extender's day and time. `weekday=None` keeps the day as it is.

    Resetting (`value=None`) resets both, since "the default time on some other day" is
    not something anyone asks for.
    """
    if weekday is not None and not 0 <= weekday <= 6:
        raise TimeError("Нет такого дня недели.")
    if value is None:
        settings.set_extend_weekday(None)
    elif weekday is not None:
        settings.set_extend_weekday(weekday)
    settings.set_schedule_time(ScheduleTime.EXTEND, value)
    _after_change(
        jobs,
        audit,
        actor_id,
        {
            "which": ScheduleTime.EXTEND.value,
            "weekday": weekday,
            "value": value.strftime("%H:%M") if value else None,
        },
    )


def _after_change(
    jobs: OfficeJobs | None, audit: AuditLog | None, actor_id: int, payload: dict[str, object]
) -> None:
    # Without this the new time is stored and the scheduler keeps firing at the old one
    # until the next deploy — the "looks configured, does nothing" failure again.
    if jobs is not None:
        jobs.reschedule()
    if audit is not None:
        audit.record(actor_id=actor_id, action="schedule.time", payload=payload)
