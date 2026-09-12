"""The office settings screens.

Every one of these is a pure function of its arguments, which is what lets them be
checked without a Telegram connection — and what stops a handler redrawing by calling a
sibling, the bug the whole screens section exists to prevent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.engine import session_scope
from tabelshchik.adapters.db.repositories import (
    SqlAdminStore,
    SqlOfficeAdminStore,
    SqlOfficeStore,
    SqlRosterStore,
)
from tabelshchik.adapters.db.seed import seed_admins
from tabelshchik.adapters.telegram.routers.admin import (
    calendar_screen,
    chat_screen,
    office_settings_screen,
    offices_screen,
)
from tabelshchik.application.ports import AdminRole

from .conftest import office_seed, seed

NOW = datetime(2026, 9, 11, 12, 0)
OWNER = 1
ADMIN = 2


class Ctx:
    def __init__(self, sessions) -> None:
        self.offices = SqlOfficeStore(sessions)
        self.roster = SqlRosterStore(sessions)
        self.office_admin = SqlOfficeAdminStore(sessions)
        self.admins = SqlAdminStore(sessions)
        self.clock = FixedClock(NOW)

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and self.admins.role_of(user_id) is AdminRole.OWNER


@pytest.fixture
def ctx(sessions) -> Any:
    seed(sessions)
    with session_scope(sessions) as session:
        seed_admins(session, frozenset({OWNER}), now=NOW)
    SqlAdminStore(sessions).grant(ADMIN, at=NOW)
    return Ctx(sessions)


def labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# ------------------------------------------------------------------------- the listing


def test_a_closed_office_is_still_listed_so_it_can_be_reopened(ctx) -> None:
    """`active_offices()` is what the bot schedules; the admin list is not that."""
    ctx.office_admin.set_active("ovest", False)
    text, markup = offices_screen(ctx, owner=True)

    assert "adm:office:ovest" in callbacks(markup)
    assert any(label.startswith("💤") for label in labels(markup))
    assert "закрытых 1" in text


def test_only_the_owner_is_offered_the_create_button(ctx) -> None:
    assert "adm:onew" in callbacks(offices_screen(ctx, owner=True)[1])
    assert "adm:onew" not in callbacks(offices_screen(ctx, owner=False)[1])


def test_an_empty_installation_says_what_to_do(sessions) -> None:
    """A fresh database has no offices at all now — the headline behaviour change."""
    ctx: Any = Ctx(sessions)
    text, markup = offices_screen(ctx, owner=True)
    assert "Офисов пока нет" in text
    assert "adm:onew" in callbacks(markup)


def test_a_non_owner_is_told_who_can_create_one(sessions) -> None:
    ctx: Any = Ctx(sessions)
    assert "только владелец" in offices_screen(ctx, owner=False)[0]


# ------------------------------------------------------------------------ the settings


def test_the_status_is_in_the_text_not_only_in_the_buttons(ctx) -> None:
    """Telegram refuses to redraw a message whose text and markup are both unchanged, and
    closing an office changes only which button the keyboard offers."""
    screen = office_settings_screen(ctx, "ovest", owner=True)
    assert screen is not None and "активен" in screen[0]

    ctx.office_admin.set_active("ovest", False)
    screen = office_settings_screen(ctx, "ovest", owner=True)
    assert screen is not None and "закрыт" in screen[0]


def test_a_closed_office_offers_reopening_instead_of_closing(ctx) -> None:
    ctx.office_admin.set_active("ovest", False)
    screen = office_settings_screen(ctx, "ovest", owner=True)
    assert screen is not None
    assert "adm:oopen:ovest" in callbacks(screen[1])
    assert "adm:oclose:ovest" not in callbacks(screen[1])


def test_an_ordinary_admin_sees_no_way_to_close_or_delete(ctx) -> None:
    screen = office_settings_screen(ctx, "ovest", owner=False)
    assert screen is not None
    assert "adm:oclose:ovest" not in callbacks(screen[1])
    assert "adm:odrop:ovest" not in callbacks(screen[1])


def test_an_unknown_office_has_no_settings_screen(ctx) -> None:
    assert office_settings_screen(ctx, "nope", owner=True) is None


# ----------------------------------------------------------------------------- the chat


def test_the_chat_screen_points_at_bind(ctx) -> None:
    """A group id is not shown anywhere in Telegram and cannot be typed from memory."""
    screen = chat_screen(ctx, "ovest")
    assert screen is not None and "/bind" in screen[0]


def test_an_unbound_office_is_not_offered_an_unbind_button(ctx) -> None:
    ctx.roster.set_chat_id("ovest", None)
    screen = chat_screen(ctx, "ovest")
    assert screen is not None
    assert "adm:ochatno:ovest" not in callbacks(screen[1])


def test_a_shared_chat_is_named_rather_than_refused(ctx, sessions) -> None:
    """Two offices sharing a group is a supported setup; people just wonder why the
    messages carry an office header."""
    seed(sessions, office_seed(id="pine", name="Pine", chatId=-100123, employees=[]))
    screen = chat_screen(ctx, "ovest")
    assert screen is not None and "Pine" in screen[0]


# ------------------------------------------------------------------------- the calendar


def test_the_current_calendar_is_ticked_and_named(ctx) -> None:
    screen = calendar_screen(ctx, "ovest")
    assert screen is not None
    assert "KZ" in screen[0]
    assert any(label.startswith("✅") and "KZ" in label for label in labels(screen[1]))


def test_the_calendar_is_a_fixed_list_not_free_text(ctx) -> None:
    """An unknown country code makes the holiday library return nothing at all, so a typo
    would be a silent "no public holidays, ever"."""
    screen = calendar_screen(ctx, "ovest")
    assert screen is not None
    picks = [item for item in callbacks(screen[1]) if item.startswith("adm:ocalpick:")]
    assert len(picks) >= 4
    assert all(len(item.encode()) <= 64 for item in picks)
