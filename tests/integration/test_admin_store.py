"""The admin table: who may use the admin surface, and the single-owner rule.

Adminship used to be `ADMIN_IDS` in the environment, read once at boot. Everything here
exists so that handing the bot to somebody else — and then leaving — is something an
owner can do from their phone, without ever passing through a state with two owners or
none.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from tabelshchik.adapters.db import models
from tabelshchik.adapters.db.engine import session_scope
from tabelshchik.adapters.db.repositories import SqlAdminStore
from tabelshchik.adapters.db.seed import seed_admins
from tabelshchik.application.ports import AdminRole

NOW = datetime(2026, 9, 11, 12, 0)
LATER = datetime(2026, 9, 12, 12, 0)


@pytest.fixture
def admins(sessions):
    return SqlAdminStore(sessions)


def bootstrap(sessions, *user_ids: int) -> tuple[int, ...]:
    with session_scope(sessions) as session:
        return seed_admins(session, frozenset(user_ids), now=NOW)


# ------------------------------------------------------------------------ the bootstrap


def test_the_environment_seeds_an_empty_table(sessions, admins) -> None:
    bootstrap(sessions, 7, 3, 9)
    assert admins.ids() == frozenset({3, 7, 9})


def test_the_lowest_id_becomes_the_owner(sessions, admins) -> None:
    """Arbitrary, but deterministic. A frozenset has no order, and an owner that changed
    on every boot would be a genuinely baffling thing to debug."""
    bootstrap(sessions, 7, 3, 9)
    assert admins.owner_id() == 3
    assert admins.role_of(7) is AdminRole.ADMIN


def test_a_second_boot_does_not_re_seed(sessions, admins) -> None:
    """The whole point. An environment variable that kept re-granting adminship every
    boot would make leaving impossible."""
    bootstrap(sessions, 3)
    admins.grant(50, label="Новый", at=LATER)
    assert admins.revoke(3) is True

    assert bootstrap(sessions, 3) == ()
    assert admins.ids() == frozenset({50})


def test_an_emptied_table_is_re_seeded(sessions, admins) -> None:
    """The way back in. Nothing in the UI can reach this state, because the owner cannot
    be revoked — but a hand-edited database can, and should not brick the bot."""
    bootstrap(sessions, 3)
    admins.revoke(3)
    assert bootstrap(sessions, 3) == (3,)
    assert admins.owner_id() == 3


def test_an_empty_environment_seeds_nothing(sessions, admins) -> None:
    assert bootstrap(sessions) == ()
    assert admins.count() == 0


# ----------------------------------------------------------------------- the owner rule


def test_there_can_only_ever_be_one_owner(sessions) -> None:
    """Enforced by the partial index, not only by the use case.

    If this fails the index is gone — most likely because a batch migration rebuilt the
    table without re-creating the predicate, which SQLite does silently.
    """
    bootstrap(sessions, 3)
    with pytest.raises(IntegrityError), session_scope(sessions) as session:
        session.add(models.Admin(telegram_user_id=99, role=AdminRole.OWNER, granted_at=LATER))


def test_a_transfer_demotes_before_it_promotes(sessions, admins) -> None:
    """Promoting first trips the index mid-statement, which surfaces as an IntegrityError
    out of a session scope — illegible to whoever pressed the button."""
    bootstrap(sessions, 3)
    admins.grant(50, label="Новый", at=LATER)

    assert admins.transfer_ownership(to_user_id=50, at=LATER) == 3
    assert admins.owner_id() == 50
    assert admins.role_of(3) is AdminRole.ADMIN


def test_an_ex_owner_can_then_remove_themselves(sessions, admins) -> None:
    """The feature, end to end: hand it over, then stop being an admin at all."""
    bootstrap(sessions, 3)
    admins.grant(50, at=LATER)
    admins.transfer_ownership(to_user_id=50, at=LATER)

    assert admins.revoke(3) is True
    assert admins.role_of(3) is None
    assert admins.owner_id() == 50


def test_transferring_to_a_stranger_changes_nothing(sessions, admins) -> None:
    """Ownership passes to an existing admin. Granting first is a separate, deliberate
    step, so the bot cannot be handed to an unverified number in one tap."""
    bootstrap(sessions, 3)
    assert admins.transfer_ownership(to_user_id=999, at=LATER) is None
    assert admins.owner_id() == 3


def test_transferring_to_the_current_owner_changes_nothing(sessions, admins) -> None:
    bootstrap(sessions, 3)
    assert admins.transfer_ownership(to_user_id=3, at=LATER) is None
    assert admins.owner_id() == 3


# ---------------------------------------------------------------------------- the rest


def test_granting_somebody_who_is_already_an_admin_reports_it(sessions, admins) -> None:
    bootstrap(sessions, 3)
    assert admins.grant(50, at=LATER) is True
    assert admins.grant(50, at=LATER) is False


def test_revoking_somebody_who_is_not_an_admin_reports_it(admins) -> None:
    assert admins.revoke(404) is False


def test_the_listing_puts_the_owner_first(sessions, admins) -> None:
    bootstrap(sessions, 3)
    admins.grant(50, label="Новый", at=LATER)
    listing = admins.listing()

    assert [record.user_id for record in listing] == [3, 50]
    assert listing[0].is_owner
    assert listing[1].label == "Новый"
