"""Seeding office data from YAML into the database.

Seeds are a bootstrap, not a live source of truth. Once an office exists in the database
that is where it is edited — from the bot's admin menu — and re-running the seed leaves
it alone unless explicitly told otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from tabelshchik.adapters.db import models
from tabelshchik.config.models import OfficeSeed
from tabelshchik.domain.entities import AbsenceKind


@dataclass(frozen=True, slots=True)
class SeedReport:
    created: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.created or self.replaced)


def seed_offices(
    session: Session,
    seeds: Sequence[OfficeSeed],
    *,
    now: datetime,
    replace_existing: bool = False,
) -> SeedReport:
    created: list[str] = []
    skipped: list[str] = []
    replaced: list[str] = []

    for seed in seeds:
        existing = session.get(models.Office, seed.id)

        if existing is not None and not replace_existing:
            skipped.append(seed.id)
            continue

        if existing is not None:
            # Cascades clear employees, template rows and calendar exceptions with it.
            session.delete(existing)
            session.flush()
            replaced.append(seed.id)
        else:
            created.append(seed.id)

        _insert_office(session, seed, now=now)

    session.flush()
    return SeedReport(created=tuple(created), skipped=tuple(skipped), replaced=tuple(replaced))


def _insert_office(session: Session, seed: OfficeSeed, *, now: datetime) -> None:
    session.add(
        models.Office(
            id=seed.id,
            name=seed.name,
            address=seed.address,
            chat_id=seed.chat_id,
            timezone=seed.timezone,
            holiday_calendar=seed.holiday_calendar,
            seed_nonce=seed.seed_nonce,
            active=seed.active,
        )
    )

    for employee in seed.employees:
        session.add(
            models.Employee(
                id=employee.id,
                office_id=seed.id,
                full_name=employee.name,
                telegram_username=employee.telegram_username,
                telegram_user_id=employee.telegram_user_id,
                gender=employee.gender,
                team_id=employee.team_id,
                started_on=employee.started_on,
                ended_on=employee.ended_on,
            )
        )
    session.flush()

    for weekday, desks in seed.schedule.desks_by_weekday.items():
        session.add(
            models.WeeklyTemplateSlot(office_id=seed.id, weekday=weekday, vacant_desks=desks)
        )

    for weekday, employee_ids in seed.schedule.fixed_by_weekday.items():
        for employee_id in employee_ids:
            session.add(
                models.TemplateFixed(office_id=seed.id, weekday=weekday, employee_id=employee_id)
            )

    for absence in seed.absences:
        session.add(
            models.Absence(
                employee_id=absence.employee_id,
                start_date=absence.start_date,
                end_date=absence.end_date,
                kind=AbsenceKind(absence.kind),
                note=absence.note,
                created_at=now,
            )
        )

    _insert_calendar(session, seed)


def _insert_calendar(session: Session, seed: OfficeSeed) -> None:
    for day in seed.calendar.closed:
        session.add(
            models.CalendarException(
                office_id=seed.id,
                day=day,
                kind="CLOSED",
                source="seed",
                note=seed.calendar.notes.get(day, ""),
            )
        )
    for day in seed.calendar.extra_workdays:
        session.add(
            models.CalendarException(
                office_id=seed.id,
                day=day,
                kind="EXTRA_WORKDAY",
                source="seed",
                note=seed.calendar.notes.get(day, ""),
            )
        )


def existing_office_ids(session: Session) -> set[str]:
    return set(session.scalars(select(models.Office.id)).all())
