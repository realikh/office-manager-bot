from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from tabelshchik.adapters.db.repositories import (
    SqlAuditLog,
    SqlJobLedger,
    SqlLedgerStore,
    SqlOfficeStore,
    SqlScheduleStore,
)
from tabelshchik.application.ports import ScheduleDiff
from tabelshchik.domain.entities import (
    SCALE,
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    LedgerEntry,
)
from tabelshchik.domain.ledger import DayRecord, rebuild

from .conftest import ANCHOR, NOW, office_seed, seed

# ------------------------------------------------------------------------- seeding


def test_seeding_creates_an_office_with_its_roster(sessions) -> None:
    report = seed(sessions)
    assert report.created == ("ovest",)

    offices = SqlOfficeStore(sessions)
    assert [o.id for o in offices.active_offices()] == ["ovest"]
    assert len(offices.employees("ovest")) == 4


def test_reseeding_leaves_an_existing_office_alone(sessions) -> None:
    """Seeds are a bootstrap. Once an office exists the database is authoritative, so
    re-running the seed must not undo an admin's edits."""
    seed(sessions)
    report = seed(sessions, office_seed(name="Renamed"))

    assert report.skipped == ("ovest",)
    assert SqlOfficeStore(sessions).get_office("ovest").name == "O'Vest"


def test_replacing_is_possible_when_explicitly_asked_for(sessions) -> None:
    seed(sessions)
    report = seed(sessions, office_seed(name="Renamed"), replace=True)

    assert report.replaced == ("ovest",)
    assert SqlOfficeStore(sessions).get_office("ovest").name == "Renamed"


def test_seeding_stores_the_weekly_template(sessions) -> None:
    seed(
        sessions,
        office_seed(schedule={"fixed": {"friday": ["anya"]}, "vacantDesks": {"friday": 3}}),
    )
    context = SqlOfficeStore(sessions).planning_context("ovest", start=ANCHOR, end=ANCHOR)

    assert context.template.desks_on(4) == 3
    assert context.template.fixed_on(4) == ("anya",)


def test_seeding_stores_absences_and_calendar_exceptions(sessions) -> None:
    seed(
        sessions,
        office_seed(
            absences=[{"employeeId": "anya", "startDate": "2026-09-15", "endDate": "2026-09-16"}],
            calendar={"closed": ["2026-09-16"], "extraWorkdays": ["2026-09-19"]},
        ),
    )
    context = SqlOfficeStore(sessions).planning_context(
        "ovest", start=ANCHOR, end=ANCHOR + timedelta(days=10)
    )

    assert len(context.absences) == 1
    assert date(2026, 9, 16) in context.spec.closed
    assert date(2026, 9, 19) in context.spec.extra_workdays


# ---------------------------------------------------------------- planning context


def test_planning_context_returns_domain_entities(sessions) -> None:
    seed(sessions)
    context = SqlOfficeStore(sessions).planning_context(
        "ovest", start=ANCHOR, end=ANCHOR + timedelta(days=30)
    )

    assert context.office.id == "ovest"
    assert context.employee("anya") is not None
    assert context.employee("nobody") is None


def test_planning_context_includes_public_holidays(sessions) -> None:
    """25 October is Republic Day in Kazakhstan."""
    seed(sessions)
    context = SqlOfficeStore(sessions).planning_context(
        "ovest", start=date(2026, 10, 1), end=date(2026, 10, 31)
    )
    assert date(2026, 10, 25) in context.spec.holidays
    assert not context.spec.is_working_day(date(2026, 10, 25))


def test_an_unknown_office_is_a_lookup_error(sessions) -> None:
    with pytest.raises(LookupError, match="unknown office"):
        SqlOfficeStore(sessions).planning_context("ghost", start=ANCHOR, end=ANCHOR)


def test_absences_outside_the_window_are_not_loaded(sessions) -> None:
    seed(
        sessions,
        office_seed(
            absences=[{"employeeId": "anya", "startDate": "2027-01-01", "endDate": "2027-01-05"}]
        ),
    )
    context = SqlOfficeStore(sessions).planning_context(
        "ovest", start=ANCHOR, end=ANCHOR + timedelta(days=7)
    )
    assert context.absences == ()


# ------------------------------------------------------------------ employee lookup


def test_an_employee_can_be_found_by_username_case_insensitively(sessions) -> None:
    seed(sessions)
    store = SqlOfficeStore(sessions)
    assert store.find_employee_by_username("@ANYA_TG").id == "anya"
    assert store.find_employee_by_username("nobody") is None


def test_linking_a_telegram_user_makes_them_findable(sessions) -> None:
    """Storing the numeric id is what lets the bot mention someone who has no username."""
    seed(sessions)
    store = SqlOfficeStore(sessions)
    store.link_telegram_user("borya", 55555)

    assert store.find_employee_by_user_id(55555).id == "borya"
    assert store.find_employee_by_user_id(999) is None


def test_linking_an_unknown_employee_fails_loudly(sessions) -> None:
    seed(sessions)
    with pytest.raises(LookupError, match="unknown employee"):
        SqlOfficeStore(sessions).link_telegram_user("ghost", 1)


# --------------------------------------------------------------------- assignments


def make_diff(*, added=(), removed=(), days=()) -> ScheduleDiff:
    return ScheduleDiff(added=tuple(added), removed=tuple(removed), days=tuple(days))


def assignment(day: date, employee_id: str, source=AssignmentSource.DRAFTED) -> Assignment:
    return Assignment(office_id="ovest", day=day, employee_id=employee_id, source=source)


def day_record(day: date, available=("anya", "borya"), attended=("anya",)) -> DayRecord:
    return DayRecord(
        day=day,
        weekday=day.weekday(),
        available=frozenset(available),
        entitlement_scaled=SCALE // 2,
        attended=frozenset(attended),
    )


def test_applying_a_diff_stores_assignments_and_days(sessions) -> None:
    seed(sessions)
    store = SqlScheduleStore(sessions)

    store.apply(
        "ovest",
        make_diff(added=[assignment(ANCHOR, "anya")], days=[day_record(ANCHOR)]),
        generation_id=1,
    )

    snapshot = store.day("ovest", ANCHOR)
    assert snapshot.roster == ("anya",)
    assert snapshot.status == "OPEN"


def test_announcing_a_day_freezes_it_and_its_assignments(sessions) -> None:
    seed(sessions)
    store = SqlScheduleStore(sessions)
    store.apply(
        "ovest",
        make_diff(added=[assignment(ANCHOR, "anya")], days=[day_record(ANCHOR)]),
        generation_id=1,
    )

    store.mark_announced("ovest", ANCHOR, fingerprint="abc", at=NOW)

    snapshot = store.day("ovest", ANCHOR)
    assert snapshot.is_announced
    assert snapshot.roster_fingerprint == "abc"
    assert snapshot.assignments[0].status is AssignmentStatus.ANNOUNCED


def test_announced_draft_assignments_become_frozen(sessions) -> None:
    seed(sessions)
    store = SqlScheduleStore(sessions)
    store.apply(
        "ovest",
        make_diff(added=[assignment(ANCHOR, "anya")], days=[day_record(ANCHOR)]),
        generation_id=1,
    )
    store.mark_announced("ovest", ANCHOR, fingerprint="abc", at=NOW)

    frozen = store.frozen_drafted("ovest", ANCHOR, ANCHOR + timedelta(days=7))
    assert frozen == {ANCHOR: frozenset({"anya"})}


def test_fixed_people_are_never_treated_as_frozen(sessions) -> None:
    """They are recomputed from the template on every regeneration, so freezing them
    would double-count."""
    seed(sessions)
    store = SqlScheduleStore(sessions)
    store.apply(
        "ovest",
        make_diff(
            added=[assignment(ANCHOR, "vera", AssignmentSource.FIXED)],
            days=[day_record(ANCHOR)],
        ),
        generation_id=1,
    )
    store.mark_announced("ovest", ANCHOR, fingerprint="abc", at=NOW)

    assert store.frozen_drafted("ovest", ANCHOR, ANCHOR) == {}


def test_a_manual_pin_is_frozen_even_before_it_is_announced(sessions) -> None:
    seed(sessions)
    store = SqlScheduleStore(sessions)
    store.apply(
        "ovest",
        make_diff(
            added=[assignment(ANCHOR, "gleb", AssignmentSource.MANUAL)],
            days=[day_record(ANCHOR)],
        ),
        generation_id=1,
    )

    assert store.frozen_drafted("ovest", ANCHOR, ANCHOR) == {ANCHOR: frozenset({"gleb"})}


def test_cancelling_an_assignment_keeps_the_row_with_a_reason(sessions) -> None:
    seed(sessions)
    store = SqlScheduleStore(sessions)
    store.apply(
        "ovest",
        make_diff(added=[assignment(ANCHOR, "anya")], days=[day_record(ANCHOR)]),
        generation_id=1,
    )

    store.set_assignment_status(
        "ovest", ANCHOR, "anya", AssignmentStatus.CANCELLED, reason="отпуск"
    )

    snapshot = store.day("ovest", ANCHOR)
    assert snapshot.roster == ()  # no longer counted
    assert snapshot.assignments[0].status is AssignmentStatus.CANCELLED


def test_upcoming_days_for_an_employee_skip_cancellations(sessions) -> None:
    seed(sessions)
    store = SqlScheduleStore(sessions)
    later = ANCHOR + timedelta(days=2)
    store.apply(
        "ovest",
        make_diff(
            added=[assignment(ANCHOR, "anya"), assignment(later, "anya")],
            days=[day_record(ANCHOR), day_record(later)],
        ),
        generation_id=1,
    )
    store.set_assignment_status("ovest", ANCHOR, "anya", AssignmentStatus.CANCELLED)

    assert list(store.upcoming_for_employee("anya", start=ANCHOR, limit=10)) == [later]


def test_a_generation_run_is_recorded_for_audit(sessions) -> None:
    seed(sessions)
    store = SqlScheduleStore(sessions)

    generation_id = store.record_generation(
        office_id="ovest",
        horizon_start=ANCHOR,
        horizon_end=ANCHOR + timedelta(days=41),
        seed=123,
        input_hash="deadbeef",
        objective="(1,0,0,0)",
        diff=make_diff(added=[assignment(ANCHOR, "anya")]),
        shortfall=0,
        triggered_by="manual",
        at=NOW,
    )

    assert generation_id > 0


# -------------------------------------------------------------------------- ledger


def test_ledger_round_trips_through_the_database(sessions) -> None:
    seed(sessions)
    store = SqlLedgerStore(sessions)
    entries = {
        "anya": LedgerEntry(
            "anya", days=5, entitlement_scaled=4 * SCALE, per_weekday=(2, 1, 1, 1, 0, 0, 0)
        )
    }

    store.save(entries, as_of=ANCHOR)

    loaded = store.entries("ovest")
    assert loaded == entries
    assert loaded["anya"].surplus_scaled == SCALE


def test_the_ledger_rebuilds_exactly_from_stored_days(sessions) -> None:
    """The property the whole retention design rests on."""
    seed(sessions)
    schedule = SqlScheduleStore(sessions)
    ledger = SqlLedgerStore(sessions)

    records = [day_record(ANCHOR + timedelta(days=offset)) for offset in (0, 1, 2)]
    schedule.apply(
        "ovest",
        make_diff(added=[assignment(record.day, "anya") for record in records], days=records),
        generation_id=1,
    )

    expected = rebuild(records)
    assert ledger.rebuild("ovest", upto=ANCHOR + timedelta(days=7)) == expected


def test_a_cancelled_assignment_does_not_credit_the_ledger(sessions) -> None:
    seed(sessions)
    schedule = SqlScheduleStore(sessions)
    ledger = SqlLedgerStore(sessions)

    schedule.apply(
        "ovest",
        make_diff(added=[assignment(ANCHOR, "anya")], days=[day_record(ANCHOR)]),
        generation_id=1,
    )
    schedule.set_assignment_status("ovest", ANCHOR, "anya", AssignmentStatus.CANCELLED)

    rebuilt = ledger.rebuild("ovest", upto=ANCHOR)
    assert rebuilt["anya"].days == 0
    assert rebuilt["anya"].surplus_scaled < 0  # still owed the day


def test_a_checkpoint_is_included_in_a_rebuild(sessions) -> None:
    seed(sessions)
    ledger = SqlLedgerStore(sessions)
    ledger.save_checkpoint(
        {"anya": LedgerEntry("anya", days=10, entitlement_scaled=9 * SCALE)},
        watermark=ANCHOR,
    )

    rebuilt = ledger.rebuild("ovest", upto=ANCHOR)
    assert rebuilt["anya"].days == 10


# ---------------------------------------------------------------------- job ledger


def test_a_job_occurrence_can_only_be_claimed_once(sessions) -> None:
    """The mechanism that makes a double trigger or a mid-run restart harmless."""
    jobs = SqlJobLedger(sessions)
    key = "attendance:ovest:2026-09-15"

    assert jobs.claim(key, job="attendance", scheduled_for=NOW)
    jobs.complete(key, at=NOW)

    assert not jobs.claim(key, job="attendance", scheduled_for=NOW)


def test_a_failed_occurrence_may_be_retried(sessions) -> None:
    jobs = SqlJobLedger(sessions)
    key = "attendance:ovest:2026-09-15"

    jobs.claim(key, job="attendance", scheduled_for=NOW)
    jobs.fail(key, at=NOW, error="Telegram timed out")

    assert jobs.claim(key, job="attendance", scheduled_for=NOW)
    assert not jobs.ran_successfully(key)


def test_a_send_that_failed_is_not_recorded_as_done(sessions) -> None:
    jobs = SqlJobLedger(sessions)
    jobs.claim("k", job="attendance", scheduled_for=NOW)
    jobs.fail("k", at=NOW, error="boom")

    assert [status for *_rest, status in jobs.recent()] == ["failed"]


def test_recent_runs_come_back_newest_first(sessions) -> None:
    jobs = SqlJobLedger(sessions)
    for offset in range(3):
        moment = NOW + timedelta(hours=offset)
        jobs.claim(f"k{offset}", job="attendance", scheduled_for=moment)
        jobs.complete(f"k{offset}", at=moment)

    assert [key for _job, key, *_rest in jobs.recent()] == ["k2", "k1", "k0"]


# ----------------------------------------------------------------------- audit log


def test_admin_actions_are_audited(sessions) -> None:
    audit = SqlAuditLog(sessions)
    audit.record(actor_id=42, action="employee.remove", payload={"employee": "anya"})

    from sqlalchemy import select

    from tabelshchik.adapters.db import models
    from tabelshchik.adapters.db.engine import session_scope

    with session_scope(sessions) as session:
        rows = session.scalars(select(models.AuditLog)).all()
    assert len(rows) == 1
    assert rows[0].payload == {"employee": "anya"}


# ------------------------------------------------------- the real migrated config


def test_the_real_offices_seed_and_plan(sessions, real_config) -> None:
    """End to end on the actual migrated rosters."""
    from tabelshchik.domain.instance import build_instance
    from tabelshchik.domain.solver import solve

    seed(sessions, *real_config.offices)
    offices = SqlOfficeStore(sessions)

    context = offices.planning_context("ovest", start=ANCHOR, end=ANCHOR + timedelta(days=41))
    instance = build_instance(
        office=context.office,
        template=context.template,
        employees=context.employees,
        absences=context.absences,
        spec=context.spec,
        anchor=ANCHOR,
        horizon_weeks=6,
    )
    result = solve(instance)

    assert len(context.employees) == 12
    assert result.shortfall == 0

    pine = offices.planning_context(
        "pine-office-park", start=ANCHOR, end=ANCHOR + timedelta(days=41)
    )
    pine_instance = build_instance(
        office=pine.office,
        template=pine.template,
        employees=pine.employees,
        absences=pine.absences,
        spec=pine.spec,
        anchor=ANCHOR,
        horizon_weeks=6,
    )
    # Fixed-schedule-only: there is nothing to draft, so the solver short-circuits.
    assert not pine_instance.needs_draft


# ------------------------------------------------------------------------ retention


def _sim_records(days: int, start: date):
    from datetime import timedelta

    return [day_record(start + timedelta(days=offset)) for offset in range(days)]


def test_pruning_folds_history_into_a_checkpoint_before_deleting(sessions) -> None:
    """The decisive retention property: bounding storage must cost no fairness history."""
    from tabelshchik.adapters.clock import FixedClock
    from tabelshchik.adapters.db.repositories import SqlMaintenance
    from tabelshchik.application.prune_history import RetentionPolicy, prune_history

    seed(sessions)
    schedule = SqlScheduleStore(sessions)
    ledger = SqlLedgerStore(sessions)

    start = date(2026, 1, 5)
    records = _sim_records(200, start)
    schedule.apply(
        "ovest",
        make_diff(added=[assignment(record.day, "anya") for record in records], days=records),
        generation_id=1,
    )

    today = start + timedelta(days=210)
    before = ledger.rebuild("ovest", upto=today)

    prune_history(
        offices=SqlOfficeStore(sessions),
        ledger=ledger,
        maintenance=SqlMaintenance(sessions),
        clock=FixedClock(datetime.combine(today, datetime.min.time())),
        policy=RetentionPolicy(schedule_months=3),
    )

    after = ledger.rebuild("ovest", upto=today)
    assert after == before


def test_pruning_actually_removes_rows(sessions) -> None:
    from tabelshchik.adapters.clock import FixedClock
    from tabelshchik.adapters.db.repositories import SqlMaintenance
    from tabelshchik.application.prune_history import RetentionPolicy, prune_history

    seed(sessions)
    start = date(2026, 1, 5)
    records = _sim_records(200, start)
    SqlScheduleStore(sessions).apply(
        "ovest",
        make_diff(added=[assignment(record.day, "anya") for record in records], days=records),
        generation_id=1,
    )
    today = start + timedelta(days=210)

    report = prune_history(
        offices=SqlOfficeStore(sessions),
        ledger=SqlLedgerStore(sessions),
        maintenance=SqlMaintenance(sessions),
        clock=FixedClock(datetime.combine(today, datetime.min.time())),
        policy=RetentionPolicy(schedule_months=3),
    )

    assert report.assignments_removed > 100
    assert report.checkpointed > 100
    remaining = SqlScheduleStore(sessions).days_between("ovest", start, today)
    assert all(snapshot.day >= report.watermark for snapshot in remaining)


def test_pruning_never_touches_people_offices_or_templates(sessions) -> None:
    from tabelshchik.adapters.clock import FixedClock
    from tabelshchik.adapters.db.repositories import SqlMaintenance
    from tabelshchik.application.prune_history import RetentionPolicy, prune_history

    seed(sessions)
    prune_history(
        offices=SqlOfficeStore(sessions),
        ledger=SqlLedgerStore(sessions),
        maintenance=SqlMaintenance(sessions),
        clock=FixedClock(datetime(2030, 1, 1)),
        policy=RetentionPolicy(schedule_months=1),
    )

    offices = SqlOfficeStore(sessions)
    assert len(offices.employees("ovest")) == 4
    assert offices.get_office("ovest") is not None
    context = offices.planning_context("ovest", start=date(2030, 1, 6), end=date(2030, 1, 6))
    assert context.template.desks_on(0) == 2


def test_a_backup_produces_a_readable_copy(sessions, tmp_path) -> None:
    """Nightly off-box backups are only worth having if they actually open."""
    import sqlite3

    from tabelshchik.adapters.db.repositories import SqlMaintenance

    seed(sessions)
    destination = tmp_path / "backup.db"
    SqlMaintenance(sessions).backup_to(str(destination))

    connection = sqlite3.connect(destination)
    try:
        rows = connection.execute("SELECT id FROM office").fetchall()
    finally:
        connection.close()
    assert rows == [("ovest",)]
