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
from oraclous_harness_runtime_service.domain.llm.openai_compatible import LLMClientError
from oraclous_harness_runtime_service.domain.loop import tool_use
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]


async def _no_sleep(_seconds: float) -> None:
    """A no-op stand-in for asyncio.sleep so retry tests are deterministic + fast."""


def _env(
    *,
    max_iterations: int = 6,
    max_tool_calls: int | None = None,
    max_wall: int | None = None,
    max_tokens: int | None = None,
    gated: frozenset[str] = frozenset(),
    ceiling: frozenset[str] = frozenset(),
    redact: tuple[str, ...] = (),
) -> PolicyEnvelope:
    return PolicyEnvelope(
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
        max_wall_time_seconds=max_wall,
        max_tokens=max_tokens,
        gated_bindings=gated,
        tool_ceiling=ceiling,
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


async def test_capability_ceiling_denies_out_of_ceiling_tool() -> None:
    # ADR-035 §5 / item 4: a binding outside the acting member's ceiling is fail-closed denied —
    # never dispatched, regardless of what the model (or any routing path) asked for.
    dispatched = False

    async def _watch(spec: ToolSpec, args: dict) -> dict:
        nonlocal dispatched
        dispatched = True
        return {"ok": 1}

    result = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_watch,
        policy=_env(ceiling=frozenset({"read"})),  # _SPEC.binding "pg" is NOT in the ceiling
    )
    assert dispatched is False  # the out-of-ceiling capability never reached a side effect
    assert any(
        s.kind is StepKind.TOOL and s.status == "error" and "capability_denied" in (s.detail or "")
        for s in result.steps
    )


async def test_in_ceiling_tool_dispatches_normally() -> None:
    result = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(ceiling=frozenset({"pg"})),  # binding "pg" IS in the ceiling
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert any(s.status == "ok" for s in result.steps if s.kind is StepKind.TOOL)


async def test_empty_ceiling_imposes_no_restriction() -> None:
    # the single-agent path passes no ceiling -> behaviour unchanged (regression guard)
    result = await run_tool_use_loop(
        llm=_ScriptedLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(),
    )
    assert result.status is HarnessStatus.SUCCEEDED
    assert any(s.status == "ok" for s in result.steps if s.kind is StepKind.TOOL)


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


# ── #252 — accumulate the input/output token split across turns ──────────────────────────────────


class _SplitLLM:
    """Calls the first tool once (reporting a usage split), then answers (another split). Used to
    prove the loop ACCUMULATES input/output across multiple turns."""

    protocol_shape = "fake"

    async def complete(self, *, messages, system, tools):  # noqa: ANN001, ANN202
        observed = [m for m in messages if m.get("role") == "tool"]
        if not observed and tools:
            return LLMResponse(
                text="",
                tool_calls=[ToolCall("c1", tools[0].name, {})],
                total_tokens=100,
                input_tokens=80,
                output_tokens=20,
            )
        return LLMResponse(text="done", total_tokens=60, input_tokens=50, output_tokens=10)


async def test_input_output_accumulated_on_success() -> None:
    result = await run_tool_use_loop(
        llm=_SplitLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(),
    )
    assert result.status is HarnessStatus.SUCCEEDED
    # two turns: (100/80/20) + (60/50/10).
    assert result.total_tokens == 160
    assert result.input_tokens == 130
    assert result.output_tokens == 30


async def test_input_output_carried_on_error_path() -> None:
    class _SplitThenBreakLLM:
        """First turn reports a split + a tool call; the second turn (after the tool) raises →
        the loop's FAILED return must still carry the accumulated split."""

        protocol_shape = "fake"
        _calls = 0

        async def complete(self, *, messages, system, tools):  # noqa: ANN001, ANN202
            type(self)._calls += 1
            if type(self)._calls == 1:
                return LLMResponse(
                    text="",
                    tool_calls=[ToolCall("c1", tools[0].name, {})],
                    total_tokens=100,
                    input_tokens=70,
                    output_tokens=30,
                )
            raise RuntimeError("provider 503")

    result = await run_tool_use_loop(
        llm=_SplitThenBreakLLM(),
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(),
    )
    assert result.status is HarnessStatus.FAILED
    assert result.total_tokens == 100
    assert result.input_tokens == 70
    assert result.output_tokens == 30


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


# ── ADR-042 (#551) — transient-only LLM retry (rate-limit / timeout / 5xx) ────────────────────


class _FlakyLLM:
    """Raises a TRANSIENT error ``fail_n`` times, then answers — models a 429 throttle clearing."""

    protocol_shape = "fake"

    def __init__(self, fail_n: int) -> None:
        self.calls = 0
        self._fail_n = fail_n

    async def complete(self, *, messages, system, tools):  # noqa: ANN001, ANN202
        self.calls += 1
        if self.calls <= self._fail_n:
            raise LLMClientError("LLM call → 429: rate limited", status_code=429, transient=True)
        return LLMResponse(text="done")


async def test_transient_llm_error_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_use, "_async_sleep", _no_sleep)  # deterministic + fast
    llm = _FlakyLLM(fail_n=2)
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(),
    )
    assert result.status is HarnessStatus.SUCCEEDED  # the throttle cleared on retry
    assert llm.calls == 3  # 2 transient failures + 1 success
    assert sum(1 for s in result.steps if s.status == "retry") == 2  # both retries are traced


async def test_permanent_llm_error_fails_fast_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_use, "_async_sleep", _no_sleep)

    class _AuthFailLLM:
        protocol_shape = "fake"
        calls = 0

        async def complete(self, *, messages, system, tools):  # noqa: ANN001, ANN202
            type(self).calls += 1
            raise LLMClientError("LLM call → 401: bad key", status_code=401, transient=False)

    llm = _AuthFailLLM()
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(),
    )
    assert result.status is HarnessStatus.FAILED
    assert llm.calls == 1  # a permanent error is NOT retried (fail fast)
    assert not any(s.status == "retry" for s in result.steps)


async def test_transient_retries_are_bounded_then_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_use, "_async_sleep", _no_sleep)
    llm = _FlakyLLM(fail_n=999)  # never clears
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(),
    )
    assert result.status is HarnessStatus.FAILED  # retries exhausted → FAILED, not infinite
    assert llm.calls == tool_use._LLM_MAX_RETRIES + 1  # the initial try + the bounded retries


def test_retry_delay_honors_a_retry_after_hint_capped() -> None:
    # ADR-042 (#551): a server Retry-After hint is honoured (wait at least that long) but CAPPED at
    # _LLM_RETRY_MAX_S so a large hint can't blow the wall-time budget; no hint → pure backoff.
    assert tool_use._retry_delay(0, retry_after=5.0) >= 5.0  # honoured
    assert tool_use._retry_delay(0, retry_after=1000.0) <= tool_use._LLM_RETRY_MAX_S  # capped
    assert tool_use._retry_delay(0, None) <= tool_use._LLM_RETRY_BASE_S  # backoff only, bounded


async def test_transient_retry_respects_the_wall_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-042 (#551): a retry storm must NOT run past max_wall_time_seconds. A clock that is within
    # budget until the first attempt, then past it, must stop the retry after ONE attempt — without
    # the wall-time guard the loop would burn all _LLM_MAX_RETRIES (each up to the LLM timeout).
    monkeypatch.setattr(tool_use, "_async_sleep", _no_sleep)
    llm = _FlakyLLM(fail_n=999)  # always transient
    monkeypatch.setattr(
        tool_use.time, "monotonic", lambda: 0.0 if llm.calls == 0 else 100.0
    )  # 0 at start + the first wall check; 100 (past the 1s budget) once an attempt has run
    result = await run_tool_use_loop(
        llm=llm,
        system="",
        user_input="go",
        tool_specs=[_SPEC],
        dispatch=_ok_dispatch,
        policy=_env(max_wall=1),
    )
    assert result.status is HarnessStatus.FAILED
    assert llm.calls == 1  # the wall-time budget stopped the retry after the first attempt
