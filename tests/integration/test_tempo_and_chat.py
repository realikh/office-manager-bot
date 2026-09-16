from __future__ import annotations

import html
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.repositories import (
    SqlLedgerStore,
    SqlOfficeAdminStore,
    SqlOfficeStore,
    SqlPostStore,
    SqlRosterStore,
    SqlScheduleStore,
    SqlUsageStore,
)
from tabelshchik.adapters.fakes import RecordingNotifier, StubChatModel
from tabelshchik.application.chat import MAX_ANSWER_LENGTH, ChatRefusal, answer
from tabelshchik.application.policy import ChatPolicy, SchedulePolicy, TempoPolicy
from tabelshchik.application.regenerate_schedule import regenerate
from tabelshchik.application.send_tempo_reminder import (
    TempoKind,
    TempoOccasion,
    send_tempo_reminder,
    tempo_occasion,
)
from tabelshchik.application.voice import MoodPolicy, Voice
from tabelshchik.bootstrap.mapping import catalog, silent_policy
from tabelshchik.config.loader import parse_file
from tabelshchik.config.messages import MessagesConfig
from tabelshchik.config.models import AppConfig
from tabelshchik.domain.calendar import CalendarSpec
from tabelshchik.domain.mood import Mood

from .conftest import office_seed, seed

CATALOG = catalog(parse_file(Path("config/messages.yaml"), MessagesConfig))
SILENT = silent_policy(parse_file(Path("config/app.yaml"), AppConfig))

FRIDAY = date(2026, 9, 18)
THURSDAY = date(2026, 9, 17)
SATURDAY = date(2026, 9, 19)
NEXT_FRIDAY = date(2026, 9, 25)
MONDAY = date(2026, 9, 14)
CHAT = -100123


def voice(model=None, mood=Mood.TOXIC) -> Voice:
    return Voice(catalog=CATALOG, moods=MoodPolicy(weights={mood: 1}), model=model)


def at(day: date, hour: int = 9, minute: int = 0) -> FixedClock:
    return FixedClock(datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute))


# ------------------------------------------------------------------- when tempo fires


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (FRIDAY, TempoOccasion(week_end=True)),
        (MONDAY, None),
        (date(2026, 9, 23), None),  # the old early warning is gone
        (date(2026, 9, 30), TempoOccasion(month_end=True)),  # a Wednesday
        (date(2026, 9, 19), None),  # Saturday
    ],
)
def test_the_right_nag_fires_on_the_right_day(day: date, expected) -> None:
    assert tempo_occasion(CalendarSpec(), day, TempoPolicy()) == expected


def test_a_friday_that_ends_the_month_is_one_occasion_not_two() -> None:
    """31 October 2026 is a Saturday, so the Friday before closes both."""
    occasion = tempo_occasion(CalendarSpec(), date(2026, 10, 30), TempoPolicy())
    assert occasion == TempoOccasion(week_end=True, month_end=True)
    assert occasion is not None and occasion.kind is TempoKind.MONTH_END


def test_a_friday_holiday_moves_the_weekly_nag_to_thursday() -> None:
    spec = CalendarSpec(holidays=frozenset({FRIDAY}))
    assert tempo_occasion(spec, FRIDAY, TempoPolicy()) is None
    assert tempo_occasion(spec, THURSDAY, TempoPolicy()) == TempoOccasion(week_end=True)


def test_a_working_saturday_takes_the_weekly_nag_from_friday() -> None:
    spec = CalendarSpec(extra_workdays=frozenset({SATURDAY}))
    assert tempo_occasion(spec, FRIDAY, TempoPolicy()) is None
    assert tempo_occasion(spec, SATURDAY, TempoPolicy()) == TempoOccasion(week_end=True)


def test_month_end_lands_on_the_last_working_day_not_the_last_day() -> None:
    # 31 January 2027 is a Sunday; the Friday before is both.
    assert tempo_occasion(CalendarSpec(), date(2027, 1, 31), TempoPolicy()) is None
    assert tempo_occasion(CalendarSpec(), date(2027, 1, 29), TempoPolicy()) == TempoOccasion(
        week_end=True, month_end=True
    )


def test_a_disabled_policy_never_fires() -> None:
    assert tempo_occasion(CalendarSpec(), FRIDAY, TempoPolicy(enabled=False)) is None


# ------------------------------------------------------------------ the tempo message


async def tempo(sessions, clock, notifier=None, *, model=None, posts=None, **kwargs):
    return await send_tempo_reminder(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        notifier=notifier or RecordingNotifier(),
        voice=voice(model),
        clock=clock,
        policy=TempoPolicy(url="https://tempo.example/my-work"),
        silent_policy=SILENT,
        posts=posts or SqlPostStore(sessions),
        **kwargs,
    )


def generated(text: str) -> StubChatModel:
    return StubChatModel(reply=json.dumps({"text": text}, ensure_ascii=False))


async def test_everyone_is_tagged_not_just_those_in_the_office(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()

    outcome = await tempo(sessions, at(FRIDAY), notifier)

    assert outcome.sent
    for name in ("Аня", "Боря", "Вера", "Глеб"):
        assert name in notifier.last_text


async def test_the_tempo_link_is_tappable_text(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()
    await tempo(sessions, at(FRIDAY), notifier)
    assert '<a href="https://tempo.example/my-work">Заполнить часы в Tempo</a>' in (
        notifier.last_text
    )


async def test_it_stays_quiet_on_a_day_nothing_is_due(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()

    outcome = await tempo(sessions, at(MONDAY), notifier)

    assert outcome.skipped == "not-due"
    assert notifier.messages == []


async def test_a_friday_that_ends_the_month_sends_one_message(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()
    model = StubChatModel(reply=None)

    outcome = await tempo(sessions, at(date(2026, 10, 30), 17, 50), notifier, model=model)

    assert outcome.kind is TempoKind.MONTH_END
    assert len(notifier.messages) == 1
    brief = model.prompts[0][1]
    assert "недели и месяца" in brief


async def test_a_mid_week_month_end_does_not_congratulate_on_the_week(sessions) -> None:
    seed(sessions)
    model = StubChatModel(reply=None)

    outcome = await tempo(sessions, at(date(2026, 9, 30), 17, 50), model=model)

    assert outcome.kind is TempoKind.MONTH_END
    assert "Неделя при этом ещё не закончилась" in model.prompts[0][1]


async def test_the_model_is_told_why_thursday_is_the_end_of_the_week(sessions) -> None:
    """Otherwise it congratulates a Thursday on being Friday."""
    seed(
        sessions,
        office_seed(
            calendar={"closed": [FRIDAY.isoformat()], "notes": {FRIDAY.isoformat(): "Субботник"}}
        ),
    )
    model = StubChatModel(reply=None)

    outcome = await tempo(sessions, at(THURSDAY, 17, 50), model=model)

    assert outcome.kind is TempoKind.WEEKLY
    brief = model.prompts[0][1]
    assert "Сегодня четверг" in brief
    assert "пятница — офис закрыт (Субботник)" in brief
    assert "хотя сегодня не пятница" in brief


async def test_ten_minutes_before_the_end_of_the_day_it_says_so(sessions) -> None:
    seed(sessions)
    model = StubChatModel(reply=None)

    outcome = await tempo(sessions, at(FRIDAY, 17, 50), model=model, gap_minutes=10)

    assert "последние 10 минут" in model.prompts[0][1]
    # The hand-written floor says it too, since the model returned nothing.
    assert not outcome.generated
    assert "10 минут" in outcome.text


async def test_well_before_the_end_of_the_day_there_is_no_countdown(sessions) -> None:
    seed(sessions)
    model = StubChatModel(reply=None)

    outcome = await tempo(sessions, at(FRIDAY, 17, 20), model=model, gap_minutes=None)

    assert "Не упоминай, сколько времени осталось" in model.prompts[0][1]
    assert "минут" not in outcome.text


async def test_the_model_writes_the_message_when_it_can(sessions) -> None:
    seed(sessions)
    line = "С пятницей! Потратьте последние 10 минут на Tempo, и свобода."

    outcome = await tempo(sessions, at(FRIDAY, 17, 50), model=generated(line), gap_minutes=10)

    assert outcome.generated
    assert outcome.text.startswith(line)


@pytest.mark.parametrize(
    "line",
    [
        "Потратьте 15 минут на Tempo.",  # not the gap it was given
        "До понедельника! Заполните Tempo.",  # another weekday
        "Заполните Tempo на tempo.example.com прямо сейчас.",  # its own link
        "Аня, заполни Tempo.",  # a name
    ],
)
async def test_a_generated_message_that_invents_something_is_replaced(sessions, line) -> None:
    seed(sessions)

    outcome = await tempo(sessions, at(FRIDAY, 17, 50), model=generated(line), gap_minutes=10)

    assert not outcome.generated
    assert line not in outcome.text
    assert "10 минут" in outcome.text


async def test_a_failed_model_still_sends_the_reminder(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()

    outcome = await tempo(sessions, at(FRIDAY), notifier, model=StubChatModel(reply="не json"))

    assert outcome.sent and not outcome.generated
    assert "Tempo" in notifier.last_text


async def test_someone_who_has_left_is_not_tagged(sessions) -> None:
    seed(
        sessions,
        office_seed(
            employees=[
                {"id": "anya", "name": "Аня"},
                {"id": "gone", "name": "Ушедший", "endedOn": "2026-01-01"},
            ]
        ),
    )
    notifier = RecordingNotifier()

    await tempo(sessions, at(FRIDAY), notifier)

    assert "Ушедший" not in notifier.last_text


async def test_every_mood_produces_a_tempo_message(sessions) -> None:
    seed(sessions)
    for mood in Mood:
        for gap in (None, 10):
            outcome = await send_tempo_reminder(
                office_id="ovest",
                offices=SqlOfficeStore(sessions),
                notifier=RecordingNotifier(),
                voice=voice(mood=mood),
                clock=at(FRIDAY),
                policy=TempoPolicy(),
                silent_policy=SILENT,
                posts=SqlPostStore(sessions),
                gap_minutes=gap,
                dry_run=True,
            )
            assert outcome.text


# ---------------------------------------------------------------------- pins, once a day


async def test_the_new_reminder_is_pinned(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()

    outcome = await tempo(sessions, at(FRIDAY), notifier)

    assert outcome.pinned
    assert notifier.pin_calls == [("pin", CHAT, notifier.last_message_id)]


async def test_the_old_pin_comes_down_first_even_after_a_restart(sessions) -> None:
    """The pin used to be remembered by the notifier, which a redeploy replaces — so the
    chat kept every Tempo reminder ever pinned. A fresh notifier has to find it anyway."""
    seed(sessions)
    first = RecordingNotifier()
    await tempo(sessions, at(FRIDAY), first)
    old = first.last_message_id

    restarted = RecordingNotifier(_next_id=5000)
    await tempo(sessions, at(NEXT_FRIDAY), restarted)

    assert restarted.pin_calls == [
        ("unpin", CHAT, old),
        ("pin", CHAT, restarted.last_message_id),
    ]


async def test_an_unpinned_reminder_is_not_unpinned_again(sessions) -> None:
    seed(sessions)
    await tempo(sessions, at(FRIDAY))
    await tempo(sessions, at(NEXT_FRIDAY))

    third = RecordingNotifier(_next_id=9000)
    await tempo(sessions, at(date(2026, 10, 2)), third)

    assert [call for call in third.pin_calls if call[0] == "unpin"] == [("unpin", CHAT, 1001)]


async def test_a_pin_that_failed_is_not_taken_down_later(sessions) -> None:
    """No rights to pin means nothing to unpin; asking would only log noise."""
    seed(sessions)
    await tempo(sessions, at(FRIDAY), RecordingNotifier(fail_pins=True))

    later = RecordingNotifier()
    await tempo(sessions, at(NEXT_FRIDAY), later)

    assert [call[0] for call in later.pin_calls] == ["pin"]


async def test_a_reminder_goes_out_once_a_day_whatever_the_scheduler_does(sessions) -> None:
    """Moving the time after it fired gives the day a second occurrence with a new key."""
    seed(sessions)
    notifier = RecordingNotifier()

    await tempo(sessions, at(FRIDAY, 17, 20), notifier)
    again = await tempo(sessions, at(FRIDAY, 17, 50), notifier)

    assert again.skipped == "already-sent"
    assert len(notifier.messages) == 1


async def test_a_failed_send_does_not_count_as_sent(sessions) -> None:
    seed(sessions)
    await tempo(sessions, at(FRIDAY), RecordingNotifier(fail=True))

    retried = await tempo(sessions, at(FRIDAY))
    assert retried.sent


async def test_a_closed_office_gets_no_tempo_reminder(sessions) -> None:
    seed(sessions)
    SqlOfficeAdminStore(sessions).set_active("ovest", False)

    outcome = await tempo(sessions, at(FRIDAY))

    assert outcome.skipped == "inactive"


# ------------------------------------------------------------------------ chat limits


async def ask(sessions, model=None, user_id=42, question="Когда я в офисе?", **policy_kwargs):
    return await answer(
        question=question,
        user_id=user_id,
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        usage=SqlUsageStore(sessions),
        voice=voice(model or StubChatModel(reply="Завтра.")),
        clock=at(MONDAY),
        policy=ChatPolicy(**policy_kwargs),
    )


async def test_a_question_gets_an_answer(sessions) -> None:
    seed(sessions)
    reply = await ask(sessions)
    assert reply.answered
    assert reply.text == "Завтра."


async def test_the_per_user_allowance_runs_out(sessions) -> None:
    seed(sessions)
    for _ in range(3):
        await ask(sessions, per_user_daily_limit=3)

    reply = await ask(sessions, per_user_daily_limit=3)

    assert reply.refusal is ChatRefusal.USER_LIMIT
    assert reply.text and reply.text != "Завтра."


async def test_one_persons_limit_does_not_affect_another(sessions) -> None:
    seed(sessions)
    for _ in range(3):
        await ask(sessions, user_id=1, per_user_daily_limit=3)

    assert (await ask(sessions, user_id=2, per_user_daily_limit=3)).answered


async def test_the_global_cap_is_a_cost_stop_loss(sessions) -> None:
    seed(sessions)
    for user in range(4):
        await ask(sessions, user_id=user, global_daily_limit=4)

    reply = await ask(sessions, user_id=99, global_daily_limit=4)
    assert reply.refusal is ChatRefusal.GLOBAL_LIMIT


async def test_a_failed_model_call_still_counts_against_the_allowance(sessions) -> None:
    """Otherwise a broken model would be an unlimited free way to spend money."""
    seed(sessions)
    await ask(sessions, model=StubChatModel(reply=None), per_user_daily_limit=1)

    reply = await ask(sessions, per_user_daily_limit=1)
    assert reply.refusal is ChatRefusal.USER_LIMIT


async def test_a_model_failure_says_so_rather_than_going_silent(sessions) -> None:
    seed(sessions)
    reply = await ask(sessions, model=StubChatModel(reply=None))
    assert reply.refusal is ChatRefusal.MODEL_FAILED
    assert reply.text


async def test_chat_can_be_switched_off(sessions) -> None:
    seed(sessions)
    reply = await ask(sessions, enabled=False)
    assert reply.refusal is ChatRefusal.DISABLED


async def test_an_empty_question_is_ignored(sessions) -> None:
    seed(sessions)
    assert (await ask(sessions, question="   ")).refusal is ChatRefusal.EMPTY


# ----------------------------------------------------------------------- grounding


async def test_the_model_is_told_the_askers_own_days(sessions) -> None:
    seed(sessions, office_seed(schedule={"vacantDesks": {"tuesday": 4}}))
    regenerate(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        ledger=SqlLedgerStore(sessions),
        clock=at(MONDAY),
        policy=SchedulePolicy(horizon_weeks=2, freeze_weeks=0),
    )
    SqlOfficeStore(sessions).link_telegram_user("anya", 42)
    model = StubChatModel(reply="ок")

    await ask(sessions, model=model)

    prompt = model.prompts[0][1]
    assert "Ближайшие дни" in prompt
    assert "сентября" in prompt


async def test_someone_unknown_gets_an_answer_without_personal_grounding(sessions) -> None:
    seed(sessions)
    model = StubChatModel(reply="ок")

    await ask(sessions, model=model, user_id=999999)

    assert "Ближайшие дни" not in model.prompts[0][1]


async def test_model_output_is_escaped_before_sending(sessions) -> None:
    """Model output is data, never markup and never a command."""
    seed(sessions)
    reply = await ask(sessions, model=StubChatModel(reply="<b>привет</b> <script>x</script>"))
    assert "<b>" not in reply.text
    assert "&lt;b&gt;" in reply.text


async def test_an_overlong_answer_is_truncated(sessions) -> None:
    seed(sessions)
    reply = await ask(sessions, model=StubChatModel(reply="а" * (MAX_ANSWER_LENGTH + 500)))
    assert len(reply.text) <= MAX_ANSWER_LENGTH


async def test_a_detailed_answer_is_not_cut_short(sessions) -> None:
    """The old ceiling of 1500 characters clipped every answer that explained anything."""
    seed(sessions)
    long = "\n".join(f"• пункт номер {index}: подробное объяснение" for index in range(80))
    reply = await ask(sessions, model=StubChatModel(reply=json.dumps({"reply": long})))
    assert reply.text == long


async def test_the_model_is_told_to_answer_anything_at_whatever_length_it_needs(
    sessions,
) -> None:
    seed(sessions)
    model = StubChatModel(reply="ок")

    await ask(sessions, model=model)

    system = model.prompts[0][0]
    assert "Отвечай на любые вопросы" in system
    assert "подробно и полно" in system
    assert "одно-три предложения" not in system


async def test_an_answer_cut_off_by_the_token_ceiling_is_recovered_not_sent_as_json(
    sessions,
) -> None:
    """A reply that ran out of tokens is unparseable JSON. Sending it as-is put
    `{"reply": "…` in front of the whole chat."""
    seed(sessions)
    cut = '{"reply": "Сначала установите пакет,\\nпотом настройте \\"конфиг\\" и запусти'

    reply = await ask(sessions, model=StubChatModel(reply=cut))

    assert reply.answered
    assert "{" not in reply.text
    assert reply.text == (
        "Сначала установите пакет,\nпотом настройте &quot;конфиг&quot; и запусти…"
    )


async def test_prose_instead_of_json_is_still_an_answer(sessions) -> None:
    seed(sessions)
    reply = await ask(sessions, model=StubChatModel(reply="Просто текст без JSON."))
    assert reply.text == "Просто текст без JSON."


# --------------------------------------------------------------------- running out


async def test_somebody_over_their_limit_is_told_off_in_many_ways(sessions) -> None:
    """A fresh retort each time reads as a bot sulking on purpose, not a stuck one."""
    seed(sessions)
    await ask(sessions, per_user_daily_limit=1)

    replies = [
        await ask(sessions, per_user_daily_limit=1, question=f"ну ответь {index}")
        for index in range(12)
    ]

    pool = {html.escape(line) for line in CATALOG.pool("ai.rateLimited", Mood.TOXIC)}
    assert all(reply.refusal is ChatRefusal.USER_LIMIT for reply in replies)
    assert all(reply.text in pool for reply in replies)
    assert len({reply.text for reply in replies}) > 1


async def test_refusals_do_not_spend_anything(sessions) -> None:
    seed(sessions)
    model = StubChatModel(reply="ок")
    await ask(sessions, model=model, per_user_daily_limit=1)
    await ask(sessions, model=model, per_user_daily_limit=1)
    await ask(sessions, model=model, per_user_daily_limit=1)
    assert len(model.prompts) == 1


async def test_the_office_wide_cap_does_not_blame_the_person_asking(sessions) -> None:
    seed(sessions)
    await ask(sessions, user_id=1, global_daily_limit=1)

    reply = await ask(sessions, user_id=2, global_daily_limit=1)

    pool = {html.escape(line) for line in CATALOG.pool("ai.globalLimited", Mood.TOXIC)}
    assert reply.refusal is ChatRefusal.GLOBAL_LIMIT
    assert reply.text in pool


async def test_the_question_is_capped_before_it_reaches_the_model(sessions) -> None:
    seed(sessions)
    model = StubChatModel(reply="ок")
    await ask(sessions, model=model, question="б" * 5000)
    assert len(model.prompts[0][1]) < 2000


# ------------------------------------------------------------- per-person AI allowances


async def test_somebody_singled_out_gets_their_own_allowance(sessions) -> None:
    """The point of the feature: one person can be moved without moving anybody else."""
    seed(sessions)
    offices = SqlOfficeStore(sessions)
    offices.link_telegram_user("anya", 42)
    SqlRosterStore(sessions).set_ai_limit("anya", 1)

    assert (await ask(sessions, user_id=42, per_user_daily_limit=10)).answered
    assert (await ask(sessions, user_id=42, per_user_daily_limit=10)).refusal is (
        ChatRefusal.USER_LIMIT
    )


async def test_an_override_can_be_more_generous_than_the_default(sessions) -> None:
    seed(sessions)
    offices = SqlOfficeStore(sessions)
    offices.link_telegram_user("anya", 42)
    SqlRosterStore(sessions).set_ai_limit("anya", 3)

    for _ in range(3):
        assert (await ask(sessions, user_id=42, per_user_daily_limit=1)).answered
    assert (await ask(sessions, user_id=42, per_user_daily_limit=1)).refusal is (
        ChatRefusal.USER_LIMIT
    )


async def test_raising_the_default_moves_everybody_who_was_never_singled_out(sessions) -> None:
    """Why the column is nullable rather than backfilled with today's number.

    Storing a copy of the default on every employee would freeze the roster at whatever
    it happened to be, and make changing the default do nothing.
    """
    seed(sessions)
    SqlOfficeStore(sessions).link_telegram_user("anya", 42)

    await ask(sessions, user_id=42, per_user_daily_limit=1)
    assert (await ask(sessions, user_id=42, per_user_daily_limit=1)).refusal is (
        ChatRefusal.USER_LIMIT
    )
    assert (await ask(sessions, user_id=42, per_user_daily_limit=5)).answered


async def test_zero_means_no_replies_at_all(sessions) -> None:
    """Distinct from None, which means "no limit of their own"."""
    seed(sessions)
    SqlOfficeStore(sessions).link_telegram_user("anya", 42)
    SqlRosterStore(sessions).set_ai_limit("anya", 0)

    assert (await ask(sessions, user_id=42, per_user_daily_limit=10)).refusal is (
        ChatRefusal.USER_LIMIT
    )


async def test_clearing_an_override_puts_somebody_back_on_the_default(sessions) -> None:
    seed(sessions)
    SqlOfficeStore(sessions).link_telegram_user("anya", 42)
    roster = SqlRosterStore(sessions)

    roster.set_ai_limit("anya", 0)
    assert (await ask(sessions, user_id=42, per_user_daily_limit=10)).refusal is (
        ChatRefusal.USER_LIMIT
    )

    roster.set_ai_limit("anya", None)
    assert (await ask(sessions, user_id=42, per_user_daily_limit=10)).answered


async def test_somebody_with_no_roster_record_gets_the_default(sessions) -> None:
    """An admin who is not an employee anywhere has nowhere to hang an override, and
    locking the bot's own operators out of it would be the wrong reading."""
    seed(sessions)
    assert (await ask(sessions, user_id=999999, per_user_daily_limit=1)).answered
