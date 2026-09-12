"""The daily attendance reminder.

One rule, no weekday special-casing: announce the next working day. Mon→Tue and Fri→Mon
both fall out of it, Sun→Mon is covered by running the job on Sunday, and a holiday chain
is skipped because the calendar says those days are not working days.

Announcing freezes the day. A later regeneration may not move people who have already
been told to come in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from tabelshchik.application.policy import SilentPolicy
from tabelshchik.application.ports import Clock, Notifier, OfficeStore, ScheduleStore
from tabelshchik.application.voice import (
    Voice,
    format_date,
    lead_in,
    mention,
    office_header,
    roster_fingerprint,
)
from tabelshchik.domain.calendar import next_working_day
from tabelshchik.domain.entities import Employee
from tabelshchik.domain.mood import Mood


class ReminderSkip(StrEnum):
    #: The office is closed, or gone. Its jobs were registered when the bot booted and
    #: outlive both, so this is what stops a closed office still announcing a roster.
    INACTIVE = "inactive"
    NO_WORKING_DAY = "no-working-day"
    NOT_SCHEDULED = "not-scheduled"
    ALREADY_ANNOUNCED = "already-announced"
    NO_CHAT = "no-chat"
    SEND_FAILED = "send-failed"


@dataclass(frozen=True, slots=True)
class ReminderOutcome:
    office_id: str
    target: date | None
    text: str = ""
    sent: bool = False
    silent: bool = False
    correction: bool = False
    skipped: ReminderSkip | None = None

    @property
    def was_skipped(self) -> bool:
        return self.skipped is not None


async def send_attendance_reminder(
    *,
    office_id: str,
    offices: OfficeStore,
    schedule: ScheduleStore,
    notifier: Notifier,
    voice: Voice,
    clock: Clock,
    silent_policy: SilentPolicy,
    chat_id: int | None = None,
    dry_run: bool = False,
    pin: bool = False,
) -> ReminderOutcome:
    office = offices.get_office(office_id)
    if office is None or not office.active:
        # Before `planning_context`, which raises LookupError for an office that is gone.
        # Per-office jobs are fixed at boot, so closing or deleting an office leaves its
        # reminder scheduled; without this it keeps announcing, or the job fails nightly.
        return ReminderOutcome(office_id, None, skipped=ReminderSkip.INACTIVE)

    today = clock.today()
    # Far enough ahead to step over a holiday chain.
    context = offices.planning_context(office_id, start=today, end=today + timedelta(days=21))

    target = next_working_day(context.spec, today)
    if target is None:
        return ReminderOutcome(office_id, None, skipped=ReminderSkip.NO_WORKING_DAY)

    snapshot = schedule.day(office_id, target)
    if snapshot is None:
        # The office expects nobody that day — a Tuesday at an office that only fills
        # desks on Fridays. Saying so every time would be noise, not information.
        return ReminderOutcome(office_id, target, skipped=ReminderSkip.NOT_SCHEDULED)

    roster = snapshot.roster
    fingerprint = roster_fingerprint(roster)

    correction = False
    if snapshot.is_announced:
        if snapshot.roster_fingerprint == fingerprint:
            return ReminderOutcome(office_id, target, skipped=ReminderSkip.ALREADY_ANNOUNCED)
        # Somebody's absence changed the roster after it was announced. Saying nothing
        # would leave people with a list that is now wrong.
        correction = True

    mood = voice.mood_for(office_id=office_id, day=today)
    employees = {employee.id: employee for employee in offices.employees(office_id)}
    common = voice.catalog.common
    # Two values, not one concatenated string. `when` is sentence-initial and capitalised;
    # `date` is the bare date. Folding them together is what let an AI-written line put its
    # own weekday in front of ours and say "В понедельник В понедельник".
    when = lead_in(today, target, common)
    date_text = format_date(target, common)

    if roster:
        text = await _render_roster(
            roster=roster,
            employees=employees,
            voice=voice,
            mood=mood,
            office_id=office_id,
            office_name=context.office.name,
            today=today,
            when=when,
            date_text=date_text,
        )
    else:
        text = voice.static(
            "attendance.empty",
            mood,
            office_id=office_id,
            day=today,
            when=when,
            date_text=date_text,
            office_name=context.office.name,
        )

    if _chat_is_shared(offices, context.office.chat_id):
        # Two offices post into this chat, so every message says which one it is about.
        text = f"{office_header(context.office.name, common)}\n\n{text}"

    if correction:
        prefix = voice.catalog.text("attendance.correction").format(
            date=format_date(target, common), office=context.office.name
        )
        text = f"{prefix}\n\n{text}"

    silent = silent_policy.is_silent(today.weekday(), clock.time_of_day())

    if dry_run:
        return ReminderOutcome(office_id, target, text=text, silent=silent, correction=correction)

    destination = chat_id if chat_id is not None else context.office.chat_id
    if destination is None:
        return ReminderOutcome(office_id, target, text=text, skipped=ReminderSkip.NO_CHAT)

    sent = await notifier.send(destination, text, silent=silent, pin=pin, pin_kind="attendance")
    if sent is None:
        # Never record a day as announced when the message did not actually go out —
        # that would freeze a roster nobody has seen.
        return ReminderOutcome(office_id, target, text=text, skipped=ReminderSkip.SEND_FAILED)

    schedule.mark_announced(office_id, target, fingerprint=fingerprint, at=clock.now())

    return ReminderOutcome(
        office_id=office_id,
        target=target,
        text=text,
        sent=True,
        silent=silent,
        correction=correction,
    )


async def _render_roster(
    *,
    roster: tuple[str, ...],
    employees: dict[str, Employee],
    voice: Voice,
    mood: Mood,
    office_id: str,
    office_name: str,
    today: date,
    when: str,
    date_text: str,
) -> str:
    """The opening line, then one row per person.

    The loop is over the roster, never over anything the model returned. A decoration that
    is missing, short, or nonsense costs a hand-written title and nothing else — everyone
    scheduled is still named and still tagged.
    """
    # Reading order, not id order: `DaySnapshot.roster` sorts by employee id, which is an
    # implementation detail nobody in the chat can see.
    ordered = sorted(roster, key=lambda employee_id: _sort_key(employees, employee_id))
    genders = [_gender_of(employees, employee_id) for employee_id in ordered]

    decoration = await voice.decorate(
        mood,
        office_id=office_id,
        office_name=office_name,
        day=today,
        genders=genders,
        forbidden=[employee.full_name for employee in employees.values()],
    )

    intro = voice.static(
        "attendance.intro",
        mood,
        office_id=office_id,
        day=today,
        when=when,
        date_text=date_text,
        office_name=office_name,
        tail=decoration.tail,
    )

    rows = [
        f"{decoration.emojis[index]} {decoration.epithets[index]} "
        f"{_mention_line(employees, employee_id)}"
        for index, employee_id in enumerate(ordered)
    ]
    return "{intro}\n\n{body}".format(intro=intro, body="\n".join(rows))


def _sort_key(employees: dict[str, Employee], employee_id: str) -> str:
    employee = employees.get(employee_id)
    return (employee.full_name if employee else employee_id).casefold()


def _gender_of(employees: dict[str, Employee], employee_id: str) -> str:
    employee = employees.get(employee_id)
    return str(employee.gender) if employee else "male"


def _chat_is_shared(offices: OfficeStore, chat_id: int | None) -> bool:
    if chat_id is None:
        return False
    return sum(1 for o in offices.active_offices() if o.chat_id == chat_id) > 1


def _mention_line(employees: dict[str, Employee], employee_id: str) -> str:
    employee = employees.get(employee_id)
    if employee is None:
        return employee_id
    return mention(
        employee.full_name,
        telegram_user_id=employee.telegram_user_id,
        username=employee.telegram_username,
    )
