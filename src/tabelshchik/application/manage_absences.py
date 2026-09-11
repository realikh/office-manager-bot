"""Adding and removing absences.

Employees manage their own; admins manage anyone's. The interesting case is an absence
that lands on a day already announced: the assignment is cancelled rather than deleted,
a replacement is drafted, and the office chat is told. Staying quiet would leave people
holding a roster that is now wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tabelshchik.application.policy import SchedulePolicy
from tabelshchik.application.ports import (
    AbsenceStore,
    AuditLog,
    Clock,
    LedgerStore,
    OfficeStore,
    ScheduleStore,
)
from tabelshchik.application.regenerate_schedule import regenerate
from tabelshchik.domain.calendar import date_range
from tabelshchik.domain.entities import AssignmentStatus

MAX_ABSENCE_DAYS = 366


class AbsenceError(ValueError):
    """The requested absence makes no sense. The message is shown to the user."""


@dataclass(frozen=True, slots=True)
class AbsenceResult:
    absence_id: int
    cancelled: tuple[date, ...] = ()
    announced_days_affected: tuple[date, ...] = ()

    @property
    def needs_correction(self) -> bool:
        return bool(self.announced_days_affected)


def add_absence(
    *,
    employee_id: str,
    office_id: str,
    start: date,
    end: date,
    kind: str = "vacation",
    note: str = "",
    actor_id: int | None,
    absences: AbsenceStore,
    schedule: ScheduleStore,
    offices: OfficeStore,
    ledger: LedgerStore,
    clock: Clock,
    policy: SchedulePolicy,
    audit: AuditLog | None = None,
    regenerate_after: bool = True,
) -> AbsenceResult:
    _validate(start, end, clock.today())

    absence_id = absences.add(
        employee_id, start, end, kind=kind, note=note, actor_id=actor_id, at=clock.now()
    )

    cancelled: list[date] = []
    announced: list[date] = []
    for snapshot in schedule.days_between(office_id, start, end):
        if employee_id not in snapshot.roster:
            continue
        schedule.set_assignment_status(
            office_id, snapshot.day, employee_id, AssignmentStatus.CANCELLED, reason=kind
        )
        cancelled.append(snapshot.day)
        if snapshot.is_announced:
            announced.append(snapshot.day)

    if regenerate_after:
        # Cancelling frees a desk; regenerating fills it. Announced days stay frozen for
        # everyone else, so only the vacated slot moves.
        regenerate(
            office_id=office_id,
            offices=offices,
            schedule=schedule,
            ledger=ledger,
            clock=clock,
            policy=policy,
            triggered_by="absence",
        )

    if audit is not None:
        audit.record(
            actor_id=actor_id,
            action="absence.add",
            payload={
                "employee": employee_id,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "cancelled": [day.isoformat() for day in cancelled],
            },
        )

    return AbsenceResult(
        absence_id=absence_id,
        cancelled=tuple(cancelled),
        announced_days_affected=tuple(announced),
    )


def remove_absence(
    *,
    absence_id: int,
    office_id: str,
    actor_id: int | None,
    absences: AbsenceStore,
    schedule: ScheduleStore,
    offices: OfficeStore,
    ledger: LedgerStore,
    clock: Clock,
    policy: SchedulePolicy,
    audit: AuditLog | None = None,
) -> bool:
    existing = absences.get(absence_id)
    if existing is None or not absences.remove(absence_id):
        return False

    regenerate(
        office_id=office_id,
        offices=offices,
        schedule=schedule,
        ledger=ledger,
        clock=clock,
        policy=policy,
        triggered_by="absence",
    )

    if audit is not None:
        audit.record(actor_id=actor_id, action="absence.remove", payload={"absence": absence_id})
    return True


def _validate(start: date, end: date, today: date) -> None:
    if end < start:
        raise AbsenceError("Дата окончания раньше даты начала.")
    if len(list(date_range(start, end))) > MAX_ABSENCE_DAYS:
        raise AbsenceError("Слишком длинный период — максимум год.")
    if end < today:
        raise AbsenceError("Этот период уже прошёл.")
