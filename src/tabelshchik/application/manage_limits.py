"""How many AI replies a day, and to whom.

The allowance has always been per person — `ai_usage` is keyed on (user_id, day) — but it
was the same number for everybody and could only be changed by editing `app.yaml` and
deploying. Two things change that: a default an admin can move, and an override for one
person.

*An override is stored, never a copy of the default.* `None` means "whatever the default
is", so raising the default lifts everybody who has not been deliberately singled out.
Backfilling the current default onto every employee would have frozen the roster at
today's number and made the default meaningless the moment it changed.

*Zero is a real answer.* It means somebody gets no AI replies at all, which is a thing an
admin may legitimately want; "no limit of their own" is spelled `None`.
"""

from __future__ import annotations

from tabelshchik.application.ports import (
    AuditLog,
    OfficeStore,
    RosterStore,
    SettingsStore,
)

#: A fat-fingered 10000 is a bill, not a setting. Generous enough that nobody meets it by
#: accident, low enough that a stray keystroke cannot run up a month's spend in a day.
MAX_DAILY_LIMIT = 200

#: The global cap sits above the sum of what individuals could plausibly use.
MAX_GLOBAL_LIMIT = 5000


class LimitError(ValueError):
    """The requested change makes no sense. The message is shown to the admin."""


def set_employee_limit(
    *,
    employee_id: str,
    limit: int | None,
    actor_id: int,
    offices: OfficeStore,
    roster: RosterStore,
    audit: AuditLog | None = None,
) -> str:
    """Give one person their own allowance, or `None` to put them back on the default."""
    employee = offices.get_employee(employee_id)
    if employee is None:
        raise LimitError("Сотрудник не найден.")
    _check(limit, MAX_DAILY_LIMIT)

    roster.set_ai_limit(employee_id, limit)
    _audit(audit, actor_id, "ai.limit.employee", {"employee": employee_id, "limit": limit})
    return employee.full_name


def set_default_limit(
    *,
    limit: int | None,
    actor_id: int,
    settings: SettingsStore,
    audit: AuditLog | None = None,
) -> None:
    """Move the number everybody without an override gets. `None` restores app.yaml's."""
    _check(limit, MAX_DAILY_LIMIT)
    settings.set_ai_daily_limit(limit)
    _audit(audit, actor_id, "ai.limit.default", {"limit": limit})


def set_global_limit(
    *,
    limit: int | None,
    actor_id: int,
    settings: SettingsStore,
    audit: AuditLog | None = None,
) -> None:
    """The cost stop-loss across everybody.

    Worth changing alongside the per-person default: raising everyone to thirty while this
    stays at two hundred means the bot goes quiet for the whole office once the cap is
    reached, which reads as a fault rather than as a budget.
    """
    _check(limit, MAX_GLOBAL_LIMIT)
    settings.set_ai_global_daily_limit(limit)
    _audit(audit, actor_id, "ai.limit.global", {"limit": limit})


def _check(limit: int | None, ceiling: int) -> None:
    if limit is None:
        return
    if limit < 0:
        raise LimitError("Лимит не может быть отрицательным.")
    if limit > ceiling:
        raise LimitError(f"Слишком много — не больше {ceiling}.")


def _audit(
    audit: AuditLog | None, actor_id: int | None, action: str, payload: dict[str, object]
) -> None:
    if audit is not None:
        audit.record(actor_id=actor_id, action=action, payload=payload)
