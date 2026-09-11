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

from oraclous_ohm.sites import InvalidSiteError, normalise_sites

from oraclous_harness_runtime_service.domain.citation_gate import (
    CitationViolation,
    check_answer_citations,
)
from oraclous_harness_runtime_service.domain.link_provenance import (
    LINK_CORRECTION_STATUS,
    LINK_FLAG_STATUS,
    LINK_GATE_NAME,
    MAX_FETCHED_URLS,
    canonical_urls,
    check_answer_links,
    expand_source_markers,
    extract_answer_urls,
    strip_unverified_links,
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
    # #944: the URLs the member's own answer links that this run never fetched — the inline-link
    # half of the citation story, which no `cit_` id can cover because the member composed these
    # itself. EMPTY, never None, for the reason `served_citation_ids` is: a caller reads it on every
    # run, and None would make "nothing was wrong" indistinguishable from "the loop never checked".
    unverified_links: list[str] = field(default_factory=list)
    # #907: which LLM client actually ran this segment (the client's own `protocol_shape`, e.g.
    # "fake"/"openai-compatible") — recorded once per run, not per step, because the loop's client
    # never changes mid-run. None only for a client that declares no protocol_shape at all.
    protocol_shape: str | None = None
    # #975 (A1): the final registry — every URL a `[Sn]` marker could have cited, in order. Set on
    # ALL FIVE return paths (success, both degrade reasons, escalate, FAILED, every budget
    # terminal), for the same reason `served_citation_ids` is: a caller (the service persists it;
    # the engine threads it member-to-member) reads it on every run, and None would be
    # indistinguishable from "the loop never populated it".
    fetched_urls: list[str] = field(default_factory=list)


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


# #944: what a draft whose EVERY link was invented tells the member. Same posture as the citation
# corrections — it names the offending URLs, because "one of your links is wrong" is not actionable,
# and it names what the member MAY link, because a remedy it cannot perform is #692/#693 again.
_LINK_CORRECTION = (
    "Your answer links pages this run never fetched: {bad}. Link only pages you actually "
    "retrieved with your tools{allowed}. Remove or replace the others, or state the claim on your "
    "own account."
)
# Bounded for the same reason the citation ids are: every URL a model invents would otherwise be
# echoed back into its context, and a correction longer than the answer teaches it nothing.
_LINK_CORRECTION_MAX_NAMED = 5

# #944 review, HIGH-2: the correction loop itself has to be bounded. Gating it on the member HAVING
# tools (rather than on there being anything fetched to check against) meant a member whose EVERY
# tool call errored — a spent search key, a rate-limited provider, a connector outage, all real and
# already hit on `main` — had an empty fetched set, so every link is unverified with none verified:
# the all-invented branch, EVERY iteration, until the budget died. Two things fix it together, both
# below: the correction only fires when there is a real fetched set to measure against (an empty one
# ships flagged instead — the same remedy #944 already applies to a some-good/some-bad draft), and
# even with a real fetched set the correction is capped, so a member that keeps failing for its OWN
# reasons (an adversarial page instructing it to cite a list of URLs it never retrieved) cannot be
# walked through its entire iteration budget one correction at a time. #944's criterion asks for the
# link to be flagged, never for the run to be refused — a bound serves that better than no bound.
_LINK_CORRECTION_MAX = 2


def _named(urls: list[str]) -> str:
    named = urls[:_LINK_CORRECTION_MAX_NAMED]
    if len(urls) > len(named):
        named = [*named, f"and {len(urls) - len(named)} more"]
    return ", ".join(named)


# #975 ruling 7: an unknown `[Sn]` marker is corrected the SAME bounded way a raw fabrication is
# (`_LINK_CORRECTION_MAX` now covers both offences together), and the message restates the marker
# protocol rather than assuming the member remembers it — the same #692/#693 lesson every correction
# in this file already applies: an error a member cannot act on is one it can only retry blindly.
_MARKER_CORRECTION = (
    "You cited {markers}, which does not name any source you were shown. Cite only the [Sn] "
    "markers from the numbered SOURCES list you were given — never invent a number, and never "
    "write a URL yourself."
)


def _link_correction(
    unverified: list[str], fetched: list[str], unknown_markers: list[int] | None = None
) -> str:
    """The message a member reads when its draft carries a raw link this run never fetched, an
    ``[Sn]`` marker naming no source it was shown, or both (#975 ruling 7)."""
    parts: list[str] = []
    if unverified:
        allowed = f" — you fetched: {_named(fetched)}" if fetched else ""
        parts.append(_LINK_CORRECTION.format(bad=_named(unverified), allowed=allowed))
    if unknown_markers:
        markers = ", ".join(f"[S{n}]" for n in unknown_markers)
        parts.append(_MARKER_CORRECTION.format(markers=markers))
    return "\n\n".join(parts)


# ── #993/#994 regression → #1005 ruling: the declared-key guarantee is UNWRAP ONLY ────────────────
#
# Contract ruling (owner, 2026-09-10, made in the issue's own terms — "whichever you judge right"):
# a key in ``outputs_schema.required`` holds the member's answer as a string or a list of strings —
# never a nested object. Live evidence: the SAME team, run twice, shipped
# ``{"linked_summary": {"summary": [...], "artifact_refs": []}}`` once and
# ``{"linked_summary": [...], "artifact_refs": []}`` the next — the console can read one shape,
# never both.
#
# #994 first tried to guarantee that shape by force: a correction turn for anything not already a
# string/list-of-strings, then a JSON-string fallback past budget. That fired on a researcher's
# ``articles`` key — legitimately a list of RECORDS, never text — on every run. Live regression, run
# ``8c0d0bc7-c659-4379-a567-eebc8c28fa90`` ("Daily AI News Digest"): the model answered the
# correction with a top-level JSON array; `_extract_answer_object` returned ``{}`` (an array is not
# an object), the forced fallback never fired because it only runs on the correction/budget path,
# and the loop shipped SUCCEEDED with no ``articles`` key at all — the engine failed the member for
# an output contract it did not deliver. The SAME answer shipped fine before #994.
#
# Re-ruled the same day: the guarantee is UNWRAP ONLY. (a) A vacuous wrapper — ``{"summary": v}``,
# every OTHER key an empty list or ``None`` — is unwrapped SILENTLY to ``v``, no correction spent.
# (b) Anything else — a list of records, a dict carrying real data — ships UNTOUCHED. Structured
# keys are legitimate for downstream members; the console shows "not plain text" for them rather
# than the platform mangling or dropping them. (c) ``artifact_refs`` stays exempt — a bookkeeping
# list of refs, never text. The pre-existing #697 rule (a missing required key fails the member) is
# unchanged.
_ARTIFACT_REFS_KEY = "artifact_refs"


def _extract_answer_object(text: str) -> dict[str, Any]:
    """The JSON object inside a member's free-form answer, or ``{}`` when there is none. The same
    lenient "widest ``{...}``" peel the engine's ``_parse_member_object`` uses (a different service,
    duplicated rather than imported — a real model wraps its JSON in prose or a fence)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _unwrap_declared_value(value: Any) -> Any:
    """Ruling (a): a ``{"summary": v}`` wrapper — or ``{"summary": v, "artifact_refs": [...]}``
    where every OTHER key is an empty list or ``None`` — unwraps to ``v``. A wrapper carrying REAL
    extra data is left exactly as it is; the shape check downstream routes it into a correction turn
    instead of silently discarding whatever that data was."""
    if not isinstance(value, dict) or "summary" not in value:
        return value
    others_vacuous = all(
        v is None or (isinstance(v, list) and not v) for k, v in value.items() if k != "summary"
    )
    return value["summary"] if others_vacuous else value


def _normalize_declared_output(obj: dict[str, Any], declared_keys: tuple[str, ...]) -> bool:
    """Unwrap every declared key's value in place (ruling a) — the ONLY shape guarantee left after
    the #994 regression ruling. Anything that is not the vacuous ``{"summary": v}`` wrapper ships
    UNTOUCHED: a list of records, a dict carrying real data, is legitimate structured output for a
    downstream member, never coerced or dropped. Returns whether ``obj`` changed, so the caller
    re-serialises it only when something actually did. ``artifact_refs`` (ruling c) is exempt — a
    list of refs, never text."""
    changed = False
    for key in declared_keys:
        if key == _ARTIFACT_REFS_KEY or key not in obj:
            continue
        original = obj[key]
        candidate = _unwrap_declared_value(original)
        if candidate is not original:
            obj[key] = candidate
            changed = True
    return changed


# ── #961: a person's list of websites BINDS the run ──────────────────────────────────────────────
#
# Ruled 2026-09-08. A person can name the websites a run may search; until now that list reached the
# member as one line of prose inside a larger request, so it could be read, acted on, or silently
# dropped — and #951's original report is a run that dropped it while satisfying every gate the run
# has. Leaving it advisory, and checking but only warning, were both offered to the owner and
# refused.
#
# Enforced BEFORE each dispatch, never on the finished run: a terminal check was offered and refused
# because by then the unrestricted work is already paid for and the person has already waited for
# it. The refused call is not dispatched at all, so it costs no vendor quota — only the turn.
#
# What passes (ruled the same day): a NON-EMPTY SUBSET of the sites the person named. Working
# through a list one site at a time is a legitimate way to research, so demanding every site on
# every call would refuse honest searches; naming a site the person did not is refused, because
# "only use these" is what they wrote.
#
# What this can never prove: that the person named the RIGHT sites. Nothing tells `bbc.com` from
# `bbc.co.uk` — both resolve and both return real pages (#951's standing limit). The gate proves a
# restriction was applied and that it stayed inside the person's list. That is all it can prove.
_SITE_GATE_NAME = "site_restriction"
_SITE_GATE_STATUS = "unrestricted_search"
#: The operation the restriction binds. `search` reaches the live web; `fetch`/`read` return a page
#: the run already found, and gating those would refuse the member's own follow-up reads.
_SITE_RESTRICTED_OPERATION = "search"
#: The argument a search carries its restriction in — the registry's own name for it (#951).
_SITES_ARG = "sites"
# What a refused search tells the member. It NAMES the sites, because a member told only "refused"
# can do nothing but retry blindly — the #692/#693 failure, where a member handed "409" repeated the
# identical call until its budget was gone. It also says a subset is fine, so a member working
# through a long list does not read the refusal as "send all of them every time".
_SITE_CORRECTION = (
    "This run may only search the websites the person named: {named}. Call the search again with "
    "`sites` set to those addresses — one or several of them, but nothing else. Your call was not "
    "sent, so nothing has been searched yet."
)
_SITE_CORRECTION_OUTSIDE = (
    "This run may only search the websites the person named: {named}. You asked for {bad}, which "
    "{is_are} not among them. Call the search again using only the named addresses. Your call was "
    "not sent, so nothing has been searched yet."
)
#: How many offending addresses a refusal names. Bounded for the reason the citation and link
#: corrections are: every address a model invents would otherwise ride into the prompt and the step
#: detail, once per turn, unbounded.
_SITE_CORRECTION_MAX_NAMED = 5
# #961 ruling 3: the reserved key a first-party search sets when the restriction came back EMPTY.
# Popped from every result — trusted or not — so the name never reaches the model, and believed only
# from a trusted search binding. Both halves are needed, and for the reason #781 learned the hard
# way: a key a model can read is a key a model can learn to write, and a forged one here would buy a
# softer terminal on demand.
_SITES_EMPTY_KEY = "sites_yielded_nothing"
# What that flag does to the run: it FINISHES, marked incomplete, with a plain sentence — #580's
# shape, reused deliberately so no new failure mode is invented. Failing the run was offered and
# refused: the vendor's search is semantic, so a genuinely empty restricted result is rare, and
# throwing away a whole run over a rare data-absence is the wrong trade.
_SITES_EMPTY_ERROR_TYPE = "restricted_search_empty"
_SITES_EMPTY_MESSAGE = (
    "the websites this run was restricted to returned nothing for one of its searches; the member "
    "finished with what it had"
)
# The terminal for a member that never complies. It can spend its whole budget re-sending
# unrestricted searches, and that must not settle as an anonymous "did not converge" — #946's own
# lesson, where a run that failed for a nameable reason reported nothing a person could act on.
_SITE_BLOCKED_ERROR_TYPE = "site_restriction_unmet"


def _clean_site(value: object) -> str | None:
    """One entry as its bare hostname, or ``None`` when it is not a website address at all.

    The SAME shared-kernel cleaner the engine used on the person's answer and the registry uses at
    the vendor hop. That is the whole reason it lives in the kernel: a person pastes
    ``https://www.theverge.com/`` and a model calls the tool with ``theverge.com``, and a check that
    read those as different sites would refuse a search that was perfectly correct.
    """
    if not isinstance(value, str):
        return None
    try:
        cleaned = normalise_sites([value])
    except InvalidSiteError:
        return None
    return cleaned[0] if cleaned else None


def _site_violation(args: dict[str, Any], required: tuple[str, ...]) -> str | None:
    """The correction a search call earns, or ``None`` when it honours the restriction.

    Three outcomes, and the middle one is the whole issue:

    * no ``sites`` at all, an empty one, or one whose every entry is unreadable → the unrestricted
      search this gate exists to stop. An empty list is what the connector already treats as "do not
      restrict", so it is the same search under another name.
    * an entry outside the person's list → refused, and the offending entries are named.
    * a non-empty subset of the list → dispatched unchanged.
    """
    raw = args.get(_SITES_ARG)
    entries = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    asked: list[str] = []
    for entry in entries:
        # A model may put the whole list in one string ("theverge.com, bbc.co.uk"); the kernel
        # cleaner splits on commas for exactly that reason, so each part is cleaned on its own.
        for part in str(entry).split(","):
            cleaned = _clean_site(part.strip())
            if cleaned is not None and cleaned not in asked:
                asked.append(cleaned)
    named = ", ".join(required)
    if not asked:
        return _SITE_CORRECTION.format(named=named)
    outside = [site for site in asked if site not in required]
    if outside:
        shown = outside[:_SITE_CORRECTION_MAX_NAMED]
        if len(outside) > len(shown):
            shown = [*shown, f"and {len(outside) - len(shown)} more"]
        return _SITE_CORRECTION_OUTSIDE.format(
            named=named, bad=", ".join(shown), is_are="is" if len(outside) == 1 else "are"
        )
    return None


# #944 review, MEDIUM-4: NAME tokens that mark an argument as carrying a URL, not the argument's
# position or type. Matched on the SNAKE-cased key so "url", "source_url", and "pageUrl" all count
# and "query"/"content"/"document" do not — crediting the WHOLE args object let a first-party tool
# launder any string through an unrelated argument (a search whose QUERY happened to be the
# fabricated URL, an ingest whose CONTENT merely mentioned it), which is not evidence the run
# fetched anything. This narrows WHAT of a trusted call is credited; it does not touch WHICH calls
# are trusted, which is the (separately ruled, still-open per #746) binding-trust question the
# tests' own docstring already records as a deliberate limit, not an oversight.
_URL_ARG_NAME_TOKENS = frozenset({"url", "urls", "uri", "uris"})
# #944 review round 3, MEDIUM-F: a boundary BEFORE an uppercase letter that follows a lowercase/
# digit ("target" | "URL"), OR before the last uppercase letter of a run that is followed by a
# lowercase letter ("HTTP" | "Url" inside "HTTPUrl"). The original `(?<!^)(?=[A-Z])` split every
# capital individually, so a run of consecutive capitals — "URL", "URI", "targetURL", "webURL" —
# came out as single letters ("u", "r", "l") that match no token. All-caps is the ordinary REST/MCP
# convention for these two words, so this was a real miss, always in the SAFE direction (an honestly
# fetched URL went uncredited and cost a correction, never the reverse) but a bug regardless.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _url_named(key: object) -> bool:
    """Whether ``key`` (an argument name) denotes a URL. See the module note above."""
    snake = _CAMEL_BOUNDARY.sub("_", str(key)).lower()
    tokens = [t for t in _NON_ALNUM.split(snake) if t]
    return bool(_URL_ARG_NAME_TOKENS.intersection(tokens))


def _url_valued_args(args: dict[str, Any], parameters: dict[str, Any] | None) -> list[str]:
    """The URLs carried by ``args``' URL-NAMED, string-valued entries — at most one per entry.

    ``parameters`` is the dispatched ``ToolSpec``'s own JSON schema (``None`` when the call cannot
    be resolved to a spec, e.g. an already-unknown tool). ``args`` is authored ENTIRELY by the
    model and forwarded to ``dispatch`` with no schema validation in the harness (#944 review round
    3, MEDIUM-E) — a schema-blind name check alone lets a model attach ``url=`` to ANY call that
    happens to return ok (a search whose extra ``url`` argument the connector silently ignores) and
    have it credited, laundering a fabricated URL into the set that judges its own answer. Two
    independent narrowings close this without touching the still-open, separately-ruled #746
    question of WHICH bindings are trusted at all:

    * **Declared-schema gate.** When the spec's schema declares a non-empty ``properties`` object,
      an argument name absent from it was never a parameter the tool's own operation reads, no
      matter what the model called it — the model invented an out-of-schema argument and it is
      never credited. A schema with EMPTY (or missing) ``properties`` carries no information either
      way (the flat legacy hint map and a bare descriptor both produce one), so this gate is skipped
      rather than rejecting every argument of every such tool.
    * **One URL per value, strings only.** A value that is not a ``str`` (a nested object, a list)
      is never credited at all — `{"url": {"nested": {"deep": "https://…"}}}` no longer launders a
      URL through a value the tool would have to introspect to even find, and `{"urls": ["https://a",
      "https://b"]}` credits neither entry rather than crediting a whole array on one name match.
      Within one string value, only the FIRST URL found is credited — `{"url": "https://real/a
      https://smuggled/b"}` no longer credits the smuggled second address just because it shares an
      argument with a real one.
    """
    declared: set[str] | None = None
    if isinstance(parameters, dict):
        properties = parameters.get("properties")
        if isinstance(properties, dict) and properties:
            declared = set(properties)
    out: list[str] = []
    for key, value in args.items():
        if not isinstance(value, str) or not _url_named(key):
            continue
        if declared is not None and key not in declared:
            continue
        urls = extract_answer_urls(value)
        if urls:
            out.append(urls[0])
    return out


# #944 review round 3, HIGH-C: `fetched_urls` must stay a DEDUPED, BOUNDED accumulator. Round 2's
# fix (`if url not in fetched_urls: fetched_urls.append(url)`) scanned the whole list on every
# insert — quadratic in the number of distinct URLs one tool result can harvest, over text that is
# the FULL (untruncated) tool result an attacker-controlled page fully controls, on this loop's own
# async call stack with no `await` to yield it. Measured: 4.58s of blocking CPU harvesting 40k URLs
# from one 1.3MB page — the exact HIGH-1 shape, moved from `_trim` to this accumulation rather than
# removed. A parallel `set` makes membership O(1) (mirroring the `seen` set below, which already got
# this right); the cap bounds the worst case regardless of how large a single crafted page is, and
# also bounds what `check_answer_links` pays re-canonicalising the accumulated set on every LLM
# turn. Past the cap a real fetch is never un-credited — accumulation just stops taking NEW ones,
# which is the safe direction (more URLs never verifies more, only ever fewer).
# MAJOR 2 (code-reviewer, PR #977): this used to be the canonical definition, reached across a
# layer boundary by the repository and the request schema (each importing this PRIVATE name
# straight out of the loop). It now aliases the public constant in ``domain.link_provenance`` —
# the shared home for the two other layers that need the same number — kept as a module-local name
# here only so this file's own reads of the cap stay unchanged.
_MAX_FETCHED_URLS = MAX_FETCHED_URLS


def _accumulate_fetched(urls: list[str], fetched_urls: list[str], seen: set[str]) -> None:
    """Register each candidate — from ANY source: a tool harvest, a person-supplied seed, a prior
    run's carried-forward set — into the run's registry, fail-closed (#975 ruling 3, S1). An entry
    the acceptance pass's own ``_canonical`` would refuse (userinfo, over-length, non-http) is
    dropped here rather than earning a real ``[Sn]`` number just because a caller handed it in —
    ``prior_fetched_urls`` and a mined ``person_supplied_text`` are caller-supplied and trusted
    for PROVENANCE (#975 ruling 6), never for SHAPE. A refused entry is still visible to the #944
    raw-URL backstop if the member cites it anyway; it is simply never registered as a number.
    """
    for url in urls:
        if len(fetched_urls) >= _MAX_FETCHED_URLS:
            return
        if url in seen:
            continue
        if not canonical_urls([url]):
            continue
        seen.add(url)
        fetched_urls.append(url)


# ── #946 T2: an identical failing call is not dispatched a third time ─────────────────────────────
#
# A model handed an argument it can never satisfy does not learn from the failure — it re-sends the
# identical call until the tool-call budget is gone. The #946 shape: `web-research.search` offered
# the model a `provider` argument naming the search VENDOR, models filled it with a website name,
# every call failed UNKNOWN_PROVIDER, and the run ended as an anonymous "did not converge" that cost
# a whole budget of real dispatches to discover.
#
# D2 (tasks/plan.md): the bound keys on the REPEATED CALL, not on an error taxonomy. This loop
# dispatches to first-party connectors and imported MCP servers alike, and no shared "this was a
# validation error" signal crosses that boundary — an identical (tool, arguments, error) triple
# repeating is the one signal that is honest for both sides. Two dispatches are allowed: the first
# could be transient, the second proves it is not.
#
# Bounded like every other correction in this file (`_LINK_CORRECTION_MAX`, the #853 one-shot
# repair): the member is told ONCE, plainly, and if it sends the refused call again anyway the run
# settles instead of spending the rest of its budget discovering the same thing.
#
# What "bounded" covers here is the DISPATCH count, not the ledger dict that holds it — the ledger
# is the one accumulator in this file with no explicit cap, and reading the sentence above as
# covering it would be reading too much into it (#946 review round 5, LOW-7). It needs none: live
# entries are bounded by the tool-call budget, and the resume derivation reads an in-memory
# transcript that the previous segments' iteration cap already bounded. Capping it would trade a
# fail-closed guarantee for no real memory saving, because past the cap a call already proven dead
# would get a fresh allowance.
#
# THE COST THIS BOUND ACCEPTS, recorded rather than left to be rediscovered (#946 review round 5,
# MEDIUM-4, ruled by the owner 2026-09-07). D2 keys on the (tool, arguments, error) triple, and an
# error CLASS that renders a fixed sentence makes two throttled calls byte-identical:
# `search_providers._STATUS_CLASSES` maps 429 and 433 to one string, and PROVIDER_UNREACHABLE does
# the same. So a transient failure that would clear in seconds locks that exact argument set out for
# the rest of the run, and across a pause. The bound stays as it is anyway: the alternative is
# plumbing a transient signal across the connector/MCP boundary, which is precisely the boundary D2
# exists not to depend on. What does NOT stay is a note claiming a third attempt "cannot work" —
# false for a throttle, and the one part of this that cost nothing to correct.
_REPEATED_FAILURE_MAX = 2
_REPEATED_FAILURE_STATUS = "repeated_failure"
#: The stable opening of the note, and the ONLY part a transcript reader matches on. The note is
#: persisted into checkpoints, so a run paused before a reword resumes carrying the old wording; a
#: full-string comparison would miss it, record the note prose as that call's error, reset the count
#: and re-dispatch the dead call — C1 by a slower route (#946 review round 3, N4). Never change this
#: prefix without a compatibility branch for transcripts that already carry the old one.
_REPEATED_FAILURE_NOTE_PREFIX = "This exact call has already failed"
#: Reworded in #946 review round 5, MEDIUM-4, KEEPING the prefix above byte-identical. What came
#: out is a claim about the future the bound cannot support — see the accepted cost in the D2 block
#: above. What stays is what is true and what is actionable: the call failed twice the same way, it
#: was not sent again, and here are the two ways out.
_REPEATED_FAILURE_NOTE = (
    "This exact call has already failed twice with the same error, so it was not sent again. "
    "Either change the arguments — a different value, or the same call without the argument that "
    "is being rejected — or drop this call and answer with what you already have."
)
#: #834 DESIGN §E — the RESULT-axis twin of the error-axis note above, own prefix and own wording.
#: A security review on PR #1017 caught the two axes sharing one message: the error-axis sentence
#: ("this exact call... could not work") is a true root cause on the error axis, and a FALSE one on
#: the result axis — the call worked perfectly; it simply returned the same answer twice. That
#: sentence flows straight into `outcome_blockers[].message` (services/execution-engine-service),
#: so an operator can be handed a specific, WRONG root cause by the exact field this issue's other
#: half exists to make trustworthy — the same class of defect as #749's "Complete — nothing is
#: silently dropped." Never let the two axes share a message again.
#:
#: Own prefix (matched the same way `_REPEATED_FAILURE_NOTE_PREFIX` is, for the same reason: a run
#: paused before a reword resumes carrying the old wording).
_REPEATED_RESULT_NOTE_PREFIX = "This exact call has already returned"
#: THE COST THIS BOUND ACCEPTS on the result axis (mirrors the error-axis disclosure above, same
#: place a reader would look for it): a genuinely still-pending POLL that happens to answer
#: identically twice in a row — "not ready yet" from a status-check tool, say — is cut off after
#: its second try exactly like a stuck one. The bound cannot tell "stuck" from "polled too fast"
#: any more than the error axis can tell "permanently wrong" from "transient"; the fix in both
#: cases is the same two-dispatch allowance, not a smarter classifier.
_REPEATED_RESULT_NOTE = (
    "This exact call has already returned the same result twice in a row, so it was not sent "
    "again — the call worked; sending it again will not change the answer. Either use what you "
    "already have, or call something else if you need different information."
)
#: The per-member ledger: a call signature → (the content it produced, how many times in a row,
#: which axis that content came from — "error" or "result"). The axis rides in the tuple so a
#: refusal can pick the CORRECT one of the two notes above without re-deriving it from the content
#: string's shape (fragile: a legitimate result can coincidentally look error-shaped).
RepeatedFailures = dict[str, tuple[str, int, str]]


def _call_signature(name: str, args: Any) -> str:
    """A stable identity for "the same call again", independent of key ORDER in the arguments.

    Providers do not guarantee argument order between turns, so a raw dump would read a re-sent
    identical call as a new one and the bound would never fire. Unserialisable arguments fall back
    to ``repr`` rather than raising — the bound is a courtesy, never a thing that can fail a run.
    """
    try:
        rendered = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - json.dumps(default=str) is total
        rendered = repr(args)
    return f"{name}\x00{rendered}"


def _record_failure(ledger: RepeatedFailures, signature: str, content: str, kind: str) -> None:
    """Count consecutive identical outcomes (``kind`` is ``"error"`` or ``"result"``). A DIFFERENT
    outcome resets the count to one — the call is the same but the world (or the answer) changed,
    and the second occurrence has not yet proven anything. An outcome on one axis never counts
    toward the other's allowance: their content strings never collide (an error blob is always
    ``{"error": ..., "detail": ...}``; a genuine result almost never is), and the axis rides
    alongside the content in the ledger so a switch between axes is always a reset, never a
    carry."""
    prior = ledger.get(signature)
    ledger[signature] = (
        (content, prior[1] + 1, kind)
        if prior is not None and prior[0] == content and prior[2] == kind
        else (content, 1, kind)
    )


#: The receipt's TAIL, matched in full against the last line rather than searched for anywhere
#: (security audit rounds 1 and 2).
_RECEIPT_OPENER = "\n[receipt: "
_RECEIPT_TAIL = re.compile(r"source_tool_call_id=\S+ status=(?P<status>ok|error)\]")
#: The #642-era receipt, written before #944 added a status. A transcript persisted then still
#: resumes, and it is WELL FORMED — it simply predates the status. It keeps the content-shape
#: fallback; only a receipt that is neither shape is treated as corrupted.
_RECEIPT_TAIL_LEGACY = re.compile(r"source_tool_call_id=\S+\]")


def _split_receipt(raw_content: str) -> tuple[str, str | None]:
    """``(content, status)`` for one persisted ``tool``-role message.

    ``status`` is ``None`` for the two shapes that carry no status to read: no receipt at all, and
    the #642-era receipt written before #944 added one. Both still resume today and both keep the
    documented content-shape fallback. A receipt that is neither shape is CORRUPTED, and reads as
    ``"error"``: a classifier deciding whether a call succeeded must never read an unparseable
    record as a success (§3.5).

    That distinction is the round-2 finding. The receipt line interpolates the tool call's id
    unescaped, and the id is taken verbatim from the model endpoint's response — so an id carrying a
    line break and a receipt opener splits the real line in two. The anchored pattern then does not
    match, the content is cut in the wrong place, and the shape heuristic reads the truncated
    remainder as a SUCCESS. Same asset as round 1: an invented URL enters the run's provenance and a
    dead call's allowance is renewed. Not reachable by prompt alone against a well-behaved endpoint,
    which generates the id, but bring-your-own-endpoint makes an untrusted one ordinary.

    ``rpartition`` takes the LAST opener, so a forged one earlier in the body can neither win the
    classification nor truncate the content this returns.
    """
    head, separator, tail = raw_content.rpartition(_RECEIPT_OPENER)
    if not separator:
        return raw_content, None
    match = _RECEIPT_TAIL.fullmatch(tail)
    if match is not None:
        return head, match.group("status")
    if _RECEIPT_TAIL_LEGACY.fullmatch(tail):
        return head, None  # a well-formed pre-#944 receipt: no status to read, fall back on shape
    return head, "error"


def _repeated_failures_from_transcript(messages: list[Message]) -> RepeatedFailures:
    """Re-derive the ledger from an already-restored transcript, at a HITL resume.

    Without this every pause hands the member a fresh allowance for a call already proven dead,
    which is the retry loop the bound exists to stop. Mirrors ``_fetched_urls_from_transcript``:
    the checkpoint carries the transcript rather than the loop's own counters, so the resumed
    segment reads its own history back out of the messages instead of being handed one.

    NOT a byte-faithful re-derivation, and the difference is deliberate (#946 review round 5,
    LOW-6). The live path records a failure in ONE place — the dispatch ``except`` — while this
    reader counts every ``tool``-role message that classifies as failed, which also picks up three
    kinds the live path never counted: a ceiling denial, an unknown tool, and the #853 JSON-repair
    correction. Only the refusal note is filtered out explicitly, because only it can do harm.
    None of the other three can reach the refusal branch on the resumed segment:

    #834 DESIGN §E extends this to the RESULT axis too: a message carrying an EXPLICIT status
    marker (#944; every message the live dispatch path itself writes carries one) is recorded
    regardless of ok/error — the live path now records a successful dispatch's own content into
    the SAME ledger (see the call site beside ``status = "ok"``), so a resume must re-derive that
    entry the same way or it would silently re-grant two dispatches the live run had already spent.
    A message with NO explicit marker predates #944 and falls back to the original error-only
    heuristic below, unchanged — it is never read as a recordable "ok" outcome, which only costs a
    resumed pre-#944 transcript two extra allowed dispatches before the bound re-engages.

    * a ceiling denial and an unknown tool are decided from the policy envelope and the tool set,
      both fixed for the run, so their own branches short-circuit ahead of the refusal check every
      time that signature comes round again;
    * the JSON repair is one-shot per RUN (``json_repair_used`` rides the checkpoint), so it can
      contribute at most one entry — below ``_REPEATED_FAILURE_MAX`` — and its prose differs from
      any dispatch error, so mixing it with a real failure resets the count rather than adding to
      it.

    Filtering them here would therefore be three more content-sniffing prefix checks buying nothing,
    and each one a new way for the reader to disagree with the live path.
    """
    args_by_call_id: dict[str, dict[str, Any]] = {}
    names_by_call_id: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if isinstance(call_id, str):
                args_by_call_id[call_id] = call.get("args") or {}
                if isinstance(call.get("name"), str):
                    names_by_call_id[call_id] = call["name"]

    ledger: RepeatedFailures = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        raw_content = message.get("content")
        call_id = message.get("tool_call_id")
        if not isinstance(raw_content, str) or not isinstance(call_id, str):
            continue
        content, explicit_status = _split_receipt(raw_content)
        # #834 DESIGN §E: the ledger now tracks BOTH axes — an identical ERROR (unchanged, #946
        # T2) and an identical successful RESULT (new). A message with no explicit status marker
        # predates #944 and carries no reliable "this was ok" signal to re-derive the result axis
        # from, so it falls back to the pre-#834 error-only heuristic (`_is_failed_tool_content`)
        # exactly as it always has — an "ok" message from that era is simply never recorded here,
        # which only means a HITL resume on a pre-#944 transcript re-allows two more identical
        # results before the bound re-engages, never a correctness break.
        recordable = explicit_status is not None or _is_failed_tool_content(content)
        if not recordable:
            continue
        # #946 review round 2, C1: a REFUSAL is not a failure of the call — nothing was dispatched,
        # so there is no error to count. It is written with `status=error` so the fetched-URL reader
        # never credits it (see the receipt line in `_run_tool_calls`), which brings it here too.
        # Counting it would record the note prose as that call's error; the note differs from the
        # real error, `_record_failure` would reset the count to one, and the resumed run would
        # re-dispatch the very call the bound had already proven dead. #834 DESIGN §E: the SAME
        # reasoning holds for the result-axis note — both prefixes are refusals, neither is a real
        # outcome to count.
        stripped = content.strip()
        if stripped.startswith(_REPEATED_FAILURE_NOTE_PREFIX) or stripped.startswith(
            _REPEATED_RESULT_NOTE_PREFIX
        ):
            continue
        name = names_by_call_id.get(call_id)
        if name is None:
            message_name = message.get("name")
            if not isinstance(message_name, str):
                continue
            name = message_name
        # #834 DESIGN §E: the axis this message came from — an explicit `status=ok` marker means a
        # genuine dispatched result; anything else (an explicit `status=error`, or the legacy
        # content-shape fallback below, which only ever recognised failures) is the error axis,
        # unchanged from before this extension.
        kind = "result" if explicit_status == "ok" else "error"
        _record_failure(
            ledger, _call_signature(name, args_by_call_id.get(call_id, {})), content, kind
        )
    return ledger


# #944 review round 3, LOW-H/LOW-I: the explicit marker a `tool`-role message's receipt line carries
# (``status=ok``/``status=error``), so a transcript reader can classify a call the same way the live
# path did rather than GUESSING from the shape of its content. A guess disagrees with the live
# classification in both directions: an ``{"error": …}`` object that is a connector's genuine OK
# payload (LOW-I) reads as failed on resume though it was credited live, and the #853 JSON-repair
# correction (prose, never dispatched, always excluded live) has no JSON shape for the guess to
# recognise as failed, so its message's URL-named arguments — never credited live — WERE credited on
# resume (LOW-H). The marker is optional on read: a checkpoint fixture or an older persisted
# transcript that never carried one falls back to the previous content-shape heuristic.
#
# Security audit finding 1 (HIGH): ANCHORED to the end of the message, and matched with `fullmatch`
# on the last line rather than searched for anywhere. The marker is plain text inside a message
# whose body is `json.dumps({"error": ..., "detail": str(exc)})`, and a connector's error routinely
# echoes the caller-supplied value that caused it. `json.dumps` escapes quotes and newlines and
# nothing else — not `[`, `]`, `=`, `:` or spaces — so a model can put a whole fake receipt line
# inside a tool ARGUMENT and have it persisted into the transcript verbatim.
#
# Searching found the FIRST such marker while the platform appends the genuine one LAST. A failed
# call therefore read back as a SUCCESS on a resume, and both readers of this marker were fooled at
# once: `_fetched_urls_from_transcript` credited a URL the run never fetched (the #944 forgery path,
# reopened), and `_repeated_failures_from_transcript` recorded nothing, renewing the allowance for a
# call already proven dead. A model can write the syntax; it cannot write past the platform's own
# append, so the end is the one position that is not forgeable.
def _explicit_tool_status(raw_content: str) -> str | None:
    return _split_receipt(raw_content)[1]


def _fetched_urls_from_transcript(
    messages: list[Message], by_name: dict[str, ToolSpec], redactors: list[re.Pattern[str]]
) -> list[str]:
    """Re-derive the run's fetched-URL set from an already-restored transcript (#944 review,
    MEDIUM-3). Used only at a HITL resume, where the loop's own ``fetched_urls`` accumulator would
    otherwise restart empty and correct a member for links it genuinely fetched before the pause.

    Mirrors the live dispatch path as closely as a persisted transcript allows: an explicit
    ``status`` marker on the message (see ``_explicit_tool_status``) classifies ok/failed when
    present; a message written before that marker existed falls back to the same content-shape
    heuristic this function always used. A failed call credits nothing, in either direction.

    #944 review round 3, HIGH-D: the args dump is redacted before harvesting, matching the live
    path (``tool_use.py`` redacts ``url_args`` before extraction). The checkpoint's assistant
    ``tool_calls`` carry the model's RAW, unredacted arguments (only ``content``/``last_text`` are
    stored redacted) — a credential-bearing URL argument restored from a paused run must not enter
    ``fetched_urls`` unredacted, because that list is echoed verbatim into a later correction
    message if one fires.
    """
    args_by_call_id: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if isinstance(call_id, str):
                args_by_call_id[call_id] = call.get("args") or {}

    out: list[str] = []
    seen: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        raw_content = message.get("content")
        if not isinstance(raw_content, str):
            continue
        content, explicit_status = _split_receipt(raw_content)
        failed = (
            explicit_status == "error"
            if explicit_status is not None
            else _is_failed_tool_content(content)
        )
        if failed:
            continue
        call_id = message.get("tool_call_id")
        args = args_by_call_id.get(call_id, {}) if isinstance(call_id, str) else {}
        message_name = message.get("name")
        spec = by_name.get(message_name) if isinstance(message_name, str) else None
        arg_urls = [
            _redact(url, redactors)
            for url in _url_valued_args(args, spec.parameters if spec else None)
        ]
        # security review 5155075040 (M2): `arg_urls` first, mirroring the live dispatch path (see
        # the matching comment there) — the opened URL outranks the page's own body URLs here too.
        _accumulate_fetched(arg_urls + extract_answer_urls(content), out, seen)
    return out


_FAILED_TOOL_CONTENT_KEYS = frozenset({"error", "detail", "did_you_mean", "available_tools"})


def _is_failed_tool_content(content: str) -> bool:
    try:
        data = json.loads(content)
    except ValueError:
        return False  # not JSON at all (e.g. the #853 repair prompt) — never the error shape
    return (
        isinstance(data, dict) and "error" in data and set(data.keys()) <= _FAILED_TOOL_CONTENT_KEYS
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


# ── #975: cite-by-reference — the model is SHOWN its numbers before it can cite them ─────────────
#
# The protocol paragraph every member reads (ruling 5: tools or not), telling it the mechanism
# exists and how to use it. Prose alone can never be the enforcement (a check implemented as a model
# instruction is a check that can be talked out of) — this is the model's half of the contract; the
# platform's half is `expand_source_markers`/`check_answer_links`/`strip_unverified_links` below,
# which never trust that the model read this paragraph.
_CITATION_MARKER_PROTOCOL = (
    "\n\nWhen you cite a source, cite it as a numbered marker like [S1] naming an entry from the "
    "numbered SOURCES list you are shown — never write a URL yourself, from memory or otherwise. A "
    "number you were not shown names nothing; do not invent one."
)

#: How many newly-registered entries one tool result's SOURCES block names (A7). Uncapped
#: registration would balloon every later turn's prompt in proportion to a single page's harvest;
#: registration itself is NOT capped by this — an entry past the cap is still a real registry
#: number, just never shown a line, and stays verifiable via the #944 raw-URL match.
_SOURCES_PER_CALL_MAX = 20

# security review 5155075040 (M3), PoC Area 2: the seed block shown in the FIRST user turn used to
# be built from the WHOLE seed list — 500 `prior_fetched_urls` put 500 lines into that very first
# turn. Same cap, same name pattern as the per-call block above: display only, never registration
# — `seed_urls` itself (and therefore `fetched_urls`) still carries every seed, and a raw URL
# naming an entry past this displayed cap still verifies via the #944 raw-URL backstop.
_SOURCES_SEED_MAX = _SOURCES_PER_CALL_MAX


def _sources_block(entries: list[str], *, start: int) -> str:
    """``SOURCES:`` followed by one ``[Sn] url`` line per entry, numbered from ``start`` (S6: no
    other text from the tool result — no title, no snippet — ever rides inside this block)."""
    lines = "\n".join(f"[S{start + i}] {url}" for i, url in enumerate(entries))
    return f"SOURCES:\n{lines}"


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
    web_search_bindings: frozenset[str] | None = None,
    prior_served_citation_ids: Collection[str] | None = None,
    prior_fetched_urls: Collection[str] | None = None,
    person_supplied_text: str | None = None,
) -> LoopResult:
    by_name = {s.name: s for s in tool_specs}
    trusted_citation_bindings = (
        _DEFAULT_CITATION_BINDINGS if citation_bindings is None else citation_bindings
    )
    trusted_data_absent_bindings = (
        _DEFAULT_DATA_ABSENT_BINDINGS if data_absent_bindings is None else data_absent_bindings
    )
    # #961: which bindings the site-restriction gate fires on. NO default set, unlike the two above:
    # a manifest alias proves nothing, and defaulting to a literal name would let an author dodge
    # the gate by renaming their tool AND fire it on an unrelated tool that borrowed the name. The
    # service always passes the registry's own resolution (`TrustedBindings.web_search`).
    trusted_web_search_bindings = web_search_bindings or frozenset()
    # #975 rulings 3/4/6: the registry's SEEDS — a URL the PERSON supplied (task text, intake
    # answers), then whatever a prior segment of this run already fetched (`prior_fetched_urls`,
    # the `prior_served_citation_ids` pattern). Both are caller-vouched provenance, trusted exactly
    # as `input_text` already is, and both pass the SAME registration gate (S1) every other source
    # does — computed here, before the resume/fresh split below, because the fresh branch shows the
    # seeds to the model in its very first turn.
    seed_urls: list[str] = []
    seed_seen: set[str] = set()
    _accumulate_fetched(extract_answer_urls(person_supplied_text or ""), seed_urls, seed_seen)
    _accumulate_fetched(list(prior_fetched_urls or []), seed_urls, seed_seen)
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
        # #975 (ruling 5, A2/T2): the seeded entries are shown as one platform-authored numbered
        # SOURCES block in the FIRST user turn — a member cannot cite a number it is never shown,
        # and the protocol applies to every member, tools or not. A resume never re-shows this: the
        # restored transcript already carries whatever the member saw before the pause, and A3
        # keeps the numbering stable by seeding `prior_fetched_urls` first, not by re-announcing it.
        if seed_urls:
            # M3: the DISPLAYED block is capped; `seed_urls` itself (below, `fetched_urls =
            # list(seed_urls)`) still carries every seed, uncapped.
            block = _sources_block(seed_urls[:_SOURCES_SEED_MAX], start=1)
            messages[0] = {**messages[0], "content": f"{user_input}\n\n{block}"}
    # #975: every member reads the marker protocol, tools or not (ruling 5) — appended once, so
    # every turn's `system=` (the loop passes the same string on every iteration) carries it. Only
    # when the registry could ever be non-empty (a seed now, or a tool that could harvest one
    # later): a member with neither has nothing it could ever cite, so the instruction would be
    # dead prose — and leaving `system` untouched here is what keeps a genuinely tool-less,
    # seed-less run's context byte-identical to before #975 (the #513 back-compat property).
    if seed_urls or tool_specs:
        system = f"{system}{_CITATION_MARKER_PROTOCOL}"
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
    # #961 ruling 3: set when a RESTRICTED search came back with nothing on the named sites. Like
    # `retrieval_empty` it degrades the run to a flagged PARTIAL rather than failing it, and for the
    # same reason — this is data-absence, not a fault. A fresh flag on a HITL resume, matching
    # `retrieval_empty`'s own accepted fidelity gap.
    restricted_search_empty = False
    # #961 ruling 2: the last refusal the site gate issued, or None if it never fired. It is what
    # turns a spent budget into a TYPED terminal rather than an anonymous "did not converge" —
    # #946's lesson, where a run that failed for a nameable reason said nothing a person could use.
    site_blocked: str | None = None
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
    # #944: every http(s) URL this run's own tool calls really returned or really read, first-seen
    # order, deduplicated. The loop is the only place that can build this — a persisted step's
    # detail is truncated, so the full tool result exists here and nowhere else afterwards.
    #
    # #944 review, MEDIUM-3: on a HITL resume this cannot start empty the way `retrieval_empty`
    # does. A link the member genuinely fetched BEFORE the pause is real provenance whether or not
    # the run was ever paused — starting fresh here corrects the member for citing honestly. Unlike
    # the citation gate's `prior_served_citation_ids` (a service-layer parameter sourced from a
    # persisted column), this needs neither: the tool-role messages the member's calls produced are
    # already sitting in the restored transcript, so the resumed segment re-derives its own fetched
    # set from `resume_state.messages` rather than being handed one.
    #
    # #975 (A3): `seed_urls` (person-supplied, then `prior_fetched_urls`) goes in FIRST, so a number
    # a member is shown after a resume never points at a different URL than it did before the pause
    # — the resumed transcript's own re-derivation is unioned in AFTER, never ahead of the seed.
    fetched_urls: list[str] = list(seed_urls)
    fetched_urls_seen: set[str] = set(seed_seen)
    if resume_state is not None:
        _accumulate_fetched(
            _fetched_urls_from_transcript(resume_state.messages, by_name, redactors),
            fetched_urls,
            fetched_urls_seen,
        )
    # #946 T2: the repeated-failure ledger — see `_REPEATED_FAILURE_MAX` above. Re-derived from the
    # restored transcript on a resume, for the same reason `fetched_urls` is: a member that already
    # proved a call dead must not get a fresh allowance for it just because the run was paused.
    repeated_failures: RepeatedFailures = (
        _repeated_failures_from_transcript(resume_state.messages)
        if resume_state is not None
        else {}
    )
    # The tools behind the calls refused as unfixable repeats, named the way the rest of the run
    # names them (`binding.operation`). Only the tool: a call signature carries the model's raw
    # arguments, which hold whatever the model put there, and the terminal below is person-facing.
    repeated_failure_names: list[str] = []
    # #944 review, HIGH-2 fix B: how many corrections THIS run has already spent — bounded by
    # `_LINK_CORRECTION_MAX`, see its docstring. A fresh count on a resume, matching `nudged`: the
    # checkpoint carries the transcript, not this counter, so a run that had already used one
    # correction before a HITL pause gets its full allowance again after — a known, accepted minor
    # generosity (never a cost — degrade-not-crash), not a cascade.
    link_corrections_used = 0
    # #944: the unverified URLs of the last draft the link check sent BACK to the member, or None if
    # it never fired. Like `citation_blocked` it is what turns a spent budget into a typed terminal
    # rather than an anonymous "did not converge", and it is cleared wherever that one is.
    links_blocked: list[str] | None = None
    # #975 ruling 7: an unknown `[Sn]` marker is a SEPARATE offence from a raw unverified URL — it
    # names no URL at all, so it cannot live in `links_blocked`'s list. Tracked only so the terminal
    # fallback below (`if link_offense_blocked:`) still recognises a draft blocked on a marker-only
    # offence, exactly as it already does for a raw-URL one.
    link_offense_blocked = False
    # #944: the unverified URLs of an answer that was ACCEPTED carrying some. Distinct from
    # `links_blocked` on purpose: one is a draft that was rejected, the other is what shipped.
    unverified_links: list[str] = []
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

    def _shipped(text: str) -> str:
        """#975 (A1/T3): the SAME expand-then-strip pass every terminal's output goes through — a
        budget-exhausted PARTIAL, an escalation, a failure, must never ship a raw fabrication or a
        literal ``[Sn]`` naming nothing, only because it happened to end the run mid-correction
        rather than at the ordinary acceptance point below. Idempotent on text this loop already
        shipped through the same pass (T1's pinned property), so calling it again here costs
        nothing when the acceptance block already finished the job.
        """
        expanded, _ = expand_source_markers(text, fetched_urls)
        check = check_answer_links(expanded, fetched_urls)
        # security review 5155075040 (M1): strip runs UNCONDITIONALLY, not only `if
        # check.unverified` — a link whose TARGET verifies but whose LABEL carries a fabricated URL
        # never appears in `check.unverified` at all (a label is never scanned for the answer's own
        # URL list), so gating the call on it skipped exactly the shape M1 exists to catch. Passing
        # `fetched=fetched_urls` is what lets the label be judged on its own terms.
        stripped = strip_unverified_links(expanded, check.unverified, fetched=fetched_urls)
        # #993/#994: a terminal that never reaches the ordinary acceptance block (an escalation, a
        # degrade, a failure) still gets the unwrap-only guarantee. `_extract_answer_object`
        # returns `{}` when `stripped` does not parse as a JSON object (ruling 4) — the `if obj:`
        # guard below is what keeps that from ever discarding a previously parsed object:
        # `stripped` ships exactly as it already stood, never blanked.
        if policy.declared_output_keys:
            obj = _extract_answer_object(stripped)
            if obj:
                changed = _normalize_declared_output(obj, policy.declared_output_keys)
                if changed:
                    stripped = json.dumps(obj)
        return stripped

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
            output=_shipped(last_text) if last_text else None,
            steps=steps,
            iterations=iterations,
            total_tokens=tokens_used,
            input_tokens=input_used,
            output_tokens=output_used,
            error_type=reason,
            error_message=message,
            checkpoint=checkpoint,
            served_citation_ids=list(served_citation_ids),
            fetched_urls=list(fetched_urls),
            protocol_shape=protocol_shape,
        )

    def _degrade(name: str, reason: str, message: str, iterations: int) -> LoopResult:
        # #587: on_exhaustion=degrade — FINISH with the best-effort last_text as a flagged PARTIAL
        # (typed reason), never a resumable checkpoint. The single degrade primitive #580 reuses.
        steps.append(LoopStep(len(steps), StepKind.GATE, name, reason, message))
        return LoopResult(
            status=HarnessStatus.PARTIAL,
            output=_shipped(last_text) if last_text else None,
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
            # #944: the degraded output is the last draft, so whatever was wrong with its links is
            # what a reader of THIS answer needs warning about — the blocked set when the run ran
            # out of correction budget, the accepted set otherwise.
            unverified_links=list(links_blocked or unverified_links),
            fetched_urls=list(fetched_urls),
            protocol_shape=protocol_shape,
        )

    def _budget_gate(name: str, reason: str, message: str, iterations: int) -> LoopResult:
        # #587: a BUDGET gate honours on_exhaustion — escalate (today) or degrade (PARTIAL). A HITL
        # pause is NOT routed here (it always _escalate-with-checkpoint); only budget breaches.
        # #580: a member that ran out of ITERATIONS while blocked by a data-absent retrieval churned
        # on missing data — degrade (PARTIAL) regardless of on_exhaustion, so a from-scratch/empty-
        # graph member never hard-fails the team on missing data (ADR-021). A token/wall/tool-call
        # overrun is real work, NOT data-absence churn → it still honours on_exhaustion (escalate).
        if site_blocked is not None and reason == "iteration_cap":
            # #961: the member spent its whole budget re-sending searches that ignored the person's
            # list. That is a nameable failure and must not settle as an anonymous "did not
            # converge" — #946's own lesson. ESCALATED rather than degraded, unlike the empty-sites
            # case above: nothing was searched, so there is no thin-but-honest answer to ship, and
            # a member that would not honour the restriction is not a data-absence.
            return _escalate(
                _SITE_GATE_NAME,
                _SITE_BLOCKED_ERROR_TYPE,
                "the member never restricted its search to the websites the person named",
                iterations,
            )
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
        nonlocal restricted_search_empty, site_blocked
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
            # #961 rulings 1+2: a person's list of websites BINDS this run, and the check happens
            # HERE — before the tool-call budget gate, and before any dispatch — so the
            # unrestricted search is never paid for. See the module note above for what passes.
            #
            # Every clause narrows: only when the run HAS a restriction (most runs do not and are
            # untouched), only for a binding the REGISTRY confirmed is the platform's own web search
            # (an alias proves nothing — #780), and only for the `search` operation (`fetch`/`read`
            # return a page the run already found).
            if (
                spec is not None
                and policy.required_sites
                and spec.binding in trusted_web_search_bindings
                and spec.operation == _SITE_RESTRICTED_OPERATION
            ):
                violation = _site_violation(tc["args"], policy.required_sites)
                if violation is not None:
                    site_blocked = violation
                    correction = _redact(violation, redactors)
                    # A `tool` turn, not a bare `user` one: the assistant turn already carries this
                    # tool_call_id and a provider transcript with a call and no matching result is
                    # malformed. The explicit `status=error` marker keeps a HITL resume's
                    # content-shape heuristic from crediting the call's arguments as real
                    # provenance — the #853 branch learned that one the hard way (#944 LOW-H).
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": tc["name"],
                            "content": f"{correction}\n[receipt: source_tool_call_id={tc['id']} "
                            "status=error]",
                        }
                    )
                    steps.append(
                        LoopStep(
                            len(steps),
                            StepKind.GATE,
                            _SITE_GATE_NAME,
                            _SITE_GATE_STATUS,
                            _truncate(violation),
                            tool_call_id=tc["id"],
                        )
                    )
                    continue  # never dispatched — no vendor call, no quota, nothing searched
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
                    #
                    # #944 review round 3, LOW-H: this call is never dispatched (`continue`s below
                    # before the harvest block), so it credits nothing live. Without an explicit
                    # `status=error` marker a HITL resume's content-shape heuristic does not
                    # recognise this prose as failed (it is not the JSON error shape at all) and
                    # DID credit its URL-named arguments — a document `source_url` the model
                    # supplied but the run never actually fetched, credited only on resume.
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": tc["name"],
                            "content": f"{correction}\n[receipt: source_tool_call_id={tc['id']} "
                            "status=error]",
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
            # Hoisted above the chain (#946 review round 5, LOW-5). Two of the three branches below
            # need it, and computing it twice inside the refusal condition read as if the two calls
            # might differ. Binding it in only some branches was the real hazard: a later edit that
            # read `signature` after the chain would have silently got the PREVIOUS iteration's
            # value, which is a wrong-call bug that nothing here would have surfaced.
            signature = _call_signature(tc["name"], tc["args"])
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
                step_detail = content
            elif (repeated_failures.get(signature) or ("", 0, "error"))[1] >= _REPEATED_FAILURE_MAX:
                # #946 T2: this exact call already failed twice the same way. It is NOT dispatched —
                # the member is handed the note instead, so the turn still gets its tool-role reply
                # (a provider REJECTS a tool_call with no answering message, so skipping the reply
                # would corrupt the transcript) but costs no real call. Deliberately not charged to
                # `tool_calls_made`: nothing was executed.
                #
                # Ruled by the owner 2026-09-07: the run KEEPS GOING. Refusing the call frees the
                # member's remaining turns to fix the value or answer without that tool, which is
                # the outcome worth having; stopping the run here would deny a member that was one
                # turn from recovering. The refusal repeats for as long as the member does, and
                # every refused attempt is recorded as its own step — a model tool call that
                # vanished from the trace is a run an operator cannot reconstruct.
                #
                # The cost the owner accepted with that ruling: a member that never adapts still
                # spends its whole ITERATION budget. So the terminal at the bottom of this function
                # NAMES the repeated call rather than reporting an anonymous "did not converge" —
                # the budget is spent either way, but the operator learns why.
                step_name = f"{spec.binding}.{spec.operation}"
                # #946 review round 2, C4: `binding.operation` is how the step trace and the run
                # page already name this tool. Reporting the provider-facing function name here as
                # well showed one tool under two spellings in one run, and the person-facing half
                # was the less readable of the two.
                if step_name not in repeated_failure_names:
                    repeated_failure_names.append(step_name)
                # Not passed through `_redact`, unlike every sibling branch, and deliberately:
                # this is platform-authored text with nothing in it to redact, and the stored form
                # has to stay byte-identical for the transcript reader above to recognise it.
                #
                # #834 DESIGN §E / security review on PR #1017: the ledger entry's own recorded
                # `kind` ("error" or "result") picks the note — NEVER the error-axis wording on a
                # result-axis refusal. The two are not interchangeable: "could not work" is TRUE for
                # an error and FALSE for a call that dispatched fine and simply repeated its answer.
                # This message flows straight into `outcome_blockers[].message`
                # (execution-engine-service) on a critical member, so a wrong root cause here is
                # handed to the exact field #834's other half exists to make trustworthy.
                repeated_kind = (repeated_failures.get(signature) or ("", 0, "error"))[2]
                content = (
                    _REPEATED_FAILURE_NOTE if repeated_kind == "error" else _REPEATED_RESULT_NOTE
                )
                status = _REPEATED_FAILURE_STATUS
                # #946 review round 3, N1: the STEP records why the call failed; the MESSAGE
                # carries the advice. They have different readers and they must not be the same
                # string.
                #
                # The note is written for the model — "change the arguments", "drop this call". The
                # step trace is what a run's failure text is built from (`envelope`'s grounding
                # check excerpts the last non-ok step's detail), so a refusal step carrying the note
                # put instructions for the model in front of a PERSON, and — worse — replaced the
                # real cause. The run page then never said the search vendor was wrong, which is the
                # one thing #946 exists to make it say: this half of the fix was cancelling out the
                # other half. The original error is still in the ledger, so recording it here costs
                # nothing. Precedent for a step detail diverging from its message: the #853
                # JSON-repair branch records the parse error while its message carries the
                # correction prose.
                step_detail = (repeated_failures.get(signature) or ("", 0, "error"))[0] or content
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
                    # #961 ruling 3: the same treatment for a RESTRICTED search that came back with
                    # nothing on the named sites. Popped from every result so the model never learns
                    # the key is live; believed only from a search binding the registry confirmed,
                    # so it cannot be forged through an echo-shaped tool (#781). The member already
                    # has the connector's own sentence in `note` — this flag is for the runtime, and
                    # the two say the same thing to different readers.
                    if isinstance(result, dict):
                        sites_empty = result.pop(_SITES_EMPTY_KEY, False)
                        if sites_empty and spec.binding in trusted_web_search_bindings:
                            restricted_search_empty = True
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
                    # #834 DESIGN §E: extend the SAME ledger to an identical (tool, arguments,
                    # RESULT) triple, not only an identical ERROR — `manifest-validate` returns
                    # SUCCESSFULLY with `would_block: true`, an unchanged verdict rather than an
                    # error, so the pre-#834 error-only ledger never fired for a member benignly
                    # re-validating the same already-blocked draft. `_record_failure`'s own
                    # "a DIFFERENT [outcome] resets the count to one" semantics already separate a
                    # genuinely evolving result from a stuck one, and already separate an error
                    # outcome from a result outcome (their content strings never collide) — no
                    # second mechanism, no second ledger. kind="result" so a refusal built off THIS
                    # entry reaches for `_REPEATED_RESULT_NOTE`, never the error-axis wording.
                    _record_failure(repeated_failures, signature, content, "result")
                except Exception as exc:  # noqa: BLE001 — feed the error back so the model can adapt
                    content = _redact(
                        json.dumps({"error": type(exc).__name__, "detail": str(exc)}), redactors
                    )
                    status = "error"
                    # #946 T2: count it against this exact call. A DIFFERENT error resets the count
                    # — see `_record_failure`. Recorded on the redacted content, so the ledger key
                    # is the same string a resumed run reads back out of the transcript.
                    _record_failure(repeated_failures, signature, content, "error")
                finally:
                    tool_ended = datetime.now(UTC)
                step_detail = content
            # #944: the run's own link provenance. An OK call contributes the URLs in its RESULT
            # and the URLs in the URL-NAMED ARGUMENTS the model passed it. The arguments count for a
            # real reason: `web-research.read(url=X)` returning ok is the strongest evidence we will
            # ever have that X was fetched, and a read's result is page text that need not repeat
            # its own URL. An ERRORED call contributes nothing in either direction — a 404 on a
            # composed URL is the check working, and crediting it would let a member legitimise any
            # URL by calling a tool with it and ignoring the error.
            #
            # #944 review, MEDIUM-4 / round 3 MEDIUM-E: only URL-NAMED arguments are scanned, and
            # only ones the dispatched tool's OWN schema declares (when it declares any) — see
            # `_url_valued_args`. Dumping the WHOLE args object credited a search whose QUERY
            # happened to be the fabricated URL, or an ingest whose CONTENT merely mentioned it:
            # neither call is evidence the run fetched anything, and both are the obvious next move
            # for a model told (by `_link_correction` below) which URLs it must stop citing. Each
            # credited URL is redacted before harvesting, matching `content` two lines above it — a
            # credential-bearing URL in a tool argument must not enter `fetched_urls` (and,
            # downstream, a persisted trace or a correction message) unredacted.
            if status == "ok":
                arg_urls = [
                    _redact(url, redactors)
                    for url in _url_valued_args(tc["args"], spec.parameters if spec else None)
                ]
                # #944 review round 3, HIGH-C: capped, set-backed accumulation — see
                # `_accumulate_fetched` above. Replaces the round-2 `if url not in fetched_urls:
                # fetched_urls.append(url)` linear scan, which was quadratic in the number of
                # distinct URLs one tool result could harvest.
                registered_before = len(fetched_urls)
                # security review 5155075040 (M2): `arg_urls` FIRST — `web-research.read(url=X)`
                # returning ok is the strongest evidence this run will ever have that X was fetched,
                # and registering the page's own (potentially dozens of) body URLs ahead of it meant
                # the URL the member actually OPENED was never `[S1]`.
                _accumulate_fetched(
                    arg_urls + extract_answer_urls(content), fetched_urls, fetched_urls_seen
                )
                # #975 (A6/A7): show the member the numbers it just earned. Every entry registered
                # THIS call gets a real `[Sn]` — but only the first `_SOURCES_PER_CALL_MAX` earn a
                # displayed line (first-registered win); registration itself is uncapped by this,
                # so an entry past the cap is still a real number, verifiable via the raw-URL match.
                # Appended to `content` — AFTER it was captured into `step_detail` above, so the
                # persisted trace is unaffected — and BEFORE the receipt line is built below (A6):
                # `_split_receipt` classifies anything after the receipt as corruption.
                newly_registered = fetched_urls[registered_before:]
                if newly_registered:
                    shown = newly_registered[:_SOURCES_PER_CALL_MAX]
                    block = _sources_block(shown, start=registered_before + 1)
                    content = f"{content}\n{block}"
            # #642: show the receipt id INSIDE the tool result the model reads. The provider's
            # `tool_call_id` field is transport metadata the model never sees, so a member asked to
            # cite its receipts could only guess — real models cited the tool NAME, a chunk id, or
            # "1", and were failed for it despite having really made the call. The visible receipt
            # line is what makes the grounding contract satisfiable rather than a trap.
            #
            # #944 review round 3, LOW-H/LOW-I: `status=` is the explicit marker a HITL-resumed
            # transcript reads back via `_explicit_tool_status`, rather than guessing failed/ok from
            # the shape of `content` (a guess that disagreed with this line's own classification in
            # both directions — see that function's docstring).
            #
            # The receipt vocabulary is `ok`/`error` ONLY. The refusal has its own STEP status,
            # which is right for the trace an operator reads, but a third value here is invisible to
            # every reader of a persisted transcript — and a call it cannot classify was read as a
            # SUCCESS, crediting a URL the run never fetched. See `_split_receipt`.
            receipt_status = "error" if status == _REPEATED_FAILURE_STATUS else status
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "content": (
                        f"{content}\n[receipt: source_tool_call_id={tc['id']} "
                        f"status={receipt_status}]"
                    ),
                }
            )
            steps.append(
                LoopStep(
                    len(steps),
                    StepKind.TOOL,
                    step_name,
                    status,
                    _truncate(step_detail),
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
                output=_shipped(last_text) if last_text else None,
                steps=steps,
                iterations=iteration,
                total_tokens=tokens_used,
                input_tokens=input_used,
                output_tokens=output_used,
                error_type=type(exc).__name__,
                error_message=str(exc),
                served_citation_ids=list(served_citation_ids),
                fetched_urls=list(fetched_urls),
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
            # #975: cite-by-reference, in the same place and for the same reason the citation gate
            # runs above — a draft sent back has to go back to the MEMBER, so this runs before the
            # answer is accepted rather than after this function returns. Applies to EVERY member,
            # tools or not (ruling 5) — the `if tool_specs:` gate #944 had is gone: the whole point
            # of #975 is that a tool-less `linker` member, whose entire job is attaching sources,
            # no longer gets a free pass to fabricate.
            #
            # 1. Expand `[Sn]` markers against the registry — a marker naming no usable entry is
            #    removed and its number reported in `unknown_markers` (never shipped: T3).
            # 2. Run the #944 raw-URL backstop on what remains. A URL expansion just inserted is, by
            #    construction, a registry entry, so it verifies trivially — never re-flagged (T10).
            # 3. Either offence, with a REAL registry to measure against, spends a correction turn
            #    — ruling 7 supersedes #944's "only when EVERY link is invented": now any offence
            #    does, bounded the same way (`_LINK_CORRECTION_MAX`). An EMPTY registry never loops
            #    (ruling 3, #944's HIGH-2 fix kept): there is nothing to correct against, so the
            #    draft ships — stripped — on the very first pass.
            # 4. Past the bound, or with an empty registry: strip the survivors, record
            #    `unverified_links`, record the existing GATE step. The shipped text is
            #    post-expansion, post-strip — never the raw draft.
            expanded_text, unknown_markers = expand_source_markers(last_text, fetched_urls)
            link_check = check_answer_links(expanded_text, fetched_urls)
            offending = bool(unknown_markers) or bool(link_check.unverified)
            if offending and fetched_urls and link_corrections_used < _LINK_CORRECTION_MAX:
                link_corrections_used += 1
                links_blocked = list(link_check.unverified)
                link_offense_blocked = True
                messages.append({"role": "assistant", "content": last_text})
                messages.append(
                    {
                        "role": "user",
                        "content": _link_correction(
                            link_check.unverified, fetched_urls, unknown_markers
                        ),
                    }
                )
                steps.append(
                    LoopStep(
                        len(steps),
                        StepKind.GATE,
                        LINK_GATE_NAME,
                        LINK_CORRECTION_STATUS,
                        _truncate(
                            json.dumps({"unverified": links_blocked, "unknown": unknown_markers})
                        ),
                    )
                )
                continue
            unverified_links = list(link_check.unverified)
            # security review 5155075040 (M1): unconditional, matching `_shipped` above — a
            # verified-target/fabricated-label link never populates `unverified_links` (the label is
            # never part of the answer's own URL list), so gating this call on it would ship that
            # label untouched. `fetched=fetched_urls` lets the label be judged independently.
            last_text = strip_unverified_links(
                expanded_text, unverified_links, fetched=fetched_urls
            )
            if unverified_links or unknown_markers:
                # MAJOR 1 (code-reviewer, PR #977): an unknown-marker-only offence past the
                # correction bound strips text just like a raw-URL offence does, but used to
                # leave NO trace at all — the `if unverified_links:` guard never fired when no
                # raw URL was ever offered, so a reader-facing consumer (the console's #294
                # warning) had no way to know anything was removed. The `unverified_links`
                # FIELD stays URLs only (never polluted with marker text); the GATE step's
                # detail is what names the marker(s). Kept as the existing plain JSON list of
                # URLs whenever there IS an unverified URL (the read-side DTO,
                # ``HarnessExecutionOut.unverified_links``, only ever treats a JSON *list* as a
                # URL list); a marker-only offence records a small object instead, which reads
                # side correctly resolves to an empty URL list while still naming the marker(s)
                # for a raw trace reader.
                detail = (
                    json.dumps(unverified_links)
                    if unverified_links
                    else json.dumps({"unknown_markers": unknown_markers})
                )
                # Accepted, and flagged. The detail is the machine-readable list a consumer
                # reads; the STATUS carries the boolean, because the detail is truncated at
                # persistence and a reader told nothing because the list would not fit is the
                # silent trust #944 exists to remove.
                steps.append(
                    LoopStep(
                        len(steps),
                        StepKind.GATE,
                        LINK_GATE_NAME,
                        LINK_FLAG_STATUS,
                        _truncate(detail),
                    )
                )
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
            if restricted_search_empty:
                # #961 ruling 3: the member finished, but the websites the person named carried
                # nothing for one of its searches. Marked INCOMPLETE, not failed — the same shape,
                # via the same primitive, for the same reason. A person reading the run learns why
                # it is thin and can correct their addresses, which is the only remedy available:
                # a wrong-but-plausible address can be shown, never detected.
                return _degrade(
                    "dependency", _SITES_EMPTY_ERROR_TYPE, _SITES_EMPTY_MESSAGE, iteration
                )
            # #993/#994 ruling: the shape guarantee is UNWRAP ONLY, last — the member's answer is
            # otherwise settled (citation + link gates already ran, above). No correction turn is
            # spent here any more: a declared key holding a list of records or a dict with real data
            # ships exactly as the member wrote it. `_extract_answer_object` returns `{}` when
            # `last_text` does not parse as a JSON object (ruling 4) — the `if answer_obj:` guard is
            # what keeps that from ever discarding a previously parsed object: `last_text` ships
            # unchanged rather than being blanked.
            if policy.declared_output_keys:
                answer_obj = _extract_answer_object(last_text)
                if answer_obj:
                    changed = _normalize_declared_output(answer_obj, policy.declared_output_keys)
                    if changed:
                        last_text = json.dumps(answer_obj)
            return LoopResult(
                HarnessStatus.SUCCEEDED,
                last_text,
                steps,
                iteration,
                total_tokens=tokens_used,
                input_tokens=input_used,
                output_tokens=output_used,
                served_citation_ids=list(served_citation_ids),
                unverified_links=list(unverified_links),
                fetched_urls=list(fetched_urls),
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
        # #961: and the same clear, for the same reason. A member corrected once and then failing to
        # converge for an unrelated reason must not be reported as a restriction failure — that is
        # the sticky-flag bug the citation terminal above already had to fix once.
        site_blocked = None
        # #944: the same clear, for the same reason. Without it the flag is sticky and a run
        # corrected once and then failing to converge for an unrelated reason is reported as a link
        # failure — the bug shape the citation terminal above already had to fix once.
        links_blocked = None
        link_offense_blocked = False
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
    # #944: the member spent the budget without producing an answer whose links it actually
    # fetched. This DEGRADES — PARTIAL, typed, carrying the last draft — and deliberately does NOT
    # follow the citation terminal above, which escalates.
    #
    # The two are not the same defect. A forged `cit_` id claims the PLATFORM served something it
    # never served, which is a lie about us and must not reach a user. An unverified inline link is
    # the model's own prose, and #944's acceptance criterion asks for it to be "flagged as
    # unverified rather than silently trusted" — flagged, not refused. Shipping the answer with the
    # bad link named is the requested outcome, so this is unconditional rather than a
    # `_budget_gate` call: a per-member `on_exhaustion="escalate"` must not turn a flag into a
    # refusal, the same way the citation terminal refuses to let `degrade` soften an escalation.
    if link_offense_blocked:
        return _degrade(
            LINK_GATE_NAME,
            LINK_FLAG_STATUS,
            "the member could not link only pages it fetched within the budget "
            f"({_named(links_blocked or [])})",
            policy.max_iterations + json_repair_grant,
        )
    # iteration cap reached without a final answer → escalate or degrade (#587).
    #
    # #946 T2: when a call was refused as an unfixable repeat, SAY SO. The owner ruled the run keeps
    # going after a refusal (a member one turn from recovering must get that turn), and accepted the
    # cost: a member that never adapts still spends its whole iteration budget and lands here. An
    # anonymous "did not converge" is precisely the report #946 was filed about — #692 is the record
    # of what an untyped failure costs an operator. The budget is spent either way; naming the call
    # the member kept repeating turns a dead end into something someone can go and fix.
    #
    # Only the TOOL NAME is named, never the arguments: the signature carries whatever the model put
    # in them, and this message reaches a person-facing surface.
    repeat_note = (
        f" — it kept re-sending a call that could not work ({', '.join(repeated_failure_names)})"
        if repeated_failure_names
        else ""
    )
    return _budget_gate(
        "budget",
        "iteration_cap",
        f"tool-use loop did not converge{repeat_note}",
        policy.max_iterations + json_repair_grant,
    )
