"""The bot actually assembles.

Cheap to run and catches the whole class of "it imports fine but explodes on boot"
mistakes: a handler asking for a dependency the container does not expose, a router
registered in the wrong order, a protocol the container stopped satisfying.
"""

from __future__ import annotations

import gc
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tabelshchik.adapters.telegram.bot import create_dispatcher
from tabelshchik.application.context import BotContext
from tabelshchik.bootstrap.container import build_services
from tabelshchik.bootstrap.jobs import register_jobs
from tabelshchik.bootstrap.settings import Secrets, load_secrets


@pytest.fixture
def services(tmp_path: Path):
    built = build_services(
        config_dir=Path("config"),
        database_path=tmp_path / "t.db",
        secrets=Secrets(telegram_bot_token="x:y", admin_ids=frozenset({1})),
    )
    # Booting no longer creates offices — they are made from the bot. Everything below
    # that needs one puts it there itself, the way an admin would.
    _seed_two_offices(built)
    yield built
    built.engine.dispose()
    # Force collection here so a late-finalised connection is attributed to this test
    # rather than surfacing as an unraisable warning inside an unrelated one.
    gc.collect()


def test_the_container_satisfies_the_handler_protocol(services) -> None:
    """Static typing already checks this; the runtime check catches a field that was
    renamed on one side only."""
    context: BotContext = services
    assert context.offices is not None
    assert context.schedule_policy.horizon_weeks >= 1


def test_a_fresh_database_starts_with_no_offices(tmp_path: Path) -> None:
    """The headline behaviour change. An office is something an admin creates, so a new
    deployment has none at all until somebody does."""
    built = build_services(
        config_dir=Path("config"),
        database_path=tmp_path / "t.db",
        secrets=Secrets(telegram_bot_token="x:y", admin_ids=frozenset({1})),
    )
    try:
        assert built.offices.all_offices() == []
        # And it still boots: the config is valid, the schema is up, the owner is seeded.
        assert built.is_owner(1)
    finally:
        built.engine.dispose()
        gc.collect()


def test_the_container_sees_the_offices_that_are_there(services) -> None:
    assert {office.id for office in services.offices.active_offices()} == {
        "ovest",
        "pine-office-park",
    }
    assert len(services.offices.employees("pine-office-park")) == 12


def test_booting_twice_does_not_undo_an_admins_edits(tmp_path: Path) -> None:
    """It cannot any more — booting reads no office files at all — but this is the rule
    that mattered, so it stays pinned rather than deleted along with the mechanism."""
    database = tmp_path / "t.db"
    first = build_services(config_dir=Path("config"), database_path=database, secrets=Secrets())
    _seed_two_offices(first)
    first.roster.rename_employee("p0", "Изменено Админом")
    first.engine.dispose()
    gc.collect()

    second = build_services(config_dir=Path("config"), database_path=database, secrets=Secrets())
    try:
        names = {e.id: e.full_name for e in second.offices.employees("ovest")}
        assert names["p0"] == "Изменено Админом"
    finally:
        second.engine.dispose()
        gc.collect()


def test_the_dispatcher_builds_with_the_routers_in_order(services) -> None:
    """One test, not two: aiogram routers are module-level singletons and may only be
    attached to a single dispatcher, so a second build in the same process fails. That
    is fine in production — there is exactly one dispatcher — but it means this has to
    assert everything in one pass.

    Order is load-bearing: chat answers anything nothing else claimed, so registering it
    earlier would swallow every command.
    """
    dispatcher = create_dispatcher(services)
    names = [router.name for router in dispatcher.sub_routers]

    assert names == ["common", "admin", "bind", "employee", "chat"]
    assert names[-1] == "chat"


def test_jobs_are_registered_per_office(services) -> None:
    from tabelshchik.adapters.scheduling.runner import JobRunner

    runner = JobRunner(ledger=services.jobs, timezone=services.config.app.timezone)
    register_jobs(runner, services)

    labels = {f"{job.name}:{job.scope}" if job.scope else job.name for job in runner.jobs}
    assert "attendance:ovest" in labels
    assert "attendance:pine-office-park" in labels
    assert "prune" in labels


def test_the_attendance_job_uses_the_configured_time_and_weekdays(services) -> None:
    from tabelshchik.adapters.scheduling.runner import JobRunner

    runner = JobRunner(ledger=services.jobs, timezone=services.config.app.timezone)
    register_jobs(runner, services)

    job = next(item for item in runner.jobs if item.name == "attendance")
    rendered = str(job.trigger)
    assert "hour='15'" in rendered and "minute='30'" in rendered
    assert "sun" in rendered  # this is what covers Sunday -> Monday


def test_without_an_openai_key_the_bot_still_has_a_voice(services) -> None:
    """Every message falls back to the hand-written corpus, so the AI is optional."""
    assert services.voice.model is None
    assert services.voice.catalog.variants["attendance.intro"]


def test_admin_ids_come_from_the_environment_when_set() -> None:
    secrets = load_secrets({"ADMIN_IDS": "1, 2;3", "ADMIN_CHAT_ID": "42"})
    assert secrets.admin_ids == frozenset({1, 2, 3})
    assert secrets.admin_chat_id == 42


def test_malformed_ids_are_ignored_rather_than_crashing_the_boot() -> None:
    secrets = load_secrets({"ADMIN_IDS": "1,not-a-number,", "ADMIN_CHAT_ID": "nonsense"})
    assert secrets.admin_ids == frozenset({1})
    assert secrets.admin_chat_id is None


# ------------------------------------------------------------------------- adminship


def test_the_first_boot_makes_the_environment_ids_admins(services) -> None:
    """ADMIN_IDS is a bootstrap for an empty table, not a permanent grant."""
    assert services.admin_ids == frozenset({1})
    assert services.is_owner(1)
    assert services.is_admin(1)


def test_a_promotion_takes_effect_without_a_restart(services) -> None:
    """The reason `admin_ids` is not cached. Adminship moved out of `.env` precisely so
    that granting it would not need a deploy; a cache would put the deploy back."""
    assert services.is_admin(77) is False
    services.admins.grant(77, label="Новый", at=datetime(2026, 9, 11, 12, 0))
    assert services.is_admin(77) is True
    assert services.is_owner(77) is False


def test_an_owner_who_leaves_is_not_re_granted_by_the_environment(tmp_path: Path) -> None:
    """The whole feature. Hand the bot over, remove yourself, and stay removed — even
    though `.env` still names you, and even across a restart."""
    secrets = Secrets(telegram_bot_token="x:y", admin_ids=frozenset({1}))
    first = build_services(
        config_dir=Path("config"), database_path=tmp_path / "t.db", secrets=secrets
    )
    first.admins.grant(2, at=datetime(2026, 9, 11, 12, 0))
    first.admins.transfer_ownership(to_user_id=2, at=datetime(2026, 9, 11, 12, 0))
    first.admins.revoke(1)
    first.engine.dispose()

    second = build_services(
        config_dir=Path("config"), database_path=tmp_path / "t.db", secrets=secrets
    )
    try:
        assert second.is_admin(1) is False
        assert second.admin_ids == frozenset({2})
        assert second.is_owner(2)
    finally:
        second.engine.dispose()
        gc.collect()


def test_the_backup_falls_back_to_the_owner_when_no_admin_chat_is_set(services) -> None:
    """A deployment that never set ADMIN_CHAT_ID still gets its nightly backup."""
    assert services.secrets.admin_chat_id is None
    assert services.admin_chat_id == 1


def test_an_explicit_admin_chat_id_still_wins(tmp_path: Path) -> None:
    built = build_services(
        config_dir=Path("config"),
        database_path=tmp_path / "t.db",
        secrets=Secrets(telegram_bot_token="x:y", admin_ids=frozenset({1}), admin_chat_id=-100),
    )
    try:
        assert built.admin_chat_id == -100
    finally:
        built.engine.dispose()
        gc.collect()


def test_a_fresh_deployment_generates_its_first_schedule(services) -> None:
    """Without this a Monday deploy sits silent until Thursday's job fires, which looks
    exactly like a broken bot."""
    from tabelshchik.bootstrap.lifespan import _seed_empty_schedules

    today = services.clock.today()
    assert not services.schedule.has_schedule_from("ovest", today)

    _seed_empty_schedules(services)

    assert services.schedule.has_schedule_from("ovest", today)


def test_a_second_boot_does_not_rebuild_an_existing_schedule(services) -> None:
    """It is a cold start, not a rebuild: an admin's manual changes must survive a
    restart."""
    from tabelshchik.bootstrap.lifespan import _seed_empty_schedules

    _seed_empty_schedules(services)
    today = services.clock.today()
    before = [
        (snapshot.day, snapshot.roster)
        for snapshot in services.schedule.days_between("ovest", today, today + timedelta(days=40))
    ]

    _seed_empty_schedules(services)

    after = [
        (snapshot.day, snapshot.roster)
        for snapshot in services.schedule.days_between("ovest", today, today + timedelta(days=40))
    ]
    assert after == before


async def test_the_first_boot_does_not_replay_work_from_before_the_bot_existed(
    services,
) -> None:
    """An empty job ledger means no history. "Recovering" a missed reminder would fire
    one at whatever hour the bot happened to be installed."""
    from tabelshchik.bootstrap.lifespan import lifespan

    async with lifespan(services) as runtime:
        assert runtime.runner.jobs
        # The catch-up sweep was skipped, so nothing was claimed.
        assert services.jobs.recent(limit=5) == []


async def test_a_later_boot_does_catch_up(services) -> None:
    """Once there is history, a missed occurrence is replayed — the whole point."""
    from datetime import datetime

    from tabelshchik.bootstrap.lifespan import lifespan

    # Pretend the bot has run before.
    services.jobs.claim("seed:1", job="attendance", scheduled_for=datetime(2026, 1, 1))
    services.jobs.complete("seed:1", at=datetime(2026, 1, 1))

    async with lifespan(services) as runtime:
        assert runtime.runner.jobs
        keys = [key for _job, key, *_rest in services.jobs.recent(limit=50)]
        assert any(key != "seed:1" for key in keys), "catch-up claimed nothing"


def test_the_schedule_is_seeded_before_catch_up_runs() -> None:
    """Ordering matters: a catch-up with no schedule marks the occurrence done and
    swallows the reminder it was meant to recover."""
    import inspect

    from tabelshchik.bootstrap import lifespan as module

    source = inspect.getsource(module.lifespan)
    assert source.index("_seed_empty_schedules") < source.index("runner.catch_up")


# ------------------------------------------------------------------------ liveness


async def test_the_heartbeat_runs_without_an_external_ping_url(services, tmp_path) -> None:
    """It used to run only when a dead-man's switch was configured. The container
    healthcheck reads the same file, so it has to run either way."""
    import asyncio

    from tabelshchik.bootstrap.lifespan import lifespan

    beat = tmp_path / "beat"
    # The config models are frozen, so redirect the path the blunt way for this test.
    object.__setattr__(services.config.app.health, "heartbeat_file", str(beat))
    object.__setattr__(services.config.app.health, "heartbeat_interval_seconds", 5)
    assert not services.config.app.health.ping_url

    async with lifespan(services):
        await asyncio.sleep(0.1)  # let the task take its first tick
        assert beat.exists(), "no heartbeat written"


def test_an_unwritable_heartbeat_does_not_take_the_bot_down(services) -> None:
    """A heartbeat that cannot be written should go stale — visibly — not crash the
    process it exists to monitor."""
    from tabelshchik.bootstrap.lifespan import _touch

    _touch("/proc/definitely/not/writable/beat")  # must not raise


def _seed_two_offices(services) -> None:
    """Two offices of the shape the shipped ones had, put there the way an admin would.

    The sizes matter: twelve people against eleven Friday desks is the dense case the
    solver has to clear without a shortfall, and a toy office of four proves much less.
    """
    from tabelshchik.adapters.db.engine import session_scope
    from tabelshchik.adapters.db.seed import seed_offices

    from .conftest import NOW, office_seed

    dense = office_seed(
        id="ovest",
        name="O'Vest",
        chatId=-100123,
        employees=[{"id": f"p{index}", "name": f"Сотрудник {index}"} for index in range(12)],
        schedule={"vacantDesks": {"friday": 11}},
    )
    fixed = office_seed(
        id="pine-office-park",
        name="Pine Office Park",
        chatId=-100123,
        employees=[{"id": f"q{index}", "name": f"Коллега {index}"} for index in range(12)],
        schedule={"fixed": {"monday": ["q0", "q1"], "friday": ["q2"]}},
    )
    with session_scope(services.sessions) as session:
        seed_offices(session, [dense, fixed], now=NOW)
