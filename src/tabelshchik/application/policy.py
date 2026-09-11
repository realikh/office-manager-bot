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
    """When to nag about filling in Tempo."""

    enabled: bool = True
    url: str = ""
    #: Weekdays for the recurring nag, 0 = Monday.
    weekly_weekdays: frozenset[int] = frozenset({4})
    #: Day of the month for the early warning; rolls forward if it is not a working day,
    #: and does not fire retroactively if a working day has already passed.
    warning_day: int = 23
    #: The month-end nag lands on the last *working* day, not the last calendar day.
    month_end: bool = True
    pin: bool = True


@dataclass(frozen=True, slots=True)
class ChatPolicy:
    enabled: bool = True
    per_user_daily_limit: int = 10
    global_daily_limit: int = 200
    max_tokens: int = 400
    temperature: float = 0.9
    triggers: frozenset[str] = frozenset({"mention", "reply", "private"})
    #: How much of the person's own schedule to put in front of the model.
    upcoming_days: int = 5


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
