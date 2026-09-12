from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.repositories import (
    SqlLedgerStore,
    SqlOfficeStore,
    SqlRosterStore,
    SqlScheduleStore,
    SqlUsageStore,
)
from tabelshchik.adapters.fakes import RecordingNotifier, StubChatModel
from tabelshchik.application.chat import ChatRefusal, answer
from tabelshchik.application.policy import ChatPolicy, SchedulePolicy, TempoPolicy
from tabelshchik.application.regenerate_schedule import regenerate
from tabelshchik.application.send_tempo_reminder import (
    TempoKind,
    due_kind,
    send_tempo_reminder,
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
MONDAY = date(2026, 9, 14)


def voice(model=None, mood=Mood.TOXIC) -> Voice:
    return Voice(catalog=CATALOG, moods=MoodPolicy(weights={mood: 1}), model=model)


def at(day: date, hour: int = 9) -> FixedClock:
    return FixedClock(datetime.combine(day, datetime.min.time()).replace(hour=hour))


# ------------------------------------------------------------------- when tempo fires


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (FRIDAY, TempoKind.WEEKLY),
        (MONDAY, None),
        (date(2026, 9, 23), TempoKind.MONTH_WARNING),  # the configured 23rd, a Wednesday
        (date(2026, 9, 30), TempoKind.MONTH_END),  # last working day of September
        (date(2026, 9, 19), None),  # Saturday
    ],
)
def test_the_right_nag_fires_on_the_right_day(day: date, expected) -> None:
    assert due_kind(CalendarSpec(), day, TempoPolicy()) is expected


def test_month_end_beats_the_warning_when_they_collide() -> None:
    """Two nags on one day would be noise; the deadline is the more useful one."""
    policy = TempoPolicy(warning_day=30)
    assert due_kind(CalendarSpec(), date(2026, 9, 30), policy) is TempoKind.MONTH_END


def test_the_warning_rolls_forward_off_a_weekend() -> None:
    policy = TempoPolicy(warning_day=26)  # a Saturday in September 2026
    assert due_kind(CalendarSpec(), date(2026, 9, 26), policy) is None
    assert due_kind(CalendarSpec(), date(2026, 9, 28), policy) is TempoKind.MONTH_WARNING


def test_the_warning_does_not_fire_retroactively() -> None:
    """If a working day has already gone by, the moment has passed and a late warning
    is just noise."""
    policy = TempoPolicy(warning_day=23)
    # The 24th is a Thursday, so nothing else is due either.
    assert due_kind(CalendarSpec(), date(2026, 9, 24), policy) is None


def test_month_end_lands_on_the_last_working_day_not_the_last_day() -> None:
    # 31 October 2026 is a Saturday.
    assert due_kind(CalendarSpec(), date(2026, 10, 31), TempoPolicy()) is None
    assert due_kind(CalendarSpec(), date(2026, 10, 30), TempoPolicy()) is TempoKind.MONTH_END


def test_a_disabled_policy_never_fires() -> None:
    assert due_kind(CalendarSpec(), FRIDAY, TempoPolicy(enabled=False)) is None


def test_an_impossible_warning_day_is_ignored() -> None:
    assert due_kind(CalendarSpec(), date(2026, 2, 28), TempoPolicy(warning_day=31)) is None


# ------------------------------------------------------------------ the tempo message


async def tempo(sessions, clock, notifier=None, **kwargs):
    return await send_tempo_reminder(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        notifier=notifier or RecordingNotifier(),
        voice=voice(),
        clock=clock,
        policy=TempoPolicy(url="https://tempo.example/my-work"),
        silent_policy=SILENT,
        **kwargs,
    )


async def test_everyone_is_tagged_not_just_those_in_the_office(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()

    outcome = await tempo(sessions, at(FRIDAY), notifier)

    assert outcome.sent
    for name in ("Аня", "Боря", "Вера", "Глеб"):
        assert name in notifier.last_text


async def test_the_tempo_link_is_included(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()
    await tempo(sessions, at(FRIDAY), notifier)
    assert "https://tempo.example/my-work" in notifier.last_text


async def test_it_stays_quiet_on_a_day_nothing_is_due(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()

    outcome = await tempo(sessions, at(MONDAY), notifier)

    assert outcome.skipped == "not-due"
    assert notifier.messages == []


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
        outcome = await send_tempo_reminder(
            office_id="ovest",
            offices=SqlOfficeStore(sessions),
            notifier=RecordingNotifier(),
            voice=voice(mood=mood),
            clock=at(FRIDAY),
            policy=TempoPolicy(),
            silent_policy=SILENT,
            dry_run=True,
        )
        assert outcome.text


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
    reply = await ask(sessions, model=StubChatModel(reply="а" * 5000))
    assert len(reply.text) <= 1600


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
