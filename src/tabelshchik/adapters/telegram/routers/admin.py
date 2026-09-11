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
from aiogram.types import CallbackQuery, Message, TelegramObject

from tabelshchik.adapters.reports.xlsx import build_workbook, upcoming_week
from tabelshchik.adapters.telegram.keyboards import (
    WEEKDAY_LABELS,
    keyboard,
    main_menu,
    office_list,
    office_menu,
    weekday_picker,
)
from tabelshchik.application.build_report import ReportDay, build_report
from tabelshchik.application.context import BotContext
from tabelshchik.application.regenerate_schedule import regenerate
from tabelshchik.application.send_attendance_reminder import send_attendance_reminder
from tabelshchik.application.voice import format_date
from tabelshchik.domain.entities import SCALE

router = Router(name="admin")


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
    office_id = _tail(query)
    today = services.clock.today()
    roster = services.offices.employees(office_id)

    lines = []
    for employee in roster:
        marks = []
        if not employee.in_tenure(today):
            marks.append("уволен")
        if employee.telegram_user_id is None:
            marks.append("не привязан")
        suffix = f" <i>({', '.join(marks)})</i>" if marks else ""
        handle = f" @{employee.telegram_username}" if employee.telegram_username else ""
        lines.append(f"• {html.escape(employee.full_name)}{handle}{suffix}")

    await _replace(
        query,
        "<b>Сотрудники</b>\n" + ("\n".join(lines) or "Пусто"),
        keyboard((_back_button(office_id),)),
    )


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
    common = services.voice.catalog.common

    lines = [
        f"<b>{common.weekdays_short[s.day.weekday()]} {s.day.strftime('%d.%m')}</b>: "
        + ", ".join(html.escape(names.get(eid, eid)) for eid in s.roster)
        for s in snapshots
        if s.roster
    ]
    if isinstance(query.message, Message):
        await query.message.answer("\n".join(lines) or "Ничего не запланировано.")


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
    await _replace(
        query,
        "<b>Последние запуски</b>\n" + "\n".join(lines) + f"\n\nБаза: {size_mb:.1f} МБ",
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

    common = services.voice.catalog.common
    caption = services.voice.catalog.text("schedule.caption").format(
        office=context.office.name,
        start=format_date(result.horizon_start, common),
        end=format_date(result.horizon_end, common),
        summary=upcoming_week(report, common),
    )
    await services.notifier.send_document(
        message.chat.id,
        f"{office_id}-{result.horizon_start.isoformat()}.xlsx",
        build_workbook(report, common),
        caption=caption[:1024],
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
