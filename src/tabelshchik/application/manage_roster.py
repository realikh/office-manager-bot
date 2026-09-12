"""Hiring and firing, from inside the bot.

The store already knew how to do this; nothing ever called it. Which meant that after the
first boot the roster could not be changed at all — seeding skips an office that already
exists, so editing the YAML does nothing, and the only recourse was destroying the
database.

Two rules shape everything here.

*Removal ends a tenure; it does not delete a person.* ``RosterStore.remove_employee``
defaults to a hard delete that cascades away every assignment they ever had and silently
rewrites everyone else's fairness numbers. An end date stops them being scheduled, leaves
the history intact, and can be undone by a misclicking admin.

*Every change is followed by a regeneration.* A new hire who gets no days until Thursday,
or a leaver whose desk sits empty for a fortnight, is the same bug in two directions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from tabelshchik.application.ids import unique_id
from tabelshchik.application.policy import SchedulePolicy
from tabelshchik.application.ports import (
    AuditLog,
    Clock,
    LedgerStore,
    OfficeStore,
    RosterStore,
    ScheduleStore,
)
from tabelshchik.application.regenerate_schedule import regenerate
from tabelshchik.domain.entities import AssignmentStatus, Employee, Gender

_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")


class RosterError(ValueError):
    """The requested change makes no sense. The message is shown to the admin."""


@dataclass(frozen=True, slots=True)
class RosterChange:
    employee_id: str
    full_name: str
    #: Days this person was holding that are now free for somebody else.
    released: tuple[date, ...] = ()


def add_employee(
    *,
    office_id: str,
    full_name: str,
    username: str | None = None,
    gender: str = "male",
    team_id: str | None = None,
    actor_id: int | None,
    offices: OfficeStore,
    roster: RosterStore,
    schedule: ScheduleStore,
    ledger: LedgerStore,
    clock: Clock,
    policy: SchedulePolicy,
    audit: AuditLog | None = None,
    regenerate_after: bool = True,
) -> RosterChange:
    name = " ".join(full_name.split())
    if not name:
        raise RosterError("Имя не может быть пустым.")
    if len(name) > 128:
        raise RosterError("Слишком длинное имя.")

    handle = normalise_username(username)
    if username and handle is None:
        raise RosterError("Ник должен быть от 5 до 32 символов: латиница, цифры и «_».")
    if handle and offices.find_employee_by_username(handle) is not None:
        raise RosterError(f"Ник @{handle} уже занят другим сотрудником.")

    taken = {employee.id for employee in offices.employees(office_id)}
    try:
        employee_id = unique_id(name, taken)
    except ValueError as error:
        raise RosterError("Слишком много однофамильцев — задайте имя иначе.") from error

    roster.add_employee(
        office_id,
        employee_id,
        name,
        username=handle,
        gender=_gender(gender),
        team_id=team_id,
        started_on=clock.today(),
    )

    if regenerate_after:
        # A new person with no days until the next weekly run looks like a bot that
        # ignored the admin.
        _regenerate(office_id, offices, schedule, ledger, clock, policy, "roster")

    _audit(audit, actor_id, "roster.add", {"office": office_id, "employee": employee_id})
    return RosterChange(employee_id=employee_id, full_name=name)


def end_tenure(
    *,
    office_id: str,
    employee_id: str,
    on: date | None = None,
    actor_id: int | None,
    offices: OfficeStore,
    roster: RosterStore,
    schedule: ScheduleStore,
    ledger: LedgerStore,
    clock: Clock,
    policy: SchedulePolicy,
    audit: AuditLog | None = None,
    regenerate_after: bool = True,
) -> RosterChange:
    employee = _require(offices, office_id, employee_id)
    last_day = on or clock.today()

    # Always explicit. Passing None here is the hard delete described at the top of this
    # module, and it is never what an admin pressing "Уволить" meant.
    if not roster.remove_employee(employee_id, ended_on=last_day):
        raise RosterError("Сотрудник не найден.")

    released = _release_future_days(
        office_id=office_id, employee_id=employee_id, after=last_day, schedule=schedule
    )

    if regenerate_after:
        _regenerate(office_id, offices, schedule, ledger, clock, policy, "roster")

    _audit(
        audit,
        actor_id,
        "roster.end",
        {"office": office_id, "employee": employee_id, "endedOn": last_day.isoformat()},
    )
    return RosterChange(employee_id=employee_id, full_name=employee.full_name, released=released)


def restore(
    *,
    office_id: str,
    employee_id: str,
    actor_id: int | None,
    offices: OfficeStore,
    roster: RosterStore,
    schedule: ScheduleStore,
    ledger: LedgerStore,
    clock: Clock,
    policy: SchedulePolicy,
    audit: AuditLog | None = None,
    regenerate_after: bool = True,
) -> RosterChange:
    """Undo an end date. The counterpart to `end_tenure`, and the reason it is soft."""
    employee = _require(offices, office_id, employee_id)
    if not roster.restore_employee(employee_id):
        raise RosterError("Сотрудник не найден.")

    if regenerate_after:
        _regenerate(office_id, offices, schedule, ledger, clock, policy, "roster")

    _audit(audit, actor_id, "roster.restore", {"office": office_id, "employee": employee_id})
    return RosterChange(employee_id=employee_id, full_name=employee.full_name)


def rename(
    *,
    office_id: str,
    employee_id: str,
    full_name: str,
    actor_id: int | None,
    offices: OfficeStore,
    roster: RosterStore,
    audit: AuditLog | None = None,
) -> RosterChange:
    _require(offices, office_id, employee_id)
    name = " ".join(full_name.split())
    if not name:
        raise RosterError("Имя не может быть пустым.")
    if len(name) > 128:
        raise RosterError("Слишком длинное имя.")

    # The id is left alone on purpose: it is referenced by assignments, ledger entries and
    # the weekly template, and a rename is a change of label, not of person.
    if not roster.rename_employee(employee_id, name):
        raise RosterError("Сотрудник не найден.")

    _audit(audit, actor_id, "roster.rename", {"office": office_id, "employee": employee_id})
    return RosterChange(employee_id=employee_id, full_name=name)


def set_username(
    *,
    office_id: str,
    employee_id: str,
    username: str | None,
    actor_id: int | None,
    offices: OfficeStore,
    roster: RosterStore,
    audit: AuditLog | None = None,
) -> RosterChange:
    employee = _require(offices, office_id, employee_id)

    handle = normalise_username(username)
    if username and handle is None:
        raise RosterError("Ник должен быть от 5 до 32 символов: латиница, цифры и «_».")

    existing = offices.find_employee_by_username(handle) if handle else None
    if existing is not None and existing.id != employee_id:
        raise RosterError(f"Ник @{handle} уже занят другим сотрудником.")

    if not roster.set_username(employee_id, handle):
        raise RosterError("Сотрудник не найден.")

    _audit(audit, actor_id, "roster.username", {"office": office_id, "employee": employee_id})
    return RosterChange(employee_id=employee_id, full_name=employee.full_name)


# ------------------------------------------------------------------------------ helpers


def normalise_username(username: str | None) -> str | None:
    """Strip a leading @ and validate. Returns None for "no username" *and* for invalid
    input; callers distinguish the two by whether anything was supplied."""
    if username is None:
        return None
    handle = username.strip().lstrip("@")
    if not handle:
        return None
    return handle if _USERNAME.match(handle) else None


def _release_future_days(
    *, office_id: str, employee_id: str, after: date, schedule: ScheduleStore
) -> tuple[date, ...]:
    """Cancel what they were down for beyond their last day.

    Cancelled rather than deleted, exactly as an absence does it, so the day still records
    that the slot existed and the regeneration that follows can fill it.
    """
    # A year ahead comfortably covers any horizon the scheduler will have generated.
    horizon = after + timedelta(days=366)
    released: list[date] = []

    for snapshot in schedule.days_between(office_id, after, horizon):
        if snapshot.day <= after or employee_id not in snapshot.roster:
            continue
        schedule.set_assignment_status(
            office_id, snapshot.day, employee_id, AssignmentStatus.CANCELLED, reason="left"
        )
        released.append(snapshot.day)
    return tuple(released)


def _require(offices: OfficeStore, office_id: str, employee_id: str) -> Employee:
    employee = next((item for item in offices.employees(office_id) if item.id == employee_id), None)
    if employee is None:
        raise RosterError("Сотрудник не найден в этом офисе.")
    return employee


def _gender(value: str) -> str:
    try:
        return Gender(value).value
    except ValueError:
        return Gender.MALE.value


def _regenerate(
    office_id: str,
    offices: OfficeStore,
    schedule: ScheduleStore,
    ledger: LedgerStore,
    clock: Clock,
    policy: SchedulePolicy,
    triggered_by: str,
) -> None:
    regenerate(
        office_id=office_id,
        offices=offices,
        schedule=schedule,
        ledger=ledger,
        clock=clock,
        policy=policy,
        triggered_by=triggered_by,
    )


def _audit(
    audit: AuditLog | None, actor_id: int | None, action: str, payload: dict[str, object]
) -> None:
    if audit is not None:
        audit.record(actor_id=actor_id, action=action, payload=payload)
