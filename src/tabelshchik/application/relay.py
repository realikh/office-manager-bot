"""Saying something in an office's group, as the bot, exactly as an admin wrote it.

The admin sends the message to the bot first and picks where it goes afterwards, so what
goes out is always a real Telegram message rather than text rebuilt from one. Copying it
carries photos, documents, albums and formatting across as they were, without the
"forwarded from" line that would name whoever wrote it.

Where it may go is the set of chats the bot already speaks in — each open office's group —
and nothing else. The button names an office, not a chat, and the chat is looked up when
the button is pressed: an office closed or rebound between drawing the picker and pressing
it is noticed rather than posted into.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tabelshchik.application.chats import Destination, destination_of
from tabelshchik.application.ports import AuditLog, Notifier, OfficeStore

#: Telegram's ceiling on one `copyMessages` call. Sending more would mean splitting, and a
#: split can land in the middle of an album.
MAX_MESSAGES = 100

NO_DESTINATION = "У этого офиса больше нет чата, или офис закрыт."


class RelayError(ValueError):
    """The message cannot go where it was pointed. The text is shown to the admin."""


@dataclass(frozen=True, slots=True)
class Relayed:
    destination: Destination
    requested: int
    delivered: int


async def deliver(
    *,
    office_id: str,
    source_chat_id: int,
    message_ids: Sequence[int],
    actor_id: int,
    offices: OfficeStore,
    notifier: Notifier,
    audit: AuditLog,
) -> Relayed:
    """Copy the admin's messages into the group of the office they picked.

    Sorted because Telegram requires increasing ids — and because that is the order they
    were written in, which is the order they should be read in.
    """
    ids = sorted(set(message_ids))
    if not ids:
        raise RelayError("Нечего отправлять — пришлите сообщение заново.")
    if len(ids) > MAX_MESSAGES:
        raise RelayError(f"Не больше {MAX_MESSAGES} сообщений за раз.")

    target = destination_of(offices, office_id)
    if target is None:
        raise RelayError(NO_DESTINATION)

    outcome = await notifier.copy(target.chat_id, from_chat_id=source_chat_id, message_ids=ids)
    if not outcome.message_ids:
        # Nothing went out, so there is nothing to audit: the log records what happened.
        reason = f": {outcome.error}" if outcome.error else " — такие сообщения копировать нельзя."
        raise RelayError(f"Telegram не принял сообщение{reason}")

    audit.record(
        actor_id=actor_id,
        action="message.relay",
        payload={
            "chat": target.chat_id,
            "offices": [office.id for office in target.offices],
            "source": source_chat_id,
            "messages": ids,
            "delivered": len(outcome.message_ids),
        },
    )
    return Relayed(destination=target, requested=len(ids), delivered=len(outcome.message_ids))
