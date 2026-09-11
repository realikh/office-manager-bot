"""Who the bot will talk to, and about which office.

One function rather than a check repeated in each handler, for the same reason `AdminOnly`
is one middleware: a permission check you have to remember to write is one you will
eventually forget. This is the only place that decides whether a message deserves an
answer at all.

The rule it replaces answered *everybody*. An unknown sender fell through to
`active_offices()[0]`, so any Telegram account that found the bot could ask who was in the
office tomorrow and be told, by name — and could get a fact written into that office's
shared chat memory, which every employee's prompt then carried.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from tabelshchik.application.ports import OfficeStore
from tabelshchik.domain.entities import Employee, Office


class Audience(StrEnum):
    #: Posted in a chat configured as an office's own group. Everyone there already sees
    #: the full tagged roster in the daily reminder, so there is nothing to withhold.
    OFFICE_CHAT = "office-chat"
    #: A linked employee, still in tenure, in a private chat.
    EMPLOYEE = "employee"
    ADMIN = "admin"
    #: Everyone else. Gets no data of any kind.
    STRANGER = "stranger"


@dataclass(frozen=True, slots=True)
class Grant:
    """What this sender is entitled to.

    ``office_id`` is None exactly when nothing may be disclosed, so callers can branch on
    one value rather than re-deriving the rule.
    """

    audience: Audience
    office_id: str | None = None
    employee: Employee | None = None

    @property
    def is_stranger(self) -> bool:
        return self.audience is Audience.STRANGER

    @property
    def may_answer(self) -> bool:
        return self.office_id is not None


def resolve(
    *,
    chat_id: int,
    user_id: int | None,
    is_private: bool,
    offices: OfficeStore,
    admin_ids: frozenset[int],
    today: date,
) -> Grant:
    """Decide the audience, in order of how much the sender has already proved."""
    active = offices.active_offices()

    # Being in an office's group chat is itself the credential: the reminder posts that
    # office's roster there every working day.
    for office in active:
        if office.chat_id == chat_id:
            return Grant(Audience.OFFICE_CHAT, office.id, linked(offices, user_id, today))

    # Outside a known office chat, only a private message can identify anybody. A group
    # the bot was added to by mistake gets nothing, not even a refusal.
    if not is_private or user_id is None:
        return Grant(Audience.STRANGER)

    employee = linked(offices, user_id, today)
    home = _office_of(offices, employee) if employee is not None else None

    if user_id in admin_ids:
        # An admin who is not on any roster still needs an office to ask about.
        return Grant(Audience.ADMIN, home or _first(active), employee)

    if employee is not None and home is not None:
        return Grant(Audience.EMPLOYEE, home, employee)

    return Grant(Audience.STRANGER)


class Claim(StrEnum):
    """What `/start` should do about a sender."""

    #: Already linked and still employed. Nothing to write.
    LINKED = "linked"
    #: A username match that is safe to act on.
    GRANTED = "granted"
    #: A username match onto a record somebody else already holds.
    TAKEN = "taken"
    #: No match, or a match on someone no longer employed.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ClaimResult:
    outcome: Claim
    employee: Employee | None = None


def claim(offices: OfficeStore, *, user_id: int, username: str | None, today: date) -> ClaimResult:
    """Decide whether a Telegram account may take an employee record.

    A username match is a claim, not a proof: Telegram handles are unique only at a point
    in time, so anyone can register one its previous owner released, and the roster is
    full of handles typed by an admin and never revalidated.

    The case that does damage is `TAKEN` — `link_telegram_user` overwrites, so a successful
    claim on a linked record silently unlinks the real person and redirects every reminder
    mention to the claimant.

    `UNKNOWN` deliberately covers both "no such handle" and "that person has left": telling
    them apart would make the bot an oracle for testing usernames against the roster.
    """
    already = linked(offices, user_id, today)
    if already is not None:
        return ClaimResult(Claim.LINKED, already)

    matched = offices.find_employee_by_username(username) if username else None
    if matched is None:
        return ClaimResult(Claim.UNKNOWN)
    if matched.telegram_user_id not in (None, user_id):
        return ClaimResult(Claim.TAKEN, matched)
    if not matched.in_tenure(today):
        return ClaimResult(Claim.UNKNOWN)
    return ClaimResult(Claim.GRANTED, matched)


def linked(offices: OfficeStore, user_id: int | None, today: date) -> Employee | None:
    """The employee behind a Telegram account, if they still work here.

    Tenure is checked here rather than by clearing the link on termination: the column is
    what lets a reminder tag somebody, `restore` should just work, and history should stay
    intact. Without this check a person who left last month keeps `/me`, `/vacation` and
    the whole office's week, indefinitely.
    """
    if user_id is None:
        return None
    found = offices.find_employee_by_user_id(user_id)
    return found if found is not None and found.in_tenure(today) else None


def _office_of(offices: OfficeStore, employee: Employee) -> str | None:
    """Office membership lives on the database row, not on the entity, so this asks."""
    for office in offices.active_offices():
        if any(person.id == employee.id for person in offices.employees(office.id)):
            return office.id
    return None


def _first(active: Sequence[Office]) -> str | None:
    return active[0].id if active else None
