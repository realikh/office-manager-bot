"""Starting and stopping the bot.

The startup order matters. The catch-up sweep runs before polling begins, so a reminder
missed while the process was down goes out immediately rather than waiting behind a
queue of user messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from tabelshchik.adapters.scheduling.runner import JobRunner
from tabelshchik.adapters.telegram.bot import create_bot, create_dispatcher
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

    dispatcher = create_dispatcher(services)

    async with lifespan(services) as runtime:
        logger.info("scheduled jobs: %s", [job.name for job in runtime.runner.jobs])
        try:
            await dispatcher.start_polling(bot, handle_signals=False)
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

    # Before polling starts: whatever was missed while the process was down goes out now.
    replayed = await runner.catch_up(
        now=services.clock.now(), grace_hours=services.app.health.catch_up_grace_hours
    )
    if replayed:
        logger.warning("replayed %s missed job(s) on startup", len(replayed))

    runner.start()
    runtime = Runtime(services=services, runner=runner)

    if services.app.health.ping_url:
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


async def _heartbeat(services: Services) -> None:
    """An external dead-man's switch.

    The job ledger and the catch-up sweep handle a process that comes back. This handles
    one that does not: if the pings stop, whoever is watching finds out in minutes rather
    than through a week of missing reminders.
    """
    url = services.app.health.ping_url
    interval = services.app.health.ping_interval_minutes * 60

    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(url)
        except httpx.HTTPError:
            logger.warning("health ping failed")
        await asyncio.sleep(interval)
