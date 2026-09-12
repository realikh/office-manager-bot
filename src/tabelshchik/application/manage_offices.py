"""Creating, editing and retiring offices, from inside the bot.

Offices used to arrive as `config/offices/<id>.yaml`, seeded once and skipped forever
afterwards — so the files looked authoritative while changing nothing, and a new office
meant a commit and a deploy. They live in the database now, and this is what edits them.

Two rules shape everything here.

*Closing is the reversible half, and the one to reach for.* It stops an office being
scheduled or messaged while leaving every employee, assignment and ledger entry exactly
where they were. Deleting cascades all of that away and is behind a typed confirmation.

*Closing has to stop the things that actually speak.* Per-office jobs are registered when
the bot boots and outlive the office, so `send_attendance_reminder` and its siblings check
`office.active` themselves. Nothing here can rely on a job simply not firing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tabelshchik.application.ids import MAX_OFFICE_ID_LENGTH, unique_id
from tabelshchik.application.ports import (
    AdminRole,
    AdminStore,
    AuditLog,
    OfficeAdminStore,
    OfficeStore,
    RosterStore,
)

#: A country code for the `holidays` package. Not free text on purpose: an unknown code
#: makes `country_holidays` return an empty mapping, so a typo is a silent "no public
#: holidays, ever" — the exact fail-open this project exists to avoid. The screens offer
#: a fixed list; this is the backstop.
_CALENDAR = re.compile(r"^[A-Z]{2,8}$")

MAX_NAME_LENGTH = 128


class OfficeError(ValueError):
    """The requested change makes no sense. The message is shown to the admin."""


@dataclass(frozen=True, slots=True)
class OfficeChange:
    office_id: str
    name: str


def create_office(
    *,
    name: str,
    timezone: str = "Asia/Almaty",
    holiday_calendar: str = "KZ",
    actor_id: int,
    offices: OfficeStore,
    office_admin: OfficeAdminStore,
    admins: AdminStore,
    audit: AuditLog | None = None,
) -> OfficeChange:
    """Create an empty office. Owner only.

    Empty is fine. It has nobody and no desks, so the solver produces nothing and there is
    no schedule to generate — `manage_roster.add_employee` regenerates after every change,
    so the first hire produces the first schedule.
    """
    _require_owner(admins, actor_id)
    clean = _clean_name(name)

    # Every office, closed ones included: a closed office still holds its id, and handing
    # it out twice is a primary-key violation out of a session scope.
    taken = {office.id for office in offices.all_offices()}
    try:
        office_id = unique_id(clean, taken, max_length=MAX_OFFICE_ID_LENGTH, fallback="office")
    except ValueError as error:
        raise OfficeError("Не удалось придумать id — назовите офис иначе.") from error

    if not office_admin.create_office(
        office_id, clean, timezone=timezone, holiday_calendar=holiday_calendar
    ):
        raise OfficeError("Офис с таким id уже есть.")

    _audit(audit, actor_id, "office.create", {"office": office_id, "name": clean})
    return OfficeChange(office_id=office_id, name=clean)


def rename_office(
    *,
    office_id: str,
    name: str,
    actor_id: int,
    offices: OfficeStore,
    office_admin: OfficeAdminStore,
    audit: AuditLog | None = None,
) -> OfficeChange:
    """A change of label, not of identity. The id is referenced by every other table."""
    _require_office(offices, office_id)
    clean = _clean_name(name)

    office_admin.rename_office(office_id, clean)
    _audit(audit, actor_id, "office.rename", {"office": office_id, "name": clean})
    return OfficeChange(office_id=office_id, name=clean)


def bind_chat(
    *,
    office_id: str,
    chat_id: int | None,
    actor_id: int,
    offices: OfficeStore,
    roster: RosterStore,
    audit: AuditLog | None = None,
) -> OfficeChange:
    """Point an office at the group it posts into.

    Two offices may deliberately share one chat — messages into a shared chat carry an
    office header — so there is no uniqueness check here.
    """
    office = _require_office(offices, office_id)

    if chat_id is not None and chat_id > 0:
        # A positive id is a private chat. Binding an office to one would post that
        # office's full tagged roster into a single person's DMs every working day.
        raise OfficeError("Id группы отрицательный и обычно начинается с −100.")

    roster.set_chat_id(office_id, chat_id)
    _audit(audit, actor_id, "office.chat", {"office": office_id, "chat": chat_id})
    return OfficeChange(office_id=office_id, name=office.name)


def set_holiday_calendar(
    *,
    office_id: str,
    code: str,
    actor_id: int,
    offices: OfficeStore,
    office_admin: OfficeAdminStore,
    audit: AuditLog | None = None,
) -> OfficeChange:
    office = _require_office(offices, office_id)
    clean = code.strip().upper()
    if not _CALENDAR.match(clean):
        raise OfficeError("Код календаря — две-восемь латинских букв, например KZ.")

    office_admin.set_holiday_calendar(office_id, clean)
    _audit(audit, actor_id, "office.calendar", {"office": office_id, "calendar": clean})
    return OfficeChange(office_id=office_id, name=office.name)


def close_office(
    *,
    office_id: str,
    actor_id: int,
    offices: OfficeStore,
    office_admin: OfficeAdminStore,
    admins: AdminStore,
    audit: AuditLog | None = None,
) -> OfficeChange:
    """Stop scheduling and messaging an office, keeping everything it ever recorded."""
    _require_owner(admins, actor_id)
    office = _require_office(offices, office_id)
    if not office.active:
        raise OfficeError("Офис уже закрыт.")

    office_admin.set_active(office_id, False)
    _audit(audit, actor_id, "office.close", {"office": office_id})
    return OfficeChange(office_id=office_id, name=office.name)


def reopen_office(
    *,
    office_id: str,
    actor_id: int,
    offices: OfficeStore,
    office_admin: OfficeAdminStore,
    admins: AdminStore,
    audit: AuditLog | None = None,
) -> OfficeChange:
    _require_owner(admins, actor_id)
    office = _require_office(offices, office_id)
    if office.active:
        raise OfficeError("Офис уже открыт.")

    office_admin.set_active(office_id, True)
    _audit(audit, actor_id, "office.reopen", {"office": office_id})
    return OfficeChange(office_id=office_id, name=office.name)


def delete_office(
    *,
    office_id: str,
    confirmation: str,
    actor_id: int,
    offices: OfficeStore,
    office_admin: OfficeAdminStore,
    admins: AdminStore,
    audit: AuditLog | None = None,
) -> OfficeChange:
    """The irreversible half. Owner only, and only with the name typed out.

    Everything hanging off `office.id` cascades: employees, the weekly template, calendar
    exceptions, schedule days, assignments, generation runs. Typing the name is the
    confirmation because a Да/Отмена pair is one misplaced thumb away from the end of an
    office's entire history.
    """
    _require_owner(admins, actor_id)
    office = _require_office(offices, office_id)

    if confirmation.strip().casefold() != office.name.casefold():
        raise OfficeError("Название не совпало. Ничего не удалено.")

    office_admin.delete_office(office_id)
    _audit(audit, actor_id, "office.delete", {"office": office_id, "name": office.name})
    return OfficeChange(office_id=office_id, name=office.name)


# ------------------------------------------------------------------------------ helpers


def _clean_name(name: str) -> str:
    clean = " ".join(name.split())
    if not clean:
        raise OfficeError("Название не может быть пустым.")
    if len(clean) > MAX_NAME_LENGTH:
        raise OfficeError("Слишком длинное название.")
    return clean


def _require_office(offices: OfficeStore, office_id: str):  # type: ignore[no-untyped-def]
    office = offices.get_office(office_id)
    if office is None:
        raise OfficeError("Офис не найден.")
    return office


def _require_owner(admins: AdminStore, actor_id: int) -> None:
    """The authoritative check; the screens hiding the buttons is a convenience.

    Creating and deleting offices is structural, so it sits with the owner alongside
    granting adminship. Everything inside an office stays open to every admin.
    """
    if admins.role_of(actor_id) is not AdminRole.OWNER:
        raise OfficeError("Это может только владелец.")


def _audit(
    audit: AuditLog | None, actor_id: int | None, action: str, payload: dict[str, object]
) -> None:
    if audit is not None:
        audit.record(actor_id=actor_id, action=action, payload=payload)
