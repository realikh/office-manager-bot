"""Account linking and the entry points everyone shares."""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from tabelshchik.adapters.telegram.keyboards import employee_menu, main_menu
from tabelshchik.application.audience import Claim, claim
from tabelshchik.application.context import BotContext
from tabelshchik.domain.entities import Employee

logger = logging.getLogger(__name__)

router = Router(name="common")

UNKNOWN = (
    "Я вас пока не знаю. Попросите администратора добавить вас в расписание — "
    "или укажите в Telegram имя пользователя, совпадающее с тем, что в списке офиса."
)

#: Deliberately vague. The claimant already knows the username they typed; telling them
#: anything more about whose it is would make the bot an enumeration oracle.
TAKEN = "Эта учётная запись уже привязана к другому аккаунту Telegram. Обратитесь к администратору."


@router.message(CommandStart())
async def start(message: Message, services: BotContext) -> None:
    user = message.from_user
    if user is None:
        return

    result = claim(
        services.offices,
        user_id=user.id,
        username=user.username,
        today=services.clock.today(),
    )
    who = result.employee

    if result.outcome is Claim.UNKNOWN or who is None:
        await message.answer(UNKNOWN)
        return

    if result.outcome is Claim.TAKEN:
        services.audit.record(
            actor_id=user.id, action="account.link.refused", payload={"employee": who.id}
        )
        await _tell_admin(
            services,
            "⚠️ Попытка привязки к занятой учётной записи: "
            f"{html.escape(who.full_name)} (<code>{who.id}</code>). "
            f"Запрос от {_describe(user.id, user.username)}.",
        )
        await message.answer(TAKEN)
        return

    if result.outcome is Claim.GRANTED:
        # Learning the numeric id is what lets the bot mention people who have no username.
        services.offices.link_telegram_user(who.id, user.id)
        services.audit.record(actor_id=user.id, action="account.link", payload={"employee": who.id})
        await _tell_admin(
            services,
            f"🔗 Привязка: {html.escape(who.full_name)} (<code>{who.id}</code>) → "
            f"{_describe(user.id, user.username)}.",
        )

    await _greet(message, services, who, user.id)


def _describe(user_id: int, username: str | None) -> str:
    handle = f" (@{html.escape(username)})" if username else ""
    return f"Telegram id <code>{user_id}</code>{handle}"


async def _greet(message: Message, services: BotContext, employee: Employee, user_id: int) -> None:
    greeting = f"Здравствуйте, {html.escape(employee.full_name)}."
    extra = "\n\nВы администратор — /admin." if services.is_admin(user_id) else ""
    await message.answer(f"{greeting}{extra}", reply_markup=employee_menu())


async def _tell_admin(services: BotContext, text: str) -> None:
    """Linking is the one thing a stranger can attempt that changes data, so it is
    reported rather than merely audited."""
    chat_id = services.admin_chat_id
    if chat_id is None or services.notifier is None:
        return
    try:
        await services.notifier.send(chat_id, text, silent=True)
    except Exception:
        logger.warning("could not notify the admin about a link", exc_info=True)


@router.message(Command("help"))
async def help_command(message: Message, services: BotContext) -> None:
    lines = [
        "<b>Табельщик</b> — учёт присутствия в офисе.",
        "",
        "/start — привязать аккаунт",
        "/me — мои ближайшие дни в офисе",
        "/vacation — мои отпуска",
        "",
        "Упомяните меня в чате или напишите в личные сообщения — отвечу.",
    ]
    if services.is_admin(message.from_user.id if message.from_user else None):
        lines.append("/admin — режим администратора")
    await message.answer("\n".join(lines))


@router.message(Command("menu"), F.chat.type == ChatType.PRIVATE)
async def menu(message: Message, services: BotContext) -> None:
    """Private only. The menu leads to screens that name other people, and a keyboard
    handed out in a group is one tap away from publishing the roster there."""
    if services.is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Меню администратора:", reply_markup=main_menu())
        return
    await message.answer("Меню:", reply_markup=employee_menu())
