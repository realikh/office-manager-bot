#!/usr/bin/env python3
"""Convert office-rotation-bot location configs into Табельщик office seeds.

Run once, review the output by hand, then commit it. Schedules are not carried over —
they are regenerated — and neither is notification state, which is meaningless under the
new job ledger.

    uv run python scripts/migrate_from_old_bot.py \
        --src ~/Developer/office-rotation-bot/configs/locations \
        --out config/offices
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import yaml

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class Report:
    """Collects warnings so the operator sees everything at once, not one per run."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)


def convert(raw: dict[str, Any], report: Report, *, today: date) -> dict[str, Any]:
    office_id = raw.get("id")
    if not office_id:
        raise ValueError("location config has no 'id'")

    employees, team_of = _convert_employees(raw.get("employeeGroups") or [], report, office_id)
    known = {employee["id"] for employee in employees}

    fixed = _convert_fixed_schedule(raw.get("fixedSchedule") or {}, known, report, office_id)
    desks = _convert_vacant_desks(raw.get("vacantDesks") or {}, fixed, report, office_id)
    absences = _convert_vacations(raw.get("vacations") or [], known, report, office_id, today)
    calendar = _convert_calendar(raw.get("calendarOverrides") or {})

    seed: dict[str, Any] = {
        "id": office_id,
        "name": raw.get("name", office_id),
    }
    if raw.get("address"):
        seed["address"] = raw["address"]

    # The old bot posted every office into one shared chat, so there is nothing to carry
    # over here. Left explicitly null so it fails validation until it is filled in.
    seed["chatId"] = None
    seed["timezone"] = "Asia/Almaty"
    seed["holidayCalendar"] = "KZ"
    seed["seedNonce"] = int((raw.get("schedule") or {}).get("seed", 0)) % 100_000

    schedule: dict[str, Any] = {}
    if fixed:
        schedule["fixed"] = fixed
    if desks:
        schedule["vacantDesks"] = desks
    seed["schedule"] = schedule

    if calendar:
        seed["calendar"] = calendar
    seed["employees"] = employees
    if absences:
        seed["absences"] = absences

    if team_of:
        report.note(f"{office_id}: kept {len(set(team_of.values()))} team ids as teamId")

    return seed


def _convert_employees(
    groups: Iterable[dict[str, Any]], report: Report, office_id: str
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    employees: list[dict[str, Any]] = []
    team_of: dict[str, str] = {}

    for group in groups:
        team_id = group.get("id")
        for person in group.get("employees") or []:
            entry: dict[str, Any] = {
                "id": person["id"],
                "name": person["name"],
            }
            if person.get("telegramUsername"):
                entry["telegramUsername"] = person["telegramUsername"]
            if person.get("gender"):
                entry["gender"] = person["gender"]
            if team_id:
                entry["teamId"] = team_id
                team_of[person["id"]] = team_id
            if person.get("startDate"):
                entry["startedOn"] = person["startDate"]
            # lastWorkDate was inclusive, and so is endedOn — a straight rename.
            if person.get("lastWorkDate"):
                entry["endedOn"] = person["lastWorkDate"]
                report.note(f"{office_id}: {person['id']} left on {person['lastWorkDate']}")
            employees.append(entry)

    return employees, team_of


def _convert_fixed_schedule(
    fixed: dict[str, Any], known: set[str], report: Report, office_id: str
) -> dict[str, list[str]]:
    converted: dict[str, list[str]] = {}
    for weekday, ids in fixed.items():
        if weekday not in WEEKDAYS:
            report.warn(f"{office_id}: ignoring unknown weekday {weekday!r} in fixedSchedule")
            continue
        unknown = [employee_id for employee_id in ids if employee_id not in known]
        if unknown:
            report.warn(
                f"{office_id}: fixedSchedule.{weekday} names people who are not on the "
                f"roster and were dropped: {unknown}"
            )
        kept = [employee_id for employee_id in ids if employee_id in known]
        if kept:
            converted[weekday] = kept
    return converted


def _convert_vacant_desks(
    desks: dict[str, Any],
    fixed: dict[str, list[str]],
    report: Report,
    office_id: str,
) -> dict[str, int]:
    converted: dict[str, int] = {}
    for weekday, count in desks.items():
        if weekday not in WEEKDAYS:
            report.warn(f"{office_id}: ignoring unknown weekday {weekday!r} in vacantDesks")
            continue

        fixed_that_day = len(fixed.get(weekday, []))
        if fixed_that_day:
            # The old code treated vacantDesks as the day's total capacity and filled the
            # remainder; the new one treats it as extra draftees on top of the fixed list.
            # Where both exist the number means something different, so flag it loudly.
            adjusted = max(0, int(count) - fixed_that_day)
            report.warn(
                f"{office_id}: {weekday} has {fixed_that_day} fixed people and "
                f"vacantDesks={count}. The old bot read that as a total capacity; the new "
                f"one reads it as extra draftees. Converted to {adjusted} — check this."
            )
            converted[weekday] = adjusted
        else:
            converted[weekday] = int(count)
    return converted


def _convert_vacations(
    vacations: Iterable[dict[str, Any]],
    known: set[str],
    report: Report,
    office_id: str,
    today: date,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for vacation in vacations:
        employee_id = vacation.get("employeeId")
        if employee_id not in known:
            report.warn(f"{office_id}: vacation for unknown employee {employee_id!r} dropped")
            continue

        end = vacation["endDate"]
        end_date = end if isinstance(end, date) else date.fromisoformat(str(end))
        if end_date < today:
            report.note(f"{office_id}: dropped {employee_id}'s vacation ending {end_date} (past)")
            continue

        converted.append(
            {
                "employeeId": employee_id,
                "startDate": vacation["startDate"],
                "endDate": vacation["endDate"],
                "kind": "vacation",
            }
        )
    return converted


def _convert_calendar(overrides: dict[str, Any]) -> dict[str, Any]:
    calendar: dict[str, Any] = {}
    if overrides.get("excludedDates"):
        calendar["closed"] = list(overrides["excludedDates"])
    if overrides.get("includedDates"):
        calendar["extraWorkdays"] = list(overrides["includedDates"])
    return calendar


def dump(seed: dict[str, Any]) -> str:
    header = (
        "# Generated by scripts/migrate_from_old_bot.py — review before use.\n"
        "# Seed data only: loaded into the database on first run, after which the\n"
        "# database is authoritative and this file is a reproducible starting point.\n"
        "# chatId must be filled in: each office now posts to its own Telegram chat.\n"
    )
    body = yaml.safe_dump(seed, allow_unicode=True, sort_keys=False, width=100)
    return header + body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        type=Path,
        default=Path.home() / "Developer/office-rotation-bot/configs/locations",
        help="the old bot's configs/locations directory",
    )
    parser.add_argument("--out", type=Path, default=Path("config/offices"))
    parser.add_argument("--force", action="store_true", help="overwrite existing output files")
    args = parser.parse_args(argv)

    if not args.src.is_dir():
        print(f"source directory not found: {args.src}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    report = Report()
    today = date.today()
    written: list[Path] = []

    for path in sorted(args.src.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        seed = convert(raw, report, today=today)

        destination = args.out / f"{seed['id']}.yaml"
        if destination.exists() and not args.force:
            print(f"refusing to overwrite {destination} (use --force)", file=sys.stderr)
            return 1

        destination.write_text(dump(seed), encoding="utf-8")
        written.append(destination)
        print(f"{path.name} -> {destination}  ({len(seed['employees'])} employees)")

    for note in report.notes:
        print(f"note: {note}")
    for warning in report.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)

    print(f"\nWrote {len(written)} office seed files.")
    print("Next: fill in chatId for each office, then run `tabelshchik validate`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
