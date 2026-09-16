"""Tempo reminders.

Two occasions and never more than one message a day: the last working day of the week,
and the last working day of the month. A Friday that also ends the month gets one
reminder that says both, not two. Neither fires on a non-working day, and every active
employee is tagged, because unlike the attendance reminder this concerns everyone.

The words are the model's, in the day's mood, with a hand-written line behind them. The
link, the names and the office header are always ours. When the reminder lands in the
last half hour of the workday it says how many minutes are left and suggests spending
exactly those on Tempo; any earlier and a countdown would be silly, so it does not.

The message is pinned, and the previous one unpinned first — the pin is what notifies the
whole chat. Both facts live in the database. They used to live in a dict on the notifier,
which forgot the old pin on every redeploy and left the chat collecting them.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum

from tabelshchik.application.policy import SilentPolicy, TempoPolicy
from tabelshchik.application.ports import (
    Clock,
    Notifier,
    OfficeStore,
    PostKind,
    PostRecord,
    PostStore,
)
from tabelshchik.application.voice import (
    Catalog,
    CommonText,
    Voice,
    mention,
    office_header,
    plural_minutes,
)
from tabelshchik.domain.calendar import (
    CalendarSpec,
    DayKind,
    last_working_day_of_month,
    last_working_day_of_week,
)
from tabelshchik.domain.entities import FRIDAY
from tabelshchik.domain.mood import Mood

logger = logging.getLogger(__name__)

SATURDAY = FRIDAY + 1


class TempoKind(StrEnum):
    """Which hand-written pool the fallback comes from. The values are catalog keys."""

    WEEKLY = "tempo.weekly"
    MONTH_END = "tempo.monthEnd"


@dataclass(frozen=True, slots=True)
class TempoOccasion:
    week_end: bool = False
    month_end: bool = False

    @property
    def kind(self) -> TempoKind:
        # The deadline is the more useful half when both apply; the brief still tells the
        # model that the week is over too.
        return TempoKind.MONTH_END if self.month_end else TempoKind.WEEKLY


@dataclass(frozen=True, slots=True)
class TempoOutcome:
    office_id: str
    kind: TempoKind | None = None
    text: str = ""
    sent: bool = False
    silent: bool = False
    pinned: bool = False
    #: Whether the model wrote the words, as opposed to the hand-written fallback.
    generated: bool = False
    skipped: str | None = None


def _chat_is_shared(offices: OfficeStore, chat_id: int | None) -> bool:
    """Whether more than one office posts into this chat."""
    if chat_id is None:
        return False
    return sum(1 for o in offices.active_offices() if o.chat_id == chat_id) > 1


def tempo_occasion(spec: CalendarSpec, today: date, policy: TempoPolicy) -> TempoOccasion | None:
    """What, if anything, ``today`` closes: the working week, the month, or both."""
    if not policy.enabled or not spec.is_working_day(today):
        return None
    week_end = last_working_day_of_week(spec, today) == today
    month_end = policy.month_end and last_working_day_of_month(spec, today) == today
    if not (week_end or month_end):
        return None
    return TempoOccasion(week_end=week_end, month_end=month_end)


async def send_tempo_reminder(
    *,
    office_id: str,
    offices: OfficeStore,
    notifier: Notifier,
    voice: Voice,
    clock: Clock,
    policy: TempoPolicy,
    silent_policy: SilentPolicy,
    posts: PostStore,
    gap_minutes: int | None = None,
    occasion: TempoOccasion | None = None,
    chat_id: int | None = None,
    dry_run: bool = False,
) -> TempoOutcome:
    """Post today's Tempo reminder, if one is due and has not gone out yet.

    ``gap_minutes`` is how much of the workday is left, or None when that is not worth
    mentioning — `ReminderTimes.tempo_gap_minutes` decides. ``chat_id`` sends a one-off
    copy somewhere else, which is neither pinned nor recorded.
    """
    office = offices.get_office(office_id)
    if office is None or not office.active:
        # Per-office jobs are fixed at boot and outlive the office. See the same guard in
        # `send_attendance_reminder`.
        return TempoOutcome(office_id, skipped="inactive")

    today = clock.today()
    context = offices.planning_context(office_id, start=today, end=today + timedelta(days=40))

    resolved = occasion or tempo_occasion(context.spec, today, policy)
    if resolved is None:
        return TempoOutcome(office_id, skipped="not-due")

    # Moving the reminder's time after it fired gives today a second occurrence that the
    # job ledger has never seen. This is what keeps that to one message.
    recording = not dry_run and chat_id is None
    if recording and posts.sent(office_id, PostKind.TEMPO, today):
        return TempoOutcome(office_id, resolved.kind, skipped="already-sent")

    roster = [employee for employee in offices.employees(office_id) if employee.in_tenure(today)]
    if not roster:
        return TempoOutcome(office_id, resolved.kind, skipped="no-employees")

    mood = voice.mood_for(office_id=office_id, day=today)
    common = voice.catalog.common
    announcement = await voice.announce(
        mood,
        brief=tempo_brief(context.spec, today, resolved, common, gap_minutes),
        fallback=_fallback(voice, resolved, mood, office_id, today, gap_minutes),
        office_name=context.office.name,
        forbidden=[employee.full_name for employee in roster],
        allow_words=(common.weekdays[today.weekday()], common.months[today.month - 1]),
        allow_numbers=(gap_minutes,) if gap_minutes else (),
    )

    parts = [announcement.text]
    footer = tempo_footer(voice.catalog, policy.url or voice.catalog.text("tempo.url"))
    if footer:
        parts.append(footer)
    parts.append(
        " ".join(
            mention(
                employee.full_name,
                telegram_user_id=employee.telegram_user_id,
                username=employee.telegram_username,
            )
            for employee in roster
        )
    )
    text = "\n\n".join(parts)
    if _chat_is_shared(offices, context.office.chat_id):
        header = office_header(context.office.name, common)
        text = f"{header}\n\n{text}"

    kind = resolved.kind
    generated = announcement.generated
    silent = silent_policy.is_silent(today.weekday(), clock.time_of_day())
    if dry_run:
        return TempoOutcome(office_id, kind, text=text, silent=silent, generated=generated)

    destination = chat_id if chat_id is not None else context.office.chat_id
    if destination is None:
        return TempoOutcome(office_id, kind, text=text, generated=generated, skipped="no-chat")

    sent = await notifier.send(destination, text, silent=silent)
    if sent is None:
        return TempoOutcome(office_id, kind, text=text, generated=generated, skipped="send-failed")

    outcome = TempoOutcome(
        office_id, kind, text=text, sent=True, silent=silent, generated=generated
    )
    if not recording:
        return outcome

    pinned = False
    if policy.pin:
        # Unpin first, then pin. Every earlier reminder still recorded as pinned is taken
        # down, not just the last one. A failed unpin is still marked done: it almost
        # always means somebody unpinned it by hand, and retrying weekly would not help.
        for previous in posts.pinned(office_id, PostKind.TEMPO, destination):
            if not await notifier.unpin(previous.chat_id, previous.message_id):
                logger.warning(
                    "tempo pin from %s in chat %s was already gone", previous.day, destination
                )
            posts.mark_unpinned(office_id, PostKind.TEMPO, previous.day, at=clock.now())
        pinned = await notifier.pin(destination, sent.message_id, silent=silent)

    posts.record(
        PostRecord(
            office_id=office_id,
            kind=PostKind.TEMPO,
            day=today,
            chat_id=destination,
            message_id=sent.message_id,
            pinned=pinned,
        ),
        at=clock.now(),
    )
    return replace(outcome, pinned=pinned)


def tempo_footer(catalog: Catalog, url: str) -> str:
    """The link, as text people can tap. Markup is ours, so the template is not escaped."""
    template = catalog.text("tempo.footer")
    if not url or not template:
        return ""
    return template.replace("{url}", html.escape(url, quote=True))


def tempo_brief(
    spec: CalendarSpec,
    today: date,
    occasion: TempoOccasion,
    common: CommonText,
    gap_minutes: int | None,
) -> str:
    """What the model is told about today.

    It is told *why* today closes the week when that is not the obvious reason — a
    Thursday before a holiday Friday — because otherwise it congratulates a Thursday on
    being Friday. It is never asked to name any other day.
    """
    weekday = common.weekdays[today.weekday()]
    lines = [f"Сегодня {weekday}."]

    if occasion.week_end:
        if today.weekday() == FRIDAY:
            lines.append("Это последний рабочий день недели, впереди выходные.")
        elif today.weekday() == SATURDAY:
            lines.append(
                "Сегодня рабочая суббота — день, перенесённый ради праздника, и это "
                "последний рабочий день недели."
            )
        else:
            reasons = "; ".join(
                f"{common.weekdays[day.weekday()]} — {_why_not_working(spec, day)}"
                for day in _rest_of_workweek(today)
            )
            lines.append(
                f"Это последний рабочий день недели, хотя сегодня не пятница: {reasons}. "
                "Эти дни не упоминай, просто учти, что неделя заканчивается сегодня."
            )

    if occasion.week_end and occasion.month_end:
        lines.append(
            "Это ещё и последний рабочий день месяца — после сегодняшнего дня отчётный "
            "период закрывается."
        )
        lines.append(
            "Поздравь всех с окончанием рабочей недели и месяца и напомни заполнить Tempo — "
            "отметить часы за неделю и проверить весь месяц."
        )
    elif occasion.month_end:
        lines.append(
            "Это последний рабочий день месяца — после сегодняшнего дня отчётный период "
            "закрывается. Неделя при этом ещё не закончилась, с её окончанием не поздравляй."
        )
        lines.append("Напомни, что сегодня последний шанс заполнить Tempo за этот месяц.")
    else:
        lines.append(
            "Поздравь всех с окончанием рабочей недели и напомни заполнить Tempo — "
            "отметить часы за прошедшую неделю."
        )

    if gap_minutes:
        left = plural_minutes(gap_minutes, common)
        lines.append(
            f"До конца рабочего дня осталось {left}. Предложи потратить именно эти последние "
            f"{left} на заполнение Tempo. Число {gap_minutes} напиши цифрами — это "
            "единственное разрешённое число."
        )
    else:
        lines.append(
            "Не упоминай, сколько времени осталось до конца рабочего дня, и не призывай "
            "тратить на Tempo последние минуты."
        )

    lines.append(f"Из дней недели можно назвать только сегодняшний ({weekday}).")
    return "\n".join(lines)


#: How the brief names a day off. The reason itself is whatever the calendar holds — a
#: holiday's own name, or the note an admin wrote — so it is quoted, not declined.
_DAY_OFF = {
    DayKind.HOLIDAY: "праздник",
    DayKind.CLOSED: "офис закрыт",
    DayKind.WEEKEND: "выходной",
}


def _why_not_working(spec: CalendarSpec, day: date) -> str:
    kind = spec.kind(day)
    label = _DAY_OFF.get(kind, "нерабочий день")
    reason = spec.holiday_names.get(day, "")
    return f"{label} ({reason})" if reason else label


def _rest_of_workweek(today: date) -> list[date]:
    """Today's weekday up to Friday, exclusive of today."""
    return [today + timedelta(days=offset) for offset in range(1, FRIDAY - today.weekday() + 1)]


def _fallback(
    voice: Voice,
    occasion: TempoOccasion,
    mood: Mood,
    office_id: str,
    today: date,
    gap_minutes: int | None,
) -> str:
    text = voice.static(str(occasion.kind), mood, office_id=office_id, day=today)
    if not gap_minutes:
        return text
    rush = voice.static(
        "tempo.lastMinutes",
        mood,
        office_id=office_id,
        day=today,
        minutes=plural_minutes(gap_minutes, voice.catalog.common),
    )
    return f"{text} {rush}"
