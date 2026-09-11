"""Account linking and the entry points everyone shares."""

from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from tabelshchik.adapters.telegram.keyboards import employee_menu, main_menu
from tabelshchik.application.context import BotContext

router = Router(name="common")

UNKNOWN = (
    "Я вас пока не знаю. Попросите администратора добавить вас в расписание — "
    "или укажите в Telegram имя пользователя, совпадающее с тем, что в списке офиса."
)


@router.message(CommandStart())
async def start(message: Message, services: BotContext) -> None:
    user = message.from_user
    if user is None:
        return

    employee = _resolve(services, user.id, user.username)
    if employee is None:
        await message.answer(UNKNOWN)
        return

    # Learning the numeric id is what lets the bot mention people who have no username.
    services.offices.link_telegram_user(employee.id, user.id)

    greeting = f"Здравствуйте, {html.escape(employee.full_name)}."
    extra = "\n\nВы администратор — /admin." if services.is_admin(user.id) else ""
    await message.answer(f"{greeting}{extra}", reply_markup=employee_menu())


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


@router.message(Command("menu"))
async def menu(message: Message, services: BotContext) -> None:
    if services.is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Меню администратора:", reply_markup=main_menu())
        return
    await message.answer("Меню:", reply_markup=employee_menu())


def _resolve(services: BotContext, user_id: int, username: str | None):  # type: ignore[no-untyped-def]
    found = services.offices.find_employee_by_user_id(user_id)
    if found is not None:
        return found
    return services.offices.find_employee_by_username(username) if username else None
