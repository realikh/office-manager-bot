"""Regenerating an office schedule.

Always a diff, never a truncate-and-rewrite. Keeping the delta is what makes churn
measurable, lets "three changes this week" be answered, and guarantees that a day already
announced cannot silently move under the people who planned around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from tabelshchik.application.policy import SchedulePolicy
from tabelshchik.application.ports import (
    Clock,
    LedgerStore,
    OfficeStore,
    ScheduleDiff,
    ScheduleStore,
)
from tabelshchik.domain.calendar import start_of_week
from tabelshchik.domain.entities import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
)
from tabelshchik.domain.instance import ScheduleInstance, build_instance
from tabelshchik.domain.ledger import DayRecord
from tabelshchik.domain.rng import stable_hash
from tabelshchik.domain.solver import DraftResult, solve


class FrozenDayChangedError(RuntimeError):
    """A regeneration tried to move a day that was already announced. Never expected."""


@dataclass(frozen=True, slots=True)
class Regeneration:
    office_id: str
    horizon_start: date
    horizon_end: date
    instance: ScheduleInstance
    draft: DraftResult
    diff: ScheduleDiff
    generation_id: int

    @property
    def shortfall(self) -> int:
        return self.draft.shortfall

    @property
    def changed(self) -> bool:
        return not self.diff.is_empty

    def roster_on(self, day: date) -> tuple[str, ...]:
        plan = self.instance.day_by_date(day)
        if plan is None:
            return ()
        return tuple(sorted(plan.committed | set(self.draft.on(day))))


def regenerate(
    *,
    office_id: str,
    offices: OfficeStore,
    schedule: ScheduleStore,
    ledger: LedgerStore,
    clock: Clock,
    policy: SchedulePolicy,
    triggered_by: str = "cron",
    anchor: date | None = None,
) -> Regeneration:
    today = clock.today()
    # Horizons are whole ISO weeks so the per-week fairness layer lines up with how
    # people actually think about their week.
    start = anchor or start_of_week(today)
    end = start + timedelta(days=policy.horizon_weeks * 7 - 1)

    context = offices.planning_context(office_id, start=start, end=end)
    frozen = _frozen_days(schedule, office_id, start=start, end=end, today=today, policy=policy)

    instance = build_instance(
        office=context.office,
        template=context.template,
        employees=context.employees,
        absences=context.absences,
        spec=context.spec,
        anchor=start,
        horizon_weeks=policy.horizon_weeks,
        ledger=ledger.entries(office_id),
        frozen=frozen,
        max_days_per_week=policy.max_days_per_week,
        surplus_clamp_days=policy.surplus_clamp_days,
        absence_policy=policy.absence_policy,
    )
    draft = solve(instance)

    existing = {
        (snapshot.day, assignment.employee_id): assignment
        for snapshot in schedule.days_between(office_id, start, end)
        for assignment in snapshot.assignments
        if assignment.is_live
    }
    diff = _build_diff(office_id, instance, draft, existing)

    _assert_frozen_preserved(instance, diff)

    generation_id = schedule.record_generation(
        office_id=office_id,
        horizon_start=start,
        horizon_end=end,
        seed=instance.seed,
        input_hash=_input_hash(instance),
        objective=str(draft.objective),
        diff=diff,
        shortfall=draft.shortfall,
        triggered_by=triggered_by,
        at=clock.now(),
    )
    schedule.apply(office_id, diff, generation_id=generation_id)

    return Regeneration(
        office_id=office_id,
        horizon_start=start,
        horizon_end=end,
        instance=instance,
        draft=draft,
        diff=diff,
        generation_id=generation_id,
    )


def _frozen_days(
    schedule: ScheduleStore,
    office_id: str,
    *,
    start: date,
    end: date,
    today: date,
    policy: SchedulePolicy,
) -> dict[date, frozenset[str]]:
    """Draft decisions this run may not revisit.

    Three sources: days already announced, admin pins, and anything inside the freeze
    window — which also covers days earlier in the current week, since the horizon is
    anchored to Monday and the past is not up for renegotiation.
    """
    frozen = dict(schedule.frozen_drafted(office_id, start, end))

    freeze_until = max(today + timedelta(days=1), start + timedelta(days=policy.freeze_weeks * 7))

    for snapshot in schedule.days_between(office_id, start, min(end, freeze_until)):
        if snapshot.day >= freeze_until:
            continue
        drafted = frozenset(
            assignment.employee_id
            for assignment in snapshot.assignments
            if assignment.is_live and assignment.source is not AssignmentSource.FIXED
        )
        if drafted:
            frozen[snapshot.day] = frozen.get(snapshot.day, frozenset()) | drafted

    return frozen


def _build_diff(
    office_id: str,
    instance: ScheduleInstance,
    draft: DraftResult,
    existing: dict[tuple[date, str], Assignment],
) -> ScheduleDiff:
    added: list[Assignment] = []
    removed: list[Assignment] = []
    kept = 0
    records: list[DayRecord] = []
    wanted: set[tuple[date, str]] = set()

    for plan in instance.days:
        drafted = set(draft.on(plan.day))
        attended = plan.committed | drafted

        # A working day on which the office expects nobody contributes nothing to the
        # schedule and nothing to the ledger, so it earns no row.
        if plan.desks_total > 0:
            records.append(DayRecord.from_plan(plan, attended))

        for employee_id in sorted(attended):
            key = (plan.day, employee_id)
            wanted.add(key)
            source = (
                AssignmentSource.FIXED if employee_id in plan.fixed else AssignmentSource.DRAFTED
            )

            current = existing.get(key)
            if current is None:
                added.append(
                    Assignment(
                        office_id=office_id,
                        day=plan.day,
                        employee_id=employee_id,
                        source=source,
                    )
                )
            elif current.source is not source:
                # Someone moved between the fixed roster and the draft; record the change
                # rather than leaving a row that misdescribes why they are coming in.
                removed.append(current)
                added.append(
                    Assignment(
                        office_id=office_id,
                        day=plan.day,
                        employee_id=employee_id,
                        source=source,
                    )
                )
            else:
                kept += 1

    horizon_days = {plan.day for plan in instance.days}
    for key, assignment in existing.items():
        day, _ = key
        if key in wanted:
            continue
        if assignment.status is not AssignmentStatus.PROVISIONAL:
            # Announced assignments are never dropped silently; an absence cancels them
            # explicitly through the absence flow instead.
            continue
        if day in horizon_days or day < instance.anchor:
            removed.append(assignment)

    return ScheduleDiff(
        added=tuple(added),
        removed=tuple(removed),
        kept=kept,
        days=tuple(records),
    )


def _assert_frozen_preserved(instance: ScheduleInstance, diff: ScheduleDiff) -> None:
    """An invariant, not a hope: a frozen day must survive the regeneration intact."""
    dropped = {(assignment.day, assignment.employee_id) for assignment in diff.removed}
    for plan in instance.days:
        for employee_id in plan.frozen:
            if (plan.day, employee_id) in dropped:
                raise FrozenDayChangedError(
                    f"regeneration tried to drop {employee_id} from {plan.day}, "
                    "which is already committed"
                )


def _input_hash(instance: ScheduleInstance) -> str:
    """Enough to replay the exact instance when someone asks why they are in on Friday."""
    parts = [instance.office_id, instance.anchor.isoformat(), str(instance.seed)]
    for plan in instance.days:
        parts.append(
            f"{plan.day}|{sorted(plan.fixed)}|{sorted(plan.frozen)}"
            f"|{sorted(plan.eligible)}|{plan.demand}"
        )
    for employee_id in instance.employees:
        parts.append(f"{employee_id}={instance.base_scaled[employee_id]}")
    return f"{stable_hash(*parts):016x}"
