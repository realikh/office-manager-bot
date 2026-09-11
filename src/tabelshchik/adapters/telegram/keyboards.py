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
