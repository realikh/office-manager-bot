"""Tempo reminders.

Three independent rules, none of which fires on a non-working day: a weekly nag, an
early month-end warning, and the last working day of the month. All three tag every
active employee, because unlike the attendance reminder this concerns everyone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from tabelshchik.application.policy import SilentPolicy, TempoPolicy
from tabelshchik.application.ports import Clock, Notifier, OfficeStore
from tabelshchik.application.voice import Voice, mention
from tabelshchik.domain.calendar import CalendarSpec, last_working_day_of_month


class TempoKind(StrEnum):
    WEEKLY = "tempo.weekly"
    MONTH_WARNING = "tempo.monthWarning"
    MONTH_END = "tempo.monthEnd"


@dataclass(frozen=True, slots=True)
class TempoOutcome:
    office_id: str
    kind: TempoKind | None = None
    text: str = ""
    sent: bool = False
    silent: bool = False
    skipped: str | None = None


def due_kind(spec: CalendarSpec, today: date, policy: TempoPolicy) -> TempoKind | None:
    """Which Tempo nag, if any, belongs to ``today``.

    Order matters: the last working day of the month beats the earlier warning, so the
    two never land together.
    """
    if not policy.enabled or not spec.is_working_day(today):
        return None

    if policy.month_end and last_working_day_of_month(spec, today) == today:
        return TempoKind.MONTH_END

    if _warning_is_due(spec, today, policy.warning_day):
        return TempoKind.MONTH_WARNING

    if today.weekday() in policy.weekly_weekdays:
        return TempoKind.WEEKLY

    return None


def _warning_is_due(spec: CalendarSpec, today: date, configured_day: int) -> bool:
    """True on the configured day, or the first working day after it.

    It deliberately does not fire retroactively: if the configured day has passed and a
    working day went by without firing, the moment is gone and a late warning is just
    noise.
    """
    if configured_day < 1 or today.day < configured_day:
        return False

    try:
        target = today.replace(day=configured_day)
    except ValueError:  # e.g. the 31st in a short month
        return False

    cursor = target
    while cursor < today:
        if spec.is_working_day(cursor):
            return False
        cursor += timedelta(days=1)

    return True


async def send_tempo_reminder(
    *,
    office_id: str,
    offices: OfficeStore,
    notifier: Notifier,
    voice: Voice,
    clock: Clock,
    policy: TempoPolicy,
    silent_policy: SilentPolicy,
    kind: TempoKind | None = None,
    chat_id: int | None = None,
    dry_run: bool = False,
) -> TempoOutcome:
    today = clock.today()
    context = offices.planning_context(office_id, start=today, end=today + timedelta(days=40))

    resolved = kind or due_kind(context.spec, today, policy)
    if resolved is None:
        return TempoOutcome(office_id, skipped="not-due")

    roster = [employee for employee in offices.employees(office_id) if employee.in_tenure(today)]
    if not roster:
        return TempoOutcome(office_id, resolved, skipped="no-employees")

    mood = voice.mood_for(office_id=office_id, day=today)
    intro = voice.static(
        str(resolved),
        mood,
        office_id=office_id,
        day=today,
        date_text="",
        office_name=context.office.name,
    )

    url = policy.url or voice.catalog.text("tempo.url")
    link = f'\n\n👉 <a href="{url}">Открыть Tempo</a>' if url else ""
    mentions = " ".join(
        mention(
            employee.full_name,
            telegram_user_id=employee.telegram_user_id,
            username=employee.telegram_username,
        )
        for employee in roster
    )
    text = f"{intro}{link}\n\n{mentions}"

    silent = silent_policy.is_silent(today.weekday(), clock.time_of_day())
    if dry_run:
        return TempoOutcome(office_id, resolved, text=text, silent=silent)

    destination = chat_id if chat_id is not None else context.office.chat_id
    if destination is None:
        return TempoOutcome(office_id, resolved, text=text, skipped="no-chat")

    sent = await notifier.send(
        destination, text, silent=silent, pin=policy.pin, pin_kind=str(resolved)
    )
    if sent is None:
        return TempoOutcome(office_id, resolved, text=text, skipped="send-failed")

    return TempoOutcome(office_id, resolved, text=text, sent=True, silent=silent)
