"""Middlewares shared by the routers that need a gate.

`AdminOnly` lives here rather than in `routers/admin.py` because the binding router needs
it too, and that router deliberately runs in group chats — importing it from a module
whose own filter is `chat.type == PRIVATE` would be one refactor away from confusion.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from tabelshchik.application.context import BotContext


class AdminOnly(BaseMiddleware):
    """One gate for the whole area.

    A non-admin gets silence rather than a refusal: the admin surface is not something
    to advertise to a group chat.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        services: BotContext = data["services"]
        user = data.get("event_from_user")
        if user is None or not services.is_admin(user.id):
            return None
        return await handler(event, data)
