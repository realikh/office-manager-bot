"""Inline keyboards.

Callback data is kept short and prefixed by area, because Telegram caps it at 64 bytes
and a truncated payload fails in a way that is tedious to diagnose.
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def keyboard(*rows: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_menu(*, owner: bool = False) -> InlineKeyboardMarkup:
    """The admin menu. `owner` defaults to False so a forgotten argument fails closed."""
    rows = [
        (button("🏢 Офисы", "adm:offices"),),
        (button("📨 Отправить сообщение", "adm:relay"),),
        (button("⚖️ Справедливость", "adm:fair"), button("🎭 Настроение", "adm:mood")),
        (button("📊 Состояние", "adm:status"), button("⚙️ Конфиг", "adm:config")),
        (button("💾 Резервная копия", "adm:backup"),),
        (button("🤖 Лимиты ИИ", "adm:limits"), button("⏰ Время рассылок", "adm:times")),
    ]
    if owner:
        rows.append((button("👑 Администраторы", "adm:admins"),))
    return keyboard(*rows)


def admin_list(entries: Sequence[tuple[int, str]], *, owner: bool) -> InlineKeyboardMarkup:
    rows = [(button(label, f"adm:admv:{user_id}"),) for user_id, label in entries]
    if owner:
        rows.append((button("➕ Добавить", "adm:admadd"),))
    return keyboard(*rows, (button("‹ Назад", "adm:menu"),))


def admin_card(user_id: int, *, owner: bool, is_owner: bool, is_self: bool) -> InlineKeyboardMarkup:
    """The owner's own card offers nothing: ownership is transferred away, not dropped."""
    rows = []
    if owner and not is_owner:
        rows.append((button("👑 Передать владение", f"adm:admown:{user_id}"),))
    if not is_owner and (owner or is_self):
        label = "🚪 Убрать себя" if is_self else "🚪 Разжаловать"
        rows.append((button(label, f"adm:admdel:{user_id}"),))
    return keyboard(*rows, (button("‹ Назад", "adm:admins"),))


def grant_picker(entries: Sequence[tuple[int, str]]) -> InlineKeyboardMarkup:
    """People the bot already knows, plus a way to type an id.

    The Bot API cannot turn a @username into a user id, so somebody who has linked their
    account is the only person who can be picked rather than typed.
    """
    rows = [(button(label, f"adm:admpick:{user_id}"),) for user_id, label in entries]
    return keyboard(
        *rows,
        (button("✍️ Ввести id", "adm:admid"),),
        (button("‹ Назад", "adm:admins"),),
    )


def office_list(
    offices: Sequence[tuple[str, str]], *, action: str = "adm:office", create: bool = False
) -> InlineKeyboardMarkup:
    rows = [(button(name, f"{action}:{office_id}"),) for office_id, name in offices]
    if create:
        rows.append((button("➕ Создать офис", "adm:onew"),))
    return keyboard(*rows, (button("‹ Назад", "adm:menu"),))


def office_menu(office_id: str) -> InlineKeyboardMarkup:
    return keyboard(
        (button("👥 Сотрудники", f"adm:emp:{office_id}"),),
        (button("📌 Фикс. расписание", f"adm:fix:{office_id}"),),
        (button("🪑 Свободные места", f"adm:desks:{office_id}"),),
        (button("🔄 Перегенерировать", f"adm:regen:{office_id}"),),
        (button("🎲 Перемешать заново", f"adm:reseed:{office_id}"),),
        (button("👀 Предпросмотр", f"adm:preview:{office_id}"),),
        (button("✉️ Тестовое напоминание", f"adm:test:{office_id}"),),
        (button("⚙️ Настройки офиса", f"adm:oset:{office_id}"),),
        (button("‹ Назад", "adm:offices"),),
    )


def office_settings_menu(office_id: str, *, owner: bool, active: bool) -> InlineKeyboardMarkup:
    """Everything structural about an office, one level below the everyday screen.

    Closing and deleting live here rather than beside «Перегенерировать» — the two are a
    thumb's width apart and one of them cannot be undone.
    """
    rows = [
        (button("✏️ Переименовать", f"adm:orename:{office_id}"),),
        (button("💬 Чат офиса", f"adm:ochat:{office_id}"),),
        (button("📅 Производственный календарь", f"adm:ocal:{office_id}"),),
    ]
    if owner:
        rows.append(
            (button("♻️ Открыть снова", f"adm:oopen:{office_id}"),)
            if not active
            else (button("🚪 Закрыть офис", f"adm:oclose:{office_id}"),)
        )
        rows.append((button("🗑 Удалить навсегда", f"adm:odrop:{office_id}"),))
    return keyboard(*rows, (button("‹ Назад", f"adm:office:{office_id}"),))


def chat_menu(office_id: str, *, bound: bool) -> InlineKeyboardMarkup:
    rows = [(button("✍️ Ввести id вручную", f"adm:ochatid:{office_id}"),)]
    if bound:
        rows.append((button("🚫 Отвязать", f"adm:ochatno:{office_id}"),))
    return keyboard(*rows, (button("‹ Назад", f"adm:oset:{office_id}"),))


def calendar_picker(office_id: str, codes: Sequence[str], current: str) -> InlineKeyboardMarkup:
    """A fixed list, never free text.

    An unknown country code makes the holiday library return nothing at all, so a typo
    would be a silent "no public holidays, ever" — which nobody would notice until a
    public holiday came and went with the office scheduled as usual.
    """
    rows = [
        (button(f"{'✅' if code == current else '▫️'} {code}", f"adm:ocalpick:{office_id}:{code}"),)
        for code in codes
    ]
    return keyboard(*rows, (button("‹ Назад", f"adm:oset:{office_id}"),))


def relay_picker(entries: Sequence[tuple[str, str]], *, done: bool) -> InlineKeyboardMarkup:
    """One button per group, carrying an office id — never a chat id.

    The bottom button ends the flow either way. Before anything is sent that is a cancel;
    after, it is the end, and calling it «Отмена» would read as an offer to unsend.
    """
    rows = [(button(label, f"adm:rto:{office_id}"),) for office_id, label in entries]
    last = button("✔️ Готово", "adm:cancel") if done else button("✖️ Отмена", "adm:cancel")
    return keyboard(*rows, (last,))


def times_menu() -> InlineKeyboardMarkup:
    """One button per time. Each opens a typed prompt; the data names what it edits."""
    return keyboard(
        (button("📣 Выход в офис", "adm:time:attendance"),),
        (button("📝 Tempo", "adm:time:tempo"), button("🏁 Конец дня", "adm:time:end")),
        (button("🎉 Праздники", "adm:time:holiday"), button("🔄 Продление", "adm:time:extend")),
        (button("‹ Назад", "adm:menu"),),
    )


def limits_menu() -> InlineKeyboardMarkup:
    return keyboard(
        (button("✏️ По умолчанию", "adm:limdef"),),
        (button("✏️ Общий предел", "adm:limcap"),),
        (button("‹ Назад", "adm:menu"),),
    )


def employee_list(office_id: str, entries: Sequence[tuple[str, str]]) -> InlineKeyboardMarkup:
    """One button per person, plus a way to add one.

    Callback data carries only the employee id — the office is derived from it. Telegram
    caps callback data at 64 bytes, and an office id plus an employee id can exceed that.
    """
    rows = [(button(label, f"adm:empv:{employee_id}"),) for employee_id, label in entries]
    return keyboard(
        *rows,
        (button("➕ Добавить", f"adm:empadd:{office_id}"),),
        (button("‹ Назад", f"adm:office:{office_id}"),),
    )


def employee_card(office_id: str, employee_id: str, *, departed: bool) -> InlineKeyboardMarkup:
    tenure = (
        button("♻️ Восстановить", f"adm:emprest:{employee_id}")
        if departed
        else button("🚪 Уволить", f"adm:empfire:{employee_id}")
    )
    return keyboard(
        (button("✏️ Переименовать", f"adm:empname:{employee_id}"),),
        (button("🔗 Telegram-ник", f"adm:empuser:{employee_id}"),),
        (button("🤖 Лимит ИИ", f"adm:limemp:{employee_id}"),),
        (tenure,),
        (button("‹ Назад", f"adm:emp:{office_id}"),),
    )


def gender_picker() -> InlineKeyboardMarkup:
    """The cancel row is not decoration.

    This is the only keyboard on screen mid-flow, and without it the only way out of a
    half-finished hire is knowing to type /cancel.
    """
    return keyboard(
        (button("♂️ Мужской", "adm:empg:male"), button("♀️ Женский", "adm:empg:female")),
        (button("✖️ Отмена", "adm:cancel"),),
    )


def weekday_picker(office_id: str, action: str, values: dict[int, str]) -> InlineKeyboardMarkup:
    rows = [
        (
            button(
                f"{WEEKDAY_LABELS[weekday]}: {values.get(weekday, '—')}",
                f"{action}:{office_id}:{weekday}",
            ),
        )
        for weekday in range(5)
    ]
    return keyboard(*rows, (button("‹ Назад", f"adm:office:{office_id}"),))


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Attached to every prompt that reads free text.

    Typing /cancel works too, but a button is what people reach for — and typing it was
    how an employee once got renamed to "/cancel".
    """
    return keyboard((button("✖️ Отмена", "adm:cancel"),))


def confirm(action: str, *, back: str) -> InlineKeyboardMarkup:
    return keyboard(
        (button("✅ Да", action), button("✖️ Отмена", back)),
    )


def employee_menu() -> InlineKeyboardMarkup:
    return keyboard(
        (button("🌴 Мои отпуска", "me:absences"),),
        (button("📅 Когда я в офисе", "me:days"),),
        (button("🏢 Расписание офиса", "me:office"),),
    )


def absence_list(entries: Sequence[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [(button(f"🗑 {label}", f"me:absdel:{absence_id}"),) for absence_id, label in entries]
    return keyboard(*rows, (button("➕ Добавить", "me:absadd"),), (button("‹ Назад", "me:menu"),))
