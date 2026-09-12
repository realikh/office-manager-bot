"""Granting adminship, taking it back, and handing the bot over.

Adminship used to be `ADMIN_IDS` in the environment, resolved once at boot. There was no
way to give it to somebody else and no way to stop being an admin, which made "I am
leaving, here are the keys" an SSH session rather than a conversation.

Three rules shape everything here.

*There is always exactly one owner.* The database enforces it with a partial unique
index; these functions enforce it in a way that produces a sentence an admin can read
rather than an IntegrityError.

*The owner cannot be revoked, only transferred.* That is what keeps the table from ever
emptying through the UI, and it is why handing the bot over is two deliberate steps:
grant, then transfer. One tap should not be able to give the bot to a number nobody has
checked.

*An admin may always remove themselves.* Otherwise the transfer above is a trap: after
handing over ownership the ex-owner is an ordinary admin, and an owner-only revoke would
leave them unable to finish leaving.
"""

from __future__ import annotations

from dataclasses import dataclass

from tabelshchik.application.ports import (
    AdminRole,
    AdminStore,
    AuditLog,
    Clock,
)


class AdminError(ValueError):
    """The requested change makes no sense. The message is shown to the admin."""


@dataclass(frozen=True, slots=True)
class AdminChange:
    user_id: int
    role: AdminRole
    label: str = ""
    #: Set by `transfer_ownership`: who stopped being the owner.
    demoted: int | None = None


def grant(
    *,
    user_id: int,
    label: str = "",
    employee_id: str | None = None,
    actor_id: int,
    admins: AdminStore,
    clock: Clock,
    audit: AuditLog | None = None,
) -> AdminChange:
    """Make somebody an admin. Owner only."""
    _require_owner(admins, actor_id)

    if user_id <= 0:
        # A negative id is a chat, not a person; making one an "admin" would grant the
        # whole group whatever that id happens to collide with.
        raise AdminError("Это не похоже на id пользователя.")

    if not admins.grant(
        user_id,
        label=label.strip()[:128],
        employee_id=employee_id,
        granted_by=actor_id,
        at=clock.now(),
    ):
        raise AdminError("Уже администратор.")

    _audit(audit, actor_id, "admin.grant", {"user": user_id})
    return AdminChange(user_id=user_id, role=AdminRole.ADMIN, label=label)


def revoke(
    *,
    user_id: int,
    actor_id: int,
    admins: AdminStore,
    audit: AuditLog | None = None,
) -> AdminChange:
    """Take adminship away. The owner, or anybody removing themselves."""
    if actor_id != user_id:
        _require_owner(admins, actor_id)

    role = admins.role_of(user_id)
    if role is None:
        raise AdminError("Этот пользователь не администратор.")
    if role is AdminRole.OWNER:
        # The one rule that keeps the table from emptying: with no owner there is nobody
        # who can grant adminship back, and the only way in is editing the database.
        raise AdminError("Владельца нельзя разжаловать — сначала передайте права.")

    admins.revoke(user_id)
    _audit(audit, actor_id, "admin.revoke", {"user": user_id})
    return AdminChange(user_id=user_id, role=role)


def transfer_ownership(
    *,
    user_id: int,
    actor_id: int,
    admins: AdminStore,
    clock: Clock,
    audit: AuditLog | None = None,
) -> AdminChange:
    """Hand the bot over. Owner only, and only to somebody already an admin."""
    _require_owner(admins, actor_id)

    if user_id == actor_id:
        raise AdminError("Вы уже владелец.")
    if admins.role_of(user_id) is None:
        # Deliberately two steps. Ownership is everything this bot can do, and a single
        # tap should not be able to hand it to a number nobody has verified.
        raise AdminError("Сначала выдайте права администратора.")

    demoted = admins.transfer_ownership(to_user_id=user_id, at=clock.now())
    _audit(audit, actor_id, "admin.transfer", {"to": user_id, "from": demoted})
    return AdminChange(user_id=user_id, role=AdminRole.OWNER, demoted=demoted)


# ------------------------------------------------------------------------------ helpers


def _require_owner(admins: AdminStore, actor_id: int) -> None:
    """The authoritative check.

    The screens hide these buttons from anyone but the owner, but a hidden button is not
    a permission: a demoted admin still has the old screen open on their phone, and the
    free-text steps carry no callback data to inspect at all.
    """
    if admins.role_of(actor_id) is not AdminRole.OWNER:
        raise AdminError("Это может только владелец.")


def _audit(
    audit: AuditLog | None, actor_id: int | None, action: str, payload: dict[str, object]
) -> None:
    if audit is not None:
        audit.record(actor_id=actor_id, action=action, payload=payload)
