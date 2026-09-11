"""What the chat carries into a prompt: the thread, the memory, and today's date.

The bug this exists to prevent is the one that shipped: a bot that had no idea what day
it was, insisting on Monday to somebody writing on a Friday, with no way for them to
correct it because nothing it had said a moment earlier was still in front of it.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.repositories import (
    SqlChatMemoryStore,
    SqlMessageCache,
    SqlOfficeStore,
    SqlScheduleStore,
    SqlUsageStore,
)
from tabelshchik.adapters.fakes import StubChatModel
from tabelshchik.application import memory
from tabelshchik.application.chat import Thread, answer
from tabelshchik.application.policy import ChatPolicy
from tabelshchik.application.ports import CachedMessage
from tabelshchik.application.voice import MoodPolicy, Voice
from tabelshchik.bootstrap.mapping import catalog
from tabelshchik.config.loader import parse_file
from tabelshchik.config.messages import MessagesConfig
from tabelshchik.domain.mood import Mood

from .conftest import seed

CATALOG = catalog(parse_file(Path("config/messages.yaml"), MessagesConfig))
FRIDAY = date(2026, 9, 11)
NOW = datetime(2026, 9, 11, 12, 0)
CHAT = -100123
#: The Telegram user id the fixture's Аня is linked to.
ANYA = 777


def reply(text: str = "Ага.", remember: dict | None = None) -> str:
    payload: dict[str, object] = {"reply": text}
    if remember is not None:
        payload["remember"] = remember
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def office(sessions):
    seed(sessions)
    SqlOfficeStore(sessions).link_telegram_user("anya", ANYA)
    return sessions


async def ask(
    sessions,
    model,
    *,
    user_id: int = ANYA,
    question: str = "Ну и как оно?",
    reply_to: int | None = None,
    **policy_kwargs,
):
    return await answer(
        question=question,
        user_id=user_id,
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        usage=SqlUsageStore(sessions),
        voice=Voice(catalog=CATALOG, moods=MoodPolicy(weights={Mood.TOXIC: 1}), model=model),
        clock=FixedClock(NOW),
        policy=ChatPolicy(**policy_kwargs),
        thread=Thread(chat_id=CHAT, reply_to_message_id=reply_to),
        messages=SqlMessageCache(sessions),
        memories=SqlChatMemoryStore(sessions),
    )


def cache(sessions, message_id: int, text: str, *, parent: int | None = None, who="Аня"):
    SqlMessageCache(sessions).remember(
        CHAT,
        CachedMessage(message_id=message_id, author=who, text=text, reply_to_message_id=parent),
        at=NOW,
    )


# ------------------------------------------------------------------------- grounding


async def test_the_prompt_says_what_day_it_is(office) -> None:
    """Without this the model cannot know it is Friday, which is how it ends up
    insisting on Monday to somebody who just told it otherwise."""
    model = StubChatModel(reply=reply())
    await ask(office, model)

    prompt = model.prompts[0][1]
    assert "Сегодня: Пятница, 11 сентября 2026 года." in prompt


async def test_the_model_is_told_not_to_volunteer_the_calendar(office) -> None:
    model = StubChatModel(reply=reply())
    await ask(office, model)
    assert "если тебя об этом не" in model.prompts[0][0]


async def test_the_persona_no_longer_prescribes_joke_topics(office) -> None:
    """The Monday fixation came from the persona listing Mondays as an allowed subject.

    The model repeated the one topic it had been handed, every day of the week.
    """
    model = StubChatModel(reply=reply())
    await ask(office, model)
    assert "понедельник" not in model.prompts[0][0].lower()


# ---------------------------------------------------------------------- reply chains


async def test_a_thread_is_quoted_oldest_first(office) -> None:
    cache(office, 1, "Кто завтра в офисе?")
    cache(office, 2, "Я и Боря", parent=1, who="Табельщик")
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=2)

    prompt = model.prompts[0][1]
    assert "Переписка, на которую отвечают:" in prompt
    assert prompt.index("Кто завтра в офисе?") < prompt.index("Я и Боря")


async def test_the_chain_is_followed_past_the_one_level_telegram_gives_us(office) -> None:
    for index in range(1, 6):
        cache(office, index, f"сообщение {index}", parent=index - 1 or None)
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=5)

    prompt = model.prompts[0][1]
    assert all(f"сообщение {index}" in prompt for index in range(1, 6))


async def test_the_chain_stops_at_the_configured_depth(office) -> None:
    for index in range(1, 11):
        cache(office, index, f"сообщение {index}", parent=index - 1 or None)
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=10, reply_depth=3)

    prompt = model.prompts[0][1]
    assert "сообщение 10" in prompt
    assert "сообщение 7" not in prompt


async def test_depth_zero_switches_threading_off_entirely(office) -> None:
    cache(office, 1, "что-то важное")
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=1, reply_depth=0)

    assert "что-то важное" not in model.prompts[0][1]


async def test_a_chain_that_runs_past_what_we_cached_stops_quietly(office) -> None:
    """With Telegram's privacy mode on, the bot never saw most of the chat. A partial
    chain is right; an error is not."""
    cache(office, 9, "видимое", parent=8)  # 8 was never cached
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=9)

    assert "видимое" in model.prompts[0][1]


async def test_an_oversized_chain_drops_the_oldest_messages_first(office) -> None:
    """The nearest messages are the ones the question refers to."""
    cache(office, 1, "старое " * 60)
    cache(office, 2, "свежее", parent=1)
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=2, reply_chars=200)

    prompt = model.prompts[0][1]
    assert "свежее" in prompt
    assert "старое" not in prompt


async def test_a_single_long_message_is_clipped_not_dropped(office) -> None:
    cache(office, 1, "начало " + "болтовня " * 200)
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=1, message_chars=60)

    prompt = model.prompts[0][1]
    assert "начало" in prompt
    assert "…" in prompt


async def test_a_reply_loop_does_not_hang(office) -> None:
    """Nothing here relies on Telegram ids being monotonic."""
    cache(office, 1, "первое", parent=2)
    cache(office, 2, "второе", parent=1)
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=1)
    assert model.prompts


# ---------------------------------------------------------------------------- memory


async def test_a_fact_the_model_asks_to_keep_is_stored(office) -> None:
    model = StubChatModel(reply=reply(remember={"scope": "office", "fact": "Стендап в 10:30"}))
    await ask(office, model)

    facts = SqlChatMemoryStore(office).facts(memory.OFFICE, "ovest", limit=10)
    assert [item.fact for item in facts] == ["Стендап в 10:30"]


async def test_most_messages_are_not_worth_remembering(office) -> None:
    model = StubChatModel(reply=reply(remember={"scope": None, "fact": ""}))
    await ask(office, model)
    assert not SqlChatMemoryStore(office).facts(memory.OFFICE, "ovest", limit=10)


async def test_a_stored_fact_comes_back_in_the_next_prompt(office) -> None:
    SqlChatMemoryStore(office).remember(memory.OFFICE, "ovest", "Стендап в 10:30", keep=5, at=NOW)
    model = StubChatModel(reply=reply())

    await ask(office, model)

    assert "Стендап в 10:30" in model.prompts[0][1]


async def test_a_personal_fact_is_shown_only_to_the_person_it_is_about(office) -> None:
    SqlChatMemoryStore(office).remember(
        memory.EMPLOYEE, "anya", "Работает из Алматы", keep=5, at=NOW
    )

    hers = StubChatModel(reply=reply())
    await ask(office, hers, user_id=ANYA)
    assert "Работает из Алматы" in hers.prompts[0][1]

    theirs = StubChatModel(reply=reply())
    await ask(office, theirs, user_id=999)
    assert "Работает из Алматы" not in theirs.prompts[0][1]


async def test_a_personal_fact_from_a_stranger_is_discarded(office) -> None:
    """There is nobody to file it under, and filing it under a guess is worse."""
    model = StubChatModel(reply=reply(remember={"scope": "user", "fact": "любит кофе"}))
    await ask(office, model, user_id=999)

    assert not SqlChatMemoryStore(office).facts(memory.EMPLOYEE, "anya", limit=10)


async def test_the_same_fact_twice_is_stored_once(office) -> None:
    model = StubChatModel(reply=reply(remember={"scope": "office", "fact": "Стендап в 10:30"}))
    await ask(office, model)
    await ask(office, model)

    assert len(SqlChatMemoryStore(office).facts(memory.OFFICE, "ovest", limit=10)) == 1


async def test_remembering_can_be_switched_off(office) -> None:
    model = StubChatModel(reply=reply(remember={"scope": "office", "fact": "Стендап в 10:30"}))
    await ask(office, model, remember=False)

    assert not SqlChatMemoryStore(office).facts(memory.OFFICE, "ovest", limit=10)
    assert "remember" not in model.prompts[0][0]


async def test_a_new_fact_evicts_the_oldest_one(office) -> None:
    store = SqlChatMemoryStore(office)
    for index in range(5):
        store.remember(memory.OFFICE, "ovest", f"факт {index}", keep=3, at=NOW)

    facts = [item.fact for item in store.facts(memory.OFFICE, "ovest", limit=10)]
    assert facts == ["факт 2", "факт 3", "факт 4"]


async def test_two_offices_do_not_share_a_memory(office) -> None:
    store = SqlChatMemoryStore(office)
    store.remember(memory.OFFICE, "ovest", "наш факт", keep=5, at=NOW)
    assert not store.facts(memory.OFFICE, "pine", limit=5)


# -------------------------------------------------------------------- answer handling


async def test_prose_instead_of_json_is_still_answered(office) -> None:
    """A model that ignored the contract still said something useful."""
    result = await ask(office, StubChatModel(reply="Завтра, как обычно."))
    assert result.answered
    assert result.text == "Завтра, как обычно."


async def test_tokens_are_recorded_so_the_cost_is_visible(office) -> None:
    model = StubChatModel(reply=reply(), prompt_tokens=120, completion_tokens=30)
    await ask(office, model)

    assert SqlUsageStore(office).tokens_today(FRIDAY) == 150


async def test_a_failed_call_records_the_attempt_but_no_tokens(office) -> None:
    await ask(office, StubChatModel(reply=None))

    usage = SqlUsageStore(office)
    assert usage.total_today(FRIDAY) == 1
    assert usage.tokens_today(FRIDAY) == 0


async def test_json_is_asked_for(office) -> None:
    model = StubChatModel(reply=reply())
    await ask(office, model)
    assert model.json_requested == [True]
