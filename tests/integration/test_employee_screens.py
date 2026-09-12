"""The employee self-service screens.

This router never adopted the edit-in-place rule the admin one has: every `me:` tap sent
a new message, so deleting three absences produced six, each leaving a live keyboard
behind pointing at ids that no longer existed. These pin the rule and the screens.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.repositories import (
    SqlAbsenceStore,
    SqlOfficeStore,
    SqlScheduleStore,
)
from tabelshchik.adapters.telegram.routers.employee import absences_screen
from tabelshchik.application.voice import MoodPolicy, Voice
from tabelshchik.bootstrap.mapping import catalog
from tabelshchik.config.loader import parse_file
from tabelshchik.config.messages import MessagesConfig
from tabelshchik.domain.entities import AbsenceKind
from tabelshchik.domain.mood import Mood

from .conftest import seed

NOW = datetime(2026, 9, 11, 12, 0)
CATALOG = catalog(parse_file(Path("config/messages.yaml"), MessagesConfig))
ANYA = 777

EMPLOYEE_SOURCE = Path("src/tabelshchik/adapters/telegram/routers/employee.py").read_text("utf-8")


class Ctx:
    def __init__(self, sessions) -> None:
        self.offices = SqlOfficeStore(sessions)
        self.absences = SqlAbsenceStore(sessions)
        self.schedule = SqlScheduleStore(sessions)
        self.clock = FixedClock(NOW)
        self.voice = Voice(catalog=CATALOG, moods=MoodPolicy(weights={Mood.TOXIC: 1}))


@pytest.fixture
def ctx(sessions) -> Any:
    seed(sessions)
    SqlOfficeStore(sessions).link_telegram_user("anya", ANYA)
    return Ctx(sessions)


def callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# ------------------------------------------------------------------------ the screens


def test_the_heading_counts_the_absences(ctx) -> None:
    """Telegram refuses to redraw a message whose text and markup are both unchanged, and
    deleting the last absence changes only the keyboard."""
    assert "Отпусков не запланировано" in absences_screen(ctx, ANYA)[0]

    ctx.absences.add(
        "anya", date(2026, 10, 1), date(2026, 10, 9), kind=AbsenceKind.VACATION, at=NOW
    )
    text, markup = absences_screen(ctx, ANYA)
    assert "— 1" in text
    assert any(item.startswith("me:absdel:") for item in callbacks(markup))


def test_somebody_the_bot_does_not_know_is_told_so(ctx) -> None:
    text, markup = absences_screen(ctx, 404)
    assert "/start" in text
    assert markup is None


# ------------------------------------------------------- the rule, not just the symptom


def test_no_callback_handler_hands_out_a_second_keyboard() -> None:
    """The same rule the admin router follows, in the half that never did.

    `redraw` and `rehome` take their markup positionally, so a `reply_markup=` keyword in
    a callback body is a keyboard being handed out directly — a second live screen, whose
    buttons keep acting on state that has moved on.
    """
    bodies = re.split(r"\n(?=@router\.)", EMPLOYEE_SOURCE)
    offenders = [
        body.split("async def ")[1].split("(")[0]
        for body in bodies
        if body.startswith("@router.callback_query") and "reply_markup=" in body
    ]
    assert not offenders, f"callback handlers sending a second keyboard: {offenders}"


def test_every_prompt_offers_a_cancel_button() -> None:
    """The prompt used to say "Отмена — /cancel" and offer nothing to press."""
    assert "_cancel_keyboard()" in EMPLOYEE_SOURCE
    assert "Отмена — /cancel" not in EMPLOYEE_SOURCE


def test_the_cancel_command_is_filtered_by_state() -> None:
    """An unfiltered /cancel in one router answers for flows belonging to another — which
    is how cancelling a half-finished admin wizard once replied with the employee menu."""
    body = EMPLOYEE_SOURCE.split("async def cancel(")[0].rsplit("@router.message(", 1)[1]
    assert "StateFilter" in body
