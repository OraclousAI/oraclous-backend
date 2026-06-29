"""Agent tool-use loop (domain layer) — reshape of the legacy ``AgentExecutor`` loop.

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

import asyncio
import json
import os
import random
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

# Transient-LLM-error retry (ADR-042 #551): a producing team fans many members at ONE shared BYOM
# key, so a random member hits a rate-limit (429) / timeout / 5xx — transient, not a real failure.
# Retry such a call a bounded number of times with exponential backoff + full jitter BEFORE the run
# fails; a PERMANENT error (auth / model-not-found / bad-request) is NOT retried (fails fast). The
# transient/permanent split is the LLM client's (LLMClientError.transient). Env-overridable.
_LLM_MAX_RETRIES = max(0, int(os.environ.get("HARNESS_LLM_MAX_RETRIES") or "4"))
_LLM_RETRY_BASE_S = max(0.0, float(os.environ.get("HARNESS_LLM_RETRY_BASE_SECONDS") or "0.5"))
_LLM_RETRY_MAX_S = max(0.0, float(os.environ.get("HARNESS_LLM_RETRY_MAX_SECONDS") or "8.0"))
# indirected so a unit test can substitute a no-op sleep (deterministic, fast)
_async_sleep = asyncio.sleep


def _is_transient(exc: BaseException) -> bool:
    """An LLM-call error a bounded retry may recover (the client marks it ``transient``)."""
    return bool(getattr(exc, "transient", False))


def _retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Exponential backoff with FULL jitter for retry ``attempt`` (0-based), capped. Honours a
    server ``Retry-After`` hint (429/503) when present — wait at least that long, but still capped
    at ``_LLM_RETRY_MAX_S`` so a large hint cannot blow the wall-time budget (ADR-042 #551)."""
    ceiling = min(_LLM_RETRY_MAX_S, _LLM_RETRY_BASE_S * (2**attempt))
    backoff = random.uniform(0, ceiling)  # noqa: S311 — jitter, not security-sensitive
    if retry_after is not None:
        return max(min(retry_after, _LLM_RETRY_MAX_S), backoff)
    return backoff


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


# Completion contract (#543): a member that HAS tools but answers on turn one without calling any of
# them has likely emitted a plan/handoff instead of doing the work (the imported-conductor-agent
# stub). Nudge it ONCE to actually use its tools before the loop accepts the answer. Bounded to a
# single nudge so a legitimately tool-less reasoning member still terminates.
_TOOL_USE_NUDGE = (
    "You replied without calling any tool. You are executing inside Oraclous now — there is no "
    "human to act on a handoff or a proposed next step. If your objective requires producing or "
    "saving any output, you MUST call your tools to do it now (your Write tool persists your "
    "result to the team graph; your Read tool gathers context). Do the work and call the tool — "
    "do not only describe it. If you genuinely have no action to take, state that explicitly."
)


async def run_tool_use_loop(
    *,
    llm: LLMClient,
    system: str,
    user_input: str,
    tool_specs: list[ToolSpec],
    dispatch: Dispatch,
    policy: PolicyEnvelope,
    resume_state: LoopCheckpoint | None = None,
    memory_context: Callable[[], Awaitable[str | None]] | None = None,
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
        # team-scope blackboard READ (#513, ADR-027): before the first LLM turn, pull the team's
        # current memory (the bound/adopted graph, scope=team) and prepend it to the system prompt,
        # so the member reasons with what concurrent members + prior runs of the team already wrote.
        # Fail-soft by contract — the reader swallows its own errors (returns None); a memory read
        # can never block/fail a run. Resumes skip it (the parked messages already carry context).
        if memory_context is not None:
            block = await memory_context()
            if block:
                system = f"{block}\n\n{system}" if system else block
    # The input/output split accumulates over THIS segment (the checkpoint cursor carries only the
    # cumulative total, so a resumed run's split reflects its post-resume turns).
    input_used = 0
    output_used = 0
    steps: list[LoopStep] = []
    last_text = ""
    nudged = False  # completion contract (#543): one-time "use your tools" re-prompt — see below
    # Gate the nudge to PRODUCING members — those with a graph-ingest ("ingest") tool that are meant
    # to persist output. A reasoning/retrieval-only member that legitimately answers without a tool
    # is never re-prompted (so the completion contract can't add a spurious turn to it).
    produces = any(s.operation == "ingest" for s in tool_specs)
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

    def _degrade(name: str, reason: str, message: str, iterations: int) -> LoopResult:
        # #587: on_exhaustion=degrade — FINISH with the best-effort last_text as a flagged PARTIAL
        # (typed reason), never a resumable checkpoint. The single degrade primitive #580 reuses.
        steps.append(LoopStep(len(steps), StepKind.GATE, name, reason, message))
        return LoopResult(
            status=HarnessStatus.PARTIAL,
            output=last_text or None,
            steps=steps,
            iterations=iterations,
            total_tokens=tokens_used,
            input_tokens=input_used,
            output_tokens=output_used,
            error_type=reason,
            error_message=message,
            checkpoint=None,
        )

    def _budget_gate(name: str, reason: str, message: str, iterations: int) -> LoopResult:
        # #587: a BUDGET gate honours on_exhaustion — escalate (today) or degrade (PARTIAL). A HITL
        # pause is NOT routed here (it always _escalate-with-checkpoint); only budget breaches.
        gate = _escalate if policy.on_exhaustion == "escalate" else _degrade
        return gate(name, reason, message, iterations)

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
                return _budget_gate("budget", "wall_time", "wall-time budget exhausted", iteration)
            # Capability-absence ceiling (ADR-035 §5) — upstream of policy; fail-closed DENY of any
            # binding outside the acting member's tools[] BEFORE the gate/budget/dispatch. No path
            # widens the ceiling; an out-of-ceiling call never reaches a side effect.
            if spec is not None and policy.tool_ceiling and spec.binding not in policy.tool_ceiling:
                denied = {
                    "error": "capability_denied",
                    "detail": f"{spec.binding!r} outside ceiling",
                }
                content = _redact(json.dumps(denied), redactors)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                        "content": content,
                    }
                )
                steps.append(
                    LoopStep(
                        len(steps),
                        StepKind.TOOL,
                        f"{spec.binding}.{spec.operation}",
                        "error",
                        _truncate(content),
                    )
                )
                continue
            gated = spec is not None and spec.binding in policy.gated_bindings
            if spec is not None and gated and tc["id"] != approved_id:
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
                return _budget_gate(
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

    async def _complete_with_retry(iteration: int) -> Any:
        """Call the model, retrying ONLY transient errors (backoff+jitter, bounded). Raises the
        last exception when retries are exhausted, the error is permanent, or the wall-time budget
        is spent — so a retry storm can never run past max_wall_time_seconds (ADR-042 #551)."""
        attempt = 0
        while True:
            try:
                return await llm.complete(messages=messages, system=system, tools=tool_specs)
            except Exception as exc:  # noqa: BLE001
                # do NOT retry past the wall-time budget — otherwise N retries (each up to the LLM
                # timeout) + their backoff could run several× past max_wall_time_seconds.
                if attempt >= _LLM_MAX_RETRIES or not _is_transient(exc) or _over_wall_time():
                    raise
                steps.append(
                    LoopStep(
                        len(steps), StepKind.LLM, "primary", "retry", _truncate(f"transient: {exc}")
                    )
                )
                await _async_sleep(_retry_delay(attempt, getattr(exc, "retry_after", None)))
                if (
                    _over_wall_time()
                ):  # the backoff itself may cross the deadline — stop, don't retry
                    raise
                attempt += 1

    for iteration in range(resume_iteration + 1, policy.max_iterations + 1):
        if _over_wall_time():
            return _budget_gate("budget", "wall_time", "wall-time budget exhausted", iteration)

        # ADR-042 (#551): a TRANSIENT provider error (rate-limit / timeout / 5xx / overloaded) is
        # retried with backoff+jitter before the run fails — so one member hitting the shared BYOM
        # key's throttle does not spuriously fail the team. A PERMANENT error (auth / model-not-
        # found / bad-request) is not retried; an exhausted transient or a permanent error → FAILED.
        try:
            resp = await _complete_with_retry(iteration)
        except Exception as exc:  # noqa: BLE001 — transient exhausted, or a permanent error → FAILED
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
            return _budget_gate("budget", "token_budget", "token budget exhausted", iteration)

        if not resp.tool_calls:
            steps.append(
                LoopStep(len(steps), StepKind.LLM, "primary", "answer", _truncate(last_text))
            )
            # Completion contract (#543): if this tool-capable member answered without ever calling
            # a tool, nudge it ONCE to actually use its tools before accepting. Turns an imported
            # conductor-agent's handoff stub into a real tool-using turn; one-shot so a genuinely
            # tool-less reasoning member still terminates on the next pass.
            if not nudged and produces and tool_calls_made == 0:
                nudged = True
                messages.append({"role": "assistant", "content": last_text})
                messages.append({"role": "user", "content": _TOOL_USE_NUDGE})
                steps.append(LoopStep(len(steps), StepKind.LLM, "primary", "nudge", "use-tools"))
                continue
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

    # iteration cap reached without a final answer → escalate or degrade (#587).
    return _budget_gate(
        "budget", "iteration_cap", "tool-use loop did not converge", policy.max_iterations
    )
