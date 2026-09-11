"""OpenAI chat completions.

Never raises. Every caller has a working fallback — a hand-written line, or a plain
apology in Russian — so a model that is down, rate-limited or out of credit degrades the
bot's voice rather than its function.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.openai.com/v1/chat/completions"


@dataclass
class OpenAiChatModel:
    api_key: str
    model: str = "gpt-4.1-nano"
    timeout: float = 20.0
    max_retries: int = 2
    endpoint: str = ENDPOINT

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> str | None:
        if not self.api_key:
            return None

        payload = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        self.endpoint,
                        json=payload,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
            except httpx.HTTPError:
                logger.warning("openai request failed (attempt %s)", attempt + 1)
                await self._backoff(attempt)
                continue

            # 429 and 5xx are worth another go; a 4xx means the request itself is wrong
            # and retrying would only waste time and money.
            if response.status_code == 429 or response.status_code >= 500:
                logger.warning("openai returned %s", response.status_code)
                await self._backoff(attempt)
                continue

            if response.status_code >= 400:
                logger.error("openai rejected the request: %s", response.text[:500])
                return None

            return _first_message(response.json())

        return None

    async def _backoff(self, attempt: int) -> None:
        if attempt < self.max_retries:
            await asyncio.sleep(2**attempt)


def _first_message(body: dict[str, object]) -> str | None:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None
