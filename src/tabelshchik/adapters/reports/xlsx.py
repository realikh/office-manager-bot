"""The schedule workbook.

Four sheets, all in Russian. The one that matters is Статистика: it ends with the
max-minus-min spread, three-colour formatted, which is the single number that shows
whether the fairness algorithm is doing its job. A workbook that looked pretty but hid
that number would be worse than no workbook.
"""

from __future__ import annotations

import html
import io
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

import xlsxwriter

from tabelshchik.application.build_report import (
    ScheduleReport,
    surplus_days,
)
from tabelshchik.application.voice import Catalog, CommonText, format_date, render_days

#: Print-safe pastels, light enough to read black text on. A person keeps their colour
#: across regenerations because the index comes from a stable roster ordering.
PALETTE = (
    "#BFD8F2",
    "#C8E6C9",
    "#FFE0B2",
    "#F8BBD0",
    "#D1C4E9",
    "#B2DFDB",
    "#FFF9C4",
    "#D7CCC8",
    "#CFD8DC",
    "#F0C4C4",
    "#C5CAE9",
    "#DCEDC8",
    "#FFCCBC",
    "#E1BEE7",
    "#B3E5FC",
    "#F5E1A4",
    "#CDE7D8",
    "#E6D0F0",
    "#BBDEFB",
    "#FFD9C0",
)

HEADER_BG = "#37474F"
HEADER_FG = "#FFFFFF"
GOOD, WARN, BAD = "#C8E6C9", "#FFE0B2", "#FFCDD2"


def build_workbook(report: ScheduleReport, common: CommonText) -> bytes:
    buffer = io.BytesIO()
    book = xlsxwriter.Workbook(buffer, {"in_memory": True, "default_date_format": "dd.mm.yyyy"})

    book.set_properties(
        {
            "title": f"Расписание — {report.office_name}",
            "comments": f"seed {report.seed}; fingerprint {report.fingerprint}",
        }
    )

    styles = _styles(book)
    colours = {
        employee.id: book.add_format(
            {"bg_color": PALETTE[employee.colour_index % len(PALETTE)], "border": 1}
        )
        for employee in report.employees
    }

    _schedule_sheet(book, report, common, styles, colours)
    _compact_sheet(book, report, common, styles)
    _statistics_sheet(book, report, common, styles)
    if report.skipped:
        _skipped_sheet(book, report, styles)

    book.close()
    return buffer.getvalue()


# XlsxWriter ships no type information, so its workbook and worksheet objects are Any
# here rather than pretending to a precision we cannot check.
def _styles(book: Any) -> dict[str, Any]:
    return {
        "header": book.add_format(
            {
                "bold": True,
                "bg_color": HEADER_BG,
                "font_color": HEADER_FG,
                "border": 1,
                "align": "center",
                "valign": "vcenter",
                "text_wrap": True,
            }
        ),
        "title": book.add_format({"bold": True, "font_size": 14}),
        "label": book.add_format({"bold": True}),
        "date": book.add_format({"num_format": "dd.mm.yyyy", "border": 1}),
        "cell": book.add_format({"border": 1}),
        "wrap": book.add_format({"border": 1, "text_wrap": True, "valign": "top"}),
        "number": book.add_format({"border": 1, "align": "center"}),
        "percent": book.add_format({"border": 1, "num_format": "0.0%"}),
        "decimal": book.add_format({"border": 1, "num_format": "0.0"}),
        "signed": book.add_format({"border": 1, "num_format": "+0.0;-0.0;0.0"}),
        "muted": book.add_format({"border": 1, "font_color": "#78909C", "italic": True}),
    }


def _schedule_sheet(
    book: Any,
    report: ScheduleReport,
    common: CommonText,
    styles: dict[str, Any],
    colours: dict[str, Any],
) -> None:
    sheet = book.add_worksheet("Расписание")
    sheet.freeze_panes(1, 2)

    sheet.write(0, 0, "Дата", styles["header"])
    sheet.write(0, 1, "День", styles["header"])
    for column, employee in enumerate(report.employees, start=2):
        sheet.write(0, column, employee.name, styles["header"])
        sheet.set_column(column, column, 18)

    sheet.set_column(0, 0, 12)
    sheet.set_column(1, 1, 14)

    for row, day in enumerate(report.days, start=1):
        sheet.write_datetime(row, 0, _as_datetime(day.day), styles["date"])
        sheet.write(row, 1, common.weekdays[day.weekday], styles["cell"])
        for column, employee in enumerate(report.employees, start=2):
            if employee.id in day.attendees:
                sheet.write(row, column, "✓", colours[employee.id])
            else:
                sheet.write_blank(row, column, None, styles["cell"])

    if report.days:
        sheet.autofilter(0, 0, len(report.days), len(report.employees) + 1)


def _compact_sheet(
    book: Any, report: ScheduleReport, common: CommonText, styles: dict[str, Any]
) -> None:
    """One row per day, everyone in a single cell. This is the sheet people print."""
    sheet = book.add_worksheet("Компактный вид")
    sheet.freeze_panes(1, 0)
    sheet.set_column(0, 0, 12)
    sheet.set_column(1, 1, 14)
    sheet.set_column(2, 2, 70)

    for column, title in enumerate(("Дата", "День", "Кто в офисе")):
        sheet.write(0, column, title, styles["header"])

    names = {employee.id: employee.name for employee in report.employees}
    for row, day in enumerate(report.days, start=1):
        attendees = [names.get(employee_id, employee_id) for employee_id in day.attendees]
        sheet.write_datetime(row, 0, _as_datetime(day.day), styles["date"])
        sheet.write(row, 1, common.weekdays[day.weekday], styles["cell"])
        sheet.write(row, 2, "\n".join(attendees) or "—", styles["wrap"])
        sheet.set_row(row, max(15, 14 * max(1, len(attendees))))


def _statistics_sheet(
    book: Any, report: ScheduleReport, common: CommonText, styles: dict[str, Any]
) -> None:
    sheet = book.add_worksheet("Статистика")
    sheet.freeze_panes(1, 1)
    sheet.set_column(0, 0, 26)
    sheet.set_column(1, 12, 11)

    stats = report.stats
    headers = [
        "Сотрудник",
        "Всего дней",
        *(common.weekdays_short[weekday] for weekday in range(5)),
        "Доля",
        "Первый день",
        "Последний день",
        "Средний интервал",
        "Баланс",
    ]
    for column, title in enumerate(headers):
        sheet.write(0, column, title, styles["header"])

    for row, employee in enumerate(report.employees, start=1):
        sheet.write(row, 0, employee.name, styles["cell"])
        sheet.write_number(row, 1, stats.totals.get(employee.id, 0), styles["number"])

        weekdays = stats.per_weekday.get(employee.id, (0,) * 7)
        for weekday in range(5):
            sheet.write_number(row, 2 + weekday, weekdays[weekday], styles["number"])

        sheet.write_number(row, 7, stats.share(employee.id), styles["percent"])

        first, last = stats.first_day.get(employee.id), stats.last_day.get(employee.id)
        _write_optional_date(sheet, row, 8, first, styles)
        _write_optional_date(sheet, row, 9, last, styles)

        gap = stats.mean_gap.get(employee.id)
        if gap is None:
            sheet.write(row, 10, "—", styles["muted"])
        else:
            sheet.write_number(row, 10, gap, styles["decimal"])

        # Surplus, in days: positive means ahead of their fair share, negative behind.
        sheet.write_number(row, 11, surplus_days(employee), styles["signed"])

    _summary_block(book, sheet, report, common, styles, first_row=len(report.employees) + 3)


def _summary_block(
    book: Any,
    sheet: Any,
    report: ScheduleReport,
    common: CommonText,
    styles: dict[str, Any],
    *,
    first_row: int,
) -> None:
    stats = report.stats
    sheet.write(first_row - 1, 0, "Итоги периода", styles["title"])

    rows: list[tuple[str, object]] = [
        ("Офис", report.office_name),
        ("Период", f"{format_date(report.start, common)} — {format_date(report.end, common)}"),
        ("Сотрудников", len(report.employees)),
        ("Всего назначений", stats.desks_filled),
        ("Мест предложено", stats.desks_offered),
        ("Не заполнено", stats.shortfall),
        ("Минимум визитов", min(stats.totals.values(), default=0)),
        ("Максимум визитов", max(stats.totals.values(), default=0)),
        ("Разброс (макс − мин)", stats.spread),
    ]

    for offset, (label, value) in enumerate(rows):
        row = first_row + offset
        sheet.write(row, 0, label, styles["label"])
        if isinstance(value, int):
            sheet.write_number(row, 1, value, styles["number"])
        else:
            sheet.write(row, 1, str(value), styles["cell"])

    # The spread is the headline. Colour it so an unfair schedule is visible at a glance
    # rather than requiring someone to compare two columns by eye.
    spread_row = first_row + len(rows) - 1
    for limit, colour in ((1, GOOD), (2, WARN)):
        sheet.conditional_format(
            spread_row,
            1,
            spread_row,
            1,
            {
                "type": "cell",
                "criteria": "<=",
                "value": limit,
                "format": book.add_format({"bg_color": colour, "border": 1, "align": "center"}),
            },
        )
    sheet.conditional_format(
        spread_row,
        1,
        spread_row,
        1,
        {
            "type": "cell",
            "criteria": ">",
            "value": 2,
            "format": book.add_format({"bg_color": BAD, "border": 1, "align": "center"}),
        },
    )


def _skipped_sheet(book: Any, report: ScheduleReport, styles: dict[str, Any]) -> None:
    sheet = book.add_worksheet("Пропущенные дни")
    sheet.set_column(0, 0, 14)
    sheet.set_column(1, 1, 40)
    sheet.write(0, 0, "Дата", styles["header"])
    sheet.write(0, 1, "Причина", styles["header"])

    for row, (day, reason) in enumerate(report.skipped, start=1):
        sheet.write_datetime(row, 0, _as_datetime(day), styles["date"])
        sheet.write(row, 1, reason, styles["cell"])


def _write_optional_date(
    sheet: Any, row: int, column: int, value: date | None, styles: dict[str, Any]
) -> None:
    if value is None:
        sheet.write(row, column, "—", styles["muted"])
    else:
        sheet.write_datetime(row, column, _as_datetime(value), styles["date"])


def _as_datetime(value: date) -> datetime:
    return datetime(value.year, value.month, value.day)


#: What is left once the caption's own header has taken its share of Telegram's 1024.
CAPTION_BUDGET = 780

#: Shown instead of a half-listed week. The workbook is attached either way.
TRUNCATED = "Полное расписание — в файле."


def upcoming_week(
    report: ScheduleReport,
    common: CommonText,
    *,
    start: date,
    span_days: int = 7,
    limit: int = CAPTION_BUDGET,
    markup: bool = True,
) -> str:
    """The coming week, for the message the workbook is attached to.

    Windowed by **date**, not by how many scheduled days happen to come next. That
    distinction is the whole point: an office that fills desks only on Fridays has seven
    scheduled days six *weeks* out, so taking the first seven listed a month and a half of
    identical rosters — which then overran Telegram's 1024-character caption limit and was
    cut off mid-name.

    Rendered the same way as the admin preview, because there is no reason for the same
    information to look different in two places.
    """
    names = {employee.id: employee.name for employee in report.employees}
    end = start + timedelta(days=span_days - 1)

    listed = [
        (
            day.day,
            sorted((names.get(who, who) for who in day.attendees), key=str.casefold),
        )
        for day in report.days
        if day.attendees and start <= day.day <= end
    ]
    if not listed:
        return ""

    chunks = render_days(listed, common, limit=limit, markup=markup)
    # More than one chunk means the week does not fit. Dropping whole days silently is how
    # the old version lost people; say so instead, since the file has all of it anyway.
    return chunks[0] if len(chunks) == 1 else f"{chunks[0]}\n\n{TRUNCATED}"


#: Telegram's hard cap on a document caption.
CAPTION_LIMIT = 1024


def schedule_caption(
    catalog: Catalog,
    report: ScheduleReport,
    *,
    office_name: str,
    horizon_start: date,
    horizon_end: date,
    week_from: date,
    shortfall: int = 0,
) -> str:
    """The caption for a published workbook, composed to fit rather than truncated.

    A blunt slice at 1024 characters cuts mid-name, which is how this message used to end
    "…Batyrkhan Tok" — and now that the summary carries markup it could cut mid-tag and
    break the formatting of the whole caption. So the week is either included whole or
    replaced by a line saying to open the file.
    """
    common = catalog.common
    week = upcoming_week(report, common, start=week_from)

    def compose(summary: str) -> str:
        return catalog.text("schedule.caption").format(
            office=html.escape(office_name),
            start=format_date(horizon_start, common),
            end=format_date(horizon_end, common),
            summary=summary,
        )

    note = catalog.text("schedule.shortfallNote").format(shortfall=shortfall) if shortfall else ""
    header = catalog.text("schedule.weekHeader")

    parts = [part for part in (f"{header}\n\n{week}" if week else "", note) if part]
    caption = compose("\n\n".join(parts))
    if len(caption) <= CAPTION_LIMIT:
        return caption

    shortened = [part for part in (f"{header}\n\n{TRUNCATED}" if week else "", note) if part]
    return compose("\n\n".join(shortened))[:CAPTION_LIMIT]


def sheet_names(_: Sequence[str] = ()) -> tuple[str, ...]:
    return ("Расписание", "Компактный вид", "Статистика", "Пропущенные дни")
