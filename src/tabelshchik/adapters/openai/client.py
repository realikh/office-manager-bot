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

from tabelshchik.application.ports import Completion

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
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        json_object: bool = False,
    ) -> Completion | None:
        if not self.api_key:
            return None

        payload: dict[str, object] = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_object:
            # Constrained decoding, so a reply that has to be parsed cannot come back as
            # prose with a fenced block in the middle of it.
            payload["response_format"] = {"type": "json_object"}

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

            return _completion(response.json())

        return None

    async def _backoff(self, attempt: int) -> None:
        if attempt < self.max_retries:
            await asyncio.sleep(2**attempt)


def _completion(body: dict[str, object]) -> Completion | None:
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
    if not isinstance(content, str) or not content.strip():
        return None

    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return Completion(
        text=content.strip(),
        prompt_tokens=_count(usage.get("prompt_tokens")),
        completion_tokens=_count(usage.get("completion_tokens")),
    )


def _count(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
