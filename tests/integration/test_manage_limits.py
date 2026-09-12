"""Moving the AI allowance without a deploy.

The allowance was always per person — `ai_usage` is keyed on (user_id, day) — but it was
the same number for everybody and lived in app.yaml. These pin what replaced that: a
default an admin can move, an override for one person, and the rule that ties them
together.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tabelshchik.adapters.db.repositories import (
    SqlAuditLog,
    SqlOfficeStore,
    SqlRosterStore,
    SqlSettingsStore,
)
from tabelshchik.application import manage_limits
from tabelshchik.application.manage_limits import (
    MAX_DAILY_LIMIT,
    MAX_GLOBAL_LIMIT,
    LimitError,
)
from tabelshchik.bootstrap.container import build_services
from tabelshchik.bootstrap.settings import Secrets

from .conftest import seed

NOW = datetime(2026, 9, 11, 12, 0)
ADMIN = 1


@pytest.fixture
def stores(sessions):
    seed(sessions)
    return {
        "offices": SqlOfficeStore(sessions),
        "roster": SqlRosterStore(sessions),
        "settings": SqlSettingsStore(sessions),
        "audit": SqlAuditLog(sessions),
    }


# ------------------------------------------------------------------ per-person override


def test_singling_somebody_out_stores_an_override(stores) -> None:
    manage_limits.set_employee_limit(
        employee_id="anya",
        limit=25,
        actor_id=ADMIN,
        offices=stores["offices"],
        roster=stores["roster"],
        audit=stores["audit"],
    )
    anya = stores["offices"].get_employee("anya")
    assert anya is not None and anya.ai_daily_limit == 25


def test_nobody_has_an_override_to_begin_with(stores) -> None:
    """Nullable rather than backfilled: a copy of today's default on every row would
    freeze the roster and make changing the default do nothing."""
    assert all(employee.ai_daily_limit is None for employee in stores["offices"].employees("ovest"))


def test_clearing_puts_them_back_on_the_default(stores) -> None:
    common = {
        "actor_id": ADMIN,
        "offices": stores["offices"],
        "roster": stores["roster"],
        "audit": stores["audit"],
    }
    manage_limits.set_employee_limit(employee_id="anya", limit=0, **common)
    manage_limits.set_employee_limit(employee_id="anya", limit=None, **common)

    anya = stores["offices"].get_employee("anya")
    assert anya is not None and anya.ai_daily_limit is None


def test_zero_is_a_real_answer_and_not_the_same_as_clearing(stores) -> None:
    manage_limits.set_employee_limit(
        employee_id="anya",
        limit=0,
        actor_id=ADMIN,
        offices=stores["offices"],
        roster=stores["roster"],
        audit=stores["audit"],
    )
    anya = stores["offices"].get_employee("anya")
    assert anya is not None and anya.ai_daily_limit == 0


def test_an_absurd_number_is_refused(stores) -> None:
    """A fat-fingered 10000 is a bill, not a setting."""
    with pytest.raises(LimitError, match="Слишком много"):
        manage_limits.set_employee_limit(
            employee_id="anya",
            limit=MAX_DAILY_LIMIT + 1,
            actor_id=ADMIN,
            offices=stores["offices"],
            roster=stores["roster"],
            audit=stores["audit"],
        )


def test_an_unknown_employee_is_reported(stores) -> None:
    with pytest.raises(LimitError, match="не найден"):
        manage_limits.set_employee_limit(
            employee_id="nobody",
            limit=5,
            actor_id=ADMIN,
            offices=stores["offices"],
            roster=stores["roster"],
            audit=stores["audit"],
        )


# ------------------------------------------------------------------------- the defaults


def test_the_default_is_an_override_of_the_file(stores) -> None:
    assert stores["settings"].ai_daily_limit() is None
    manage_limits.set_default_limit(
        limit=20, actor_id=ADMIN, settings=stores["settings"], audit=stores["audit"]
    )
    assert stores["settings"].ai_daily_limit() == 20


def test_clearing_the_default_restores_the_configured_one(stores) -> None:
    """A missing row means the file wins, so a default changed in a release still lands."""
    manage_limits.set_default_limit(
        limit=20, actor_id=ADMIN, settings=stores["settings"], audit=stores["audit"]
    )
    manage_limits.set_default_limit(
        limit=None, actor_id=ADMIN, settings=stores["settings"], audit=stores["audit"]
    )
    assert stores["settings"].ai_daily_limit() is None


def test_the_global_cap_moves_separately(stores) -> None:
    manage_limits.set_global_limit(
        limit=1000, actor_id=ADMIN, settings=stores["settings"], audit=stores["audit"]
    )
    assert stores["settings"].ai_global_daily_limit() == 1000
    assert stores["settings"].ai_daily_limit() is None


def test_an_absurd_global_cap_is_refused(stores) -> None:
    with pytest.raises(LimitError, match="Слишком много"):
        manage_limits.set_global_limit(
            limit=MAX_GLOBAL_LIMIT + 1,
            actor_id=ADMIN,
            settings=stores["settings"],
            audit=stores["audit"],
        )


# --------------------------------------------------------------------- reaching the bot


def test_the_stored_default_beats_the_configured_one(tmp_path) -> None:
    """And takes effect on the next message rather than the next deploy."""
    built = build_services(
        config_dir=Path("config"),
        database_path=tmp_path / "t.db",
        secrets=Secrets(telegram_bot_token="x:y", admin_ids=frozenset({ADMIN})),
    )
    try:
        assert built.chat_policy.per_user_daily_limit == 10

        built.settings.set_ai_daily_limit(30)
        assert built.chat_policy.per_user_daily_limit == 30

        built.settings.set_ai_global_daily_limit(900)
        assert built.chat_policy.global_daily_limit == 900

        built.settings.set_ai_daily_limit(None)
        assert built.chat_policy.per_user_daily_limit == 10
    finally:
        built.engine.dispose()
