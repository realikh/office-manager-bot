"""A year-long fairness simulation.

This is a first-class artifact, not merely a test fixture: it is how every future change
to the solver gets evaluated. It replays a full year of weekly regenerations — with
absences, a mid-year joiner and a leaver — and reports how the surplus spread behaves
over time.

The failure mode it exists to catch is the one the previous implementation had: a spread
that grows monotonically, week after week, because fairness state was reset on every
regeneration instead of being carried.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from tabelshchik.domain.calendar import CalendarSpec, start_of_week
from tabelshchik.domain.entities import (
    Absence,
    AbsencePolicy,
    Employee,
    LedgerEntry,
    Office,
    WeeklyTemplate,
)
from tabelshchik.domain.instance import build_instance
from tabelshchik.domain.ledger import DayRecord, apply_day, merge_checkpoint, spread_scaled
from tabelshchik.domain.solver import solve


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    weeks: int = 52
    horizon_weeks: int = 6
    employees: int = 12
    desks: Mapping[int, int] = field(default_factory=lambda: dict.fromkeys(range(5), 4))
    fixed: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    absence_chance: float = 0.04
    max_absence_days: int = 10
    max_days_per_week: int = 5
    absence_policy: AbsencePolicy = AbsencePolicy.NO_DEBT
    seed: int = 20260911
    #: Drop day records older than this many weeks, folding them into a checkpoint.
    retain_weeks: int | None = None
    start: date = date(2026, 1, 5)


@dataclass(frozen=True, slots=True)
class WeekSnapshot:
    week: int
    anchor: date
    spread_scaled: int
    attended: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class SimulationResult:
    snapshots: tuple[WeekSnapshot, ...]
    ledger: Mapping[str, LedgerEntry]
    #: Day records still retained after pruning — what a rebuild would replay.
    records: tuple[DayRecord, ...]
    #: Everything older than the retention window, folded down.
    checkpoint: Mapping[str, LedgerEntry]
    roster: tuple[Employee, ...]

    @property
    def final_spread_scaled(self) -> int:
        return self.snapshots[-1].spread_scaled if self.snapshots else 0

    def spread_series(self) -> tuple[int, ...]:
        return tuple(snapshot.spread_scaled for snapshot in self.snapshots)

    def comparable_spread_scaled(self, employee_ids: Sequence[str]) -> int:
        """Spread among a chosen cohort — used to exclude a joiner or leaver, whose
        surplus is legitimately different because their tenure is."""
        return spread_scaled([self.ledger[eid] for eid in employee_ids if eid in self.ledger])


def default_roster(config: SimulationConfig) -> list[Employee]:
    """A steady cohort, plus one person who joins mid-year and one who leaves."""
    people = [Employee(id=f"e{i:02d}", full_name=f"Сотрудник {i}") for i in range(config.employees)]
    people.append(
        Employee(
            id="joiner",
            full_name="Новенький",
            started_on=config.start + timedelta(weeks=config.weeks // 2),
        )
    )
    people.append(
        Employee(
            id="leaver",
            full_name="Уходящий",
            ended_on=config.start + timedelta(weeks=int(config.weeks * 0.6)),
        )
    )
    return people


def run(
    config: SimulationConfig | None = None,
    *,
    roster: Sequence[Employee] | None = None,
    absences: Sequence[Absence] | None = None,
    spec: CalendarSpec | None = None,
) -> SimulationResult:
    """Replay ``config.weeks`` of weekly regeneration and report what happened."""
    config = config or SimulationConfig()
    spec = spec or CalendarSpec()
    people = list(roster) if roster is not None else default_roster(config)
    template = WeeklyTemplate(fixed=dict(config.fixed), vacant_desks=dict(config.desks))
    office = Office(id="sim", name="Симуляция")

    schedule_absences = list(absences) if absences is not None else _random_absences(config, people)

    ledger: dict[str, LedgerEntry] = {}
    checkpoint: dict[str, LedgerEntry] = {}
    records: list[DayRecord] = []
    snapshots: list[WeekSnapshot] = []
    frozen: dict[date, frozenset[str]] = {}
    attended_total: dict[str, int] = {}

    anchor = start_of_week(config.start)

    for week in range(config.weeks):
        instance = build_instance(
            office=office,
            template=template,
            employees=people,
            absences=schedule_absences,
            spec=spec,
            anchor=anchor,
            horizon_weeks=config.horizon_weeks,
            ledger=ledger,
            frozen=frozen,
            max_days_per_week=config.max_days_per_week,
            absence_policy=config.absence_policy,
        )
        result = solve(instance)

        # The anchor week is now in the past: commit it to history and to the ledger.
        for plan in instance.days:
            if plan.week != 0:
                continue
            attended = plan.fixed | plan.frozen | frozenset(result.on(plan.day))
            record = DayRecord.from_plan(plan, attended)
            records.append(record)
            ledger = apply_day(ledger, record)
            for employee_id in attended:
                attended_total[employee_id] = attended_total.get(employee_id, 0) + 1

        # Next week has been announced, so it is frozen against the following regeneration.
        frozen = {
            plan.day: frozenset(result.on(plan.day)) for plan in instance.days if plan.week == 1
        }

        snapshots.append(
            WeekSnapshot(
                week=week,
                anchor=anchor,
                spread_scaled=spread_scaled(ledger.values()),
                attended=dict(attended_total),
            )
        )

        if config.retain_weeks is not None:
            checkpoint, records = _prune(
                checkpoint, records, cutoff=anchor - timedelta(weeks=config.retain_weeks)
            )

        anchor += timedelta(days=7)

    return SimulationResult(
        snapshots=tuple(snapshots),
        ledger=ledger,
        records=tuple(records),
        checkpoint=checkpoint,
        roster=tuple(people),
    )


def _prune(
    checkpoint: Mapping[str, LedgerEntry],
    records: Sequence[DayRecord],
    *,
    cutoff: date,
) -> tuple[dict[str, LedgerEntry], list[DayRecord]]:
    """Fold day records older than ``cutoff`` into the checkpoint, then drop them.

    This is what keeps the database bounded without making fairness unverifiable: the
    checkpoint is one row per employee, never pruned, and a rebuild resumes from it.
    """
    stale = [record for record in records if record.day < cutoff]
    if not stale:
        return dict(checkpoint), list(records)

    folded: dict[str, LedgerEntry] = {}
    for record in sorted(stale, key=lambda item: item.day):
        folded = apply_day(folded, record)

    return (
        merge_checkpoint(checkpoint, folded),
        [record for record in records if record.day >= cutoff],
    )


def _random_absences(config: SimulationConfig, people: Sequence[Employee]) -> list[Absence]:
    rng = random.Random(config.seed)
    horizon_end = config.start + timedelta(weeks=config.weeks + config.horizon_weeks)

    absences: list[Absence] = []
    for employee in people:
        day = config.start
        while day < horizon_end:
            if rng.random() < config.absence_chance:
                length = rng.randint(1, config.max_absence_days)
                absences.append(
                    Absence(
                        employee_id=employee.id,
                        start_date=day,
                        end_date=day + timedelta(days=length - 1),
                    )
                )
                day += timedelta(days=length)
            day += timedelta(days=1)
    return absences
