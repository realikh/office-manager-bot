"""The command menu Telegram shows.

Registered from code rather than typed into @BotFather, so the menu cannot drift from
the handlers that actually exist — a command listed but unimplemented is worse than one
that is merely undiscoverable.

Scoped deliberately. Everyone sees the commands they can use; `/admin` is published only
to admins, so it does not advertise itself in a group of twenty people who cannot use it.
"""

from __future__ import annotations

from collections.abc import Iterable

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

#: What anyone can do in a private chat with the bot.
PRIVATE: tuple[tuple[str, str], ...] = (
    ("start", "Привязать аккаунт"),
    ("menu", "Меню"),
    ("me", "Мои ближайшие дни в офисе"),
    ("vacation", "Мои отпуска"),
    ("help", "Справка"),
)

#: Groups get almost nothing: the personal commands answer with private information, and
#: a menu of twenty entries in a busy chat is clutter.
GROUP: tuple[tuple[str, str], ...] = (("help", "Справка"),)

ADMIN_ONLY: tuple[tuple[str, str], ...] = (("admin", "Режим администратора"),)


def _commands(pairs: Iterable[tuple[str, str]]) -> list[BotCommand]:
    return [BotCommand(command=name, description=text) for name, text in pairs]


async def publish_commands(bot: Bot, *, admin_ids: Iterable[int] = ()) -> None:
    """Publish the command menu. Safe to call on every boot; it overwrites."""
    await bot.set_my_commands(_commands(PRIVATE), scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(_commands(GROUP), scope=BotCommandScopeAllGroupChats())

    for admin_id in admin_ids:
        # A per-chat scope overrides the private-chat one, so the admin's menu has to
        # repeat the ordinary commands rather than only adding to them.
        await bot.set_my_commands(
            _commands((*PRIVATE, *ADMIN_ONLY)),
            scope=BotCommandScopeChat(chat_id=admin_id),
        )
