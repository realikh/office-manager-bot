"""Granting adminship, taking it back, and handing the bot over.

The rules these pin are the ones that make "I am leaving, here are the keys" possible
without an SSH session — and the ones that stop the bot ending up with no owner at all,
a state nothing in the UI can recover from.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.engine import session_scope
from tabelshchik.adapters.db.repositories import SqlAdminStore, SqlAuditLog
from tabelshchik.adapters.db.seed import seed_admins
from tabelshchik.application import manage_admins
from tabelshchik.application.manage_admins import AdminError
from tabelshchik.application.ports import AdminRole

NOW = datetime(2026, 9, 11, 12, 0)
OWNER = 1
SECOND = 2
STRANGER = 999


@pytest.fixture
def admins(sessions):
    store = SqlAdminStore(sessions)
    with session_scope(sessions) as session:
        seed_admins(session, frozenset({OWNER}), now=NOW)
    return store


@pytest.fixture
def stores(sessions, admins):
    return {"admins": admins, "clock": FixedClock(NOW), "audit": SqlAuditLog(sessions)}


# ------------------------------------------------------------------------------- grant


def test_the_owner_can_make_somebody_an_admin(stores) -> None:
    change = manage_admins.grant(user_id=SECOND, label="Дина", actor_id=OWNER, **stores)
    assert change.role is AdminRole.ADMIN
    assert stores["admins"].role_of(SECOND) is AdminRole.ADMIN


def test_an_ordinary_admin_cannot_grant_adminship(stores) -> None:
    """Otherwise "primary admin" is decorative: anybody you promote could promote anybody
    else, or remove you."""
    manage_admins.grant(user_id=SECOND, actor_id=OWNER, **stores)
    with pytest.raises(AdminError, match="владелец"):
        manage_admins.grant(user_id=3, actor_id=SECOND, **stores)


def test_a_stranger_cannot_grant_adminship(stores) -> None:
    with pytest.raises(AdminError, match="владелец"):
        manage_admins.grant(user_id=3, actor_id=STRANGER, **stores)


def test_granting_twice_says_so_instead_of_failing_silently(stores) -> None:
    manage_admins.grant(user_id=SECOND, actor_id=OWNER, **stores)
    with pytest.raises(AdminError, match="Уже администратор"):
        manage_admins.grant(user_id=SECOND, actor_id=OWNER, **stores)


def test_a_chat_id_is_refused_where_a_user_id_belongs(stores) -> None:
    """A negative id is a group. Granting one adminship would hand it to whoever that id
    collides with, which is nobody in particular and possibly everybody."""
    with pytest.raises(AdminError, match="не похоже"):
        manage_admins.grant(user_id=-100123, actor_id=OWNER, **stores)


def test_a_grant_is_audited(sessions, stores) -> None:
    manage_admins.grant(user_id=SECOND, actor_id=OWNER, **stores)
    assert any(entry.action == "admin.grant" for entry in _audit_entries(sessions))


# ------------------------------------------------------------------------------ revoke


def test_the_owner_cannot_be_revoked(stores) -> None:
    """With no owner there is nobody who can grant adminship back, and the only way in is
    editing the database by hand. This is what keeps the table from emptying."""
    with pytest.raises(AdminError, match="Владельца нельзя"):
        manage_admins.revoke(
            user_id=OWNER, actor_id=OWNER, admins=stores["admins"], audit=stores["audit"]
        )


def test_an_admin_may_always_remove_themselves(stores) -> None:
    """The other half of transferring ownership. Without this, handing the bot over is a
    trap: the ex-owner is an ordinary admin and an owner-only revoke would strand them."""
    manage_admins.grant(user_id=SECOND, actor_id=OWNER, **stores)
    manage_admins.revoke(
        user_id=SECOND, actor_id=SECOND, admins=stores["admins"], audit=stores["audit"]
    )
    assert stores["admins"].role_of(SECOND) is None


def test_an_admin_cannot_remove_another_admin(stores) -> None:
    manage_admins.grant(user_id=SECOND, actor_id=OWNER, **stores)
    manage_admins.grant(user_id=3, actor_id=OWNER, **stores)
    with pytest.raises(AdminError, match="владелец"):
        manage_admins.revoke(
            user_id=3, actor_id=SECOND, admins=stores["admins"], audit=stores["audit"]
        )


def test_revoking_somebody_who_is_not_an_admin_says_so(stores) -> None:
    with pytest.raises(AdminError, match="не администратор"):
        manage_admins.revoke(
            user_id=STRANGER, actor_id=OWNER, admins=stores["admins"], audit=stores["audit"]
        )


# ---------------------------------------------------------------------------- transfer


def test_handing_the_bot_over_and_then_leaving(stores) -> None:
    """The feature, end to end."""
    admins = stores["admins"]
    manage_admins.grant(user_id=SECOND, label="Дина", actor_id=OWNER, **stores)

    change = manage_admins.transfer_ownership(user_id=SECOND, actor_id=OWNER, **stores)
    assert change.demoted == OWNER
    assert admins.owner_id() == SECOND

    manage_admins.revoke(user_id=OWNER, actor_id=OWNER, admins=admins, audit=stores["audit"])
    assert admins.role_of(OWNER) is None
    assert admins.owner_id() == SECOND


def test_ownership_only_passes_to_an_existing_admin(stores) -> None:
    """Two deliberate steps. Ownership is everything this bot can do, and one tap should
    not hand it to a number nobody has checked."""
    with pytest.raises(AdminError, match="Сначала выдайте"):
        manage_admins.transfer_ownership(user_id=STRANGER, actor_id=OWNER, **stores)


def test_only_the_owner_may_transfer_ownership(stores) -> None:
    manage_admins.grant(user_id=SECOND, actor_id=OWNER, **stores)
    manage_admins.grant(user_id=3, actor_id=OWNER, **stores)
    with pytest.raises(AdminError, match="владелец"):
        manage_admins.transfer_ownership(user_id=3, actor_id=SECOND, **stores)


def test_transferring_to_yourself_says_so(stores) -> None:
    with pytest.raises(AdminError, match="уже владелец"):
        manage_admins.transfer_ownership(user_id=OWNER, actor_id=OWNER, **stores)


def test_there_is_still_exactly_one_owner_afterwards(stores) -> None:
    manage_admins.grant(user_id=SECOND, actor_id=OWNER, **stores)
    manage_admins.transfer_ownership(user_id=SECOND, actor_id=OWNER, **stores)

    owners = [record for record in stores["admins"].listing() if record.is_owner]
    assert [record.user_id for record in owners] == [SECOND]


def _audit_entries(sessions):
    from sqlalchemy import select

    from tabelshchik.adapters.db import models

    with session_scope(sessions) as session:
        return list(session.scalars(select(models.AuditLog)).all())
