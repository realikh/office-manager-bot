"""The published command menu against the handlers that exist.

A menu entry with no handler is worse than an undiscoverable command: it looks
supported and silently does nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tabelshchik.adapters.telegram.commands import (
    ADMIN_ONLY,
    GROUP,
    PRIVATE,
    publish_commands,
)


def handlers_in(router: str) -> set[str]:
    """Command names the router actually registers, read from its source."""
    source = Path(f"src/tabelshchik/adapters/telegram/routers/{router}.py").read_text(
        encoding="utf-8"
    )
    found = set(re.findall(r'Command\("([a-z-]+)"\)', source))
    # /start has its own filter rather than being a Command("start").
    if "CommandStart()" in source:
        found.add("start")
    return found


def all_handlers() -> set[str]:
    return handlers_in("common") | handlers_in("employee") | handlers_in("admin")


@pytest.mark.parametrize("name", [name for name, _ in (*PRIVATE, *GROUP, *ADMIN_ONLY)])
def test_every_published_command_has_a_handler(name: str) -> None:
    assert name in all_handlers(), f"/{name} is advertised but not implemented"


def test_admin_is_not_advertised_to_everyone() -> None:
    """Publishing it to a group of twenty people who cannot use it is noise."""
    assert "admin" not in {name for name, _ in PRIVATE}
    assert "admin" not in {name for name, _ in GROUP}
    assert "admin" in {name for name, _ in ADMIN_ONLY}


def test_groups_do_not_advertise_personal_commands() -> None:
    """They answer with one person's schedule; in a group that is both noise and a leak."""
    group_names = {name for name, _ in GROUP}
    assert "me" not in group_names
    assert "vacation" not in group_names


def test_descriptions_are_present_and_within_telegram_limits() -> None:
    for name, description in (*PRIVATE, *GROUP, *ADMIN_ONLY):
        assert description, f"/{name} has no description"
        assert 1 <= len(description) <= 256
        assert re.fullmatch(r"[a-z_]{1,32}", name), f"/{name} is not a valid command name"


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def set_my_commands(self, commands, scope):
        self.calls.append((type(scope).__name__, tuple(c.command for c in commands)))


async def test_scopes_are_published_for_every_audience() -> None:
    bot = RecordingBot()
    await publish_commands(bot, admin_ids=[42])  # type: ignore[arg-type]

    scopes = {scope for scope, _ in bot.calls}
    assert scopes == {
        "BotCommandScopeAllPrivateChats",
        "BotCommandScopeAllGroupChats",
        "BotCommandScopeChat",
    }


async def test_an_admins_menu_keeps_the_ordinary_commands_too() -> None:
    """A per-chat scope replaces the private-chat one rather than adding to it, so the
    admin menu has to repeat everything or admins lose /me and /vacation."""
    bot = RecordingBot()
    await publish_commands(bot, admin_ids=[42])  # type: ignore[arg-type]

    admin_menu = next(names for scope, names in bot.calls if scope == "BotCommandScopeChat")
    assert "admin" in admin_menu
    assert "me" in admin_menu and "vacation" in admin_menu


async def test_no_per_chat_scope_without_admins() -> None:
    bot = RecordingBot()
    await publish_commands(bot, admin_ids=[])  # type: ignore[arg-type]
    assert not any(scope == "BotCommandScopeChat" for scope, _ in bot.calls)
