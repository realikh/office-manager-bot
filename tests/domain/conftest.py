"""Shared builders and a brute-force optimality oracle for the domain tests."""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from datetime import date

from tabelshchik.domain.entities import SCALE, Employee, Office, WeeklyTemplate
from tabelshchik.domain.instance import ScheduleInstance

#: A Monday, used as the anchor everywhere so horizons line up with ISO weeks.
ANCHOR = date(2026, 9, 14)

WORKWEEK_DESKS = {0: 3, 1: 3, 2: 3, 3: 3, 4: 3}


def office(**kwargs: object) -> Office:
    defaults: dict[str, object] = {"id": "test", "name": "Тестовый офис"}
    return Office(**{**defaults, **kwargs})  # type: ignore[arg-type]


def roster(count: int, prefix: str = "e") -> list[Employee]:
    return [Employee(id=f"{prefix}{i}", full_name=f"Сотрудник {i}") for i in range(count)]


def template(desks: Mapping[int, int], fixed: Mapping[int, tuple[str, ...]] | None = None):
    return WeeklyTemplate(fixed=dict(fixed or {}), vacant_desks=dict(desks))


def draft_counts(
    instance: ScheduleInstance, drafted: Mapping[date, tuple[str, ...]]
) -> dict[str, int]:
    counts = dict.fromkeys(instance.employees, 0)
    for names in drafted.values():
        for name in names:
            counts[name] += 1
    return counts


def surplus_vector(
    instance: ScheduleInstance, drafted: Mapping[date, tuple[str, ...]]
) -> tuple[int, ...]:
    """End-of-horizon surplus per employee, sorted descending.

    This is the quantity the optimality theorem is about: over a base polyhedron, every
    strictly convex objective picks the same decreasingly-minimal point, so two optimal
    solutions must agree here even when they draft different people.
    """
    counts = draft_counts(instance, drafted)
    return tuple(
        sorted(
            (
                instance.base_scaled[employee_id] + counts[employee_id] * SCALE
                for employee_id in instance.employees
            ),
            reverse=True,
        )
    )


def brute_force(instance: ScheduleInstance) -> tuple[int, tuple[int, ...]]:
    """Exhaustively find the optimum. Only usable on deliberately tiny instances.

    Returns ``(filled, surplus_vector)`` for the assignment that fills the most desks and,
    among those, minimises the sum of squared surpluses.
    """
    days = [plan for plan in instance.days if plan.demand > 0]

    per_day_options: list[list[tuple[str, ...]]] = []
    for plan in days:
        options: list[tuple[str, ...]] = []
        eligible = sorted(plan.eligible)
        for size in range(plan.demand + 1):
            options.extend(itertools.combinations(eligible, size))
        per_day_options.append(options)

    best: tuple[int, int, tuple[int, ...]] | None = None

    for combination in itertools.product(*per_day_options):
        assignment = {plan.day: choice for plan, choice in zip(days, combination, strict=True)}

        if not _within_weekly_caps(instance, days, combination):
            continue

        filled = sum(len(choice) for choice in combination)
        vector = surplus_vector(instance, assignment)
        squares = sum(value * value for value in vector)

        candidate = (-filled, squares, vector)
        if best is None or candidate < best:
            best = candidate

    assert best is not None
    return -best[0], best[2]


def _within_weekly_caps(
    instance: ScheduleInstance,
    days: Sequence[object],
    combination: Iterable[tuple[str, ...]],
) -> bool:
    per_week: dict[tuple[str, int], int] = {}
    for plan, choice in zip(days, combination, strict=True):
        week = plan.week  # type: ignore[attr-defined]
        for employee_id in choice:
            key = (employee_id, week)
            per_week[key] = per_week.get(key, 0) + 1
    return all(count <= instance.week_cap.get(key, 0) for key, count in per_week.items())
