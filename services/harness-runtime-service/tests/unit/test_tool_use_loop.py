"""The tool-use loop (slice 1): plan→act→observe with fakes — convergence, errors, escalation.

Pure unit: a scripted LLM + an in-memory dispatch, no registry/network. Asserts the loop dispatches
tools, feeds results back, records a step trace, and escalates when it does not converge.
"""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind

pytestmark = pytest.mark.unit

_SPEC = ToolSpec(
    name="pg__list_tables",
    description="list tables",
    parameters={"type": "object", "properties": {}, "required": []},
    binding="pg",
    operation="list_tables",
)


class _ScriptedLLM:
    """Calls the first tool once, then answers with what it observed."""

    protocol_shape = "fake"

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        observed = [m for m in messages if m.get("role") == "tool"]
        if not observed and tools:
            return LLMResponse(text="", tool_calls=[ToolCall("c1", tools[0].name, {})])
        return LLMResponse(text=f"answer: {observed[-1]['content'] if observed else 'none'}")


class _LoopingLLM:
    """Always calls a tool — never answers (forces the iteration cap)."""

    protocol_shape = "fake"

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        return LLMResponse(text="", tool_calls=[ToolCall("c1", tools[0].name, {})])


async def _ok_dispatch(spec: ToolSpec, args: dict) -> dict:
    return {"tables": ["a", "b"]}


async def _boom_dispatch(spec: ToolSpec, args: dict) -> dict:
    raise RuntimeError("connection refused")


async def test_converges_after_one_tool_call() -> None:
    result = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        max_iterations=6,
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert "tables" in (result.output or "")
    kinds = [s.kind for s in result.steps]
    assert StepKind.TOOL in kinds and StepKind.LLM in kinds
    assert any(s.status == "ok" for s in result.steps if s.kind is StepKind.TOOL)


async def test_tool_error_is_fed_back_not_fatal() -> None:
    result = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_boom_dispatch,
        max_iterations=6,
    )
    # the agent still answers (observed the error); the trace records the tool failure.
    assert result.status is HarnessStatus.SUCCEEDED
    assert any(
        s.kind is StepKind.TOOL and s.status == "error" and "connection refused" in (s.detail or "")
        for s in result.steps
    )


async def test_iteration_cap_escalates() -> None:
    result = await run_tool_use_loop(
        llm=_LoopingLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        max_iterations=3,
    )
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "iteration_cap"
    assert result.iterations == 3


async def test_unknown_tool_is_recorded_as_error() -> None:
    class _BadCallLLM:
        protocol_shape = "fake"

        async def complete(self, *, messages, system, tools):  # noqa: ANN001, ANN202
            observed = [m for m in messages if m.get("role") == "tool"]
            if not observed:
                return LLMResponse(text="", tool_calls=[ToolCall("c1", "does_not_exist", {})])
            return LLMResponse(text="done")

    result = await run_tool_use_loop(
        llm=_BadCallLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        max_iterations=6,
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert any("unknown_tool" in (s.detail or "") for s in result.steps)
