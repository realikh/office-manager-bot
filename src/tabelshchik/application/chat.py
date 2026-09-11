"""Talking to the bot.

Rate-limited twice over: a per-person daily allowance so nobody monopolises it, and a
global daily cap that exists purely so a strange day cannot run up a bill.

The model is grounded rather than given tools. It is handed the asking person's own
upcoming office days and tomorrow's roster, so "когда я в офисе?" is answered from real
data — and nothing else, so there is very little for it to get wrong.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from tabelshchik.application.policy import ChatPolicy
from tabelshchik.application.ports import (
    Clock,
    OfficeStore,
    ScheduleStore,
    UsageStore,
)
from tabelshchik.application.voice import Voice, format_date, name_stems
from tabelshchik.domain.mood import Mood

MAX_QUESTION_LENGTH = 1000
MAX_ANSWER_LENGTH = 1500


class ChatRefusal(StrEnum):
    DISABLED = "disabled"
    USER_LIMIT = "user-limit"
    GLOBAL_LIMIT = "global-limit"
    EMPTY = "empty"
    MODEL_FAILED = "model-failed"


@dataclass(frozen=True, slots=True)
class ChatReply:
    text: str
    refusal: ChatRefusal | None = None

    @property
    def answered(self) -> bool:
        return self.refusal is None


async def answer(
    *,
    question: str,
    user_id: int,
    office_id: str,
    offices: OfficeStore,
    schedule: ScheduleStore,
    usage: UsageStore,
    voice: Voice,
    clock: Clock,
    policy: ChatPolicy,
) -> ChatReply:
    today = clock.today()
    mood = voice.mood_for(office_id=office_id, day=today)

    if not policy.enabled or voice.model is None:
        return ChatReply(voice.catalog.text("ai.disabled"), ChatRefusal.DISABLED)

    cleaned = question.strip()[:MAX_QUESTION_LENGTH]
    if not cleaned:
        return ChatReply("", ChatRefusal.EMPTY)

    if usage.used_today(user_id, today) >= policy.per_user_daily_limit:
        return ChatReply(
            _line(voice, "ai.rateLimited", mood, office_id, today), ChatRefusal.USER_LIMIT
        )

    if usage.total_today(today) >= policy.global_daily_limit:
        return ChatReply(
            _line(voice, "ai.rateLimited", mood, office_id, today), ChatRefusal.GLOBAL_LIMIT
        )

    system = _system_prompt(voice, mood)
    context = _grounding(
        offices=offices,
        schedule=schedule,
        user_id=user_id,
        office_id=office_id,
        clock=clock,
        policy=policy,
        voice=voice,
    )
    raw = await voice.model.complete(
        system,
        f"{context}\n\nВопрос: {cleaned}" if context else cleaned,
        max_tokens=policy.max_tokens,
        temperature=policy.temperature,
    )

    # The attempt is counted whatever happened. Otherwise a failing model would be a
    # free, unlimited way to spend money.
    usage.record(user_id, today)

    if raw is None:
        return ChatReply(
            _line(voice, "ai.failed", mood, office_id, today), ChatRefusal.MODEL_FAILED
        )

    return ChatReply(_sanitise(raw, voice))


def _line(voice: Voice, key: str, mood: Mood, office_id: str, day: date) -> str:
    return html.escape(voice.catalog.variant(key, mood, office_id=office_id, day=day))


def _system_prompt(voice: Voice, mood: Mood) -> str:
    persona = voice.catalog.line("ai.persona", mood)
    return (
        f"{persona}\n\n"
        "Отвечай кратко — не больше трёх предложений — и только на русском языке.\n"
        "Если в вопросе спрашивают про расписание, отвечай строго по данным ниже. "
        "Если данных нет, так и скажи: не выдумывай даты, имена и цифры.\n"
        "Не используй разметку и не упоминай, что ты языковая модель."
    )


def _grounding(
    *,
    offices: OfficeStore,
    schedule: ScheduleStore,
    user_id: int,
    office_id: str,
    clock: Clock,
    policy: ChatPolicy,
    voice: Voice,
) -> str:
    today = clock.today()
    employee = offices.find_employee_by_user_id(user_id)
    lines: list[str] = []

    if employee is not None:
        upcoming = schedule.upcoming_for_employee(
            employee.id, start=today, limit=policy.upcoming_days
        )
        if upcoming:
            rendered = ", ".join(format_date(day, voice.catalog.common) for day in upcoming)
            lines.append(f"Ближайшие дни этого человека в офисе: {rendered}.")
        else:
            lines.append("У этого человека нет запланированных дней в офисе.")

    tomorrow = schedule.day(office_id, today + timedelta(days=1))
    if tomorrow is not None and tomorrow.roster:
        names = {e.id: e.full_name for e in offices.employees(office_id)}
        listed = ", ".join(names.get(eid, eid) for eid in tomorrow.roster)
        lines.append(f"Завтра в офис выходят: {listed}.")

    return "\n".join(lines)


def _sanitise(raw: str, voice: Voice) -> str:
    """Escape before sending, and strip anything that looks like an address.

    Model output is data, never markup and never a command. Escaping is what makes that
    true regardless of what comes back.
    """
    text = raw.strip()[:MAX_ANSWER_LENGTH]
    escaped = html.escape(text)

    banned = name_stems(voice.moods.banned_terms) if voice.moods.banned_terms else set()
    lowered = escaped.lower()
    if any(term in lowered for term in banned):
        return html.escape("…")
    return escaped
