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


@dataclass(slots=True)
class LoopResult:
    status: HarnessStatus
    output: str | None
    steps: list[LoopStep] = field(default_factory=list)
    iterations: int = 0
    total_tokens: int = 0
    error_type: str | None = None
    error_message: str | None = None


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
) -> LoopResult:
    by_name = {s.name: s for s in tool_specs}
    redactors = [re.compile(p) for p in policy.redact_patterns]
    messages: list[Message] = [{"role": "user", "content": user_input}]
    steps: list[LoopStep] = []
    last_text = ""
    tool_calls_made = 0
    tokens_used = 0
    started = time.monotonic()

    def _over_wall_time() -> bool:
        return policy.max_wall_time_seconds is not None and (
            time.monotonic() - started > policy.max_wall_time_seconds
        )

    def _escalate(name: str, reason: str, message: str, iterations: int) -> LoopResult:
        steps.append(LoopStep(len(steps), StepKind.GATE, name, reason, message))
        return LoopResult(
            status=HarnessStatus.ESCALATED,
            output=last_text or None,
            steps=steps,
            iterations=iterations,
            total_tokens=tokens_used,
            error_type=reason,
            error_message=message,
        )

    for iteration in range(1, policy.max_iterations + 1):
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
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        tokens_used += resp.total_tokens
        last_text = _redact(resp.text, redactors)
        # token budget (S3 PolicyEnvelope.max_tokens, now enforceable with real usage from S4).
        if policy.max_tokens is not None and tokens_used > policy.max_tokens:
            return _escalate("budget", "token_budget", "token budget exhausted", iteration)

        if not resp.tool_calls:
            steps.append(
                LoopStep(len(steps), StepKind.LLM, "primary", "answer", _truncate(last_text))
            )
            return LoopResult(
                HarnessStatus.SUCCEEDED, last_text, steps, iteration, total_tokens=tokens_used
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
        messages.append(
            {
                "role": "assistant",
                "content": resp.text,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "args": tc.args} for tc in resp.tool_calls
                ],
            }
        )

        for tc in resp.tool_calls:
            spec = by_name.get(tc.name)
            # Coded governance — enforced BEFORE any dispatch, regardless of what the prose said.
            # Re-checked per tool call so a batched turn can't overrun the budget mid-batch.
            if _over_wall_time():
                return _escalate("budget", "wall_time", "wall-time budget exhausted", iteration)
            if spec is not None and spec.binding in policy.gated_bindings:
                return _escalate(
                    f"{spec.binding}.{spec.operation}",
                    "hitl_required",
                    "capability requires human approval (HITL gate)",
                    iteration,
                )
            if policy.max_tool_calls is not None and tool_calls_made >= policy.max_tool_calls:
                return _escalate(
                    "budget", "tool_call_budget", "tool-call budget exhausted", iteration
                )

            if spec is None:
                content = _redact(
                    json.dumps({"error": "unknown_tool", "detail": tc.name}), redactors
                )
                status = "error"
                step_name = tc.name
            else:
                step_name = f"{spec.binding}.{spec.operation}"
                tool_calls_made += 1
                try:
                    result = await dispatch(spec, tc.args)
                    content = _redact(json.dumps(result, default=str), redactors)
                    status = "ok"
                except Exception as exc:  # noqa: BLE001 — feed the error back so the model can adapt
                    content = _redact(
                        json.dumps({"error": type(exc).__name__, "detail": str(exc)}), redactors
                    )
                    status = "error"
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": content}
            )
            steps.append(LoopStep(len(steps), StepKind.TOOL, step_name, status, _truncate(content)))

    # iteration cap reached without a final answer → escalate.
    return _escalate(
        "budget", "iteration_cap", "tool-use loop did not converge", policy.max_iterations
    )
