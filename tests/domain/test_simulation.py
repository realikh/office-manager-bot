"""Long-run fairness properties.

These are the tests that would have caught the previous implementation's central bug:
fairness state reset on every regeneration, so the spread grew without bound.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

import pytest

from tabelshchik.domain.entities import SCALE, Absence, Employee
from tabelshchik.domain.ledger import rebuild, spread_scaled
from tabelshchik.domain.simulation import SimulationConfig, run

START = date(2026, 1, 5)  # a Monday


def weekly_attendance(result, employee_id: str) -> list[int]:
    """Per-week attendance, by differencing the cumulative snapshots."""
    totals = [snapshot.attended.get(employee_id, 0) for snapshot in result.snapshots]
    return [totals[0]] + [b - a for a, b in pairwise(totals)]


@pytest.mark.slow
def test_spread_stays_bounded_over_a_year() -> None:
    result = run(SimulationConfig(weeks=52, employees=12))
    series = result.spread_series()

    # Roughly 80 office days each across the year; a residual gap of a few days is the
    # price of freezing next week before next week's absences are known.
    assert max(series) < 8 * SCALE


@pytest.mark.slow
def test_spread_does_not_grow_monotonically() -> None:
    """The failure mode of the old system: every regeneration reset the counters, so
    unfairness accumulated instead of being corrected."""
    result = run(SimulationConfig(weeks=104, employees=12))
    series = result.spread_series()

    first_half = sum(series[:52]) / 52
    second_half = sum(series[52:]) / 52

    # Mean-reverting, not drifting: the second year is no worse than the first.
    assert second_half < first_half * 1.5 + SCALE
    assert any(series[i] > series[i + 1] for i in range(len(series) - 1))


@pytest.mark.slow
def test_a_new_joiner_is_treated_fairly_immediately() -> None:
    """Starting at surplus zero rather than at a large deficit is what stops a joiner
    being drafted every day to 'catch up' with the cohort."""
    result = run(SimulationConfig(weeks=52, employees=10))
    joiner = result.ledger["joiner"]

    assert abs(joiner.surplus_scaled) < 3 * SCALE

    joined_week = 52 // 2
    first_weeks = weekly_attendance(result, "joiner")[joined_week : joined_week + 4]
    assert max(first_weeks) <= 5


def test_returning_from_a_long_absence_causes_no_catch_up_burst() -> None:
    people = [Employee(id=f"e{i:02d}", full_name=f"Сотрудник {i}") for i in range(8)]
    away = Absence(
        employee_id="e00",
        start_date=START + timedelta(weeks=4),
        end_date=START + timedelta(weeks=7) - timedelta(days=1),
    )

    result = run(
        SimulationConfig(weeks=16, employees=8, desks=dict.fromkeys(range(5), 3)),
        roster=people,
        absences=[away],
    )

    returner = weekly_attendance(result, "e00")
    cohort = [weekly_attendance(result, e.id) for e in people if e.id != "e00"]

    for week in range(7, 11):
        median = sorted(counts[week] for counts in cohort)[len(cohort) // 2]
        assert returner[week] <= median + 1, (
            f"week {week}: returner did {returner[week]} days against a median of {median}"
        )


@pytest.mark.slow
def test_absences_leave_surplus_untouched() -> None:
    """NO_DEBT in aggregate: someone who took time off should end the year neither ahead
    nor behind, just with fewer raw days."""
    result = run(SimulationConfig(weeks=52, employees=12))
    steady = [entry for eid, entry in result.ledger.items() if eid.startswith("e")]

    raw_days = [entry.days for entry in steady]
    assert max(raw_days) - min(raw_days) > 5  # absences really did vary attendance
    assert spread_scaled(steady) < 8 * SCALE  # yet surplus stayed close


def test_a_fixed_schedule_office_never_drafts_anyone() -> None:
    result = run(
        SimulationConfig(
            weeks=8,
            employees=6,
            desks={},
            fixed={0: ("e00", "e01"), 2: ("e02", "e03"), 4: ("e04", "e05")},
        )
    )
    fixed_weekday = {"e00": 0, "e01": 0, "e02": 2, "e03": 2, "e04": 4, "e05": 4}
    for employee_id, entry in result.ledger.items():
        if employee_id not in fixed_weekday:
            # The joiner and leaver are on nobody's fixed schedule, so with no vacant
            # desks to draft into they never come in at all.
            assert entry.days == 0
            continue
        expected = fixed_weekday[employee_id]
        # They come in on their own weekday and no other — absences may cost them a
        # week, but nothing ever drafts them into a different day.
        assert entry.per_weekday[expected] == entry.days
        assert sum(entry.per_weekday) == entry.days


# ------------------------------------------------------------------------- retention


@pytest.mark.slow
def test_pruning_history_does_not_change_the_ledger() -> None:
    """The decisive retention test. Folding old rows into a checkpoint and deleting them
    must be invisible to fairness — otherwise bounding the database would quietly break
    the thing the database exists for."""
    kept = run(SimulationConfig(weeks=40, employees=10))
    pruned = run(SimulationConfig(weeks=40, employees=10, retain_weeks=12))

    assert dict(pruned.ledger) == dict(kept.ledger)


def test_a_rebuild_from_the_checkpoint_reproduces_the_ledger() -> None:
    result = run(SimulationConfig(weeks=30, employees=10, retain_weeks=8))

    rebuilt = rebuild(result.records, checkpoint=result.checkpoint)

    assert rebuilt == dict(result.ledger)


def test_pruning_actually_bounds_what_is_stored() -> None:
    unbounded = run(SimulationConfig(weeks=40, employees=8))
    bounded = run(SimulationConfig(weeks=40, employees=8, retain_weeks=6))

    assert len(bounded.records) < len(unbounded.records) / 3
    # One checkpoint row per employee, regardless of how long the bot has been running.
    assert len(bounded.checkpoint) <= len(bounded.roster)


def test_simulation_is_reproducible() -> None:
    config = SimulationConfig(weeks=12, employees=8)
    assert dict(run(config).ledger) == dict(run(config).ledger)


def test_a_different_seed_changes_who_goes_but_not_how_fair_it_is() -> None:
    first = run(SimulationConfig(weeks=12, employees=8, seed=1))
    second = run(SimulationConfig(weeks=12, employees=8, seed=2))

    assert dict(first.ledger) != dict(second.ledger)
    assert abs(first.final_spread_scaled - second.final_spread_scaled) < 4 * SCALE
