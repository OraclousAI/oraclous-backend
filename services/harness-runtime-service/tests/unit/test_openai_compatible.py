"""OpenAI-compatible client (slice 4): wire marshalling + tool_call parsing (mock transport)."""

from __future__ import annotations

import json

import httpx
import pytest
from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.llm.openai_compatible import (
    LLMClientError,
    OpenAICompatibleClient,
)

pytestmark = pytest.mark.unit

_SPEC = ToolSpec(
    name="pg__list_tables",
    description="list tables",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="pg",
    operation="list_tables",
)


def _client(handler) -> OpenAICompatibleClient:  # noqa: ANN001
    return OpenAICompatibleClient(
        base_url="https://router.test/api/v1",
        api_key="sk-test",
        model="vendor/model",
        transport=httpx.MockTransport(handler),
    )


async def test_emits_tools_and_parses_tool_calls() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-9",
                                    "type": "function",
                                    "function": {
                                        "name": "pg__list_tables",
                                        "arguments": '{"schema": "public"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    resp = await _client(handler).complete(
        messages=[{"role": "user", "content": "go"}], system="be helpful", tools=[_SPEC]
    )
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["model"] == "vendor/model"
    assert captured["body"]["messages"][0] == {"role": "system", "content": "be helpful"}
    assert captured["body"]["tools"][0]["function"]["name"] == "pg__list_tables"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "pg__list_tables"
    assert resp.tool_calls[0].args == {"schema": "public"}


async def test_parses_text_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "the answer"}}]})

    resp = await _client(handler).complete(
        messages=[{"role": "user", "content": "go"}], system="", tools=[]
    )
    assert resp.text == "the answer"
    assert resp.tool_calls == []


async def test_marshals_assistant_tool_calls_and_tool_results() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["msgs"] = json.loads(request.content)["messages"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "pg__list_tables", "args": {}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "pg__list_tables",
            "content": '{"tables": []}',
        },
    ]
    await _client(handler).complete(messages=messages, system="", tools=[_SPEC])
    msgs = captured["msgs"]
    assert msgs[1]["tool_calls"][0]["id"] == "c1"
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "pg__list_tables"
    assert msgs[2] == {"role": "tool", "tool_call_id": "c1", "content": '{"tables": []}'}


async def test_non_2xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    with pytest.raises(LLMClientError):
        await _client(handler).complete(
            messages=[{"role": "user", "content": "x"}], system="", tools=[]
        )
