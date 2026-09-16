"""Talking to the bot.

Deliberately last in the router chain: it answers anything that no command claimed, so
registering it earlier would swallow the admin and employee flows.

It also keeps a short cache of what it has seen. Telegram populates ``reply_to_message``
exactly one level deep, so following a thread further back is only possible from messages
we stored ourselves — including the bot's own replies, which are usually what a follow-up
question is attached to.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from tabelshchik.adapters.telegram.notifier import split_message
from tabelshchik.adapters.telegram.routers.common import UNKNOWN
from tabelshchik.application.audience import resolve
from tabelshchik.application.chat import Thread, answer
from tabelshchik.application.context import BotContext
from tabelshchik.application.ports import CachedMessage

logger = logging.getLogger(__name__)

router = Router(name="chat")


@router.message(F.text)
async def talk(message: Message, services: BotContext) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return

    is_private = message.chat.type == ChatType.PRIVATE
    grant = resolve(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        is_private=is_private,
        offices=services.offices,
        admin_ids=services.admin_ids,
        today=services.clock.today(),
    )

    # Before anything is read, cached or sent. A stranger reaching the model would be
    # handed a roster by name, and could write a fact into an office's shared memory that
    # every employee's prompt then carries.
    if grant.office_id is None:
        if is_private:
            await message.answer(UNKNOWN)
        # In a group the bot was added to by mistake, silence: a refusal is still noise.
        return

    # Cached after the gate and before the trigger check: a message nobody addressed to
    # the bot today is exactly the middle link a reply chain needs tomorrow, but a
    # stranger's messages serve no chain we will ever build.
    _remember(services, message, author=_display_name(message))

    # Telegram hands us the immediate parent in full even when privacy mode kept us from
    # seeing it live. Storing it is what guarantees at least one level of context in a
    # chat where the bot cannot read everything.
    if message.reply_to_message is not None:
        _remember(
            services,
            message.reply_to_message,
            author=_display_name(message.reply_to_message),
        )

    trigger = _trigger_for(message, services)
    if trigger is None or trigger not in services.chat_policy.triggers:
        return

    reply = await answer(
        question=_strip_mention(message, services),
        user_id=message.from_user.id,
        office_id=grant.office_id,
        offices=services.offices,
        schedule=services.schedule,
        usage=services.usage,
        voice=services.voice,
        clock=services.clock,
        policy=services.chat_policy,
        thread=Thread(
            chat_id=message.chat.id,
            # The parent, not this message: the question itself is passed separately, and
            # quoting it back to the model would just pay for it twice.
            reply_to_message_id=(
                message.reply_to_message.message_id
                if message.reply_to_message is not None
                else None
            ),
        ),
        messages=services.messages_cache,
        memories=services.memories,
    )
    if not reply.text:
        return

    # A detailed answer can outgrow one message, and Telegram does not truncate an
    # over-long one — it refuses it. Split on line breaks, so an escaped entity is never
    # cut in half, and thread only the first part.
    for index, chunk in enumerate(split_message(reply.text)):
        if index == 0:
            try:
                sent = await message.reply(chunk)
            except TelegramBadRequest:
                # The message being replied to can be deleted between us reading it and
                # answering. The answer is still worth sending; it just loses the threading.
                sent = await message.answer(chunk)
        else:
            sent = await message.answer(chunk)

        # Without this, a follow-up replying to the bot's own answer would find a chain
        # that stops dead at the bot's message — the half of the conversation that
        # matters most. Every part hangs off the question, whichever one is replied to.
        _remember(services, sent, author="Табельщик", replying_to=message.message_id)


def _remember(
    services: BotContext, message: Message, *, author: str, replying_to: int | None = None
) -> None:
    text = message.text or message.caption or ""
    if not text.strip():
        return
    services.messages_cache.remember(
        message.chat.id,
        CachedMessage(
            message_id=message.message_id,
            author=author,
            text=text,
            reply_to_message_id=(
                replying_to
                if replying_to is not None
                else (
                    message.reply_to_message.message_id
                    if message.reply_to_message is not None
                    else None
                )
            ),
        ),
        at=services.clock.now(),
    )


def _display_name(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "Кто-то"
    if user.is_bot:
        return "Табельщик"
    return user.full_name or (f"@{user.username}" if user.username else "Кто-то")


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


def _strip_mention(message: Message, services: BotContext) -> str:
    text = message.text or ""
    username = services.bot_username
    if username:
        text = text.replace(f"@{username}", " ")
    return text.strip()
