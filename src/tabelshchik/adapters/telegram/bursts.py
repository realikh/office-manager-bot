"""Messages that arrive together, handled together.

An album is not one update but one per picture, and forwarding several messages at once is
one update per message. Polling hands every update to its own task, so a flow step written
for "the message" runs once per part, all at the same time — five pictures would draw five
screens, each believing it had the whole message.

`Bursts` holds the first part back until the chat has gone quiet and then hands over the
whole batch. The other parts join that batch and are done. It is a helper a handler calls
rather than a middleware, so it applies to exactly the steps that ask for it.
"""

from __future__ import annotations

import asyncio


class Bursts[T]:
    def __init__(self, *, quiet: float) -> None:
        #: Telegram delivers an album's parts within milliseconds of each other; the wait is
        #: a sliding one, so a long multi-forward keeps it open for as long as it lasts.
        self._quiet = quiet
        self._open: dict[int, list[T]] = {}

    async def gather(self, key: int, item: T) -> list[T] | None:
        """The whole batch to the first caller once `key` goes quiet; None to the rest."""
        batch = self._open.get(key)
        if batch is not None:
            batch.append(item)
            return None

        batch = self._open[key] = [item]
        try:
            while True:
                seen = len(batch)
                await asyncio.sleep(self._quiet)
                if len(batch) == seen:
                    break
        finally:
            # Also on cancellation: a batch nobody is waiting on would quietly swallow
            # every message sent to this chat afterwards.
            self._open.pop(key, None)
        return batch
