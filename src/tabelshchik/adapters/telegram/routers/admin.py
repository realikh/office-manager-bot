"""Admin mode.

The whole router is gated by one filter rather than a check repeated in each handler —
a permission check you have to remember to write is one you will eventually forget.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from tabelshchik.adapters.reports.xlsx import build_workbook, schedule_caption
from tabelshchik.adapters.telegram.keyboards import (
    WEEKDAY_LABELS,
    confirm,
    employee_card,
    employee_list,
    gender_picker,
    keyboard,
    main_menu,
    office_list,
    office_menu,
    weekday_picker,
)
from tabelshchik.application import manage_roster
from tabelshchik.application.build_report import ReportDay, build_report
from tabelshchik.application.context import BotContext
from tabelshchik.application.manage_roster import RosterError
from tabelshchik.application.regenerate_schedule import regenerate
from tabelshchik.application.send_attendance_reminder import send_attendance_reminder
from tabelshchik.application.voice import format_date, render_days
from tabelshchik.domain.entities import SCALE, Employee

router = Router(name="admin")


class AddEmployee(StatesGroup):
    waiting_for_name = State()
    waiting_for_username = State()
    waiting_for_gender = State()


class EditEmployee(StatesGroup):
    waiting_for_name = State()
    waiting_for_username = State()


SKIP = "-"


class AdminOnly(BaseMiddleware):
    """One gate for the whole area.

    A non-admin gets silence rather than a refusal: the admin surface is not something
    to advertise to a group chat.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        services: BotContext = data["services"]
        user = data.get("event_from_user")
        if user is None or not services.is_admin(user.id):
            return None
        return await handler(event, data)


router.message.middleware(AdminOnly())
router.callback_query.middleware(AdminOnly())


@router.message(Command("admin"))
async def admin_menu(message: Message) -> None:
    await message.answer("Режим администратора:", reply_markup=main_menu())


@router.callback_query(F.data == "adm:menu")
async def back(query: CallbackQuery) -> None:
    await _replace(query, "Режим администратора:", main_menu())


@router.callback_query(F.data == "adm:offices")
async def offices(query: CallbackQuery, services: BotContext) -> None:
    entries = [(office.id, office.name) for office in services.offices.active_offices()]
    await _replace(query, "Выберите офис:", office_list(entries))


@router.callback_query(F.data.startswith("adm:office:"))
async def office(query: CallbackQuery, services: BotContext) -> None:
    office_id = _tail(query)
    found = services.offices.get_office(office_id)
    if found is None:
        await query.answer("Офис не найден", show_alert=True)
        return

    chat = found.chat_id or "не задан"
    headcount = len(services.offices.employees(office_id))
    await _replace(
        query,
        f"<b>{html.escape(found.name)}</b>\nЧат: <code>{chat}</code>\nСотрудников: {headcount}",
        office_menu(office_id),
    )


@router.callback_query(F.data.startswith("adm:emp:"))
async def employees(query: CallbackQuery, services: BotContext) -> None:
    await _show_roster(query, services, _tail(query))


async def _show_roster(query: CallbackQuery, services: BotContext, office_id: str) -> None:
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

    await _replace(
        query,
        f"<b>Сотрудники</b> — {len(roster)} чел. Нажмите, чтобы открыть карточку.",
        employee_list(office_id, entries),
    )


@router.callback_query(F.data.startswith("adm:empadd:"))
async def start_adding_employee(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(AddEmployee.waiting_for_name)
    await state.update_data(office_id=_tail(query))
    if isinstance(query.message, Message):
        await query.message.answer(
            "Имя и фамилия нового сотрудника — как они должны выглядеть в напоминаниях.\n"
            "/cancel — отмена."
        )


@router.message(AddEmployee.waiting_for_name)
async def receive_new_name(message: Message, state: FSMContext) -> None:
    await state.update_data(full_name=message.text or "")
    await state.set_state(AddEmployee.waiting_for_username)
    await message.answer(
        f"Telegram-ник без «@». Если его нет, отправьте «{SKIP}» — "
        "человек сможет привязать себя сам через /start."
    )


@router.message(AddEmployee.waiting_for_username)
async def receive_new_username(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    await state.update_data(username=None if raw in {SKIP, ""} else raw)
    await state.set_state(AddEmployee.waiting_for_gender)
    # Asked for, not guessed: the reminder writes a gendered epithet in front of the name,
    # and a name alone is not evidence of anything.
    await message.answer("Пол — от него зависит род в напоминаниях:", reply_markup=gender_picker())


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
        await query.message.answer(f"❌ {html.escape(str(error))}")
        return

    await query.message.answer(
        f"✅ {html.escape(change.full_name)} добавлен(а), id "
        f"<code>{html.escape(change.employee_id)}</code>. Расписание пересчитано."
    )


@router.callback_query(F.data.startswith("adm:empv:"))
async def employee_card_screen(query: CallbackQuery, services: BotContext) -> None:
    await _show_card(query, services, _tail(query))


async def _show_card(query: CallbackQuery, services: BotContext, employee_id: str) -> None:
    found = _find(services, employee_id)
    if found is None:
        await query.answer("Сотрудник не найден", show_alert=True)
        return
    office_id, employee = found

    today = services.clock.today()
    departed = not employee.in_tenure(today)
    upcoming = services.schedule.upcoming_for_employee(employee.id, start=today, limit=5)
    common = services.voice.catalog.common

    handle = f"@{employee.telegram_username}" if employee.telegram_username else "не задан"
    linked = "да" if employee.telegram_user_id is not None else "нет"
    gender = "женский" if str(employee.gender) == "female" else "мужской"
    days = ", ".join(format_date(day, common) for day in upcoming) or "нет"

    lines = [
        f"<b>{html.escape(employee.full_name)}</b>",
        f"id: <code>{html.escape(employee.id)}</code>",
        f"Ник: {html.escape(handle)} · привязан: {linked}",
        f"Род: {gender}",
        f"Ближайшие дни: {html.escape(days)}",
    ]
    if departed:
        ended = employee.ended_on.isoformat() if employee.ended_on else "—"
        lines.append(f"<i>Уволен с {ended}</i>")

    await _replace(
        query, "\n".join(lines), employee_card(office_id, employee.id, departed=departed)
    )


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
    await query.answer()
    await state.set_state(EditEmployee.waiting_for_name)
    await state.update_data(employee_id=_tail(query))
    if isinstance(query.message, Message):
        await query.message.answer("Новое имя и фамилия. /cancel — отмена.")


@router.message(EditEmployee.waiting_for_name)
async def apply_rename(message: Message, state: FSMContext, services: BotContext) -> None:
    data = await state.get_data()
    employee_id = str(data.get("employee_id", ""))
    found = _find(services, employee_id)
    if found is None:
        await state.clear()
        await message.answer("Сотрудник не найден.")
        return

    try:
        change = manage_roster.rename(
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
        await message.answer(f"❌ {html.escape(str(error))}")
        return

    await state.clear()
    await message.answer(f"✅ Теперь это {html.escape(change.full_name)}.")


@router.callback_query(F.data.startswith("adm:empuser:"))
async def start_setting_username(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(EditEmployee.waiting_for_username)
    await state.update_data(employee_id=_tail(query))
    if isinstance(query.message, Message):
        await query.message.answer(
            f"Telegram-ник без «@». Отправьте «{SKIP}», чтобы очистить. /cancel — отмена."
        )


@router.message(EditEmployee.waiting_for_username)
async def apply_username(message: Message, state: FSMContext, services: BotContext) -> None:
    data = await state.get_data()
    employee_id = str(data.get("employee_id", ""))
    found = _find(services, employee_id)
    if found is None:
        await state.clear()
        await message.answer("Сотрудник не найден.")
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
        await message.answer(f"❌ {html.escape(str(error))}")
        return

    await state.clear()
    await message.answer("✅ Ник обновлён.")


@router.message(Command("cancel"), AddEmployee.waiting_for_name)
@router.message(Command("cancel"), AddEmployee.waiting_for_username)
@router.message(Command("cancel"), AddEmployee.waiting_for_gender)
@router.message(Command("cancel"), EditEmployee.waiting_for_name)
@router.message(Command("cancel"), EditEmployee.waiting_for_username)
async def cancel_admin_flow(message: Message, state: FSMContext) -> None:
    """Admin mode needs its own cancel.

    This router is registered before the employee one, but the only `/cancel` used to live
    there — so cancelling a half-finished admin wizard answered with the *employee* menu.
    Filtering on the admin states keeps each flow's escape hatch its own.
    """
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())


def _find(services: BotContext, employee_id: str) -> tuple[str, Employee] | None:
    """The employee and the office they belong to.

    Office membership lives on the database row rather than on the entity, so finding it
    means asking each office. With two of them that is cheaper than another port method.
    """
    for office in services.offices.active_offices():
        for employee in services.offices.employees(office.id):
            if employee.id == employee_id:
                return office.id, employee
    return None


@router.callback_query(F.data.startswith("adm:desks:"))
async def desks(query: CallbackQuery, services: BotContext) -> None:
    office_id = _tail(query)
    context = _context(services, office_id)
    values = {weekday: str(context.template.desks_on(weekday)) for weekday in range(5)}
    await _replace(
        query,
        "Свободных мест по дням. Нажмите, чтобы увеличить (после 9 — обнуляется):",
        weekday_picker(office_id, "adm:desk", values),
    )


@router.callback_query(F.data.startswith("adm:desk:"))
async def cycle_desks(query: CallbackQuery, services: BotContext) -> None:
    _, _, office_id, weekday_raw = str(query.data).split(":")
    weekday = int(weekday_raw)
    context = _context(services, office_id)

    current = context.template.desks_on(weekday)
    services.roster.set_vacant_desks(office_id, weekday, 0 if current >= 9 else current + 1)
    services.audit.record(
        actor_id=query.from_user.id,
        action="template.desks",
        payload={"office": office_id, "weekday": weekday},
    )
    await desks(query, services)


@router.callback_query(F.data.startswith("adm:fix:"))
async def fixed(query: CallbackQuery, services: BotContext) -> None:
    office_id = _tail(query)
    context = _context(services, office_id)
    values = {weekday: str(len(context.template.fixed_on(weekday))) or "0" for weekday in range(5)}
    await _replace(
        query,
        "Фиксированное расписание — сколько человек закреплено за днём:",
        weekday_picker(office_id, "adm:fixday", values),
    )


@router.callback_query(F.data.startswith("adm:fixday:"))
async def fixed_day(query: CallbackQuery, services: BotContext) -> None:
    _, _, office_id, weekday_raw = str(query.data).split(":")
    weekday = int(weekday_raw)
    context = _context(services, office_id)
    assigned = set(context.template.fixed_on(weekday))

    rows = [
        (
            _button(
                f"{'✅' if employee.id in assigned else '▫️'} {employee.full_name}",
                f"adm:fixtog:{office_id}:{weekday}:{employee.id}",
            ),
        )
        for employee in services.offices.employees(office_id)
        if employee.in_tenure(services.clock.today())
    ]
    await _replace(
        query,
        f"Кто всегда в офисе в {WEEKDAY_LABELS[weekday]}:",
        keyboard(*rows, (_button("‹ Назад", f"adm:fix:{office_id}"),)),
    )


@router.callback_query(F.data.startswith("adm:fixtog:"))
async def toggle_fixed(query: CallbackQuery, services: BotContext) -> None:
    _, _, office_id, weekday_raw, employee_id = str(query.data).split(":", 4)
    now_fixed = services.roster.toggle_fixed(office_id, int(weekday_raw), employee_id)
    services.audit.record(
        actor_id=query.from_user.id,
        action="template.fixed",
        payload={
            "office": office_id,
            "weekday": weekday_raw,
            "employee": employee_id,
            "fixed": now_fixed,
        },
    )
    await fixed_day(query, services)


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


@router.callback_query(F.data.startswith("adm:reseed:"))
async def reseed(query: CallbackQuery, services: BotContext) -> None:
    office_id = _tail(query)
    services.roster.bump_seed(office_id)
    await query.answer("Перемешано — теперь перегенерируйте.")
    await office(query, services)


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
    """Validates the files on disk without applying them."""
    from tabelshchik.config.loader import ConfigError, load

    try:
        loaded = load(services.config_dir)
    except ConfigError as error:
        await _replace(
            query,
            f"❌ <pre>{html.escape(str(error))}</pre>",
            keyboard((_button("‹ Назад", "adm:menu"),)),
        )
        return

    await _replace(
        query,
        f"✅ Конфигурация корректна. Офисов: {len(loaded.offices)}.",
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


def _context(services: BotContext, office_id: str):  # type: ignore[no-untyped-def]
    today = services.clock.today()
    return services.offices.planning_context(office_id, start=today, end=today)


def _tail(query: CallbackQuery) -> str:
    return str(query.data).rsplit(":", 1)[1]


def _button(text: str, data: str):  # type: ignore[no-untyped-def]
    from tabelshchik.adapters.telegram.keyboards import button

    return button(text, data)


def _back_button(office_id: str):  # type: ignore[no-untyped-def]
    return _button("‹ Назад", f"adm:office:{office_id}")


async def _replace(query: CallbackQuery, text: str, markup: Any) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except Exception:
            await query.message.answer(text, reply_markup=markup)
