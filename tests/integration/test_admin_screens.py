"""The admin screens, built from explicit arguments rather than from callback data.

The bug these exist to prevent: a handler that redraws by calling a sibling handler,
which then re-parses `query.data`. Pressing a `adm:fixtog:…` button ran the toggle and
called `fixed_day`, whose parser expected four segments and got five — so it raised
*after* the write had landed. The checkbox was right the next time the screen was opened
and never on the tap itself, which reads as a UI that does not respond.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.telegram.routers.admin import (
    MAX_DESKS,
    _parse_count,
    desks_screen,
    fixed_day_screen,
    fixed_screen,
    office_screen,
    roster_screen,
)
from tabelshchik.application.voice import MoodPolicy, Voice
from tabelshchik.bootstrap.mapping import catalog
from tabelshchik.config.loader import parse_file
from tabelshchik.config.messages import MessagesConfig
from tabelshchik.domain.mood import Mood

from .conftest import seed

NOW = datetime(2026, 9, 11, 12, 0)
CATALOG = catalog(parse_file(Path("config/messages.yaml"), MessagesConfig))
MONDAY = 0

ADMIN_SOURCE = Path("src/tabelshchik/adapters/telegram/routers/admin.py").read_text("utf-8")


class Ctx:
    """Just enough of `BotContext` for a screen builder."""

    def __init__(self, sessions) -> None:
        from tabelshchik.adapters.db.repositories import SqlOfficeStore, SqlRosterStore

        self.offices = SqlOfficeStore(sessions)
        self.roster = SqlRosterStore(sessions)
        self.clock = FixedClock(NOW)
        self.voice = Voice(catalog=CATALOG, moods=MoodPolicy(weights={Mood.TOXIC: 1}))


@pytest.fixture
def ctx(sessions):
    seed(sessions)
    return Ctx(sessions)


def labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# ----------------------------------------------------------------- the fixed-day screen


def test_a_toggle_shows_up_on_the_very_next_render(ctx) -> None:
    """The reported symptom: the tick only appeared after leaving and re-entering."""
    before = labels(fixed_day_screen(ctx, "ovest", MONDAY)[1])
    assert "▫️ Аня" in before

    ctx.roster.toggle_fixed("ovest", MONDAY, "anya")

    after = labels(fixed_day_screen(ctx, "ovest", MONDAY)[1])
    assert "✅ Аня" in after
    assert "▫️ Аня" not in after


def test_toggling_back_clears_the_tick(ctx) -> None:
    ctx.roster.toggle_fixed("ovest", MONDAY, "anya")
    ctx.roster.toggle_fixed("ovest", MONDAY, "anya")
    assert "▫️ Аня" in labels(fixed_day_screen(ctx, "ovest", MONDAY)[1])


def test_the_heading_counts_what_is_ticked(ctx) -> None:
    """Telegram refuses to redraw a message whose text *and* markup are unchanged, so a
    screen whose only difference is a checkbox needs something in the text to move."""
    assert "отмечено 0" in fixed_day_screen(ctx, "ovest", MONDAY)[0]
    ctx.roster.toggle_fixed("ovest", MONDAY, "anya")
    assert "отмечено 1" in fixed_day_screen(ctx, "ovest", MONDAY)[0]


def test_a_toggle_button_carries_the_office_the_weekday_and_the_person(ctx) -> None:
    data = [
        item for item in callbacks(fixed_day_screen(ctx, "ovest", MONDAY)[1]) if "fixtog" in item
    ]
    assert "adm:fixtog:ovest:0:anya" in data
    assert all(
        len(item.encode()) <= 64 for item in callbacks(fixed_day_screen(ctx, "ovest", MONDAY)[1])
    )


def test_departed_people_are_not_offered(ctx) -> None:
    ctx.roster.remove_employee("borya", ended_on=date(2026, 9, 1))
    assert not any("Боря" in label for label in labels(fixed_day_screen(ctx, "ovest", MONDAY)[1]))


def test_the_fixed_overview_counts_each_day(ctx) -> None:
    ctx.roster.toggle_fixed("ovest", MONDAY, "anya")
    ctx.roster.toggle_fixed("ovest", MONDAY, "borya")
    assert "Пн: 2" in labels(fixed_screen(ctx, "ovest")[1])


# --------------------------------------------------------------------- the desks screen


def test_the_desks_screen_shows_the_current_numbers(ctx) -> None:
    assert "Пн: 2" in labels(desks_screen(ctx, "ovest")[1])


def test_a_set_count_is_reflected_immediately(ctx) -> None:
    ctx.roster.set_vacant_desks("ovest", MONDAY, 7)
    assert "Пн: 7" in labels(desks_screen(ctx, "ovest")[1])


def test_the_desks_screen_no_longer_promises_incrementing(ctx) -> None:
    """Eleven taps to get from 9 to 8, and no way at all to reach ten."""
    text = desks_screen(ctx, "ovest")[0]
    assert "увеличить" not in text
    assert "задать число" in text


@pytest.mark.parametrize("raw", ["0", "3", "12", str(MAX_DESKS), " 5 "])
def test_a_plain_number_is_accepted(raw: str) -> None:
    assert _parse_count(raw) == int(raw.strip())


@pytest.mark.parametrize("raw", ["", "-1", "3.5", "много", "1e3", str(MAX_DESKS + 1), "٣٣٣"])
def test_anything_else_is_refused(raw: str) -> None:
    assert _parse_count(raw) is None


# ------------------------------------------------------------------- the roster screen


def test_the_roster_lists_everyone_with_a_card_button(ctx) -> None:
    text, markup = roster_screen(ctx, "ovest")
    assert "4 чел." in text
    assert "adm:empv:anya" in callbacks(markup)


def test_a_departed_person_is_listed_and_marked(ctx) -> None:
    ctx.roster.remove_employee("borya", ended_on=date(2026, 9, 1))
    assert any("Боря · уволен" in label for label in labels(roster_screen(ctx, "ovest")[1]))


# -------------------------------------------------------------------- the office screen


def test_the_office_screen_reports_a_reseed_without_reparsing_callback_data(ctx) -> None:
    """`reseed` used to redraw by calling the `office` handler. It happened to work, only
    because both buttons carry an office id in the same position — which is luck, not
    design, and the next screen to be reused this way is the one that breaks."""
    before = office_screen(ctx, "ovest")
    assert before is not None

    ctx.roster.bump_seed("ovest")

    after = office_screen(ctx, "ovest")
    assert after is not None
    assert after[0] != before[0], "a reseed the screen does not show is indistinguishable from none"


def test_the_office_screen_separates_active_from_departed(ctx) -> None:
    first = office_screen(ctx, "ovest")
    assert first is not None and "Сотрудников: 4" in first[0]

    ctx.roster.remove_employee("borya", ended_on=date(2026, 9, 1))

    second = office_screen(ctx, "ovest")
    assert second is not None
    assert "Сотрудников: 3" in second[0]
    assert "+1 уволенных" in second[0]


def test_an_unknown_office_is_reported_rather_than_rendered(ctx) -> None:
    assert office_screen(ctx, "nope") is None


# ------------------------------------------------------- the rule, not just the symptom


def test_no_handler_redraws_by_calling_another_handler() -> None:
    """The structural rule. Handlers parse `query.data`; screens take arguments.

    A handler calling another handler works right up until the two are reached by
    different buttons, at which point the second one parses callback data meant for the
    first and fails — silently, because the write it follows has already succeeded.
    """
    handlers = set(re.findall(r"@router\.callback_query[^\n]*\)\s*\nasync def (\w+)", ADMIN_SOURCE))
    assert handlers, "no callback handlers found — has the decorator style changed?"

    bodies = re.split(r"\n(?=@router\.)", ADMIN_SOURCE)
    offenders = [
        f"{caller} -> {callee}"
        for body in bodies
        for caller in re.findall(r"\nasync def (\w+)", body)
        for callee in handlers
        if callee != caller and re.search(rf"await {callee}\(query", body)
    ]
    assert not offenders, f"redraw by calling a sibling handler: {offenders}"
