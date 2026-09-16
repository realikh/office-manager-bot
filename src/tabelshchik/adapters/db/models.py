"""SQLAlchemy models.

Dates are stored as plain ``Date``, never as timestamps: a working day is a calendar
concept, and carrying instants around is how scheduling systems acquire off-by-one bugs
at midnight. Timestamps are used only for "when did this actually happen".
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# The only import here that is not a domain entity. Adminship is a Telegram-permissions
# concept, not a scheduling one, so it lives with the ports rather than in the domain.
from tabelshchik.application.ports import AdminRole, PostKind
from tabelshchik.domain.entities import (
    AbsenceKind,
    AssignmentSource,
    AssignmentStatus,
    Gender,
)


def _enum(enum_type: type[StrEnum]) -> SAEnum:
    """Store a StrEnum by value and read it back as the enum member.

    A plain String column round-trips to ``str``, which silently breaks every ``is``
    comparison against an enum member — and the code that filters assignments by source
    and status is built entirely out of those.
    """
    return SAEnum(
        enum_type,
        native_enum=False,
        length=16,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSON,
        list[str]: JSON,
    }


class Admin(Base):
    """Who may use the admin surface.

    The single-owner rule is a partial unique index rather than application-only
    discipline, because two owners is not a state anything here knows how to resolve.
    SQLite checks it per statement rather than at commit, which is why
    `SqlAdminStore.transfer_ownership` demotes before it promotes.

    The index matches `role = 'OWNER'` literally, so `AdminRole.OWNER` must keep that
    value: changing it disarms the index silently. And `migrations/env.py` renders in
    batch mode, so anything that rebuilds this table must re-create the predicate by
    hand — SQLite cannot alter an index in place.
    """

    __tablename__ = "admin"

    telegram_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[AdminRole] = mapped_column(_enum(AdminRole), default=AdminRole.ADMIN)
    #: Cosmetic, so the list reads as names rather than numbers. Snapshotted when
    #: adminship was granted; Telegram display names are not stable.
    label: Mapped[str] = mapped_column(String(128), default="")
    #: Set when adminship was granted by picking somebody off a roster. Deliberately not
    #: a foreign key: adminship has to survive the person being fired, and a cascade
    #: would revoke it as a silent side effect of a roster edit.
    employee_id: Mapped[str | None] = mapped_column(String(64), default=None)
    granted_by: Mapped[int | None] = mapped_column(Integer, default=None)
    granted_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (
        Index("uq_admin_single_owner", "role", unique=True, sqlite_where=text("role = 'OWNER'")),
    )


class Office(Base):
    __tablename__ = "office"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    address: Mapped[str] = mapped_column(String(256), default="")
    # Not unique: two offices may deliberately share one chat, in which case their
    # messages carry an office header so they stay distinguishable.
    chat_id: Mapped[int | None] = mapped_column(Integer, default=None)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Almaty")
    holiday_calendar: Mapped[str] = mapped_column(String(8), default="KZ")
    #: Bumped by an admin to reshuffle a week without changing anything else.
    seed_nonce: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    employees: Mapped[list[Employee]] = relationship(
        back_populates="office", cascade="all, delete-orphan"
    )


class Employee(Base):
    __tablename__ = "employee"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    office_id: Mapped[str] = mapped_column(ForeignKey("office.id", ondelete="CASCADE"))
    full_name: Mapped[str] = mapped_column(String(128))
    telegram_username: Mapped[str | None] = mapped_column(String(32), default=None)
    #: Numeric id, learned when the person first talks to the bot. Lets us mention people
    #: who have no @username, which an @-mention cannot do.
    telegram_user_id: Mapped[int | None] = mapped_column(Integer, unique=True, default=None)
    gender: Mapped[Gender] = mapped_column(_enum(Gender), default=Gender.MALE)
    team_id: Mapped[str | None] = mapped_column(String(64), default=None)
    started_on: Mapped[date | None] = mapped_column(Date, default=None)
    #: Inclusive last working day.
    ended_on: Mapped[date | None] = mapped_column(Date, default=None)
    #: Per-person AI allowance. NULL means the configured default rather than zero, so
    #: changing the default moves everybody who has not been singled out.
    ai_daily_limit: Mapped[int | None] = mapped_column(Integer, default=None)

    office: Mapped[Office] = relationship(back_populates="employees")

    __table_args__ = (Index("ix_employee_office", "office_id"),)


class WeeklyTemplateSlot(Base):
    """How many people to draft on a given weekday, *on top of* the fixed list."""

    __tablename__ = "weekly_template"

    office_id: Mapped[str] = mapped_column(
        ForeignKey("office.id", ondelete="CASCADE"), primary_key=True
    )
    weekday: Mapped[int] = mapped_column(Integer, primary_key=True)
    vacant_desks: Mapped[int] = mapped_column(Integer, default=0)


class TemplateFixed(Base):
    """People who always come in on a given weekday."""

    __tablename__ = "template_fixed"

    office_id: Mapped[str] = mapped_column(
        ForeignKey("office.id", ondelete="CASCADE"), primary_key=True
    )
    weekday: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[str] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), primary_key=True
    )


class Absence(Base):
    __tablename__ = "absence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.id", ondelete="CASCADE"))
    start_date: Mapped[date] = mapped_column(Date)
    #: Inclusive, so a one-day absence has start == end.
    end_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[AbsenceKind] = mapped_column(_enum(AbsenceKind), default=AbsenceKind.VACATION)
    note: Mapped[str] = mapped_column(String(256), default="")
    #: Telegram user id of whoever added it — an employee editing their own, or an admin.
    created_by: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (Index("ix_absence_employee_range", "employee_id", "start_date", "end_date"),)


class CalendarException(Base):
    __tablename__ = "calendar_exception"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Null means it applies everywhere.
    office_id: Mapped[str | None] = mapped_column(
        ForeignKey("office.id", ondelete="CASCADE"), default=None
    )
    day: Mapped[date] = mapped_column(Date)
    #: HOLIDAY | CLOSED | EXTRA_WORKDAY. The last is required: Kazakhstan transfers
    #: holidays onto working Saturdays.
    kind: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16), default="manual")
    note: Mapped[str] = mapped_column(String(128), default="")

    __table_args__ = (
        UniqueConstraint("office_id", "day", "kind", name="uq_calendar_exception"),
        Index("ix_calendar_exception_day", "day"),
    )


class ScheduleDay(Base):
    """One office-day, carrying the facts that were true when it was planned.

    Storing ``available_count`` and ``entitlement_scaled`` here is what lets the ledger be
    rebuilt without versioning the weekly template: editing the template tomorrow cannot
    retroactively change what last month meant.
    """

    __tablename__ = "schedule_day"

    office_id: Mapped[str] = mapped_column(
        ForeignKey("office.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    weekday: Mapped[int] = mapped_column(Integer)
    is_workday: Mapped[bool] = mapped_column(Boolean, default=True)
    desks_required: Mapped[int] = mapped_column(Integer, default=0)
    available_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Who was available that day. The count alone is not enough to rebuild the ledger,
    #: because entitlement accrues per available head and absences can be edited later.
    available_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    entitlement_scaled: Mapped[int] = mapped_column(Integer, default=0)
    filled: Mapped[int] = mapped_column(Integer, default=0)
    shortfall: Mapped[int] = mapped_column(Integer, default=0)
    #: OPEN -> ANNOUNCED (frozen) -> CLOSED (in the past).
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    announced_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    roster_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    generation_id: Mapped[int | None] = mapped_column(Integer, default=None)

    __table_args__ = (Index("ix_schedule_day_day", "day"),)


class Assignment(Base):
    __tablename__ = "assignment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    office_id: Mapped[str] = mapped_column(ForeignKey("office.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.id", ondelete="CASCADE"))
    source: Mapped[AssignmentSource] = mapped_column(_enum(AssignmentSource))
    status: Mapped[AssignmentStatus] = mapped_column(
        _enum(AssignmentStatus), default=AssignmentStatus.PROVISIONAL
    )
    generation_id: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    announced_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    #: Cancellations are recorded, never silently deleted.
    cancelled_reason: Mapped[str | None] = mapped_column(String(128), default=None)

    __table_args__ = (
        Index("ix_assignment_office_day", "office_id", "day"),
        Index("ix_assignment_employee_day", "employee_id", "day"),
    )


class AttendanceLedger(Base):
    """The rollup. A cache: ``rebuild_ledger`` must reproduce it exactly."""

    __tablename__ = "attendance_ledger"

    employee_id: Mapped[str] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), primary_key=True
    )
    as_of: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer, default=0)
    entitlement_scaled: Mapped[int] = mapped_column(Integer, default=0)
    surplus_scaled: Mapped[int] = mapped_column(Integer, default=0)
    per_weekday: Mapped[str] = mapped_column(String(64), default="0,0,0,0,0,0,0")
    rebuilt_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class LedgerCheckpoint(Base):
    """Everything older than the retention window, folded down. Never pruned.

    One row per employee, bounded by headcount rather than by time, which is what lets
    old schedule rows be deleted without losing any fairness history.
    """

    __tablename__ = "ledger_checkpoint"

    employee_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    watermark: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer, default=0)
    entitlement_scaled: Mapped[int] = mapped_column(Integer, default=0)
    per_weekday: Mapped[str] = mapped_column(String(64), default="0,0,0,0,0,0,0")


class GenerationRun(Base):
    """Audit and reproducibility: "why am I in on Friday?" needs an answer."""

    __tablename__ = "generation_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    office_id: Mapped[str] = mapped_column(ForeignKey("office.id", ondelete="CASCADE"))
    horizon_start: Mapped[date] = mapped_column(Date)
    horizon_end: Mapped[date] = mapped_column(Date)
    seed: Mapped[int] = mapped_column(Integer)
    solver_version: Mapped[str] = mapped_column(String(16))
    input_hash: Mapped[str] = mapped_column(String(64))
    objective: Mapped[str] = mapped_column(String(128), default="")
    added: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    kept: Mapped[int] = mapped_column(Integer, default=0)
    shortfall: Mapped[int] = mapped_column(Integer, default=0)
    triggered_by: Mapped[str] = mapped_column(String(24), default="cron")
    created_at: Mapped[datetime] = mapped_column(DateTime)


class JobRun(Base):
    """Durable idempotency for scheduled work.

    A handler checks for its occurrence key before acting and records the outcome after,
    so a retry, a restart mid-run or a double trigger cannot double-send.
    """

    __tablename__ = "job_run"

    occurrence_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    job: Mapped[str] = mapped_column(String(64))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    status: Mapped[str] = mapped_column(String(16), default="running")
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (Index("ix_job_run_job_scheduled", "job", "scheduled_for"),)


class AiUsage(Base):
    __tablename__ = "ai_usage"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class Setting(Base):
    """The handful of numbers an admin may change without a deploy.

    Deliberately narrow: integers only, and reached through named accessors on
    `SettingsStore` rather than as a free-form bag. `app.yaml` still carries the defaults
    and still refuses an unknown key — a row here is an override, and a missing row means
    "whatever the file says", so nothing has to be migrated when a default changes.
    """

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer)


class BotPost(Base):
    """A scheduled message the bot may have to find again.

    Kept for the pin: the previous Tempo reminder has to be unpinned before the next one
    is pinned, and a dict in memory forgot it on every restart. It doubles as a
    once-a-day guard, because moving a reminder's time after it fired gives the day a
    second occurrence the job ledger has never seen.
    """

    __tablename__ = "bot_post"

    office_id: Mapped[str] = mapped_column(
        ForeignKey("office.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[PostKind] = mapped_column(_enum(PostKind), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[int] = mapped_column(Integer)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime)
    unpinned_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    __table_args__ = (Index("ix_bot_post_chat", "chat_id", "kind", "day"),)


class BotMood(Base):
    """Today's mood, per office. Stable for the day so the bot reads as a character
    rather than as something broken."""

    __tablename__ = "bot_mood"

    office_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    mood: Mapped[str] = mapped_column(String(16))
    chosen_by: Mapped[str] = mapped_column(String(8), default="roll")


class ChatMessage(Base):
    """A message the bot saw, kept only so a reply chain can be walked back up.

    Telegram fills in ``reply_to_message`` exactly one level deep, so following a thread
    any further means having kept the messages ourselves. These rows are pruned
    aggressively — nobody follows a thread back a fortnight, and storing other people's
    conversation for longer than it is useful is its own problem.
    """

    __tablename__ = "chat_message"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    author: Mapped[str] = mapped_column(String(128), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    reply_to_message_id: Mapped[int | None] = mapped_column(Integer, default=None)
    at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (Index("ix_chat_message_at", "at"),)


class ChatMemory(Base):
    """A short fact the bot asked to keep.

    ``scope`` is ``office`` (shared with everyone in that chat) or ``employee`` (shown
    only to the person it is about). Both are ring buffers bounded by config, so the
    prompt built from them has a fixed maximum size.
    """

    __tablename__ = "chat_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(64))
    fact: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (Index("ix_chat_memory_scope_subject", "scope", "subject", "id"),)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, default=None)
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (Index("ix_audit_at", "at"),)
