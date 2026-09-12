"""Employee self-service.

Everyone manages their own absences. The bot has to know who is talking to it, which is
what /start establishes; without that link there is nothing safe to show.
"""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from tabelshchik.adapters.telegram.keyboards import absence_list, button, employee_menu, keyboard
from tabelshchik.adapters.telegram.ui import redraw, rehome
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
    "Отправьте даты отпуска в формате <code>2026-10-01 2026-10-09</code>.\n"
    "Для одного дня достаточно одной даты."
)


#: Attached to every prompt and every rejected answer. Typing /cancel works too, but a
#: button is what people reach for — the admin router learned that the hard way.
def _cancel_keyboard() -> InlineKeyboardMarkup:
    return keyboard((button("✖️ Отмена", "me:menu"),))


def _menu_screen() -> tuple[str, InlineKeyboardMarkup]:
    return ("Меню:", employee_menu())


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
    text, markup = absences_screen(services, message.from_user.id if message.from_user else None)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "me:absences")
async def absences_callback(query: CallbackQuery, services: BotContext) -> None:
    await redraw(query, *absences_screen(services, query.from_user.id))


@router.callback_query(F.data == "me:menu")
async def back_to_menu(query: CallbackQuery, state: FSMContext) -> None:
    # Doubles as the ✖️ Отмена of the absence flow, so it clears the state too.
    await state.clear()
    await redraw(query, *_menu_screen())


@router.callback_query(F.data == "me:days")
async def days_callback(query: CallbackQuery, services: BotContext) -> None:
    # The callback's from_user is the person who pressed the button; the message's is the
    # bot, which is why the rendering takes an explicit user id.
    await redraw(
        query,
        render_my_days(services, query.from_user.id),
        keyboard((button("‹ Назад", "me:menu"),)),
    )


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
    # The week is content, so it lands below the menu; bring the menu down after it
    # rather than leaving it stranded above.
    await rehome(query.message, query.message.message_id, *_menu_screen())


@router.callback_query(F.data == "me:absadd")
async def start_adding(query: CallbackQuery, state: FSMContext) -> None:
    # The prompt replaces the list rather than stacking under it, so there is never a
    # second live keyboard to press by mistake.
    screen = await redraw(query, DATE_HELP, _cancel_keyboard())
    await state.set_state(AddAbsence.waiting_for_dates)
    await state.set_data({"screen": screen})


@router.message(Command("cancel"), StateFilter(AddAbsence.waiting_for_dates))
async def cancel(message: Message, state: FSMContext) -> None:
    """State-filtered on purpose.

    An unfiltered /cancel in one router answers for flows belonging to another — which is
    how cancelling a half-finished admin wizard once replied with the employee menu.
    """
    await _finish(message, state, *_menu_screen())


@router.message(AddAbsence.waiting_for_dates)
async def receive_dates(message: Message, state: FSMContext, services: BotContext) -> None:
    # A command is never a date. It only fails to become one here because `_parse_dates`
    # rejects it, which is luck; the admin flows lost a name to exactly this.
    if (message.text or "").lstrip().startswith("/"):
        await _step(message, state, DATE_HELP, _cancel_keyboard())
        return

    employee = _employee(services, message.from_user.id if message.from_user else None)
    if employee is None:
        await _finish(message, state, NOT_LINKED)
        return

    try:
        start, end = _parse_dates(message.text or "")
    except ValueError:
        await _step(message, state, DATE_HELP, _cancel_keyboard())
        return

    office_id = _office_of(services, employee.id)
    if office_id is None:
        await _finish(message, state, NOT_LINKED)
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
        # Stays in the state: a rejected range is worth retyping, not restarting.
        await _step(message, state, str(error), _cancel_keyboard())
        return

    common = services.voice.catalog.common
    reply = f"Отпуск записан: {format_date(start, common)} — {format_date(end, common)}."

    if result.cancelled:
        reply += f"\nСнято с расписания дней: {len(result.cancelled)}."
    if result.needs_correction:
        reply += "\nНа уже объявленные дни отправлю в чат уточнение."

    text, markup = absences_screen(services, message.from_user.id if message.from_user else None)
    await _finish(message, state, f"{reply}\n\n{text}", markup)
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
        await query.answer("Не найдено.", show_alert=True)
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
    text, markup = absences_screen(services, query.from_user.id)
    await redraw(query, f"Отпуск удалён.\n\n{text}", markup)


def absences_screen(services: BotContext, user_id: int | None) -> tuple[str, Any]:
    """Somebody's upcoming absences.

    A builder rather than a sender, so deleting one redraws the list in place. It used to
    send two fresh messages per deletion, each leaving a live keyboard behind pointing at
    ids that no longer existed.

    The count is in the text on purpose: Telegram refuses to redraw a message whose text
    and markup are both unchanged, and deleting the last one changes only the keyboard.
    """
    employee = _employee(services, user_id)
    if employee is None:
        return (NOT_LINKED, None)

    entries = services.absences.for_employee(employee.id, upcoming_from=services.clock.today())
    common = services.voice.catalog.common
    labels = [
        (absence_id, f"{format_date(start, common)} — {format_date(end, common)}")
        for absence_id, start, end, _kind in entries
    ]
    text = f"Ваши отпуска — {len(labels)}:" if labels else "Отпусков не запланировано."
    return (text, absence_list(labels))


async def _step(message: Message, state: FSMContext, text: str, markup: Any = None) -> None:
    """A re-prompt, moved down below what the user just typed."""
    data = await state.get_data()
    await state.update_data(screen=await rehome(message, data.get("screen"), text, markup))


async def _finish(message: Message, state: FSMContext, text: str, markup: Any = None) -> None:
    """The last screen of the flow. Moves it down, then forgets the flow."""
    data = await state.get_data()
    await rehome(message, data.get("screen"), text, markup)
    await state.clear()


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
