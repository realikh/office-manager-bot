"""Typed configuration.

Every model forbids unknown keys. A typo'd setting is a startup error rather than a
silent fallback to a default — the previous system failed open on typos, which is how a
setting can appear configured and do nothing for months.
"""

from __future__ import annotations

from datetime import date
from datetime import time as clock_time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from tabelshchik.domain.entities import WEEKDAY_BY_NAME, AbsencePolicy, Gender

WeekdayName = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LongWeekdayName = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]
Mood = Literal["toxic", "fun", "happy", "sad", "depressive"]

Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]
AiTrigger = Literal["mention", "reply", "private"]
TelegramUsername = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{5,32}$")]


class Base(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
        frozen=True,
    )


def weekday_number(name: str) -> int:
    return WEEKDAY_BY_NAME[name[:3].lower()]


_DEFAULT_RUN_ON: list[WeekdayName] = ["mon", "tue", "wed", "thu", "fri", "sun"]
_DEFAULT_TRIGGERS: list[AiTrigger] = ["mention", "reply", "private"]


# --------------------------------------------------------------------------- app.yaml


class SilentWindow(Base):
    """A quiet period, which may cross midnight (20:00 -> 08:00)."""

    start: clock_time = Field(alias="from")
    end: clock_time = Field(alias="to")

    def covers(self, moment: clock_time) -> bool:
        if self.start <= self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end


SilentRule = SilentWindow | Literal["all-day"] | None


class SilentHours(Base):
    """Quiet hours per weekday, falling back to ``default``.

    Silent means the message still arrives — it just does not buzz anyone at 21:00.
    """

    default: SilentRule = None
    monday: SilentRule = None
    tuesday: SilentRule = None
    wednesday: SilentRule = None
    thursday: SilentRule = None
    friday: SilentRule = None
    saturday: SilentRule = None
    sunday: SilentRule = None

    def rule_for(self, weekday: int) -> SilentRule:
        names: tuple[LongWeekdayName, ...] = (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        )
        specific = getattr(self, names[weekday])
        return specific if specific is not None else self.default

    def is_silent(self, weekday: int, moment: clock_time) -> bool:
        rule = self.rule_for(weekday)
        if rule is None:
            return False
        if rule == "all-day":
            return True
        assert isinstance(rule, SilentWindow)
        return rule.covers(moment)


class AttendanceReminder(Base):
    time: clock_time
    #: Which weekdays the job runs. Including `sun` is what covers Sunday -> Monday;
    #: Friday -> Monday needs nothing special, it is just the next working day.
    run_on: list[WeekdayName] = Field(default_factory=lambda: list(_DEFAULT_RUN_ON))
    pin: bool = False

    @property
    def weekdays(self) -> frozenset[int]:
        return frozenset(weekday_number(name) for name in self.run_on)


class RemindersSection(Base):
    attendance: AttendanceReminder


class AutoExtend(Base):
    weekday: WeekdayName = "thu"
    time: clock_time = clock_time(10, 0)

    @property
    def weekday_number(self) -> int:
        return weekday_number(self.weekday)


class ScheduleSection(Base):
    horizon_weeks: int = Field(default=6, ge=1, le=52)
    freeze_weeks: int = Field(default=1, ge=0, le=8)
    max_days_per_week: int = Field(default=5, ge=1, le=7)
    auto_extend: AutoExtend = AutoExtend()
    holiday_calendar: str = "KZ"
    absence_policy: AbsencePolicy = AbsencePolicy.NO_DEBT
    surplus_clamp: int = Field(default=10, ge=1, le=365)

    @model_validator(mode="after")
    def freeze_fits_in_horizon(self) -> ScheduleSection:
        if self.freeze_weeks >= self.horizon_weeks:
            raise ValueError(
                f"freezeWeeks ({self.freeze_weeks}) must be smaller than "
                f"horizonWeeks ({self.horizon_weeks}), or nothing is ever re-planned"
            )
        return self


class RetentionSection(Base):
    schedule_months: int = Field(default=3, ge=1, le=120)
    job_runs_days: int = Field(default=90, ge=1)
    audit_days: int = Field(default=180, ge=1)
    ai_usage_days: int = Field(default=60, ge=1)
    #: Raw chat messages exist only to reconstruct a reply chain, which nobody follows
    #: back more than a few days. Keeping them longer stores other people's conversation
    #: for no benefit.
    chat_messages_days: int = Field(default=7, ge=1, le=90)
    #: Remembered facts are bounded by count already (a ring buffer per scope), but not by
    #: age. Holding something the bot learned about a person indefinitely is not a thing
    #: to do by omission.
    chat_memory_days: int = Field(default=90, ge=1, le=730)


class MoodWeight(Base):
    weight: int = Field(ge=0)


_DEFAULT_MOODS: dict[Mood, MoodWeight] = {
    "toxic": MoodWeight(weight=55),
    "fun": MoodWeight(weight=25),
    "happy": MoodWeight(weight=12),
    "sad": MoodWeight(weight=5),
    "depressive": MoodWeight(weight=3),
}


class PersonalitySection(Base):
    enabled: bool = True
    rotation: Literal["daily", "per-message"] = "daily"
    moods: dict[Mood, MoodWeight] = Field(default_factory=lambda: dict(_DEFAULT_MOODS))
    #: Forces the gentlest mood everywhere, immediately.
    safe_mode: bool = False
    #: Rejected outright in generated text, on top of the built-in checks.
    banned_terms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def at_least_one_mood_can_occur(self) -> PersonalitySection:
        if sum(mood.weight for mood in self.moods.values()) <= 0:
            raise ValueError("at least one mood must have a weight above zero")
        return self


class ChatContextSection(Base):
    """How much conversation the chat carries, and what it is allowed to remember.

    Every limit is a *character* budget rather than a token one, because characters are
    what the code can actually enforce before the request is built. The point of them all
    is the same: the prompt must have a bounded worst case, so a long thread or a chatty
    week cannot quietly multiply the bill.
    """

    #: How far back a reply chain is followed. Telegram only ever hands us one level, so
    #: the rest is reconstructed from the message cache.
    reply_depth: int = Field(default=10, ge=0, le=25)
    #: Ceiling on the whole quoted chain.
    reply_chars: int = Field(default=1200, ge=0, le=8000)
    #: Ceiling on any single quoted message within it.
    message_chars: int = Field(default=200, ge=40, le=1000)
    #: Whether the model may write things down at all.
    remember: bool = True
    #: Ring-buffer sizes. Oldest fact is evicted when a new one arrives.
    general_facts: int = Field(default=20, ge=0, le=100)
    personal_facts: int = Field(default=10, ge=0, le=50)
    fact_chars: int = Field(default=120, ge=20, le=500)


class AiSection(Base):
    enabled: bool = True
    model: str = "gpt-4.1-nano"
    max_tokens: int = Field(default=400, ge=1, le=4000)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    per_user_daily_limit: int = Field(default=10, ge=0)
    global_daily_limit: int = Field(default=200, ge=0)
    triggers: list[AiTrigger] = Field(default_factory=lambda: list(_DEFAULT_TRIGGERS))
    request_timeout_seconds: float = Field(default=20.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)
    chat: ChatContextSection = ChatContextSection()


class HealthSection(Base):
    #: Optional dead-man's switch (healthchecks.io free tier). Empty disables it.
    ping_url: str = ""
    ping_interval_minutes: int = Field(default=15, ge=1)
    #: Touched by the bot's own event loop, and read by the container healthcheck. A
    #: fresh file is the only evidence that the loop is actually turning — a process
    #: that starts and then wedges looks identical from the outside.
    heartbeat_file: str = "/data/heartbeat"
    #: How old the file may get before the container is considered unhealthy. Comfortably
    #: more than the tick interval, so one slow iteration is not a false alarm.
    heartbeat_stale_seconds: int = Field(default=180, ge=30)
    #: The file is touched at least this often, regardless of the external ping interval:
    #: a healthcheck that has to wait 15 minutes for a verdict is no use to a deploy.
    heartbeat_interval_seconds: int = Field(default=30, ge=5, le=300)
    #: How far back the startup sweep will re-run occurrences that were missed.
    catch_up_grace_hours: int = Field(default=12, ge=0, le=72)
    nightly_backup: bool = True


class AppConfig(Base):
    timezone: str = "Asia/Almaty"
    admins: list[int] = Field(default_factory=list)
    reminders: RemindersSection
    silent_hours: SilentHours = SilentHours()
    schedule: ScheduleSection = ScheduleSection()
    retention: RetentionSection = RetentionSection()
    personality: PersonalitySection = PersonalitySection()
    ai: AiSection = AiSection()
    health: HealthSection = HealthSection()


# ------------------------------------------------------------------- offices/<id>.yaml


class EmployeeSeed(Base):
    id: Identifier
    name: str = Field(min_length=1)
    telegram_username: TelegramUsername | None = None
    telegram_user_id: int | None = None
    gender: Gender = Gender.MALE
    team_id: Identifier | None = None
    started_on: date | None = None
    #: Inclusive last working day. The old config called this `lastWorkDate`.
    ended_on: date | None = None

    @model_validator(mode="after")
    def tenure_is_ordered(self) -> EmployeeSeed:
        if self.started_on and self.ended_on and self.ended_on < self.started_on:
            raise ValueError(f"{self.id}: endedOn is before startedOn")
        return self


class AbsenceSeed(Base):
    employee_id: Identifier
    start_date: date
    end_date: date
    kind: Literal["vacation", "sick", "trip", "other"] = "vacation"
    note: str = ""

    @model_validator(mode="after")
    def range_is_ordered(self) -> AbsenceSeed:
        if self.end_date < self.start_date:
            raise ValueError(f"{self.employee_id}: absence ends before it starts")
        return self


class CalendarSeed(Base):
    #: Office shut for a reason that is not a public holiday.
    closed: list[date] = Field(default_factory=list)
    #: Working Saturdays. Kazakhstan routinely transfers holidays onto one.
    extra_workdays: list[date] = Field(default_factory=list)
    notes: dict[date, str] = Field(default_factory=dict)


class ScheduleSeed(Base):
    """The weekly shape of the office.

    ``vacantDesks`` is how many people to draft *in addition to* the fixed list for that
    weekday — not the day's total capacity.
    """

    fixed: dict[LongWeekdayName, list[Identifier]] = Field(default_factory=dict)
    vacant_desks: dict[LongWeekdayName, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def desks_are_not_negative(self) -> ScheduleSeed:
        for day, count in self.vacant_desks.items():
            if count < 0:
                raise ValueError(f"vacantDesks.{day} is negative")
        return self

    @property
    def fixed_by_weekday(self) -> dict[int, tuple[str, ...]]:
        return {weekday_number(day): tuple(ids) for day, ids in self.fixed.items()}

    @property
    def desks_by_weekday(self) -> dict[int, int]:
        return {weekday_number(day): count for day, count in self.vacant_desks.items()}


class OfficeSeed(Base):
    """Bootstrap data for one office.

    Loaded into the database on first run. After that the database is authoritative and
    this file is a reproducible starting point, not a live source of truth.
    """

    id: Identifier
    name: str = Field(min_length=1)
    address: str = ""
    chat_id: int | None = None
    timezone: str = "Asia/Almaty"
    holiday_calendar: str = "KZ"
    seed_nonce: int = 0
    active: bool = True
    schedule: ScheduleSeed = ScheduleSeed()
    calendar: CalendarSeed = CalendarSeed()
    employees: list[EmployeeSeed] = Field(default_factory=list)
    absences: list[AbsenceSeed] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_resolve(self) -> OfficeSeed:
        known = {employee.id for employee in self.employees}

        duplicates = [
            employee.id
            for employee in self.employees
            if sum(1 for other in self.employees if other.id == employee.id) > 1
        ]
        if duplicates:
            raise ValueError(f"duplicate employee ids: {sorted(set(duplicates))}")

        for day, ids in self.schedule.fixed.items():
            unknown = sorted(set(ids) - known)
            if unknown:
                raise ValueError(f"schedule.fixed.{day} names unknown employees: {unknown}")
            if len(set(ids)) != len(ids):
                raise ValueError(f"schedule.fixed.{day} lists someone twice")

        unknown_absences = sorted({absence.employee_id for absence in self.absences} - known)
        if unknown_absences:
            raise ValueError(f"absences name unknown employees: {unknown_absences}")

        return self
