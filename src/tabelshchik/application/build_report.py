"""Assembling the data a schedule workbook needs.

Kept apart from the spreadsheet writer so the interesting part — what counts as a
statistic, how the spread is computed — is testable without opening a workbook.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

from tabelshchik.domain.calendar import CalendarSpec, date_range
from tabelshchik.domain.entities import SCALE, Employee, LedgerEntry


@dataclass(frozen=True, slots=True)
class ReportEmployee:
    id: str
    name: str
    #: Stable across regenerations, so a person keeps their colour.
    colour_index: int
    surplus_scaled: int = 0


@dataclass(frozen=True, slots=True)
class ReportDay:
    day: date
    weekday: int
    attendees: tuple[str, ...]
    desks_required: int = 0
    shortfall: int = 0
    #: Already announced, so shown as settled rather than provisional.
    committed: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleReport:
    office_name: str
    start: date
    end: date
    employees: tuple[ReportEmployee, ...]
    days: tuple[ReportDay, ...]
    skipped: tuple[tuple[date, str], ...] = ()
    generated_at: date | None = None
    seed: int = 0
    fingerprint: str = ""
    stats: Statistics = field(default_factory=lambda: Statistics())

    def employee(self, employee_id: str) -> ReportEmployee | None:
        return next((e for e in self.employees if e.id == employee_id), None)


@dataclass(frozen=True, slots=True)
class Statistics:
    totals: Mapping[str, int] = field(default_factory=dict)
    per_weekday: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    first_day: Mapping[str, date] = field(default_factory=dict)
    last_day: Mapping[str, date] = field(default_factory=dict)
    mean_gap: Mapping[str, float] = field(default_factory=dict)
    desks_offered: int = 0
    desks_filled: int = 0

    @property
    def shortfall(self) -> int:
        return self.desks_offered - self.desks_filled

    @property
    def spread(self) -> int:
        """Max minus min attendance. The number that shows whether this is working."""
        if not self.totals:
            return 0
        return max(self.totals.values()) - min(self.totals.values())

    def share(self, employee_id: str) -> float:
        total = sum(self.totals.values())
        return (self.totals.get(employee_id, 0) / total) if total else 0.0


def build_report(
    *,
    office_name: str,
    start: date,
    end: date,
    employees: Sequence[Employee],
    days: Sequence[ReportDay],
    spec: CalendarSpec,
    ledger: Mapping[str, LedgerEntry] | None = None,
    seed: int = 0,
    fingerprint: str = "",
    generated_at: date | None = None,
) -> ScheduleReport:
    ledger = ledger or {}
    # Someone who left before the period began, or joins after it ends, would otherwise
    # sit at zero days and make the spread look terrible for no reason.
    roster = sorted(
        (employee for employee in employees if _overlaps_period(employee, start, end)),
        key=lambda employee: employee.full_name.casefold(),
    )

    report_employees = tuple(
        ReportEmployee(
            id=employee.id,
            name=employee.full_name,
            colour_index=index,
            surplus_scaled=ledger[employee.id].surplus_scaled if employee.id in ledger else 0,
        )
        for index, employee in enumerate(roster)
    )

    return ScheduleReport(
        office_name=office_name,
        start=start,
        end=end,
        employees=report_employees,
        days=tuple(days),
        skipped=_skipped_days(spec, start, end),
        stats=compute_statistics(report_employees, days),
        seed=seed,
        fingerprint=fingerprint,
        generated_at=generated_at,
    )


def compute_statistics(
    employees: Sequence[ReportEmployee], days: Sequence[ReportDay]
) -> Statistics:
    totals: dict[str, int] = {employee.id: 0 for employee in employees}
    per_weekday: dict[str, list[int]] = {employee.id: [0] * 7 for employee in employees}
    appearances: dict[str, list[date]] = {employee.id: [] for employee in employees}

    for day in days:
        for employee_id in day.attendees:
            if employee_id not in totals:
                continue
            totals[employee_id] += 1
            per_weekday[employee_id][day.weekday] += 1
            appearances[employee_id].append(day.day)

    return Statistics(
        totals=totals,
        per_weekday={key: tuple(value) for key, value in per_weekday.items()},
        first_day={key: min(value) for key, value in appearances.items() if value},
        last_day={key: max(value) for key, value in appearances.items() if value},
        mean_gap={key: _mean_gap(value) for key, value in appearances.items() if len(value) > 1},
        desks_offered=sum(day.desks_required for day in days),
        desks_filled=sum(len(day.attendees) for day in days),
    )


def _overlaps_period(employee: Employee, start: date, end: date) -> bool:
    if employee.started_on is not None and employee.started_on > end:
        return False
    return not (employee.ended_on is not None and employee.ended_on < start)


def surplus_days(employee: ReportEmployee) -> float:
    return employee.surplus_scaled / SCALE


def _mean_gap(days: Sequence[date]) -> float:
    ordered = sorted(days)
    gaps = [(later - earlier).days for earlier, later in pairwise(ordered)]
    return sum(gaps) / len(gaps) if gaps else 0.0


def _skipped_days(spec: CalendarSpec, start: date, end: date) -> tuple[tuple[date, str], ...]:
    """Days inside the period the office was shut, with the reason.

    Weekends are left out: nobody needs a report telling them Saturday was a Saturday.
    """
    return tuple(
        (day, spec.reason(day))
        for day in date_range(start, end)
        if not spec.is_working_day(day) and day.weekday() < 5
    )
