from __future__ import annotations

import json

import httpx
import pytest

from tabelshchik.adapters.openai.client import OpenAiChatModel
from tabelshchik.application.ports import ChatTurn, Role, ToolCall, ToolSpec


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


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {
        "body": {"choices": [{"message": {"content": "готово"}}]},
        "requests": [],
    }

    def answer(request: httpx.Request) -> httpx.Response:
        requests = state["requests"]
        assert isinstance(requests, list)
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=state["body"])

    real = httpx.AsyncClient

    def mocked(**kwargs: object) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(answer), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", mocked)
    return state


SCHEDULE = ToolSpec("schedule", "Расписание офиса.", {"type": "object", "properties": {}})


async def converse(model: OpenAiChatModel, turns, **kwargs):
    return await model.converse(turns, max_tokens=400, temperature=1.0, json_object=True, **kwargs)


def sent_payload(server: dict[str, object]) -> dict[str, object]:
    requests = server["requests"]
    assert isinstance(requests, list)
    payload = requests[-1]
    assert isinstance(payload, dict)
    return payload


async def test_lookups_are_offered_and_the_choice_is_left_to_the_model(server) -> None:
    await converse(
        OpenAiChatModel(api_key="test"),
        [ChatTurn(Role.SYSTEM, "s"), ChatTurn(Role.USER, "u")],
        tools=[SCHEDULE],
    )

    payload = sent_payload(server)
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "schedule",
                "description": "Расписание офиса.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert payload["tool_choice"] == "auto"
    assert payload["response_format"] == {"type": "json_object"}


async def test_the_last_round_keeps_the_tools_but_forbids_calling_them(server) -> None:
    await converse(
        OpenAiChatModel(api_key="test"),
        [ChatTurn(Role.USER, "u")],
        tools=[SCHEDULE],
        allow_tools=False,
    )

    payload = sent_payload(server)
    assert payload["tool_choice"] == "none"
    assert payload["tools"]


async def test_without_tools_nothing_about_them_is_sent(server) -> None:
    await converse(OpenAiChatModel(api_key="test"), [ChatTurn(Role.USER, "u")])

    payload = sent_payload(server)
    assert "tools" not in payload
    assert "tool_choice" not in payload


async def test_a_tool_call_comes_back_as_one_even_with_no_text(server) -> None:
    server["body"] = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "schedule", "arguments": '{"from": "2026-11-09"}'},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 8},
    }

    turn = await converse(
        OpenAiChatModel(api_key="test"), [ChatTurn(Role.USER, "u")], tools=[SCHEDULE]
    )

    assert turn is not None
    assert turn.text == ""
    assert turn.tool_calls == (ToolCall("call-1", "schedule", '{"from": "2026-11-09"}'),)
    assert turn.total_tokens == 128


async def test_the_conversation_goes_back_exactly_as_the_api_expects(server) -> None:
    call = ToolCall("call-1", "schedule", "{}")
    await converse(
        OpenAiChatModel(api_key="test"),
        [
            ChatTurn(Role.SYSTEM, "s"),
            ChatTurn(Role.USER, "u"),
            ChatTurn(Role.ASSISTANT, None, (call,)),
            ChatTurn(Role.TOOL, "пн 10 ноября: Аня", tool_call_id="call-1"),
        ],
        tools=[SCHEDULE],
    )

    messages = sent_payload(server)["messages"]
    assert messages == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "schedule", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "пн 10 ноября: Аня"},
    ]


async def test_an_answer_with_neither_text_nor_calls_is_no_answer(server) -> None:
    server["body"] = {"choices": [{"message": {"content": ""}}]}
    assert await converse(OpenAiChatModel(api_key="test"), [ChatTurn(Role.USER, "u")]) is None
