"""Agent tool-use loop (ORAA-4 §21 domain layer) — reshape of the legacy ``AgentExecutor`` loop.

Plan→act→observe, capability-agnostic: call the LLM with the available ``ToolSpec``s; if it returns
no tool calls, that text is the final answer; otherwise dispatch each call (via the injected
``dispatch`` callback → the registry), feed results back, and iterate up to ``max_iterations``. A
tool error is fed back to the model (so it can adapt) rather than aborting the run — matching the
legacy behaviour. Pure of I/O except through the injected ``llm`` and ``dispatch`` collaborators, so
it is unit-testable with fakes. Governance gates (budget/HITL/redaction) hook in here in slice 3.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from oraclous_harness_runtime_service.domain.llm.base import LLMClient, Message, ToolSpec
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind

# A dispatch maps a selected tool + its args to a JSON-able result (or raises, which is fed back).
Dispatch = Callable[[ToolSpec, dict[str, Any]], Awaitable[dict[str, Any]]]


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
    error_type: str | None = None
    error_message: str | None = None


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


async def run_tool_use_loop(
    *,
    llm: LLMClient,
    system: str,
    user_input: str,
    tool_specs: list[ToolSpec],
    dispatch: Dispatch,
    max_iterations: int,
) -> LoopResult:
    by_name = {s.name: s for s in tool_specs}
    messages: list[Message] = [{"role": "user", "content": user_input}]
    steps: list[LoopStep] = []
    last_text = ""

    for iteration in range(1, max_iterations + 1):
        resp = await llm.complete(messages=messages, system=system, tools=tool_specs)
        last_text = resp.text

        if not resp.tool_calls:
            steps.append(
                LoopStep(len(steps), StepKind.LLM, "primary", "answer", _truncate(resp.text))
            )
            return LoopResult(HarnessStatus.SUCCEEDED, resp.text, steps, iteration)

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
            if spec is None:
                content = json.dumps({"error": "unknown_tool", "detail": tc.name})
                status = "error"
                step_name = tc.name
            else:
                step_name = f"{spec.binding}.{spec.operation}"
                try:
                    result = await dispatch(spec, tc.args)
                    content = json.dumps(result, default=str)
                    status = "ok"
                except Exception as exc:  # noqa: BLE001 — feed the error back so the model can adapt
                    content = json.dumps({"error": type(exc).__name__, "detail": str(exc)})
                    status = "error"
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": content}
            )
            steps.append(LoopStep(len(steps), StepKind.TOOL, step_name, status, _truncate(content)))

    # iteration cap reached without a final answer → escalate (slice 3 enriches gate outcomes).
    return LoopResult(
        status=HarnessStatus.ESCALATED,
        output=last_text or None,
        steps=steps,
        iterations=max_iterations,
        error_type="iteration_cap",
        error_message="tool-use loop did not converge within the iteration budget",
    )
