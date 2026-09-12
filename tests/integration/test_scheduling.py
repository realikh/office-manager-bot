"""Tests for the reliability machinery.

These cover the failure class that motivated the rebuild: a scheduled run that was
delayed, skipped, or run twice.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tabelshchik.adapters.db.repositories import SqlJobLedger
from tabelshchik.adapters.scheduling.runner import (
    Job,
    JobContext,
    JobRunner,
    daily_at,
    occurrences_between,
)

MONDAY_1530 = datetime(2026, 9, 14, 15, 30)

#: Pinned deliberately. Left to the host, the trigger adopts one zone while the
#: catch-up window uses another, and the sweep silently finds nothing — the bug these
#: tests exist to pin down, and the reason they passed locally and failed in CI.
ZONE = "Asia/Almaty"


class Recorder:
    def __init__(self, explode: bool = False) -> None:
        self.runs: list[JobContext] = []
        self.explode = explode

    async def __call__(self, context: JobContext) -> None:
        self.runs.append(context)
        if self.explode:
            raise RuntimeError("Telegram is down")


def runner(sessions, **kwargs) -> JobRunner:
    kwargs.setdefault("timezone", ZONE)
    return JobRunner(ledger=SqlJobLedger(sessions), **kwargs)


def job(handler, *, name="attendance", scope="ovest", hour=15, minute=30, **kwargs) -> Job:
    return Job(
        name=name,
        trigger=daily_at(hour, minute, timezone=ZONE),
        handler=handler,
        scope=scope,
        **kwargs,
    )


# ------------------------------------------------------------------ occurrence keys


def test_the_key_identifies_the_job_the_office_and_the_minute() -> None:
    assert job(Recorder()).key_for(MONDAY_1530) == "attendance:ovest:2026-09-14T15:30"


def test_two_offices_do_not_share_an_occurrence() -> None:
    first = job(Recorder(), scope="ovest").key_for(MONDAY_1530)
    second = job(Recorder(), scope="pine").key_for(MONDAY_1530)
    assert first != second


# ------------------------------------------------------------------------ catch-up


async def test_a_missed_run_is_replayed_on_startup(sessions) -> None:
    """The exact scenario GitHub Actions used to produce: the run simply did not happen."""
    handler = Recorder()
    engine = runner(sessions)
    engine.add(job(handler))

    replayed = await engine.catch_up(now=datetime(2026, 9, 14, 16, 10), grace_hours=12)

    assert len(replayed) == 1
    assert handler.runs[0].scheduled_for == MONDAY_1530
    assert handler.runs[0].late


async def test_a_run_that_already_happened_is_not_replayed(sessions) -> None:
    handler = Recorder()
    engine = runner(sessions)
    engine.add(job(handler))

    await engine.catch_up(now=datetime(2026, 9, 14, 16, 10), grace_hours=12)
    await engine.catch_up(now=datetime(2026, 9, 14, 16, 20), grace_hours=12)

    assert len(handler.runs) == 1


async def test_occurrences_outside_the_grace_window_are_left_alone(sessions) -> None:
    """Yesterday's reminder is noise by now, not a recovery."""
    handler = Recorder()
    engine = runner(sessions)
    engine.add(job(handler))

    # 08:00 with two hours of grace covers 06:00-08:00; the 15:30 runs are all outside.
    await engine.catch_up(now=datetime(2026, 9, 16, 8, 0), grace_hours=2)

    assert handler.runs == []


async def test_several_missed_days_are_all_replayed(sessions) -> None:
    handler = Recorder()
    engine = runner(sessions)
    engine.add(job(handler))

    replayed = await engine.catch_up(now=datetime(2026, 9, 16, 16, 0), grace_hours=72)

    assert len(replayed) == 3  # the 14th, 15th and 16th


async def test_catch_up_can_be_disabled_per_job(sessions) -> None:
    handler = Recorder()
    engine = runner(sessions)
    engine.add(job(handler, catch_up=False))

    assert await engine.catch_up(now=datetime(2026, 9, 14, 16, 10), grace_hours=12) == []


async def test_a_zero_grace_window_replays_nothing(sessions) -> None:
    handler = Recorder()
    engine = runner(sessions)
    engine.add(job(handler))

    assert await engine.catch_up(now=datetime(2026, 9, 14, 16, 10), grace_hours=0) == []


# ------------------------------------------------------------------- idempotency


async def test_the_same_occurrence_never_runs_twice(sessions) -> None:
    handler = Recorder()
    engine = runner(sessions)
    a_job = job(handler)
    engine.add(a_job)

    context = JobContext(
        job="attendance", scheduled_for=MONDAY_1530, key=a_job.key_for(MONDAY_1530)
    )
    assert await engine._execute(a_job, context)
    assert not await engine._execute(a_job, context)
    assert len(handler.runs) == 1


# ----------------------------------------------------------------------- failures


async def test_a_failing_job_is_recorded_and_reported(sessions) -> None:
    """A job that dies quietly is how the old system lost a week of reminders."""
    handler = Recorder(explode=True)
    reported: list[tuple[str, str]] = []

    async def on_failure(failed_job: Job, error: BaseException) -> None:
        reported.append((failed_job.name, repr(error)))

    engine = runner(sessions, on_failure=on_failure)
    engine.add(job(handler))

    await engine.catch_up(now=datetime(2026, 9, 14, 16, 10), grace_hours=12)

    assert reported == [("attendance", "RuntimeError('Telegram is down')")]
    assert [status for *_rest, status in SqlJobLedger(sessions).recent()] == ["failed"]


async def test_a_failing_job_does_not_stop_the_others(sessions) -> None:
    failing, healthy = Recorder(explode=True), Recorder()
    engine = runner(sessions)
    engine.add(job(failing, scope="ovest"))
    engine.add(job(healthy, scope="pine"))

    await engine.catch_up(now=datetime(2026, 9, 14, 16, 10), grace_hours=12)

    assert len(healthy.runs) == 1


async def test_a_failed_job_is_retried_on_the_next_sweep(sessions) -> None:
    handler = Recorder(explode=True)
    engine = runner(sessions)
    engine.add(job(handler))

    await engine.catch_up(now=datetime(2026, 9, 14, 16, 10), grace_hours=12)
    handler.explode = False
    await engine.catch_up(now=datetime(2026, 9, 14, 16, 20), grace_hours=12)

    assert len(handler.runs) == 2
    assert [status for *_rest, status in SqlJobLedger(sessions).recent()] == ["ok"]


# ------------------------------------------------------------------------ triggers


def test_a_trigger_can_be_restricted_to_weekdays() -> None:
    from zoneinfo import ZoneInfo

    trigger = daily_at(15, 30, weekdays=frozenset({0, 6}), timezone=ZONE)  # Mon and Sun
    found = occurrences_between(
        trigger, datetime(2026, 9, 14), datetime(2026, 9, 22), ZoneInfo(ZONE)
    )

    assert [moment.date().isoformat() for moment in found] == [
        "2026-09-14",
        "2026-09-20",
        "2026-09-21",
    ]


def test_occurrences_are_returned_as_naive_local_times() -> None:
    from zoneinfo import ZoneInfo

    found = occurrences_between(
        daily_at(15, 30, timezone=ZONE),
        datetime(2026, 9, 14),
        datetime(2026, 9, 15),
        ZoneInfo(ZONE),
    )
    assert found[0] == MONDAY_1530
    assert found[0].tzinfo is None


@pytest.mark.parametrize("grace", [1, 6, 24, 72])
def test_the_window_is_bounded_whatever_the_grace(grace: int) -> None:
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    now = datetime(2026, 9, 16, 16, 0)
    found = occurrences_between(
        daily_at(15, 30, timezone=ZONE), now - timedelta(hours=grace), now, ZoneInfo(ZONE)
    )
    assert all(moment <= now for moment in found)


# ------------------------------------------------------------------------ timezones


def test_a_trigger_keeps_the_timezone_it_was_given_not_the_hosts() -> None:
    """The failure this guards against is silent.

    Without an explicit zone APScheduler uses the host's. Scheduled firing still works,
    because the scheduler applies its own timezone when the job is added — but the
    catch-up sweep evaluates the trigger directly, so its window and the trigger drift
    apart and the sweep replays nothing at all. Nothing errors; reminders simply stop
    being recovered.
    """
    from zoneinfo import ZoneInfo

    trigger = daily_at(15, 30, timezone="Asia/Almaty")
    assert trigger.timezone == ZoneInfo("Asia/Almaty")


def test_catch_up_finds_the_occurrence_whatever_the_host_zone_is() -> None:
    """This is the assertion that would have failed in CI and passed locally."""
    from zoneinfo import ZoneInfo

    for zone in ("Asia/Almaty", "UTC", "America/New_York", "Pacific/Kiritimati"):
        found = occurrences_between(
            daily_at(15, 30, timezone=zone),
            datetime(2026, 9, 14, 0, 0),
            datetime(2026, 9, 14, 23, 59),
            ZoneInfo(zone),
        )
        assert found == [MONDAY_1530], f"{zone}: got {found}"


def test_every_scheduled_job_pins_its_timezone(sessions, tmp_path) -> None:
    """Catches a trigger added later without one — the mistake is easy and invisible."""
    from pathlib import Path

    from tabelshchik.adapters.scheduling.runner import JobRunner
    from tabelshchik.bootstrap.container import build_services
    from tabelshchik.bootstrap.jobs import register_jobs
    from tabelshchik.bootstrap.settings import Secrets

    services = build_services(
        config_dir=Path("config"), database_path=tmp_path / "t.db", secrets=Secrets()
    )
    try:
        engine = JobRunner(ledger=services.jobs, timezone=services.config.app.timezone)
        register_jobs(engine, services)

        assert engine.jobs
        for item in engine.jobs:
            assert str(item.trigger.timezone) == services.config.app.timezone, (
                f"{item.name}:{item.scope} follows the host timezone"
            )
    finally:
        services.engine.dispose()


# ------------------------------------------------------- jobs added while bot is running


async def test_an_office_created_today_gets_its_jobs_today(sessions) -> None:
    """`add` only appends to the list `start` reads once.

    Without `add_live`, an office created from the bot this morning has no attendance
    reminder until the next deploy — the "looks configured, does nothing" failure this
    codebase keeps running into.
    """
    live = runner(sessions)
    live.start()
    try:
        live.add_live(
            Job(
                name="attendance",
                scope="new-office",
                trigger=daily_at(15, 30, timezone="UTC"),
                handler=_nothing,
            )
        )
        assert "attendance:new-office" in _scheduled(live)
    finally:
        live.shutdown()


async def test_closing_an_office_takes_its_jobs_off_the_scheduler(sessions) -> None:
    live = runner(sessions)
    for name in ("attendance", "tempo"):
        live.add(
            Job(name=name, scope="ovest", trigger=daily_at(9, 0, timezone="UTC"), handler=_nothing)
        )
    live.add(Job(name="prune", trigger=daily_at(3, 30, timezone="UTC"), handler=_nothing))
    live.start()
    try:
        live.drop(scope="ovest")
        assert _scheduled(live) == ["prune"]
        assert [item.name for item in live.jobs] == ["prune"]
    finally:
        live.shutdown()


def _scheduled(runner: JobRunner) -> list[str]:
    return sorted(job_id for job_id, _next in runner.next_runs())


async def _nothing(_context) -> None:
    return None
