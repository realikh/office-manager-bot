"""Repositories: the database side of the application ports.

Each method opens its own short transaction. That costs a negligible amount on a local
SQLite file and removes a whole class of bug where a session outlives an ``await``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from tabelshchik.adapters.db import models
from tabelshchik.adapters.db.engine import session_scope
from tabelshchik.adapters.holiday_calendar import public_holidays
from tabelshchik.application.ports import (
    CachedMessage,
    DaySnapshot,
    MemoryFact,
    PlanningContext,
    ScheduleDiff,
)
from tabelshchik.domain.calendar import CalendarSpec
from tabelshchik.domain.entities import (
    Absence,
    AbsenceKind,
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    Employee,
    Gender,
    LedgerEntry,
    Office,
    WeeklyTemplate,
)
from tabelshchik.domain.ledger import DayRecord


class SqlRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions


# --------------------------------------------------------------------------- offices


class SqlOfficeStore(SqlRepository):
    def active_offices(self) -> Sequence[Office]:
        with session_scope(self._sessions) as session:
            rows = session.scalars(
                select(models.Office).where(models.Office.active).order_by(models.Office.id)
            ).all()
            return [_to_office(row) for row in rows]

    def all_offices(self) -> Sequence[Office]:
        with session_scope(self._sessions) as session:
            rows = session.scalars(select(models.Office).order_by(models.Office.id)).all()
            return [_to_office(row) for row in rows]

    def get_office(self, office_id: str) -> Office | None:
        with session_scope(self._sessions) as session:
            row = session.get(models.Office, office_id)
            return _to_office(row) if row else None

    def get_employee(self, employee_id: str) -> Employee | None:
        with session_scope(self._sessions) as session:
            row = session.get(models.Employee, employee_id)
            return _to_employee(row) if row else None

    def office_of(self, employee_id: str) -> str | None:
        """One indexed lookup, replacing a scan of every office's roster.

        Office membership lives on the employee row rather than on the entity, so this
        used to be answered by iterating `active_offices()` and looking for the person —
        in three separate places, each of which quietly also filtered out anyone whose
        office was closed. That filter is a real rule and now lives at the callers that
        mean it, rather than being a side effect of how the lookup happened to be done.
        """
        with session_scope(self._sessions) as session:
            return session.scalar(
                select(models.Employee.office_id).where(models.Employee.id == employee_id)
            )

    def employees(self, office_id: str) -> Sequence[Employee]:
        with session_scope(self._sessions) as session:
            return [_to_employee(row) for row in _employee_rows(session, office_id)]

    def planning_context(self, office_id: str, *, start: date, end: date) -> PlanningContext:
        with session_scope(self._sessions) as session:
            office_row = session.get(models.Office, office_id)
            if office_row is None:
                raise LookupError(f"unknown office: {office_id}")

            employees = [_to_employee(row) for row in _employee_rows(session, office_id)]
            employee_ids = {employee.id for employee in employees}

            absences = [
                Absence(
                    employee_id=row.employee_id,
                    start_date=row.start_date,
                    end_date=row.end_date,
                    kind=row.kind,
                    note=row.note,
                )
                for row in session.scalars(
                    select(models.Absence)
                    .where(models.Absence.employee_id.in_(employee_ids))
                    .where(models.Absence.end_date >= start)
                    .where(models.Absence.start_date <= end)
                ).all()
            ]

            template = _load_template(session, office_id)
            spec = _load_calendar(session, office_row, start=start, end=end)

            return PlanningContext(
                office=_to_office(office_row),
                template=template,
                employees=tuple(employees),
                absences=tuple(absences),
                spec=spec,
            )

    def find_employee_by_user_id(self, telegram_user_id: int) -> Employee | None:
        with session_scope(self._sessions) as session:
            row = session.scalar(
                select(models.Employee).where(models.Employee.telegram_user_id == telegram_user_id)
            )
            return _to_employee(row) if row else None

    def find_employee_by_username(self, username: str) -> Employee | None:
        cleaned = username.lstrip("@").lower()
        with session_scope(self._sessions) as session:
            rows = session.scalars(
                select(models.Employee).where(models.Employee.telegram_username.isnot(None))
            ).all()
            match = next(
                (
                    row
                    for row in rows
                    if row.telegram_username and row.telegram_username.lower() == cleaned
                ),
                None,
            )
            return _to_employee(match) if match else None

    def link_telegram_user(self, employee_id: str, telegram_user_id: int) -> None:
        with session_scope(self._sessions) as session:
            row = session.get(models.Employee, employee_id)
            if row is None:
                raise LookupError(f"unknown employee: {employee_id}")
            row.telegram_user_id = telegram_user_id


def _employee_rows(session: Session, office_id: str) -> Sequence[models.Employee]:
    return session.scalars(
        select(models.Employee)
        .where(models.Employee.office_id == office_id)
        .order_by(models.Employee.id)
    ).all()


def _load_template(session: Session, office_id: str) -> WeeklyTemplate:
    desks = {
        row.weekday: row.vacant_desks
        for row in session.scalars(
            select(models.WeeklyTemplateSlot).where(
                models.WeeklyTemplateSlot.office_id == office_id
            )
        ).all()
    }

    fixed: dict[int, list[str]] = {}
    for row in session.scalars(
        select(models.TemplateFixed)
        .where(models.TemplateFixed.office_id == office_id)
        .order_by(models.TemplateFixed.weekday, models.TemplateFixed.employee_id)
    ).all():
        fixed.setdefault(row.weekday, []).append(row.employee_id)

    return WeeklyTemplate(
        fixed={weekday: tuple(ids) for weekday, ids in fixed.items()},
        vacant_desks=desks,
    )


def _load_calendar(
    session: Session, office: models.Office, *, start: date, end: date
) -> CalendarSpec:
    names = dict(public_holidays(office.holiday_calendar, start, end))
    holiday_days = set(names)
    closed: set[date] = set()
    extra: set[date] = set()

    rows = session.scalars(
        select(models.CalendarException)
        .where(
            (models.CalendarException.office_id == office.id)
            | (models.CalendarException.office_id.is_(None))
        )
        .where(models.CalendarException.day.between(start, end))
    ).all()

    for row in rows:
        if row.kind == "CLOSED":
            closed.add(row.day)
        elif row.kind == "EXTRA_WORKDAY":
            extra.add(row.day)
        elif row.kind == "HOLIDAY":
            holiday_days.add(row.day)
        if row.note:
            names[row.day] = row.note

    return CalendarSpec(
        holidays=frozenset(holiday_days),
        closed=frozenset(closed),
        extra_workdays=frozenset(extra),
        holiday_names=names,
    )


def _to_office(row: models.Office) -> Office:
    return Office(
        id=row.id,
        name=row.name,
        chat_id=row.chat_id,
        timezone=row.timezone,
        holiday_calendar=row.holiday_calendar,
        seed_nonce=row.seed_nonce,
        active=row.active,
    )


def _to_employee(row: models.Employee) -> Employee:
    return Employee(
        id=row.id,
        full_name=row.full_name,
        telegram_username=row.telegram_username,
        telegram_user_id=row.telegram_user_id,
        gender=row.gender,
        team_id=row.team_id,
        started_on=row.started_on,
        ended_on=row.ended_on,
    )


# -------------------------------------------------------------------------- schedule


class SqlScheduleStore(SqlRepository):
    def day(self, office_id: str, day: date) -> DaySnapshot | None:
        with session_scope(self._sessions) as session:
            row = session.get(models.ScheduleDay, (office_id, day))
            if row is None:
                return None
            return _to_snapshot(row, _assignments_for(session, office_id, day, day))

    def days_between(self, office_id: str, start: date, end: date) -> Sequence[DaySnapshot]:
        with session_scope(self._sessions) as session:
            rows = session.scalars(
                select(models.ScheduleDay)
                .where(models.ScheduleDay.office_id == office_id)
                .where(models.ScheduleDay.day.between(start, end))
                .order_by(models.ScheduleDay.day)
            ).all()

            by_day: dict[date, list[Assignment]] = {}
            for assignment in _assignments_for(session, office_id, start, end):
                by_day.setdefault(assignment.day, []).append(assignment)

            return [_to_snapshot(row, by_day.get(row.day, [])) for row in rows]

    def frozen_drafted(self, office_id: str, start: date, end: date) -> dict[date, frozenset[str]]:
        """Draft decisions a regeneration may not revisit.

        Announced days and manual pins. Fixed-schedule people are excluded because they
        are recomputed from the template every time.
        """
        with session_scope(self._sessions) as session:
            announced = {
                row.day
                for row in session.scalars(
                    select(models.ScheduleDay)
                    .where(models.ScheduleDay.office_id == office_id)
                    .where(models.ScheduleDay.day.between(start, end))
                    .where(models.ScheduleDay.status == "ANNOUNCED")
                ).all()
            }

            frozen: dict[date, set[str]] = {}
            for assignment in _assignments_for(session, office_id, start, end):
                if not assignment.is_live or assignment.source is AssignmentSource.FIXED:
                    continue
                pinned = assignment.source is AssignmentSource.MANUAL
                if pinned or assignment.day in announced:
                    frozen.setdefault(assignment.day, set()).add(assignment.employee_id)

            return {day: frozenset(names) for day, names in frozen.items()}

    def upcoming_for_employee(self, employee_id: str, *, start: date, limit: int) -> Sequence[date]:
        with session_scope(self._sessions) as session:
            return list(
                session.scalars(
                    select(models.Assignment.day)
                    .where(models.Assignment.employee_id == employee_id)
                    .where(models.Assignment.day >= start)
                    .where(models.Assignment.status != AssignmentStatus.CANCELLED)
                    .order_by(models.Assignment.day)
                    .limit(limit)
                ).all()
            )

    def has_schedule_from(self, office_id: str, start: date) -> bool:
        """Whether anything at all is planned on or after ``start``."""
        with session_scope(self._sessions) as session:
            found = session.scalar(
                select(models.ScheduleDay.day)
                .where(models.ScheduleDay.office_id == office_id)
                .where(models.ScheduleDay.day >= start)
                .limit(1)
            )
            return found is not None

    def apply(self, office_id: str, diff: ScheduleDiff, *, generation_id: int) -> None:
        """Persist a regeneration as a diff, never as a truncate-and-rewrite.

        Keeping the diff is what makes churn measurable and makes "three changes this
        week" answerable.
        """
        now = datetime.now()
        with session_scope(self._sessions) as session:
            for assignment in diff.removed:
                session.execute(
                    delete(models.Assignment)
                    .where(models.Assignment.office_id == office_id)
                    .where(models.Assignment.day == assignment.day)
                    .where(models.Assignment.employee_id == assignment.employee_id)
                    .where(models.Assignment.status == AssignmentStatus.PROVISIONAL)
                )

            for assignment in diff.added:
                session.add(
                    models.Assignment(
                        office_id=office_id,
                        day=assignment.day,
                        employee_id=assignment.employee_id,
                        source=assignment.source,
                        status=assignment.status,
                        generation_id=generation_id,
                        created_at=now,
                    )
                )

            for record in diff.days:
                _upsert_day(session, office_id, record, generation_id=generation_id)

    def mark_announced(self, office_id: str, day: date, *, fingerprint: str, at: datetime) -> None:
        with session_scope(self._sessions) as session:
            row = session.get(models.ScheduleDay, (office_id, day))
            if row is None:
                return
            row.status = "ANNOUNCED"
            row.announced_at = at
            row.roster_fingerprint = fingerprint

            for assignment in session.scalars(
                select(models.Assignment)
                .where(models.Assignment.office_id == office_id)
                .where(models.Assignment.day == day)
                .where(models.Assignment.status == AssignmentStatus.PROVISIONAL)
            ).all():
                assignment.status = AssignmentStatus.ANNOUNCED
                assignment.announced_at = at

    def set_assignment_status(
        self,
        office_id: str,
        day: date,
        employee_id: str,
        status: AssignmentStatus,
        *,
        reason: str | None = None,
    ) -> None:
        with session_scope(self._sessions) as session:
            row = session.scalar(
                select(models.Assignment)
                .where(models.Assignment.office_id == office_id)
                .where(models.Assignment.day == day)
                .where(models.Assignment.employee_id == employee_id)
                .where(models.Assignment.status != AssignmentStatus.CANCELLED)
            )
            if row is None:
                return
            row.status = status
            row.cancelled_reason = reason

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
    ) -> int:
        with session_scope(self._sessions) as session:
            row = models.GenerationRun(
                office_id=office_id,
                horizon_start=horizon_start,
                horizon_end=horizon_end,
                seed=seed,
                solver_version=_solver_version(),
                input_hash=input_hash,
                objective=objective,
                added=len(diff.added),
                removed=len(diff.removed),
                kept=diff.kept,
                shortfall=shortfall,
                triggered_by=triggered_by,
                created_at=at,
            )
            session.add(row)
            session.flush()
            return row.id


def _solver_version() -> str:
    from tabelshchik.domain.solver import SOLVER_VERSION

    return SOLVER_VERSION


def _assignments_for(session: Session, office_id: str, start: date, end: date) -> list[Assignment]:
    rows = session.scalars(
        select(models.Assignment)
        .where(models.Assignment.office_id == office_id)
        .where(models.Assignment.day.between(start, end))
        .order_by(models.Assignment.day, models.Assignment.employee_id)
    ).all()
    return [
        Assignment(
            office_id=row.office_id,
            day=row.day,
            employee_id=row.employee_id,
            source=row.source,
            status=row.status,
        )
        for row in rows
    ]


def _upsert_day(session: Session, office_id: str, record: DayRecord, *, generation_id: int) -> None:
    row = session.get(models.ScheduleDay, (office_id, record.day))
    if row is None:
        row = models.ScheduleDay(office_id=office_id, day=record.day)
        session.add(row)

    row.weekday = record.weekday
    row.is_workday = True
    row.available_count = len(record.available)
    row.available_ids = sorted(record.available)
    row.entitlement_scaled = record.entitlement_scaled
    row.filled = len(record.attended)
    row.generation_id = generation_id
    # An announced day keeps its status: freezing means the reminder already went out.
    if row.status != "ANNOUNCED":
        row.status = "OPEN"


def _to_snapshot(row: models.ScheduleDay, assignments: Sequence[Assignment]) -> DaySnapshot:
    return DaySnapshot(
        office_id=row.office_id,
        day=row.day,
        status=row.status,
        desks_required=row.desks_required,
        shortfall=row.shortfall,
        roster_fingerprint=row.roster_fingerprint,
        assignments=tuple(assignments),
    )


# ---------------------------------------------------------------------------- ledger


class SqlLedgerStore(SqlRepository):
    """Fairness state.

    The rollup is a cache: ``rebuild`` replays raw day records forward from the
    checkpoint and must reproduce it exactly. Counters that can only be mutated, never
    recomputed, are how the previous implementation's fairness drifted unnoticed.
    """

    def entries(self, office_id: str) -> dict[str, LedgerEntry]:
        with session_scope(self._sessions) as session:
            employee_ids = [row.id for row in _employee_rows(session, office_id)]
            rows = session.scalars(
                select(models.AttendanceLedger).where(
                    models.AttendanceLedger.employee_id.in_(employee_ids)
                )
            ).all()
            return {row.employee_id: _to_ledger_entry(row) for row in rows}

    def save(self, entries: dict[str, LedgerEntry], *, as_of: date) -> None:
        with session_scope(self._sessions) as session:
            for employee_id, entry in entries.items():
                row = session.get(models.AttendanceLedger, employee_id)
                if row is None:
                    row = models.AttendanceLedger(employee_id=employee_id)
                    session.add(row)
                row.as_of = as_of
                row.days = entry.days
                row.entitlement_scaled = entry.entitlement_scaled
                row.surplus_scaled = entry.surplus_scaled
                row.per_weekday = _pack_weekdays(entry.per_weekday)

    def checkpoint(self) -> dict[str, LedgerEntry]:
        with session_scope(self._sessions) as session:
            rows = session.scalars(select(models.LedgerCheckpoint)).all()
            return {
                row.employee_id: LedgerEntry(
                    employee_id=row.employee_id,
                    days=row.days,
                    entitlement_scaled=row.entitlement_scaled,
                    per_weekday=_unpack_weekdays(row.per_weekday),
                )
                for row in rows
            }

    def rebuild(self, office_id: str, *, upto: date) -> dict[str, LedgerEntry]:
        from tabelshchik.domain.ledger import rebuild as replay

        with session_scope(self._sessions) as session:
            records = _day_records(session, office_id, upto=upto)
        return replay(records, checkpoint=self.checkpoint())

    def day_records(self, office_id: str, *, upto: date) -> list[DayRecord]:
        with session_scope(self._sessions) as session:
            return _day_records(session, office_id, upto=upto)

    def save_checkpoint(self, entries: dict[str, LedgerEntry], *, watermark: date) -> None:
        with session_scope(self._sessions) as session:
            for employee_id, entry in entries.items():
                row = session.get(models.LedgerCheckpoint, employee_id)
                if row is None:
                    row = models.LedgerCheckpoint(employee_id=employee_id)
                    session.add(row)
                row.watermark = watermark
                row.days = entry.days
                row.entitlement_scaled = entry.entitlement_scaled
                row.per_weekday = _pack_weekdays(entry.per_weekday)


def _day_records(session: Session, office_id: str, *, upto: date) -> list[DayRecord]:
    days = session.scalars(
        select(models.ScheduleDay)
        .where(models.ScheduleDay.office_id == office_id)
        .where(models.ScheduleDay.day <= upto)
        .order_by(models.ScheduleDay.day)
    ).all()

    attended: dict[date, set[str]] = {}
    for row in session.scalars(
        select(models.Assignment)
        .where(models.Assignment.office_id == office_id)
        .where(models.Assignment.day <= upto)
        .where(models.Assignment.status != AssignmentStatus.CANCELLED)
    ).all():
        attended.setdefault(row.day, set()).add(row.employee_id)

    return [
        DayRecord(
            day=row.day,
            weekday=row.weekday,
            available=frozenset(row.available_ids or ()),
            entitlement_scaled=row.entitlement_scaled,
            attended=frozenset(attended.get(row.day, set())),
        )
        for row in days
    ]


def _to_ledger_entry(row: models.AttendanceLedger) -> LedgerEntry:
    return LedgerEntry(
        employee_id=row.employee_id,
        days=row.days,
        entitlement_scaled=row.entitlement_scaled,
        per_weekday=_unpack_weekdays(row.per_weekday),
    )


def _pack_weekdays(counts: tuple[int, ...]) -> str:
    return ",".join(str(value) for value in counts)


def _unpack_weekdays(packed: str) -> tuple[int, ...]:
    if not packed:
        return (0,) * 7
    return tuple(int(part) for part in packed.split(","))


# ------------------------------------------------------------------------ job ledger


class SqlJobLedger(SqlRepository):
    """Durable idempotency, so a retry or a restart cannot double-send."""

    def claim(
        self,
        occurrence_key: str,
        *,
        job: str,
        scheduled_for: datetime,
        late: bool = False,
    ) -> bool:
        with session_scope(self._sessions) as session:
            existing = session.get(models.JobRun, occurrence_key)
            if existing is not None:
                # A previous attempt that died mid-run may be retried; a successful one
                # never is.
                if existing.status == "ok":
                    return False
                existing.started_at = datetime.now()
                existing.status = "running"
                existing.late = late
                return True

            session.add(
                models.JobRun(
                    occurrence_key=occurrence_key,
                    job=job,
                    scheduled_for=scheduled_for,
                    started_at=datetime.now(),
                    status="running",
                    late=late,
                )
            )
            return True

    def complete(self, occurrence_key: str, *, at: datetime) -> None:
        with session_scope(self._sessions) as session:
            row = session.get(models.JobRun, occurrence_key)
            if row is not None:
                row.status = "ok"
                row.finished_at = at
                row.error = None

    def fail(self, occurrence_key: str, *, at: datetime, error: str) -> None:
        with session_scope(self._sessions) as session:
            row = session.get(models.JobRun, occurrence_key)
            if row is not None:
                row.status = "failed"
                row.finished_at = at
                row.error = error[:4000]

    def ran_successfully(self, occurrence_key: str) -> bool:
        with session_scope(self._sessions) as session:
            row = session.get(models.JobRun, occurrence_key)
            return row is not None and row.status == "ok"

    def recent(self, limit: int = 20) -> Sequence[tuple[str, str, datetime, str]]:
        with session_scope(self._sessions) as session:
            rows = session.scalars(
                select(models.JobRun).order_by(models.JobRun.scheduled_for.desc()).limit(limit)
            ).all()
            return [(row.job, row.occurrence_key, row.scheduled_for, row.status) for row in rows]


# ------------------------------------------------------------------------ audit log


class SqlAuditLog(SqlRepository):
    def record(self, *, actor_id: int | None, action: str, payload: dict[str, object]) -> None:
        with session_scope(self._sessions) as session:
            session.add(
                models.AuditLog(
                    actor_id=actor_id,
                    action=action,
                    payload=dict(payload),
                    at=datetime.now(),
                )
            )


# ----------------------------------------------------------------------- maintenance


def _rows_affected(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


class SqlMaintenance(SqlRepository):
    """Deletion and housekeeping.

    Nothing here touches employees, offices, weekly templates, the ledger or its
    checkpoints: bounding the database must never cost the state the bot reasons from.
    """

    def delete_schedule_before(self, office_id: str, watermark: date) -> int:
        with session_scope(self._sessions) as session:
            removed = _rows_affected(
                session.execute(
                    delete(models.Assignment)
                    .where(models.Assignment.office_id == office_id)
                    .where(models.Assignment.day < watermark)
                )
            )
            session.execute(
                delete(models.ScheduleDay)
                .where(models.ScheduleDay.office_id == office_id)
                .where(models.ScheduleDay.day < watermark)
            )
            session.execute(
                delete(models.GenerationRun)
                .where(models.GenerationRun.office_id == office_id)
                .where(models.GenerationRun.horizon_end < watermark)
            )
            return removed

    def delete_job_runs_before(self, cutoff: datetime) -> int:
        with session_scope(self._sessions) as session:
            return _rows_affected(
                session.execute(delete(models.JobRun).where(models.JobRun.scheduled_for < cutoff))
            )

    def delete_audit_before(self, cutoff: datetime) -> int:
        with session_scope(self._sessions) as session:
            return _rows_affected(
                session.execute(delete(models.AuditLog).where(models.AuditLog.at < cutoff))
            )

    def delete_ai_usage_before(self, cutoff: date) -> int:
        with session_scope(self._sessions) as session:
            return _rows_affected(
                session.execute(delete(models.AiUsage).where(models.AiUsage.day < cutoff))
            )

    def delete_chat_messages_before(self, cutoff: datetime) -> int:
        with session_scope(self._sessions) as session:
            return _rows_affected(
                session.execute(delete(models.ChatMessage).where(models.ChatMessage.at < cutoff))
            )

    def delete_chat_memory_before(self, cutoff: datetime) -> int:
        with session_scope(self._sessions) as session:
            return _rows_affected(
                session.execute(delete(models.ChatMemory).where(models.ChatMemory.at < cutoff))
            )

    def delete_absences_before(self, cutoff: date) -> int:
        with session_scope(self._sessions) as session:
            return _rows_affected(
                session.execute(delete(models.Absence).where(models.Absence.end_date < cutoff))
            )

    def database_bytes(self) -> int:
        with session_scope(self._sessions) as session:
            page_size = session.execute(text("PRAGMA page_size")).scalar() or 0
            page_count = session.execute(text("PRAGMA page_count")).scalar() or 0
            return int(page_size) * int(page_count)

    def vacuum(self) -> None:
        # VACUUM cannot run inside a transaction, so it needs its own bare connection.
        engine = self._sessions.kw["bind"]
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text("VACUUM"))

    def backup_to(self, path: str) -> None:
        """A consistent copy, taken with SQLite's own backup API rather than by copying
        the file out from under a live writer."""
        import sqlite3

        engine = self._sessions.kw["bind"]
        raw = engine.raw_connection()
        try:
            destination = sqlite3.connect(path)
            try:
                raw.driver_connection.backup(destination)
            finally:
                destination.close()
        finally:
            raw.close()


# ------------------------------------------------------------------------ ai and mood


class SqlUsageStore(SqlRepository):
    """Per-day AI counters.

    Two limits, for two different worries: a per-person one so nobody monopolises the
    bot, and a global one that exists purely as a cost stop-loss.
    """

    def used_today(self, user_id: int, day: date) -> int:
        with session_scope(self._sessions) as session:
            row = session.get(models.AiUsage, (user_id, day))
            return row.count if row else 0

    def total_today(self, day: date) -> int:
        with session_scope(self._sessions) as session:
            counts = session.scalars(
                select(models.AiUsage.count).where(models.AiUsage.day == day)
            ).all()
            return sum(counts)

    def record(self, user_id: int, day: date, *, tokens: int = 0) -> None:
        with session_scope(self._sessions) as session:
            row = session.get(models.AiUsage, (user_id, day))
            if row is None:
                row = models.AiUsage(user_id=user_id, day=day, count=0, tokens=0)
                session.add(row)
            row.count += 1
            row.tokens += tokens

    def tokens_today(self, day: date) -> int:
        with session_scope(self._sessions) as session:
            counts = session.scalars(
                select(models.AiUsage.tokens).where(models.AiUsage.day == day)
            ).all()
            return sum(counts)


class SqlMessageCache(SqlRepository):
    """Recent chat messages, so a reply chain can be followed further than one level.

    Telegram hands us the parent of a message and nothing beyond it, so a thread three
    replies deep is only reconstructable from what we kept. Note the bot only *receives*
    what Telegram's privacy setting lets it see: with privacy on, that is messages
    mentioning it or replying to it, and a chain through ordinary chatter will stop at
    the first link nobody cached. That is a partial chain, not a wrong one.
    """

    def remember(self, chat_id: int, message: CachedMessage, *, at: datetime) -> None:
        with session_scope(self._sessions) as session:
            row = session.get(models.ChatMessage, (chat_id, message.message_id))
            if row is not None:
                return
            session.add(
                models.ChatMessage(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    author=message.author[:128],
                    text=message.text,
                    reply_to_message_id=message.reply_to_message_id,
                    at=at,
                )
            )

    def chain(self, chat_id: int, message_id: int, *, depth: int) -> Sequence[CachedMessage]:
        collected: list[CachedMessage] = []
        seen: set[int] = set()
        current: int | None = message_id

        with session_scope(self._sessions) as session:
            while current is not None and len(collected) < depth:
                # A malformed reply graph must not become an infinite loop; Telegram ids
                # are monotonic in practice but nothing here depends on that being true.
                if current in seen:
                    break
                seen.add(current)

                row = session.get(models.ChatMessage, (chat_id, current))
                if row is None:
                    break
                collected.append(
                    CachedMessage(
                        message_id=row.message_id,
                        author=row.author,
                        text=row.text,
                        reply_to_message_id=row.reply_to_message_id,
                    )
                )
                current = row.reply_to_message_id

        collected.reverse()  # oldest first: the thread reads in the order it happened
        return collected


class SqlChatMemoryStore(SqlRepository):
    """Short facts the bot asked to keep, as a ring buffer per scope.

    Writing a new fact is what evicts the oldest one. That is the whole retention policy:
    a bounded number of rows means a bounded prompt, and a bounded prompt means a bill
    that cannot creep.
    """

    def facts(self, scope: str, subject: str, *, limit: int) -> Sequence[MemoryFact]:
        if limit <= 0:
            return []
        with session_scope(self._sessions) as session:
            rows = session.scalars(
                select(models.ChatMemory)
                .where(models.ChatMemory.scope == scope, models.ChatMemory.subject == subject)
                .order_by(models.ChatMemory.id.desc())
                .limit(limit)
            ).all()
            # Newest first out of the database, oldest first to the caller, so the most
            # recent thing the bot was told reads last and nearest to the question.
            return [
                MemoryFact(scope=row.scope, subject=row.subject, fact=row.fact)
                for row in reversed(rows)
            ]

    def remember(self, scope: str, subject: str, fact: str, *, keep: int, at: datetime) -> None:
        with session_scope(self._sessions) as session:
            session.add(models.ChatMemory(scope=scope, subject=subject, fact=fact, at=at))
            session.flush()

            surplus = session.scalars(
                select(models.ChatMemory.id)
                .where(models.ChatMemory.scope == scope, models.ChatMemory.subject == subject)
                .order_by(models.ChatMemory.id.desc())
                .offset(max(keep, 0))
            ).all()
            if surplus:
                session.execute(delete(models.ChatMemory).where(models.ChatMemory.id.in_(surplus)))

    def forget_all(self, scope: str, subject: str) -> int:
        with session_scope(self._sessions) as session:
            return _rows_affected(
                session.execute(
                    delete(models.ChatMemory).where(
                        models.ChatMemory.scope == scope, models.ChatMemory.subject == subject
                    )
                )
            )


class SqlMoodStore(SqlRepository):
    """Today's mood, stored so a restart mid-afternoon does not change the bot's
    personality halfway through the day, and so an admin override sticks."""

    def mood_for(self, office_id: str, day: date) -> str | None:
        with session_scope(self._sessions) as session:
            row = session.get(models.BotMood, (office_id, day))
            return row.mood if row else None

    def set_mood(self, office_id: str, day: date, mood: str, *, by: str = "roll") -> None:
        with session_scope(self._sessions) as session:
            row = session.get(models.BotMood, (office_id, day))
            if row is None:
                row = models.BotMood(office_id=office_id, day=day, mood=mood, chosen_by=by)
                session.add(row)
            else:
                row.mood = mood
                row.chosen_by = by


# ------------------------------------------------------------------ absences and roster


class SqlAbsenceStore(SqlRepository):
    def add(
        self,
        employee_id: str,
        start: date,
        end: date,
        *,
        kind: str = "vacation",
        note: str = "",
        actor_id: int | None = None,
        at: datetime,
    ) -> int:
        with session_scope(self._sessions) as session:
            row = models.Absence(
                employee_id=employee_id,
                start_date=start,
                end_date=end,
                kind=AbsenceKind(kind),
                note=note,
                created_by=actor_id,
                created_at=at,
            )
            session.add(row)
            session.flush()
            return row.id

    def remove(self, absence_id: int) -> bool:
        with session_scope(self._sessions) as session:
            row = session.get(models.Absence, absence_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def get(self, absence_id: int) -> tuple[int, str, date, date] | None:
        with session_scope(self._sessions) as session:
            row = session.get(models.Absence, absence_id)
            if row is None:
                return None
            return (row.id, row.employee_id, row.start_date, row.end_date)

    def for_employee(
        self, employee_id: str, *, upcoming_from: date | None = None
    ) -> Sequence[tuple[int, date, date, str]]:
        with session_scope(self._sessions) as session:
            query = select(models.Absence).where(models.Absence.employee_id == employee_id)
            if upcoming_from is not None:
                query = query.where(models.Absence.end_date >= upcoming_from)
            rows = session.scalars(query.order_by(models.Absence.start_date)).all()
            return [(row.id, row.start_date, row.end_date, str(row.kind)) for row in rows]


class SqlRosterStore(SqlRepository):
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
    ) -> None:
        with session_scope(self._sessions) as session:
            session.add(
                models.Employee(
                    id=employee_id,
                    office_id=office_id,
                    full_name=full_name,
                    telegram_username=username,
                    gender=Gender(gender),
                    team_id=team_id,
                    started_on=started_on,
                )
            )

    def remove_employee(self, employee_id: str, *, ended_on: date | None = None) -> bool:
        """Ending a tenure is preferred to deleting a person.

        A hard delete would cascade away their history and silently rewrite the ledger;
        an end date stops them being scheduled while leaving the record intact.
        """
        with session_scope(self._sessions) as session:
            row = session.get(models.Employee, employee_id)
            if row is None:
                return False
            if ended_on is None:
                session.delete(row)
            else:
                row.ended_on = ended_on
            return True

    def restore_employee(self, employee_id: str) -> bool:
        """Undo an end date.

        The reason `remove_employee` prefers an end date to a delete: a misclick costs one
        button press to reverse, and nothing about their history was ever thrown away.
        """
        with session_scope(self._sessions) as session:
            row = session.get(models.Employee, employee_id)
            if row is None:
                return False
            row.ended_on = None
            return True

    def rename_employee(self, employee_id: str, full_name: str) -> bool:
        with session_scope(self._sessions) as session:
            row = session.get(models.Employee, employee_id)
            if row is None:
                return False
            row.full_name = full_name
            return True

    def set_username(self, employee_id: str, username: str | None) -> bool:
        with session_scope(self._sessions) as session:
            row = session.get(models.Employee, employee_id)
            if row is None:
                return False
            row.telegram_username = username.lstrip("@") if username else None
            return True

    def set_vacant_desks(self, office_id: str, weekday: int, desks: int) -> None:
        with session_scope(self._sessions) as session:
            row = session.get(models.WeeklyTemplateSlot, (office_id, weekday))
            if desks <= 0:
                if row is not None:
                    session.delete(row)
                return
            if row is None:
                row = models.WeeklyTemplateSlot(office_id=office_id, weekday=weekday)
                session.add(row)
            row.vacant_desks = desks

    def toggle_fixed(self, office_id: str, weekday: int, employee_id: str) -> bool:
        """Returns whether the person is fixed on that weekday afterwards."""
        with session_scope(self._sessions) as session:
            key = (office_id, weekday, employee_id)
            row = session.get(models.TemplateFixed, key)
            if row is not None:
                session.delete(row)
                return False
            session.add(
                models.TemplateFixed(office_id=office_id, weekday=weekday, employee_id=employee_id)
            )
            return True

    def bump_seed(self, office_id: str) -> int:
        with session_scope(self._sessions) as session:
            row = session.get(models.Office, office_id)
            if row is None:
                raise LookupError(f"unknown office: {office_id}")
            row.seed_nonce += 1
            return row.seed_nonce

    def set_chat_id(self, office_id: str, chat_id: int | None) -> None:
        with session_scope(self._sessions) as session:
            row = session.get(models.Office, office_id)
            if row is None:
                raise LookupError(f"unknown office: {office_id}")
            row.chat_id = chat_id
