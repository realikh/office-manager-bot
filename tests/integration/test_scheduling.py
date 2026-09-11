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


class Recorder:
    def __init__(self, explode: bool = False) -> None:
        self.runs: list[JobContext] = []
        self.explode = explode

    async def __call__(self, context: JobContext) -> None:
        self.runs.append(context)
        if self.explode:
            raise RuntimeError("Telegram is down")


def runner(sessions, **kwargs) -> JobRunner:
    return JobRunner(ledger=SqlJobLedger(sessions), **kwargs)


def job(handler, *, name="attendance", scope="ovest", hour=15, minute=30, **kwargs) -> Job:
    return Job(name=name, trigger=daily_at(hour, minute), handler=handler, scope=scope, **kwargs)


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

    trigger = daily_at(15, 30, weekdays=frozenset({0, 6}))  # Monday and Sunday
    found = occurrences_between(
        trigger, datetime(2026, 9, 14), datetime(2026, 9, 22), ZoneInfo("Asia/Almaty")
    )

    assert [moment.date().isoformat() for moment in found] == [
        "2026-09-14",
        "2026-09-20",
        "2026-09-21",
    ]


def test_occurrences_are_returned_as_naive_local_times() -> None:
    from zoneinfo import ZoneInfo

    found = occurrences_between(
        daily_at(15, 30),
        datetime(2026, 9, 14),
        datetime(2026, 9, 15),
        ZoneInfo("Asia/Almaty"),
    )
    assert found[0] == MONDAY_1530
    assert found[0].tzinfo is None


@pytest.mark.parametrize("grace", [1, 6, 24, 72])
def test_the_window_is_bounded_whatever_the_grace(grace: int) -> None:
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    now = datetime(2026, 9, 16, 16, 0)
    found = occurrences_between(
        daily_at(15, 30), now - timedelta(hours=grace), now, ZoneInfo("Asia/Almaty")
    )
    assert all(moment <= now for moment in found)
