"""The composition root.

Everything is wired here and nowhere else. Handlers and use cases receive what they need
rather than reaching for it, which is what keeps them testable and what makes the
dependency graph something you can read in one file instead of inferring from imports.
"""

from __future__ import annotations

import logging
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
    SqlAdminStore,
    SqlAuditLog,
    SqlChatMemoryStore,
    SqlJobLedger,
    SqlLedgerStore,
    SqlMaintenance,
    SqlMessageCache,
    SqlMoodStore,
    SqlOfficeAdminStore,
    SqlOfficeStore,
    SqlRosterStore,
    SqlScheduleStore,
    SqlUsageStore,
)
from tabelshchik.adapters.db.seed import seed_admins, seed_offices
from tabelshchik.adapters.openai.client import OpenAiChatModel
from tabelshchik.application.policy import (
    ChatPolicy,
    SchedulePolicy,
    SilentPolicy,
    TempoPolicy,
)
from tabelshchik.application.ports import (
    AbsenceStore,
    AdminRole,
    AdminStore,
    AuditLog,
    ChatMemoryStore,
    ChatModel,
    Clock,
    JobLedger,
    LedgerStore,
    MaintenanceStore,
    MessageCache,
    MoodStore,
    Notifier,
    OfficeAdminStore,
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

logger = logging.getLogger(__name__)


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
    admins: AdminStore = field(init=False)
    office_admin: OfficeAdminStore = field(init=False)
    schedule: ScheduleStore = field(init=False)
    ledger: LedgerStore = field(init=False)
    absences: AbsenceStore = field(init=False)
    roster: RosterStore = field(init=False)
    usage: UsageStore = field(init=False)
    messages_cache: MessageCache = field(init=False)
    memories: ChatMemoryStore = field(init=False)
    moods: MoodStore = field(init=False)
    jobs: JobLedger = field(init=False)
    audit: AuditLog = field(init=False)
    maintenance: MaintenanceStore = field(init=False)

    def __post_init__(self) -> None:
        self.offices = SqlOfficeStore(self.sessions)
        self.admins = SqlAdminStore(self.sessions)
        self.office_admin = SqlOfficeAdminStore(self.sessions)
        self.schedule = SqlScheduleStore(self.sessions)
        self.ledger = SqlLedgerStore(self.sessions)
        self.absences = SqlAbsenceStore(self.sessions)
        self.roster = SqlRosterStore(self.sessions)
        self.usage = SqlUsageStore(self.sessions)
        self.messages_cache = SqlMessageCache(self.sessions)
        self.memories = SqlChatMemoryStore(self.sessions)
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
            chat_messages_days=section.chat_messages_days,
            chat_memory_days=section.chat_memory_days,
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
            reply_depth=section.chat.reply_depth,
            reply_chars=section.chat.reply_chars,
            message_chars=section.chat.message_chars,
            remember=section.chat.remember,
            general_facts=section.chat.general_facts,
            personal_facts=section.chat.personal_facts,
            fact_chars=section.chat.fact_chars,
        )

    @property
    def admin_ids(self) -> frozenset[int]:
        """Every admin. Used to publish command menus; `is_admin` is the gate.

        Read from the database rather than the environment, and deliberately not cached.
        It is a handful of rows on a local SQLite file, and an uncached read is what
        makes a promotion take effect on that person's next message instead of at the
        next deploy — which is the whole reason adminship moved out of `.env`.
        """
        return self.admins.ids()

    @property
    def admin_chat_id(self) -> int | None:
        """Where alerts and backups go. A Telegram *chat* id, not a user id list.

        Falls back to the owner's own id so a deployment that never set ADMIN_CHAT_ID
        still gets its nightly backup somewhere. Setting it explicitly still wins, and
        `scripts/deploy.sh` reads it out of `.env` directly to report a failed deploy —
        it cannot reach the database, so the variable is worth keeping set.
        """
        return self.secrets.admin_chat_id or self.admins.owner_id()

    def is_admin(self, user_id: int | None) -> bool:
        # A point lookup rather than `user_id in self.admin_ids`: this runs on every
        # update that reaches the admin middleware.
        return user_id is not None and self.admins.role_of(user_id) is not None

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and self.admins.role_of(user_id) is AdminRole.OWNER


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

    # Both are bootstraps, not live sources: an office that already exists is left
    # exactly as the admins have edited it, and once anybody is an admin in the database
    # ADMIN_IDS does nothing — which is what lets an owner hand the bot over and leave.
    now = SystemClock(config.app.timezone).now()
    with session_scope(sessions) as session:
        seed_offices(session, config.offices, now=now)
        seeded = seed_admins(session, secrets.admin_ids, now=now)
    if seeded:
        # Worth saying out loud: "the lowest id becomes the owner" is an arbitrary rule,
        # and it decides who can hand the bot to somebody else.
        logger.warning("seeded admins from ADMIN_IDS; %s is the owner", seeded[0])

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
