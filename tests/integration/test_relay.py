"""Sending an admin's own message into an office group, as the bot.

What breaks in production when these fail: a post lands in a group the bot has no business
speaking in — a closed office's, or one unbound since the picker was drawn — an album
arrives as loose photos, or a refusal is swallowed and the admin believes the office was
told.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from tabelshchik.adapters.db import models
from tabelshchik.adapters.db.engine import session_scope
from tabelshchik.adapters.db.repositories import (
    SqlAuditLog,
    SqlOfficeAdminStore,
    SqlOfficeStore,
    SqlRosterStore,
)
from tabelshchik.adapters.fakes import RecordingNotifier
from tabelshchik.adapters.telegram.bursts import Bursts
from tabelshchik.application import relay
from tabelshchik.application.relay import Relayed, RelayError

from .conftest import office_seed, seed

ADMIN = 1
OTHER_ADMIN = 2
#: `office_seed`'s group.
GROUP = -100123


@pytest.fixture
def stores(sessions) -> Any:
    seed(sessions)
    return SimpleNamespace(
        sessions=sessions,
        offices=SqlOfficeStore(sessions),
        roster=SqlRosterStore(sessions),
        office_admin=SqlOfficeAdminStore(sessions),
        audit=SqlAuditLog(sessions),
    )


async def deliver(
    stores: Any, notifier: RecordingNotifier, ids: tuple[int, ...] = (5,), office_id: str = "ovest"
) -> Relayed:
    return await relay.deliver(
        office_id=office_id,
        source_chat_id=ADMIN,
        message_ids=ids,
        actor_id=ADMIN,
        offices=stores.offices,
        notifier=notifier,
        audit=stores.audit,
    )


def audited(stores: Any) -> list[tuple[str, dict[str, Any]]]:
    with session_scope(stores.sessions) as session:
        rows = session.scalars(select(models.AuditLog)).all()
        return [(row.action, dict(row.payload)) for row in rows]


# ------------------------------------------------------------------------------ sending


async def test_a_message_goes_to_the_office_group_as_the_bot(stores) -> None:
    notifier = RecordingNotifier()

    outcome = await deliver(stores, notifier)

    assert notifier.copies == [(GROUP, ADMIN, (5,))]
    assert outcome.delivered == outcome.requested == 1
    assert outcome.destination.label == "O'Vest"


async def test_an_album_goes_in_one_call_in_the_order_it_was_written(stores) -> None:
    """One call is what keeps an album grouped; three would post three loose photos.
    Telegram also rejects ids that are not increasing, so the order is not cosmetic."""
    notifier = RecordingNotifier()

    await deliver(stores, notifier, ids=(7, 5, 6, 6))

    assert notifier.copies == [(GROUP, ADMIN, (5, 6, 7))]


async def test_the_send_is_audited_with_what_went_where(stores) -> None:
    await deliver(stores, RecordingNotifier(), ids=(5, 6))

    [(action, payload)] = audited(stores)
    assert action == "message.relay"
    assert payload["chat"] == GROUP
    assert payload["offices"] == ["ovest"]
    assert payload["messages"] == [5, 6]
    assert payload["delivered"] == 2


async def test_parts_telegram_cannot_copy_are_reported_as_partial(stores) -> None:
    """Telegram skips an uncopyable part instead of failing the call, so "sent" alone
    would claim the whole thing arrived."""
    notifier = RecordingNotifier(uncopyable=frozenset({6}))

    outcome = await deliver(stores, notifier, ids=(5, 6, 7))

    assert (outcome.delivered, outcome.requested) == (2, 3)


# ---------------------------------------------------------------------- where it may go


def test_offices_sharing_a_group_are_one_destination(stores) -> None:
    """One post there is read by both offices; two buttons would invite sending it twice."""
    seed(stores.sessions, office_seed(id="pine", name="Pine", chatId=GROUP, employees=[]))

    [target] = relay.destinations(stores.offices)

    assert target.chat_id == GROUP
    assert target.label == "O'Vest · Pine"
    assert relay.destination_of(stores.offices, "pine") == target


async def test_a_closed_office_is_not_a_destination_and_is_refused(stores) -> None:
    """A closed office answers nobody. The picker could have been drawn before it closed."""
    stores.office_admin.set_active("ovest", False)
    notifier = RecordingNotifier()

    assert relay.destinations(stores.offices) == []
    with pytest.raises(RelayError, match="закрыт"):
        await deliver(stores, notifier)
    assert notifier.copies == []
    assert audited(stores) == []


async def test_an_office_without_a_group_is_not_a_destination(stores) -> None:
    stores.roster.set_chat_id("ovest", None)
    notifier = RecordingNotifier()

    assert relay.destinations(stores.offices) == []
    with pytest.raises(RelayError):
        await deliver(stores, notifier)
    assert notifier.copies == []


async def test_an_office_rebound_after_the_picker_was_drawn_gets_it_in_its_new_group(
    stores,
) -> None:
    """The button names an office; the group is looked up when it is pressed."""
    stores.roster.set_chat_id("ovest", -100999)
    notifier = RecordingNotifier()

    await deliver(stores, notifier)

    assert notifier.copies == [(-100999, ADMIN, (5,))]


# ------------------------------------------------------------------------------ refusals


async def test_a_refusal_carries_telegram_s_reason_and_audits_nothing(stores) -> None:
    """The admin is the one who has to go and re-add the bot, so they need to know that."""
    with pytest.raises(RelayError, match="kicked"):
        await deliver(stores, RecordingNotifier(fail=True))
    assert audited(stores) == []


async def test_nothing_telegram_would_copy_is_a_refusal_not_a_success(stores) -> None:
    notifier = RecordingNotifier(uncopyable=frozenset({5}))

    with pytest.raises(RelayError, match="копировать нельзя"):
        await deliver(stores, notifier)
    assert audited(stores) == []


async def test_nothing_to_send_is_refused(stores) -> None:
    notifier = RecordingNotifier()
    with pytest.raises(RelayError):
        await deliver(stores, notifier, ids=())
    assert notifier.copies == []


async def test_more_than_one_call_can_carry_is_refused_before_sending(stores) -> None:
    """Splitting could cut an album in half, so it is refused rather than attempted."""
    notifier = RecordingNotifier()
    with pytest.raises(RelayError, match=str(relay.MAX_MESSAGES)):
        await deliver(stores, notifier, ids=tuple(range(1, relay.MAX_MESSAGES + 2)))
    assert notifier.copies == []


# ------------------------------------------------------------------ gathering the parts
#
# An album is one update per part, and each update runs in its own task. Without the
# gathering, the picker step ran once per part — five pictures, five pickers.


async def test_an_album_is_handed_over_once_and_whole() -> None:
    bursts: Bursts[int] = Bursts(quiet=0.02)

    results = await asyncio.gather(*(bursts.gather(ADMIN, part) for part in (1, 2, 3)))

    assert [batch for batch in results if batch is not None] == [[1, 2, 3]]


async def test_a_part_after_the_chat_went_quiet_starts_its_own_batch() -> None:
    bursts: Bursts[int] = Bursts(quiet=0.01)

    assert await bursts.gather(ADMIN, 1) == [1]
    assert await bursts.gather(ADMIN, 2) == [2]


async def test_two_admins_never_share_a_batch() -> None:
    bursts: Bursts[int] = Bursts(quiet=0.02)

    mine, theirs = await asyncio.gather(bursts.gather(ADMIN, 1), bursts.gather(OTHER_ADMIN, 2))

    assert (mine, theirs) == ([1], [2])


async def test_a_cancelled_wait_does_not_swallow_the_next_message() -> None:
    """A batch nobody is waiting on would absorb every later message into nowhere."""
    bursts: Bursts[int] = Bursts(quiet=0.02)
    waiting = asyncio.create_task(bursts.gather(ADMIN, 1))
    await asyncio.sleep(0)
    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting

    assert await bursts.gather(ADMIN, 2) == [2]
