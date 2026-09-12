"""`/bind`: pointing an office at the group it posts into.

A supergroup id is not shown anywhere in Telegram and cannot be typed from memory, so the
only reliable way to learn it is to be in the chat. These pin the two things that make
that safe: the button is checked when it is pressed, not only when it is offered, and the
chat id comes from the chat rather than from the callback.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from tabelshchik.adapters.db.engine import session_scope
from tabelshchik.adapters.db.repositories import (
    SqlAdminStore,
    SqlAuditLog,
    SqlOfficeStore,
    SqlRosterStore,
)
from tabelshchik.adapters.db.seed import seed_admins
from tabelshchik.application import manage_offices

from .conftest import seed

NOW = datetime(2026, 9, 11, 12, 0)
OWNER = 1
GROUP = -1003760453775

BIND_SOURCE = Path("src/tabelshchik/adapters/telegram/routers/bind.py").read_text("utf-8")


@pytest.fixture
def stores(sessions):
    seed(sessions)
    with session_scope(sessions) as session:
        seed_admins(session, frozenset({OWNER}), now=NOW)
    return {
        "offices": SqlOfficeStore(sessions),
        "roster": SqlRosterStore(sessions),
        "audit": SqlAuditLog(sessions),
        "admins": SqlAdminStore(sessions),
    }


def test_binding_points_the_office_at_the_group(stores) -> None:
    manage_offices.bind_chat(
        office_id="ovest",
        chat_id=GROUP,
        actor_id=OWNER,
        offices=stores["offices"],
        roster=stores["roster"],
        audit=stores["audit"],
    )
    office = stores["offices"].get_office("ovest")
    assert office is not None and office.chat_id == GROUP


def test_unbinding_leaves_the_office_alone_otherwise(stores) -> None:
    manage_offices.bind_chat(
        office_id="ovest",
        chat_id=None,
        actor_id=OWNER,
        offices=stores["offices"],
        roster=stores["roster"],
        audit=stores["audit"],
    )
    office = stores["offices"].get_office("ovest")
    assert office is not None and office.chat_id is None
    assert len(stores["offices"].employees("ovest")) == 4


# ------------------------------------------------------------------ structural rules


def test_the_router_runs_in_groups_only() -> None:
    """In a private chat `chat.id` is the user's own id, so `/bind` there would point an
    office at one person's messages and post the whole roster into them daily."""
    assert "ChatType.GROUP" in BIND_SOURCE
    assert "ChatType.SUPERGROUP" in BIND_SOURCE
    assert "F.chat.type.in_(_GROUPS)" in BIND_SOURCE


def test_the_button_is_checked_when_it_is_pressed_not_only_when_offered() -> None:
    """Everyone in the group can see and press it. `AdminOnly` gates the command; without
    this check the picker's visibility would be the only thing standing in the way."""
    body = BIND_SOURCE.split("async def choose")[1]
    assert "services.is_admin(query.from_user.id)" in body
    assert "show_alert=True" in body


def test_the_chat_id_comes_from_the_chat_not_from_the_callback() -> None:
    """So it cannot go stale, and cannot be pointed at a chat the presser chose."""
    body = BIND_SOURCE.split("async def choose")[1]
    assert "chat_id=query.message.chat.id" in body
    assert not re.search(r"chat_id=int\(.*query\.data", body)


def test_the_admin_middleware_is_not_attached_to_the_callback() -> None:
    """It answers with silence, which in a group reads as a broken bot."""
    assert "router.message.middleware(AdminOnly())" in BIND_SOURCE
    assert "router.callback_query.middleware(AdminOnly())" not in BIND_SOURCE
