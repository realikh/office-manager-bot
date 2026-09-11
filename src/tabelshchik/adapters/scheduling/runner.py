"""Scheduled work, and the machinery that makes it survive reality.

The previous implementation ran on GitHub Actions cron, which is best-effort: runs were
delayed, sometimes skipped entirely, and nothing noticed. Four things fix that here, and
three of them live in this module.

1. Every occurrence is claimed in a durable ledger before it runs and recorded after, so
   a retry, a double trigger or a restart mid-run cannot double-send.
2. On startup, occurrences that should have fired inside a grace window are replayed, so
   a reminder delayed by a reboot still goes out, marked late.
3. A failure is reported outward rather than swallowed.

The fourth — a process that restarts itself — belongs to Docker.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from tabelshchik.application.ports import JobLedger

logger = logging.getLogger(__name__)

Handler = Callable[["JobContext"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class JobContext:
    """What a handler is told about the run it is performing."""

    job: str
    scheduled_for: datetime
    late: bool = False
    key: str = ""


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    trigger: CronTrigger
    handler: Handler
    #: Extra identity for the occurrence key, so per-office jobs do not collide.
    scope: str = ""
    #: Occurrences older than this are not replayed on startup. A reminder for a day
    #: that has already begun is noise, not a recovery.
    catch_up: bool = True

    def key_for(self, moment: datetime) -> str:
        parts = [self.name, self.scope, moment.strftime("%Y-%m-%dT%H:%M")]
        return ":".join(part for part in parts if part)


@dataclass
class JobRunner:
    """Runs jobs on a cron schedule, idempotently, and says so when one fails."""

    ledger: JobLedger
    timezone: str = "Asia/Almaty"
    on_failure: Callable[[Job, BaseException], Awaitable[None]] | None = None
    jobs: list[Job] = field(default_factory=list)
    _scheduler: AsyncIOScheduler | None = None

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def add(self, job: Job) -> None:
        self.jobs.append(job)

    def start(self) -> None:
        scheduler = AsyncIOScheduler(timezone=self.zone)
        for job in self.jobs:
            scheduler.add_job(
                self._run,
                trigger=job.trigger,
                args=[job],
                id=f"{job.name}:{job.scope}" if job.scope else job.name,
                # A missed fire is handled by the catch-up sweep, which knows about the
                # ledger; APScheduler's own coalescing does not.
                misfire_grace_time=3600,
                coalesce=True,
                max_instances=1,
            )
        scheduler.start()
        self._scheduler = scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def next_runs(self) -> list[tuple[str, datetime | None]]:
        if self._scheduler is None:
            return []
        return [(job.id, job.next_run_time) for job in self._scheduler.get_jobs()]

    async def catch_up(self, *, now: datetime, grace_hours: int) -> list[JobContext]:
        """Replay occurrences that should have fired inside the grace window.

        This is the direct answer to "the cron just did not run": whatever the reason —
        a reboot, a crash, a machine that was asleep — the work still happens, once,
        and is recorded as late.
        """
        if grace_hours <= 0:
            return []

        since = now - timedelta(hours=grace_hours)
        replayed: list[JobContext] = []

        for job in self.jobs:
            if not job.catch_up:
                continue
            for moment in occurrences_between(job.trigger, since, now, self.zone):
                context = JobContext(
                    job=job.name, scheduled_for=moment, late=True, key=job.key_for(moment)
                )
                if await self._execute(job, context):
                    replayed.append(context)

        return replayed

    async def _run(self, job: Job) -> None:
        moment = datetime.now(self.zone).replace(second=0, microsecond=0, tzinfo=None)
        await self._execute(
            job, JobContext(job=job.name, scheduled_for=moment, key=job.key_for(moment))
        )

    async def _execute(self, job: Job, context: JobContext) -> bool:
        """Claim, run, record. Returns whether the handler actually ran."""
        if not self.ledger.claim(
            context.key,
            job=job.name,
            scheduled_for=context.scheduled_for,
            late=context.late,
        ):
            return False

        try:
            await job.handler(context)
        except Exception as error:
            logger.exception("job %s failed", context.key)
            self.ledger.fail(context.key, at=datetime.now(), error=repr(error))
            if self.on_failure is not None:
                await self.on_failure(job, error)
            return False

        self.ledger.complete(context.key, at=datetime.now())
        return True


def occurrences_between(
    trigger: CronTrigger, since: datetime, until: datetime, zone: ZoneInfo
) -> list[datetime]:
    """Every fire time of ``trigger`` in ``(since, until]``, as naive local datetimes.

    Naive on the way out because the rest of the system reasons in one timezone, and
    mixing aware and naive values is a reliable source of off-by-one-day bugs.
    """
    cursor = _aware(since, zone)
    limit = _aware(until, zone)
    found: list[datetime] = []

    for _ in range(500):  # a guard against a pathological trigger, never reached in practice
        following = trigger.get_next_fire_time(None, cursor)
        if following is None or following > limit:
            break
        found.append(following.replace(tzinfo=None))
        cursor = following + timedelta(minutes=1)

    return found


def _aware(moment: datetime, zone: ZoneInfo) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def daily_at(hour: int, minute: int, *, weekdays: frozenset[int] | None = None) -> CronTrigger:
    """A cron trigger for a time of day, optionally restricted to certain weekdays."""
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    day_of_week = ",".join(names[weekday] for weekday in sorted(weekdays)) if weekdays else "*"
    return CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week)
