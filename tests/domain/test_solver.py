"""Tests for the fairness solver.

The optimality claim is the load-bearing part of this system, so it is checked against an
independent brute-force oracle rather than against itself.
"""

from __future__ import annotations

from datetime import timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tabelshchik.domain.calendar import CalendarSpec
from tabelshchik.domain.entities import Absence, Employee
from tabelshchik.domain.instance import build_instance
from tabelshchik.domain.solver import solve

from .conftest import (
    ANCHOR,
    WORKWEEK_DESKS,
    brute_force,
    draft_counts,
    office,
    roster,
    surplus_vector,
    template,
)


def make(
    *,
    employees: list[Employee] | None = None,
    desks: dict[int, int] | None = None,
    fixed: dict[int, tuple[str, ...]] | None = None,
    absences: list[Absence] | None = None,
    weeks: int = 1,
    spec: CalendarSpec | None = None,
    **kwargs: object,
):
    return build_instance(
        office=office(),
        template=template(desks if desks is not None else WORKWEEK_DESKS, fixed),
        employees=employees if employees is not None else roster(8),
        absences=absences or [],
        spec=spec or CalendarSpec(),
        anchor=ANCHOR,
        horizon_weeks=weeks,
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- structure


def test_fixed_only_office_never_builds_a_graph() -> None:
    instance = make(desks={}, fixed={0: ("e0", "e1")})
    assert not instance.needs_draft
    result = solve(instance)
    assert result.drafted == {}
    assert result.shortfall == 0


def test_demand_beyond_the_roster_underfills_without_crashing() -> None:
    instance = make(employees=roster(2), desks={0: 5})
    result = solve(instance)
    assert result.filled == 2
    assert result.shortfall == 3


def test_everyone_fixed_all_week_leaves_nobody_to_draft() -> None:
    people = roster(3)
    everyone = tuple(e.id for e in people)
    instance = make(
        employees=people,
        desks=dict.fromkeys(range(5), 2),
        fixed=dict.fromkeys(range(5), everyone),
    )
    result = solve(instance)
    assert result.filled == 0
    assert result.shortfall == 10


def test_empty_roster_is_handled() -> None:
    result = solve(make(employees=[], desks={0: 3}))
    assert result.filled == 0


# ------------------------------------------------------------------------- optimality


def test_homogeneous_instance_has_spread_of_at_most_one() -> None:
    instance = make(employees=roster(8), weeks=2)
    result = solve(instance)
    counts = draft_counts(instance, result.drafted)
    assert max(counts.values()) - min(counts.values()) <= 1


def test_greedy_counterexample_is_repaired() -> None:
    """Chronological greedy gives A both days; the optimum splits them.

    Filling day 2 forces the solver to walk back and hand day 1 to B. A greedy pass has
    no such repair step, which is why this is a flow problem and not a sort.
    """
    people = roster(2, prefix="p")  # p0, p1
    # Tuesday only p0 is available, so Monday must go to p1.
    absence = Absence(
        employee_id="p1", start_date=ANCHOR + timedelta(days=1), end_date=ANCHOR + timedelta(days=1)
    )
    instance = make(employees=people, desks={0: 1, 1: 1}, absences=[absence])

    result = solve(instance)

    assert result.filled == 2
    assert result.on(ANCHOR) == ("p1",)
    assert result.on(ANCHOR + timedelta(days=1)) == ("p0",)


def test_fixed_schedule_overload_is_not_drafted_further() -> None:
    """The user's explicit requirement: somebody already in four days a week should not
    be drafted while colleagues come in less."""
    people = roster(5)
    busy = "e0"
    instance = make(
        employees=people,
        desks={4: 2},  # two vacant desks on Friday
        fixed={0: (busy,), 1: (busy,), 2: (busy,), 3: (busy,)},
    )
    result = solve(instance)
    assert busy not in result.on(ANCHOR + timedelta(days=4))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    n_employees=st.integers(min_value=2, max_value=5),
    demands=st.lists(st.integers(min_value=0, max_value=2), min_size=1, max_size=4),
    absent=st.lists(st.tuples(st.integers(0, 4), st.integers(0, 3)), max_size=4),
)
def test_matches_brute_force_optimum(
    n_employees: int, demands: list[int], absent: list[tuple[int, int]]
) -> None:
    people = roster(n_employees)
    desks = dict(enumerate(demands))

    absences = [
        Absence(
            employee_id=f"e{who % n_employees}",
            start_date=ANCHOR + timedelta(days=day),
            end_date=ANCHOR + timedelta(days=day),
        )
        for who, day in absent
    ]

    instance = make(employees=people, desks=desks, absences=absences)
    result = solve(instance)

    expected_filled, expected_vector = brute_force(instance)

    assert result.filled == expected_filled
    # Equal sorted surplus vectors, not merely an equal sum of squares: an equal sum
    # would hide a tie-break bug that produced a different-shaped solution.
    assert surplus_vector(instance, result.drafted) == expected_vector


# ----------------------------------------------------------------- history and fairness


def test_prior_surplus_steers_the_next_draft() -> None:
    from tabelshchik.domain.entities import LedgerEntry

    people = roster(4)
    # e0 is two days ahead of everyone else.
    ledger = {"e0": LedgerEntry(employee_id="e0", days=2, entitlement_scaled=0)}
    instance = make(employees=people, desks={0: 3}, ledger=ledger)

    result = solve(instance)
    assert "e0" not in result.on(ANCHOR)


def test_returning_from_vacation_does_not_trigger_a_catch_up_burst() -> None:
    """NO_DEBT: time away accrues no obligation, so the returner gets a normal share."""
    people = roster(4)
    away = Absence(employee_id="e0", start_date=ANCHOR, end_date=ANCHOR + timedelta(days=4))
    instance = make(employees=people, desks=dict.fromkeys(range(5), 2), absences=[away], weeks=2)
    result = solve(instance)

    counts = draft_counts(instance, result.drafted)
    # Second week only: e0 should not be handed every single day to make up the deficit.
    second_week = [plan.day for plan in instance.days if plan.week == 1]
    e0_second_week = sum(1 for day in second_week if "e0" in result.on(day))
    assert e0_second_week <= 3
    assert counts["e0"] <= max(counts.values())


def test_weekly_cap_is_never_exceeded() -> None:
    instance = make(employees=roster(2), desks=dict.fromkeys(range(5), 2), max_days_per_week=3)
    result = solve(instance)
    for employee_id in instance.employees:
        in_week = sum(1 for plan in instance.days if employee_id in result.on(plan.day))
        assert in_week <= 3


def test_week_layer_spreads_days_across_the_horizon() -> None:
    """Without the per-week layer an optimal solution may clump someone's whole month
    into one fortnight."""
    instance = make(employees=roster(10), desks={0: 2, 2: 2, 4: 2}, weeks=3)
    result = solve(instance)
    for employee_id in instance.employees:
        per_week = [
            sum(
                1 for plan in instance.days if plan.week == w and employee_id in result.on(plan.day)
            )
            for w in range(3)
        ]
        assert max(per_week) - min(per_week) <= 1


def test_shortfall_is_spread_across_days_rather_than_concentrated() -> None:
    """Three people who may each come in once, against three days wanting two each.

    Only three of the six desks can be filled. Humans strongly prefer one person on each
    day over two on Monday and none on Wednesday, and the convex day-sink arcs deliver
    that; plain max-flow would be indifferent.
    """
    instance = make(employees=roster(3), desks={0: 2, 1: 2, 2: 2}, max_days_per_week=1)
    result = solve(instance)

    assert result.filled == 3
    assert result.shortfall == 3
    per_day = sorted(len(result.on(plan.day)) for plan in instance.days if plan.demand)
    assert per_day == [1, 1, 1]


# -------------------------------------------------------------------------- determinism


def test_same_inputs_produce_an_identical_plan() -> None:
    first = solve(make(weeks=2))
    second = solve(make(weeks=2))
    assert first.drafted == second.drafted


def test_a_different_seed_reshuffles_without_costing_fairness() -> None:
    base = make(employees=roster(9), weeks=2)
    reshuffled = build_instance(
        office=office(seed_nonce=7),
        template=template(WORKWEEK_DESKS),
        employees=roster(9),
        absences=[],
        spec=CalendarSpec(),
        anchor=ANCHOR,
        horizon_weeks=2,
    )

    first, second = solve(base), solve(reshuffled)

    assert first.drafted != second.drafted
    # Different people, identically fair: this is what proves variety is free.
    assert surplus_vector(base, first.drafted) == surplus_vector(reshuffled, second.drafted)


def test_output_does_not_depend_on_roster_ordering() -> None:
    forwards = make(employees=roster(6), weeks=2)
    backwards = build_instance(
        office=office(),
        template=template(WORKWEEK_DESKS),
        employees=list(reversed(roster(6))),
        absences=[],
        spec=CalendarSpec(),
        anchor=ANCHOR,
        horizon_weeks=2,
    )
    assert solve(forwards).drafted == solve(backwards).drafted


# ------------------------------------------------------------------------ frozen days


def test_frozen_days_are_preserved_and_counted() -> None:
    people = roster(6)
    frozen = {ANCHOR: frozenset({"e5"})}
    instance = make(employees=people, desks={0: 2, 1: 2}, frozen=frozen)

    result = solve(instance)

    # One desk was already spoken for, so only one more is drafted that day.
    assert len(result.on(ANCHOR)) == 1
    assert "e5" not in result.on(ANCHOR)
    # And the frozen day counts toward e5's surplus, so they are less likely elsewhere.
    assert instance.base_scaled["e5"] > instance.base_scaled["e0"]


def test_a_frozen_day_survives_a_wildly_different_ledger() -> None:
    from tabelshchik.domain.entities import LedgerEntry

    frozen = {ANCHOR: frozenset({"e5"})}
    ledger = {"e5": LedgerEntry(employee_id="e5", days=50, entitlement_scaled=0)}
    instance = make(employees=roster(6), desks={0: 2}, frozen=frozen, ledger=ledger)

    result = solve(instance)
    assert "e5" not in result.on(ANCHOR)
    assert len(result.on(ANCHOR)) == 1


# ------------------------------------------------------------------------- eligibility


def test_absent_people_are_never_drafted() -> None:
    away = Absence(employee_id="e0", start_date=ANCHOR, end_date=ANCHOR + timedelta(days=2))
    instance = make(employees=roster(5), absences=[away])
    for offset in range(3):
        assert "e0" not in solve(instance).on(ANCHOR + timedelta(days=offset))


def test_people_outside_their_tenure_are_never_drafted() -> None:
    people = [
        Employee(id="left", full_name="Ушёл", ended_on=ANCHOR - timedelta(days=1)),
        Employee(id="future", full_name="Ещё не вышел", started_on=ANCHOR + timedelta(days=30)),
        *roster(3),
    ]
    instance = make(employees=people, desks={0: 2})
    drafted = solve(instance).on(ANCHOR)
    assert "left" not in drafted
    assert "future" not in drafted


def test_holidays_get_no_assignments() -> None:
    spec = CalendarSpec(holidays=frozenset({ANCHOR}))
    instance = make(spec=spec)
    assert all(plan.day != ANCHOR for plan in instance.days)


def test_transferred_saturday_does_get_assignments() -> None:
    saturday = ANCHOR + timedelta(days=5)
    spec = CalendarSpec(extra_workdays=frozenset({saturday}))
    instance = make(desks={**WORKWEEK_DESKS, 5: 2}, spec=spec)
    assert any(plan.day == saturday for plan in instance.days)
    assert len(solve(instance).on(saturday)) == 2


def test_timezone_of_the_process_does_not_affect_the_result() -> None:
    import os

    baseline = solve(make(weeks=2)).drafted
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Pacific/Kiritimati"
        assert solve(make(weeks=2)).drafted == baseline
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous


def test_everyone_taking_the_same_day_leaves_surpluses_equal() -> None:
    instance = make(employees=roster(5), desks={0: 5})
    result = solve(instance)
    assert len(result.on(ANCHOR)) == 5
    assert len(set(surplus_vector(instance, result.drafted))) == 1
