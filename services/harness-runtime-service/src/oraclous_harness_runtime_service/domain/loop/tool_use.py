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
from oraclous_harness_runtime_service.domain.link_provenance import (
    LINK_CORRECTION_STATUS,
    LINK_FLAG_STATUS,
    LINK_GATE_NAME,
    check_answer_links,
    extract_answer_urls,
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


def _link_correction(unverified: list[str], fetched: list[str]) -> str:
    """The message a member reads when every link in its draft was invented."""
    allowed = f" — you fetched: {_named(fetched)}" if fetched else ""
    return _LINK_CORRECTION.format(bad=_named(unverified), allowed=allowed)


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
_MAX_FETCHED_URLS = 2000


def _accumulate_fetched(urls: list[str], fetched_urls: list[str], seen: set[str]) -> None:
    for url in urls:
        if len(fetched_urls) >= _MAX_FETCHED_URLS:
            return
        if url not in seen:
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
_REPEATED_FAILURE_MAX = 2
_REPEATED_FAILURE_STATUS = "repeated_failure"
_REPEATED_FAILURE_NOTE = (
    "This exact call has already failed twice with the same error, so it was not sent again. "
    "Sending it a third time cannot work. Either change the arguments — a different value, or the "
    "same call without the argument that is being rejected — or drop this call and answer with "
    "what you already have."
)
#: The per-member ledger: a call signature → (the error it produced, how many times in a row).
RepeatedFailures = dict[str, tuple[str, int]]


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


def _record_failure(ledger: RepeatedFailures, signature: str, error: str) -> None:
    """Count consecutive identical failures. A DIFFERENT error resets the count to one — the call
    is the same but the world changed, and the second failure has not yet proven anything."""
    prior = ledger.get(signature)
    ledger[signature] = (
        (error, prior[1] + 1)
        if prior is not None and prior[0] == error
        else (
            error,
            1,
        )
    )


def _repeated_failures_from_transcript(messages: list[Message]) -> RepeatedFailures:
    """Re-derive the ledger from an already-restored transcript, at a HITL resume.

    Without this every pause hands the member a fresh allowance for a call already proven dead,
    which is the retry loop the bound exists to stop. Mirrors ``_fetched_urls_from_transcript``:
    the checkpoint carries the transcript rather than the loop's own counters, so the resumed
    segment reads its own history back out of the messages instead of being handed one.
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
        explicit_status = _explicit_tool_status(raw_content)
        content = raw_content.split("\n[receipt:", 1)[0]
        failed = (
            explicit_status == "error"
            if explicit_status is not None
            else _is_failed_tool_content(content)
        )
        if not failed:
            continue
        name = names_by_call_id.get(call_id)
        if name is None:
            message_name = message.get("name")
            if not isinstance(message_name, str):
                continue
            name = message_name
        _record_failure(ledger, _call_signature(name, args_by_call_id.get(call_id, {})), content)
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
_TOOL_STATUS_MARKER = re.compile(
    r"\[receipt: source_tool_call_id=\S+ status=(?P<status>ok|error)\]"
)


def _explicit_tool_status(raw_content: str) -> str | None:
    match = _TOOL_STATUS_MARKER.search(raw_content)
    return match.group("status") if match else None


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
        explicit_status = _explicit_tool_status(raw_content)
        content = raw_content.split("\n[receipt:", 1)[0]  # strip the #642 receipt line
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
        _accumulate_fetched(extract_answer_urls(content) + arg_urls, out, seen)
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
    fetched_urls: list[str] = (
        _fetched_urls_from_transcript(resume_state.messages, by_name, redactors)
        if resume_state is not None
        else []
    )
    # #944 review round 3, HIGH-C: the parallel membership set — see `_accumulate_fetched` above.
    fetched_urls_seen: set[str] = set(fetched_urls)
    # #946 T2: the repeated-failure ledger — see `_REPEATED_FAILURE_MAX` above. Re-derived from the
    # restored transcript on a resume, for the same reason `fetched_urls` is: a member that already
    # proved a call dead must not get a fresh allowance for it just because the run was paused.
    repeated_failures: RepeatedFailures = (
        _repeated_failures_from_transcript(resume_state.messages)
        if resume_state is not None
        else {}
    )
    # Which call signatures have been refused as unfixable repeats, and the TOOL NAMES behind them.
    # The names are what the terminal reports: a signature carries the model's raw arguments, which
    # may hold anything the model put there, and a run's error message is a person-facing surface.
    repeated_failure_told: set[str] = set()
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
            # #944: the degraded output is the last draft, so whatever was wrong with its links is
            # what a reader of THIS answer needs warning about — the blocked set when the run ran
            # out of correction budget, the accepted set otherwise.
            unverified_links=list(links_blocked or unverified_links),
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
            elif (repeated_failures.get(_call_signature(tc["name"], tc["args"])) or ("", 0))[
                1
            ] >= _REPEATED_FAILURE_MAX:
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
                signature = _call_signature(tc["name"], tc["args"])
                repeated_failure_told.add(signature)
                if tc["name"] not in repeated_failure_names:
                    repeated_failure_names.append(tc["name"])
                content = _REPEATED_FAILURE_NOTE
                status = _REPEATED_FAILURE_STATUS
                step_name = f"{spec.binding}.{spec.operation}"
            else:
                step_name = f"{spec.binding}.{spec.operation}"
                signature = _call_signature(tc["name"], tc["args"])
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
                    # #946 T2: count it against this exact call. A DIFFERENT error resets the count
                    # — see `_record_failure`. Recorded on the redacted content, so the ledger key
                    # is the same string a resumed run reads back out of the transcript.
                    _record_failure(repeated_failures, signature, content)
                finally:
                    tool_ended = datetime.now(UTC)
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
                _accumulate_fetched(
                    extract_answer_urls(content) + arg_urls, fetched_urls, fetched_urls_seen
                )
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
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "content": (
                        f"{content}\n[receipt: source_tool_call_id={tc['id']} status={status}]"
                    ),
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
            # #944: the inline-link provenance check, in the same place and for the same reason
            # as the citation gate above — a draft sent back has to go back to the MEMBER, so this
            # runs before the answer is accepted rather than after this function returns.
            #
            # Gated on the member HAVING tools. Under a strict reading every link in a tool-less
            # member's answer is unverified, which is not the intent: there is no fetched set to
            # measure it against, and inserting a correction turn into every reasoning-only member
            # is a cost with no signal behind it.
            #
            # The consequence SPLITS (ruled on #944, 2026-09-07): every link invented sends the
            # draft back, some-good-some-bad ships flagged. The split is the whole ruling — a
            # member that fabricated all of its links answered from training data while holding
            # real fetched pages and can fix that, whereas re-running a whole answer over one bad
            # link among good ones throws away real work.
            #
            # #944 review, HIGH-2: "every link invented" is gated on there being a REAL fetched set
            # to have measured against (`fetched_urls`), not merely on `tool_specs` being non-empty.
            # A member whose every tool call errored (a spent key, a rate-limited provider, a
            # connector outage) has an empty fetched set, so under the plain-`tool_specs` gate every
            # answer with any link at all read as "every link invented" and looped until the budget
            # died — the same rationale already applied to a tool-less member, above, now applied
            # to a tooled member with nothing successfully fetched. And even with a real fetched set
            # the correction is capped at `_LINK_CORRECTION_MAX`: past the cap the next attempt
            # ships flagged rather than being sent back again, so a member that keeps failing for
            # its own reasons cannot be walked through its whole iteration budget one correction at
            # a time.
            if tool_specs:
                link_check = check_answer_links(last_text, fetched_urls)
                all_invented = (
                    bool(fetched_urls) and link_check.unverified and not link_check.verified
                )
                if all_invented and link_corrections_used < _LINK_CORRECTION_MAX:
                    link_corrections_used += 1
                    links_blocked = list(link_check.unverified)
                    messages.append({"role": "assistant", "content": last_text})
                    messages.append(
                        {
                            "role": "user",
                            "content": _link_correction(link_check.unverified, fetched_urls),
                        }
                    )
                    steps.append(
                        LoopStep(
                            len(steps),
                            StepKind.GATE,
                            LINK_GATE_NAME,
                            LINK_CORRECTION_STATUS,
                            _truncate(json.dumps(links_blocked)),
                        )
                    )
                    continue
                unverified_links = list(link_check.unverified)
                if unverified_links:
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
                            _truncate(json.dumps(unverified_links)),
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
        # #944: the same clear, for the same reason. Without it the flag is sticky and a run
        # corrected once and then failing to converge for an unrelated reason is reported as a link
        # failure — the bug shape the citation terminal above already had to fix once.
        links_blocked = None
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
    if links_blocked:
        return _degrade(
            LINK_GATE_NAME,
            LINK_FLAG_STATUS,
            "the member could not link only pages it fetched within the budget "
            f"({_named(links_blocked)})",
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
