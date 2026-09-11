"""Starting and stopping the bot.

The startup order matters. The catch-up sweep runs before polling begins, so a reminder
missed while the process was down goes out immediately rather than waiting behind a
queue of user messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from aiogram.exceptions import TelegramAPIError

from tabelshchik.adapters.scheduling.runner import JobRunner
from tabelshchik.adapters.telegram.bot import create_bot, create_dispatcher
from tabelshchik.adapters.telegram.commands import publish_commands
from tabelshchik.adapters.telegram.notifier import TelegramNotifier
from tabelshchik.bootstrap.container import Services
from tabelshchik.bootstrap.jobs import alerting, register_jobs

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    services: Services
    runner: JobRunner
    heartbeat: asyncio.Task[None] | None = None


async def run_bot(services: Services) -> None:
    """Run until interrupted."""
    bot = create_bot(services.secrets.telegram_bot_token)
    services.notifier = TelegramNotifier(bot=bot)

    me = await bot.get_me()
    services.bot_username = me.username or ""
    logger.info("running as @%s", services.bot_username)

    # Keeps the slash-command menu in step with the handlers that exist.
    try:
        await publish_commands(bot, admin_ids=services.admin_ids)
    except TelegramAPIError:
        # Cosmetic: the bot works without a menu, so this must not stop it starting.
        logger.warning("could not publish the command menu", exc_info=True)

    _check_admin_chat(services)
    dispatcher = create_dispatcher(services)

    async with lifespan(services) as runtime:
        logger.info("scheduled jobs: %s", [job.name for job in runtime.runner.jobs])
        try:
            await dispatcher.start_polling(
                bot,
                handle_signals=False,
                # Telegram keeps undelivered updates for 24 hours. Answering a question
                # asked before a restart is noise, and replaying admin button presses
                # would be worse. Scheduled work is protected by the job ledger, not by
                # this queue, so nothing important is lost.
                drop_pending_updates=True,
            )
        finally:
            await bot.session.close()


@asynccontextmanager
async def lifespan(services: Services) -> AsyncIterator[Runtime]:
    runner = JobRunner(
        ledger=services.jobs,
        timezone=services.app.timezone,
        on_failure=alerting(services),
    )
    register_jobs(runner, services)

    # Seed first. A catch-up that runs before the schedule exists finds nothing to
    # announce, marks the occurrence done, and so swallows the very reminder it is
    # meant to recover.
    _seed_empty_schedules(services)

    # Before polling starts, so a missed reminder goes out ahead of any user messages.
    # Skipped entirely on a first-ever boot: an empty job ledger means there is no
    # history to catch up on, and "recovering" work from before the bot existed would
    # fire a reminder at whatever hour it happened to be installed.
    if services.jobs.recent(limit=1):
        replayed = await runner.catch_up(
            now=services.clock.now(), grace_hours=services.app.health.catch_up_grace_hours
        )
        if replayed:
            logger.warning("replayed %s missed job(s) on startup", len(replayed))
    else:
        logger.info("first run: no job history, nothing to catch up on")

    runner.start()
    runtime = Runtime(services=services, runner=runner)

    # Always: the local heartbeat is what the container healthcheck reads, so it must
    # run whether or not an external dead-man's switch is configured.
    runtime.heartbeat = asyncio.create_task(_heartbeat(services))

    try:
        yield runtime
    finally:
        runner.shutdown()
        if runtime.heartbeat is not None:
            runtime.heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.heartbeat
        services.engine.dispose()


def _check_admin_chat(services: Services) -> None:
    """Telegram group ids are negative; user ids are positive.

    The nightly job sends the **entire database** to this chat — names, usernames, user
    ids, every schedule and absence — and `.env.example` documents it as "your own
    Telegram user id". A private admin group is a legitimate choice, but it should be a
    deliberate one rather than a paste that happened to be a group.
    """
    chat_id = services.secrets.admin_chat_id
    if chat_id is not None and chat_id < 0:
        logger.warning(
            "ADMIN_CHAT_ID %s looks like a group chat. The nightly database backup and "
            "every failure alert will be visible to everyone in it.",
            chat_id,
        )


def _seed_empty_schedules(services: Services) -> None:
    """Give an office a schedule the first time it has none.

    Without this a fresh deployment sits silent until the weekly regeneration job fires,
    which on a Monday means three days of a bot that looks broken. Offices that already
    have a schedule are left alone — this is a cold start, not a rebuild.
    """
    from tabelshchik.application.regenerate_schedule import regenerate

    today = services.clock.today()
    for office in services.offices.active_offices():
        if services.schedule.has_schedule_from(office.id, today):
            continue
        logger.info("no schedule for %s yet; generating the first one", office.id)
        regenerate(
            office_id=office.id,
            offices=services.offices,
            schedule=services.schedule,
            ledger=services.ledger,
            clock=services.clock,
            policy=services.schedule_policy,
            triggered_by="first-boot",
        )


async def _heartbeat(services: Services) -> None:
    """Proof that the event loop is turning, for two different audiences.

    Locally it touches a file the container healthcheck reads. Because this task runs on
    the bot's own loop, a fresh file means the loop is alive — which a process that
    started and then wedged would not manage, and which merely being able to run
    `validate` in a second process never proved.

    Outwardly, when configured, it pings a dead-man's switch. The job ledger and the
    catch-up sweep handle a process that comes back; this handles one that does not.
    """
    health = services.app.health
    tick = health.heartbeat_interval_seconds
    ping_every = max(1, (health.ping_interval_minutes * 60) // tick)
    ticks = 0

    while True:
        _touch(health.heartbeat_file)

        if health.ping_url and ticks % ping_every == 0:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.get(health.ping_url)
            except httpx.HTTPError:
                logger.warning("health ping failed")

        ticks += 1
        await asyncio.sleep(tick)


def _touch(path: str) -> None:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        # touch() only creates; an existing file needs its mtime moved explicitly.
        os.utime(target, None)
    except OSError:
        # A heartbeat that cannot be written should not take the bot down with it; the
        # healthcheck going stale is the correct, visible consequence.
        logger.warning("could not write the heartbeat file %s", path, exc_info=True)
