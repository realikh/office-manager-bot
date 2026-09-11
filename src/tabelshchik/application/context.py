"""What a bot handler is allowed to depend on.

Handlers used to take the composition root itself, which quietly inverted the layering:
an adapter reaching up into bootstrap. This Protocol says exactly what they need
instead, so the dependency points the right way and a handler's requirements are
readable from its own module.

``Services`` satisfies this structurally — there is no base class to inherit and no
registration to forget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from tabelshchik.application.policy import (
    ChatPolicy,
    SchedulePolicy,
    SilentPolicy,
    TempoPolicy,
)
from tabelshchik.application.ports import (
    AbsenceStore,
    AuditLog,
    Clock,
    JobLedger,
    LedgerStore,
    MaintenanceStore,
    Notifier,
    OfficeStore,
    RosterStore,
    ScheduleStore,
    UsageStore,
)
from tabelshchik.application.voice import Voice


class BotContext(Protocol):
    """Everything the Telegram handlers use, and nothing else."""

    clock: Clock
    voice: Voice
    notifier: Notifier | None
    #: Learned from Telegram at startup; needed to recognise an @mention.
    bot_username: str
    #: So the admin "check config" screen can re-validate the files on disk.
    config_dir: Path

    offices: OfficeStore
    schedule: ScheduleStore
    ledger: LedgerStore
    absences: AbsenceStore
    roster: RosterStore
    usage: UsageStore
    jobs: JobLedger
    audit: AuditLog
    maintenance: MaintenanceStore

    @property
    def schedule_policy(self) -> SchedulePolicy: ...

    @property
    def silent_policy(self) -> SilentPolicy: ...

    @property
    def tempo_policy(self) -> TempoPolicy: ...

    @property
    def chat_policy(self) -> ChatPolicy: ...

    @property
    def admin_ids(self) -> frozenset[int]: ...

    def is_admin(self, user_id: int | None) -> bool: ...
