"""Registering the scheduled work.

Jobs are per office rather than one job that fans out, so a failure in one office is
isolated, its occurrence key is unambiguous, and the catch-up sweep can replay exactly
what was missed.
"""

from __future__ import annotations

import html
import logging
from datetime import timedelta

from tabelshchik.adapters.reports.xlsx import build_workbook, schedule_caption
from tabelshchik.adapters.scheduling.runner import Job, JobContext, JobRunner, daily_at
from tabelshchik.application.build_report import ReportDay, build_report
from tabelshchik.application.prune_history import prune_history
from tabelshchik.application.regenerate_schedule import Regeneration, regenerate
from tabelshchik.application.send_attendance_reminder import send_attendance_reminder
from tabelshchik.application.send_holiday_greeting import send_holiday_greeting
from tabelshchik.application.send_tempo_reminder import send_tempo_reminder
from tabelshchik.bootstrap.container import Services

logger = logging.getLogger(__name__)


def office_jobs(services: Services, office_id: str) -> list[Job]:
    """The four jobs one office needs.

    Its own function so boot, "an office was just created" and "an admin moved a time"
    all build the same list. The times are read from the database here, every time.

    Every trigger takes `app.timezone` explicitly: without it CronTrigger inherits the
    host's zone, and while scheduled firing survives that, the catch-up sweep evaluates
    the trigger directly and silently replays nothing.
    """
    app = services.app
    times = services.reminder_times
    return [
        Job(
            name="attendance",
            scope=office_id,
            trigger=daily_at(
                times.attendance.hour,
                times.attendance.minute,
                weekdays=app.reminders.attendance.weekdays,
                timezone=app.timezone,
            ),
            handler=_attendance_handler(services, office_id),
        ),
        Job(
            # Daily: whether today closes the week or the month is the handler's question,
            # asked of a calendar that knows about holidays.
            name="tempo",
            scope=office_id,
            trigger=daily_at(times.tempo.hour, times.tempo.minute, timezone=app.timezone),
            handler=_tempo_handler(services, office_id),
        ),
        Job(
            name="holiday",
            scope=office_id,
            trigger=daily_at(times.holiday.hour, times.holiday.minute, timezone=app.timezone),
            handler=_holiday_handler(services, office_id),
        ),
        Job(
            name="extend",
            scope=office_id,
            trigger=daily_at(
                times.extend.hour,
                times.extend.minute,
                weekdays=frozenset({times.extend_weekday}),
                timezone=app.timezone,
            ),
            handler=_extend_handler(services, office_id),
        ),
    ]


class LiveOfficeJobs:
    """Adds and removes an office's jobs while the bot is running.

    Lives here because it needs both the runner and the container, and `bootstrap` is the
    only layer allowed to know about both. Handlers reach it through the `OfficeJobs`
    protocol on `BotContext`.
    """

    def __init__(self, runner: JobRunner, services: Services) -> None:
        self._runner = runner
        self._services = services

    def add_office(self, office_id: str) -> None:
        for job in office_jobs(self._services, office_id):
            self._runner.add_live(job)

    def drop_office(self, office_id: str) -> None:
        self._runner.drop(scope=office_id)

    def reschedule(self) -> None:
        for office in self._services.offices.active_offices():
            self.drop_office(office.id)
            self.add_office(office.id)


def register_jobs(runner: JobRunner, services: Services) -> None:
    app = services.app

    for office in services.offices.active_offices():
        for job in office_jobs(services, office.id):
            runner.add(job)

    runner.add(
        Job(
            name="prune",
            trigger=daily_at(3, 30, timezone=app.timezone),
            handler=_prune_handler(services),
            # A missed prune is not worth replaying: tomorrow's run does the same work.
            catch_up=False,
        )
    )

    if app.health.nightly_backup:
        runner.add(
            Job(
                name="backup",
                trigger=daily_at(4, 0, timezone=app.timezone),
                handler=_backup_handler(services),
                catch_up=False,
            )
        )


def _attendance_handler(services: Services, office_id: str):  # type: ignore[no-untyped-def]
    async def handler(_: JobContext) -> None:
        if services.notifier is None:
            return
        outcome = await send_attendance_reminder(
            office_id=office_id,
            offices=services.offices,
            schedule=services.schedule,
            notifier=services.notifier,
            voice=services.voice,
            clock=services.clock,
            silent_policy=services.silent_policy,
            pin=services.app.reminders.attendance.pin,
        )
        if outcome.was_skipped:
            logger.info("attendance for %s skipped: %s", office_id, outcome.skipped)

    return handler


def _tempo_handler(services: Services, office_id: str):  # type: ignore[no-untyped-def]
    async def handler(_: JobContext) -> None:
        if services.notifier is None:
            return
        outcome = await send_tempo_reminder(
            office_id=office_id,
            offices=services.offices,
            notifier=services.notifier,
            voice=services.voice,
            clock=services.clock,
            policy=services.tempo_policy,
            silent_policy=services.silent_policy,
            posts=services.posts,
            gap_minutes=services.reminder_times.tempo_gap_minutes,
        )
        if outcome.skipped and outcome.skipped != "not-due":
            logger.info("tempo for %s skipped: %s", office_id, outcome.skipped)

    return handler


def _holiday_handler(services: Services, office_id: str):  # type: ignore[no-untyped-def]
    async def handler(_: JobContext) -> None:
        if services.notifier is None:
            return
        outcome = await send_holiday_greeting(
            office_id=office_id,
            offices=services.offices,
            notifier=services.notifier,
            voice=services.voice,
            clock=services.clock,
            calendar=services.holiday_calendar,
            posts=services.posts,
            silent_policy=services.silent_policy,
        )
        if outcome.sent:
            logger.info("greeted %s: %s", office_id, ", ".join(outcome.holidays))
        elif outcome.skipped != "not-a-holiday":
            logger.info("holiday greeting for %s skipped: %s", office_id, outcome.skipped)

    return handler


def _extend_handler(services: Services, office_id: str):  # type: ignore[no-untyped-def]
    """Rolls the horizon forward and publishes the workbook if anything moved."""

    async def handler(_: JobContext) -> None:
        office = services.offices.get_office(office_id)
        if office is None or not office.active:
            # `regenerate` reaches `planning_context`, which raises LookupError once the
            # office is gone — and the runner turns that into a Telegram failure alert
            # every Thursday, forever.
            return

        result = regenerate(
            office_id=office_id,
            offices=services.offices,
            schedule=services.schedule,
            ledger=services.ledger,
            clock=services.clock,
            policy=services.schedule_policy,
            triggered_by="cron",
        )
        if not result.changed or services.notifier is None:
            logger.info("schedule for %s unchanged", office_id)
            return
        await publish_workbook(services, office_id, result)

    return handler


def _prune_handler(services: Services):  # type: ignore[no-untyped-def]
    async def handler(_: JobContext) -> None:
        report = prune_history(
            offices=services.offices,
            ledger=services.ledger,
            maintenance=services.maintenance,
            clock=services.clock,
            policy=services.retention_policy,
        )
        logger.info(
            "pruned to %s: %s assignments, %s checkpointed, %.1f MB",
            report.watermark,
            report.assignments_removed,
            report.checkpointed,
            report.bytes_after / (1024 * 1024),
        )

    return handler


def _backup_handler(services: Services):  # type: ignore[no-untyped-def]
    """A nightly copy sent to the admin chat — free off-box backups."""

    async def handler(_: JobContext) -> None:
        chat_id = services.admin_chat_id
        if chat_id is None or services.notifier is None:
            return

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "tabelshchik.db"
            services.maintenance.backup_to(str(destination))
            content = destination.read_bytes()

        stamp = services.clock.today().isoformat()
        await services.notifier.send_document(
            chat_id,
            f"tabelshchik-{stamp}.db",
            content,
            caption=f"Резервная копия на {stamp}.",
            silent=True,
        )

    return handler


async def publish_workbook(services: Services, office_id: str, result: Regeneration) -> None:
    if services.notifier is None:
        return
    office = services.offices.get_office(office_id)
    if office is None or office.chat_id is None:
        logger.warning("office %s has no chat; workbook not sent", office_id)
        return

    context = services.offices.planning_context(
        office_id, start=result.horizon_start, end=result.horizon_end
    )
    days = [
        ReportDay(
            day=plan.day,
            weekday=plan.weekday,
            attendees=result.roster_on(plan.day),
            desks_required=plan.desks_total,
        )
        for plan in result.instance.days
        if plan.desks_total
    ]
    report = build_report(
        office_name=office.name,
        start=result.horizon_start,
        end=result.horizon_end,
        employees=context.employees,
        days=days,
        spec=context.spec,
        ledger=services.ledger.entries(office_id),
        seed=result.instance.seed,
    )

    caption = schedule_caption(
        services.voice.catalog,
        report,
        office_name=office.name,
        horizon_start=result.horizon_start,
        horizon_end=result.horizon_end,
        week_from=services.clock.today() + timedelta(days=1),
        shortfall=result.shortfall,
    )
    await services.notifier.send_document(
        office.chat_id,
        f"{office_id}-{result.horizon_start.isoformat()}.xlsx",
        build_workbook(report, services.voice.catalog.common),
        caption=caption,
        silent=services.silent_policy.is_silent(
            services.clock.today().weekday(), services.clock.time_of_day()
        ),
    )


def alerting(services: Services):  # type: ignore[no-untyped-def]
    """Report a failed job outward instead of letting it die quietly."""

    async def on_failure(job: Job, error: BaseException) -> None:
        chat_id = services.admin_chat_id
        if chat_id is None or services.notifier is None:
            return
        where = f"{job.name}:{job.scope}" if job.scope else job.name
        await services.notifier.send(
            chat_id,
            f"⚠️ Задача <code>{html.escape(where)}</code> завершилась ошибкой:\n"
            f"<pre>{html.escape(repr(error)[:600])}</pre>",
        )

    return on_failure
