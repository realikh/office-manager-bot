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
from typing import Any

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
from tabelshchik.application.ids import MAX_ID_LENGTH, MAX_OFFICE_ID_LENGTH
from tabelshchik.application.voice import MoodPolicy, Voice
from tabelshchik.bootstrap.mapping import catalog
from tabelshchik.config.loader import parse_file
from tabelshchik.config.messages import MessagesConfig
from tabelshchik.domain.mood import Mood

from .conftest import office_seed, seed

NOW = datetime(2026, 9, 11, 12, 0)
CATALOG = catalog(parse_file(Path("config/messages.yaml"), MessagesConfig))
MONDAY = 0

ADMIN_SOURCE = Path("src/tabelshchik/adapters/telegram/routers/admin.py").read_text("utf-8")

#: Read out of the source rather than listed here. A hard-coded list silently exempts
#: every flow added after it was written, which is exactly the class of flow these tests
#: exist to catch.
STATE_GROUPS = frozenset(re.findall(r"class (\w+)\(StatesGroup\)", ADMIN_SOURCE))


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


def test_a_toggle_button_carries_the_weekday_and_the_person_but_not_the_office(ctx) -> None:
    """The office is derived from the person.

    Carrying both spent `14 + len(office) + len(employee)` of a 64-byte budget, so a
    long office name plus a long employee id silently produced a dead button. Nothing
    about the office may come back into this string.
    """
    data = [
        item for item in callbacks(fixed_day_screen(ctx, "ovest", MONDAY)[1]) if "fixtog" in item
    ]
    assert "adm:fixtog:0:anya" in data
    assert not any("ovest" in item for item in data)


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


def test_no_free_text_step_accepts_a_command() -> None:
    """Typing /cancel during a rename renamed the employee to "/cancel".

    `apply_rename` was registered before the cancel handler, and a state handler matches
    *any* message in its state, so it won and wrote the command as data. Registration
    order now puts cancel first, but order alone is one reordering away from breaking:
    every step that reads free text must refuse a command itself.
    """
    groups = "|".join(sorted(STATE_GROUPS))
    bodies = re.split(r"\n(?=@router\.)", ADMIN_SOURCE)
    offenders = [
        body.split("async def ")[1].split("(")[0]
        for body in bodies
        if re.search(rf"@router\.message\((?:{groups})\.", body)
        and "_refused_a_command(message)" not in body
    ]
    assert not offenders, f"state handlers that would swallow a command: {offenders}"


def test_the_cancel_handler_is_registered_before_any_state_handler() -> None:
    """Belt to the guard's braces: aiogram dispatches in registration order."""
    cancel = ADMIN_SOURCE.index("async def cancel_admin_flow")
    first_state = min(
        ADMIN_SOURCE.index(f"@router.message({group}.")
        for group in STATE_GROUPS
        if f"@router.message({group}." in ADMIN_SOURCE
    )
    assert cancel < first_state


def test_every_free_text_prompt_offers_a_cancel_button() -> None:
    """Because people reach for a button, and typing the command is what went wrong."""
    prompts = re.findall(r"await query\.message\.answer\(\s*\n?(.*?)\n\s*\)", ADMIN_SOURCE, re.S)
    asking = [text for text in prompts if "Отправьте" in text or "Имя и фамилия" in text]
    assert asking, "no free-text prompts found — has the wording changed?"
    assert all("cancel_keyboard()" in text for text in asking)


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


# ------------------------------------------------------------------- callback budget


def test_no_callback_prefix_shadows_another() -> None:
    """Dispatch is `startswith` in registration order, so a prefix of a prefix wins.

    `adm:emp:` and `adm:empadd:` only coexist because both filters carry the trailing
    colon; drop it from either and every `adm:empadd:…` tap runs the roster handler
    instead. The rule was folklore until this test.
    """
    literals = set(re.findall(r'F\.data(?:\s*==\s*|\.startswith\()"([^"]+)"', ADMIN_SOURCE))
    assert literals, "no callback literals found — has the filter style changed?"

    shadowed = [
        (outer, inner)
        for outer in literals
        for inner in literals
        if outer != inner and outer.startswith(inner)
    ]
    assert not shadowed, f"callback prefixes that swallow each other: {shadowed}"


def test_every_callback_a_screen_emits_fits_telegram_s_limit(sessions) -> None:
    """64 bytes, total. Over it the button is simply dead, with no error anywhere.

    Worst case, not the ids that happen to be seeded: an office named by whoever created
    it and a person with a long name are both ordinary, and together they are what broke
    `adm:fixtog:`.
    """
    long_office = "o" * MAX_OFFICE_ID_LENGTH
    long_employee = "e" * MAX_ID_LENGTH
    seed(
        sessions,
        office_seed(
            id=long_office,
            name="Длинный",
            employees=[{"id": long_employee, "name": "Длинное Имя"}],
            schedule={"vacantDesks": {"monday": 1}},
        ),
    )
    # `Ctx` is deliberately partial, exactly as the fixture's callers get it.
    ctx: Any = Ctx(sessions)

    screens = [
        office_screen(ctx, long_office),
        desks_screen(ctx, long_office),
        fixed_screen(ctx, long_office),
        fixed_day_screen(ctx, long_office, MONDAY),
        roster_screen(ctx, long_office),
    ]
    for screen in screens:
        assert screen is not None
        for data in callbacks(screen[1]):
            assert len(data.encode()) <= 64, f"{data} is {len(data.encode())} bytes"
