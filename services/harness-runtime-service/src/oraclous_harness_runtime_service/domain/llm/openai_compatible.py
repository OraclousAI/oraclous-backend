"""OpenAI-compatible LLM client (ORAA-4 §21 domain layer; ADR-007 ``openai-compatible`` shape).

Speaks the OpenAI Chat Completions protocol (``POST /chat/completions`` with ``tools``) — which
OpenRouter serves for Claude / OpenAI / Gemini / many models behind one key. It owns its own
marshalling: the loop's neutral transcript (``user``/``assistant``+tool_calls/``tool`` messages) →
the wire shape, and the response's ``tool_calls`` (with JSON-string ``arguments``) → ``ToolCall``s.
Holds the BYOM key in memory only for the request; never logs or persists it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)


class LLMClientError(Exception):
    """The upstream LLM call failed (transport or non-2xx). Surfaced as a FAILED run by the loop."""


def _to_wire(messages: list[Message], system: str) -> list[dict[str, Any]]:
    """Convert the loop's neutral transcript to OpenAI chat-completions messages."""
    wire: list[dict[str, Any]] = []
    if system:
        wire.append({"role": "system", "content": system})
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            wire.append(
                {
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc.get("args") or {}),
                            },
                        }
                        for tc in m["tool_calls"]
                    ],
                }
            )
        elif role == "tool":
            wire.append(
                {"role": "tool", "tool_call_id": m.get("tool_call_id"), "content": m.get("content")}
            )
        else:
            wire.append({"role": role, "content": m.get("content") or ""})
    return wire


def _tools_payload(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


class OpenAICompatibleClient:
    protocol_shape = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # OpenRouter attribution headers (ignored by other providers).
                "X-Title": "Oraclous Harness Runtime",
            },
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        body: dict[str, Any] = {"model": self._model, "messages": _to_wire(messages, system)}
        if tools:
            body["tools"] = _tools_payload(tools)
            body["tool_choice"] = "auto"
        resp = await self._client.post("/chat/completions", json=body)
        if resp.status_code // 100 != 2:
            raise LLMClientError(f"LLM call → {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls: list[ToolCall] = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ToolCall(id=raw.get("id") or "call", name=fn.get("name") or "", args=args))
        total_tokens = int((body.get("usage") or {}).get("total_tokens") or 0)
        return LLMResponse(
            text=msg.get("content") or "", tool_calls=calls, total_tokens=total_tokens
        )
