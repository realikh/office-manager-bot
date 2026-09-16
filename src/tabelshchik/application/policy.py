"""Policy the use cases obey, as plain dataclasses.

Deliberately not the pydantic config models: bootstrap maps YAML onto these, which keeps
every use case runnable in a test without a config file, and keeps the config *format*
free to change without touching the rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from tabelshchik.domain.entities import AbsencePolicy
from tabelshchik.domain.instance import (
    DEFAULT_MAX_DAYS_PER_WEEK,
    DEFAULT_SURPLUS_CLAMP_DAYS,
)


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    horizon_weeks: int = 6
    #: Weeks from the horizon anchor that a regeneration may not touch. Announced days
    #: are frozen regardless; this additionally protects the imminent, unannounced ones
    #: so next week does not reshuffle under people who have already planned around it.
    freeze_weeks: int = 1
    max_days_per_week: int = DEFAULT_MAX_DAYS_PER_WEEK
    surplus_clamp_days: int = DEFAULT_SURPLUS_CLAMP_DAYS
    absence_policy: AbsencePolicy = AbsencePolicy.NO_DEBT


@dataclass(frozen=True, slots=True)
class TempoPolicy:
    """When to nag about filling in Tempo.

    On the last working day of the week, and on the last working day of the month — one
    message when the two coincide. There used to be a third, an early warning around the
    23rd; it was dropped as one nag too many.
    """

    enabled: bool = True
    url: str = ""
    #: The month-end nag lands on the last *working* day, not the last calendar day.
    month_end: bool = True
    pin: bool = True


#: How close to the end of the workday the Tempo reminder has to land before it counts the
#: minutes down. Further out, "spend your last two hours on Tempo" is not advice anyone
#: would take, so the message just says its piece instead.
RUSH_WINDOW_MINUTES = 30


@dataclass(frozen=True, slots=True)
class ReminderTimes:
    """When the scheduled messages go out.

    These used to be in `app.yaml`, which made moving a reminder by ten minutes a deploy.
    They live in the database now, and these are the values until an admin changes one.
    """

    attendance: time = time(15, 30)
    tempo: time = time(17, 50)
    holiday: time = time(10, 0)
    extend: time = time(10, 0)
    #: 0 = Monday. The horizon extender is the one weekly job.
    extend_weekday: int = 3
    workday_end: time = time(18, 0)

    @property
    def tempo_gap_minutes(self) -> int | None:
        """Minutes left in the workday when Tempo goes out, if few enough to mention.

        None when the reminder is sent at or after the end of the day, or so early that a
        countdown would be silly.
        """
        gap = _minutes(self.workday_end) - _minutes(self.tempo)
        return gap if 0 < gap <= RUSH_WINDOW_MINUTES else None


def _minutes(moment: time) -> int:
    return moment.hour * 60 + moment.minute


@dataclass(frozen=True, slots=True)
class ChatPolicy:
    """What the chat may spend, and how much it may carry.

    Every context limit is a character budget rather than a token one, because characters
    are what can be enforced before the request is built. They exist so the worst-case
    prompt is known in advance instead of discovered on an invoice.
    """

    enabled: bool = True
    per_user_daily_limit: int = 10
    global_daily_limit: int = 200
    #: A ceiling, not a target. The model is told to answer as briefly as the question
    #: allows; this only has to leave room for the questions that need a real answer.
    max_tokens: int = 2000
    temperature: float = 1.0
    triggers: frozenset[str] = frozenset({"mention", "reply", "private"})
    #: How much of the person's own schedule to put in front of the model.
    upcoming_days: int = 5
    #: How far back a reply chain is followed. Telegram gives us one level; the rest is
    #: reconstructed from the message cache.
    reply_depth: int = 10
    reply_chars: int = 1200
    message_chars: int = 200
    #: Whether the model may write facts down at all.
    remember: bool = True
    general_facts: int = 20
    personal_facts: int = 10
    fact_chars: int = 120


@dataclass(frozen=True, slots=True)
class SilentPolicy:
    """Quiet hours, resolved per weekday. Silent still sends — it just does not buzz."""

    #: weekday -> (start, end) or None. An all-day rule is (00:00, 00:00).
    windows: tuple[tuple[time, time] | None, ...] = (None,) * 7

    def is_silent(self, weekday: int, moment: time) -> bool:
        window = self.windows[weekday]
        if window is None:
            return False
        start, end = window
        if start == end:
            return True  # all day
        if start <= end:
            return start <= moment < end
        return moment >= start or moment < end  # crosses midnight
