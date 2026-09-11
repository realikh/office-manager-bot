"""Talking to the bot.

Deliberately last in the router chain: it answers anything that no command claimed, so
registering it earlier would swallow the admin and employee flows.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from tabelshchik.application.chat import answer
from tabelshchik.application.context import BotContext

logger = logging.getLogger(__name__)

router = Router(name="chat")


@router.message(F.text)
async def talk(message: Message, services: BotContext) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return

    trigger = _trigger_for(message, services)
    if trigger is None or trigger not in services.chat_policy.triggers:
        return

    office_id = _office_for(services, message)
    if office_id is None:
        return

    reply = await answer(
        question=_strip_mention(message, services),
        user_id=message.from_user.id,
        office_id=office_id,
        offices=services.offices,
        schedule=services.schedule,
        usage=services.usage,
        voice=services.voice,
        clock=services.clock,
        policy=services.chat_policy,
    )
    if reply.text:
        await message.reply(reply.text)


def _trigger_for(message: Message, services: BotContext) -> str | None:
    if message.chat.type == ChatType.PRIVATE:
        return "private"

    bot_username = (services.bot_username or "").lower()
    text = (message.text or "").lower()
    if bot_username and f"@{bot_username}" in text:
        return "mention"

    replied = message.reply_to_message
    if replied is not None and replied.from_user is not None and replied.from_user.is_bot:
        return "reply"

    return None


def _office_for(services: BotContext, message: Message) -> str | None:
    """Which office's schedule to answer from.

    In a group that is the office whose chat it is. In a private message it is the
    asker's own office, and failing that the first active one — so a stranger still
    gets an answer rather than silence.
    """
    for office in services.offices.active_offices():
        if office.chat_id == message.chat.id:
            return office.id

    if message.from_user is not None:
        employee = services.offices.find_employee_by_user_id(message.from_user.id)
        if employee is not None:
            for office in services.offices.active_offices():
                if any(e.id == employee.id for e in services.offices.employees(office.id)):
                    return office.id

    active = services.offices.active_offices()
    return active[0].id if active else None


def _strip_mention(message: Message, services: BotContext) -> str:
    text = message.text or ""
    username = services.bot_username
    if username:
        text = text.replace(f"@{username}", " ")
    return text.strip()
