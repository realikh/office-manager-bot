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


def main_menu() -> InlineKeyboardMarkup:
    return keyboard(
        (button("🏢 Офисы", "adm:offices"),),
        (button("⚖️ Справедливость", "adm:fair"), button("🎭 Настроение", "adm:mood")),
        (button("📊 Состояние", "adm:status"), button("⚙️ Конфиг", "adm:config")),
        (button("💾 Резервная копия", "adm:backup"),),
    )


def office_list(
    offices: Sequence[tuple[str, str]], *, action: str = "adm:office"
) -> InlineKeyboardMarkup:
    rows = [(button(name, f"{action}:{office_id}"),) for office_id, name in offices]
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
        (button("‹ Назад", "adm:offices"),),
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
        (tenure,),
        (button("‹ Назад", f"adm:emp:{office_id}"),),
    )


def gender_picker() -> InlineKeyboardMarkup:
    return keyboard(
        (button("♂️ Мужской", "adm:empg:male"), button("♀️ Женский", "adm:empg:female")),
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
