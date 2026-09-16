"""Admin mode.

The whole router is gated by one filter rather than a check repeated in each handler —
a permission check you have to remember to write is one you will eventually forget.
"""

from __future__ import annotations

import asyncio
import html
from collections import defaultdict
from collections.abc import Collection
from datetime import time, timedelta
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from tabelshchik.adapters.reports.xlsx import build_workbook, schedule_caption
from tabelshchik.adapters.telegram.bursts import Bursts
from tabelshchik.adapters.telegram.commands import publish_for
from tabelshchik.adapters.telegram.keyboards import (
    WEEKDAY_LABELS,
    admin_card,
    admin_list,
    calendar_picker,
    cancel_keyboard,
    chat_menu,
    confirm,
    employee_card,
    employee_list,
    gender_picker,
    grant_picker,
    keyboard,
    limits_menu,
    main_menu,
    office_list,
    office_menu,
    office_settings_menu,
    relay_picker,
    times_menu,
    weekday_picker,
)
from tabelshchik.adapters.telegram.middlewares import AdminOnly
from tabelshchik.adapters.telegram.ui import redraw, rehome
from tabelshchik.application import (
    manage_admins,
    manage_limits,
    manage_offices,
    manage_roster,
    manage_times,
    relay,
)
from tabelshchik.application.build_report import ReportDay, build_report
from tabelshchik.application.context import BotContext
from tabelshchik.application.manage_admins import AdminError
from tabelshchik.application.manage_limits import (
    MAX_DAILY_LIMIT,
    MAX_GLOBAL_LIMIT,
    LimitError,
)
from tabelshchik.application.manage_offices import OfficeError
from tabelshchik.application.manage_roster import RosterError
from tabelshchik.application.manage_times import TimeError
from tabelshchik.application.policy import RUSH_WINDOW_MINUTES
from tabelshchik.application.ports import ScheduleTime
from tabelshchik.application.regenerate_schedule import regenerate
from tabelshchik.application.relay import Destination, Relayed, RelayError
from tabelshchik.application.send_attendance_reminder import send_attendance_reminder
from tabelshchik.application.voice import format_date, render_days
from tabelshchik.config.loader import parse_file
from tabelshchik.domain.entities import SCALE, Employee

router = Router(name="admin")


class SetDesks(StatesGroup):
    waiting_for_count = State()


class AddEmployee(StatesGroup):
    waiting_for_name = State()
    waiting_for_username = State()
    waiting_for_gender = State()


class EditEmployee(StatesGroup):
    waiting_for_name = State()
    waiting_for_username = State()


class GrantAdmin(StatesGroup):
    waiting_for_user_id = State()


class SetLimit(StatesGroup):
    waiting_for_value = State()


class SetTime(StatesGroup):
    waiting_for_value = State()


class CreateOffice(StatesGroup):
    waiting_for_name = State()


class EditOffice(StatesGroup):
    waiting_for_name = State()
    waiting_for_chat_id = State()
    waiting_for_deletion = State()


class Relay(StatesGroup):
    #: One state for the whole flow: whether the message has been chosen yet lives in the
    #: data, so a part arriving late finds the same handler either way.
    composing = State()


SKIP = "-"

#: Sanity bound on a typed desk count. There is no schema maximum, and a fat-fingered
#: 1000 would quietly guarantee a shortfall on that weekday forever.
MAX_DESKS = 99

#: Every step that reads free text uses these two. Registered *above* the state handlers
#: on purpose: aiogram dispatches in registration order, and a state handler matches any
#: message in its state — which is how typing `/cancel` during a rename renamed somebody
#: to "/cancel" instead of cancelling.
_IN_A_FLOW = (
    SetDesks.waiting_for_count,
    AddEmployee.waiting_for_name,
    AddEmployee.waiting_for_username,
    AddEmployee.waiting_for_gender,
    EditEmployee.waiting_for_name,
    EditEmployee.waiting_for_username,
    GrantAdmin.waiting_for_user_id,
    SetLimit.waiting_for_value,
    SetTime.waiting_for_value,
    CreateOffice.waiting_for_name,
    EditOffice.waiting_for_name,
    EditOffice.waiting_for_chat_id,
    EditOffice.waiting_for_deletion,
    Relay.composing,
)

COMMAND_IN_FLOW = "Это похоже на команду. Отправьте значение или нажмите «Отмена»."
NOT_THE_OWNER = "Это может только владелец."
NO_SUCH_OFFICE = "Офис не найден"
NO_SUCH_EMPLOYEE = "Сотрудник не найден"

#: Offered as buttons rather than typed. An unknown code makes the holiday library return
#: nothing, so a typo would be a silent "no public holidays, ever".
CALENDARS = ("KZ", "RU", "UZ", "KG", "AZ", "GE", "AM", "TR")
GRANT_HINT = "Если пункт /admin не появился, попросите человека написать боту /start."


@router.message(Command("cancel"), StateFilter(*_IN_A_FLOW))
async def cancel_admin_flow(message: Message, state: FSMContext, services: BotContext) -> None:
    """Admin mode needs its own cancel.

    This router is registered before the employee one, but the only `/cancel` used to live
    there — so cancelling a half-finished admin wizard answered with the *employee* menu.
    """
    data = await state.get_data()
    await _finish(
        message,
        state,
        *_return_screen(
            services,
            data,
            viewer_id=message.from_user.id if message.from_user else 0,
            owner=_is_owner(services, message),
        ),
    )


@router.callback_query(F.data == "adm:cancel")
async def cancel_button(query: CallbackQuery, state: FSMContext, services: BotContext) -> None:
    """Back to where the flow started, not to the top.

    The prompt is the screen — `_ask` replaced it rather than stacking under it — so this
    edits in place and there is nothing left over. It used to edit a prompt that sat
    *below* the screen it came from, which left that screen above it, stale and live.
    """
    data = await state.get_data()
    await state.clear()
    await _replace(
        query,
        *_return_screen(
            services,
            data,
            viewer_id=query.from_user.id,
            owner=_is_owner(services, query),
        ),
    )


async def _ask(
    query: CallbackQuery,
    state: FSMContext,
    step: State,
    prompt: str,
    *,
    back_to: str,
    back_arg: str = "",
    **extra: Any,
) -> None:
    """Turn the current screen into a prompt, remembering how to get back.

    The prompt *replaces* the screen rather than stacking under it. Two messages meant two
    live keyboards, and ✖️ Отмена then edited the wrong one — the button is on the prompt,
    so the screen the flow was launched from stayed above it, stale and still clickable.

    `cancel_keyboard()` is attached here rather than at each call site, so a prompt with no
    way out stops being something anybody can forget. The data is *replaced*, not merged,
    so one flow cannot inherit a stale `back_arg` from the last one.
    """
    await state.set_state(step)
    screen = await _replace(query, prompt, cancel_keyboard())
    await state.set_data({"back_to": back_to, "back_arg": back_arg, "screen": screen, **extra})


async def _step(message: Message, state: FSMContext, text: str, markup: Any = None) -> None:
    """The next prompt, or a rejected answer, moved down below what was just typed."""
    data = await state.get_data()
    screen = await rehome(message, data.get("screen"), text, markup)
    await state.update_data(screen=screen)


async def _finish(message: Message, state: FSMContext, text: str, markup: Any = None) -> None:
    """The last screen of a flow. Moves it down, then forgets the flow.

    Clearing the remembered id afterwards is safe: whatever the user does next is a button
    on the screen this just drew, and a callback redraws in place without needing it.
    """
    data = await state.get_data()
    await rehome(message, data.get("screen"), text, markup)
    await state.clear()


def _return_screen(
    services: BotContext, data: dict[str, Any], *, viewer_id: int, owner: bool
) -> tuple[str, Any]:
    """The screen a cancelled or finished flow goes back to.

    A lookup over the screen builders, which already take explicit arguments — not a
    handler calling a handler, which is the thing that breaks. `back_to` names the builder
    and `back_arg` is what it takes.

    Anything that has been deleted since the flow started falls back to the menu. Firing
    somebody and then cancelling out of their card is an ordinary sequence, not an error.
    """
    tag = str(data.get("back_to", "menu"))
    arg = str(data.get("back_arg", ""))
    home = ("Режим администратора:", main_menu(owner=owner))

    if tag in _OFFICE_SCOPED and services.offices.get_office(arg) is None:
        return home

    if tag == "offices":
        return offices_screen(services, owner=owner)
    if tag == "admins":
        return admins_screen(services, viewer_id=viewer_id)
    if tag == "limits":
        return limits_screen(services)
    if tag == "times":
        return times_screen(services)
    if tag == "roster":
        return roster_screen(services, arg)
    if tag == "desks":
        return desks_screen(services, arg)
    if tag == "card":
        return card_screen(services, arg) or home
    if tag == "office":
        return office_screen(services, arg) or home
    if tag == "settings":
        return office_settings_screen(services, arg, owner=owner) or home
    if tag == "chat":
        return chat_screen(services, arg) or home
    return home


#: Destinations whose builder reads a planning context, which raises for a missing office.
_OFFICE_SCOPED = frozenset({"roster", "desks", "office", "settings", "chat"})


def _is_owner(services: BotContext, event: Message | CallbackQuery) -> bool:
    """Whether the person who sent this may see the owner-only rows."""
    return services.is_owner(event.from_user.id if event.from_user else None)


async def _refused_a_command(message: Message, state: FSMContext) -> bool:
    """True when the message was a command, and the caller should stop.

    Free text is how a name, a handle and a number all arrive, so "/" is the one thing
    that can never be meant as a value. Without this, any command typed mid-flow is
    written as data — and a username pattern or an integer parser rejecting it elsewhere
    is luck, not a design.

    The refusal moves the prompt down rather than appending a new one, or mashing `/`
    would leave a column of live cancel keyboards.
    """
    if not (message.text or "").lstrip().startswith("/"):
        return False
    await _step(message, state, COMMAND_IN_FLOW, cancel_keyboard())
    return True


router.message.middleware(AdminOnly())
router.callback_query.middleware(AdminOnly())

# Private chats only, on top of the identity gate. Every admin screen names people —
# rosters, usernames, the workbook — and `_replace` edits in whatever chat the button
# lives in, so `/admin` typed in an unrelated group would publish all of it there.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


@router.message(Command("admin"))
async def admin_menu(message: Message, services: BotContext) -> None:
    await message.answer(
        "Режим администратора:", reply_markup=main_menu(owner=_is_owner(services, message))
    )


@router.callback_query(F.data == "adm:menu")
async def back(query: CallbackQuery, services: BotContext) -> None:
    await _replace(query, "Режим администратора:", main_menu(owner=_is_owner(services, query)))


@router.callback_query(F.data == "adm:offices")
async def offices(query: CallbackQuery, services: BotContext) -> None:
    await _replace(query, *offices_screen(services, owner=_is_owner(services, query)))


@router.callback_query(F.data.startswith("adm:office:"))
async def office(query: CallbackQuery, services: BotContext) -> None:
    screen = office_screen(services, _tail(query))
    if screen is None:
        await query.answer("Офис не найден", show_alert=True)
        return
    await _replace(query, *screen)


@router.callback_query(F.data.startswith("adm:emp:"))
async def employees(query: CallbackQuery, services: BotContext) -> None:
    await _show_roster(query, services, _tail(query))


async def _show_roster(query: CallbackQuery, services: BotContext, office_id: str) -> None:
    await _replace(query, *roster_screen(services, office_id))


@router.callback_query(F.data.startswith("adm:empadd:"))
async def start_adding_employee(query: CallbackQuery, state: FSMContext) -> None:
    office_id = _tail(query)
    await _ask(
        query,
        state,
        AddEmployee.waiting_for_name,
        "Отправьте имя и фамилию нового сотрудника — как они должны выглядеть в напоминаниях.",
        back_to="roster",
        back_arg=office_id,
        office_id=office_id,
    )


@router.message(AddEmployee.waiting_for_name)
async def receive_new_name(message: Message, state: FSMContext) -> None:
    if await _refused_a_command(message, state):
        return
    await state.update_data(full_name=message.text or "")
    await state.set_state(AddEmployee.waiting_for_username)
    await _step(
        message,
        state,
        f"Отправьте Telegram-ник без «@». Если его нет, отправьте «{SKIP}» — "
        "человек сможет привязать себя сам через /start.",
        cancel_keyboard(),
    )


@router.message(AddEmployee.waiting_for_username)
async def receive_new_username(message: Message, state: FSMContext) -> None:
    if await _refused_a_command(message, state):
        return
    raw = (message.text or "").strip()
    await state.update_data(username=None if raw in {SKIP, ""} else raw)
    await state.set_state(AddEmployee.waiting_for_gender)
    # Asked for, not guessed: the reminder writes a gendered epithet in front of the name,
    # and a name alone is not evidence of anything.
    await _step(message, state, "Пол — от него зависит род в напоминаниях:", gender_picker())


@router.callback_query(AddEmployee.waiting_for_gender, F.data.startswith("adm:empg:"))
async def finish_adding_employee(
    query: CallbackQuery, state: FSMContext, services: BotContext
) -> None:
    data = await state.get_data()
    await state.clear()
    await query.answer()
    if not isinstance(query.message, Message):
        return

    office_id = str(data.get("office_id", ""))
    try:
        change = manage_roster.add_employee(
            office_id=office_id,
            full_name=str(data.get("full_name", "")),
            username=data.get("username"),
            gender=_tail(query),
            actor_id=query.from_user.id,
            offices=services.offices,
            roster=services.roster,
            schedule=services.schedule,
            ledger=services.ledger,
            clock=services.clock,
            policy=services.schedule_policy,
            audit=services.audit,
        )
    except RosterError as error:
        text, markup = roster_screen(services, office_id)
        await _replace(query, f"❌ {html.escape(str(error))}\n\n{text}", markup)
        return

    text, markup = roster_screen(services, office_id)
    await _replace(
        query,
        f"✅ {html.escape(change.full_name)} добавлен(а), id "
        f"<code>{html.escape(change.employee_id)}</code>. Расписание пересчитано.\n\n{text}",
        markup,
    )


@router.callback_query(F.data.startswith("adm:empv:"))
async def employee_card_view(query: CallbackQuery, services: BotContext) -> None:
    await _show_card(query, services, _tail(query))


async def _show_card(query: CallbackQuery, services: BotContext, employee_id: str) -> None:
    screen = card_screen(services, employee_id)
    if screen is None:
        await query.answer(NO_SUCH_EMPLOYEE, show_alert=True)
        return
    await _replace(query, *screen)


@router.callback_query(F.data.startswith("adm:empfire:"))
async def confirm_firing(query: CallbackQuery, services: BotContext) -> None:
    employee_id = _tail(query)
    found = _find(services, employee_id)
    if found is None:
        await query.answer("Сотрудник не найден", show_alert=True)
        return

    await _replace(
        query,
        f"Уволить <b>{html.escape(found[1].full_name)}</b>?\n\n"
        "Запись останется в базе: человек перестанет попадать в расписание с сегодняшнего "
        "дня, а его история и статистика сохранятся. Решение обратимо.",
        confirm(f"adm:empgo:{employee_id}", back=f"adm:empv:{employee_id}"),
    )


@router.callback_query(F.data.startswith("adm:empgo:"))
async def fire_employee(query: CallbackQuery, services: BotContext) -> None:
    employee_id = _tail(query)
    found = _find(services, employee_id)
    if found is None:
        await query.answer("Сотрудник не найден", show_alert=True)
        return

    try:
        change = manage_roster.end_tenure(
            office_id=found[0],
            employee_id=employee_id,
            actor_id=query.from_user.id,
            offices=services.offices,
            roster=services.roster,
            schedule=services.schedule,
            ledger=services.ledger,
            clock=services.clock,
            policy=services.schedule_policy,
            audit=services.audit,
        )
    except RosterError as error:
        await query.answer(str(error), show_alert=True)
        return

    await query.answer(f"Освобождено дней: {len(change.released)}")
    await _show_card(query, services, employee_id)


@router.callback_query(F.data.startswith("adm:emprest:"))
async def restore_employee(query: CallbackQuery, services: BotContext) -> None:
    employee_id = _tail(query)
    found = _find(services, employee_id)
    if found is None:
        await query.answer("Сотрудник не найден", show_alert=True)
        return

    try:
        manage_roster.restore(
            office_id=found[0],
            employee_id=employee_id,
            actor_id=query.from_user.id,
            offices=services.offices,
            roster=services.roster,
            schedule=services.schedule,
            ledger=services.ledger,
            clock=services.clock,
            policy=services.schedule_policy,
            audit=services.audit,
        )
    except RosterError as error:
        await query.answer(str(error), show_alert=True)
        return

    await query.answer("Восстановлен")
    await _show_card(query, services, employee_id)


@router.callback_query(F.data.startswith("adm:empname:"))
async def start_rename(query: CallbackQuery, state: FSMContext) -> None:
    employee_id = _tail(query)
    await _ask(
        query,
        state,
        EditEmployee.waiting_for_name,
        "Отправьте новое имя и фамилию.",
        back_to="card",
        back_arg=employee_id,
        employee_id=employee_id,
    )


@router.message(EditEmployee.waiting_for_name)
async def apply_rename(message: Message, state: FSMContext, services: BotContext) -> None:
    if await _refused_a_command(message, state):
        return
    data = await state.get_data()
    employee_id = str(data.get("employee_id", ""))
    found = _find(services, employee_id)
    if found is None:
        await _finish(
            message, state, NO_SUCH_EMPLOYEE, main_menu(owner=_is_owner(services, message))
        )
        return

    try:
        manage_roster.rename(
            office_id=found[0],
            employee_id=employee_id,
            full_name=message.text or "",
            actor_id=message.from_user.id if message.from_user else None,
            offices=services.offices,
            roster=services.roster,
            audit=services.audit,
        )
    except RosterError as error:
        # Stays in the state: a rejected name is worth retyping, not restarting.
        await _step(message, state, f"❌ {html.escape(str(error))}", cancel_keyboard())
        return

    await _finish(message, state, *_card_or_menu(services, employee_id, message))


@router.callback_query(F.data.startswith("adm:empuser:"))
async def start_setting_username(query: CallbackQuery, state: FSMContext) -> None:
    employee_id = _tail(query)
    await _ask(
        query,
        state,
        EditEmployee.waiting_for_username,
        f"Отправьте Telegram-ник без «@», или «{SKIP}», чтобы очистить.",
        back_to="card",
        back_arg=employee_id,
        employee_id=employee_id,
    )


@router.message(EditEmployee.waiting_for_username)
async def apply_username(message: Message, state: FSMContext, services: BotContext) -> None:
    if await _refused_a_command(message, state):
        return
    data = await state.get_data()
    employee_id = str(data.get("employee_id", ""))
    found = _find(services, employee_id)
    if found is None:
        await _finish(
            message, state, NO_SUCH_EMPLOYEE, main_menu(owner=_is_owner(services, message))
        )
        return

    raw = (message.text or "").strip()
    try:
        manage_roster.set_username(
            office_id=found[0],
            employee_id=employee_id,
            username=None if raw == SKIP else raw,
            actor_id=message.from_user.id if message.from_user else None,
            offices=services.offices,
            roster=services.roster,
            audit=services.audit,
        )
    except RosterError as error:
        # Stays in the state, like the rename: a rejected handle is worth retyping.
        await _step(message, state, f"❌ {html.escape(str(error))}", cancel_keyboard())
        return

    await _finish(message, state, *_card_or_menu(services, employee_id, message))


async def _follow(query: CallbackQuery, services: BotContext, office_id: str) -> None:
    """Move the office screen down beneath content that was just sent.

    A workbook, a rendered fortnight or a test reminder is content, and content is a new
    message — but the screen that produced it is then stranded above, and you have to
    scroll back up to do anything else. This brings it down to the bottom instead.
    """
    screen = office_screen(services, office_id)
    if screen is not None and isinstance(query.message, Message):
        await rehome(query.message, query.message.message_id, *screen)


def _settings_or_menu(
    services: BotContext, office_id: str, event: Message | CallbackQuery
) -> tuple[str, Any]:
    """An office's settings, or the menu if the office has gone while the flow was open.

    The owner flag comes from whoever is looking: renaming an office is open to every
    admin, and hardcoding it would hand a non-owner the close and delete buttons.
    """
    owner = _is_owner(services, event)
    return office_settings_screen(services, office_id, owner=owner) or _home(owner)


def _card_or_menu(
    services: BotContext, employee_id: str, event: Message | CallbackQuery
) -> tuple[str, Any]:
    """Their card, or the admin menu if the record has gone while the flow was open."""
    return card_screen(services, employee_id) or _home(_is_owner(services, event))


def _home(owner: bool) -> tuple[str, Any]:
    return ("Режим администратора:", main_menu(owner=owner))


def _find(services: BotContext, employee_id: str) -> tuple[str, Employee] | None:
    """The employee and the office they belong to.

    Two indexed lookups. This used to scan every active office's roster, which also made
    a closed office's people unreachable — fine by accident when no office could be
    closed, wrong now that one can be: an admin fixing a name on a closed office is a
    perfectly ordinary thing to want.
    """
    employee = services.offices.get_employee(employee_id)
    if employee is None:
        return None
    office_id = services.offices.office_of(employee_id)
    return (office_id, employee) if office_id is not None else None


@router.callback_query(F.data.startswith("adm:desks:"))
async def desks(query: CallbackQuery, services: BotContext) -> None:
    await _replace(query, *desks_screen(services, _tail(query)))


@router.callback_query(F.data.startswith("adm:desk:"))
async def start_setting_desks(
    query: CallbackQuery, state: FSMContext, services: BotContext
) -> None:
    """Ask for the number instead of cycling to it.

    This used to increment on each tap and wrap after nine, which is eleven taps to go
    from 9 to 8 and no way at all to reach a number above nine.
    """
    _, _, office_id, weekday_raw = str(query.data).split(":")
    weekday = int(weekday_raw)

    current = _context(services, office_id).template.desks_on(weekday)
    name = services.voice.catalog.common.weekdays[weekday]
    await _ask(
        query,
        state,
        SetDesks.waiting_for_count,
        f"Сколько свободных мест в {name}? Сейчас {current}.\nОтправьте число от 0 до {MAX_DESKS}.",
        back_to="desks",
        back_arg=office_id,
        office_id=office_id,
        weekday=weekday,
    )


@router.message(SetDesks.waiting_for_count)
async def apply_desks(message: Message, state: FSMContext, services: BotContext) -> None:
    if await _refused_a_command(message, state):
        return
    data = await state.get_data()
    office_id = str(data.get("office_id", ""))
    weekday = int(data.get("weekday", 0))

    count = _parse_count(message.text or "")
    if count is None:
        # Stays in the state: a mistyped number is worth retyping, not restarting.
        await _step(message, state, f"Нужно целое число от 0 до {MAX_DESKS}.", cancel_keyboard())
        return

    services.roster.set_vacant_desks(office_id, weekday, count)
    services.audit.record(
        actor_id=message.from_user.id if message.from_user else None,
        action="template.desks",
        payload={"office": office_id, "weekday": weekday, "desks": count},
    )

    headcount = sum(
        1
        for employee in services.offices.employees(office_id)
        if employee.in_tenure(services.clock.today())
    )
    name = services.voice.catalog.common.weekdays[weekday]
    reply = f"✅ {name.capitalize()}: свободных мест {count}."
    if count > headcount:
        # Not refused — an office may be planning to hire — but a silent permanent
        # shortfall is worth one sentence.
        reply += f"\nВ офисе всего {headcount} чел., так что места останутся пустыми."

    text, markup = desks_screen(services, office_id)
    await _finish(message, state, f"{reply}\n\n{text}", markup)


@router.callback_query(F.data.startswith("adm:fix:"))
async def fixed(query: CallbackQuery, services: BotContext) -> None:
    await _replace(query, *fixed_screen(services, _tail(query)))


@router.callback_query(F.data.startswith("adm:fixday:"))
async def fixed_day(query: CallbackQuery, services: BotContext) -> None:
    _, _, office_id, weekday_raw = str(query.data).split(":")
    await _replace(query, *fixed_day_screen(services, office_id, int(weekday_raw)))


@router.callback_query(F.data.startswith("adm:fixtog:"))
async def toggle_fixed(query: CallbackQuery, services: BotContext) -> None:
    """The office is derived from the person, not carried alongside them.

    Carrying both cost `14 + len(office) + len(employee)` bytes of a 64-byte budget, so a
    40-character employee id and a 16-character office id already overflowed it — which
    it survived only because the real slugs are short. Offices are about to be named by
    whoever creates them, so the id stops riding here.
    """
    _, _, weekday_raw, employee_id = str(query.data).split(":", 3)
    weekday = int(weekday_raw)

    office_id = services.offices.office_of(employee_id)
    if office_id is None:
        await query.answer("Сотрудник не найден", show_alert=True)
        return

    now_fixed = services.roster.toggle_fixed(office_id, weekday, employee_id)
    services.audit.record(
        actor_id=query.from_user.id,
        action="template.fixed",
        payload={
            "office": office_id,
            "weekday": weekday,
            "employee": employee_id,
            "fixed": now_fixed,
        },
    )
    # Redrawn from the values just parsed, never by calling the sibling handler: that
    # handler would re-parse `query.data`, which now names a *toggle* and not a day.
    await _replace(query, *fixed_day_screen(services, office_id, weekday))


@router.callback_query(F.data.startswith("adm:regen:"))
async def regenerate_office(query: CallbackQuery, services: BotContext) -> None:
    office_id = _tail(query)
    await query.answer("Считаю…")

    result = regenerate(
        office_id=office_id,
        offices=services.offices,
        schedule=services.schedule,
        ledger=services.ledger,
        clock=services.clock,
        policy=services.schedule_policy,
        triggered_by="manual",
    )
    services.audit.record(
        actor_id=query.from_user.id, action="schedule.regenerate", payload={"office": office_id}
    )

    summary = (
        f"Добавлено: {len(result.diff.added)}, снято: {len(result.diff.removed)}, "
        f"без изменений: {result.diff.kept}."
    )
    if result.shortfall:
        summary += f"\n⚠️ Не заполнено мест: {result.shortfall}."

    if isinstance(query.message, Message):
        await query.message.answer(summary)
        await _send_workbook(query.message, services, office_id, result)
        await _follow(query, services, office_id)


@router.callback_query(F.data.startswith("adm:reseed:"))
async def reseed(query: CallbackQuery, services: BotContext) -> None:
    office_id = _tail(query)
    seed = services.roster.bump_seed(office_id)
    services.audit.record(
        actor_id=query.from_user.id,
        action="template.reseed",
        payload={"office": office_id, "seed": seed},
    )

    screen = office_screen(services, office_id)
    if screen is None:
        await query.answer("Офис не найден", show_alert=True)
        return
    await _replace(query, *screen)


@router.callback_query(F.data.startswith("adm:preview:"))
async def preview(query: CallbackQuery, services: BotContext) -> None:
    """Renders next week without touching the office chat."""
    office_id = _tail(query)
    await query.answer()

    today = services.clock.today()
    snapshots = services.schedule.days_between(office_id, today, today + timedelta(days=13))
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
    if isinstance(query.message, Message):
        for chunk in chunks or ["Ничего не запланировано."]:
            await query.message.answer(chunk)
        await _follow(query, services, office_id)


@router.callback_query(F.data.startswith("adm:test:"))
async def test_reminder(query: CallbackQuery, services: BotContext) -> None:
    """Sends the real reminder to the admin only, so it can be checked before a group
    sees it."""
    office_id = _tail(query)
    await query.answer()

    outcome = await send_attendance_reminder(
        office_id=office_id,
        offices=services.offices,
        schedule=services.schedule,
        notifier=services.notifier,  # type: ignore[arg-type]
        voice=services.voice,
        clock=services.clock,
        silent_policy=services.silent_policy,
        chat_id=query.from_user.id,
        dry_run=True,
    )
    if isinstance(query.message, Message):
        await query.message.answer(outcome.text or f"Нечего отправлять ({outcome.skipped}).")
        await _follow(query, services, office_id)


@router.callback_query(F.data == "adm:fair")
async def fairness(query: CallbackQuery, services: BotContext) -> None:
    """Separates an unfair template from an unfair algorithm."""
    blocks: list[str] = []

    for office_item in services.offices.active_offices():
        entries = services.ledger.entries(office_item.id)
        if not entries:
            continue
        names = {e.id: e.full_name for e in services.offices.employees(office_item.id)}
        ordered = sorted(entries.values(), key=lambda entry: -entry.surplus_scaled)
        rows = "\n".join(
            f"  {html.escape(names.get(entry.employee_id, entry.employee_id))}: "
            f"{entry.surplus_scaled / SCALE:+.1f}"
            for entry in ordered
        )
        spread = (ordered[0].surplus_scaled - ordered[-1].surplus_scaled) / SCALE
        blocks.append(f"<b>{html.escape(office_item.name)}</b> — разброс {spread:.1f} дн.\n{rows}")

    await _replace(
        query,
        "\n\n".join(blocks) or "Пока нет данных.",
        keyboard((_button("‹ Назад", "adm:menu"),)),
    )


@router.callback_query(F.data == "adm:mood")
async def mood_menu(query: CallbackQuery, services: BotContext) -> None:
    today = services.clock.today()
    lines = [
        f"• {html.escape(item.name)}: "
        f"<b>{services.voice.mood_for(office_id=item.id, day=today).value}</b>"
        for item in services.offices.active_offices()
    ]
    safe = "включён" if services.voice.moods.safe_mode else "выключен"
    body = "\n".join(lines) + f"\n\nБезопасный режим: {safe}."
    await _replace(
        query,
        f"<b>Настроение на сегодня</b>\n{body}",
        keyboard((_button("‹ Назад", "adm:menu"),)),
    )


@router.callback_query(F.data == "adm:status")
async def status(query: CallbackQuery, services: BotContext) -> None:
    recent = services.jobs.recent(limit=8)
    lines = [
        f"• {job} — {moment:%d.%m %H:%M} — {state}" for job, _key, moment, state in recent
    ] or ["Пока ничего не запускалось."]

    size_mb = services.maintenance.database_bytes() / (1024 * 1024)
    today = services.clock.today()
    # Real numbers at last: the usage table has always had a `tokens` column, but the
    # OpenAI client threw the API's own count away, so it read zero forever.
    calls = services.usage.total_today(today)
    tokens = services.usage.tokens_today(today)

    await _replace(
        query,
        "<b>Последние запуски</b>\n"
        + "\n".join(lines)
        + f"\n\nБаза: {size_mb:.1f} МБ"
        + f"\nИИ сегодня: {calls} запрос(ов), {tokens} токен(ов)",
        keyboard((_button("‹ Назад", "adm:menu"),)),
    )


@router.callback_query(F.data == "adm:config")
async def check_config(query: CallbackQuery, services: BotContext) -> None:
    """Validates the files on disk without applying them.

    The office count comes from the database, which is where offices live. It used to
    come from the files, and would now read zero forever.
    """
    from tabelshchik.config.loader import ConfigError, load
    from tabelshchik.config.messages import MessagesConfig

    try:
        load(services.config_dir)
        parse_file(services.config_dir / "messages.yaml", MessagesConfig)
    except ConfigError as error:
        await _replace(
            query,
            f"❌ <pre>{html.escape(str(error))}</pre>",
            keyboard((_button("‹ Назад", "adm:menu"),)),
        )
        return

    offices = services.offices.all_offices()
    active = sum(1 for office in offices if office.active)
    await _replace(
        query,
        f"✅ Конфигурация корректна.\nОфисов в базе: {len(offices)} (активных {active})",
        keyboard((_button("‹ Назад", "adm:menu"),)),
    )


@router.callback_query(F.data == "adm:backup")
async def backup(query: CallbackQuery, services: BotContext) -> None:
    await query.answer("Готовлю копию…")
    if not isinstance(query.message, Message) or services.notifier is None:
        return

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "tabelshchik.db"
        services.maintenance.backup_to(str(destination))
        content = destination.read_bytes()

    stamp = services.clock.today().isoformat()
    await services.notifier.send_document(
        query.from_user.id,
        f"tabelshchik-{stamp}.db",
        content,
        caption=f"Резервная копия на {stamp}.",
        silent=True,
    )

    # The document lands below the menu, which otherwise stays exactly where it was and
    # makes it look as though the button did nothing.
    await rehome(
        query.message,
        query.message.message_id,
        "Режим администратора:",
        main_menu(owner=_is_owner(services, query)),
    )


async def _send_workbook(
    message: Message, services: BotContext, office_id: str, result: Any
) -> None:
    if services.notifier is None:
        return

    context = services.offices.planning_context(
        office_id, start=result.horizon_start, end=result.horizon_end
    )
    days = [
        ReportDay(
            day=plan.day,
            weekday=plan.weekday,
            attendees=result.roster_on(plan.day),
            desks_required=plan.desks_total,
        )
        for plan in result.instance.days
        if plan.desks_total
    ]
    report = build_report(
        office_name=context.office.name,
        start=result.horizon_start,
        end=result.horizon_end,
        employees=context.employees,
        days=days,
        spec=context.spec,
        ledger=services.ledger.entries(office_id),
        seed=result.instance.seed,
    )

    caption = schedule_caption(
        services.voice.catalog,
        report,
        office_name=context.office.name,
        horizon_start=result.horizon_start,
        horizon_end=result.horizon_end,
        # Tomorrow and the six days after it. The horizon runs six weeks; nobody reads a
        # caption that long, and the workbook has the rest.
        week_from=services.clock.today() + timedelta(days=1),
        shortfall=result.shortfall,
    )
    await services.notifier.send_document(
        message.chat.id,
        f"{office_id}-{result.horizon_start.isoformat()}.xlsx",
        build_workbook(report, services.voice.catalog.common),
        caption=caption,
    )


# ---------------------------------------------------------------------- office settings


@router.callback_query(F.data.startswith("adm:oset:"))
async def office_settings(query: CallbackQuery, services: BotContext) -> None:
    screen = office_settings_screen(services, _tail(query), owner=_is_owner(services, query))
    if screen is None:
        await query.answer(NO_SUCH_OFFICE, show_alert=True)
        return
    await _replace(query, *screen)


@router.callback_query(F.data == "adm:onew")
async def start_creating_office(
    query: CallbackQuery, services: BotContext, state: FSMContext
) -> None:
    if not _is_owner(services, query):
        await query.answer(NOT_THE_OWNER, show_alert=True)
        return
    await _ask(
        query,
        state,
        CreateOffice.waiting_for_name,
        "Отправьте название офиса — так, как его должны видеть сотрудники.",
        back_to="offices",
    )


@router.message(CreateOffice.waiting_for_name)
async def apply_new_office(message: Message, state: FSMContext, services: BotContext) -> None:
    if await _refused_a_command(message, state):
        return
    await state.clear()
    actor_id = message.from_user.id if message.from_user else 0
    try:
        change = manage_offices.create_office(
            name=message.text or "",
            timezone=services.app_timezone,
            actor_id=actor_id,
            offices=services.offices,
            office_admin=services.office_admin,
            admins=services.admins,
            audit=services.audit,
        )
    except OfficeError as error:
        await _step(message, state, str(error), cancel_keyboard())
        return

    await _finish(
        message,
        state,
        f"Офис «{html.escape(change.name)}» создан, id <code>{change.office_id}</code>.\n\n"
        "Дальше: добавьте сотрудников, задайте свободные места, и отправьте /bind "
        "в группе офиса, чтобы бот знал, куда писать.",
        office_menu(change.office_id),
    )


@router.callback_query(F.data.startswith("adm:orename:"))
async def start_renaming_office(query: CallbackQuery, state: FSMContext) -> None:
    office_id = _tail(query)
    await _ask(
        query,
        state,
        EditOffice.waiting_for_name,
        "Отправьте новое название офиса.",
        back_to="settings",
        back_arg=office_id,
        office_id=office_id,
    )


@router.message(EditOffice.waiting_for_name)
async def apply_office_rename(message: Message, state: FSMContext, services: BotContext) -> None:
    if await _refused_a_command(message, state):
        return
    data = await state.get_data()
    office_id = str(data.get("office_id", ""))
    try:
        manage_offices.rename_office(
            office_id=office_id,
            name=message.text or "",
            actor_id=message.from_user.id if message.from_user else 0,
            offices=services.offices,
            office_admin=services.office_admin,
            audit=services.audit,
        )
    except OfficeError as error:
        # A rejected name is worth retyping, not restarting.
        await _step(message, state, str(error), cancel_keyboard())
        return

    await _finish(message, state, *_settings_or_menu(services, office_id, message))


@router.callback_query(F.data.startswith("adm:ochat:"))
async def office_chat(query: CallbackQuery, services: BotContext) -> None:
    screen = chat_screen(services, _tail(query))
    if screen is None:
        await query.answer(NO_SUCH_OFFICE, show_alert=True)
        return
    await _replace(query, *screen)


@router.callback_query(F.data.startswith("adm:ochatid:"))
async def start_typing_chat_id(query: CallbackQuery, state: FSMContext) -> None:
    office_id = _tail(query)
    await _ask(
        query,
        state,
        EditOffice.waiting_for_chat_id,
        "Отправьте id группы — отрицательное число, обычно начинается с −100. "
        "Проще отправить /bind прямо в этой группе.",
        back_to="chat",
        back_arg=office_id,
        office_id=office_id,
    )


@router.message(EditOffice.waiting_for_chat_id)
async def apply_chat_id(message: Message, state: FSMContext, services: BotContext) -> None:
    if await _refused_a_command(message, state):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdecimal():
        await _step(message, state, "Нужно число. Попробуйте ещё раз.", cancel_keyboard())
        return

    data = await state.get_data()
    office_id = str(data.get("office_id", ""))
    try:
        manage_offices.bind_chat(
            office_id=office_id,
            chat_id=int(raw),
            actor_id=message.from_user.id if message.from_user else 0,
            offices=services.offices,
            roster=services.roster,
            audit=services.audit,
        )
    except OfficeError as error:
        await _step(message, state, str(error), cancel_keyboard())
        return

    screen = chat_screen(services, office_id)
    await _finish(message, state, *(screen or _home(_is_owner(services, message))))


@router.callback_query(F.data.startswith("adm:ochatno:"))
async def unbind_chat(query: CallbackQuery, services: BotContext) -> None:
    office_id = _tail(query)
    try:
        manage_offices.bind_chat(
            office_id=office_id,
            chat_id=None,
            actor_id=query.from_user.id,
            offices=services.offices,
            roster=services.roster,
            audit=services.audit,
        )
    except OfficeError as error:
        await query.answer(str(error), show_alert=True)
        return
    screen = chat_screen(services, office_id)
    if screen is None:
        # `query.answer()` lives inside `_replace`; skipping it leaves the client
        # spinning on the button forever.
        await query.answer(NO_SUCH_OFFICE, show_alert=True)
        return
    await _replace(query, *screen)


@router.callback_query(F.data.startswith("adm:ocal:"))
async def office_calendar(query: CallbackQuery, services: BotContext) -> None:
    screen = calendar_screen(services, _tail(query))
    if screen is None:
        await query.answer(NO_SUCH_OFFICE, show_alert=True)
        return
    await _replace(query, *screen)


@router.callback_query(F.data.startswith("adm:ocalpick:"))
async def pick_calendar(query: CallbackQuery, services: BotContext) -> None:
    _, _, office_id, code = str(query.data).split(":", 3)
    try:
        manage_offices.set_holiday_calendar(
            office_id=office_id,
            code=code,
            actor_id=query.from_user.id,
            offices=services.offices,
            office_admin=services.office_admin,
            audit=services.audit,
        )
    except OfficeError as error:
        await query.answer(str(error), show_alert=True)
        return
    screen = calendar_screen(services, office_id)
    if screen is None:
        # `query.answer()` lives inside `_replace`; skipping it leaves the client
        # spinning on the button forever.
        await query.answer(NO_SUCH_OFFICE, show_alert=True)
        return
    await _replace(query, *screen)


@router.callback_query(F.data.startswith("adm:oclose:"))
async def confirm_closing(query: CallbackQuery, services: BotContext) -> None:
    if not _is_owner(services, query):
        await query.answer(NOT_THE_OWNER, show_alert=True)
        return
    office_id = _tail(query)
    await _replace(
        query,
        "Закрыть офис? Он перестанет попадать в расписание и напоминания, "
        "но все сотрудники и вся история останутся на месте — открыть обратно "
        "можно одной кнопкой.",
        confirm(f"adm:oclosego:{office_id}", back=f"adm:oset:{office_id}"),
    )


@router.callback_query(F.data.startswith("adm:oclosego:"))
async def close_office(query: CallbackQuery, services: BotContext) -> None:
    await _set_open(query, services, _tail(query), open_it=False)


@router.callback_query(F.data.startswith("adm:oopen:"))
async def reopen_office(query: CallbackQuery, services: BotContext) -> None:
    await _set_open(query, services, _tail(query), open_it=True)


async def _set_open(
    query: CallbackQuery, services: BotContext, office_id: str, *, open_it: bool
) -> None:
    action = manage_offices.reopen_office if open_it else manage_offices.close_office
    try:
        action(
            office_id=office_id,
            actor_id=query.from_user.id,
            offices=services.offices,
            office_admin=services.office_admin,
            admins=services.admins,
            audit=services.audit,
        )
    except OfficeError as error:
        await query.answer(str(error), show_alert=True)
        return

    if services.office_jobs is not None:
        # Per-office jobs are registered at boot, so without this a reopened office stays
        # silent until the next deploy. The handlers also check `active`, so the two
        # halves agree even if one of them is skipped.
        if open_it:
            services.office_jobs.add_office(office_id)
        else:
            services.office_jobs.drop_office(office_id)

    screen = office_settings_screen(services, office_id, owner=_is_owner(services, query))
    if screen is None:
        # `query.answer()` lives inside `_replace`; skipping it leaves the client
        # spinning on the button forever.
        await query.answer(NO_SUCH_OFFICE, show_alert=True)
        return
    await _replace(query, *screen)


@router.callback_query(F.data.startswith("adm:odrop:"))
async def start_deleting_office(
    query: CallbackQuery, services: BotContext, state: FSMContext
) -> None:
    if not _is_owner(services, query):
        await query.answer(NOT_THE_OWNER, show_alert=True)
        return
    office_id = _tail(query)
    office = services.offices.get_office(office_id)
    if office is None:
        await query.answer(NO_SUCH_OFFICE, show_alert=True)
        return

    await _ask(
        query,
        state,
        EditOffice.waiting_for_deletion,
        "⚠️ Это удалит офис вместе со всеми сотрудниками, назначениями и историей "
        "справедливости. Отменить будет нельзя.\n\n"
        f"Отправьте название офиса, чтобы подтвердить: <b>{html.escape(office.name)}</b>",
        back_to="settings",
        back_arg=office_id,
        office_id=office_id,
    )


@router.message(EditOffice.waiting_for_deletion)
async def apply_deletion(message: Message, state: FSMContext, services: BotContext) -> None:
    if await _refused_a_command(message, state):
        return
    data = await state.get_data()
    office_id = str(data.get("office_id", ""))
    try:
        change = manage_offices.delete_office(
            office_id=office_id,
            confirmation=message.text or "",
            actor_id=message.from_user.id if message.from_user else 0,
            offices=services.offices,
            office_admin=services.office_admin,
            admins=services.admins,
            audit=services.audit,
        )
    except OfficeError as error:
        await _step(message, state, str(error), cancel_keyboard())
        return

    if services.office_jobs is not None:
        services.office_jobs.drop_office(office_id)

    tail = ""
    if not services.offices.active_offices():
        # Worth saying: with no open office the audience gate has nothing to grant, so
        # the bot answers nobody at all — admins included — until one exists again.
        tail = "\n\nОткрытых офисов не осталось, так что бот пока никому не отвечает."
    text, markup = offices_screen(services, owner=_is_owner(services, message))
    await _finish(
        message, state, f"Офис «{html.escape(change.name)}» удалён.{tail}\n\n{text}", markup
    )


# ------------------------------------------------------------------------- AI limits
#
# The allowance has always been per person; what is new is being able to move it. An
# override is stored rather than a copy of the default, so raising the default lifts
# everybody who was never singled out.


@router.callback_query(F.data == "adm:limits")
async def limits(query: CallbackQuery, services: BotContext) -> None:
    await _replace(query, *limits_screen(services))


@router.callback_query(F.data == "adm:limdef")
async def start_setting_default(query: CallbackQuery, state: FSMContext) -> None:
    await _ask(
        query,
        state,
        SetLimit.waiting_for_value,
        f"Сколько запросов к ИИ в день на человека? Отправьте число от 0 до "
        f"{MAX_DAILY_LIMIT}, или «{SKIP}», чтобы вернуть значение из конфигурации.",
        back_to="limits",
        target="default",
    )


@router.callback_query(F.data == "adm:limcap")
async def start_setting_cap(query: CallbackQuery, state: FSMContext) -> None:
    await _ask(
        query,
        state,
        SetLimit.waiting_for_value,
        "Общий предел на все офисы за день — страховка от неожиданного счёта. "
        f"Отправьте число от 0 до {MAX_GLOBAL_LIMIT}, или «{SKIP}» для значения "
        "из конфигурации.",
        back_to="limits",
        target="cap",
    )


@router.callback_query(F.data.startswith("adm:limemp:"))
async def start_setting_employee_limit(query: CallbackQuery, state: FSMContext) -> None:
    employee_id = _tail(query)
    await _ask(
        query,
        state,
        SetLimit.waiting_for_value,
        f"Сколько запросов к ИИ в день у этого человека? Число от 0 до "
        f"{MAX_DAILY_LIMIT}, или «{SKIP}», чтобы вернуть общий лимит.",
        back_to="card",
        back_arg=employee_id,
        target=employee_id,
    )


@router.message(SetLimit.waiting_for_value)
async def apply_limit(message: Message, state: FSMContext, services: BotContext) -> None:
    if await _refused_a_command(message, state):
        return

    raw = (message.text or "").strip()
    # `SKIP` clears the override; 0 is a real answer meaning no replies at all.
    limit = None if raw == SKIP else _parse_limit(raw)
    if raw != SKIP and limit is None:
        await _step(message, state, "Нужно целое неотрицательное число.", cancel_keyboard())
        return

    data = await state.get_data()
    target = str(data.get("target", ""))
    actor_id = message.from_user.id if message.from_user else 0

    try:
        if target == "default":
            manage_limits.set_default_limit(
                limit=limit, actor_id=actor_id, settings=services.settings, audit=services.audit
            )
        elif target == "cap":
            manage_limits.set_global_limit(
                limit=limit, actor_id=actor_id, settings=services.settings, audit=services.audit
            )
        else:
            manage_limits.set_employee_limit(
                employee_id=target,
                limit=limit,
                actor_id=actor_id,
                offices=services.offices,
                roster=services.roster,
                audit=services.audit,
            )
    except LimitError as error:
        await _step(message, state, str(error), cancel_keyboard())
        return

    if target in {"default", "cap"}:
        await _finish(message, state, *limits_screen(services))
        return
    await _finish(message, state, *_card_or_menu(services, target, message))


def _parse_limit(raw: str) -> int | None:
    # `isdecimal`, not `int()`: a minus sign parses fine and would mean something absurd.
    return int(raw) if raw.isdecimal() else None


# ------------------------------------------------------------------- delivery times
#
# These were `app.yaml` keys, so moving a reminder by ten minutes was a deploy. Saving one
# here reschedules the running bot straight away.

#: What each `adm:time:` button edits, and what the prompt says about it.
_TIME_TARGETS: dict[str, tuple[ScheduleTime, str]] = {
    "attendance": (ScheduleTime.ATTENDANCE, "Во сколько напоминать, кто завтра в офисе?"),
    "tempo": (
        ScheduleTime.TEMPO,
        "Во сколько напоминать про Tempo в последний рабочий день недели и месяца? "
        f"Если до конца рабочего дня останется не больше {RUSH_WINDOW_MINUTES} минут, "
        "бот предложит потратить их на Tempo.",
    ),
    "end": (
        ScheduleTime.WORKDAY_END,
        "Во сколько заканчивается рабочий день? От этого времени Tempo отсчитывает "
        "оставшиеся минуты.",
    ),
    "holiday": (ScheduleTime.HOLIDAY, "Во сколько поздравлять с праздниками?"),
}


@router.callback_query(F.data == "adm:times")
async def times(query: CallbackQuery, services: BotContext) -> None:
    await _replace(query, *times_screen(services))


@router.callback_query(F.data.startswith("adm:time:"))
async def start_setting_time(query: CallbackQuery, state: FSMContext) -> None:
    target = _tail(query)
    if target == "extend":
        prompt = (
            "Когда продлевать расписание и рассылать обновлённую таблицу? Отправьте день и "
            "время, например «чт 10:00», или только время, чтобы оставить день."
        )
    elif target in _TIME_TARGETS:
        prompt = f"{_TIME_TARGETS[target][1]} Отправьте время в формате ЧЧ:ММ, например 17:50."
    else:
        await query.answer("Неизвестная настройка")
        return
    await _ask(
        query,
        state,
        SetTime.waiting_for_value,
        f"{prompt}\n\n«{SKIP}» — вернуть значение по умолчанию. "
        "Если новое время сегодня уже прошло, оно сработает со следующего раза.",
        back_to="times",
        target=target,
    )


@router.message(SetTime.waiting_for_value)
async def apply_time(message: Message, state: FSMContext, services: BotContext) -> None:
    if await _refused_a_command(message, state):
        return

    raw = (message.text or "").strip()
    data = await state.get_data()
    target = str(data.get("target", ""))
    actor_id = message.from_user.id if message.from_user else 0

    try:
        if target == "extend":
            if raw == SKIP:
                weekday, moment = None, None
            else:
                parsed = manage_times.parse_extend(
                    raw, services.voice.catalog.common.weekdays_short
                )
                if parsed is None:
                    await _step(message, state, "Нужно «чт 10:00» или «10:00».", cancel_keyboard())
                    return
                weekday, moment = parsed
            manage_times.set_extend(
                weekday=weekday,
                value=moment,
                actor_id=actor_id,
                settings=services.settings,
                jobs=services.office_jobs,
                audit=services.audit,
            )
        elif target in _TIME_TARGETS:
            value = None if raw == SKIP else manage_times.parse_time(raw)
            if raw != SKIP and value is None:
                await _step(message, state, "Нужно время в формате ЧЧ:ММ.", cancel_keyboard())
                return
            manage_times.set_time(
                which=_TIME_TARGETS[target][0],
                value=value,
                actor_id=actor_id,
                settings=services.settings,
                jobs=services.office_jobs,
                audit=services.audit,
            )
    except TimeError as error:
        await _step(message, state, str(error), cancel_keyboard())
        return

    await _finish(message, state, *times_screen(services))


# ------------------------------------------------------------------ sending a message
#
# The admin sends the message first and picks the group afterwards, so what goes out is a
# copy of a real message — media, album and formatting included. Two things set this flow
# apart. Its input can arrive in several parts at once, which is what the batching is for.
# And its output cannot be taken back, which is what the confirmation and the lock are for:
# every step that reads the flow's data, talks to Telegram and writes the data back holds
# it, so a double-tap on «Да» cannot read "not sent yet" twice.

_BURSTS: Bursts[Message] = Bursts(quiet=0.6)
_RELAY_LOCKS: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

RELAY_PROMPT = (
    "Пришлите сообщение, которое нужно отправить от имени бота: текст, фото, документ, "
    "альбом — что угодно. Оно уйдёт в точности как есть, без пометки «переслано».\n\n"
    "Куда отправить, спрошу следом."
)
RELAY_ALREADY = (
    "Что отправлять, уже выбрано — новое сообщение не добавлено. "
    "Чтобы отправить его, закончите с этим и начните заново."
)
RELAY_GONE = "Отправка уже завершена, или бот перезапускался. Начните заново."
NO_RELAY_TARGETS = "Ни у одного открытого офиса нет чата. Отправьте /bind в группе офиса."


def _relay_key(event: Message | CallbackQuery) -> int:
    """Whose relay this is. The router is private-only, so the person is also the chat."""
    return event.from_user.id if event.from_user else 0


@router.callback_query(F.data == "adm:relay")
async def start_relay(query: CallbackQuery, state: FSMContext, services: BotContext) -> None:
    if not relay.destinations(services.offices):
        # Before the prompt rather than after the message: asking for something that has
        # nowhere to go wastes whatever the admin sends next.
        await query.answer(NO_RELAY_TARGETS, show_alert=True)
        return
    await _ask(
        query,
        state,
        Relay.composing,
        RELAY_PROMPT,
        back_to="menu",
    )


@router.message(Relay.composing)
async def receive_relay(message: Message, state: FSMContext, services: BotContext) -> None:
    """The message to send — or its parts, gathered into one batch first.

    Any content type, which is the point. Only the batch's first part can be a typed
    command: the rest arrived alongside it, and typing cannot do that.
    """
    key = _relay_key(message)
    batch = await _BURSTS.gather(key, message)
    if batch is None:
        return

    async with _RELAY_LOCKS[key]:
        if await state.get_state() != Relay.composing.state:
            # Cancelled while the rest of the batch was arriving.
            return

        data = await state.get_data()
        chosen = list(data.get("messages") or [])
        if chosen:
            # Ahead of the command guard, whose prompt carries only a cancel button and
            # would take the picker away with it.
            sent = list(data.get("sent") or [])
            await _step(
                message,
                state,
                *relay_screen(services, count=len(chosen), sent=sent, note=RELAY_ALREADY),
            )
            return

        if await _refused_a_command(message, state):
            return

        ids = [part.message_id for part in batch]
        if len(ids) > relay.MAX_MESSAGES:
            await _step(
                message,
                state,
                f"Не больше {relay.MAX_MESSAGES} сообщений за раз. Пришлите меньше.",
                cancel_keyboard(),
            )
            return

        # The source is remembered here, never read from a button: callback data can be
        # forged, and it must not be able to point the bot at somebody else's chat.
        await state.update_data(messages=ids, source=message.chat.id, sent=[])
        await _step(message, state, *relay_screen(services, count=len(ids), sent=()))


@router.callback_query(F.data.startswith("adm:rto:"))
async def choose_relay_target(
    query: CallbackQuery, state: FSMContext, services: BotContext
) -> None:
    data = await state.get_data()
    ids = list(data.get("messages") or [])
    sent = list(data.get("sent") or [])
    if not ids:
        await _replace(query, *_relay_gone(services, query))
        return

    target = relay.destination_of(services.offices, _tail(query))
    if target is None:
        note = f"❌ {relay.NO_DESTINATION}"
        await _replace(query, *relay_screen(services, count=len(ids), sent=sent, note=note))
        return
    if target.chat_id in sent:
        await query.answer("Сюда уже отправлено.", show_alert=True)
        return
    await _replace(query, *relay_confirm_screen(target, count=len(ids)))


@router.callback_query(F.data.startswith("adm:rgo:"))
async def send_relay(query: CallbackQuery, state: FSMContext, services: BotContext) -> None:
    """Held under the lock from the first read to the last write.

    A second tap waits, then finds the group already ticked. Without the lock both taps
    read "not sent yet" before either had sent, and the group got the message twice.
    """
    async with _RELAY_LOCKS[_relay_key(query)]:
        data = await state.get_data()
        ids = list(data.get("messages") or [])
        sent = list(data.get("sent") or [])
        # No notifier only outside the running bot, where there is nothing to send with.
        if not ids or services.notifier is None:
            await _replace(query, *_relay_gone(services, query))
            return

        office_id = _tail(query)
        target = relay.destination_of(services.offices, office_id)
        if target is not None and target.chat_id in sent:
            note = f"Уже отправлено в «{html.escape(target.label)}»."
            await _replace(query, *relay_screen(services, count=len(ids), sent=sent, note=note))
            return

        try:
            outcome = await relay.deliver(
                office_id=office_id,
                source_chat_id=int(data["source"]),
                message_ids=ids,
                actor_id=query.from_user.id,
                offices=services.offices,
                notifier=services.notifier,
                audit=services.audit,
            )
        except RelayError as error:
            # Back to the picker rather than an alert: another group is the obvious next
            # move, and the reason stays on screen while the admin fixes it.
            note = f"❌ {html.escape(str(error))}"
            await _replace(query, *relay_screen(services, count=len(ids), sent=sent, note=note))
            return

        sent.append(outcome.destination.chat_id)
        await state.update_data(sent=sent)
        note = _delivered(outcome)
        await _replace(query, *relay_screen(services, count=len(ids), sent=sent, note=note))


@router.callback_query(F.data == "adm:rback")
async def back_to_relay_targets(
    query: CallbackQuery, state: FSMContext, services: BotContext
) -> None:
    data = await state.get_data()
    ids = list(data.get("messages") or [])
    if not ids:
        await _replace(query, *_relay_gone(services, query))
        return
    sent = list(data.get("sent") or [])
    await _replace(query, *relay_screen(services, count=len(ids), sent=sent))


def _relay_gone(services: BotContext, event: Message | CallbackQuery) -> tuple[str, Any]:
    """A picker from before a restart, or from a flow already finished.

    Redrawn as the menu rather than answered with an alert, so the stale buttons go away
    instead of staying live.
    """
    text, markup = _home(_is_owner(services, event))
    return (f"{RELAY_GONE}\n\n{text}", markup)


def _delivered(outcome: Relayed) -> str:
    where = html.escape(outcome.destination.label)
    if outcome.delivered < outcome.requested:
        # Telegram drops what it will not copy instead of failing the call.
        return (
            f"⚠️ В «{where}» ушло {outcome.delivered} из {outcome.requested}: "
            "остальное Telegram копировать не даёт."
        )
    return f"✅ Отправлено в «{where}»."


# ----------------------------------------------------------------------------- admins
#
# Owner-only, twice over. These handlers hide the buttons, and `manage_admins` refuses
# the call anyway: a hidden button is not a permission — a demoted admin still has the
# old screen open on their phone, and the free-text step carries no callback data at all.


@router.callback_query(F.data == "adm:admins")
async def admins(query: CallbackQuery, services: BotContext) -> None:
    if not _is_owner(services, query):
        await query.answer(NOT_THE_OWNER, show_alert=True)
        return
    await _replace(query, *admins_screen(services, viewer_id=query.from_user.id))


@router.callback_query(F.data.startswith("adm:admv:"))
async def admin_card_view(query: CallbackQuery, services: BotContext) -> None:
    screen = admin_card_screen(services, int(_tail(query)), viewer_id=query.from_user.id)
    if screen is None:
        await query.answer("Администратор не найден", show_alert=True)
        return
    await _replace(query, *screen)


@router.callback_query(F.data == "adm:admadd")
async def start_granting(query: CallbackQuery, services: BotContext) -> None:
    if not _is_owner(services, query):
        await query.answer(NOT_THE_OWNER, show_alert=True)
        return
    await _replace(query, *grant_screen(services))


@router.callback_query(F.data.startswith("adm:admpick:"))
async def grant_to_employee(query: CallbackQuery, services: BotContext, bot: Bot) -> None:
    # The button carries the *Telegram* id, because that is what adminship is keyed on.
    # The roster is asked only for a name to show in the list. Reading it as an employee
    # id finds nobody, and reports that the person has not linked their account.
    user_id = int(_tail(query))
    employee = services.offices.find_employee_by_user_id(user_id)
    await _grant(
        query, services, bot, user_id=user_id, label=employee.full_name if employee else ""
    )


@router.callback_query(F.data == "adm:admid")
async def start_typing_an_id(query: CallbackQuery, services: BotContext, state: FSMContext) -> None:
    if not _is_owner(services, query):
        await query.answer(NOT_THE_OWNER, show_alert=True)
        return
    await _ask(
        query,
        state,
        GrantAdmin.waiting_for_user_id,
        "Отправьте числовой Telegram id — его подскажет @userinfobot. "
        "Это нужно только тем, кого нет ни в одном офисе.",
        back_to="admins",
    )


@router.message(GrantAdmin.waiting_for_user_id)
async def apply_typed_id(
    message: Message, state: FSMContext, services: BotContext, bot: Bot
) -> None:
    if await _refused_a_command(message, state):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdecimal():
        await _step(message, state, "Нужно число. Попробуйте ещё раз.", cancel_keyboard())
        return

    actor_id = message.from_user.id if message.from_user else 0
    try:
        manage_admins.grant(
            user_id=int(raw),
            actor_id=actor_id,
            admins=services.admins,
            clock=services.clock,
            audit=services.audit,
        )
    except AdminError as error:
        # Stays in the state: a mistyped id is worth retyping, not restarting.
        await _step(message, state, str(error), cancel_keyboard())
        return

    await publish_for(bot, int(raw), admin=True)
    text, markup = admins_screen(services, viewer_id=actor_id)
    await _finish(message, state, f"Готово. {GRANT_HINT}\n\n{text}", markup)


@router.callback_query(F.data.startswith("adm:admdel:"))
async def confirm_revoking(query: CallbackQuery, services: BotContext) -> None:
    user_id = int(_tail(query))
    if not (_is_owner(services, query) or user_id == query.from_user.id):
        await query.answer(NOT_THE_OWNER, show_alert=True)
        return
    question = (
        "Убрать себя из администраторов? Вернуть права сможет только владелец."
        if user_id == query.from_user.id
        else f"Разжаловать <code>{user_id}</code>?"
    )
    await _replace(query, question, confirm(f"adm:admdelgo:{user_id}", back=f"adm:admv:{user_id}"))


@router.callback_query(F.data.startswith("adm:admdelgo:"))
async def revoke_admin(query: CallbackQuery, services: BotContext, bot: Bot) -> None:
    user_id = int(_tail(query))
    try:
        manage_admins.revoke(
            user_id=user_id,
            actor_id=query.from_user.id,
            admins=services.admins,
            audit=services.audit,
        )
    except AdminError as error:
        await query.answer(str(error), show_alert=True)
        return

    await publish_for(bot, user_id, admin=False)
    if user_id == query.from_user.id:
        # They have just removed their own access; there is no admin screen to go back to,
        # and an empty keyboard is a markup Telegram will reject. Drop it entirely.
        await _replace(query, "Готово. Вы больше не администратор.", None)
        return
    await _replace(query, *admins_screen(services, viewer_id=query.from_user.id))


@router.callback_query(F.data.startswith("adm:admown:"))
async def confirm_transfer(query: CallbackQuery, services: BotContext) -> None:
    if not _is_owner(services, query):
        await query.answer(NOT_THE_OWNER, show_alert=True)
        return
    user_id = int(_tail(query))
    await _replace(
        query,
        f"Передать владение <code>{user_id}</code>?\n\n"
        "Вы станете обычным администратором и больше не сможете выдавать права "
        "или создавать офисы. Вернуть владение сможет только новый владелец.",
        confirm(f"adm:admowngo:{user_id}", back=f"adm:admv:{user_id}"),
    )


@router.callback_query(F.data.startswith("adm:admowngo:"))
async def transfer_ownership(query: CallbackQuery, services: BotContext) -> None:
    try:
        manage_admins.transfer_ownership(
            user_id=int(_tail(query)),
            actor_id=query.from_user.id,
            admins=services.admins,
            clock=services.clock,
            audit=services.audit,
        )
    except AdminError as error:
        await query.answer(str(error), show_alert=True)
        return
    await _replace(query, *admins_screen(services, viewer_id=query.from_user.id))


async def _grant(
    query: CallbackQuery, services: BotContext, bot: Bot, *, user_id: int, label: str
) -> None:
    try:
        manage_admins.grant(
            user_id=user_id,
            label=label,
            actor_id=query.from_user.id,
            admins=services.admins,
            clock=services.clock,
            audit=services.audit,
        )
    except AdminError as error:
        await query.answer(str(error), show_alert=True)
        return

    # Their `/admin` entry appears now rather than at the next deploy, which is most of
    # what moving adminship out of the environment was for.
    await publish_for(bot, user_id, admin=True)
    await _replace(query, *admins_screen(services, viewer_id=query.from_user.id))


# ---------------------------------------------------------------------------- screens
#
# Each returns the text and keyboard for one screen from *explicit* arguments. Handlers
# parse `query.data` and call these; handlers never call each other.
#
# That rule exists because they used to. Pressing a `adm:fixtog:…` button ran the toggle
# and then called `fixed_day`, which re-parsed `query.data` — by then a toggle and not a
# day — and raised. The write had already succeeded, so the checkbox was right the next
# time the screen was opened and never on the tap itself. `cycle_desks` had the same
# shape and looked up an office named "0". A screen built from arguments cannot do this.


def office_screen(services: BotContext, office_id: str) -> tuple[str, InlineKeyboardMarkup] | None:
    """None when there is no such office, so the caller can say so."""
    found = services.offices.get_office(office_id)
    if found is None:
        return None

    today = services.clock.today()
    roster = services.offices.employees(office_id)
    active = sum(1 for employee in roster if employee.in_tenure(today))
    chat = found.chat_id or "не задан"

    return (
        f"<b>{html.escape(found.name)}</b>\n"
        f"Чат: <code>{chat}</code>\n"
        f"Сотрудников: {active}"
        + (f" (+{len(roster) - active} уволенных)" if len(roster) > active else "")
        + f"\nПеремешивание: <code>{found.seed_nonce}</code>",
        office_menu(office_id),
    )


def desks_screen(services: BotContext, office_id: str) -> tuple[str, InlineKeyboardMarkup]:
    context = _context(services, office_id)
    values = {weekday: str(context.template.desks_on(weekday)) for weekday in range(5)}
    return (
        "Свободных мест по дням. Нажмите на день, чтобы задать число:",
        weekday_picker(office_id, "adm:desk", values),
    )


def fixed_screen(services: BotContext, office_id: str) -> tuple[str, InlineKeyboardMarkup]:
    context = _context(services, office_id)
    values = {weekday: str(len(context.template.fixed_on(weekday))) for weekday in range(5)}
    return (
        "Фиксированное расписание — сколько человек закреплено за днём:",
        weekday_picker(office_id, "adm:fixday", values),
    )


def fixed_day_screen(
    services: BotContext, office_id: str, weekday: int
) -> tuple[str, InlineKeyboardMarkup]:
    context = _context(services, office_id)
    assigned = set(context.template.fixed_on(weekday))
    today = services.clock.today()

    rows = [
        (
            _button(
                f"{'✅' if employee.id in assigned else '▫️'} {employee.full_name}",
                f"adm:fixtog:{weekday}:{employee.id}",
            ),
        )
        for employee in sorted(
            services.offices.employees(office_id), key=lambda e: e.full_name.casefold()
        )
        if employee.in_tenure(today)
    ]
    return (
        f"Кто всегда в офисе в {WEEKDAY_LABELS[weekday]}: отмечено {len(assigned)}.",
        keyboard(*rows, (_button("‹ Назад", f"adm:fix:{office_id}"),)),
    )


def card_screen(services: BotContext, employee_id: str) -> tuple[str, InlineKeyboardMarkup] | None:
    """One person. None when there is no such employee, so the caller can say so.

    A builder rather than a handler helper: a cancelled rename has to come back here, and
    a screen you can only reach by pressing its own button is not somewhere you can return
    to.
    """
    found = _find(services, employee_id)
    if found is None:
        return None
    office_id, employee = found

    today = services.clock.today()
    departed = not employee.in_tenure(today)
    upcoming = services.schedule.upcoming_for_employee(employee.id, start=today, limit=5)
    common = services.voice.catalog.common

    handle = f"@{employee.telegram_username}" if employee.telegram_username else "не задан"
    linked = "да" if employee.telegram_user_id is not None else "нет"
    gender = "женский" if str(employee.gender) == "female" else "мужской"
    days = ", ".join(format_date(day, common) for day in upcoming) or "нет"
    allowance = (
        f"{employee.ai_daily_limit} (свой)"
        if employee.ai_daily_limit is not None
        else f"{services.chat_policy.per_user_daily_limit} (по умолчанию)"
    )

    lines = [
        f"<b>{html.escape(employee.full_name)}</b>",
        f"id: <code>{html.escape(employee.id)}</code>",
        f"Ник: {html.escape(handle)} · привязан: {linked}",
        f"Род: {gender}",
        f"Ближайшие дни: {html.escape(days)}",
        f"Лимит ИИ: {allowance}",
    ]
    if departed:
        ended = employee.ended_on.isoformat() if employee.ended_on else "—"
        lines.append(f"<i>Уволен с {ended}</i>")

    return ("\n".join(lines), employee_card(office_id, employee.id, departed=departed))


def roster_screen(services: BotContext, office_id: str) -> tuple[str, InlineKeyboardMarkup]:
    today = services.clock.today()
    roster = sorted(services.offices.employees(office_id), key=lambda e: e.full_name.casefold())

    entries = []
    for employee in roster:
        marks = []
        if not employee.in_tenure(today):
            marks.append("уволен")
        if employee.telegram_user_id is None:
            marks.append("не привязан")
        suffix = f" · {', '.join(marks)}" if marks else ""
        entries.append((employee.id, f"{employee.full_name}{suffix}"))

    return (
        f"<b>Сотрудники</b> — {len(roster)} чел. Нажмите, чтобы открыть карточку.",
        employee_list(office_id, entries),
    )


def _parse_count(raw: str) -> int | None:
    text = raw.strip()
    if not text.isdecimal():
        return None
    value = int(text)
    return value if 0 <= value <= MAX_DESKS else None


def _context(services: BotContext, office_id: str):  # type: ignore[no-untyped-def]
    today = services.clock.today()
    return services.offices.planning_context(office_id, start=today, end=today)


def offices_screen(services: BotContext, *, owner: bool) -> tuple[str, InlineKeyboardMarkup]:
    """Closed offices are listed too, badged, or they could never be reopened."""
    offices = services.offices.all_offices()
    entries = [
        (office.id, office.name if office.active else f"💤 {office.name}") for office in offices
    ]
    closed = sum(1 for office in offices if not office.active)

    if not entries:
        return (
            "Офисов пока нет."
            + ("\n\nНачните с «Создать офис»." if owner else "\n\nСоздать может только владелец."),
            office_list(entries, create=owner),
        )

    heading = f"Выберите офис — всего {len(entries)}"
    return (
        heading + (f", из них закрытых {closed}." if closed else "."),
        office_list(entries, create=owner),
    )


def office_settings_screen(
    services: BotContext, office_id: str, *, owner: bool
) -> tuple[str, InlineKeyboardMarkup] | None:
    found = services.offices.get_office(office_id)
    if found is None:
        return None

    # The status belongs in the *text*: closing an office changes only which button the
    # keyboard offers, and Telegram refuses to redraw an otherwise identical message.
    return (
        f"<b>{html.escape(found.name)}</b>\n"
        f"Id: <code>{found.id}</code>\n"
        f"Статус: {'активен' if found.active else 'закрыт'}\n"
        f"Календарь: {found.holiday_calendar}",
        office_settings_menu(office_id, owner=owner, active=found.active),
    )


def chat_screen(services: BotContext, office_id: str) -> tuple[str, InlineKeyboardMarkup] | None:
    found = services.offices.get_office(office_id)
    if found is None:
        return None

    sharing = [
        other.name
        for other in services.offices.all_offices()
        if other.id != office_id and found.chat_id is not None and other.chat_id == found.chat_id
    ]
    note = ""
    if sharing:
        # Supported, not a mistake — but worth saying, because messages into a shared
        # chat carry an office header and people wonder why.
        note = "\n\nЭтот же чат у: " + ", ".join(html.escape(name) for name in sharing)

    return (
        f"<b>{html.escape(found.name)}</b>\n"
        f"Чат: <code>{found.chat_id if found.chat_id is not None else 'не задан'}</code>\n\n"
        "Проще всего отправить /bind в группе офиса — id чата нигде не показывается "
        "и по памяти его не набрать." + note,
        chat_menu(office_id, bound=found.chat_id is not None),
    )


def calendar_screen(
    services: BotContext, office_id: str
) -> tuple[str, InlineKeyboardMarkup] | None:
    found = services.offices.get_office(office_id)
    if found is None:
        return None
    return (
        f"Производственный календарь офиса «{html.escape(found.name)}».\n"
        f"Сейчас: <b>{found.holiday_calendar}</b>",
        calendar_picker(office_id, CALENDARS, found.holiday_calendar),
    )


def limits_screen(services: BotContext) -> tuple[str, InlineKeyboardMarkup]:
    """What the AI allowance is, and who is not on it.

    The counts are in the text because Telegram refuses to redraw a message whose text and
    markup are both unchanged, and every button here leads away rather than toggling.
    """
    policy = services.chat_policy
    singled_out = [
        employee
        for office in services.offices.all_offices()
        for employee in services.offices.employees(office.id)
        if employee.ai_daily_limit is not None
    ]

    lines = [
        "<b>Лимиты ИИ</b>",
        f"По умолчанию: <b>{policy.per_user_daily_limit}</b> запросов в день на человека",
        f"Общий предел: <b>{policy.global_daily_limit}</b> запросов в день на всех",
        "",
        f"Сегодня использовано: {services.usage.total_today(services.clock.today())}",
    ]
    if singled_out:
        lines.append("")
        lines.append(f"Свой лимит у {len(singled_out)} чел.:")
        lines += [
            f"• {html.escape(employee.full_name)} — {employee.ai_daily_limit}"
            for employee in sorted(singled_out, key=lambda e: e.full_name.casefold())[:10]
        ]
    else:
        lines.append("")
        lines.append("Личные лимиты меняются в карточке сотрудника.")

    return ("\n".join(lines), limits_menu())


def times_screen(services: BotContext) -> tuple[str, InlineKeyboardMarkup]:
    """When everything goes out, and what the Tempo time means in practice.

    The times are in the text, so a changed one always changes the message — Telegram
    refuses to redraw a message that has not changed.
    """
    times = services.reminder_times
    common = services.voice.catalog.common
    gap = times.tempo_gap_minutes
    tempo_effect = (
        f"⏳ за {gap} мин до конца дня — предложит потратить их на Tempo"
        if gap
        else f"⏳ без отсчёта: до конца дня больше {RUSH_WINDOW_MINUTES} мин или день уже кончился"
    )

    lines = [
        "<b>Время рассылок</b>",
        f"Часовой пояс: {html.escape(services.app_timezone)}",
        "",
        f"📣 Кто завтра в офисе: <b>{_hhmm(times.attendance)}</b>",
        f"📝 Tempo: <b>{_hhmm(times.tempo)}</b>, в последний рабочий день недели и месяца",
        f"      {tempo_effect}",
        f"🏁 Конец рабочего дня: <b>{_hhmm(times.workday_end)}</b>",
        f"🎉 Поздравление с праздником: <b>{_hhmm(times.holiday)}</b>",
        f"🔄 Продление расписания: <b>{common.weekdays_short[times.extend_weekday]} "
        f"{_hhmm(times.extend)}</b>",
    ]

    # One line per calendar in use rather than per office: two offices on KZ would only
    # say the same thing twice. Shown so an admin can tell a stale calendar at a glance.
    calendar = services.holiday_calendar
    today = services.clock.today()
    codes = sorted({office.holiday_calendar for office in services.offices.active_offices()})
    upcoming = [(code, calendar.next_celebration(code, today)) for code in codes]
    known = [(code, found) for code, found in upcoming if found is not None]
    if known:
        lines.append("")
        lines.append(f"Ближайший праздник ({html.escape(calendar.source())}):")
        lines += [
            f"• {html.escape(code)}: {format_date(found.day, common)} — "
            f"{html.escape(found.local_name)}"
            for code, found in known
        ]

    return ("\n".join(lines), times_menu())


def _hhmm(moment: time) -> str:
    return moment.strftime("%H:%M")


def relay_screen(
    services: BotContext, *, count: int, sent: Collection[int], note: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    """Where the message can go, and where it has already gone.

    Built from the groups as they are now, so an office closed or unbound since the last
    draw is simply not offered. The text names where it has gone rather than leaving that
    to the ticks, for the same reason the other headings here carry counts.
    """
    targets = relay.destinations(services.offices)
    lines = [note, ""] if note else []
    lines.append(f"<b>Куда отправить?</b> Сообщений: {count}.")

    done = [target.label for target in targets if target.chat_id in sent]
    if done:
        lines.append("Отправлено: " + ", ".join(html.escape(label) for label in done) + ".")
    if not targets:
        lines += ["", NO_RELAY_TARGETS]

    entries = [
        (target.office_id, f"{'✅' if target.chat_id in sent else '▫️'} {target.label}")
        for target in targets
    ]
    return ("\n".join(lines), relay_picker(entries, done=bool(sent)))


def relay_confirm_screen(target: Destination, *, count: int) -> tuple[str, InlineKeyboardMarkup]:
    """The last stop before a whole group's phones go off, which is why it exists."""
    return (
        f"Отправить в «{html.escape(target.label)}»? Сообщений: {count}.\n\n"
        "Уйдёт от имени бота, в точности как вы прислали, "
        "и все в группе получат уведомление.",
        confirm(f"adm:rgo:{target.office_id}", back="adm:rback"),
    )


def admins_screen(services: BotContext, *, viewer_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """The count in the text is load-bearing.

    Telegram refuses to redraw a message whose text and markup are both unchanged, and
    revoking the last admin in the list changes only the keyboard.
    """
    entries, owner = _admin_entries(services, viewer_id=viewer_id)
    return (
        f"<b>Администраторы</b> — {len(entries)}\n\n"
        "👑 — владелец: только он выдаёт права и создаёт офисы.\n"
        "Владение передаётся, а не снимается.",
        admin_list(entries, owner=owner),
    )


def admin_card_screen(
    services: BotContext, user_id: int, *, viewer_id: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    record = next((item for item in services.admins.listing() if item.user_id == user_id), None)
    if record is None:
        return None

    name = html.escape(record.label) if record.label else "без имени"
    granted = record.granted_at.date().isoformat() if record.granted_at else "—"
    return (
        f"{'👑' if record.is_owner else '🛡'} <b>{name}</b>\n"
        f"Id: <code>{record.user_id}</code>\n"
        f"Права с: {granted}",
        admin_card(
            record.user_id,
            owner=services.is_owner(viewer_id),
            is_owner=record.is_owner,
            is_self=record.user_id == viewer_id,
        ),
    )


def grant_screen(services: BotContext) -> tuple[str, InlineKeyboardMarkup]:
    """Only people the bot already knows can be picked.

    The Bot API cannot turn a @username into a user id, so somebody who has never sent
    `/start` has to be typed in by number.
    """
    today = services.clock.today()
    taken = services.admins.ids()

    entries = []
    for office in services.offices.all_offices():
        for employee in sorted(
            services.offices.employees(office.id), key=lambda e: e.full_name.casefold()
        ):
            if (
                employee.telegram_user_id is not None
                and employee.telegram_user_id not in taken
                and employee.in_tenure(today)
            ):
                entries.append((employee.telegram_user_id, employee.full_name))

    return (
        f"Кого сделать администратором? Привязанных сотрудников: {len(entries)}.",
        grant_picker(entries),
    )


def _admin_entries(services: BotContext, *, viewer_id: int) -> tuple[list[tuple[int, str]], bool]:
    entries = [
        (
            record.user_id,
            f"{'👑' if record.is_owner else '🛡'} {record.label or record.user_id}"
            + (" · вы" if record.user_id == viewer_id else ""),
        )
        for record in services.admins.listing()
    ]
    return entries, services.is_owner(viewer_id)


def _tail(query: CallbackQuery) -> str:
    return str(query.data).rsplit(":", 1)[1]


def _button(text: str, data: str):  # type: ignore[no-untyped-def]
    from tabelshchik.adapters.telegram.keyboards import button

    return button(text, data)


def _back_button(office_id: str):  # type: ignore[no-untyped-def]
    return _button("‹ Назад", f"adm:office:{office_id}")


async def _replace(query: CallbackQuery, text: str, markup: Any = None) -> int | None:
    """Redraw the screen in place. See `adapters/telegram/ui.py` for the rule."""
    return await redraw(query, text, markup)
