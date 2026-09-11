"""The composition root.

Everything is wired here and nowhere else. Handlers and use cases receive what they need
rather than reaching for it, which is what keeps them testable and what makes the
dependency graph something you can read in one file instead of inferring from imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from tabelshchik.adapters.clock import SystemClock
from tabelshchik.adapters.db.engine import (
    create_db_engine,
    create_session_factory,
    session_scope,
    upgrade_schema,
)
from tabelshchik.adapters.db.repositories import (
    SqlAbsenceStore,
    SqlAuditLog,
    SqlJobLedger,
    SqlLedgerStore,
    SqlMaintenance,
    SqlMoodStore,
    SqlOfficeStore,
    SqlRosterStore,
    SqlScheduleStore,
    SqlUsageStore,
)
from tabelshchik.adapters.db.seed import seed_offices
from tabelshchik.adapters.openai.client import OpenAiChatModel
from tabelshchik.application.policy import (
    ChatPolicy,
    SchedulePolicy,
    SilentPolicy,
    TempoPolicy,
)
from tabelshchik.application.ports import (
    AbsenceStore,
    AuditLog,
    ChatModel,
    Clock,
    JobLedger,
    LedgerStore,
    MaintenanceStore,
    MoodStore,
    Notifier,
    OfficeStore,
    RosterStore,
    ScheduleStore,
    UsageStore,
)
from tabelshchik.application.prune_history import RetentionPolicy
from tabelshchik.application.voice import Voice
from tabelshchik.bootstrap.mapping import (
    catalog,
    mood_policy,
    schedule_policy,
    silent_policy,
)
from tabelshchik.bootstrap.settings import Secrets
from tabelshchik.config.loader import LoadedConfig, load, parse_file
from tabelshchik.config.messages import MessagesConfig
from tabelshchik.config.models import AppConfig


@dataclass
class Services:
    """Everything the handlers and jobs need, assembled once."""

    config: LoadedConfig
    messages: MessagesConfig
    secrets: Secrets
    #: Kept so the admin "check config" screen can re-validate the files on disk.
    config_dir: Path
    engine: Engine
    sessions: sessionmaker[Session]

    clock: Clock
    voice: Voice
    notifier: Notifier | None = None
    #: Learned from Telegram at startup; needed to recognise an @mention.
    bot_username: str = ""

    # Declared as the ports, not the SQL classes: handlers depend on the protocol, and a
    # mutable attribute typed by its implementation would not satisfy one.
    offices: OfficeStore = field(init=False)
    schedule: ScheduleStore = field(init=False)
    ledger: LedgerStore = field(init=False)
    absences: AbsenceStore = field(init=False)
    roster: RosterStore = field(init=False)
    usage: UsageStore = field(init=False)
    moods: MoodStore = field(init=False)
    jobs: JobLedger = field(init=False)
    audit: AuditLog = field(init=False)
    maintenance: MaintenanceStore = field(init=False)

    def __post_init__(self) -> None:
        self.offices = SqlOfficeStore(self.sessions)
        self.schedule = SqlScheduleStore(self.sessions)
        self.ledger = SqlLedgerStore(self.sessions)
        self.absences = SqlAbsenceStore(self.sessions)
        self.roster = SqlRosterStore(self.sessions)
        self.usage = SqlUsageStore(self.sessions)
        self.moods = SqlMoodStore(self.sessions)
        self.jobs = SqlJobLedger(self.sessions)
        self.audit = SqlAuditLog(self.sessions)
        self.maintenance = SqlMaintenance(self.sessions)

    # ---------------------------------------------------------------- policy views

    @property
    def app(self) -> AppConfig:
        return self.config.app

    @property
    def schedule_policy(self) -> SchedulePolicy:
        return schedule_policy(self.app)

    @property
    def silent_policy(self) -> SilentPolicy:
        return silent_policy(self.app)

    @property
    def retention_policy(self) -> RetentionPolicy:
        section = self.app.retention
        return RetentionPolicy(
            schedule_months=section.schedule_months,
            job_runs_days=section.job_runs_days,
            audit_days=section.audit_days,
            ai_usage_days=section.ai_usage_days,
        )

    @property
    def tempo_policy(self) -> TempoPolicy:
        return TempoPolicy(url=self.messages.tempo.url)

    @property
    def chat_policy(self) -> ChatPolicy:
        section = self.app.ai
        return ChatPolicy(
            enabled=section.enabled,
            per_user_daily_limit=section.per_user_daily_limit,
            global_daily_limit=section.global_daily_limit,
            max_tokens=section.max_tokens,
            temperature=section.temperature,
            triggers=frozenset(section.triggers),
        )

    @property
    def admin_ids(self) -> frozenset[int]:
        return frozenset(self.secrets.admin_ids or self.app.admins)

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids


def build_services(
    *,
    config_dir: Path,
    database_path: Path,
    secrets: Secrets,
    notifier: Notifier | None = None,
) -> Services:
    config = load(config_dir)
    messages = parse_file(config_dir / "messages.yaml", MessagesConfig)

    engine = create_db_engine(database_path)
    upgrade_schema(engine)
    sessions = create_session_factory(engine)

    # Seeds are a bootstrap, not a live source: an office that already exists in the
    # database is left exactly as the admins have edited it.
    with session_scope(sessions) as session:
        seed_offices(session, config.offices, now=SystemClock(config.app.timezone).now())

    model = _chat_model(config.app, secrets)

    return Services(
        config=config,
        messages=messages,
        secrets=secrets,
        config_dir=config_dir,
        engine=engine,
        sessions=sessions,
        clock=SystemClock(config.app.timezone),
        voice=Voice(
            catalog=catalog(messages),
            moods=mood_policy(config.app),
            model=model,
            max_tokens=config.app.ai.max_tokens,
            temperature=config.app.ai.temperature,
        ),
        notifier=notifier,
    )


def _chat_model(app: AppConfig, secrets: Secrets) -> ChatModel | None:
    if not app.ai.enabled or not secrets.openai_api_key:
        return None
    return OpenAiChatModel(
        api_key=secrets.openai_api_key,
        model=app.ai.model,
        timeout=app.ai.request_timeout_seconds,
        max_retries=app.ai.max_retries,
    )
