"""What the chat carries into a prompt: the conversation, today's date, and what it can
look up.

The bug this exists to prevent is the one that shipped: a bot that had no idea what day
it was, insisting on Monday to somebody writing on a Friday, with no way for them to
correct it because nothing it had said a moment earlier was still in front of it.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.repositories import (
    SqlLedgerStore,
    SqlMessageCache,
    SqlNoteStore,
    SqlOfficeStore,
    SqlScheduleStore,
    SqlUsageStore,
)
from tabelshchik.adapters.fakes import ScriptedChatModel, StubChatModel
from tabelshchik.application import chat_tools
from tabelshchik.application.chat import ChatRefusal, Thread, answer
from tabelshchik.application.policy import ChatPolicy, SchedulePolicy
from tabelshchik.application.ports import (
    CachedMessage,
    ModelTurn,
    NoteScope,
    Role,
    ToolCall,
)
from tabelshchik.application.regenerate_schedule import regenerate
from tabelshchik.application.voice import MoodPolicy, Voice
from tabelshchik.bootstrap.mapping import catalog
from tabelshchik.config.loader import parse_file
from tabelshchik.config.messages import MessagesConfig
from tabelshchik.domain.mood import Mood

from .conftest import office_seed, seed

CATALOG = catalog(parse_file(Path("config/messages.yaml"), MessagesConfig))
FRIDAY = date(2026, 9, 11)
NOW = datetime(2026, 9, 11, 12, 0)
CHAT = -100123
#: The Telegram user id the fixture's Аня is linked to.
ANYA = 777


def reply(text: str = "Ага.", *, notes: list | None = None, forget: list | None = None) -> str:
    payload: dict[str, object] = {"reply": text}
    if notes is not None:
        payload["notes"] = notes
    if forget is not None:
        payload["forget"] = forget
    return json.dumps(payload, ensure_ascii=False)


def said(text: str = "Ага.", **kwargs) -> ModelTurn:
    return ModelTurn(text=reply(text, **kwargs), prompt_tokens=100, completion_tokens=20)


def calls(*names: str, arguments: str = "{}") -> ModelTurn:
    return ModelTurn(
        tool_calls=tuple(
            ToolCall(id=f"call-{index}", name=name, arguments=arguments)
            for index, name in enumerate(names)
        ),
        prompt_tokens=50,
        completion_tokens=5,
    )


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
    message_id: int | None = None,
    private: bool = False,
    reach: tuple[str, ...] = (),
    office_id: str = "ovest",
    **policy_kwargs,
):
    return await answer(
        question=question,
        user_id=user_id,
        office_id=office_id,
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        usage=SqlUsageStore(sessions),
        voice=Voice(catalog=CATALOG, moods=MoodPolicy(weights={Mood.TOXIC: 1}), model=model),
        clock=FixedClock(NOW),
        policy=ChatPolicy(**policy_kwargs),
        thread=Thread(
            chat_id=CHAT, reply_to_message_id=reply_to, message_id=message_id, is_private=private
        ),
        messages=SqlMessageCache(sessions),
        notes=SqlNoteStore(sessions),
        reach=reach,
    )


def cache(
    sessions,
    message_id: int,
    text: str,
    *,
    parent: int | None = None,
    who: str = "Аня",
    at: datetime = NOW,
):
    SqlMessageCache(sessions).remember(
        CHAT,
        CachedMessage(message_id=message_id, author=who, text=text, reply_to_message_id=parent),
        at=at,
    )


def tool_outputs(model: ScriptedChatModel, call: int = 1) -> list[str]:
    turns, _, _ = model.calls[call]
    return [turn.content or "" for turn in turns if turn.role is Role.TOOL]


def notes_of(sessions, scope: NoteScope, subject: str) -> list[tuple[str, str]]:
    return [
        (note.topic, note.text) for note in SqlNoteStore(sessions).notes(scope, subject, limit=50)
    ]


def two_offices_in_one_chat(sessions) -> None:
    seed(
        sessions,
        office_seed(),
        office_seed(id="pine", name="Pine", employees=[{"id": "zed", "name": "Зед"}]),
    )
    SqlOfficeStore(sessions).link_telegram_user("anya", ANYA)


def regenerated(sessions) -> None:
    regenerate(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        ledger=SqlLedgerStore(sessions),
        clock=FixedClock(NOW),
        policy=SchedulePolicy(horizon_weeks=3, freeze_weeks=0),
    )


# ------------------------------------------------------------------- always in view


async def test_the_prompt_says_what_day_and_time_it_is(office) -> None:
    """Without it the model cannot know it is Friday, which is how it ends up insisting
    on Monday to somebody who just told it otherwise."""
    model = StubChatModel(reply=reply())
    await ask(office, model)

    assert "Сегодня: Пятница, 11 сентября 2026 года. Время: 12:00." in model.prompts[0][1]


async def test_the_model_is_told_not_to_volunteer_the_calendar(office) -> None:
    model = StubChatModel(reply=reply())
    await ask(office, model)
    assert "если тебя об этом не" in model.prompts[0][0]


async def test_the_reply_follows_the_language_of_the_question(office) -> None:
    model = StubChatModel(reply=reply())
    await ask(office, model, question="How do I reverse a list in Python?")

    system = model.prompts[0][0]
    rules = system.split("\n\n")[1]
    assert rules.startswith("Отвечай на том языке, на котором написано сообщение")
    assert "Пиши на русском" not in system


async def test_the_persona_no_longer_prescribes_joke_topics(office) -> None:
    """The Monday fixation came from the persona listing Mondays as an allowed subject.

    The model repeated the one topic it had been handed, every day of the week.
    """
    model = StubChatModel(reply=reply())
    await ask(office, model)
    assert "понедельник" not in model.prompts[0][0].lower()


async def test_the_prompt_says_who_is_writing_and_where(office) -> None:
    model = StubChatModel(reply=reply())
    await ask(office, model)

    prompt = model.prompts[0][1]
    assert "Пишет: Аня, офис «O'Vest»." in prompt
    assert "Чат: группа офиса «O'Vest»." in prompt


async def test_a_woman_is_addressed_in_the_feminine(sessions) -> None:
    seed(sessions, office_seed(employees=[{"id": "anya", "name": "Аня", "gender": "female"}]))
    SqlOfficeStore(sessions).link_telegram_user("anya", ANYA)
    model = StubChatModel(reply=reply())

    await ask(sessions, model)

    assert "Пишет: Аня (женщина, обращайся к ней в женском роде)" in model.prompts[0][1]


async def test_the_default_gender_is_not_passed_on(office) -> None:
    model = StubChatModel(reply=reply())
    await ask(office, model)
    assert "роде" not in model.prompts[0][1]


async def test_somebody_off_the_roster_is_described_as_such(office) -> None:
    model = StubChatModel(reply=reply())
    await ask(office, model, user_id=999)
    assert "не сотрудник ни одного офиса" in model.prompts[0][1]


async def test_a_private_chat_says_so(office) -> None:
    model = StubChatModel(reply=reply())
    await ask(office, model, private=True)
    assert "Чат: личная переписка с тобой." in model.prompts[0][1]


async def test_a_shared_group_names_every_office_in_it(sessions) -> None:
    two_offices_in_one_chat(sessions)
    model = StubChatModel(reply=reply())

    await ask(sessions, model, reach=("ovest", "pine"))

    assert "Чат: общая группа офисов «O'Vest», «Pine»." in model.prompts[0][1]


# ---------------------------------------------------------------- the conversation


async def test_a_thread_is_quoted_oldest_first(office) -> None:
    cache(office, 1, "Кто завтра в офисе?")
    cache(office, 2, "Я и Боря", parent=1, who="Табельщик")
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=2)

    prompt = model.prompts[0][1]
    assert "Переписка в этом чате" in prompt
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
    assert "): сообщение 7\n" not in prompt
    assert "Аня: сообщение 7\n" not in prompt
    assert "сообщение 6" not in prompt


async def test_depth_zero_switches_the_reply_chain_off(office) -> None:
    cache(office, 1, "что-то важное")
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=1, reply_depth=0)

    assert "что-то важное" not in model.prompts[0][1]


async def test_a_chain_that_runs_past_what_we_cached_stops_quietly(office) -> None:
    """A partial chain is right; an error is not."""
    cache(office, 9, "видимое", parent=8)  # 8 was never cached
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=9)

    assert "видимое" in model.prompts[0][1]


async def test_the_recent_conversation_is_there_without_a_reply(office) -> None:
    cache(office, 1, "Когда я в офисе?")
    cache(office, 2, "В понедельник и среду.", parent=1, who="Табельщик")
    model = StubChatModel(reply=reply())

    await ask(office, model, question="а Боря?", message_id=3, private=True)

    prompt = model.prompts[0][1]
    assert "#1 [12:00] Аня: Когда я в офисе?" in prompt
    assert "#2 [12:00] Табельщик (ответ на #1): В понедельник и среду." in prompt


async def test_what_two_other_people_said_to_each_other_is_in_view(office) -> None:
    cache(office, 1, "Обед в пятницу переносим?", who="Боря")
    cache(office, 2, "Да, на четверг", parent=1, who="Вера")
    model = StubChatModel(reply=reply())

    await ask(office, model, question="@bot что решили?", message_id=3)

    assert "#2 [12:00] Вера (ответ на #1): Да, на четверг" in model.prompts[0][1]


async def test_the_question_itself_is_not_quoted_back(office) -> None:
    cache(office, 6, "Сколько стоит кофе?")
    model = StubChatModel(reply=reply())

    await ask(office, model, question="Сколько стоит кофе?", message_id=6)

    assert model.prompts[0][1].count("Сколько стоит кофе?") == 1


async def test_a_message_that_arrived_while_answering_is_not_in_view(office) -> None:
    cache(office, 7, "написано позже")
    model = StubChatModel(reply=reply())

    await ask(office, model, message_id=6)

    assert "написано позже" not in model.prompts[0][1]


async def test_a_long_conversation_keeps_its_newest_end(office) -> None:
    for index in range(1, 41):
        cache(office, index, f"реплика номер {index}")
    model = StubChatModel(reply=reply())

    await ask(office, model, message_id=41, history_chars=500)

    prompt = model.prompts[0][1]
    assert "реплика номер 40" in prompt
    assert "реплика номер 1\n" not in prompt


async def test_the_question_says_which_message_it_answers(office) -> None:
    cache(office, 1, "Кто завтра в офисе?", who="Боря")
    model = StubChatModel(reply=reply())

    await ask(office, model, reply_to=1, message_id=2)

    assert "Сообщение, на которое ты отвечаешь (ответ на #1):" in model.prompts[0][1]


async def test_an_older_message_carries_its_date(office) -> None:
    cache(office, 1, "вчерашнее", at=NOW - timedelta(days=1))
    model = StubChatModel(reply=reply())

    await ask(office, model, message_id=2)

    assert "[10 сентября 12:00]" in model.prompts[0][1]


# ---------------------------------------------------------------------- on demand


async def test_a_general_question_is_answered_in_one_call_without_lookups(office) -> None:
    model = StubChatModel(reply=reply("Через reversed()."))

    result = await ask(office, model, question="Как развернуть список в Python?")

    assert result.answered
    assert len(model.conversations) == 1
    assert result.lookups == ()


async def test_the_first_prompt_carries_no_schedule_and_no_notes(office) -> None:
    regenerated(office)
    SqlNoteStore(office).write(
        NoteScope.PERSON, str(ANYA), "день рождения", "3 марта", until=None, keep=9, at=NOW
    )
    model = StubChatModel(reply=reply())

    await ask(office, model)

    prompt = model.prompts[0][1]
    assert "3 марта" not in prompt
    assert "Дни собеседника" not in prompt


async def test_the_schedule_and_the_notes_are_offered_as_lookups(office) -> None:
    model = StubChatModel(reply=reply())
    await ask(office, model)

    _, tools = model.conversations[0]
    assert [tool.name for tool in tools] == [chat_tools.SCHEDULE, chat_tools.RECALL]


async def test_without_memory_there_is_nothing_to_recall_and_nothing_to_write(office) -> None:
    model = StubChatModel(reply=reply())
    await ask(office, model, memory=False)

    _, tools = model.conversations[0]
    assert [tool.name for tool in tools] == [chat_tools.SCHEDULE]
    assert '"notes"' not in model.prompts[0][0]


async def test_a_schedule_question_looks_the_schedule_up(office) -> None:
    regenerated(office)
    model = ScriptedChatModel([calls(chat_tools.SCHEDULE), said("В понедельник.")])

    result = await ask(office, model, question="Когда мой следующий день в офисе?")

    assert result.text == "В понедельник."
    assert result.lookups == (chat_tools.SCHEDULE,)
    [schedule] = tool_outputs(model)
    assert "Офис «O'Vest»" in schedule
    assert "Дни собеседника: " in schedule
    assert "сентября" in schedule


async def test_a_shared_group_can_look_at_the_other_office_by_name(sessions) -> None:
    two_offices_in_one_chat(sessions)
    model = ScriptedChatModel(
        [calls(chat_tools.SCHEDULE, arguments='{"office": "Pine"}'), said("Никого.")]
    )

    await ask(sessions, model, reach=("ovest", "pine"))

    _, tools, _ = model.calls[0]
    schedule_tool = next(tool for tool in tools if tool.name == chat_tools.SCHEDULE)
    properties = schedule_tool.parameters["properties"]
    assert isinstance(properties, dict)
    assert properties["office"]["enum"] == ["O'Vest", "Pine"]
    [schedule] = tool_outputs(model)
    assert schedule.startswith("Офис «Pine»")
    assert "Дни собеседника" not in schedule


async def test_an_office_outside_the_chat_is_refused(office) -> None:
    model = ScriptedChatModel(
        [calls(chat_tools.SCHEDULE, arguments='{"office": "Elsewhere"}'), said()]
    )

    await ask(office, model)

    assert tool_outputs(model)[0].startswith("Ошибка")


async def test_a_recall_reads_the_notes_back(office) -> None:
    store = SqlNoteStore(office)
    store.write(NoteScope.PERSON, str(ANYA), "день рождения", "3 марта", until=None, keep=9, at=NOW)
    store.write(NoteScope.OFFICE, "ovest", "кофемашина", "Сломана", until=None, keep=9, at=NOW)
    model = ScriptedChatModel([calls(chat_tools.RECALL), said("Третьего марта.")])

    result = await ask(office, model, question="Когда у меня день рождения?")

    assert result.text == "Третьего марта."
    [recalled] = tool_outputs(model)
    assert "- день рождения: 3 марта (записано 11 сентября 2026)" in recalled
    assert "- кофемашина: Сломана" in recalled


async def test_somebody_elses_personal_notes_are_never_recalled(office) -> None:
    SqlNoteStore(office).write(
        NoteScope.PERSON, "888", "здоровье", "Чужая тайна", until=None, keep=9, at=NOW
    )
    model = ScriptedChatModel([calls(chat_tools.RECALL), said()])

    await ask(office, model)

    assert "Чужая тайна" not in tool_outputs(model)[0]


async def test_bad_arguments_come_back_as_an_error_not_a_failure(office) -> None:
    model = ScriptedChatModel([calls(chat_tools.SCHEDULE, arguments="не json"), said("Ок.")])

    result = await ask(office, model)

    assert result.answered
    assert tool_outputs(model)[0].startswith("Ошибка")


async def test_every_tool_call_gets_an_answer_even_past_the_cap(office) -> None:
    model = ScriptedChatModel([calls(*[chat_tools.SCHEDULE] * 5), said()])

    result = await ask(office, model)

    outputs = tool_outputs(model)
    assert len(outputs) == 5
    assert outputs[3:] == [chat_tools.SKIPPED, chat_tools.SKIPPED]
    assert len(result.lookups) == chat_tools.CALLS_PER_ROUND


async def test_the_last_round_has_to_answer(office) -> None:
    model = ScriptedChatModel(
        [calls(chat_tools.SCHEDULE), calls(chat_tools.RECALL), said("Готово.")]
    )

    result = await ask(office, model, tool_rounds=2)

    assert result.text == "Готово."
    assert [allow for _, _, allow in model.calls] == [True, True, False]


async def test_a_model_that_keeps_calling_tools_fails_rather_than_loops(office) -> None:
    model = ScriptedChatModel([calls(chat_tools.SCHEDULE)] * 3)

    result = await ask(office, model, tool_rounds=2)

    assert result.refusal is ChatRefusal.MODEL_FAILED
    assert len(model.calls) == 3


async def test_one_question_counts_once_however_many_rounds(office) -> None:
    model = ScriptedChatModel([calls(chat_tools.SCHEDULE), said()])

    await ask(office, model)

    usage = SqlUsageStore(office)
    assert usage.total_today(FRIDAY) == 1
    assert usage.tokens_today(FRIDAY) == 55 + 120


# --------------------------------------------------------------------------- notes


async def test_a_note_the_model_asks_to_keep_is_stored_for_that_person(office) -> None:
    note = {"about": "me", "topic": "день рождения", "text": "3 марта"}
    await ask(office, StubChatModel(reply=reply(notes=[note])))

    assert notes_of(office, NoteScope.PERSON, str(ANYA)) == [("день рождения", "3 марта")]


async def test_most_answers_keep_nothing(office) -> None:
    await ask(office, StubChatModel(reply=reply(notes=[], forget=[])))

    assert notes_of(office, NoteScope.PERSON, str(ANYA)) == []
    assert notes_of(office, NoteScope.OFFICE, "ovest") == []


async def test_the_same_topic_updates_the_note(office) -> None:
    broken = {"about": "office", "topic": "кофемашина", "text": "Сломана"}
    fixed = {"about": "office", "topic": "Кофемашина", "text": "Снова работает"}

    await ask(office, StubChatModel(reply=reply(notes=[broken])))
    await ask(office, StubChatModel(reply=reply(notes=[fixed])))

    assert notes_of(office, NoteScope.OFFICE, "ovest") == [("кофемашина", "Снова работает")]


async def test_a_note_about_one_persons_birthday_does_not_overwrite_anothers(office) -> None:
    borya = {"about": "office", "topic": "день рождения Бори", "text": "У Бори день рождения 5 мая"}
    petya = {"about": "office", "topic": "день рождения Пети", "text": "У Пети день рождения 5 мая"}

    await ask(office, StubChatModel(reply=reply(notes=[borya, petya])))

    assert len(notes_of(office, NoteScope.OFFICE, "ovest")) == 2


async def test_the_same_fact_under_a_new_topic_is_not_kept_twice(office) -> None:
    first = {"about": "office", "topic": "стендап", "text": "Стендап в 10:30"}
    again = {"about": "office", "topic": "утренний созвон", "text": "Стендап в 10:30"}

    await ask(office, StubChatModel(reply=reply(notes=[first])))
    await ask(office, StubChatModel(reply=reply(notes=[again])))

    assert notes_of(office, NoteScope.OFFICE, "ovest") == [("стендап", "Стендап в 10:30")]


async def test_a_note_can_be_forgotten(office) -> None:
    broken = {"about": "office", "topic": "кофемашина", "text": "Сломана"}
    await ask(office, StubChatModel(reply=reply(notes=[broken])))

    await ask(
        office,
        StubChatModel(reply=reply(forget=[{"about": "office", "topic": "кофемашина"}])),
    )

    assert notes_of(office, NoteScope.OFFICE, "ovest") == []


async def test_somebody_off_the_roster_cannot_write_into_an_office(office) -> None:
    office_note = {"about": "office", "topic": "кофемашина", "text": "Сломана"}
    own_note = {"about": "me", "topic": "кофе", "text": "Пьет без сахара"}

    await ask(office, StubChatModel(reply=reply(notes=[office_note, own_note])), user_id=999)

    assert notes_of(office, NoteScope.OFFICE, "ovest") == []
    assert notes_of(office, NoteScope.PERSON, "999") == [("кофе", "Пьет без сахара")]


async def test_a_shared_group_files_office_notes_under_the_askers_own_office(sessions) -> None:
    two_offices_in_one_chat(sessions)
    SqlOfficeStore(sessions).link_telegram_user("zed", 555)
    note = {"about": "office", "topic": "кофемашина", "text": "Сломана"}

    await ask(
        sessions,
        StubChatModel(reply=reply(notes=[note])),
        user_id=555,
        office_id="pine",
        reach=("pine", "ovest"),
    )

    assert notes_of(sessions, NoteScope.OFFICE, "pine") == [("кофемашина", "Сломана")]
    assert notes_of(sessions, NoteScope.OFFICE, "ovest") == []


async def test_remembering_can_be_switched_off(office) -> None:
    note = {"about": "me", "topic": "день рождения", "text": "3 марта"}
    await ask(office, StubChatModel(reply=reply(notes=[note])), memory=False)

    assert notes_of(office, NoteScope.PERSON, str(ANYA)) == []


async def test_nothing_is_written_when_the_answer_failed(office) -> None:
    note = {"about": "me", "topic": "день рождения", "text": "3 марта"}
    await ask(office, StubChatModel(reply=reply("", notes=[note])))

    assert notes_of(office, NoteScope.PERSON, str(ANYA)) == []


async def test_a_note_beyond_the_cap_evicts_the_one_least_recently_used(office) -> None:
    store = SqlNoteStore(office)
    for index in range(3):
        store.write(
            NoteScope.OFFICE,
            "ovest",
            f"тема {index}",
            f"факт {index}",
            until=None,
            keep=3,
            at=NOW + timedelta(minutes=index),
        )
    [oldest] = [
        note for note in store.notes(NoteScope.OFFICE, "ovest", limit=9) if note.topic == "тема 0"
    ]
    store.touch([oldest.id], at=NOW + timedelta(hours=1))

    store.write(
        NoteScope.OFFICE,
        "ovest",
        "тема 3",
        "факт 3",
        until=None,
        keep=3,
        at=NOW + timedelta(hours=2),
    )

    topics = sorted(note.topic for note in store.notes(NoteScope.OFFICE, "ovest", limit=9))
    assert topics == ["тема 0", "тема 2", "тема 3"]


async def test_two_offices_do_not_share_notes(office) -> None:
    store = SqlNoteStore(office)
    store.write(NoteScope.OFFICE, "ovest", "стендап", "наш факт", until=None, keep=5, at=NOW)
    assert not store.notes(NoteScope.OFFICE, "pine", limit=5)


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
