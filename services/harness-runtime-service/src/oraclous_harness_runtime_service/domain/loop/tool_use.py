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
import difflib
import json
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from oraclous_harness_runtime_service.domain.citation_gate import (
    CitationViolation,
    check_answer_citations,
)
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


# #899: what an unknown tool name is answered with. The loop has always failed closed on a name it
# does not recognise, but it handed the model back its own mistake and nothing else, so the only
# move left was to guess again — the #692/#693 failure, where a member told "409" repeated the
# failing call until its budget ran out.
#: How many near misses to offer. A shortlist the model can act on, never the catalogue relabelled.
_MAX_SUGGESTED_TOOLS = 3
#: The similarity floor for calling something a near miss. Below it the match is a guess, and a
#: wrong suggestion is worse than none: the model takes it and the next turn fails for a new reason.
_NAME_MATCH_CUTOFF = 0.6
#: How many real names to list when NOTHING is close. This reply is written into the transcript on
#: every failing turn, so an unbounded catalogue would grow the prompt once per iteration — for a
#: member that is already failing. Name a sample and stop.
_MAX_LISTED_TOOLS = 20


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
    # #641: the LLM's own id for the tool call this step records (None for an LLM/gate step). It is
    # what makes a member's later claim resolvable back to the call that produced it — without it
    # nothing durable links a driving_signal to a dispatch that actually ran.
    tool_call_id: str | None = None
    # #828 item 2: wall-clock bounds of the real dispatch this step records (an LLM completion or a
    # tool call) — None for a synthetic, effectively-instantaneous bookkeeping step (a gate, a
    # budget halt, a retry note). Nullable/additive like tool_call_id (#641's precedent): every
    # trace persisted before this change still validates.
    started_at: datetime | None = None
    ended_at: datetime | None = None


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
    # #853: the repair state at the moment of the pause. Both are needed, and for opposite reasons.
    # Without the grant, a member that had ALREADY earned its extra call comes back to a budget gate
    # and its corrected document is refused — the repair rejecting the document it asked for.
    # Without the flag, every pause renews the one-shot allowance, which is the retry loop this
    # feature exists not to be. Defaulted so a checkpoint written before #853 resumes unchanged.
    json_repair_used: bool = False
    json_repair_grant: int = 0


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
    # #743 (§CITE): the citation_ids the PLATFORM served to this run, accumulated across every
    # retrieval call and every iteration, deduplicated. A `list` (not a `set`) because this is
    # carried out of the loop and serialised. EMPTY, never None: the answer-time gate reads it on
    # every run, and a None would make "served nothing" indistinguishable from "the loop forgot to
    # record" at the one moment it matters.
    served_citation_ids: list[str] = field(default_factory=list)
    # #907: which LLM client actually ran this segment (the client's own `protocol_shape`, e.g.
    # "fake"/"openai-compatible") — recorded once per run, not per step, because the loop's client
    # never changes mid-run. None only for a client that declares no protocol_shape at all.
    protocol_shape: str | None = None


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

# #580 (ADR-021 degrade-not-crash): a retrieval that returns no data is data-absence, not an error.
# Feed the model this note (instead of letting it loop on the empty result) so it proceeds with what
# it has and STOPS retrying — the run is then flagged PARTIAL at the terminal (never silently).
_EMPTY_RETRIEVAL_NOTE = (
    "No data was found for this query — the graph returned nothing. This is NOT an error: "
    "proceed with what you already have and complete your objective as best you can. Do not keep "
    "retrying the same retrieval; there is no data there to find."
)

# #743 (Contract #735 §CITE): the reserved result key carrying the citation_ids a retrieval served.
# The loop POPS it from EVERY tool result — trusted or not — so the name never reaches the model,
# and ACCUMULATES it only from a trusted binding. Both halves are needed: popping everywhere keeps
# the model from learning the name is live, and accumulating selectively is what makes the id
# unforgeable. Without the second half a model could push the key through any echo-shaped tool (a
# generic REST call, an imported MCP server) and write its own id into the set that the answer-time
# gate checks against — which would make rule 2 check nothing at all.
_SERVED_CITATION_IDS_KEY = "served_citation_ids"

# The registry's own names for the first-party retrieval capabilities that mint citations today.
# This is the DEFAULT only. `run_tool_use_loop(citation_bindings=...)` overrides it, and the harness
# service always does, because a `ToolSpec.binding` is the MANIFEST-chosen alias (a manifest may
# bind core/knowledge-retriever as "retriever", "Read", or anything else), not a verified identity.
# Trusting the alias string alone would let a manifest name an imported MCP binding
# "knowledge-retriever" and be believed. #746 extends the resolved set to web/MCP reads.
_DEFAULT_CITATION_BINDINGS = frozenset({"knowledge-retriever", "federated-search", "find-similar"})

# #781 (security): the same treatment for #580's `data_absent`, and a DELIBERATELY NARROWER set.
# `data_absent` is emitted in exactly one place in the platform — the knowledge-retriever connector
# — while the citation set above covers three capabilities and #746 extends it to live web reads and
# imported MCP tools. An MCP tool's result is whatever the remote server returned, which is the
# echo-shaped surface #781 is about; sharing one set would re-trust this key for those rows the day
# #746 lands, with nobody having decided to. Trust each reserved key from the connectors that
# actually emit it. Both sets are derived together in `harness_execution_service`, so #746 reads
# them side by side rather than discovering this one later.
_DEFAULT_DATA_ABSENT_BINDINGS = frozenset({"knowledge-retriever"})

# #782 (§CITE rev4): what a blocked answer tells the member. A blocked answer goes back to the
# MEMBER, not to the user — the member is the only party that can fix the defect, and an error it
# cannot act on is one it can only retry blindly (the #692/#693 failure, where a member was told
# "409" and simply repeated the failing call). Both strings are the Contract's own wording; rule 2
# additionally NAMES the offending id, because "you cited something wrong" is not actionable.
_CITATION_CORRECTION_RULE_1 = (
    "Your answer names a source in text but carries no citation. Cite the `citation_id` you were "
    "given for that source, or remove the claim."
)
# The same verdict, for the run that was served NOTHING. Approved 2026-08-12, out of the #788
# investigation: a live member burned all 25 of its iterations being told to "cite the `citation_id`
# you were given" for a run whose served set was empty. It had been given none, so the only remedy
# it could actually perform was the one the message buries. An instruction the member cannot follow
# is #692/#693 again — a member told "409" can only retry blindly.
_CITATION_CORRECTION_RULE_1_NOTHING_SERVED = (
    "Your answer names a source in text but carries no citation. No citations were served to this "
    "run, so there is no `citation_id` for you to cite — remove the source attribution from your "
    "answer and state the claim on your own account, or drop the claim."
)
_CITATION_CORRECTION_RULE_2 = (
    "You cited an id that was never served to this run: {ids}. Cite only ids from the results you "
    "were given."
)
# Rule 2 NAMES the offending ids (actionability), but bounded: every id a model fabricates would
# otherwise ride into the correction prompt AND the step detail, once per iteration, unbounded.
# Five is plenty to act on; the remainder is counted, not listed.
_CITATION_CORRECTION_MAX_NAMED_IDS = 5

# #853: the one bounded repair turn for a malformed structured document. A member that declares
# `requires_valid_json` writes its brief as a JSON `graph-ingest` call, and that connector is
# fire-and-forget — it returns {job_id, status} and the knowledge-graph worker parses the document
# later, possibly after the run has already settled. Pre-dispatch is therefore both the right layer
# (the document is wrong where it is written) and the only layer early enough to ask for a fix.
_JSON_REPAIR_STATUS = "json_repair"
# Keyed on the OPERATION, never on the capability's binding name. A binding is the author's own
# label for a capability in their manifest, so matching it meant an author who bound the same
# ingest capability under any other name silently got no check at all — the run lost to a bad
# document exactly as before, with nothing to say why. `operation == "ingest"` is how this loop
# already identifies a producing member (see `produces`), and `source_type` comes from the caller.
_JSON_REPAIR_OPERATION = "ingest"
_JSON_REPAIR_SOURCE_TYPE = "json"
# The parser's OWN message rides into the prompt verbatim ("Expecting property name enclosed in
# double quotes: line 1 column 2541 (char 2540)"). A generic "invalid JSON" is what makes a
# one-shot repair unreliable — a model given the exact position usually fixes it in one turn.
_JSON_REPAIR_MESSAGE = (
    "Your document was NOT saved, because it is not parseable JSON. The parser stopped here:\n\n"
    "{error}\n\n"
    "Write the whole document again with that fixed, and call the tool once more. Send only the "
    "JSON document itself — no prose around it and no markdown fence. This is your one correction: "
    "a second malformed document is saved exactly as written."
)


def _citation_correction(
    violations: list[CitationViolation], *, nothing_served: bool
) -> tuple[str, str]:
    """Turn a failed gate into (the message the member reads, the detail the trace records).

    Both rules can fail one draft, so both messages are carried — correcting only the first would
    cost the member an extra iteration to discover the second. The detail is never blank: a step
    that does not say WHICH rule blocked the answer leaves an operator unable to tell a corrected
    run from a model that simply changed its mind.

    ``nothing_served`` swaps rule 1's remedy for the one that exists when the run's served set is
    empty. The verdict is unchanged — pointing at a source the platform never issued is the defect
    whatever the run served — but the remedy has to be performable, or the member spends the whole
    budget discovering that it is not.
    """
    messages: list[str] = []
    detail: list[str] = []
    if any(violation.rule == 1 for violation in violations):
        messages.append(
            _CITATION_CORRECTION_RULE_1_NOTHING_SERVED
            if nothing_served
            else _CITATION_CORRECTION_RULE_1
        )
        detail.append("rule 1: a source named in prose with no citation alongside it")
    unserved = [v.citation_id for v in violations if v.rule == 2 and v.citation_id]
    if unserved:
        named = unserved[:_CITATION_CORRECTION_MAX_NAMED_IDS]
        if len(unserved) > len(named):
            named.append(f"and {len(unserved) - len(named)} more")
        ids = ", ".join(named)
        messages.append(_CITATION_CORRECTION_RULE_2.format(ids=ids))
        detail.append(f"rule 2: never served — {ids}")
    return "\n\n".join(messages), "; ".join(detail) or "citation gate violation"


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
    citation_bindings: frozenset[str] | None = None,
    data_absent_bindings: frozenset[str] | None = None,
    prior_served_citation_ids: Collection[str] | None = None,
) -> LoopResult:
    by_name = {s.name: s for s in tool_specs}
    trusted_citation_bindings = (
        _DEFAULT_CITATION_BINDINGS if citation_bindings is None else citation_bindings
    )
    trusted_data_absent_bindings = (
        _DEFAULT_DATA_ABSENT_BINDINGS if data_absent_bindings is None else data_absent_bindings
    )
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
    # #853: whether this member has already spent its ONE repair turn. Like `nudged`, a one-shot
    # flag — a repair, not a loop. A second malformed document falls through to the ordinary path
    # and settles exactly as it does today (no second correction, no silent retry storm).
    json_repair_used = resume_state.json_repair_used if resume_state is not None else False
    # #853 (ruled 2026-08-21): the repair is granted ON TOP of the member's own budget, never
    # charged to it — a member that already spent its whole budget on real work is exactly the
    # member this fix exists for, and could not otherwise pay for its own correction. One extra
    # tool call AND one extra iteration, spent only on the repair, and only once. Not a standing
    # exemption: the member's cap binds again on the very next call after the granted one.
    json_repair_grant = resume_state.json_repair_grant if resume_state is not None else 0
    # #580: set when a retrieval reports data-absence (an empty result it flagged). A run that
    # completes after this degrades to a flagged PARTIAL (never a silent SUCCEEDED) — ADR-021.
    # Intentionally NOT carried across a HITL resume (a fresh nonlocal): an empty-retrieval-then-
    # paused run that resumes to completion reports SUCCEEDED — acceptable (degrade-not-crash; the
    # model still saw the "no data" note), a known minor fidelity gap, never a cascade.
    retrieval_empty = False
    # #743: the run's served set — what the PLATFORM handed this member, in first-seen order. Like
    # `retrieval_empty` it is a fresh list on a HITL resume, because the checkpoint carries the
    # transcript rather than platform counters. That loses nothing durable: the resume path UNIONS
    # this segment into the row the pre-pause segment already wrote, so the persisted set stays
    # whole and an answer written after the pause can still cite what was served before it.
    served_citation_ids: list[str] = []
    # #782: what a PRIOR segment of this run served, handed in by the service from the persisted
    # row. It widens what the answer-time gate checks against and NOTHING else — it is deliberately
    # not merged into `served_citation_ids`, because the repository owns the union (`update_run`)
    # and a loop that returned it too would move that merge to the wrong layer.
    prior_served = list(prior_served_citation_ids or [])
    # #782: the detail of the last correction the citation gate issued, or None if it never fired.
    # It is what turns a spent iteration budget into a TYPED citation failure at the terminal below
    # rather than an anonymous "did not converge".
    citation_blocked: str | None = None
    # #792: whether that last block included a rule 2 violation (a forged id). The terminal's
    # precedence SPLITS BY RULE, so the flag has to carry which defect it recorded. Cleared
    # wherever `citation_blocked` is.
    citation_blocked_rule2 = False
    # Gate the nudge to PRODUCING members — those with a graph-ingest ("ingest") tool that are meant
    # to persist output. A reasoning/retrieval-only member that legitimately answers without a tool
    # is never re-prompted (so the completion contract can't add a spurious turn to it).
    produces = any(s.operation == "ingest" for s in tool_specs)
    started = time.monotonic()
    # #907: the client's own declared shape — read once, stamped on every LoopResult this run
    # produces. The loop's client never changes mid-run, so this is not a per-step concern.
    protocol_shape = getattr(llm, "protocol_shape", None)

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
            served_citation_ids=list(served_citation_ids),
            protocol_shape=protocol_shape,
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
            # #580 + #743: a run that degrades on a LATER empty retrieval still served what it
            # served, and the answer may legitimately cite it. Carry it out.
            served_citation_ids=list(served_citation_ids),
            protocol_shape=protocol_shape,
        )

    def _budget_gate(name: str, reason: str, message: str, iterations: int) -> LoopResult:
        # #587: a BUDGET gate honours on_exhaustion — escalate (today) or degrade (PARTIAL). A HITL
        # pause is NOT routed here (it always _escalate-with-checkpoint); only budget breaches.
        # #580: a member that ran out of ITERATIONS while blocked by a data-absent retrieval churned
        # on missing data — degrade (PARTIAL) regardless of on_exhaustion, so a from-scratch/empty-
        # graph member never hard-fails the team on missing data (ADR-021). A token/wall/tool-call
        # overrun is real work, NOT data-absence churn → it still honours on_exhaustion (escalate).
        if retrieval_empty and reason == "iteration_cap":
            return _degrade(
                "dependency", "empty_retrieval", "did not converge on missing data", iterations
            )
        gate = _escalate if policy.on_exhaustion == "escalate" else _degrade
        return gate(name, reason, message, iterations)

    async def _run_tool_calls(
        tool_calls: list[dict[str, Any]], iteration: int, approved_id: str | None
    ) -> LoopResult | None:
        """Dispatch a turn's tool calls. Returns an escalation LoopResult (pause/budget) or None to
        continue. ``approved_id`` (resume only) bypasses the HITL gate for exactly that one call."""
        nonlocal tool_calls_made, retrieval_empty, json_repair_used, json_repair_grant
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
                        tool_call_id=tc["id"],
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
                    json_repair_used=json_repair_used,
                    json_repair_grant=json_repair_grant,
                )
                return _escalate(
                    f"{spec.binding}.{spec.operation}",
                    "hitl_required",
                    "capability requires human approval (HITL gate)",
                    iteration,
                    checkpoint=checkpoint,
                )
            # #853: the structured-output check, BEFORE the tool-call budget gate on purpose. The
            # member this fix exists for spends its budget on real work and only then writes the
            # broken brief, so a check placed after the gate would never run on the call that
            # matters. Scoped to a JSON `graph-ingest` document: a declared member still ingests
            # prose, and prose that was never meant to be JSON is not malformed JSON.
            if (
                spec is not None
                and policy.requires_valid_json
                and not json_repair_used
                and spec.operation == _JSON_REPAIR_OPERATION
                and str(tc["args"].get("source_type", "")).strip().lower()
                == _JSON_REPAIR_SOURCE_TYPE
            ):
                document = tc["args"].get("content")
                parse_error: str | None = None
                if isinstance(document, str):  # a non-str content is already structured
                    try:
                        json.loads(document)
                    except ValueError as exc:
                        parse_error = str(exc)
                if parse_error is not None:
                    json_repair_used = True
                    json_repair_grant = 1
                    correction = _redact(_JSON_REPAIR_MESSAGE.format(error=parse_error), redactors)
                    # A `tool` turn, not a bare `user` one: the assistant turn already carries this
                    # tool_call_id, and a provider transcript with a call and no matching result is
                    # malformed. The member reads the correction where it expects the result.
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": tc["name"],
                            "content": correction,
                        }
                    )
                    steps.append(
                        LoopStep(
                            len(steps),
                            StepKind.GATE,
                            "structured_output",
                            _JSON_REPAIR_STATUS,
                            _truncate(parse_error),
                            tool_call_id=tc["id"],
                        )
                    )
                    continue  # never dispatched — the malformed document is not persisted at all
            if (
                policy.max_tool_calls is not None
                and tool_calls_made >= policy.max_tool_calls + json_repair_grant
            ):
                return _budget_gate(
                    "budget", "tool_call_budget", "tool-call budget exhausted", iteration
                )

            tool_started: datetime | None = None
            tool_ended: datetime | None = None
            if spec is None:
                # #899: name what the model probably meant. The call is still never dispatched —
                # the hint sits ALONGSIDE the fail-closed rule, never in place of it.
                unknown: dict[str, Any] = {"error": "unknown_tool", "detail": tc["name"]}
                near = difflib.get_close_matches(
                    tc["name"], by_name, n=_MAX_SUGGESTED_TOOLS, cutoff=_NAME_MATCH_CUTOFF
                )
                if near:
                    unknown["did_you_mean"] = near
                else:
                    # nothing resembles it, so a shortlist would be empty — which is the verdict
                    # this replaces. Name what DOES exist instead, bounded.
                    unknown["available_tools"] = sorted(by_name)[:_MAX_LISTED_TOOLS]
                content = _redact(json.dumps(unknown), redactors)
                status = "error"
                step_name = tc["name"]
            else:
                step_name = f"{spec.binding}.{spec.operation}"
                tool_calls_made += 1
                tool_started = datetime.now(UTC)
                try:
                    result = await dispatch(spec, tc["args"])
                    # #580: a retrieval that found nothing flags `data_absent` — a RESERVED result
                    # key set ONLY by the knowledge-retriever connector on an empty result (no other
                    # tool may emit it). Strip the private flag, swap in a clear proceed-note so the
                    # model stops looping on empty, and mark the run to degrade (ADR-021).
                    #
                    # #781 (security): "no other tool may emit it" is now ENFORCED rather than
                    # asserted in a comment. The pop stays unconditional — the key never reaches the
                    # model, trusted binding or not — but the flag is BELIEVED only from a trusted
                    # retrieval. A forged one bought the model three things: a false data-absence
                    # alert to an operator, the platform-authored proceed-note on demand, and (since
                    # the #792 precedence ruling) a softer citation terminal — a rule 1-only block
                    # degrading to PARTIAL/`empty_retrieval`, which SHIPS the blocked answer to the
                    # user flagged, where ESCALATED would have stopped it. See the trust-set note.
                    if isinstance(result, dict):
                        absent = result.pop("data_absent", False)
                        if absent and spec.binding in trusted_data_absent_bindings:
                            retrieval_empty = True
                            result["note"] = _EMPTY_RETRIEVAL_NOTE
                    # #743 (§CITE): pop the served-ids key from EVERY tool result, then accumulate
                    # it only from a TRUSTED retrieval binding. A model-supplied key of the same
                    # name — pushed through a generic REST call or an imported MCP server — is
                    # therefore stripped and never merged, so an id the model chose can never be in
                    # the set rule 2 checks against. Provenance, never a format check: a forged id
                    # is indistinguishable from a real one by shape.
                    if isinstance(result, dict):
                        served = result.pop(_SERVED_CITATION_IDS_KEY, None)
                        if spec.binding in trusted_citation_bindings and isinstance(served, list):
                            for citation_id in served:
                                if (
                                    isinstance(citation_id, str)
                                    and citation_id
                                    and citation_id not in served_citation_ids
                                ):
                                    served_citation_ids.append(citation_id)
                    content = _redact(json.dumps(result, default=str), redactors)
                    status = "ok"
                except Exception as exc:  # noqa: BLE001 — feed the error back so the model can adapt
                    content = _redact(
                        json.dumps({"error": type(exc).__name__, "detail": str(exc)}), redactors
                    )
                    status = "error"
                finally:
                    tool_ended = datetime.now(UTC)
            # #642: show the receipt id INSIDE the tool result the model reads. The provider's
            # `tool_call_id` field is transport metadata the model never sees, so a member asked to
            # cite its receipts could only guess — real models cited the tool NAME, a chunk id, or
            # "1", and were failed for it despite having really made the call. The visible receipt
            # line is what makes the grounding contract satisfiable rather than a trap.
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "content": f"{content}\n[receipt: source_tool_call_id={tc['id']}]",
                }
            )
            steps.append(
                LoopStep(
                    len(steps),
                    StepKind.TOOL,
                    step_name,
                    status,
                    _truncate(content),
                    tool_call_id=tc["id"],
                    started_at=tool_started,
                    ended_at=tool_ended,
                )
            )
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

    # #853: a `while` rather than a `range()` because the iteration cap is no longer fixed — a
    # spent repair turn grants one extra iteration alongside the extra tool call, so a member that
    # discovers its malformed document on its last allowed iteration can still write the fixed one.
    iteration = resume_iteration
    while iteration < policy.max_iterations + json_repair_grant:
        iteration += 1
        if _over_wall_time():
            return _budget_gate("budget", "wall_time", "wall-time budget exhausted", iteration)

        # ADR-042 (#551): a TRANSIENT provider error (rate-limit / timeout / 5xx / overloaded) is
        # retried with backoff+jitter before the run fails — so one member hitting the shared BYOM
        # key's throttle does not spuriously fail the team. A PERMANENT error (auth / model-not-
        # found / bad-request) is not retried; an exhausted transient or a permanent error → FAILED.
        llm_started = datetime.now(UTC)
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
                served_citation_ids=list(served_citation_ids),
                protocol_shape=protocol_shape,
            )
        llm_ended = datetime.now(UTC)
        tokens_used += resp.total_tokens
        input_used += resp.input_tokens
        output_used += resp.output_tokens
        last_text = _redact(resp.text, redactors)
        # token budget (S3 PolicyEnvelope.max_tokens, now enforceable with real usage from S4).
        if policy.max_tokens is not None and tokens_used > policy.max_tokens:
            return _budget_gate("budget", "token_budget", "token budget exhausted", iteration)

        if not resp.tool_calls:
            steps.append(
                LoopStep(
                    len(steps),
                    StepKind.LLM,
                    "primary",
                    "answer",
                    _truncate(last_text),
                    started_at=llm_started,
                    ended_at=llm_ended,
                )
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
            # #782 (Contract #735 §CITE rev4): the answer-time citation gate, INSIDE the loop. A
            # blocked answer goes back to the MEMBER and never to the user, so the gate has to run
            # before the answer is accepted rather than after this function returns. The mechanism
            # is the completion nudge's, twelve lines above — append the answer, append a
            # correction, record the step, continue — but NOT its one-shot flag: each correction
            # consumes an iteration from the run's existing budget (§CITE Limit 1), because a
            # one-shot correction would accept whatever the member writes on attempt two, including
            # a second fabrication. The gate checks against the PERSISTED UNION (a prior segment's
            # served set + this one's), or a post-pause answer citing a pre-pause source would be
            # failed by bookkeeping.
            gate_served = [*prior_served, *served_citation_ids]
            check = check_answer_citations(last_text, gate_served)
            if not check.passed:
                correction, citation_blocked = _citation_correction(
                    check.violations, nothing_served=not gate_served
                )
                citation_blocked_rule2 = any(v.rule == 2 for v in check.violations)
                messages.append({"role": "assistant", "content": last_text})
                messages.append({"role": "user", "content": correction})
                steps.append(
                    LoopStep(
                        len(steps),
                        StepKind.GATE,
                        "citation",
                        "citation_correction",
                        _truncate(citation_blocked),
                    )
                )
                continue
            if retrieval_empty:
                # #580: the member completed, but a retrieval reported data-absence — degrade to a
                # flagged PARTIAL (never a silent SUCCEEDED) via #587's _degrade, so the data gap
                # surfaces (ADR-021 never-silently). Non-cascading: the team still completes.
                return _degrade(
                    "dependency",
                    "empty_retrieval",
                    "retrieval returned no data; the member proceeded with what was available",
                    iteration,
                )
            return LoopResult(
                HarnessStatus.SUCCEEDED,
                last_text,
                steps,
                iteration,
                total_tokens=tokens_used,
                input_tokens=input_used,
                output_tokens=output_used,
                served_citation_ids=list(served_citation_ids),
                protocol_shape=protocol_shape,
            )

        # A tool-call turn is the member moving PAST a blocked draft, so the run no longer ends on
        # one. Without this clear, the flag is sticky: a run corrected once and then failing to
        # converge for an unrelated reason is reported `citation_unresolved` ("could not produce a
        # citable answer"), and #587's on_exhaustion="degrade" is overridden for plain
        # non-convergence. The terminal below must fire only when the LAST completed turn was a
        # blocked answer.
        citation_blocked = None
        citation_blocked_rule2 = False
        steps.append(
            LoopStep(
                len(steps),
                StepKind.LLM,
                "primary",
                "tool_calls",
                f"{len(resp.tool_calls)} tool call(s)",
                started_at=llm_started,
                ended_at=llm_ended,
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

    # #782 (§CITE rev4 Limit 1): the member spent the run's budget without producing an answer that
    # clears the citation gate. The run FAILS, typed — "did not converge" tells an operator nothing,
    # and #692 is the record of what an untyped failure costs. The last blocked draft is still
    # carried out as the output (`_escalate` uses `last_text`): a blocked run with an empty output
    # is unauditable.
    #
    # This is its OWN return and deliberately NOT a `_budget_gate` call, even though `_budget_gate`
    # is the obvious precedent. `on_exhaustion` is a per-member BUDGET preference (#587,
    # `member_on_exhaustion` rides the resume cursor) meaning "finish with what you have rather than
    # pause". A member that could not clear the gate produced WRONG data, and shipping it as a
    # flagged PARTIAL still ships it — which is the option the Contract rejected, restored through a
    # user knob. `_escalate` is unconditional, which is exactly what this terminal needs.
    #
    # #792 (ruled 2026-08-13): the precedence against #580's empty-retrieval degrade SPLITS BY
    # RULE. A rule 2 violation (a forged id) is wrong data and escalates regardless of data-absence
    # and of `on_exhaustion` — degrading it would ship the forged citation flagged. A rule 1-only
    # block on a run whose retrieval reported data-absence is the accepted Limit 2 misfire landing
    # on MISSING data: fall through to `_budget_gate`, whose #580 branch degrades it to
    # PARTIAL/`empty_retrieval` (ADR-021) — the same terminal the identical decline reaches without
    # the marker. A rule 1-only block with data present still escalates: the member had sources to
    # cite and spent the budget not citing them.
    if citation_blocked is not None and (citation_blocked_rule2 or not retrieval_empty):
        return _escalate(
            "citation",
            "citation_unresolved",
            f"the member could not produce a citable answer within the budget ({citation_blocked})",
            policy.max_iterations + json_repair_grant,
        )
    # iteration cap reached without a final answer → escalate or degrade (#587).
    return _budget_gate(
        "budget",
        "iteration_cap",
        "tool-use loop did not converge",
        policy.max_iterations + json_repair_grant,
    )
