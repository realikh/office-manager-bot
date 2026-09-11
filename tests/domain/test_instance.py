from __future__ import annotations

from datetime import timedelta

import pytest

from tabelshchik.domain.calendar import CalendarSpec
from tabelshchik.domain.entities import (
    SCALE,
    Absence,
    AbsencePolicy,
    Employee,
    LedgerEntry,
)
from tabelshchik.domain.instance import build_instance

from .conftest import ANCHOR, WORKWEEK_DESKS, office, roster, template


def make(**kwargs: object):
    defaults: dict[str, object] = {
        "office": office(),
        "template": template(WORKWEEK_DESKS),
        "employees": roster(5),
        "absences": [],
        "spec": CalendarSpec(),
        "anchor": ANCHOR,
        "horizon_weeks": 1,
    }
    return build_instance(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_horizon_covers_whole_weeks_of_working_days() -> None:
    instance = make(horizon_weeks=2)
    assert len(instance.days) == 10
    assert instance.days[0].day == ANCHOR
    assert instance.horizon_end == ANCHOR + timedelta(days=13)


def test_horizon_must_be_at_least_one_week() -> None:
    with pytest.raises(ValueError, match="horizon_weeks"):
        make(horizon_weeks=0)


def test_a_fixed_person_is_not_also_draftable_that_day() -> None:
    instance = make(template=template({0: 2}, {0: ("e0",)}))
    monday = instance.days[0]
    assert "e0" in monday.fixed
    assert "e0" not in monday.eligible


def test_an_absent_fixed_person_drops_off_the_day() -> None:
    away = Absence(employee_id="e0", start_date=ANCHOR, end_date=ANCHOR)
    instance = make(template=template({0: 2}, {0: ("e0",)}), absences=[away])
    monday = instance.days[0]
    assert "e0" not in monday.fixed
    assert "e0" not in monday.available
    # One fewer head means the office consumes one fewer person-day that day.
    assert monday.desks_total == 2


def test_desks_total_counts_fixed_heads_plus_vacant_desks() -> None:
    """vacantDesks is additional draftees, not the day's total capacity."""
    instance = make(template=template({0: 3}, {0: ("e0", "e1")}))
    monday = instance.days[0]
    assert monday.desks_total == 5
    assert monday.demand == 3


def test_frozen_draftees_reduce_the_remaining_demand() -> None:
    instance = make(template=template({0: 3}), frozen={ANCHOR: frozenset({"e0", "e1"})})
    monday = instance.days[0]
    assert monday.frozen == frozenset({"e0", "e1"})
    assert monday.demand == 1
    assert "e0" not in monday.eligible


def test_entitlement_is_the_days_cost_shared_among_the_available() -> None:
    instance = make(employees=roster(4), template=template({0: 2}))
    assert instance.days[0].entitlement_scaled == 2 * SCALE // 4


def test_no_debt_excludes_the_absent_from_sharing_the_cost() -> None:
    away = Absence(employee_id="e0", start_date=ANCHOR, end_date=ANCHOR)
    instance = make(employees=roster(4), template=template({0: 2}), absences=[away])
    monday = instance.days[0]
    assert "e0" not in monday.available
    assert monday.entitlement_scaled == 2 * SCALE // 3


def test_accrue_debt_still_charges_the_absent() -> None:
    away = Absence(employee_id="e0", start_date=ANCHOR, end_date=ANCHOR)
    instance = make(
        employees=roster(4),
        template=template({0: 2}),
        absences=[away],
        absence_policy=AbsencePolicy.ACCRUE_DEBT,
    )
    monday = instance.days[0]
    assert "e0" in monday.available
    assert monday.entitlement_scaled == 2 * SCALE // 4
    # And so they carry a deficit for the day they missed.
    assert instance.base_scaled["e0"] < 0


def test_fixed_days_push_someone_ahead_before_any_drafting() -> None:
    instance = make(template=template({4: 2}, dict.fromkeys(range(4), ("e0",))))
    assert instance.base_scaled["e0"] > instance.base_scaled["e1"]


def test_prior_surplus_is_carried_in_and_clamped() -> None:
    huge = LedgerEntry(employee_id="e0", days=10_000, entitlement_scaled=0)
    instance = make(ledger={"e0": huge}, surplus_clamp_days=10)
    fixed_and_accrual = instance.base_scaled["e1"]
    assert instance.base_scaled["e0"] == 10 * SCALE + fixed_and_accrual


def test_weekly_cap_leaves_room_only_for_what_is_left() -> None:
    instance = make(
        template=template({4: 2}, dict.fromkeys(range(4), ("e0",))),
        max_days_per_week=5,
    )
    assert instance.week_cap[("e0", 0)] == 1
    assert instance.week_cap[("e1", 0)] == 1  # only Friday is draftable at all


def test_weekly_cap_is_bounded_by_actually_draftable_days() -> None:
    instance = make(template=template({0: 2}), max_days_per_week=5)
    assert instance.week_cap[("e0", 0)] == 1


def test_people_outside_the_horizon_are_left_out_of_the_roster() -> None:
    people = [
        Employee(id="gone", full_name="Ушёл", ended_on=ANCHOR - timedelta(days=1)),
        Employee(id="later", full_name="Позже", started_on=ANCHOR + timedelta(days=90)),
        *roster(2),
    ]
    instance = make(employees=people)
    assert instance.employees == ("e0", "e1")


def test_a_fixed_schedule_only_office_needs_no_draft() -> None:
    instance = make(template=template({}, {0: ("e0", "e1")}))
    assert not instance.needs_draft
    assert instance.total_demand == 0


def test_unknown_employee_ids_in_the_template_are_ignored() -> None:
    instance = make(template=template({0: 1}, {0: ("ghost",)}))
    assert instance.days[0].fixed == frozenset()


def test_holidays_are_absent_from_the_horizon_entirely() -> None:
    spec = CalendarSpec(holidays=frozenset({ANCHOR}))
    instance = make(spec=spec)
    assert all(plan.day != ANCHOR for plan in instance.days)
    assert len(instance.days) == 4


def test_seed_depends_on_office_anchor_and_nonce() -> None:
    base = make().seed
    assert make(office=office(seed_nonce=1)).seed != base
    assert make(anchor=ANCHOR + timedelta(days=7)).seed != base
    assert make().seed == base


def test_single_day_absence_is_inclusive() -> None:
    away = Absence(employee_id="e0", start_date=ANCHOR, end_date=ANCHOR)
    instance = make(absences=[away])
    assert "e0" not in instance.days[0].available
    assert "e0" in instance.days[1].available


def test_overlapping_absences_are_treated_as_a_union() -> None:
    absences = [
        Absence(employee_id="e0", start_date=ANCHOR, end_date=ANCHOR + timedelta(days=2)),
        Absence(
            employee_id="e0",
            start_date=ANCHOR + timedelta(days=1),
            end_date=ANCHOR + timedelta(days=3),
        ),
    ]
    instance = make(absences=absences)
    for offset in range(4):
        plan = instance.day_by_date(ANCHOR + timedelta(days=offset))
        assert plan is not None
        assert "e0" not in plan.available


def test_an_absence_ending_before_it_starts_is_rejected() -> None:
    with pytest.raises(ValueError, match="before it starts"):
        Absence(employee_id="e0", start_date=ANCHOR, end_date=ANCHOR - timedelta(days=1))
