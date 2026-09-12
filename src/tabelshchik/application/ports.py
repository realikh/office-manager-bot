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
from enum import StrEnum
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


class AdminRole(StrEnum):
    """Who may do what on the admin surface.

    The values are stored by `_enum` and matched literally by the single-owner index in
    `adapters/db/models.py`. Renaming one silently disarms that index.
    """

    #: Exactly one, always. May grant and revoke adminship, create and delete offices,
    #: and hand the whole bot over. Cannot be revoked — only transferred.
    OWNER = "OWNER"
    #: Everything else the admin menu offers, across every office.
    ADMIN = "ADMIN"


@dataclass(frozen=True, slots=True)
class AdminRecord:
    user_id: int
    role: AdminRole = AdminRole.ADMIN
    #: Cosmetic, so the list reads as names rather than numbers. Telegram display names
    #: are not stable, so this is a snapshot taken when adminship was granted.
    label: str = ""
    employee_id: str | None = None
    granted_at: datetime | None = None

    @property
    def is_owner(self) -> bool:
        return self.role is AdminRole.OWNER


class AdminStore(Protocol):
    """Who may use the admin surface.

    Adminship lives here rather than in the configuration because handing the bot to
    somebody else should not require an SSH session, and because an owner who wants to
    stop being an admin must be able to actually stop.
    """

    def role_of(self, user_id: int) -> AdminRole | None: ...
    def ids(self) -> frozenset[int]: ...
    def owner_id(self) -> int | None: ...
    def listing(self) -> Sequence[AdminRecord]: ...
    def count(self) -> int: ...
    def grant(
        self,
        user_id: int,
        *,
        role: AdminRole = AdminRole.ADMIN,
        label: str = "",
        employee_id: str | None = None,
        granted_by: int | None = None,
        at: datetime,
    ) -> bool: ...
    def revoke(self, user_id: int) -> bool: ...
    #: One transaction, demoting before promoting. Two calls would trip the single-owner
    #: index halfway through and could leave the bot with no owner at all.
    def transfer_ownership(self, *, to_user_id: int, at: datetime) -> int | None: ...


class OfficeAdminStore(Protocol):
    """Creating and retiring offices, as opposed to editing what is inside one.

    Apart from `RosterStore` because `delete_office` is a cascading hard delete, and
    `RosterStore` is documented as the things an admin changes about who works where.
    """

    def create_office(
        self, office_id: str, name: str, *, timezone: str, holiday_calendar: str
    ) -> bool: ...
    def rename_office(self, office_id: str, name: str) -> bool: ...
    def set_holiday_calendar(self, office_id: str, code: str) -> bool: ...
    def set_active(self, office_id: str, active: bool) -> bool: ...
    #: Cascades away every employee, assignment and ledger entry the office ever had.
    #: Only reachable behind a typed confirmation.
    def delete_office(self, office_id: str) -> bool: ...


class OfficeStore(Protocol):
    def active_offices(self) -> Sequence[Office]: ...
    #: Closed offices too. The admin list needs them, or a closed office cannot be
    #: reopened, and an id it still holds could be handed out to a new office.
    def all_offices(self) -> Sequence[Office]: ...
    def get_office(self, office_id: str) -> Office | None: ...
    def get_employee(self, employee_id: str) -> Employee | None: ...
    #: Which office someone belongs to. Deliberately *not* filtered by `active`: the
    #: callers that care check it themselves, where the rule is visible.
    def office_of(self, employee_id: str) -> str | None: ...
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
    def has_schedule_from(self, office_id: str, start: date) -> bool: ...
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


class AbsenceStore(Protocol):
    def add(
        self,
        employee_id: str,
        start: date,
        end: date,
        *,
        kind: str,
        note: str = "",
        actor_id: int | None = None,
        at: datetime,
    ) -> int: ...

    def remove(self, absence_id: int) -> bool: ...
    def for_employee(
        self, employee_id: str, *, upcoming_from: date | None = None
    ) -> Sequence[tuple[int, date, date, str]]: ...
    def get(self, absence_id: int) -> tuple[int, str, date, date] | None: ...


class RosterStore(Protocol):
    """Everything an admin can change about who works where."""

    def add_employee(
        self,
        office_id: str,
        employee_id: str,
        full_name: str,
        *,
        username: str | None = None,
        gender: str = "male",
        team_id: str | None = None,
        started_on: date | None = None,
    ) -> None: ...

    def remove_employee(self, employee_id: str, *, ended_on: date | None = None) -> bool: ...
    def restore_employee(self, employee_id: str) -> bool: ...
    def rename_employee(self, employee_id: str, full_name: str) -> bool: ...
    def set_username(self, employee_id: str, username: str | None) -> bool: ...
    def set_vacant_desks(self, office_id: str, weekday: int, desks: int) -> None: ...
    def toggle_fixed(self, office_id: str, weekday: int, employee_id: str) -> bool: ...
    def bump_seed(self, office_id: str) -> int: ...
    def set_chat_id(self, office_id: str, chat_id: int | None) -> None: ...


class MaintenanceStore(Protocol):
    """Deletion, kept apart from the stores that only ever add.

    Pruning is the one operation that can destroy information, so it lives behind its
    own port rather than being one more method on a repository everything already holds.
    """

    def delete_schedule_before(self, office_id: str, watermark: date) -> int: ...
    def delete_job_runs_before(self, cutoff: datetime) -> int: ...
    def delete_audit_before(self, cutoff: datetime) -> int: ...
    def delete_ai_usage_before(self, cutoff: date) -> int: ...
    def delete_chat_messages_before(self, cutoff: datetime) -> int: ...
    def delete_chat_memory_before(self, cutoff: datetime) -> int: ...
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


@dataclass(frozen=True, slots=True)
class Completion:
    """One model reply, with what it cost.

    The token counts come back from the API on every call and used to be thrown away, so
    ``ai_usage.tokens`` was always zero and nothing in the system could answer "how much
    is this costing". Carrying them here is what makes the admin status screen honest.
    """

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatModel(Protocol):
    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        json_object: bool = False,
    ) -> Completion | None:
        """Returns None rather than raising — every caller has a working fallback."""
        ...


class UsageStore(Protocol):
    """Daily AI usage counters, per person and in total."""

    def used_today(self, user_id: int, day: date) -> int: ...
    def total_today(self, day: date) -> int: ...
    def record(self, user_id: int, day: date, *, tokens: int = 0) -> None: ...
    def tokens_today(self, day: date) -> int: ...


@dataclass(frozen=True, slots=True)
class CachedMessage:
    """One message the bot saw, kept only so a reply chain can be walked back up."""

    message_id: int
    author: str
    text: str
    reply_to_message_id: int | None = None


class MessageCache(Protocol):
    """Recent chat messages, addressed by Telegram's own ids.

    Telegram populates ``reply_to_message`` exactly one level deep, so following a thread
    any further means having kept the messages ourselves.
    """

    def remember(self, chat_id: int, message: CachedMessage, *, at: datetime) -> None: ...

    def chain(self, chat_id: int, message_id: int, *, depth: int) -> Sequence[CachedMessage]:
        """The reply chain ending at ``message_id``, oldest first, root excluded if
        missing. Stops early at the first link that was never cached."""
        ...


@dataclass(frozen=True, slots=True)
class MemoryFact:
    scope: str
    subject: str
    fact: str


class ChatMemoryStore(Protocol):
    """What the bot has chosen to remember, as short facts in two scopes.

    ``office`` facts are shared; ``employee`` facts are shown only to the person they are
    about. Both are ring buffers — remembering something new is what forgets something
    old, so the prompt has a fixed maximum size.
    """

    def facts(self, scope: str, subject: str, *, limit: int) -> Sequence[MemoryFact]: ...

    def remember(self, scope: str, subject: str, fact: str, *, keep: int, at: datetime) -> None: ...

    def forget_all(self, scope: str, subject: str) -> int: ...


class MoodStore(Protocol):
    def mood_for(self, office_id: str, day: date) -> str | None: ...
    def set_mood(self, office_id: str, day: date, mood: str, *, by: str = "roll") -> None: ...


class AuditLog(Protocol):
    def record(self, *, actor_id: int | None, action: str, payload: dict[str, object]) -> None: ...
