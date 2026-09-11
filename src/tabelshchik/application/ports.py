"""Ports: what the use cases need from the world.

These are Protocols rather than base classes, so an adapter satisfies one by having the
right shape. They exist because the layering contract forbids ``application`` from
importing ``adapters`` — which is what keeps the use cases testable without a database,
a network or a clock, and what makes the storage and messaging choices replaceable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Protocol, runtime_checkable

from tabelshchik.domain.calendar import CalendarSpec
from tabelshchik.domain.entities import (
    Absence,
    Assignment,
    AssignmentStatus,
    Employee,
    LedgerEntry,
    Office,
    WeeklyTemplate,
)
from tabelshchik.domain.ledger import DayRecord


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """Everything the solver needs about one office, as domain entities."""

    office: Office
    template: WeeklyTemplate
    employees: tuple[Employee, ...]
    absences: tuple[Absence, ...]
    spec: CalendarSpec

    def employee(self, employee_id: str) -> Employee | None:
        return next((e for e in self.employees if e.id == employee_id), None)


@dataclass(frozen=True, slots=True)
class DaySnapshot:
    """A stored office day, as the reminder and report paths see it."""

    office_id: str
    day: date
    status: str
    desks_required: int
    shortfall: int
    roster_fingerprint: str
    assignments: tuple[Assignment, ...] = ()

    @property
    def is_announced(self) -> bool:
        return self.status == "ANNOUNCED"

    @property
    def roster(self) -> tuple[str, ...]:
        return tuple(sorted(a.employee_id for a in self.assignments if a.is_live))


@dataclass(frozen=True, slots=True)
class ScheduleDiff:
    """What a regeneration actually changed. Stored so churn is measurable."""

    added: tuple[Assignment, ...] = ()
    removed: tuple[Assignment, ...] = ()
    kept: int = 0
    days: tuple[DayRecord, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.removed


@runtime_checkable
class Clock(Protocol):
    """The only source of "now". Injected so tests can control time exactly."""

    def now(self) -> datetime: ...
    def today(self) -> date: ...
    def time_of_day(self) -> time: ...


class OfficeStore(Protocol):
    def active_offices(self) -> Sequence[Office]: ...
    def get_office(self, office_id: str) -> Office | None: ...
    def planning_context(self, office_id: str, *, start: date, end: date) -> PlanningContext: ...
    def employees(self, office_id: str) -> Sequence[Employee]: ...
    def find_employee_by_user_id(self, telegram_user_id: int) -> Employee | None: ...
    def find_employee_by_username(self, username: str) -> Employee | None: ...
    def link_telegram_user(self, employee_id: str, telegram_user_id: int) -> None: ...


class ScheduleStore(Protocol):
    def day(self, office_id: str, day: date) -> DaySnapshot | None: ...
    def days_between(self, office_id: str, start: date, end: date) -> Sequence[DaySnapshot]: ...
    def frozen_drafted(
        self, office_id: str, start: date, end: date
    ) -> dict[date, frozenset[str]]: ...
    def upcoming_for_employee(
        self, employee_id: str, *, start: date, limit: int
    ) -> Sequence[date]: ...
    def apply(self, office_id: str, diff: ScheduleDiff, *, generation_id: int) -> None: ...
    def mark_announced(
        self, office_id: str, day: date, *, fingerprint: str, at: datetime
    ) -> None: ...
    def set_assignment_status(
        self,
        office_id: str,
        day: date,
        employee_id: str,
        status: AssignmentStatus,
        *,
        reason: str | None = None,
    ) -> None: ...
    def record_generation(
        self,
        *,
        office_id: str,
        horizon_start: date,
        horizon_end: date,
        seed: int,
        input_hash: str,
        objective: str,
        diff: ScheduleDiff,
        shortfall: int,
        triggered_by: str,
        at: datetime,
    ) -> int: ...


class LedgerStore(Protocol):
    def entries(self, office_id: str) -> dict[str, LedgerEntry]: ...
    def save(self, entries: dict[str, LedgerEntry], *, as_of: date) -> None: ...
    def checkpoint(self) -> dict[str, LedgerEntry]: ...
    def save_checkpoint(self, entries: dict[str, LedgerEntry], *, watermark: date) -> None: ...
    def rebuild(self, office_id: str, *, upto: date) -> dict[str, LedgerEntry]: ...
    def day_records(self, office_id: str, *, upto: date) -> list[DayRecord]: ...


class MaintenanceStore(Protocol):
    """Deletion, kept apart from the stores that only ever add.

    Pruning is the one operation that can destroy information, so it lives behind its
    own port rather than being one more method on a repository everything already holds.
    """

    def delete_schedule_before(self, office_id: str, watermark: date) -> int: ...
    def delete_job_runs_before(self, cutoff: datetime) -> int: ...
    def delete_audit_before(self, cutoff: datetime) -> int: ...
    def delete_ai_usage_before(self, cutoff: date) -> int: ...
    def delete_absences_before(self, cutoff: date) -> int: ...
    def database_bytes(self) -> int: ...
    def vacuum(self) -> None: ...
    def backup_to(self, path: str) -> None: ...


class JobLedger(Protocol):
    """Durable idempotency for scheduled work."""

    def claim(
        self, occurrence_key: str, *, job: str, scheduled_for: datetime, late: bool = False
    ) -> bool:
        """True if this occurrence is ours to run; False if it already ran."""
        ...

    def complete(self, occurrence_key: str, *, at: datetime) -> None: ...
    def fail(self, occurrence_key: str, *, at: datetime, error: str) -> None: ...
    def recent(self, limit: int = 20) -> Sequence[tuple[str, str, datetime, str]]: ...


@dataclass(frozen=True, slots=True)
class SentMessage:
    chat_id: int
    message_id: int


class Notifier(Protocol):
    """Outbound messaging. Every send is explicit about whether it should make a noise."""

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        silent: bool = False,
        pin: bool = False,
        pin_kind: str | None = None,
    ) -> SentMessage | None: ...

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        *,
        caption: str = "",
        silent: bool = False,
    ) -> SentMessage | None: ...


class ChatModel(Protocol):
    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> str | None:
        """Returns None rather than raising — every caller has a working fallback."""
        ...


class AuditLog(Protocol):
    def record(self, *, actor_id: int | None, action: str, payload: dict[str, object]) -> None: ...
