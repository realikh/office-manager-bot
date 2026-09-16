"""Congratulating the office on a public holiday.

What breaks in production without these: a greeting on the Monday a Sunday holiday was
moved to, three greetings for one Nauryz, two for one shared group, or a sarcastic one on
Victory Day because the bot happened to be in a toxic mood.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.repositories import (
    SqlOfficeAdminStore,
    SqlOfficeStore,
    SqlPostStore,
)
from tabelshchik.adapters.fakes import RecordingNotifier, StubChatModel
from tabelshchik.adapters.holiday_calendar import PackageHolidayCalendar
from tabelshchik.application.ports import Celebration
from tabelshchik.application.send_holiday_greeting import send_holiday_greeting
from tabelshchik.application.voice import MoodPolicy, Voice
from tabelshchik.bootstrap.mapping import catalog, silent_policy
from tabelshchik.config.loader import parse_file
from tabelshchik.config.messages import MessagesConfig
from tabelshchik.config.models import AppConfig
from tabelshchik.domain.mood import Mood

from .conftest import office_seed, seed

CATALOG = catalog(parse_file(Path("config/messages.yaml"), MessagesConfig))
SILENT = silent_policy(parse_file(Path("config/app.yaml"), AppConfig))

NAURYZ = [date(2026, 3, 21), date(2026, 3, 22), date(2026, 3, 23)]
WOMENS_DAY = date(2026, 3, 8)
CHRISTMAS = date(2026, 1, 7)
ORDINARY = date(2026, 9, 16)


class FakeCalendar:
    def __init__(self, table: dict[date, list[tuple[str, str]]]) -> None:
        self.table = table
        self.asked: list[date] = []

    def celebrations(self, country: str, day: date) -> Sequence[Celebration]:
        self.asked.append(day)
        return [
            Celebration(day=day, name=name, local_name=local)
            for name, local in self.table.get(day, [])
        ]

    def next_celebration(self, country: str, after: date) -> Celebration | None:
        return None

    def source(self) -> str:
        return "fake"


CALENDAR = FakeCalendar(
    {
        **{day: [("Nowruz Holiday", "Наурыз мейрамы")] for day in NAURYZ},
        WOMENS_DAY: [("International Women's Day", "Халықаралық әйелдер күні")],
        CHRISTMAS: [("Orthodox Christmas", "Православиелік Рождество")],
    }
)


def at(day: date, hour: int = 10) -> FixedClock:
    return FixedClock(datetime.combine(day, datetime.min.time()).replace(hour=hour))


async def greet(sessions, day: date, notifier=None, *, model=None, office_id="ovest", **kwargs):
    return await send_holiday_greeting(
        office_id=office_id,
        offices=SqlOfficeStore(sessions),
        notifier=notifier or RecordingNotifier(),
        voice=Voice(catalog=CATALOG, moods=MoodPolicy(weights={Mood.TOXIC: 1}), model=model),
        clock=at(day),
        calendar=kwargs.pop("calendar", CALENDAR),
        posts=SqlPostStore(sessions),
        silent_policy=SILENT,
        **kwargs,
    )


def generated(text: str) -> StubChatModel:
    return StubChatModel(reply=json.dumps({"text": text}, ensure_ascii=False))


# ----------------------------------------------------------------------------- when


async def test_a_holiday_is_greeted(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()

    outcome = await greet(sessions, NAURYZ[0], notifier)

    assert outcome.sent
    assert outcome.holidays == ("Nowruz Holiday",)
    assert "«Наурыз мейрамы»" in notifier.last_text


async def test_an_ordinary_day_is_not(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()

    outcome = await greet(sessions, ORDINARY, notifier)

    assert outcome.skipped == "not-a-holiday"
    assert notifier.messages == []


@pytest.mark.parametrize("day", NAURYZ[1:])
async def test_a_holiday_that_lasts_several_days_is_greeted_once(sessions, day) -> None:
    seed(sessions)
    outcome = await greet(sessions, day)
    assert outcome.skipped == "not-a-holiday"


async def test_the_calendar_is_asked_on_the_day_not_worked_out_in_advance(sessions) -> None:
    """A calendar that changed since boot is the one that has to answer."""
    seed(sessions)
    calendar = FakeCalendar({})
    await greet(sessions, ORDINARY, calendar=calendar)

    calendar.table[ORDINARY] = [("Surprise Day", "Тосын күн")]
    outcome = await greet(sessions, ORDINARY, calendar=calendar)

    assert outcome.sent
    assert ORDINARY in calendar.asked


async def test_a_holiday_is_greeted_once_a_day(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()

    await greet(sessions, WOMENS_DAY, notifier)
    again = await greet(sessions, WOMENS_DAY, notifier)

    assert again.skipped == "already-sent"
    assert len(notifier.messages) == 1


async def test_two_offices_in_one_group_greet_it_once(sessions) -> None:
    seed(
        sessions,
        office_seed(),
        office_seed(id="second", name="Second", employees=[{"id": "zed", "name": "Зед"}]),
    )
    notifier = RecordingNotifier()

    first = await greet(sessions, WOMENS_DAY, notifier)
    second = await greet(sessions, WOMENS_DAY, notifier, office_id="second")

    assert first.sent
    assert second.skipped == "chat-already-greeted"
    assert len(notifier.messages) == 1


async def test_a_closed_office_greets_nobody(sessions) -> None:
    seed(sessions)
    SqlOfficeAdminStore(sessions).set_active("ovest", False)

    outcome = await greet(sessions, WOMENS_DAY)

    assert outcome.skipped == "inactive"


# ----------------------------------------------------------------------------- what


async def test_the_model_writes_the_greeting_and_may_name_the_date(sessions) -> None:
    """«С 8 Марта» is the whole point of the day; the number guard must let it through."""
    seed(sessions)
    line = "С 8 Марта! Пусть сегодня всё будет легко."

    outcome = await greet(sessions, WOMENS_DAY, model=generated(line))

    assert outcome.generated
    assert outcome.text == line


async def test_a_greeting_that_invents_a_date_falls_back(sessions) -> None:
    seed(sessions)

    outcome = await greet(
        sessions, WOMENS_DAY, model=generated("С праздником! До 9 марта отдыхаем.")
    )

    assert not outcome.generated
    assert "Халықаралық әйелдер күні" in outcome.text


async def test_a_failed_model_still_greets(sessions) -> None:
    seed(sessions)
    outcome = await greet(sessions, WOMENS_DAY, model=StubChatModel(reply=None))
    assert outcome.sent and not outcome.generated


async def test_a_religious_holiday_is_never_joked_about(sessions) -> None:
    seed(sessions)
    model = StubChatModel(reply=None)

    await greet(sessions, CHRISTMAS, model=model)

    assert "никакой иронии" in model.prompts[0][1]


async def test_a_secular_one_may_be(sessions) -> None:
    seed(sessions)
    model = StubChatModel(reply=None)

    await greet(sessions, NAURYZ[0], model=model)

    brief = model.prompts[0][1]
    assert "никакой иронии" not in brief
    assert "Nowruz Holiday" in brief


async def test_nobody_is_tagged_on_a_day_off(sessions) -> None:
    seed(sessions)
    notifier = RecordingNotifier()

    await greet(sessions, WOMENS_DAY, notifier)

    assert "tg://" not in notifier.last_text
    assert "Аня" not in notifier.last_text
    assert notifier.pin_calls == []


@pytest.mark.parametrize("mood", list(Mood))
def test_every_mood_has_a_greeting_that_names_the_holiday(mood) -> None:
    for line in CATALOG.pool("holiday.greeting", mood):
        assert "{holiday}" in line


# ------------------------------------------------------------ the real calendar data


def test_a_transferred_day_off_is_not_a_celebration() -> None:
    """3 January 2025 was a working day moved by decree. Nothing to congratulate."""
    assert PackageHolidayCalendar().celebrations("KZ", date(2025, 1, 3)) == []


def test_the_weekday_a_weekend_holiday_moved_onto_is_not_one_either() -> None:
    # 8 March 2026 is a Sunday; the day off is Monday the 9th.
    calendar = PackageHolidayCalendar()
    assert calendar.celebrations("KZ", date(2026, 3, 9)) == []
    assert [item.name for item in calendar.celebrations("KZ", WOMENS_DAY)] == [
        "International Women's Day"
    ]


def test_the_calendar_gives_an_english_and_a_local_name() -> None:
    [new_year] = PackageHolidayCalendar().celebrations("KZ", date(2026, 1, 1))
    assert new_year.name == "New Year's Day"
    assert new_year.local_name == "Жаңа жыл"


def test_an_unknown_country_has_no_holidays_rather_than_an_error() -> None:
    assert PackageHolidayCalendar().celebrations("XX", date(2026, 1, 1)) == []


def test_the_next_holiday_is_found_across_the_new_year() -> None:
    found = PackageHolidayCalendar().next_celebration("KZ", date(2026, 12, 17))
    assert found is not None
    assert found.day == date(2027, 1, 1)
