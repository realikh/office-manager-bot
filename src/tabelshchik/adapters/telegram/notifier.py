"""Outbound Telegram.

Everything the bot says to a chat goes through here. Sends never raise: a failed send
returns None so the caller can decide, and the callers all do — an attendance reminder
that could not be delivered must not mark the day as announced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, LinkPreviewOptions

from tabelshchik.application.ports import SentMessage

logger = logging.getLogger(__name__)

#: Telegram's hard limits. Longer content is split rather than lost.
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024


@dataclass
class TelegramNotifier:
    """Sends messages, and pins them.

    `send(pin=True)` replaces a pin by kind using a dict that lives only as long as the
    process; the attendance reminder is the one caller. The Tempo reminder pins through
    `pin`/`unpin` instead and keeps its record in the database, because a pin nobody can
    find after a redeploy is a pin that stays up forever.
    """

    bot: Bot
    #: (chat_id, kind) -> message_id currently pinned for that kind.
    pins: dict[tuple[int, str], int] = field(default_factory=dict)

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        silent: bool = False,
        pin: bool = False,
        pin_kind: str | None = None,
    ) -> SentMessage | None:
        chunks = split_message(text)
        sent: SentMessage | None = None

        for chunk in chunks:
            try:
                message = await self.bot.send_message(
                    chat_id,
                    chunk,
                    parse_mode="HTML",
                    disable_notification=silent,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramAPIError:
                logger.exception("failed to send to chat %s", chat_id)
                return None
            sent = SentMessage(chat_id=chat_id, message_id=message.message_id)

        if sent is not None and pin:
            await self._replace_pin(sent, kind=pin_kind or "default", silent=silent)
        return sent

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        *,
        caption: str = "",
        silent: bool = False,
    ) -> SentMessage | None:
        try:
            message = await self.bot.send_document(
                chat_id,
                BufferedInputFile(content, filename=filename),
                caption=caption[:MAX_CAPTION_LENGTH],
                parse_mode="HTML",
                disable_notification=silent,
            )
        except TelegramAPIError:
            logger.exception("failed to send document to chat %s", chat_id)
            return None
        return SentMessage(chat_id=chat_id, message_id=message.message_id)

    async def pin(self, chat_id: int, message_id: int, *, silent: bool = False) -> bool:
        """Pin one message. Not silent is the point: the pin is what notifies everyone."""
        try:
            await self.bot.pin_chat_message(chat_id, message_id, disable_notification=silent)
        except TelegramAPIError:
            logger.exception("failed to pin message %s in chat %s", message_id, chat_id)
            return False
        return True

    async def unpin(self, chat_id: int, message_id: int) -> bool:
        try:
            await self.bot.unpin_chat_message(chat_id, message_id=message_id)
        except TelegramAPIError:
            # Already unpinned by hand, or the message is gone. Either way there is
            # nothing left to take down.
            logger.warning("could not unpin %s in chat %s", message_id, chat_id)
            return False
        return True

    async def _replace_pin(self, message: SentMessage, *, kind: str, silent: bool) -> None:
        """Pin the new message before unpinning the old one.

        In that order deliberately: if the unpin fails the chat has a redundant pin,
        which is untidy. The other order risks a chat with no pin at all.
        """
        try:
            await self.bot.pin_chat_message(
                message.chat_id, message.message_id, disable_notification=silent
            )
        except TelegramAPIError:
            logger.exception("failed to pin message in chat %s", message.chat_id)
            return

        previous = self.pins.get((message.chat_id, kind))
        self.pins[(message.chat_id, kind)] = message.message_id

        if previous is None or previous == message.message_id:
            return
        try:
            await self.bot.unpin_chat_message(message.chat_id, message_id=previous)
        except TelegramAPIError:
            # Keep the new pin recorded regardless; a stale pin is a cosmetic problem.
            logger.warning("could not unpin %s in chat %s", previous, message.chat_id)


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split on line boundaries so a tagged roster is never cut mid-name."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # A single line longer than the limit is rare and is cut hard.
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line

    if current:
        chunks.append(current)
    return chunks
