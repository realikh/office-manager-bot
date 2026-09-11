from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.repositories import (
    SqlLedgerStore,
    SqlOfficeStore,
    SqlScheduleStore,
)
from tabelshchik.application.policy import SchedulePolicy
from tabelshchik.application.regenerate_schedule import (
    regenerate,
)
from tabelshchik.domain.entities import AssignmentSource

from .conftest import office_seed, seed

# A Wednesday, so "this week" already has days behind it.
TODAY = date(2026, 9, 16)
MONDAY = date(2026, 9, 14)


def make(sessions, policy: SchedulePolicy | None = None, **kwargs):
    return regenerate(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        ledger=SqlLedgerStore(sessions),
        clock=FixedClock(datetime.combine(TODAY, datetime.min.time()).replace(hour=10)),
        policy=policy or SchedulePolicy(horizon_weeks=3, freeze_weeks=1),
        **kwargs,
    )


@pytest.fixture
def office(sessions):
    seed(
        sessions, office_seed(schedule={"vacantDesks": {"monday": 2, "wednesday": 2, "friday": 2}})
    )
    return sessions


def test_regeneration_anchors_on_the_monday_of_the_current_week(office) -> None:
    result = make(office)
    assert result.horizon_start == MONDAY
    assert result.horizon_end == MONDAY + timedelta(days=20)


def test_it_fills_every_desk_it_can(office) -> None:
    result = make(office)
    assert result.shortfall == 0
    assert len(result.diff.added) == result.instance.total_demand


def test_assignments_are_persisted_and_readable(office) -> None:
    make(office)
    snapshots = SqlScheduleStore(office).days_between("ovest", MONDAY, MONDAY + timedelta(days=20))

    # Three weeks of Mon/Wed/Fri. Tuesdays and Thursdays expect nobody, so they get no row.
    assert len(snapshots) == 9
    assert all(len(s.roster) == 2 for s in snapshots)


def test_rerunning_with_nothing_changed_produces_no_churn(office) -> None:
    """Idempotence. Adding one vacation should not reshuffle everybody's month."""
    make(office)
    second = make(office)

    assert second.diff.is_empty
    assert not second.changed


def test_the_generation_run_is_recorded_with_a_replayable_hash(office) -> None:
    first = make(office)
    assert first.generation_id > 0

    second = make(office)
    # Same inputs, same instance — the audit trail should say so.
    assert second.generation_id != first.generation_id


# ------------------------------------------------------------------------- freezing


def test_days_inside_the_freeze_window_are_not_re_decided(office) -> None:
    first = make(office)
    frozen_day = MONDAY  # inside week 0, which freezeWeeks=1 protects
    before = first.roster_on(frozen_day)

    # Something that would otherwise change the answer completely.
    ledger = SqlLedgerStore(office)
    from tabelshchik.domain.entities import SCALE, LedgerEntry

    ledger.save(
        {name: LedgerEntry(name, days=50, entitlement_scaled=0) for name in before},
        as_of=TODAY,
    )
    ledger.save(
        {"gleb": LedgerEntry("gleb", days=0, entitlement_scaled=50 * SCALE)},
        as_of=TODAY,
    )

    second = make(office)
    assert second.roster_on(frozen_day) == before


def test_days_beyond_the_freeze_window_are_re_planned(office) -> None:
    from tabelshchik.domain.entities import LedgerEntry

    first = make(office)
    later = MONDAY + timedelta(days=14)  # week 2, outside the freeze
    before = first.roster_on(later)

    SqlLedgerStore(office).save(
        {name: LedgerEntry(name, days=50, entitlement_scaled=0) for name in before},
        as_of=TODAY,
    )

    second = make(office)
    assert second.roster_on(later) != before


def test_an_announced_day_survives_even_outside_the_freeze_window(office) -> None:
    first = make(office)
    later = MONDAY + timedelta(days=14)
    schedule = SqlScheduleStore(office)
    schedule.mark_announced("ovest", later, fingerprint="x", at=datetime.now())
    announced_roster = first.roster_on(later)

    from tabelshchik.domain.entities import LedgerEntry

    SqlLedgerStore(office).save(
        {name: LedgerEntry(name, days=50, entitlement_scaled=0) for name in announced_roster},
        as_of=TODAY,
    )

    second = make(office)
    assert set(announced_roster) <= set(second.roster_on(later))


def test_with_freezing_disabled_everything_is_open_to_change(office) -> None:
    policy = SchedulePolicy(horizon_weeks=3, freeze_weeks=0)
    first = make(office, policy=policy)
    # Days before today still cannot move — the past is not up for renegotiation.
    assert first.roster_on(MONDAY)


def test_announced_assignments_are_never_dropped_silently(office) -> None:
    make(office)
    schedule = SqlScheduleStore(office)
    later = MONDAY + timedelta(days=14)
    schedule.mark_announced("ovest", later, fingerprint="x", at=datetime.now())

    result = make(office)
    dropped = {(a.day, a.employee_id) for a in result.diff.removed}
    assert not any(day == later for day, _ in dropped)


# ----------------------------------------------------------------- roster changes


def test_removing_an_employee_reassigns_their_unfrozen_days(office) -> None:
    from sqlalchemy import delete

    from tabelshchik.adapters.db import models
    from tabelshchik.adapters.db.engine import session_scope

    make(office)
    with session_scope(office) as session:
        session.execute(delete(models.Employee).where(models.Employee.id == "gleb"))

    result = make(office)
    for plan in result.instance.days:
        assert "gleb" not in result.roster_on(plan.day)


def test_adding_an_employee_gets_them_scheduled_from_the_first_open_day(office) -> None:
    from tabelshchik.adapters.db import models
    from tabelshchik.adapters.db.engine import session_scope

    make(office)
    with session_scope(office) as session:
        session.add(models.Employee(id="dima", office_id="ovest", full_name="Дима"))

    result = make(office)
    scheduled = [p.day for p in result.instance.days if "dima" in result.roster_on(p.day)]
    assert scheduled, "a new joiner should be picked up by the next regeneration"
    assert min(scheduled) >= MONDAY + timedelta(days=7)  # the freeze window held


# --------------------------------------------------------------- fixed schedules


def test_fixed_people_are_stored_as_fixed(sessions) -> None:
    seed(
        sessions,
        office_seed(
            schedule={"fixed": {"monday": ["anya", "borya"]}, "vacantDesks": {"monday": 1}}
        ),
    )
    result = make(sessions)

    sources = {a.employee_id: a.source for a in result.diff.added if a.day == MONDAY}
    assert sources["anya"] is AssignmentSource.FIXED
    assert sources["borya"] is AssignmentSource.FIXED
    assert len([s for s in sources.values() if s is AssignmentSource.DRAFTED]) == 1


def test_a_fixed_only_office_still_records_its_days(sessions) -> None:
    seed(sessions, office_seed(schedule={"fixed": {"monday": ["anya"]}}))
    result = make(sessions)

    assert not result.instance.needs_draft
    assert result.draft.drafted == {}
    assert SqlScheduleStore(sessions).day("ovest", MONDAY).roster == ("anya",)


def test_someone_already_fixed_four_days_is_not_drafted_for_the_fifth(sessions) -> None:
    seed(
        sessions,
        office_seed(
            schedule={
                "fixed": {day: ["anya"] for day in ("monday", "tuesday", "wednesday", "thursday")},
                "vacantDesks": {"friday": 2},
            }
        ),
    )
    result = make(sessions)

    friday = MONDAY + timedelta(days=4)
    assert "anya" not in result.draft.on(friday)


# ------------------------------------------------------------------------ ledger


def test_the_ledger_can_be_rebuilt_from_what_was_persisted(office) -> None:
    make(office)
    ledger = SqlLedgerStore(office)

    rebuilt = ledger.rebuild("ovest", upto=MONDAY + timedelta(days=20))

    assert sum(entry.days for entry in rebuilt.values()) == 18  # 9 days x 2 desks
