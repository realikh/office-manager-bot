"""The «Администраторы» screens.

Built from explicit arguments, like every other screen here, so they can be checked
without a Telegram connection. The rules they encode — the owner's card offers nothing,
an ordinary admin sees no add button — are a convenience; `manage_admins` is what
actually refuses. These tests are about what people see.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.engine import session_scope
from tabelshchik.adapters.db.repositories import SqlAdminStore, SqlOfficeStore, SqlRosterStore
from tabelshchik.adapters.db.seed import seed_admins
from tabelshchik.adapters.telegram.routers.admin import (
    admin_card_screen,
    admins_screen,
    grant_screen,
)
from tabelshchik.application.ports import AdminRole

from .conftest import seed

NOW = datetime(2026, 9, 11, 12, 0)
OWNER = 1
SECOND = 2
ANYA = 777


class Ctx:
    """Just enough of `BotContext` for these screens."""

    def __init__(self, sessions) -> None:
        self.offices = SqlOfficeStore(sessions)
        self.roster = SqlRosterStore(sessions)
        self.admins = SqlAdminStore(sessions)
        self.clock = FixedClock(NOW)

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and self.admins.role_of(user_id) is AdminRole.OWNER


@pytest.fixture
def ctx(sessions) -> Any:
    seed(sessions)
    with session_scope(sessions) as session:
        seed_admins(session, frozenset({OWNER}), now=NOW)
    return Ctx(sessions)


def labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# ------------------------------------------------------------------------- the listing


def test_the_owner_is_marked_and_comes_first(ctx) -> None:
    ctx.admins.grant(SECOND, label="Дина", at=NOW)
    shown = labels(admins_screen(ctx, viewer_id=OWNER)[1])
    assert shown[0].startswith("👑")
    assert shown[1].startswith("🛡")


def test_the_heading_counts_the_admins(ctx) -> None:
    """Telegram refuses to redraw a message whose text and markup are both unchanged, and
    revoking the last admin in the list changes only the keyboard."""
    assert "— 1" in admins_screen(ctx, viewer_id=OWNER)[0]
    ctx.admins.grant(SECOND, at=NOW)
    assert "— 2" in admins_screen(ctx, viewer_id=OWNER)[0]


def test_only_the_owner_is_offered_the_add_button(ctx) -> None:
    ctx.admins.grant(SECOND, at=NOW)
    assert "adm:admadd" in callbacks(admins_screen(ctx, viewer_id=OWNER)[1])
    assert "adm:admadd" not in callbacks(admins_screen(ctx, viewer_id=SECOND)[1])


def test_you_are_marked_in_the_list(ctx) -> None:
    assert any("вы" in label for label in labels(admins_screen(ctx, viewer_id=OWNER)[1]))


# ---------------------------------------------------------------------------- the card


def test_the_owners_card_offers_nothing(ctx) -> None:
    """Ownership is transferred away, never dropped. A «Разжаловать» button on the
    owner's own card would be a promise the use case refuses to keep."""
    card = admin_card_screen(ctx, OWNER, viewer_id=OWNER)
    assert card is not None
    assert callbacks(card[1]) == ["adm:admins"]


def test_the_owner_may_transfer_or_revoke_somebody_else(ctx) -> None:
    ctx.admins.grant(SECOND, at=NOW)
    card = admin_card_screen(ctx, SECOND, viewer_id=OWNER)
    assert card is not None
    assert f"adm:admown:{SECOND}" in callbacks(card[1])
    assert f"adm:admdel:{SECOND}" in callbacks(card[1])


def test_an_admin_may_remove_themselves_but_not_others(ctx) -> None:
    """The other half of handing the bot over: after a transfer the ex-owner is an
    ordinary admin, and must still be able to finish leaving."""
    ctx.admins.grant(SECOND, at=NOW)
    ctx.admins.grant(3, at=NOW)

    own = admin_card_screen(ctx, SECOND, viewer_id=SECOND)
    assert own is not None
    assert f"adm:admdel:{SECOND}" in callbacks(own[1])
    assert any("себя" in label for label in labels(own[1]))

    other = admin_card_screen(ctx, 3, viewer_id=SECOND)
    assert other is not None
    assert callbacks(other[1]) == ["adm:admins"]


def test_an_unknown_admin_has_no_card(ctx) -> None:
    assert admin_card_screen(ctx, 404, viewer_id=OWNER) is None


# --------------------------------------------------------------------------- the grant


def test_only_linked_employees_can_be_picked(ctx) -> None:
    """The Bot API cannot turn a @username into a user id, so anybody who has never sent
    /start has to be typed in by number."""
    picked = callbacks(grant_screen(ctx)[1])
    assert f"adm:admpick:{ANYA}" not in picked

    ctx.offices.link_telegram_user("anya", ANYA)
    picked = callbacks(grant_screen(ctx)[1])
    assert f"adm:admpick:{ANYA}" in picked
    assert "adm:admid" in picked


def test_somebody_who_is_already_an_admin_is_not_offered(ctx) -> None:
    ctx.offices.link_telegram_user("anya", ANYA)
    ctx.admins.grant(ANYA, label="Аня", at=NOW)
    assert f"adm:admpick:{ANYA}" not in callbacks(grant_screen(ctx)[1])


def test_the_pick_button_carries_a_telegram_id_not_an_employee_id(ctx) -> None:
    """The two are different keys and neither is a valid lookup for the other.

    Adminship is keyed on the Telegram id, so that is what the button carries; the roster
    is asked only for a name. Reading this as an employee id silently found nobody and
    reported that the person had not linked their account.
    """
    ctx.offices.link_telegram_user("anya", ANYA)
    picked = [item for item in callbacks(grant_screen(ctx)[1]) if item.startswith("adm:admpick:")]

    assert picked == [f"adm:admpick:{ANYA}"]
    assert all(item.rsplit(":", 1)[1].isdecimal() for item in picked)
