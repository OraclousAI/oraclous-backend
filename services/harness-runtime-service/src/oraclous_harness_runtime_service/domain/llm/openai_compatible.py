"""OpenAI-compatible LLM client (domain layer; ADR-007 ``openai-compatible`` shape).

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

# Which non-2xx statuses are TRANSIENT — a retry (with backoff) may succeed: 408 request-timeout,
# 409 conflict, 429 rate-limit (the shared BYOM key throttle — #543's random-member killer), and ANY
# 5xx (500–599). The 5xx range deliberately spans the Cloudflare statuses 520–527 too: OpenRouter
# sits behind Cloudflare, which returns 52x on an origin hiccup (transient) — a fixed {500,502,503,
# 504,529} set would fail those fast and defeat the retry for the real transient class. Everything
# else (400/401/403/404/422 — bad request, auth, KEY-LIMIT 403, model-not-found) is PERMANENT and
# fails fast (ADR-042 #551).
_TRANSIENT_4XX = frozenset({408, 409, 429})


def _status_is_transient(status_code: int) -> bool:
    """True when a non-2xx LLM-call status is worth a bounded retry (transient), not fail-fast."""
    return status_code in _TRANSIENT_4XX or 500 <= status_code <= 599


class LLMClientError(Exception):
    """The upstream LLM call failed (transport or non-2xx). Surfaced as a FAILED run by the loop.

    ``transient`` marks a failure a bounded retry may recover (rate-limit / timeout / 5xx /
    overloaded); the tool-use loop retries those with backoff+jitter before declaring FAILED, and
    fails a permanent error (auth / model-not-found / bad-request) fast (ADR-042 #551)."""

    def __init__(
        self, message: str, *, status_code: int | None = None, transient: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


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
        try:
            resp = await self._client.post("/chat/completions", json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # a network timeout / transport error is transient — the loop retries it (ADR-042 #551)
            raise LLMClientError(
                f"LLM call transport error: {type(exc).__name__}", transient=True
            ) from exc
        if resp.status_code // 100 != 2:
            sc = resp.status_code
            # leak-safe: the provider's response BODY may carry the customer's prompt/output echoed
            # back, and this message flows into the harness LoopResult.error_message → the team-run
            # error_message (persisted + served via GET). Surface only the coarse status, never the
            # body (CLAUDE.md §11 — no customer data in error messages/logs; ADR-042 broadened the
            # reach of this string). The body, if needed, belongs in a debug log, never a surfaced
            # error.
            raise LLMClientError(
                f"LLM call → {sc}",
                status_code=sc,
                transient=_status_is_transient(sc),
            )
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls: list[ToolCall] = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ToolCall(id=raw.get("id") or "call", name=fn.get("name") or "", args=args))
        usage = data.get("usage") or {}

        def _usage(key: str) -> int:
            try:  # a provider sending a non-numeric usage field → don't fail the run, count 0
                return int(usage.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        # The provider's usage block carries prompt_tokens + completion_tokens (the input/output
        # split); keep total_tokens. A provider that omits the split leaves input/output 0.
        total_tokens = _usage("total_tokens")
        input_tokens = _usage("prompt_tokens")
        output_tokens = _usage("completion_tokens")
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
