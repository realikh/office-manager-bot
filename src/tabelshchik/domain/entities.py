"""Core domain entities.

All dates are plain civil dates (``datetime.date``), never UTC instants. Scheduling
systems that carry timezone-aware instants around develop off-by-one-day bugs at
midnight boundaries; a "workday" is a calendar concept, so we keep it one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

#: Fixed-point scale for entitlement and surplus. Floats accumulate drift over a year
#: of daily accrual, which is exactly what makes a fairness claim unfalsifiable.
SCALE = 1_000_000

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)

WEEKDAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_BY_NAME: Mapping[str, int] = {name: i for i, name in enumerate(WEEKDAY_NAMES)}


class Gender(StrEnum):
    MALE = "male"
    FEMALE = "female"


class AbsenceKind(StrEnum):
    VACATION = "vacation"
    SICK = "sick"
    TRIP = "trip"
    OTHER = "other"


class AssignmentSource(StrEnum):
    FIXED = "fixed"
    DRAFTED = "drafted"
    MANUAL = "manual"


class AssignmentStatus(StrEnum):
    PROVISIONAL = "provisional"
    ANNOUNCED = "announced"
    CANCELLED = "cancelled"


class AbsencePolicy(StrEnum):
    #: Accrue no entitlement while absent, so a returner resumes a normal share.
    NO_DEBT = "NO_DEBT"
    #: Accrue entitlement while absent, so a returner must catch up.
    ACCRUE_DEBT = "ACCRUE_DEBT"


@dataclass(frozen=True, slots=True)
class Employee:
    id: str
    full_name: str
    telegram_username: str | None = None
    telegram_user_id: int | None = None
    gender: Gender = Gender.MALE
    team_id: str | None = None
    started_on: date | None = None
    #: Inclusive. ``None`` means still employed.
    ended_on: date | None = None

    def in_tenure(self, day: date) -> bool:
        if self.started_on is not None and day < self.started_on:
            return False
        return not (self.ended_on is not None and day > self.ended_on)


@dataclass(frozen=True, slots=True)
class Absence:
    employee_id: str
    start_date: date
    #: Inclusive, so a single-day absence has ``start_date == end_date``.
    end_date: date
    kind: AbsenceKind = AbsenceKind.VACATION
    note: str = ""

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise ValueError(
                f"absence for {self.employee_id} ends ({self.end_date}) "
                f"before it starts ({self.start_date})"
            )

    def covers(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date

    def overlaps(self, start: date, end: date) -> bool:
        return self.start_date <= end and start <= self.end_date


@dataclass(frozen=True, slots=True)
class WeeklyTemplate:
    """The weekly shape of an office.

    ``fixed`` lists people who always come in on that weekday. ``vacant_desks`` is the
    number of *additional* people to draft on top of them — not the day's total capacity.
    """

    fixed: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    vacant_desks: Mapping[int, int] = field(default_factory=dict)

    def fixed_on(self, weekday: int) -> tuple[str, ...]:
        return self.fixed.get(weekday, ())

    def desks_on(self, weekday: int) -> int:
        return self.vacant_desks.get(weekday, 0)

    @property
    def drafts_at_all(self) -> bool:
        return any(n > 0 for n in self.vacant_desks.values())


@dataclass(frozen=True, slots=True)
class Office:
    id: str
    name: str
    chat_id: int | None = None
    timezone: str = "Asia/Almaty"
    holiday_calendar: str = "KZ"
    seed_nonce: int = 0
    active: bool = True


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Persisted fairness state for one employee. All quantities are fixed-point."""

    employee_id: str
    days: int = 0
    entitlement_scaled: int = 0
    per_weekday: tuple[int, ...] = (0, 0, 0, 0, 0, 0, 0)

    @property
    def surplus_scaled(self) -> int:
        """How far ahead (positive) or behind (negative) this person is, in days."""
        return self.days * SCALE - self.entitlement_scaled


@dataclass(frozen=True, slots=True)
class Assignment:
    office_id: str
    day: date
    employee_id: str
    source: AssignmentSource
    status: AssignmentStatus = AssignmentStatus.PROVISIONAL

    @property
    def is_live(self) -> bool:
        return self.status is not AssignmentStatus.CANCELLED


def absent_on(absences: Iterable[Absence], employee_id: str, day: date) -> bool:
    return any(a.employee_id == employee_id and a.covers(day) for a in absences)
