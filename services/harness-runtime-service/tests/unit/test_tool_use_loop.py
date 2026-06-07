"""The tool-use loop (slice 1): plan→act→observe with fakes — convergence, errors, escalation.

Pure unit: a scripted LLM + an in-memory dispatch, no registry/network. Asserts the loop dispatches
tools, feeds results back, records a step trace, and escalates when it does not converge.
"""

from __future__ import annotations

import json

import pytest
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind

pytestmark = pytest.mark.unit


def _env(
    *,
    max_iterations: int = 6,
    max_tool_calls: int | None = None,
    max_wall: int | None = None,
    max_tokens: int | None = None,
    gated: frozenset[str] = frozenset(),
    redact: tuple[str, ...] = (),
) -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
        max_wall_time_seconds=max_wall,
        max_tokens=max_tokens,
        gated_bindings=gated,
        redact_patterns=redact,
    )


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
        policy=_env(),
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
        policy=_env(),
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
        policy=_env(max_iterations=3),
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
        policy=_env(),
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert any("unknown_tool" in (s.detail or "") for s in result.steps)


async def test_llm_failure_is_a_hard_fail() -> None:
    class _BrokenLLM:
        protocol_shape = "fake"

        async def complete(self, *, messages, system, tools):  # noqa: ANN001, ANN202
            raise RuntimeError("provider 503")

    result = await run_tool_use_loop(
        llm=_BrokenLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(),
    )
    # an LLM-call failure is terminal (FAILED), distinct from a tool error (which is fed back).
    assert result.status is HarnessStatus.FAILED
    assert result.error_type == "RuntimeError"
    assert "provider 503" in (result.error_message or "")


# ── slice 3 — coded governance enforcement (code wins over prose) ─────────────────────────────────


async def test_tool_call_budget_halts() -> None:
    result = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(max_tool_calls=0),  # no tool calls allowed
    )
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "tool_call_budget"
    assert any(s.kind is StepKind.GATE for s in result.steps)


async def test_hitl_gate_halts_before_dispatch() -> None:
    dispatched = False

    async def _watch(spec: ToolSpec, args: dict) -> dict:
        nonlocal dispatched
        dispatched = True
        return {}

    result = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_watch,
        policy=_env(gated=frozenset({"pg"})),
    )
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "hitl_required"
    assert dispatched is False  # the gated capability was NOT executed


async def test_wall_time_budget_halts() -> None:
    result = await run_tool_use_loop(
        llm=_LoopingLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(max_wall=0),  # any elapsed time exhausts the budget
    )
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "wall_time"


class _TokenLLM:
    """Reports token usage on each turn; answers immediately."""

    protocol_shape = "fake"

    def __init__(self, tokens: int) -> None:
        self._tokens = tokens

    async def complete(self, *, messages, system, tools):  # noqa: ANN001, ANN202
        return LLMResponse(text="done", tool_calls=[], total_tokens=self._tokens)


async def test_token_budget_halts() -> None:
    result = await run_tool_use_loop(
        llm=_TokenLLM(150),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(max_tokens=100),  # the first turn's 150 tokens exceeds the budget
    )
    assert result.status is HarnessStatus.ESCALATED
    assert result.error_type == "token_budget"
    assert result.total_tokens == 150


async def test_total_tokens_recorded_on_success() -> None:
    result = await run_tool_use_loop(
        llm=_TokenLLM(42),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(max_tokens=1000),
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert result.total_tokens == 42


async def test_output_is_redacted() -> None:
    async def _secret(spec: ToolSpec, args: dict) -> dict:
        return {"token": "SECRET123"}

    result = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_secret,
        policy=_env(redact=("SECRET123",)),
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert "SECRET123" not in (result.output or "")
    assert "[REDACTED]" in (result.output or "")


# ── slice S6 — mid-loop HITL checkpoint + resume ─────────────────────────────────────────────────


async def test_hitl_gate_carries_a_resumable_checkpoint() -> None:
    result = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(gated=frozenset({"pg"})),
    )
    assert result.status is HarnessStatus.ESCALATED and result.error_type == "hitl_required"
    cp = result.checkpoint
    assert cp is not None
    assert cp.approved_tool_call_id == "c1"
    assert [t["id"] for t in cp.pending_tool_calls] == ["c1"]  # the gated call, not yet dispatched
    assert any(m.get("role") == "assistant" for m in cp.messages)  # transcript captured


async def test_resume_dispatches_the_approved_tool_and_converges() -> None:
    paused = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(gated=frozenset({"pg"})),
    )
    dispatched = False

    async def _watch(spec: ToolSpec, args: dict) -> dict:
        nonlocal dispatched
        dispatched = True
        return {"tables": ["a", "b"]}

    resumed = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_watch,
        policy=_env(gated=frozenset({"pg"})),
        resume_state=paused.checkpoint,
    )
    assert dispatched is True  # the approved gated tool ran on resume (bypassed the gate once)
    assert resumed.status is HarnessStatus.SUCCEEDED
    assert "tables" in (resumed.output or "")


async def test_checkpoint_messages_never_persist_a_secret() -> None:
    # the model echoes a secret in its assistant text; the checkpoint must store it redacted.
    class _SecretLLM:
        protocol_shape = "fake"

        async def complete(self, *, messages, system, tools):  # noqa: ANN001, ANN202
            return LLMResponse(
                text="my secret is SECRET123", tool_calls=[ToolCall("c1", tools[0].name, {})]
            )

    result = await run_tool_use_loop(
        llm=_SecretLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(gated=frozenset({"pg"}), redact=("SECRET123",)),
    )
    cp = result.checkpoint
    assert cp is not None
    blob = json.dumps(cp.messages)
    assert "SECRET123" not in blob and "[REDACTED]" in blob
