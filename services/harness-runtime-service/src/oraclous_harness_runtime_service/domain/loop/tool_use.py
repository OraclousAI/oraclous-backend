"""Agent tool-use loop (ORAA-4 §21 domain layer) — reshape of the legacy ``AgentExecutor`` loop.

Plan→act→observe, capability-agnostic: call the LLM with the available ``ToolSpec``s; if it returns
no tool calls, that text is the final answer; otherwise dispatch each call (via the injected
``dispatch`` callback → the registry), feed results back, and iterate. A tool error is fed back to
the model (so it can adapt) rather than aborting the run.

This is the **coded governance enforcement point** (Section 6 — code wins over prose): before every
dispatch the loop enforces the ``PolicyEnvelope`` — tool-call + wall-time budgets (→ ESCALATED),
HITL gates on flagged capabilities (halt → ESCALATED), and output redaction on every tool result +
the final answer. The prompt (prose) cannot relax any of this. Pure of I/O except through the
injected ``llm`` and ``dispatch``, so it is unit-testable with fakes.

**Mid-loop HITL resume (R5-S6):** when a gated capability halts the loop, the escalation carries a
``LoopCheckpoint`` — the (already-redacted) message transcript, the not-yet-dispatched tool calls
(the gated one first), and the budget cursor — which the service persists. ``resume_state``
re-enters
there: the approved tool-call id bypasses the gate exactly once, everything else is
re-evaluated (a later gated call re-escalates with a fresh checkpoint). Secrets never enter the
checkpoint: the assistant turn is stored redacted (``last_text``), like every tool result.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from oraclous_harness_runtime_service.domain.llm.base import LLMClient, Message, ToolSpec
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind

# A dispatch maps a selected tool + its args to a JSON-able result (or raises, which is fed back).
Dispatch = Callable[[ToolSpec, dict[str, Any]], Awaitable[dict[str, Any]]]

_REDACTED = "[REDACTED]"


@dataclass(frozen=True, slots=True)
class LoopStep:
    index: int
    kind: StepKind
    name: str
    status: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class LoopCheckpoint:
    """The parkable + resumable state at a mid-loop HITL pause. All strings are already redacted, so
    it is safe to persist. ``pending_tool_calls`` are the not-yet-dispatched calls of the paused
    turn (the gated one first); ``approved_tool_call_id`` is the call awaiting human approval."""

    messages: list[Message]
    pending_tool_calls: list[dict[str, Any]]
    approved_tool_call_id: str
    iteration: int
    tool_calls_made: int
    tokens_used: int
    redact_patterns: list[str]


@dataclass(slots=True)
class LoopResult:
    status: HarnessStatus
    output: str | None
    steps: list[LoopStep] = field(default_factory=list)
    iterations: int = 0
    total_tokens: int = 0
    # The input/output split of total_tokens (prompt vs completion). Carried so spend can be priced
    # honestly downstream (output costs ~3-4× input). 0 when the provider omits the split / fake.
    input_tokens: int = 0
    output_tokens: int = 0
    error_type: str | None = None
    error_message: str | None = None
    checkpoint: LoopCheckpoint | None = None  # set only on a mid-loop HITL pause (resumable)


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _redact(text: str, patterns: list[re.Pattern[str]]) -> str:
    for pat in patterns:
        text = pat.sub(_REDACTED, text)
    return text


async def run_tool_use_loop(
    *,
    llm: LLMClient,
    system: str,
    user_input: str,
    tool_specs: list[ToolSpec],
    dispatch: Dispatch,
    policy: PolicyEnvelope,
    resume_state: LoopCheckpoint | None = None,
) -> LoopResult:
    by_name = {s.name: s for s in tool_specs}
    if resume_state is not None:
        redactors = [re.compile(p) for p in resume_state.redact_patterns]
        messages: list[Message] = list(resume_state.messages)
        tool_calls_made = resume_state.tool_calls_made
        tokens_used = resume_state.tokens_used
        resume_iteration = resume_state.iteration
    else:
        redactors = [re.compile(p) for p in policy.redact_patterns]
        messages = [{"role": "user", "content": user_input}]
        tool_calls_made = 0
        tokens_used = 0
        resume_iteration = 0
    # The input/output split accumulates over THIS segment (the checkpoint cursor carries only the
    # cumulative total, so a resumed run's split reflects its post-resume turns).
    input_used = 0
    output_used = 0
    steps: list[LoopStep] = []
    last_text = ""
    started = time.monotonic()

    def _over_wall_time() -> bool:
        return policy.max_wall_time_seconds is not None and (
            time.monotonic() - started > policy.max_wall_time_seconds
        )

    def _escalate(
        name: str,
        reason: str,
        message: str,
        iterations: int,
        checkpoint: LoopCheckpoint | None = None,
    ) -> LoopResult:
        steps.append(LoopStep(len(steps), StepKind.GATE, name, reason, message))
        return LoopResult(
            status=HarnessStatus.ESCALATED,
            output=last_text or None,
            steps=steps,
            iterations=iterations,
            total_tokens=tokens_used,
            input_tokens=input_used,
            output_tokens=output_used,
            error_type=reason,
            error_message=message,
            checkpoint=checkpoint,
        )

    async def _run_tool_calls(
        tool_calls: list[dict[str, Any]], iteration: int, approved_id: str | None
    ) -> LoopResult | None:
        """Dispatch a turn's tool calls. Returns an escalation LoopResult (pause/budget) or None to
        continue. ``approved_id`` (resume only) bypasses the HITL gate for exactly that one call."""
        nonlocal tool_calls_made
        for i, tc in enumerate(tool_calls):
            spec = by_name.get(tc["name"])
            # Coded governance — enforced BEFORE any dispatch, regardless of what the prose said.
            if _over_wall_time():
                return _escalate("budget", "wall_time", "wall-time budget exhausted", iteration)
            gated = spec is not None and spec.binding in policy.gated_bindings
            if gated and tc["id"] != approved_id:
                # Pause: checkpoint the not-yet-dispatched calls (this one first) for resume.
                checkpoint = LoopCheckpoint(
                    messages=list(messages),
                    pending_tool_calls=list(tool_calls[i:]),
                    approved_tool_call_id=tc["id"],
                    iteration=iteration,
                    tool_calls_made=tool_calls_made,
                    tokens_used=tokens_used,
                    redact_patterns=[p.pattern for p in redactors],
                )
                return _escalate(
                    f"{spec.binding}.{spec.operation}",
                    "hitl_required",
                    "capability requires human approval (HITL gate)",
                    iteration,
                    checkpoint=checkpoint,
                )
            if policy.max_tool_calls is not None and tool_calls_made >= policy.max_tool_calls:
                return _escalate(
                    "budget", "tool_call_budget", "tool-call budget exhausted", iteration
                )

            if spec is None:
                unknown = {"error": "unknown_tool", "detail": tc["name"]}
                content = _redact(json.dumps(unknown), redactors)
                status = "error"
                step_name = tc["name"]
            else:
                step_name = f"{spec.binding}.{spec.operation}"
                tool_calls_made += 1
                try:
                    result = await dispatch(spec, tc["args"])
                    content = _redact(json.dumps(result, default=str), redactors)
                    status = "ok"
                except Exception as exc:  # noqa: BLE001 — feed the error back so the model can adapt
                    content = _redact(
                        json.dumps({"error": type(exc).__name__, "detail": str(exc)}), redactors
                    )
                    status = "error"
            messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "name": tc["name"], "content": content}
            )
            steps.append(LoopStep(len(steps), StepKind.TOOL, step_name, status, _truncate(content)))
        return None

    # Resume: finish the paused turn (the approved gated call + any remaining), then continue.
    if resume_state is not None:
        escalation = await _run_tool_calls(
            resume_state.pending_tool_calls, resume_iteration, resume_state.approved_tool_call_id
        )
        if escalation is not None:
            return escalation

    for iteration in range(resume_iteration + 1, policy.max_iterations + 1):
        if _over_wall_time():
            return _escalate("budget", "wall_time", "wall-time budget exhausted", iteration)

        try:
            resp = await llm.complete(messages=messages, system=system, tools=tool_specs)
        except Exception as exc:  # noqa: BLE001 — an LLM-call failure is a hard fail for the run
            steps.append(
                LoopStep(len(steps), StepKind.LLM, "primary", "error", _truncate(str(exc)))
            )
            return LoopResult(
                status=HarnessStatus.FAILED,
                output=last_text or None,
                steps=steps,
                iterations=iteration,
                total_tokens=tokens_used,
                input_tokens=input_used,
                output_tokens=output_used,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        tokens_used += resp.total_tokens
        input_used += resp.input_tokens
        output_used += resp.output_tokens
        last_text = _redact(resp.text, redactors)
        # token budget (S3 PolicyEnvelope.max_tokens, now enforceable with real usage from S4).
        if policy.max_tokens is not None and tokens_used > policy.max_tokens:
            return _escalate("budget", "token_budget", "token budget exhausted", iteration)

        if not resp.tool_calls:
            steps.append(
                LoopStep(len(steps), StepKind.LLM, "primary", "answer", _truncate(last_text))
            )
            return LoopResult(
                HarnessStatus.SUCCEEDED,
                last_text,
                steps,
                iteration,
                total_tokens=tokens_used,
                input_tokens=input_used,
                output_tokens=output_used,
            )

        steps.append(
            LoopStep(
                len(steps),
                StepKind.LLM,
                "primary",
                "tool_calls",
                f"{len(resp.tool_calls)} tool call(s)",
            )
        )
        # Store the REDACTED assistant text (not resp.text) so a checkpoint never persists a secret
        # the model may have echoed; the loop's behaviour is unchanged for non-secret text.
        tool_call_dicts = [
            {"id": tc.id, "name": tc.name, "args": tc.args} for tc in resp.tool_calls
        ]
        messages.append({"role": "assistant", "content": last_text, "tool_calls": tool_call_dicts})

        escalation = await _run_tool_calls(tool_call_dicts, iteration, approved_id=None)
        if escalation is not None:
            return escalation

    # iteration cap reached without a final answer → escalate.
    return _escalate(
        "budget", "iteration_cap", "tool-use loop did not converge", policy.max_iterations
    )
