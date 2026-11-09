from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from tabelshchik.application import memory
from tabelshchik.application.policy import ChatPolicy
from tabelshchik.application.ports import (
    Clock,
    Note,
    NoteScope,
    NoteStore,
    OfficeStore,
    ScheduleStore,
    ToolCall,
    ToolSpec,
)
from tabelshchik.application.voice import CommonText
from tabelshchik.domain.entities import Employee, Office

SCHEDULE = "schedule"
RECALL = "recall"
CALLS_PER_ROUND = 3
SPAN_DAYS = 62
SKIPPED = "Пропущено: за один раз можно сделать не больше трех запросов."
CUT = "Дальше не поместилось. Спроси с датами поуже."


@dataclass(frozen=True, slots=True)
class Toolbox:
    user_id: int
    employee: Employee | None
    home: str | None
    office_id: str
    reach: tuple[Office, ...]
    offices: OfficeStore
    schedule: ScheduleStore
    notes: NoteStore | None
    clock: Clock
    policy: ChatPolicy
    common: CommonText

    @property
    def remembers(self) -> bool:
        return self.notes is not None and self.policy.memory


def specs(box: Toolbox) -> tuple[ToolSpec, ...]:
    schedule_properties: dict[str, object] = {
        "from": {
            "type": "string",
            "description": "Первый день, YYYY-MM-DD. По умолчанию сегодня.",
        },
        "to": {
            "type": "string",
            "description": "Последний день, YYYY-MM-DD. По умолчанию все расписание вперед.",
        },
    }
    if len(box.reach) > 1:
        schedule_properties["office"] = {
            "type": "string",
            "enum": [office.name for office in box.reach],
            "description": "Офис. По умолчанию офис собеседника.",
        }
    tools = [
        ToolSpec(
            SCHEDULE,
            "Расписание офиса: кто в какие дни выходит в офис, и отдельно дни собеседника. "
            "Расписание составлено примерно на шесть недель вперед, прошлые дни тоже есть. "
            "Формат строки: пн 10 ноября: имена.",
            {"type": "object", "properties": schedule_properties, "additionalProperties": False},
        )
    ]
    if box.remembers:
        tools.append(
            ToolSpec(
                RECALL,
                "Твои заметки: то, что тебе раньше рассказывали о собеседнике (me) и об "
                "офисе (office). Дни рождения, предпочтения, договоренности, что сломано, "
                "что планируется.",
                {
                    "type": "object",
                    "properties": {
                        "about": {"type": "string", "enum": ["me", "office", "both"]},
                    },
                    "additionalProperties": False,
                },
            )
        )
    return tuple(tools)


def run(call: ToolCall, box: Toolbox) -> str:
    arguments = _arguments(call.arguments)
    if arguments is None:
        return "Ошибка: аргументы должны быть JSON-объектом."
    if call.name == SCHEDULE:
        return _schedule(arguments, box)
    if call.name == RECALL and box.remembers:
        return _recall(arguments, box)
    return f"Ошибка: инструмента {call.name} нет."


def _schedule(arguments: dict[str, object], box: Toolbox) -> str:
    office = _office(arguments.get("office"), box)
    if office is None:
        return "Ошибка: такого офиса здесь нет."

    today = box.clock.today()
    start = _day(arguments.get("from")) or today
    end = _day(arguments.get("to")) or start + timedelta(days=SPAN_DAYS - 1)
    if end < start:
        start, end = end, start
    end = min(end, start + timedelta(days=SPAN_DAYS - 1))

    days = box.schedule.days_between(office.id, start, end)
    names = {employee.id: employee.full_name for employee in box.offices.employees(office.id)}
    common = box.common
    planned_until = max((snapshot.day for snapshot in days), default=None)

    lines = [f"Офис «{office.name}», с {_short(start, common)} по {_short(end, common)}."]
    if planned_until is None:
        lines.append("На эти даты расписания нет.")
    elif planned_until < end:
        lines.append(
            f"Расписание составлено по {_short(planned_until, common)}, дальше его пока нет."
        )
    if box.employee is not None and box.home == office.id:
        mine = [snapshot.day for snapshot in days if box.employee.id in snapshot.roster]
        lines.append(
            "Дни собеседника: " + ", ".join(_short(day, common) for day in mine) + "."
            if mine
            else "У собеседника в эти даты нет дней в офисе."
        )
    rows = [
        f"{_short(snapshot.day, common)}: "
        + ", ".join(sorted((names.get(eid, eid) for eid in snapshot.roster), key=str.casefold))
        for snapshot in days
        if snapshot.roster
    ]
    lines.extend(rows or ["В эти даты в офис никто не записан."])
    lines.append("Дней, которых нет в списке, никто не занимает, или они нерабочие.")
    return _fit(lines, box.policy.lookup_chars)


def _recall(arguments: dict[str, object], box: Toolbox) -> str:
    assert box.notes is not None
    about = str(arguments.get("about") or "both").strip().lower()
    sections: list[tuple[str, str, Sequence[Note]]] = []
    if about in ("me", "both"):
        sections.append(
            (
                "Заметки о собеседнике, их видит только он сам:",
                "Заметок о собеседнике нет.",
                box.notes.notes(
                    NoteScope.PERSON, str(box.user_id), limit=box.policy.personal_notes
                ),
            )
        )
    if about in ("office", "both"):
        office = box.offices.get_office(box.office_id)
        name = office.name if office is not None else box.office_id
        sections.append(
            (
                f"Заметки об офисе «{name}»:",
                "Заметок об офисе нет.",
                box.notes.notes(NoteScope.OFFICE, box.office_id, limit=box.policy.office_notes),
            )
        )
    if not sections:
        return 'Ошибка: about может быть "me", "office" или "both".'

    lines: list[str] = []
    shown: list[int] = []
    spent = 0
    for heading, empty, notes in sections:
        lines.append(heading)
        spent += len(heading) + 1
        if not notes:
            lines.append(empty)
            spent += len(empty) + 1
            continue
        for note in notes:
            line = memory.render_note(note, box.common.months)
            if spent + len(line) + 1 > box.policy.lookup_chars:
                lines.append("Более старые заметки не поместились.")
                break
            lines.append(line)
            spent += len(line) + 1
            shown.append(note.id)

    box.notes.touch(shown, at=box.clock.now())
    return "\n".join(lines)


def _office(raw: object, box: Toolbox) -> Office | None:
    if raw is None or raw == "":
        wanted = box.home if box.home is not None else box.office_id
        return next((office for office in box.reach if office.id == wanted), None)
    name = str(raw).strip().casefold()
    return next((office for office in box.reach if office.name.casefold() == name), None)


def _arguments(raw: str) -> dict[str, object] | None:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _day(raw: object) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _short(day: date, common: CommonText) -> str:
    return f"{common.weekdays_short[day.weekday()]} {day.day} {common.months[day.month - 1]}"


def _fit(lines: Sequence[str], limit: int) -> str:
    kept: list[str] = []
    spent = 0
    for line in lines:
        if spent + len(line) + 1 > limit - len(CUT):
            kept.append(CUT)
            break
        kept.append(line)
        spent += len(line) + 1
    return "\n".join(kept)
