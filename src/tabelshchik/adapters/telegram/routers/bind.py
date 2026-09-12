"""Pointing an office at the group it posts into.

A supergroup id is not shown anywhere in the Telegram interface and cannot be typed from
memory, so the only reliable way to learn it is to be in the chat. `/bind` is that: run it
in the office's group and pick the office.

This cannot live in the admin router. That one filters on `chat.type == PRIVATE` at the
router level, deliberately — its screens name people, and `_replace` edits in whatever
chat the button lives in, so an admin screen opened in a group publishes the roster there.
Nothing here names anybody: the picker lists office names, which the group already sees in
every message header.
"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from tabelshchik.adapters.telegram.keyboards import button, keyboard
from tabelshchik.adapters.telegram.middlewares import AdminOnly
from tabelshchik.application import manage_offices
from tabelshchik.application.context import BotContext
from tabelshchik.application.manage_offices import OfficeError

router = Router(name="bind")

# Gates the *command*, not the button: see `choose` below.
router.message.middleware(AdminOnly())

#: Groups only. In a private chat `chat.id` is the user's own id, so `/bind` there would
#: point an office at one person's messages and post that office's full tagged roster
#: into them every working day. The private office screen has a paste-an-id route for the
#: rare case where an admin cannot post in the group.
_GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
router.message.filter(F.chat.type.in_(_GROUPS))
router.callback_query.filter(F.message.chat.type.in_(_GROUPS))

NOT_AN_ADMIN = "Это может только администратор."


@router.message(Command("bind"))
async def bind(message: Message, services: BotContext) -> None:
    """Offer the offices this chat could be bound to."""
    offices = services.offices.active_offices()
    if not offices:
        await message.reply("Пока нет ни одного офиса. Создайте его в /admin.")
        return

    current = next((office for office in offices if office.chat_id == message.chat.id), None)
    heading = (
        f"Сейчас этот чат — «{html.escape(current.name)}».\nВыберите офис, чтобы изменить:"
        if current is not None
        else "Какой офис писать в этот чат?"
    )
    rows = [(button(office.name, f"bind:pick:{office.id}"),) for office in offices]
    await message.reply(heading, reply_markup=keyboard(*rows))


@router.callback_query(F.data.startswith("bind:pick:"))
async def choose(query: CallbackQuery, services: BotContext) -> None:
    """Bind this chat to the chosen office.

    Checked here rather than by the middleware. Everyone in the group can see and press
    this button, and `AdminOnly` answers with silence — right for a private admin surface,
    wrong in a group, where a dead button reads as a broken bot. There is nothing secret
    about "only an admin may do this", so say it.
    """
    if not services.is_admin(query.from_user.id):
        await query.answer(NOT_AN_ADMIN, show_alert=True)
        return
    if not isinstance(query.message, Message):
        return

    office_id = str(query.data).rsplit(":", 1)[1]
    try:
        change = manage_offices.bind_chat(
            office_id=office_id,
            # From the chat the button is in, not from the callback data: it cannot go
            # stale and it cannot be pointed somewhere else.
            chat_id=query.message.chat.id,
            actor_id=query.from_user.id,
            offices=services.offices,
            roster=services.roster,
            audit=services.audit,
        )
    except OfficeError as error:
        await query.answer(str(error), show_alert=True)
        return

    await query.answer()
    await query.message.edit_text(
        f"Готово — напоминания офиса «{html.escape(change.name)}» будут приходить сюда."
    )


@router.message(Command("unbind"))
async def unbind(message: Message, services: BotContext) -> None:
    bound = [
        office for office in services.offices.all_offices() if office.chat_id == message.chat.id
    ]
    if not bound:
        await message.reply("Этот чат ни к какому офису не привязан.")
        return

    for office in bound:
        manage_offices.bind_chat(
            office_id=office.id,
            chat_id=None,
            actor_id=message.from_user.id if message.from_user else 0,
            offices=services.offices,
            roster=services.roster,
            audit=services.audit,
        )
    names = ", ".join(html.escape(office.name) for office in bound)
    await message.reply(f"Отвязано: {names}. Напоминания сюда больше не придут.")
