"""The two ways a screen reaches the user.

One rule: **the screen is the last message.** If nothing has been added to the chat since
it was drawn, `redraw` edits it in place. If something has — the answer the user just
typed, or content the bot just sent — `rehome` deletes it and posts it again at the
bottom. It is never duplicated.

That rule is not cosmetic. A second message carrying a keyboard is a second *live* screen,
and the older one keeps working: pressing a button on it acts on state that has moved on.
The bug that prompted this put two office lists on screen three minutes apart, because a
flow's prompt arrived as a new message and ✖️ Отмена then edited the prompt — the button
was on it — leaving the list it was launched from above, stale and still clickable.

The asymmetry is Telegram's: a bot may delete its own messages, but not somebody else's in
a private chat. The user's typed answer therefore always stays in the transcript, which is
why the screen follows it down instead of updating silently above it — with a keyboard up
on a phone, an edit above the fold looks like nothing happened at all.
"""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message


async def redraw(query: CallbackQuery, text: str, markup: Any = None) -> int | None:
    """Edit the screen the pressed button lives on, in place.

    Narrow on purpose. A bare `except Exception` here turned a crash in the screen being
    drawn into a second, stale message — which looked like the UI simply not responding.

    Returns the id the screen now occupies, which is not always the id it started on: an
    un-editable message is replaced by a fresh one, and a caller remembering the old id
    would later delete the wrong thing.
    """
    await query.answer()
    if not isinstance(query.message, Message):
        return None

    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" in str(error):
            # Pressing a button that changes nothing is not a failure; Telegram just
            # declines to redraw an identical message.
            return query.message.message_id
        # Too old to edit, or deleted. A fresh message beats silence.
        sent = await query.message.answer(text, reply_markup=markup)
        return sent.message_id
    return query.message.message_id


async def rehome(below: Message, previous: int | None, text: str, markup: Any = None) -> int:
    """Post the screen beneath `below` and take the old one away.

    Sends before deleting on purpose: a failed delete leaves a stale screen, which is
    untidy, while a failed send after a delete leaves the user with no screen at all.

    Deleting is best-effort — Telegram refuses beyond 48 hours, and the message may
    already be gone — so nothing may depend on it having worked.
    """
    sent = await below.answer(text, reply_markup=markup)
    bot = below.bot
    if bot is not None and previous is not None and previous != sent.message_id:
        with contextlib.suppress(TelegramBadRequest):
            await bot.delete_message(below.chat.id, previous)
    return sent.message_id
