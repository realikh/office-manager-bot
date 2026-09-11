"""Repositories: the database side of the application ports.

Each method opens its own short transaction. That costs a negligible amount on a local
SQLite file and removes a whole class of bug where a session outlives an ``await``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from tabelshchik.adapters.db import models
from tabelshchik.adapters.db.engine import session_scope
from tabelshchik.adapters.holiday_calendar import public_holidays
from tabelshchik.application.ports import (
    DaySnapshot,
    PlanningContext,
    ScheduleDiff,
)
from tabelshchik.domain.calendar import CalendarSpec
from tabelshchik.domain.entities import (
    Absence,
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    Employee,
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

    def get_office(self, office_id: str) -> Office | None:
        with session_scope(self._sessions) as session:
            row = session.get(models.Office, office_id)
            return _to_office(row) if row else None

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
