"""The daily reminder, end to end against a real database and a fake Telegram."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.repositories import (
    SqlLedgerStore,
    SqlOfficeStore,
    SqlScheduleStore,
)
from tabelshchik.adapters.fakes import RecordingNotifier, StubChatModel
from tabelshchik.application.policy import SchedulePolicy
from tabelshchik.application.regenerate_schedule import regenerate
from tabelshchik.application.send_attendance_reminder import (
    ReminderSkip,
    send_attendance_reminder,
)
from tabelshchik.application.voice import MoodPolicy, Voice
from tabelshchik.bootstrap.mapping import catalog, silent_policy
from tabelshchik.config.loader import parse_file
from tabelshchik.config.messages import MessagesConfig
from tabelshchik.config.models import AppConfig
from tabelshchik.domain.mood import Mood

from .conftest import office_seed, seed

MESSAGES = parse_file(Path("config/messages.yaml"), MessagesConfig)
APP = parse_file(Path("config/app.yaml"), AppConfig)
CATALOG = catalog(MESSAGES)
SILENT = silent_policy(APP)

MONDAY = date(2026, 9, 14)
TUESDAY = date(2026, 9, 15)
FRIDAY = date(2026, 9, 18)
SUNDAY = date(2026, 9, 20)
NEXT_MONDAY = date(2026, 9, 21)


def voice(model=None, mood_weights=None) -> Voice:
    return Voice(
        catalog=CATALOG,
        moods=MoodPolicy(weights=mood_weights or {Mood.TOXIC: 1}),
        model=model,
    )


def at(day: date, hour: int = 15, minute: int = 30) -> FixedClock:
    return FixedClock(datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute))


@pytest.fixture
def office(sessions):
    seed(
        sessions,
        office_seed(
            schedule={
                "vacantDesks": dict.fromkeys(
                    ["monday", "tuesday", "wednesday", "thursday", "friday"], 2
                )
            }
        ),
    )
    regenerate(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        ledger=SqlLedgerStore(sessions),
        clock=at(MONDAY, hour=8),
        policy=SchedulePolicy(horizon_weeks=3, freeze_weeks=0),
    )
    return sessions


async def remind(sessions, clock, notifier=None, model=None, **kwargs):
    return await send_attendance_reminder(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        notifier=notifier or RecordingNotifier(),
        voice=voice(model),
        clock=clock,
        silent_policy=SILENT,
        **kwargs,
    )


# ---------------------------------------------------------------------- targeting


async def test_monday_announces_tuesday(office) -> None:
    outcome = await remind(office, at(MONDAY))
    assert outcome.target == TUESDAY
    assert outcome.sent


async def test_friday_announces_monday(office) -> None:
    """Falls out of "next working day" — there is no Friday special case."""
    outcome = await remind(office, at(FRIDAY))
    assert outcome.target == NEXT_MONDAY


async def test_sunday_announces_monday(office) -> None:
    outcome = await remind(office, at(SUNDAY))
    assert outcome.target == NEXT_MONDAY


async def test_a_holiday_chain_is_stepped_over(sessions) -> None:
    seed(
        sessions,
        office_seed(
            schedule={"vacantDesks": {"monday": 2, "tuesday": 2, "thursday": 2}},
            calendar={"closed": ["2026-09-15", "2026-09-16"]},
        ),
    )
    regenerate(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        ledger=SqlLedgerStore(sessions),
        clock=at(MONDAY, hour=8),
        policy=SchedulePolicy(horizon_weeks=3, freeze_weeks=0),
    )

    outcome = await remind(sessions, at(MONDAY))

    # Tuesday and Wednesday are closed, so the reminder steps over both to Thursday.
    assert outcome.target == date(2026, 9, 17)
    assert outcome.sent


async def test_a_day_the_office_expects_nobody_is_passed_over_quietly(sessions) -> None:
    """An office that only fills desks on Fridays should not announce "nobody is coming"
    every Monday."""
    seed(sessions, office_seed(schedule={"vacantDesks": {"friday": 2}}))
    regenerate(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        ledger=SqlLedgerStore(sessions),
        clock=at(MONDAY, hour=8),
        policy=SchedulePolicy(horizon_weeks=3, freeze_weeks=0),
    )
    notifier = RecordingNotifier()

    outcome = await remind(sessions, at(MONDAY), notifier)

    assert outcome.skipped is ReminderSkip.NOT_SCHEDULED
    assert notifier.messages == []


# ------------------------------------------------------------------- the message


async def test_everyone_scheduled_is_tagged(office) -> None:
    notifier = RecordingNotifier()
    await remind(office, at(MONDAY), notifier)

    roster = SqlScheduleStore(office).day("ovest", TUESDAY).roster
    names = {e.id: e for e in SqlOfficeStore(office).employees("ovest")}
    text = notifier.last_text

    assert len(roster) == 2
    for employee_id in roster:
        assert names[employee_id].full_name in text


async def test_someone_with_a_username_is_tagged_by_it(sessions) -> None:
    """The fallback for people the bot has never spoken to."""
    seed(sessions, office_seed(schedule={"vacantDesks": {"tuesday": 4}}))
    regenerate(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        ledger=SqlLedgerStore(sessions),
        clock=at(MONDAY, hour=8),
        policy=SchedulePolicy(horizon_weeks=2, freeze_weeks=0),
    )
    notifier = RecordingNotifier()

    await remind(sessions, at(MONDAY), notifier)

    assert "(@anya_tg)" in notifier.last_text


async def test_a_linked_user_is_tagged_by_id_so_it_works_without_a_username(office) -> None:
    SqlOfficeStore(office).link_telegram_user("borya", 777)
    notifier = RecordingNotifier()

    await remind(office, at(MONDAY), notifier)

    assert "tg://user?id=777" in notifier.last_text


async def test_the_date_is_rendered_in_russian(office) -> None:
    notifier = RecordingNotifier()
    await remind(office, at(MONDAY), notifier)
    assert "Завтра, 15 сентября 2026 года" in notifier.last_text


async def test_friday_says_on_monday_rather_than_tomorrow(office) -> None:
    notifier = RecordingNotifier()
    await remind(office, at(FRIDAY), notifier)
    assert "В понедельник" in notifier.last_text


async def test_names_are_escaped(sessions) -> None:
    seed(
        sessions,
        office_seed(
            employees=[{"id": "tricky", "name": "A <b>& Co</b>"}],
            schedule={"vacantDesks": {"tuesday": 1}},
        ),
    )
    regenerate(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        ledger=SqlLedgerStore(sessions),
        clock=at(MONDAY, hour=8),
        policy=SchedulePolicy(horizon_weeks=2, freeze_weeks=0),
    )
    notifier = RecordingNotifier()

    await remind(sessions, at(MONDAY), notifier)
    assert "&lt;b&gt;" in notifier.last_text


# --------------------------------------------------------------------- silencing


async def test_an_evening_reminder_arrives_without_a_notification(office) -> None:
    outcome = await remind(office, at(MONDAY, hour=21))
    assert outcome.sent
    assert outcome.silent


async def test_a_working_afternoon_reminder_makes_a_noise(office) -> None:
    assert not (await remind(office, at(MONDAY, hour=15))).silent


async def test_a_saturday_reminder_is_always_silent(office) -> None:
    # Saturday is configured all-day silent; nothing is scheduled so this exercises the
    # policy rather than the send.
    from tabelshchik.application.policy import SilentPolicy

    policy: SilentPolicy = SILENT
    assert policy.is_silent(5, at(MONDAY, hour=12).time_of_day())


async def test_sunday_before_ten_is_silent_and_after_is_not(office) -> None:
    assert (await remind(office, at(SUNDAY, hour=9))).silent
    assert not (await remind(office, at(SUNDAY, hour=11))).silent


# ------------------------------------------------------------- announcing and freezing


async def test_announcing_freezes_the_day(office) -> None:
    await remind(office, at(MONDAY))
    snapshot = SqlScheduleStore(office).day("ovest", TUESDAY)
    assert snapshot.is_announced
    assert snapshot.roster_fingerprint


async def test_a_repeat_run_stays_quiet(office) -> None:
    """Belt and braces alongside the job ledger: even a direct second call is silent."""
    notifier = RecordingNotifier()
    await remind(office, at(MONDAY), notifier)
    outcome = await remind(office, at(MONDAY), notifier)

    assert outcome.skipped is ReminderSkip.ALREADY_ANNOUNCED
    assert len(notifier.messages) == 1


async def test_a_changed_roster_sends_a_correction(office) -> None:
    """Staying quiet would leave people holding a list that is now wrong."""
    from tabelshchik.domain.entities import AssignmentStatus

    notifier = RecordingNotifier()
    await remind(office, at(MONDAY), notifier)

    who = SqlScheduleStore(office).day("ovest", TUESDAY).roster[0]
    SqlScheduleStore(office).set_assignment_status(
        "ovest", TUESDAY, who, AssignmentStatus.CANCELLED, reason="отпуск"
    )

    outcome = await remind(office, at(MONDAY), notifier)

    assert outcome.correction
    assert len(notifier.messages) == 2
    assert "Уточнение" in notifier.last_text


async def test_a_failed_send_does_not_mark_the_day_announced(office) -> None:
    """Otherwise a roster nobody has seen would be frozen against regeneration."""
    outcome = await remind(office, at(MONDAY), RecordingNotifier(fail=True))

    assert outcome.skipped is ReminderSkip.SEND_FAILED
    assert not SqlScheduleStore(office).day("ovest", TUESDAY).is_announced


async def test_an_office_with_no_chat_is_reported_not_crashed(sessions) -> None:
    seed(sessions, office_seed(chatId=None, schedule={"vacantDesks": {"tuesday": 1}}))
    regenerate(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        ledger=SqlLedgerStore(sessions),
        clock=at(MONDAY, hour=8),
        policy=SchedulePolicy(horizon_weeks=2, freeze_weeks=0),
    )

    assert (await remind(sessions, at(MONDAY))).skipped is ReminderSkip.NO_CHAT


async def test_a_dry_run_renders_without_sending_or_freezing(office) -> None:
    notifier = RecordingNotifier()
    outcome = await remind(office, at(MONDAY), notifier, dry_run=True)

    assert outcome.text
    assert not outcome.sent
    assert notifier.messages == []
    assert not SqlScheduleStore(office).day("ovest", TUESDAY).is_announced


# --------------------------------------------------------------------------- voice


def decoration(tail: str, epithets: list[str]) -> str:
    return json.dumps({"tail": tail, "epithets": epithets}, ensure_ascii=False)


async def test_the_ai_flavoured_intro_is_used_when_it_is_valid(office) -> None:
    model = StubChatModel(reply=decoration("кофе стынет", ["Первый", "Второй"]))
    notifier = RecordingNotifier()

    await remind(office, at(MONDAY), notifier, model=model)

    assert "Кофе стынет." in notifier.last_text
    assert "Первый" in notifier.last_text
    assert "Второй" in notifier.last_text
    assert model.json_requested == [True]


async def test_the_date_is_never_said_twice(office) -> None:
    """The reason the weekday and the date are separate tokens.

    A model that opens with its own lead-in used to produce "В понедельник В понедельник,
    14 сентября". It can no longer put a weekday anywhere: it is not given one, and a tail
    containing one is thrown away.
    """
    model = StubChatModel(reply=decoration("в понедельник снова весело", ["Первый", "Второй"]))
    notifier = RecordingNotifier()

    await remind(office, at(FRIDAY), notifier, model=model)

    assert notifier.last_text.count("понедельник") == 1
    assert "снова весело" not in notifier.last_text


async def test_an_invented_colleague_falls_back_to_the_written_line(office) -> None:
    model = StubChatModel(reply=decoration("особенно ждём Аню", ["Первый", "Второй"]))
    notifier = RecordingNotifier()

    await remind(office, at(MONDAY), notifier, model=model)

    assert "особенно ждём" not in notifier.last_text
    # The roster still renders — only the model's line was discarded.
    assert SqlScheduleStore(office).day("ovest", TUESDAY).roster
    assert notifier.last_text.count("\n") >= 2


async def test_everyone_is_named_even_when_the_model_says_nothing(office) -> None:
    """The guarantee. Decoration is optional; being reminded is not."""
    notifier = RecordingNotifier()
    await remind(office, at(MONDAY), notifier, model=StubChatModel(reply=None))

    roster = SqlScheduleStore(office).day("ovest", TUESDAY).roster
    names = {e.id: e.full_name for e in SqlOfficeStore(office).employees("ovest")}
    assert roster
    for employee_id in roster:
        assert names[employee_id] in notifier.last_text
    # And each line carries a title, not a bare name.
    assert notifier.last_text.count("\n") >= len(roster) + 1


async def test_the_model_never_learns_the_date_the_office_or_the_roster(office) -> None:
    model = StubChatModel(reply=decoration("кофе стынет", ["Первый", "Второй"]))
    await remind(office, at(MONDAY), notifier=RecordingNotifier(), model=model)

    everything = " ".join(part for prompt in model.prompts for part in prompt)
    assert "сентября" not in everything
    assert "O'Vest" not in everything
    assert "Аня" not in everything
    assert "вторник" not in everything.lower()


async def test_every_mood_produces_a_sendable_message(office) -> None:
    for mood in Mood:
        notifier = RecordingNotifier()
        await send_attendance_reminder(
            office_id="ovest",
            offices=SqlOfficeStore(office),
            schedule=SqlScheduleStore(office),
            notifier=notifier,
            voice=Voice(catalog=CATALOG, moods=MoodPolicy(weights={mood: 1})),
            clock=at(MONDAY),
            silent_policy=SILENT,
            dry_run=True,
        )


# ------------------------------------------------------------------- shared chats


async def test_two_offices_in_one_chat_each_name_themselves(sessions) -> None:
    """Allowed on purpose, but the whole point of per-office chats was that people stop
    reading each other's reminders — so a shared chat has to say which office it means.
    """
    from .conftest import office_seed

    seed(
        sessions,
        office_seed(schedule={"vacantDesks": {"tuesday": 2}}),
        office_seed(
            id="pine",
            name="Pine Office Park",
            chatId=-100123,  # the same chat
            employees=[{"id": "petya", "name": "Петя"}, {"id": "katya", "name": "Катя"}],
            schedule={"vacantDesks": {"tuesday": 1}},
        ),
    )
    for office_id in ("ovest", "pine"):
        regenerate(
            office_id=office_id,
            offices=SqlOfficeStore(sessions),
            schedule=SqlScheduleStore(sessions),
            ledger=SqlLedgerStore(sessions),
            clock=at(MONDAY, hour=8),
            policy=SchedulePolicy(horizon_weeks=2, freeze_weeks=0),
        )

    notifier = RecordingNotifier()
    await remind(sessions, at(MONDAY), notifier)

    assert "O&#x27;Vest" in notifier.last_text
    assert notifier.last_text.startswith("🏢")


async def test_an_office_with_its_own_chat_gets_no_header(office) -> None:
    """The header is only useful where it disambiguates; everywhere else it is clutter."""
    notifier = RecordingNotifier()
    await remind(office, at(MONDAY), notifier)
    assert not notifier.last_text.startswith("🏢")
