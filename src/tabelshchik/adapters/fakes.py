"""Test doubles that ship with the code.

They live in ``src`` rather than in ``tests`` so that the dry-run and preview paths of
the real CLI can use them — "show me what you would send" is then exercising the same
code as a send, with only the last step swapped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tabelshchik.application.ports import SentMessage


@dataclass
class RecordingNotifier:
    """Records what would have been sent instead of sending it."""

    messages: list[tuple[int, str, bool]] = field(default_factory=list)
    documents: list[tuple[int, str, int, str]] = field(default_factory=list)
    fail: bool = False
    _next_id: int = 1000

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        silent: bool = False,
        pin: bool = False,
        pin_kind: str | None = None,
    ) -> SentMessage | None:
        if self.fail:
            return None
        self.messages.append((chat_id, text, silent))
        self._next_id += 1
        return SentMessage(chat_id=chat_id, message_id=self._next_id)

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        *,
        caption: str = "",
        silent: bool = False,
    ) -> SentMessage | None:
        if self.fail:
            return None
        self.documents.append((chat_id, filename, len(content), caption))
        self._next_id += 1
        return SentMessage(chat_id=chat_id, message_id=self._next_id)

    @property
    def last_text(self) -> str:
        return self.messages[-1][1] if self.messages else ""


@dataclass
class StubChatModel:
    """A model that always says the same thing, or nothing at all."""

    reply: str | None = None
    prompts: list[tuple[str, str]] = field(default_factory=list)

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> str | None:
        self.prompts.append((system, user))
        return self.reply
