"""Turning the world into a solver instance.

This is where most of the subtle logic lives — eligibility, entitlement accrual, frozen
days, weekly caps — and it is a pure function, so all of it is unit-testable without a
database, a clock or a network.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from tabelshchik.domain.calendar import CalendarSpec, week_index, working_days
from tabelshchik.domain.entities import (
    SCALE,
    Absence,
    AbsencePolicy,
    Employee,
    LedgerEntry,
    Office,
    WeeklyTemplate,
)
from tabelshchik.domain.rng import stable_hash

DEFAULT_MAX_DAYS_PER_WEEK = 5
DEFAULT_SURPLUS_CLAMP_DAYS = 10


@dataclass(frozen=True, slots=True)
class DayPlan:
    """One working day of the horizon, after eligibility has been resolved."""

    day: date
    weekday: int
    week: int
    #: Template-fixed people who are actually available that day.
    fixed: frozenset[str]
    #: Draft assignments already committed (announced, or an admin pin) — never re-decided.
    frozen: frozenset[str]
    #: Who the solver may still draft.
    eligible: frozenset[str]
    #: Everyone who could have been used, which is what entitlement is shared among.
    available: frozenset[str]
    #: Person-days the office consumes that day: fixed heads plus the vacant desks.
    desks_total: int
    #: Vacant desks still to fill after frozen draftees are accounted for.
    demand: int
    #: Entitlement accrued per available head, fixed-point.
    entitlement_scaled: int

    @property
    def committed(self) -> frozenset[str]:
        return self.fixed | self.frozen


@dataclass(frozen=True, slots=True)
class ScheduleInstance:
    """A complete, self-contained fairness problem."""

    office_id: str
    anchor: date
    horizon_end: date
    days: tuple[DayPlan, ...]
    employees: tuple[str, ...]
    #: Projected end-of-horizon surplus before any new drafting, fixed-point.
    base_scaled: Mapping[str, int]
    #: Remaining draftable days per (employee, week) after the hard weekly cap.
    week_cap: Mapping[tuple[str, int], int]
    seed: int

    @property
    def total_demand(self) -> int:
        return sum(day.demand for day in self.days)

    @property
    def needs_draft(self) -> bool:
        """False for a fixed-schedule-only office, which skips the solver entirely."""
        return self.total_demand > 0

    def day_by_date(self, day: date) -> DayPlan | None:
        return next((plan for plan in self.days if plan.day == day), None)


def build_instance(
    *,
    office: Office,
    template: WeeklyTemplate,
    employees: Sequence[Employee],
    absences: Iterable[Absence],
    spec: CalendarSpec,
    anchor: date,
    horizon_weeks: int,
    ledger: Mapping[str, LedgerEntry] | None = None,
    frozen: Mapping[date, frozenset[str]] | None = None,
    max_days_per_week: int = DEFAULT_MAX_DAYS_PER_WEEK,
    surplus_clamp_days: int = DEFAULT_SURPLUS_CLAMP_DAYS,
    absence_policy: AbsencePolicy = AbsencePolicy.NO_DEBT,
) -> ScheduleInstance:
    """Build the solver instance for ``office`` over ``horizon_weeks`` from ``anchor``.

    ``anchor`` is expected to be a Monday; horizons are whole ISO weeks so that the
    per-week fairness layer lines up with how people actually think about their week.
    """
    if horizon_weeks < 1:
        raise ValueError("horizon_weeks must be at least 1")

    ledger = ledger or {}
    frozen = frozen or {}

    horizon_end = anchor + timedelta(days=horizon_weeks * 7 - 1)
    days_in_scope = working_days(spec, anchor, horizon_end)

    absences_by_employee: dict[str, list[Absence]] = {}
    for absence in absences:
        absences_by_employee.setdefault(absence.employee_id, []).append(absence)

    # Only people whose tenure overlaps the horizon at all can matter.
    roster = sorted(
        (e for e in employees if _tenure_overlaps(e, anchor, horizon_end)),
        key=lambda e: e.id,
    )
    by_id = {e.id: e for e in roster}

    def is_absent(employee_id: str, day: date) -> bool:
        return any(a.covers(day) for a in absences_by_employee.get(employee_id, ()))

    plans: list[DayPlan] = []
    for day in days_in_scope:
        weekday = day.weekday()

        available = frozenset(e.id for e in roster if e.in_tenure(day) and not is_absent(e.id, day))
        # ACCRUE_DEBT shares the day's cost among everyone on the roster, so time away
        # still builds an obligation. NO_DEBT (the default) shares it only among the
        # people who could actually have been asked to come in.
        entitled = (
            available
            if absence_policy is AbsencePolicy.NO_DEBT
            else frozenset(e.id for e in roster if e.in_tenure(day))
        )

        fixed = frozenset(
            employee_id
            for employee_id in template.fixed_on(weekday)
            if employee_id in by_id and employee_id in available
        )
        frozen_today = frozenset(
            employee_id
            for employee_id in frozen.get(day, frozenset())
            if employee_id in by_id and employee_id not in fixed
        )

        vacant = template.desks_on(weekday)
        desks_total = len(fixed) + vacant
        demand = max(0, vacant - len(frozen_today))
        eligible = available - fixed - frozen_today

        entitlement_scaled = (desks_total * SCALE // len(entitled)) if entitled else 0

        plans.append(
            DayPlan(
                day=day,
                weekday=weekday,
                week=week_index(anchor, day),
                fixed=fixed,
                frozen=frozen_today,
                eligible=eligible,
                available=entitled,
                desks_total=desks_total,
                demand=demand,
                entitlement_scaled=entitlement_scaled,
            )
        )

    base_scaled = _projected_surplus(roster, plans, ledger, surplus_clamp_days)
    week_cap = _weekly_caps(roster, plans, max_days_per_week)

    return ScheduleInstance(
        office_id=office.id,
        anchor=anchor,
        horizon_end=horizon_end,
        days=tuple(plans),
        employees=tuple(e.id for e in roster),
        base_scaled=base_scaled,
        week_cap=week_cap,
        seed=stable_hash(office.id, anchor.isoformat(), office.seed_nonce),
    )


def _tenure_overlaps(employee: Employee, start: date, end: date) -> bool:
    if employee.started_on is not None and employee.started_on > end:
        return False
    return not (employee.ended_on is not None and employee.ended_on < start)


def _projected_surplus(
    roster: Sequence[Employee],
    plans: Sequence[DayPlan],
    ledger: Mapping[str, LedgerEntry],
    surplus_clamp_days: int,
) -> dict[str, int]:
    """Where each person stands *before* the solver drafts anyone.

    Counting fixed and frozen days here is what makes "if someone already comes in four
    days a week, don't draft them while others come in less" fall out of the arithmetic
    instead of needing a special rule.
    """
    clamp = surplus_clamp_days * SCALE
    base: dict[str, int] = {}

    for employee in roster:
        entry = ledger.get(employee.id)
        # A repair accident or an import shouldn't be able to buy someone a month of
        # punishment duty, so historical surplus is bounded.
        carried = min(max(entry.surplus_scaled, -clamp), clamp) if entry else 0

        committed = sum(1 for plan in plans if employee.id in plan.committed)
        accrued = sum(plan.entitlement_scaled for plan in plans if employee.id in plan.available)
        base[employee.id] = carried + committed * SCALE - accrued

    return base


def _weekly_caps(
    roster: Sequence[Employee],
    plans: Sequence[DayPlan],
    max_days_per_week: int,
) -> dict[tuple[str, int], int]:
    """How many more days each person may be drafted in each week of the horizon.

    A hard cap, independent of fairness: it is what stops "you're in five days this week"
    even in the moment the arithmetic would happily allow it.
    """
    weeks = sorted({plan.week for plan in plans})
    caps: dict[tuple[str, int], int] = {}

    for employee in roster:
        for week in weeks:
            in_week = [plan for plan in plans if plan.week == week]
            committed = sum(1 for plan in in_week if employee.id in plan.committed)
            # Only days that actually have a desk going spare can consume the cap —
            # being "eligible" on a day nobody is drafted for is not an opportunity.
            draftable = sum(
                1 for plan in in_week if plan.demand > 0 and employee.id in plan.eligible
            )
            caps[(employee.id, week)] = max(0, min(max_days_per_week - committed, draftable))

    return caps
