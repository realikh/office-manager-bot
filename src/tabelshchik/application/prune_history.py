"""Keeping the database bounded without losing any fairness history.

The obvious way to bound storage — delete old rows — would quietly break the thing the
database exists for, because the ledger's rebuildability depends on replaying those rows.
So the prune folds everything older than the watermark into a per-employee checkpoint
first. The checkpoint is one row per employee, bounded by headcount rather than by time,
and is never pruned; a rebuild then means "start from the checkpoint and replay forward",
which stays exact forever rather than only until the first prune.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from tabelshchik.application.ports import (
    Clock,
    LedgerStore,
    MaintenanceStore,
    OfficeStore,
)
from tabelshchik.domain.ledger import merge_checkpoint, rebuild

#: Months are approximated in days deliberately: the watermark only has to be roughly
#: where it says it is, and calendar arithmetic here would add nothing but edge cases.
DAYS_PER_MONTH = 30


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    schedule_months: int = 3
    job_runs_days: int = 90
    audit_days: int = 180
    ai_usage_days: int = 60
    #: Raw chat messages exist only to rebuild a reply chain, which nobody follows back
    #: more than a few days.
    chat_messages_days: int = 7
    #: Remembered facts are bounded by count but not by age without this.
    chat_memory_days: int = 90
    vacuum: bool = True


@dataclass(frozen=True, slots=True)
class PruneReport:
    watermark: date
    checkpointed: int = 0
    assignments_removed: int = 0
    job_runs_removed: int = 0
    audit_removed: int = 0
    ai_usage_removed: int = 0
    absences_removed: int = 0
    chat_messages_removed: int = 0
    chat_memory_removed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0

    @property
    def anything_removed(self) -> bool:
        return bool(
            self.assignments_removed
            or self.job_runs_removed
            or self.audit_removed
            or self.ai_usage_removed
            or self.absences_removed
            or self.chat_messages_removed
            or self.chat_memory_removed
        )


def prune_history(
    *,
    offices: OfficeStore,
    ledger: LedgerStore,
    maintenance: MaintenanceStore,
    clock: Clock,
    policy: RetentionPolicy,
) -> PruneReport:
    today = clock.today()
    now = clock.now()
    watermark = today - timedelta(days=policy.schedule_months * DAYS_PER_MONTH)

    bytes_before = maintenance.database_bytes()
    checkpoint = ledger.checkpoint()
    checkpointed = 0
    assignments_removed = 0

    for office in offices.active_offices():
        stale = [
            record
            for record in ledger.day_records(office.id, upto=watermark)
            if record.day < watermark
        ]
        if stale:
            # Fold first, delete second. In that order the worst case is a checkpoint
            # that double-counts nothing because the delete failed — recoverable — rather
            # than history deleted before it was ever recorded.
            checkpoint = merge_checkpoint(checkpoint, rebuild(stale))
            checkpointed += len(stale)

        assignments_removed += maintenance.delete_schedule_before(office.id, watermark)

    if checkpointed:
        ledger.save_checkpoint(checkpoint, watermark=watermark)

    report = PruneReport(
        watermark=watermark,
        checkpointed=checkpointed,
        assignments_removed=assignments_removed,
        job_runs_removed=maintenance.delete_job_runs_before(_cutoff(now, policy.job_runs_days)),
        audit_removed=maintenance.delete_audit_before(_cutoff(now, policy.audit_days)),
        ai_usage_removed=maintenance.delete_ai_usage_before(
            today - timedelta(days=policy.ai_usage_days)
        ),
        absences_removed=maintenance.delete_absences_before(watermark),
        chat_messages_removed=maintenance.delete_chat_messages_before(
            _cutoff(now, policy.chat_messages_days)
        ),
        chat_memory_removed=maintenance.delete_chat_memory_before(
            _cutoff(now, policy.chat_memory_days)
        ),
        bytes_before=bytes_before,
    )

    if policy.vacuum and report.anything_removed:
        maintenance.vacuum()

    return replace(report, bytes_after=maintenance.database_bytes())


def _cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)
