"""Assembling the bot.

Router order is load-bearing: chat answers anything nothing else claimed, so it goes
last. Registering it earlier would swallow every command.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import TelegramObject

from tabelshchik.adapters.telegram.routers import admin, chat, common, employee
from tabelshchik.application.context import BotContext


class ServicesMiddleware(BaseMiddleware):
    """Hands every handler the container, so nothing reaches for a global."""

    def __init__(self, services: BotContext) -> None:
        self.services = services

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["services"] = self.services
        return await handler(event, data)


def create_bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_dispatcher(services: BotContext) -> Dispatcher:
    dispatcher = Dispatcher()
    middleware = ServicesMiddleware(services)
    dispatcher.message.middleware(middleware)
    dispatcher.callback_query.middleware(middleware)

    dispatcher.include_router(common.router)
    dispatcher.include_router(admin.router)
    dispatcher.include_router(employee.router)
    # Last: it is the catch-all.
    dispatcher.include_router(chat.router)

    return dispatcher
