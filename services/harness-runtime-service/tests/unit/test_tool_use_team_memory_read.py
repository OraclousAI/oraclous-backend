"""The tool-use loop injects the TEAM-scope memory blackboard before reasoning (#513, ADR-027).

The graph-adopt blackboard's READ side: a team member, before its first LLM turn, pulls the team's
current memory (``scope=team`` for THIS team, from the adopted graph) and injects it into context —
so a member sees what concurrent members + prior runs of the team already wrote (the shared
world-model). The harness binds the reader to (adopted graph_id, team_id, the run query); the loop
just invokes it early and feeds the block into the model's context.

Absent reader (non-team / single-agent run) → the loop behaves exactly as before (no injection).

RED until #513 [impl] adds the ``memory_context`` read seam to ``run_tool_use_loop`` and injects it
before the first ``llm.complete``.
"""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolSpec,
)
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]

_TEAM_BLOCK = "## Relevant Memory\n- TEAM-FINDING-7f3a: the adopted-graph world-model node"


def _env() -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=4,
        max_tool_calls=None,
        max_wall_time_seconds=None,
        max_tokens=None,
        gated_bindings=frozenset(),
        tool_ceiling=frozenset(),
        redact_patterns=(),
    )


class _RecordingLLM:
    """Answers immediately (no tool) and records the (system, messages) of each completion."""

    protocol_shape = "fake"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return LLMResponse(text="done", tool_calls=[])


async def _noop_dispatch(spec: ToolSpec, args: dict) -> dict:  # pragma: no cover - never called
    return {}


async def test_team_memory_block_is_injected_before_the_first_llm_call() -> None:
    """The team-scope read runs before reasoning; its block reaches the FIRST LLM call's context."""
    llm = _RecordingLLM()
    reads = 0

    async def memory_context() -> str | None:
        nonlocal reads
        reads += 1
        return _TEAM_BLOCK

    await run_tool_use_loop(
        llm=llm,
        system="You are a team researcher.",
        user_input="extend the world model",
        tool_specs=[],
        dispatch=_noop_dispatch,
        policy=_env(),
        memory_context=memory_context,
    )

    assert reads >= 1, "the team-scope memory read must run"
    assert llm.calls, "the LLM must have been called"
    first = llm.calls[0]
    blob = first["system"] + "\n" + "\n".join(str(m.get("content", "")) for m in first["messages"])
    assert "TEAM-FINDING-7f3a" in blob, "the team memory block must be in the first call's context"


async def test_no_reader_means_no_injection_back_compat() -> None:
    """A single-agent run (no ``memory_context``) reasons with exactly the original context."""
    llm = _RecordingLLM()
    await run_tool_use_loop(
        llm=llm,
        system="You are a lone agent.",
        user_input="go",
        tool_specs=[],
        dispatch=_noop_dispatch,
        policy=_env(),
    )
    first = llm.calls[0]
    assert first["system"] == "You are a lone agent."
    assert all("Relevant Memory" not in str(m.get("content", "")) for m in first["messages"])
