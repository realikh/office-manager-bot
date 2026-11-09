"""OpenAI chat completions.

Never raises. Every caller has a working fallback — a hand-written line, or a plain
apology in Russian — so a model that is down, rate-limited or out of credit degrades the
bot's voice rather than its function.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from tabelshchik.application.ports import (
    ChatTurn,
    Completion,
    ModelTurn,
    Role,
    ToolCall,
    ToolSpec,
)

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.openai.com/v1/chat/completions"


@dataclass
class OpenAiChatModel:
    api_key: str
    model: str = "gpt-6-luna"
    reasoning_effort: str | None = None
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
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        if json_object:
            # Constrained decoding, so a reply that has to be parsed cannot come back as
            # prose with a fenced block in the middle of it.
            payload["response_format"] = {"type": "json_object"}

        body = await self._post(payload)
        return _completion(body) if body is not None else None

    async def converse(
        self,
        turns: Sequence[ChatTurn],
        *,
        tools: Sequence[ToolSpec] = (),
        allow_tools: bool = True,
        max_tokens: int,
        temperature: float,
        json_object: bool = False,
    ) -> ModelTurn | None:
        if not self.api_key:
            return None

        payload: dict[str, object] = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
            "messages": [_message(turn) for turn in turns],
        }
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                    },
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto" if allow_tools else "none"

        body = await self._post(payload)
        return _model_turn(body) if body is not None else None

    async def _post(self, payload: dict[str, object]) -> dict[str, object] | None:
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

            body = response.json()
            return body if isinstance(body, dict) else None

        return None

    async def _backoff(self, attempt: int) -> None:
        if attempt < self.max_retries:
            await asyncio.sleep(2**attempt)


def _completion(body: dict[str, object]) -> Completion | None:
    message = _first_message(body)
    if message is None:
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None

    prompt_tokens, completion_tokens = _usage(body)
    return Completion(
        text=content.strip(),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _model_turn(body: dict[str, object]) -> ModelTurn | None:
    message = _first_message(body)
    if message is None:
        return None
    content = message.get("content")
    text = content.strip() if isinstance(content, str) else ""
    calls = tuple(_tool_calls(message.get("tool_calls")))
    if not text and not calls:
        return None

    prompt_tokens, completion_tokens = _usage(body)
    return ModelTurn(
        text=text,
        tool_calls=calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _tool_calls(raw: object) -> list[ToolCall]:
    if not isinstance(raw, list):
        return []
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        call_id = item.get("id")
        if not isinstance(function, dict) or not isinstance(call_id, str):
            continue
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str):
            continue
        calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=arguments if isinstance(arguments, str) else "",
            )
        )
    return calls


def _message(turn: ChatTurn) -> dict[str, object]:
    if turn.role is Role.TOOL:
        return {"role": "tool", "tool_call_id": turn.tool_call_id, "content": turn.content or ""}
    if turn.role is Role.ASSISTANT and turn.tool_calls:
        return {
            "role": "assistant",
            "content": turn.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in turn.tool_calls
            ],
        }
    return {"role": str(turn.role), "content": turn.content or ""}


def _first_message(body: dict[str, object]) -> dict[str, object] | None:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    return message if isinstance(message, dict) else None


def _usage(body: dict[str, object]) -> tuple[int, int]:
    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))


def _count(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
