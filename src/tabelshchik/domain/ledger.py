"""The persistent fairness ledger.

The ledger is a *cache*, not a source of truth: it must be exactly reproducible by
replaying raw day records forward from a checkpoint. Fairness counters that are only ever
mutated in place, and cannot be rebuilt, are precisely how the previous implementation's
fairness drifted with nobody noticing. Being able to rebuild gives both a repair path and
a free correctness test.

Everything is fixed-point integer arithmetic. Float drift over a year of daily accrual is
small, but it is exactly what would make a fairness claim unfalsifiable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from tabelshchik.domain.entities import SCALE, LedgerEntry
from tabelshchik.domain.instance import DayPlan


@dataclass(frozen=True, slots=True)
class DayRecord:
    """What was actually true about one office day, once it was committed.

    Storing the realised facts — who was available, what a head's share cost — is what
    lets the ledger be rebuilt without versioning the weekly template. Editing the
    template tomorrow cannot retroactively rewrite what last month meant.
    """

    day: date
    weekday: int
    available: frozenset[str]
    entitlement_scaled: int
    attended: frozenset[str]

    @classmethod
    def from_plan(cls, plan: DayPlan, attended: Iterable[str]) -> DayRecord:
        return cls(
            day=plan.day,
            weekday=plan.weekday,
            available=plan.available,
            entitlement_scaled=plan.entitlement_scaled,
            attended=frozenset(attended),
        )


def apply_day(entries: Mapping[str, LedgerEntry], record: DayRecord) -> dict[str, LedgerEntry]:
    """Fold one day into the ledger. Pure: returns a new mapping."""
    updated = dict(entries)

    touched = record.available | record.attended
    for employee_id in touched:
        entry = updated.get(employee_id) or LedgerEntry(employee_id=employee_id)

        came_in = employee_id in record.attended
        # Entitlement accrues only to people who were actually available. Someone on
        # vacation owes nothing for the days they were away, so they come back to a
        # normal share instead of a month of catch-up duty.
        accrued = record.entitlement_scaled if employee_id in record.available else 0

        per_weekday = list(entry.per_weekday)
        if came_in:
            per_weekday[record.weekday] += 1

        updated[employee_id] = LedgerEntry(
            employee_id=employee_id,
            days=entry.days + (1 if came_in else 0),
            entitlement_scaled=entry.entitlement_scaled + accrued,
            per_weekday=tuple(per_weekday),
        )

    return updated


def rebuild(
    records: Iterable[DayRecord],
    *,
    checkpoint: Mapping[str, LedgerEntry] | None = None,
) -> dict[str, LedgerEntry]:
    """Replay ``records`` forward from ``checkpoint`` to reproduce the ledger exactly.

    After the retention prune has deleted old rows, the checkpoint is where replay
    starts — which is what keeps this property true forever rather than only until the
    first prune.
    """
    entries: dict[str, LedgerEntry] = dict(checkpoint or {})
    for record in sorted(records, key=lambda item: item.day):
        entries = apply_day(entries, record)
    return entries


def surplus_days(entry: LedgerEntry) -> float:
    """Surplus in whole days, for display only. Never feed this back into the solver."""
    return entry.surplus_scaled / SCALE


def spread_scaled(entries: Iterable[LedgerEntry]) -> int:
    """Max-minus-min surplus — the single number that shows whether this is working."""
    surpluses = [entry.surplus_scaled for entry in entries]
    if not surpluses:
        return 0
    return max(surpluses) - min(surpluses)


def merge_checkpoint(
    checkpoint: Mapping[str, LedgerEntry], entries: Mapping[str, LedgerEntry]
) -> dict[str, LedgerEntry]:
    """Fold a rollup into a checkpoint, for the nightly retention prune.

    One row per employee, bounded by headcount rather than by time, and never pruned —
    so deleting the raw rows behind it loses no fairness information.
    """
    merged = dict(checkpoint)
    for employee_id, entry in entries.items():
        existing = merged.get(employee_id)
        if existing is None:
            merged[employee_id] = entry
            continue
        merged[employee_id] = LedgerEntry(
            employee_id=employee_id,
            days=existing.days + entry.days,
            entitlement_scaled=existing.entitlement_scaled + entry.entitlement_scaled,
            per_weekday=tuple(
                a + b for a, b in zip(existing.per_weekday, entry.per_weekday, strict=True)
            ),
        )
    return merged
