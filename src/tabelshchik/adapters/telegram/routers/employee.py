"""Employee self-service.

Everyone manages their own absences. The bot has to know who is talking to it, which is
what /start establishes; without that link there is nothing safe to show.
"""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from tabelshchik.adapters.telegram.keyboards import absence_list, employee_menu
from tabelshchik.application.audience import linked
from tabelshchik.application.context import BotContext
from tabelshchik.application.manage_absences import (
    AbsenceError,
    add_absence,
    remove_absence,
)
from tabelshchik.application.voice import format_date, render_days
from tabelshchik.domain.entities import Employee

router = Router(name="employee")

NOT_LINKED = "Сначала напишите /start, чтобы я вас узнал."
DATE_HELP = (
    "Пришлите даты отпуска в формате <code>2026-10-01 2026-10-09</code>.\n"
    "Для одного дня достаточно одной даты. Отмена — /cancel."
)


class AddAbsence(StatesGroup):
    waiting_for_dates = State()


def _employee(services: BotContext, user_id: int | None) -> Employee | None:
    """The caller, if they still work here.

    Tenure is what this adds. Ending a tenure leaves the Telegram link in place — on
    purpose, so a restore just works and reminders can still tag people historically —
    which meant somebody who left last month kept `/me`, `/vacation` and the whole
    office's week for as long as the bot ran.
    """
    return linked(services.offices, user_id, services.clock.today())


def _office_of(services: BotContext, employee_id: str) -> str | None:
    """Their office, if it is still open.

    The open check is deliberate rather than incidental: self-service shows the office's
    week and accepts absences against it, and a closed office has neither.
    """
    office_id = services.offices.office_of(employee_id)
    if office_id is None:
        return None
    return office_id if any(o.id == office_id for o in services.offices.active_offices()) else None


def render_my_days(services: BotContext, user_id: int | None) -> str:
    employee = _employee(services, user_id)
    if employee is None:
        return NOT_LINKED

    days = services.schedule.upcoming_for_employee(
        employee.id, start=services.clock.today(), limit=10
    )
    if not days:
        return "Ближайших дней в офисе не запланировано."

    common = services.voice.catalog.common
    listed = "\n".join(
        f"• {format_date(day, common)} ({common.weekdays[day.weekday()]})" for day in days
    )
    return f"Ваши ближайшие дни в офисе:\n\n{listed}"


@router.message(Command("me"))
async def my_days(message: Message, services: BotContext) -> None:
    user_id = message.from_user.id if message.from_user else None
    await message.answer(render_my_days(services, user_id))


@router.message(Command("vacation"))
async def my_absences(message: Message, services: BotContext) -> None:
    await _show_absences(message, services, message.from_user.id if message.from_user else None)


@router.callback_query(F.data == "me:absences")
async def absences_callback(query: CallbackQuery, services: BotContext) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await _show_absences(query.message, services, query.from_user.id)


@router.callback_query(F.data == "me:menu")
async def back_to_menu(query: CallbackQuery) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.answer("Меню:", reply_markup=employee_menu())


@router.callback_query(F.data == "me:days")
async def days_callback(query: CallbackQuery, services: BotContext) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        # The callback's from_user is the person who pressed the button; the message's
        # is the bot, which is why the rendering takes an explicit user id.
        await query.message.answer(render_my_days(services, query.from_user.id))


@router.callback_query(F.data == "me:office", F.message.chat.type == ChatType.PRIVATE)
async def office_week(query: CallbackQuery, services: BotContext) -> None:
    """Private only: this one names everybody, unlike the other `me:` screens, which show
    the caller their own data and are theirs to publish wherever they like."""
    await query.answer()
    employee = _employee(services, query.from_user.id)
    if employee is None or not isinstance(query.message, Message):
        return

    office_id = _office_of(services, employee.id)
    if office_id is None:
        await query.message.answer(NOT_LINKED)
        return

    today = services.clock.today()
    snapshots = services.schedule.days_between(office_id, today, today + timedelta(days=7))
    names = {e.id: e.full_name for e in services.offices.employees(office_id)}

    chunks = render_days(
        [
            (
                snapshot.day,
                sorted((names.get(eid, eid) for eid in snapshot.roster), key=str.casefold),
            )
            for snapshot in snapshots
        ],
        services.voice.catalog.common,
    )
    for chunk in chunks or ["На ближайшую неделю никого не запланировано."]:
        await query.message.answer(chunk)


@router.callback_query(F.data == "me:absadd")
async def start_adding(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(AddAbsence.waiting_for_dates)
    if isinstance(query.message, Message):
        await query.message.answer(DATE_HELP)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=employee_menu())


@router.message(AddAbsence.waiting_for_dates)
async def receive_dates(message: Message, state: FSMContext, services: BotContext) -> None:
    # A command is never a date. It only fails to become one here because `_parse_dates`
    # rejects it, which is luck; the admin flows lost a name to exactly this.
    if (message.text or "").lstrip().startswith("/"):
        await message.answer(DATE_HELP)
        return

    employee = _employee(services, message.from_user.id if message.from_user else None)
    if employee is None:
        await state.clear()
        await message.answer(NOT_LINKED)
        return

    try:
        start, end = _parse_dates(message.text or "")
    except ValueError:
        await message.answer(DATE_HELP)
        return

    office_id = _office_of(services, employee.id)
    if office_id is None:
        await state.clear()
        await message.answer(NOT_LINKED)
        return

    try:
        result = add_absence(
            employee_id=employee.id,
            office_id=office_id,
            start=start,
            end=end,
            actor_id=message.from_user.id if message.from_user else None,
            absences=services.absences,
            schedule=services.schedule,
            offices=services.offices,
            ledger=services.ledger,
            clock=services.clock,
            policy=services.schedule_policy,
            audit=services.audit,
        )
    except AbsenceError as error:
        await message.answer(str(error))
        return

    await state.clear()
    common = services.voice.catalog.common
    reply = f"Отпуск записан: {format_date(start, common)} — {format_date(end, common)}."

    if result.cancelled:
        reply += f"\nСнято с расписания дней: {len(result.cancelled)}."
    if result.needs_correction:
        reply += "\nНа уже объявленные дни отправлю в чат уточнение."

    await message.answer(reply, reply_markup=employee_menu())
    await _notify_corrections(services, office_id, result.announced_days_affected, employee)


@router.callback_query(F.data.startswith("me:absdel:"))
async def delete_absence(query: CallbackQuery, services: BotContext) -> None:
    await query.answer()
    employee = _employee(services, query.from_user.id)
    if employee is None or not isinstance(query.message, Message):
        return

    absence_id = int(str(query.data).rsplit(":", 1)[1])
    existing = services.absences.get(absence_id)
    # Only your own: a callback id is guessable, so ownership is checked, not assumed.
    if existing is None or existing[1] != employee.id:
        await query.message.answer("Не найдено.")
        return

    office_id = _office_of(services, employee.id)
    if office_id is None:
        return

    remove_absence(
        absence_id=absence_id,
        office_id=office_id,
        actor_id=query.from_user.id,
        absences=services.absences,
        schedule=services.schedule,
        offices=services.offices,
        ledger=services.ledger,
        clock=services.clock,
        policy=services.schedule_policy,
        audit=services.audit,
    )
    await query.message.answer("Отпуск удалён.")
    await _show_absences(query.message, services, query.from_user.id)


async def _show_absences(message: Message, services: BotContext, user_id: int | None) -> None:
    employee = _employee(services, user_id)
    if employee is None:
        await message.answer(NOT_LINKED)
        return

    entries = services.absences.for_employee(employee.id, upcoming_from=services.clock.today())
    common = services.voice.catalog.common
    labels = [
        (absence_id, f"{format_date(start, common)} — {format_date(end, common)}")
        for absence_id, start, end, _kind in entries
    ]
    text = "Ваши отпуска:" if labels else "Отпусков не запланировано."
    await message.answer(text, reply_markup=absence_list(labels))


async def _notify_corrections(
    services: BotContext, office_id: str, days: tuple[date, ...], employee: Employee
) -> None:
    """Tell the chat when an absence lands on a day that was already announced."""
    if not days or services.notifier is None:
        return
    office = services.offices.get_office(office_id)
    if office is None or office.chat_id is None:
        return

    common = services.voice.catalog.common
    listed = ", ".join(format_date(day, common) for day in days)
    await services.notifier.send(
        office.chat_id,
        f"✏️ {html.escape(employee.full_name)} не сможет выйти в офис: {listed}. "
        "Расписание пересчитано.",
        silent=True,
    )


def _parse_dates(raw: str) -> tuple[date, date]:
    parts = raw.replace(",", " ").split()
    if not parts:
        raise ValueError("no dates")
    start = _parse_one(parts[0])
    end = _parse_one(parts[1]) if len(parts) > 1 else start
    return start, end


def _parse_one(raw: str) -> date:
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {raw}")
