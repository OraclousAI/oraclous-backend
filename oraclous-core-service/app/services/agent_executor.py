"""Agent execution engine — native tool-use loop backed by the capability registry.

ORAA-76: _tool_use_loop now calls tool_schemas_from_registry to build provider
schemas from OHM descriptors fetched from the capability registry, replacing
the previous static _TOOL_SCHEMAS lookup.

Two SDK paths are supported:
- OpenAI-compatible (AsyncOpenAI / OpenRouter / Azure): llm.chat.completions.create
- Anthropic native (AsyncAnthropic): llm.messages.create

graph_id is stripped from LLM-visible schemas by tool_schemas_from_registry;
the toolkit injects it at dispatch via prov.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.services.agent_tool_schemas import ProviderFormat, tool_schemas_from_registry

_MAX_TOOL_ITERATIONS = 5


@dataclass(frozen=True, slots=True)
class _ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _LLMResponse:
    text: str
    tool_calls: list[_ToolCall] = field(default_factory=list)
    raw_assistant_payload: Any = None


def _is_anthropic_client(llm: Any) -> bool:
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return False
    return isinstance(llm, AsyncAnthropic)


class AgentExecutor:
    """Runs a single agent's tool-use loop for one chat turn."""

    def __init__(
        self,
        agent_def: dict[str, Any],
        toolkit: Any,
        llm: Any,
        model: str,
        *,
        driver: Any = None,
        requesting_user_id: str | None = None,
        registry_client: Any = None,
    ) -> None:
        self._agent = agent_def
        self._toolkit = toolkit
        self._llm = llm
        self._model = model
        self._driver = driver
        self._requesting_user_id = requesting_user_id
        self._registry_client = registry_client

    # ── LLM format helpers ────────────────────────────────────────────────────

    def _provider_format(self) -> ProviderFormat:
        return "anthropic" if _is_anthropic_client(self._llm) else "openai"

    async def _call_llm(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
    ) -> _LLMResponse:
        sp = system_prompt or self._agent.get("system_prompt", "You are a helpful assistant.")
        if _is_anthropic_client(self._llm):
            return await self._call_anthropic(messages, sp, tools)
        return await self._call_openai_compatible(messages, sp, tools)

    async def _call_openai_compatible(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict] | None,
    ) -> _LLMResponse:
        all_msgs = [{"role": "system", "content": system_prompt}] + messages
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": all_msgs,
            "temperature": 0.7,
        }
        if tools:
            kwargs["tools"] = tools
        resp = await self._llm.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        text = msg.content or ""
        tool_calls: list[_ToolCall] = []
        for raw_tc in getattr(msg, "tool_calls", None) or []:
            fn = getattr(raw_tc, "function", None)
            fn_name = getattr(fn, "name", "") if fn else ""
            fn_args_raw = getattr(fn, "arguments", "") if fn else ""
            try:
                fn_args = json.loads(fn_args_raw) if fn_args_raw else {}
            except (json.JSONDecodeError, TypeError):
                fn_args = {}
            tool_calls.append(
                _ToolCall(id=getattr(raw_tc, "id", "") or "", name=fn_name, args=fn_args)
            )
        return _LLMResponse(text=text, tool_calls=tool_calls, raw_assistant_payload=msg)

    async def _call_anthropic(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict] | None,
    ) -> _LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        resp = await self._llm.messages.create(**kwargs)
        text_parts: list[str] = []
        tool_calls: list[_ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "tool_use":
                tool_calls.append(
                    _ToolCall(
                        id=getattr(block, "id", "") or "",
                        name=getattr(block, "name", "") or "",
                        args=dict(getattr(block, "input", {}) or {}),
                    )
                )
        return _LLMResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            raw_assistant_payload=resp.content,
        )

    # ── Message-list helpers ──────────────────────────────────────────────────

    def _append_assistant_turn(self, messages: list[dict], resp: _LLMResponse) -> None:
        if _is_anthropic_client(self._llm):
            messages.append({"role": "assistant", "content": resp.raw_assistant_payload})
        else:
            entry: dict[str, Any] = {"role": "assistant", "content": resp.text or None}
            if resp.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                    }
                    for tc in resp.tool_calls
                ]
            messages.append(entry)

    def _append_tool_result(
        self,
        messages: list[dict],
        tool_call: _ToolCall,
        content: str,
        *,
        is_error: bool = False,
    ) -> None:
        if _is_anthropic_client(self._llm):
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call.id,
                            "content": content,
                            "is_error": is_error,
                        }
                    ],
                }
            )
        else:
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": content})

    async def _dispatch(
        self, name: str, args: dict[str, Any], prov: Any, tool_call_id: str
    ) -> list[Any]:
        return await self._toolkit.dispatch(name, args, prov, tool_call_id)

    # ── Core tool-use loop ────────────────────────────────────────────────────

    async def _tool_use_loop(
        self,
        message: str,
        prov: Any,
        system_prompt: str,
        history: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """Drive the native tool-use loop using registry-backed schemas.

        Fetches provider schemas from the capability registry, then iterates
        until the model produces a turn with no tool_calls or the iteration
        cap is reached.
        """
        tools = await tool_schemas_from_registry(
            self._agent.get("tools", []) or [],
            self._provider_format(),
            registry_client=self._registry_client,
        )
        messages: list[dict] = (history or []) + [{"role": "user", "content": message}]

        last_text = ""
        for _ in range(_MAX_TOOL_ITERATIONS):
            resp = await self._call_llm(messages, system_prompt=system_prompt, tools=tools or None)
            last_text = resp.text

            if not resp.tool_calls:
                return resp.text, messages

            self._append_assistant_turn(messages, resp)

            for tc in resp.tool_calls:
                try:
                    nodes = await self._dispatch(tc.name, tc.args, prov, tc.id)
                    self._append_tool_result(
                        messages,
                        tc,
                        json.dumps({"results": nodes}, default=str),
                        is_error=False,
                    )
                except Exception as exc:
                    self._append_tool_result(
                        messages,
                        tc,
                        json.dumps({"error": type(exc).__name__, "detail": str(exc)}),
                        is_error=True,
                    )

        return last_text, messages
