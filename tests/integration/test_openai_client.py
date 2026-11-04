from __future__ import annotations

import json

import httpx
import pytest

from tabelshchik.adapters.openai.client import OpenAiChatModel


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    requests: list[dict[str, object]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "готово"}}]})

    real = httpx.AsyncClient

    def mocked(**kwargs: object) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(answer), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", mocked)
    return requests


async def complete(model: OpenAiChatModel) -> str | None:
    completion = await model.complete("system", "user", max_tokens=400, temperature=1.0)
    return completion.text if completion is not None else None


async def test_reasoning_is_switched_off_so_the_token_budget_goes_to_the_answer(sent) -> None:
    model = OpenAiChatModel(api_key="test", reasoning_effort="none")
    assert await complete(model) == "готово"
    assert sent[0]["reasoning_effort"] == "none"
    assert sent[0]["model"] == "gpt-6-luna"


async def test_a_model_without_reasoning_is_never_sent_the_parameter(sent) -> None:
    await complete(OpenAiChatModel(api_key="test", model="gpt-4.1-mini"))
    assert "reasoning_effort" not in sent[0]
