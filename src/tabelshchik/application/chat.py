"""Talking to the bot.

Rate-limited twice over: a per-person daily allowance so nobody monopolises it, and a
global daily cap that exists purely so a strange day cannot run up a bill.

The per-person allowance is the configured default unless an admin has singled somebody
out. Storing an override rather than a copy of the default is what lets the default be
raised later without walking the roster — only the people deliberately moved stay put.

Every prompt carries the date and time, who is writing, and the recent conversation in
this chat. The schedule and saved notes are looked up only when a question needs them,
through two tools; what is worth remembering comes back inside the answer itself.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from tabelshchik.application import chat_tools, memory, transcript
from tabelshchik.application.audience import linked
from tabelshchik.application.chat_tools import Toolbox
from tabelshchik.application.policy import ChatPolicy
from tabelshchik.application.ports import (
    ChatTurn,
    Clock,
    MessageCache,
    ModelTurn,
    NoteScope,
    NoteStore,
    OfficeStore,
    Role,
    ScheduleStore,
    UsageStore,
)
from tabelshchik.application.voice import Voice, format_long_date, name_stems
from tabelshchik.domain.entities import Employee, Gender
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
    lookups: tuple[str, ...] = ()

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
    message_id: int | None = None
    is_private: bool = False


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
    notes: NoteStore | None = None,
    reach: Sequence[str] = (),
    asker_name: str = "",
) -> ChatReply:
    today = clock.today()
    mood = voice.mood_for(office_id=office_id, day=today)

    if not policy.enabled or voice.model is None:
        return ChatReply(voice.catalog.text("ai.disabled"), ChatRefusal.DISABLED)

    cleaned = question.strip()[:MAX_QUESTION_LENGTH]
    if not cleaned:
        return ChatReply("", ChatRefusal.EMPTY)

    employee = linked(offices, user_id, today)
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

    box = _toolbox(
        user_id=user_id,
        employee=employee,
        office_id=office_id,
        reach=reach,
        offices=offices,
        schedule=schedule,
        notes=notes,
        clock=clock,
        policy=policy,
        voice=voice,
    )
    tools = chat_tools.specs(box) if policy.tool_rounds > 0 else ()
    turns = [
        ChatTurn(
            Role.SYSTEM, _system_prompt(voice, mood, lookups=bool(tools), notes=box.remembers)
        ),
        ChatTurn(
            Role.USER,
            _user_prompt(
                question=cleaned,
                box=box,
                asker_name=asker_name,
                thread=thread,
                messages=messages,
                voice=voice,
                clock=clock,
                policy=policy,
            ),
        ),
    ]

    tokens = 0
    lookups: list[str] = []
    result: ModelTurn | None = None
    try:
        for round_number in range(policy.tool_rounds + 1):
            last = round_number == policy.tool_rounds
            result = await voice.model.converse(
                turns,
                tools=tools,
                allow_tools=not last,
                max_tokens=policy.max_tokens,
                temperature=policy.temperature,
                json_object=True,
            )
            if result is None:
                break
            tokens += result.total_tokens
            if last or not result.tool_calls:
                break
            turns.append(ChatTurn(Role.ASSISTANT, result.text or None, result.tool_calls))
            for index, call in enumerate(result.tool_calls):
                if index < chat_tools.CALLS_PER_ROUND:
                    output = chat_tools.run(call, box)
                    lookups.append(call.name)
                else:
                    output = chat_tools.SKIPPED
                turns.append(ChatTurn(Role.TOOL, output, tool_call_id=call.id))
    finally:
        # The attempt is counted whatever happened. Otherwise a failing model would be a
        # free, unlimited way to spend money.
        usage.record(user_id, today, tokens=tokens)

    if result is None or not result.text:
        return ChatReply(
            _line(voice, "ai.failed", mood, office_id, today),
            ChatRefusal.MODEL_FAILED,
            tuple(lookups),
        )

    payload = _parse(result.text)
    reply = str(payload.get("reply", "")).strip() if payload is not None else _salvage(result.text)
    if not reply:
        return ChatReply(
            _line(voice, "ai.failed", mood, office_id, today),
            ChatRefusal.MODEL_FAILED,
            tuple(lookups),
        )

    if payload is not None and box.remembers:
        _keep(payload, box, voice, clock)

    return ChatReply(_sanitise(reply, voice), lookups=tuple(lookups))


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


_REPLY_START = re.compile(r'"reply"\s*:\s*"')
_ESCAPES = {"n": "\n", "t": "\t", "r": "", "b": "", "f": "", "/": "/", '"': '"', "\\": "\\"}


def _keep(payload: dict[str, object], box: Toolbox, voice: Voice, clock: Clock) -> None:
    store = box.notes
    if store is None:
        return
    now = clock.now()
    banned = [term.lower() for term in voice.moods.banned_terms if term]

    writes = memory.parse_writes(
        payload.get("notes"),
        limit=box.policy.notes_per_reply,
        max_chars=box.policy.note_chars,
        today=clock.today(),
    )
    for write in writes:
        target = _owner(write.scope, box)
        if target is None:
            continue
        subject, keep = target
        lowered = f"{write.topic} {write.text}".lower()
        if keep <= 0 or any(term in lowered for term in banned):
            continue
        known = store.notes(write.scope, subject, limit=keep)
        if not any(note.topic == write.topic for note in known) and memory.is_near_identical(
            write.text, [note.text for note in known]
        ):
            continue
        store.write(
            write.scope, subject, write.topic, write.text, until=write.until, keep=keep, at=now
        )

    for forget in memory.parse_forgets(payload.get("forget")):
        target = _owner(forget.scope, box)
        if target is not None:
            store.forget(forget.scope, target[0], forget.topic)


def _owner(scope: NoteScope, box: Toolbox) -> tuple[str, int] | None:
    if scope is NoteScope.PERSON:
        return str(box.user_id), box.policy.personal_notes
    if box.employee is not None and box.home is not None:
        return box.home, box.policy.office_notes
    return None


def _toolbox(
    *,
    user_id: int,
    employee: Employee | None,
    office_id: str,
    reach: Sequence[str],
    offices: OfficeStore,
    schedule: ScheduleStore,
    notes: NoteStore | None,
    clock: Clock,
    policy: ChatPolicy,
    voice: Voice,
) -> Toolbox:
    reached = tuple(
        office
        for office in (offices.get_office(member) for member in (tuple(reach) or (office_id,)))
        if office is not None
    )
    home = offices.office_of(employee.id) if employee is not None else None
    return Toolbox(
        user_id=user_id,
        employee=employee,
        home=home if any(office.id == home for office in reached) else None,
        office_id=office_id,
        reach=reached,
        offices=offices,
        schedule=schedule,
        notes=notes,
        clock=clock,
        policy=policy,
        common=voice.catalog.common,
    )


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


def _system_prompt(voice: Voice, mood: Mood, *, lookups: bool, notes: bool) -> str:
    """The voice, then the house rules, then the lookups, then the contract.

    The persona sets tone only. It used to name allowed joke topics — "только про
    понедельники, дорогу…" — and the model dutifully mentioned Monday in every answer on
    every day of the week. Hence the explicit instruction not to volunteer the calendar.

    The rules used to cap every answer at three sentences, which made a question about
    anything real get a shrug. They now say how to decide the length instead.
    """
    persona = voice.catalog.line("ai.persona", mood)
    rules = (
        "Отвечай на том языке, на котором написано сообщение, на которое ты отвечаешь "
        "(reply in the language of the message you are answering): на английское по-английски, "
        "на казахское по-казахски, на любое другое на нем же. По-русски, только если оно на "
        "русском или язык не определить.\n"
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
        "Не здоровайся и не представляйся без повода.\n"
        "Не заговаривай о дне недели, дате или расписании, если тебя об этом не "
        "спросили. Ниже указано, какой сегодня день — используй это, чтобы не ошибиться, "
        "а не чтобы упомянуть.\n"
        "Не повторяй одну и ту же мысль, шутку или эмодзи в каждом ответе. Эмодзи вообще "
        "ставь редко.\n"
        "Про расписание и людей этого офиса отвечай только по данным из инструментов и "
        "переписки. Если данных нет, так и скажи: не выдумывай даты, имена и цифры.\n"
        "Оформление — обычный текст без Markdown: без звёздочек, решёток и обратных "
        "кавычек. Для структуры используй абзацы и списки с «•» или «1.», код пиши "
        "отдельными строками."
    )

    parts = [persona, rules]
    if lookups:
        parts.append(_LOOKUPS_WITH_NOTES if notes else _LOOKUPS)
    parts.append(_CONTRACT_WITH_NOTES if notes else _CONTRACT)
    return "\n\n".join(parts)


_LOOKUPS = (
    "Инструмент schedule показывает расписание офиса: кто и в какие дни выходит в офис, и "
    "отдельно дни собеседника. Вызывай его, только когда ответ от этого зависит; на общие "
    "вопросы отвечай сразу, без инструментов. Никогда не угадывай расписание: посмотри, а "
    "если данных нет, так и скажи."
)
_LOOKUPS_WITH_NOTES = (
    _LOOKUPS + "\n"
    "Инструмент recall показывает твои заметки о собеседнике и об офисе: то, что тебе "
    "рассказывали раньше. Смотри в них, когда ответ может зависеть от того, что тебе "
    "говорили: личные данные собеседника, его планы, состояние офиса, договоренности. "
    "Заметки о собеседнике видит только он сам: в групповом чате не пересказывай их, если "
    "он сам об этом не спросил."
)
_CONTRACT = 'Верни СТРОГО JSON: {"reply": "..."}'
_CONTRACT_WITH_NOTES = (
    'Верни СТРОГО JSON: {"reply": "...", "notes": [...], "forget": [...]}\n'
    "reply: твой ответ собеседнику.\n"
    "notes: что стоит запомнить из этого сообщения, то, что пригодится потом. Дни "
    "рождения, предпочтения, роли, планы с датами, договоренности, события, что сломалось "
    "или починилось. Каждая заметка: "
    '{"about": "me" или "office", "topic": "...", "text": "...", "until": "YYYY-MM-DD" или null}. '
    'about "me" значит о самом собеседнике, "office" значит полезное всем в офисе. '
    "topic: одно-три слова по-русски; для office называй, о ком или о чем речь, например "
    "день рождения Бори. Та же тема обновляет заметку. text: одна короткая фраза по-русски, "
    "в третьем лице, даже если разговор на другом языке. until: дата, после которой факт "
    "перестанет быть верным, если он временный.\n"
    'forget: [{"about": ..., "topic": ...}] для того, что больше не верно или что просили '
    "забыть. Чтобы исправить или удалить заметку, сначала посмотри recall и возьми точную "
    "тему.\n"
    "Никогда не записывай пароли, токены и другие секреты, здоровье, внешность, сплетни и "
    "оценки людей. Если запоминать нечего, верни пустые списки."
)


def _user_prompt(
    *,
    question: str,
    box: Toolbox,
    asker_name: str,
    thread: Thread | None,
    messages: MessageCache | None,
    voice: Voice,
    clock: Clock,
    policy: ChatPolicy,
) -> str:
    common = voice.catalog.common
    today = clock.today()
    # Without this the model has no idea what day it is, which is how it ends up insisting
    # on Monday to someone writing on a Friday.
    header = "\n".join(
        (
            f"Сегодня: {format_long_date(today, common)}. Время: {clock.now():%H:%M}.",
            _asker_line(box, asker_name),
            _chat_line(box, thread),
        )
    )
    history, marker = _history(thread, messages, policy, today, common.months)
    sections = [header, history, f"Сообщение, на которое ты отвечаешь{marker}:\n{question}"]
    return "\n\n".join(section for section in sections if section)


def _asker_line(box: Toolbox, asker_name: str) -> str:
    if box.employee is None:
        return f"Пишет: {asker_name or 'собеседник'}, не сотрудник ни одного офиса."
    hint = (
        " (женщина, обращайся к ней в женском роде)" if box.employee.gender is Gender.FEMALE else ""
    )
    office = box.offices.get_office(box.home) if box.home is not None else None
    where = f", офис «{office.name}»" if office is not None else ""
    return f"Пишет: {box.employee.full_name}{hint}{where}."


def _chat_line(box: Toolbox, thread: Thread | None) -> str:
    if thread is not None and thread.is_private:
        return "Чат: личная переписка с тобой."
    names = ", ".join(f"«{office.name}»" for office in box.reach)
    if len(box.reach) > 1:
        return f"Чат: общая группа офисов {names}."
    return f"Чат: группа офиса {names}."


def _history(
    thread: Thread | None,
    messages: MessageCache | None,
    policy: ChatPolicy,
    today: date,
    months: Sequence[str],
) -> tuple[str, str]:
    if thread is None or messages is None:
        return "", ""
    budget = transcript.Budget(
        messages=policy.history_messages,
        chars=policy.history_chars,
        message_chars=policy.message_chars,
    )
    chain = (
        messages.chain(thread.chat_id, thread.reply_to_message_id, depth=policy.reply_depth)
        if thread.reply_to_message_id is not None and policy.reply_depth > 0
        else ()
    )
    recent = (
        messages.recent(thread.chat_id, before=thread.message_id, limit=policy.history_messages)
        if thread.message_id is not None
        else ()
    )
    selected = transcript.select(chain=chain, recent=recent, budget=budget)
    parents = (
        messages.get(
            thread.chat_id, transcript.missing_parents(selected, thread.reply_to_message_id)
        )
        if policy.reply_depth > 0
        else {}
    )
    text = transcript.render(selected, parents=parents, budget=budget, today=today, months=months)
    marker = transcript.reply_marker(
        thread.reply_to_message_id, transcript.numbering(selected), parents
    )
    return text, marker


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
