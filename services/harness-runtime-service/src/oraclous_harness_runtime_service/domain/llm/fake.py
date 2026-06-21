"""Deterministic fake LLM client (domain layer).

The key-free seam that lets the whole runtime — OHM load → tool-use loop → real registry dispatch →
provenance — be exercised in CI without an external model key. Its behaviour is intentionally simple
and total: on the first turn it calls the entrypoint tool (the first available ``ToolSpec``) so the
loop genuinely dispatches a real capability; once it has observed a tool result it returns a final
answer summarising it. This proves the loop; the real protocol-shape clients land in slice 4.
"""

from __future__ import annotations

from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)


class FakeLLMClient:
    """A scripted, single-tool-then-answer responder. Never makes a network call."""

    protocol_shape = "fake"

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        observed = [m for m in messages if m.get("role") == "tool"]
        # First turn with tools available → call the entrypoint tool so the loop dispatches it.
        if not observed and tools:
            spec = tools[0]
            return LLMResponse(
                text="",
                tool_calls=[ToolCall(id="call-1", name=spec.name, args={})],
            )
        # Otherwise answer, summarising what was observed (or that nothing was available).
        if observed:
            last = observed[-1]
            return LLMResponse(
                text=f"Completed. Observed tool result: {str(last.get('content'))[:500]}",
                tool_calls=[],
            )
        return LLMResponse(text="No tools were available; nothing to do.", tool_calls=[])
