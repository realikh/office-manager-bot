"""Test doubles that ship with the code.

They live in ``src`` rather than in ``tests`` so that the dry-run and preview paths of
the real CLI can use them — "show me what you would send" is then exercising the same
code as a send, with only the last step swapped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from tabelshchik.application.ports import Completion, CopiedMessages, SentMessage


@dataclass
class RecordingNotifier:
    """Records what would have been sent instead of sending it."""

    messages: list[tuple[int, str, bool]] = field(default_factory=list)
    #: (chat_id, filename, byte count, caption, silent), in call order. `silent` is
    #: recorded because a document nobody asked for — the nightly backup — must not
    #: make a noise, and a dropped flag is invisible until somebody's phone rings.
    documents: list[tuple[int, str, int, str, bool]] = field(default_factory=list)
    #: ("pin" | "unpin", chat_id, message_id), in call order.
    pin_calls: list[tuple[str, int, int]] = field(default_factory=list)
    #: (chat_id, from_chat_id, message ids actually copied), in call order.
    copies: list[tuple[int, int, tuple[int, ...]]] = field(default_factory=list)
    #: Ids Telegram would skip as uncopyable — a service message, an invoice — which it
    #: does silently rather than failing the call.
    uncopyable: frozenset[int] = frozenset()
    fail: bool = False
    fail_pins: bool = False
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
        self.documents.append((chat_id, filename, len(content), caption, silent))
        self._next_id += 1
        return SentMessage(chat_id=chat_id, message_id=self._next_id)

    async def pin(self, chat_id: int, message_id: int, *, silent: bool = False) -> bool:
        self.pin_calls.append(("pin", chat_id, message_id))
        return not self.fail_pins

    async def unpin(self, chat_id: int, message_id: int) -> bool:
        self.pin_calls.append(("unpin", chat_id, message_id))
        return not self.fail_pins

    async def copy(
        self,
        chat_id: int,
        *,
        from_chat_id: int,
        message_ids: Sequence[int],
        silent: bool = False,
    ) -> CopiedMessages:
        if self.fail:
            return CopiedMessages(
                chat_id=chat_id, error="Forbidden: bot was kicked from the supergroup chat"
            )
        kept = tuple(item for item in message_ids if item not in self.uncopyable)
        self.copies.append((chat_id, from_chat_id, kept))
        created = []
        for _ in kept:
            self._next_id += 1
            created.append(self._next_id)
        return CopiedMessages(chat_id=chat_id, message_ids=tuple(created))

    @property
    def last_message_id(self) -> int:
        return self._next_id

    @property
    def last_text(self) -> str:
        return self.messages[-1][1] if self.messages else ""


@dataclass
class StubChatModel:
    """A model that always says the same thing, or nothing at all.

    ``reply`` is returned verbatim, including when JSON was asked for — so a test can
    hand back prose where the caller wanted an object and check that the fallback holds.
    """

    reply: str | None = None
    prompts: list[tuple[str, str]] = field(default_factory=list)
    #: Whether each call asked for a JSON object, in call order.
    json_requested: list[bool] = field(default_factory=list)
    #: What the API would have reported spending. Zero unless a test cares.
    prompt_tokens: int = 0
    completion_tokens: int = 0

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        json_object: bool = False,
    ) -> Completion | None:
        self.prompts.append((system, user))
        self.json_requested.append(json_object)
        if self.reply is None:
            return None
        return Completion(
            text=self.reply,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )
