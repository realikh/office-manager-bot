"""Talking to the bot.

Rate-limited twice over: a per-person daily allowance so nobody monopolises it, and a
global daily cap that exists purely so a strange day cannot run up a bill.

The per-person allowance is the configured default unless an admin has singled somebody
out. Storing an override rather than a copy of the default is what lets the default be
raised later without walking the roster — only the people deliberately moved stay put.

The model is grounded rather than given tools. It is handed today's date, the asking
person's own upcoming office days, tomorrow's roster, the thread being replied to, and a
handful of short facts it previously asked to remember. About the office that is all it
knows, so there is very little for it to get wrong there. About anything else it may
answer freely — the bot is meant to be worth talking to, not a schedule lookup with a
personality bolted on.

Length follows the question. A one-line cap made every answer terse, including the ones
that needed an explanation; the ceiling is now `ChatPolicy.max_tokens`, and the model is
told to be brief only when brief is enough.

The reply and the decision about what to remember come back in one JSON response. Two
calls would cost twice as much and could disagree with each other.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from tabelshchik.application import memory
from tabelshchik.application.policy import ChatPolicy
from tabelshchik.application.ports import (
    ChatMemoryStore,
    Clock,
    MessageCache,
    OfficeStore,
    ScheduleStore,
    UsageStore,
)
from tabelshchik.application.voice import (
    Voice,
    format_date,
    format_long_date,
    name_stems,
)
from tabelshchik.domain.entities import Employee
from tabelshchik.domain.mood import Mood
from tabelshchik.domain.rng import stable_hash

MAX_QUESTION_LENGTH = 1000
#: Characters, before escaping. Longer than one Telegram message on purpose: the router
#: splits on line breaks, and a detailed answer cut in half mid-thought is worse than two
#: messages.
MAX_ANSWER_LENGTH = 6000
#: Marks an answer the token ceiling cut off, so it does not read as finished.
CUT_OFF = "…"


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


@dataclass(frozen=True, slots=True)
class Thread:
    """Where in the conversation this question was asked.

    Telegram fills in ``reply_to_message`` exactly one level deep, so anything further
    back has to come from our own cache of what we have seen.
    """

    chat_id: int = 0
    reply_to_message_id: int | None = None


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
    thread: Thread | None = None,
    messages: MessageCache | None = None,
    memories: ChatMemoryStore | None = None,
) -> ChatReply:
    today = clock.today()
    mood = voice.mood_for(office_id=office_id, day=today)

    if not policy.enabled or voice.model is None:
        return ChatReply(voice.catalog.text("ai.disabled"), ChatRefusal.DISABLED)

    cleaned = question.strip()[:MAX_QUESTION_LENGTH]
    if not cleaned:
        return ChatReply("", ChatRefusal.EMPTY)

    employee = offices.find_employee_by_user_id(user_id)
    # Every refused attempt gets a different line. The same retort to every message reads
    # as a stuck bot; a new one each time reads as a bot that is sulking on purpose.
    nonce = stable_hash("refusal", str(user_id), cleaned)
    if usage.used_today(user_id, today) >= _allowance(employee, policy):
        return ChatReply(
            _line(voice, "ai.rateLimited", mood, office_id, today, nonce=nonce),
            ChatRefusal.USER_LIMIT,
        )

    if usage.total_today(today) >= policy.global_daily_limit:
        return ChatReply(
            _line(voice, "ai.globalLimited", mood, office_id, today, nonce=nonce),
            ChatRefusal.GLOBAL_LIMIT,
        )

    system = _system_prompt(voice, mood, remember=policy.remember and memories is not None)
    prompt = _user_prompt(
        question=cleaned,
        employee=employee,
        offices=offices,
        schedule=schedule,
        office_id=office_id,
        clock=clock,
        policy=policy,
        voice=voice,
        thread=thread,
        messages=messages,
        memories=memories,
    )

    completion = await voice.model.complete(
        system,
        prompt,
        max_tokens=policy.max_tokens,
        temperature=policy.temperature,
        json_object=True,
    )

    # The attempt is counted whatever happened. Otherwise a failing model would be a
    # free, unlimited way to spend money.
    usage.record(user_id, today, tokens=completion.total_tokens if completion else 0)

    if completion is None:
        return ChatReply(
            _line(voice, "ai.failed", mood, office_id, today), ChatRefusal.MODEL_FAILED
        )

    payload = _parse(completion.text)
    if payload is not None:
        reply = str(payload.get("reply", "")).strip()
    else:
        reply = _salvage(completion.text)
    if not reply:
        return ChatReply(
            _line(voice, "ai.failed", mood, office_id, today), ChatRefusal.MODEL_FAILED
        )

    if payload is not None and memories is not None and policy.remember:
        _store(payload.get("remember"), memories, policy, employee, office_id, clock)

    return ChatReply(_sanitise(reply, voice))


def _salvage(raw: str) -> str:
    """What to say when the reply was not valid JSON.

    Prose is used as it is. A JSON object that does not parse almost always means the
    token ceiling cut it off mid-answer — sending that would put `{"reply": "…` in front
    of twenty people, so the answer is recovered from it instead and marked as cut.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return text

    match = _REPLY_START.search(text)
    if match is None:
        return ""
    body = text[match.end() :]

    decoded: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character == '"':
            # The closing quote: the reply was complete and only something after it broke.
            return "".join(decoded).strip()
        if character != "\\":
            decoded.append(character)
            index += 1
            continue
        escape = body[index : index + 2]
        if escape == "\\u":
            digits = body[index + 2 : index + 6]
            if len(digits) < 4:
                break
            try:
                decoded.append(chr(int(digits, 16)))
            except ValueError:
                break
            index += 6
            continue
        if len(escape) < 2:
            break
        decoded.append(_ESCAPES.get(escape[1], escape[1]))
        index += 2

    recovered = "".join(decoded).strip()
    return f"{recovered}{CUT_OFF}" if recovered else ""


_REPLY_START = re.compile(r'"reply"\s*:\s*"')
_ESCAPES = {"n": "\n", "t": "\t", "r": "", "b": "", "f": "", "/": "/", '"': '"', "\\": "\\"}


def _store(
    payload: object,
    memories: ChatMemoryStore,
    policy: ChatPolicy,
    employee: Employee | None,
    office_id: str,
    clock: Clock,
) -> None:
    """Keep one fact, if the model asked and we do not already know it."""
    remembered = memory.distil(payload, max_chars=policy.fact_chars)
    if remembered is None:
        return

    if remembered.scope == memory.EMPLOYEE:
        if employee is None:
            # Nothing to attach it to. Discarding beats filing it under a stranger.
            return
        subject, keep = employee.id, policy.personal_facts
    else:
        subject, keep = office_id, policy.general_facts

    if keep <= 0:
        return

    known = [item.fact for item in memories.facts(remembered.scope, subject, limit=keep)]
    if memory.is_redundant(remembered.fact, known):
        return

    memories.remember(remembered.scope, subject, remembered.fact, keep=keep, at=clock.now())


def _allowance(employee: Employee | None, policy: ChatPolicy) -> int:
    """How many replies this person gets today.

    Somebody with no roster record — an admin who is not an employee anywhere — gets the
    default. There is nowhere to hang an override on them, and refusing them outright
    would lock the bot's own operators out of it.
    """
    if employee is not None and employee.ai_daily_limit is not None:
        return employee.ai_daily_limit
    return policy.per_user_daily_limit


def _line(voice: Voice, key: str, mood: Mood, office_id: str, day: date, *, nonce: int = 0) -> str:
    return html.escape(voice.catalog.variant(key, mood, office_id=office_id, day=day, nonce=nonce))


def _parse(raw: str) -> dict[str, object] | None:
    import json

    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        _, _, text = text.partition("\n")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # A model that answered in plain prose still answered. Its reply is used as-is and
        # nothing is remembered, which is a fine outcome for a one-off.
        return None
    return parsed if isinstance(parsed, dict) else None


def _system_prompt(voice: Voice, mood: Mood, *, remember: bool) -> str:
    """The voice, then the house rules.

    The persona sets tone only. It used to name allowed joke topics — "только про
    понедельники, дорогу…" — and the model dutifully mentioned Monday in every answer on
    every day of the week. Hence the explicit instruction not to volunteer the calendar.

    The rules used to cap every answer at three sentences, which made a question about
    anything real get a shrug. They now say how to decide the length instead.
    """
    persona = voice.catalog.line("ai.persona", mood)
    rules = (
        "Отвечай на любые вопросы: о работе, технологиях, программировании, науке, "
        "языках, быте, чём угодно. Не отказывайся и не уводи разговор только потому, что "
        "вопрос не про офис.\n"
        "Длину выбирай по вопросу. На простой вопрос или реплику — коротко, одной-двумя "
        "фразами. Если нужно объяснение, инструкция, код, сравнение или совет — отвечай "
        "подробно и полно, со всеми нужными шагами и деталями, не экономь слова. Не "
        "растягивай простое и не обрывай сложное.\n"
        "Оставайся собой: ты Табельщик, и сегодняшнее настроение окрашивает тон любого "
        "ответа — но не заменяет суть.\n"
        "Границы: не оскорбляй и не унижай людей, не обсуждай внешность, здоровье и "
        "личную жизнь коллег, не помогай с тем, что явно вредно или незаконно.\n"
        "Пиши на русском, если собеседник не пишет на другом языке.\n"
        "Не здоровайся и не представляйся без повода.\n"
        "Не заговаривай о дне недели, дате или расписании, если тебя об этом не "
        "спросили. Ниже указано, какой сегодня день — используй это, чтобы не ошибиться, "
        "а не чтобы упомянуть.\n"
        "Не повторяй одну и ту же мысль или шутку в каждом ответе.\n"
        "Про расписание и людей этого офиса отвечай строго по данным ниже. Если данных "
        "нет, так и скажи: не выдумывай даты, имена и цифры.\n"
        "Оформление — обычный текст без Markdown: без звёздочек, решёток и обратных "
        "кавычек. Для структуры используй абзацы и списки с «•» или «1.», код пиши "
        "отдельными строками."
    )

    if remember:
        contract = (
            'Верни СТРОГО JSON: {"reply": "...", "remember": {"scope": ..., "fact": "..."}}\n'
            "reply — твой ответ собеседнику.\n"
            "remember — заполняй только если в сообщении есть устойчивый факт, который "
            "пригодится потом: предпочтение, роль, договорённость, особенность работы. "
            'scope — "office", если это касается всех, или "user", если только собеседника. '
            "fact — одна короткая фраза.\n"
            'В остальных случаях (а это большинство) — {"scope": null, "fact": ""}.'
        )
    else:
        contract = 'Верни СТРОГО JSON: {"reply": "..."}'

    return f"{persona}\n\n{rules}\n\n{contract}"


def _user_prompt(
    *,
    question: str,
    employee: Employee | None,
    offices: OfficeStore,
    schedule: ScheduleStore,
    office_id: str,
    clock: Clock,
    policy: ChatPolicy,
    voice: Voice,
    thread: Thread | None,
    messages: MessageCache | None,
    memories: ChatMemoryStore | None,
) -> str:
    sections = [
        _grounding(
            employee=employee,
            offices=offices,
            schedule=schedule,
            office_id=office_id,
            clock=clock,
            policy=policy,
            voice=voice,
        ),
        _memory(memories, employee, office_id, policy),
        _chain(thread, messages, policy),
    ]
    body = "\n\n".join(section for section in sections if section)
    return f"{body}\n\nВопрос: {question}" if body else f"Вопрос: {question}"


def _grounding(
    *,
    employee: Employee | None,
    offices: OfficeStore,
    schedule: ScheduleStore,
    office_id: str,
    clock: Clock,
    policy: ChatPolicy,
    voice: Voice,
) -> str:
    today = clock.today()
    common = voice.catalog.common
    # Without this the model has no idea what day it is, which is how it ends up insisting
    # on Monday to someone writing on a Friday.
    lines = [f"Сегодня: {format_long_date(today, common)}."]

    if employee is not None:
        upcoming = schedule.upcoming_for_employee(
            employee.id, start=today, limit=policy.upcoming_days
        )
        if upcoming:
            rendered = ", ".join(format_date(day, common) for day in upcoming)
            lines.append(f"Ближайшие дни этого человека в офисе: {rendered}.")
        else:
            lines.append("У этого человека нет запланированных дней в офисе.")

    tomorrow = schedule.day(office_id, today + timedelta(days=1))
    if tomorrow is not None and tomorrow.roster:
        names = {e.id: e.full_name for e in offices.employees(office_id)}
        listed = ", ".join(names.get(eid, eid) for eid in tomorrow.roster)
        lines.append(f"Завтра в офис выходят: {listed}.")

    return "\n".join(lines)


def _memory(
    memories: ChatMemoryStore | None,
    employee: Employee | None,
    office_id: str,
    policy: ChatPolicy,
) -> str:
    """Shared facts always; personal facts only for the person they are about."""
    if memories is None:
        return ""

    general = [
        item.fact for item in memories.facts(memory.OFFICE, office_id, limit=policy.general_facts)
    ]
    personal = (
        [
            item.fact
            for item in memories.facts(memory.EMPLOYEE, employee.id, limit=policy.personal_facts)
        ]
        if employee is not None
        else []
    )
    return memory.render(general, personal)


def _chain(thread: Thread | None, messages: MessageCache | None, policy: ChatPolicy) -> str:
    """The messages this question hangs off, oldest first.

    Budgeted twice — per message and for the chain as a whole — and trimmed from the
    *oldest* end, because the nearest messages are the ones the question refers to.
    """
    if thread is None or messages is None or policy.reply_depth <= 0:
        return ""
    if thread.reply_to_message_id is None:
        return ""

    cached = messages.chain(thread.chat_id, thread.reply_to_message_id, depth=policy.reply_depth)
    if not cached:
        return ""

    rendered = [
        f"{item.author}: {_clip(item.text, policy.message_chars)}" for item in cached if item.text
    ]

    while rendered and sum(len(line) + 1 for line in rendered) > policy.reply_chars:
        rendered.pop(0)

    return "Переписка, на которую отвечают:\n" + "\n".join(rendered) if rendered else ""


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


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
