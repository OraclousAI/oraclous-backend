"""Team-run bridge — drive the orchestrator core with the real harness execution (#419 wiring).

ADR-035 §2. Connects ``oraclous_ohm.orchestrate.run_team`` (the dispatch-injected team-DAG executor,
proven in packages/ohm) to the engine's ``HarnessClient``: each member dispatch becomes a harness
execution of that member's generated sub-harness (passed inline). The typed ``HandoffEnvelope``s are
rendered into the harness input — structured, not a flattened 4000-char truncation. A member whose
harness does not SUCCEED fails the team run (fail-closed). Human gates pause the run (ADR-035 §6).

This is the IN-MEMORY bridge; durable persistence of the run state + the gate pauses (so a pause
survives across requests) is the next wiring step on a team-run model + the task board.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from oraclous_ohm._slug import (
    FILE_SUBSTRATE_READ_TOOLS,
    FILE_SUBSTRATE_WRITE_TOOLS,
    GRAPH_READ_TOOLS,
    GRAPH_WRITE_TOOLS,
    tool_slug,
)
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.errors import OHMError
from oraclous_ohm.manifest import (
    OHMBudget,
    OHMLoop,
    OHMManifest,
    OHMMember,
    resolve_member_caps,
    resolve_member_on_exhaustion,
)
from oraclous_ohm.orchestrate import (
    CheckpointFn,
    Diagnostic,
    DispatchAnnounceFn,
    DispatchFn,
    DoneCheckFn,
    LoopCoordinateFn,
    LoopSeamResult,
    RecalDirective,
    RecalibrateFn,
    TeamRunResult,
    run_loop_seam,
    run_team,
)

from oraclous_execution_engine_service.domain.app_answers import parse_answers
from oraclous_execution_engine_service.domain.refresh import REFRESH_SEED_KEY
from oraclous_execution_engine_service.services.harness_client import HarnessClientError


class _Harness(Protocol):
    """The slice of ``HarnessClient`` the bridge needs (so a fake satisfies it in tests)."""

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = ...,
        manifest_ref: str | None = ...,
        capability_ceiling: list[str] | None = ...,
        parent_execution_id: uuid.UUID | None = ...,
        trace_id: uuid.UUID | None = ...,
        workspace_root: str | None = ...,
        graph_id: str | None = ...,
        team_id: str | None = ...,
        producer: dict[str, Any] | None = ...,
        precedence_order: list[str] | None = ...,
        graph_authoritative: bool = ...,
        max_tokens: int | None = ...,
        max_tool_calls: int | None = ...,
    ) -> dict[str, Any]: ...


# Imported Claude-Code "conductor" agents are written to PROPOSE a `## Handoff` for a human to
# dispatch; run inline inside Oraclous with a thin objective, a model satisfies that persona by
# emitting a handoff stub (no tool calls, no output). This directive re-frames the member as the
# EXECUTOR — do the work now and call its tools — so its in-loop graph-ingest fires and the
# artifacts land on the bound graph (#543 / ADR-041). Compatible with non-imported members (it
# only asks them to use whatever tools they have); fail-soft (it never blocks a legitimately
# tool-less reasoning turn — the loop's completion contract handles acceptance).
EXECUTION_DIRECTIVE = (
    "You are EXECUTING this objective right now inside Oraclous — you are not planning, and "
    "there is no human who will act on a handoff. Do the work yourself and USE YOUR TOOLS to do "
    "it. Produce your substantive output before you finish. A reply that only proposes a "
    "'## Handoff' or a next step, without doing the work and calling your tools, is NOT an "
    "acceptable result."
)


# #694 defect 3. The directive above used to assert, unconditionally, that "your Write tool
# persists your output to the team's shared knowledge graph". That is true on the import on-ramp,
# where ``Write`` resolves to ``graph-ingest``, and it was FALSE for all 14 members of run
# ``fe548aac``, every one of which held ``core/write@1`` and wrote disposable files while being
# told it was saving to the graph. Telling a model a false fact about its own tools is the
# mechanism by which that run looked successful and delivered nothing: each member persisted
# "successfully", reported done, and the next member read an empty graph.
#
# So the persistence sentence is DERIVED from the member's resolved sub-harness capability refs,
# and a member holding neither kind gets no persistence sentence at all — silence beats a guess.
# The four membership sets are the ``_slug`` leaf's, not this module's: the directive, the compile
# gate and the drafter's menu must agree about what a file tool is, or they drift the way the two
# on-ramps drifted about what a tool is CALLED.
_GRAPH_WRITE_TOOLS = GRAPH_WRITE_TOOLS
_GRAPH_READ_TOOLS = GRAPH_READ_TOOLS
_SANDBOX_WRITE_TOOLS = FILE_SUBSTRATE_WRITE_TOOLS
_SANDBOX_READ_TOOLS = FILE_SUBSTRATE_READ_TOOLS

_GRAPH_WRITE_SENTENCE = (
    "Your graph-ingest tool persists your output to the team's shared knowledge graph — this is "
    "how your work is saved and made visible to the rest of the team, so persist your substantive "
    "output there before you finish."
)
_GRAPH_READ_SENTENCE = (
    "Your retrieval tools read the team's shared knowledge graph, which is where the other "
    "members put their work."
)
_SANDBOX_WRITE_SENTENCE = (
    "Your write tool persists your output as a file in your sandbox workspace — this is how your "
    "work is saved and made visible to the rest of the team, so persist your substantive output "
    "there before you finish."
)
_SANDBOX_READ_SENTENCE = (
    "Your read tools read your sandbox workspace, which is where the other members put their work."
)
# #696: a member with NO tools is told so, in the words the #697 contract uses. Run fe548aac's
# reviewer held no tools and still closed with two file paths it had "documented" — nothing had
# told it that it could not. The grade (validate_no_tool_claims) now fails that claim; this
# sentence is the prevention half, so the reply shape the member is asked for and the claim it
# must not make are one instruction. Deliberately names no substrate (#694's silence rule).
_NO_TOOLS_SENTENCE = (
    "You have no tools in this run: you cannot save, write, create, fetch or persist anything, and "
    "no file or location exists because you named it. Do not claim that you did any of those — "
    "reason only over what you were handed, and if asked for `artifact_refs`, report an empty "
    "list."
)


def execution_directive(capability_refs: list[str], *, declared_tools: Sequence[str] = ()) -> str:
    """The run directive for a member holding these resolved sub-harness capability ``ref``s.

    ``declared_tools`` (#696) is the member's own ``tools[]`` ceiling: a member holding NEITHER a
    resolved capability nor a declared tool is told it has no tools (``_NO_TOOLS_SENTENCE``). Both
    are consulted because a ``manifest_ref`` dispatch resolves no sub-harness here and would
    otherwise tell a tooled member it has none — the #694 false-fact failure in a new coat.

    The executor re-framing (#543) is unconditional: an imported Claude-Code "conductor" persona is
    written to PROPOSE a ``## Handoff`` for a human to dispatch, and run inline with a thin
    objective a model satisfies that persona by emitting a handoff stub. The directive re-frames the
    member as the EXECUTOR so it does the work and calls its tools.

    The PERSISTENCE sentence is derived, never asserted (#694). A member whose capabilities reach
    the graph is told about the knowledge graph; one whose capabilities reach the per-org file
    sandbox is told about its sandbox workspace; one with neither is told nothing about persistence.
    A member holding BOTH kinds is told about the graph: that is ambiguous, and graph indexing is
    the invariant (ADR-041 Decision 3 — a sink that writes externally without graph-indexing is
    non-conformant), so the graph sentence wins rather than a guess between them.
    """
    slugs = {tool_slug(ref) for ref in capability_refs}
    parts = [EXECUTION_DIRECTIVE]
    if not slugs and not declared_tools:
        parts.append(_NO_TOOLS_SENTENCE)
    elif slugs & (_GRAPH_WRITE_TOOLS | _GRAPH_READ_TOOLS):
        if slugs & _GRAPH_WRITE_TOOLS:
            parts.append(_GRAPH_WRITE_SENTENCE)
        if slugs & _GRAPH_READ_TOOLS:
            parts.append(_GRAPH_READ_SENTENCE)
    elif slugs & (_SANDBOX_WRITE_TOOLS | _SANDBOX_READ_TOOLS):
        if slugs & _SANDBOX_WRITE_TOOLS:
            parts.append(_SANDBOX_WRITE_SENTENCE)
        if slugs & _SANDBOX_READ_TOOLS:
            parts.append(_SANDBOX_READ_SENTENCE)
    return " ".join(parts)


# #642 — the OTHER half of the grounding contract. ``validate_grounding`` demands that every claim
# cite the tool call that produced it, so a member that DECLARED tools must be told to emit those
# citations; grading a member against a rule it was never given would be unfair (and would fail
# every honest run). Sent only to tool-declaring members — a pure-reasoning stage makes no claims
# and carries no new obligation, so its input is byte-for-byte unchanged.
GROUNDING_DIRECTIVE = (
    "Every factual claim in your output must be BACKED BY A TOOL CALL YOU ACTUALLY MADE. Alongside "
    "your substantive output, emit a JSON object with a `driving_signals` array: one entry per "
    'claim, each {"signal": <what you claim>, "value": <the value>, "source_tool_call_id": <the id '
    "of YOUR tool call that produced it>}. Copy that id VERBATIM from the "
    "`[receipt: source_tool_call_id=...]` line at the end of the tool result you used — never "
    "invent one and never use the tool's name. A claim with no source_tool_call_id, or one citing "
    "a call that failed or that you did not make, is ungrounded and FAILS your step."
)


# #602 (ADR-048 §3) — the seeded-refresh COST LEVER. When a run is seeded from a prior run
# (``seed_from_run_id``), the producing (sink) member receives ITS OWN prior records here, with this
# directive to carry forward the unchanged ones instead of re-deriving them (the token saving). The
# engine stays authoritative: at settle ``compute_delta`` credits ``unchanged`` ONLY when the fresh
# record's evidence fingerprint MATCHES the seed AND it carries the ``refresh_status: unchanged``
# marker — a mismatch is ``changed`` regardless of the marker, so a member can never smuggle a moved
# record through as skipped. Carrying forward is thus a genuine cost lever, never a soundness hole.
REFRESH_CARRY_FORWARD_DIRECTIVE = (
    "This is a REFRESH run. Below are the records YOU produced on the prior run. For every record "
    "whose underlying evidence is UNCHANGED since then, CARRY IT FORWARD: re-emit it "
    'byte-identical to the prior version and add the field "refresh_status": "unchanged". For a '
    "carried-forward record you do NOT need to re-derive, re-analyse, re-research, or re-explain "
    "anything — emit it DIRECTLY; skipping that work is the whole point of a refresh, so keep your "
    "output minimal (do not repeat prior analysis/prose for unchanged records). Only re-derive a "
    "record whose evidence has genuinely CHANGED (emit the new version WITHOUT the marker), drop "
    "records that no longer apply, and add genuinely new ones. The engine independently verifies "
    "each carried record's fingerprint against the prior run, so a carried-forward record MUST be "
    "identical to its prior evidence or it is classified as changed."
)


# #846 — the two framings the validation desk's intake answers arrive under. ADR-052 decision 3
# ships this as that app's own one-off field; the general mechanism (an app-descriptor layer, and a
# platform-level hypothesis flag) is #845's remaining scope. Both blocks are additive: a run with no
# ``answers`` renders byte-for-byte as it does today (the #602/#674 default-OFF discipline).
#
# The flagged half is the point of the feature. Frontend #210 makes "I don't know" a first-class
# intake answer precisely because a founder who does not know will otherwise invent something, and
# nothing downstream catches that: the citation gate checks sources, not premises. A fabricated
# premise produces an impeccably-evidenced wrong brief. So the member is told, in the same breath as
# the question, that this one is an assumption to test.
CONFIRMED_ANSWERS_HEADER = (
    "ANSWERS THE USER GAVE. These are supplied facts about their own situation — use them as "
    "given, and do not re-derive or second-guess them."
)
HYPOTHESIS_DIRECTIVE = (
    "UNVERIFIED ASSUMPTIONS. The user did NOT know the answer to these. Treat every item below as "
    "a hypothesis to TEST against evidence, never as a premise you may rely on. Do not invent an "
    "answer for one, and never present one as established. If your work depends on one of them, "
    "say so explicitly and state what evidence would settle it."
)
#: What an item with no answer renders as. Never a blank — a blank reads to a model as an omission
#: it is free to fill in, which is the exact failure this field exists to stop.
_NO_ANSWER = "no answer given"


def _render_answers(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        answer = item.get("answer")
        rendered = f'"{answer}"' if isinstance(answer, str) and answer.strip() else _NO_ANSWER
        lines.append(f"  - {item['question']} — {rendered}")
    return "\n".join(lines)


def resolve_run_answers(
    inputs: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """#846: the app's intake answers as ``(confirmed, hypotheses)``, or ``None`` for a run without
    them. Read-side only — the SHAPE was already fail-closed at create (``validate_answers``), so a
    malformed payload never reaches a dispatch."""
    try:
        return parse_answers(inputs)
    except ValueError:  # unreachable via create; a stored pre-validation row must not kill a drive
        return None


def resolve_run_task(manifest: OHMManifest, inputs: dict[str, Any] | None) -> str | None:
    """Contract §TASK (#674): the run's task text, or ``None`` when there is none to deliver.

    Only a manifest that DECLARES ``task_input`` consumes it — a stray ``inputs[key]`` on an
    undeclared team is ignored (``inputs`` stays fan-out/refresh seed state, back-compat). A
    declared-but-empty/non-string value resolves to ``None`` here; whether that is allowed is the
    create gate's call (``validate_task_input`` fail-closes the ``required`` case at 422)."""
    declared = manifest.task_input
    if declared is None:
        return None
    value = (inputs or {}).get(declared.key)
    if isinstance(value, str) and value.strip():
        return value
    return None


#: #697 — the member is TOLD what it declared, in the words it has to answer in. Without this the
#: contract is enforced against a member that was never asked: on run b3fce78f the reviewer read
#: the pull request, wrote a real review, and failed its own contract because nothing had said the
#: answer must carry `summary` and `artifact_refs`. Enforcing a promise the member never heard is
#: worse than not enforcing it — it turns a member that worked into one that fails.
OUTPUT_CONTRACT_DIRECTIVE = (
    "Your answer MUST be a single JSON object carrying exactly these keys, because the next member "
    "reads them BY NAME and never reads your prose: {keys}. Put your real work IN those values — "
    "`summary` is what you would have written as your answer, and `artifact_refs` is a list naming "
    "WHERE you persisted anything (the ids or references your persistence tool returned; an empty "
    "list if you persisted nothing). Reply with the JSON object and nothing else."
)


def render_member_input(
    member: OHMMember,
    envelopes: list[HandoffEnvelope],
    fan_item: Any = None,
    *,
    refresh_records: list[dict[str, Any]] | None = None,
    task: str | None = None,
    answers: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    capability_refs: list[str] | None = None,
) -> str:
    """Render a member's objective + fan item + inbound typed hand-offs into the harness input.

    ``refresh_records`` (#602): the producing (sink) member's OWN records from the seed run,
    rendered with the carry-forward directive so the member can skip re-deriving unchanged records
    (the cost lever). Only passed to the sink member of a seeded refresh; ``None`` on every normal
    dispatch, so a non-refresh run's input is byte-for-byte unchanged (default-OFF).

    ``capability_refs`` (#694): the member's RESOLVED sub-harness capability refs, from which the
    run directive derives its persistence sentence instead of asserting the knowledge graph
    unconditionally. ``None``/empty yields the directive with no persistence sentence at all."""
    parts: list[str] = []
    # #577: the inbound handoff's objective_slice scopes THIS dispatch (the producer's ## Handoff
    # Next-task — e.g. "Draft Chapter 04") and takes precedence over the member's static subgoal
    # (e.g. "draft a chapter"); falls back to the subgoal when no inbound handoff carries one. This
    # is what makes a consumer act on its per-edge objective instead of a generic blurb.
    # Single-producer-per-consumer assumption: a FAN-IN consumer takes the FIRST inbound objective
    # (depends_on order); per-producer objective composition is out of scope for this slice (the
    # targeted pipeline artifacts — bitcoin's ## Handoff chain, the book charters — have one handoff
    # producer per consumer). Every producer's PAYLOAD still reaches the member via the From-lines.
    scoped = next((e.objective_slice for e in envelopes if e.objective_slice), "")
    objective = scoped or member.subgoal
    if objective:
        parts.append(f"Objective: {objective}")
    # Contract §TASK (#674): the user's per-run task, VERBATIM, in every member's input — no member
    # reconstructs the target from a hand-off (or worse, invents one). None → byte-identical
    # rendering to a pre-#674 run (the #602 default-OFF discipline).
    if task is not None:
        parts.append(f"Task: {task}")
    # #846: the app's intake answers, delivered to EVERY member like the task. The confirmed ones
    # come FIRST and the flagged ones last, under the directive that reframes them — an answer that
    # drifted under the wrong heading is worse than sending neither, since it either invites the
    # member to distrust a supplied fact or to build on an admitted guess.
    if answers is not None:
        confirmed, hypotheses = answers
        if confirmed:
            parts.append(f"{CONFIRMED_ANSWERS_HEADER}\n{_render_answers(confirmed)}")
        if hypotheses:
            parts.append(f"{HYPOTHESIS_DIRECTIVE}\n{_render_answers(hypotheses)}")
    if fan_item is not None:
        parts.append(f"Item: {json.dumps(fan_item, default=str)}")
    for env in envelopes:
        parts.append(f"From {env.from_role}: {json.dumps(env.payload, default=str)}")
    if (
        refresh_records is not None
    ):  # #602 cost lever — the sink member's prior records to carry fwd
        parts.append(REFRESH_CARRY_FORWARD_DIRECTIVE)
        parts.append(f"Your prior records ({len(refresh_records)}):\n{json.dumps(refresh_records)}")
    parts.append(execution_directive(capability_refs or [], declared_tools=member.tools))
    if member.tools:  # #642: a member that declared tools is graded on receipts — ask for them
        parts.append(GROUNDING_DIRECTIVE)
    # #697: last, so the shape of the reply is the final instruction the member reads.
    declared = _declared_output_keys(member)
    if declared:
        parts.append(OUTPUT_CONTRACT_DIRECTIVE.format(keys=", ".join(repr(k) for k in declared)))
    return "\n\n".join(parts)


def _capability_refs(sub: dict[str, Any] | None) -> list[str]:
    """The ``capabilities[].ref`` strings on a member's resolved sub-harness, for #694's derived
    persistence sentence. A member with no sub-harness (a ``manifest_ref`` dispatch) or a malformed
    one yields none, which renders the directive WITHOUT a persistence sentence — the fail-quiet
    direction, since a guess here is what misinformed all 14 members of run ``fe548aac``."""
    caps = (sub or {}).get("capabilities")
    if not isinstance(caps, list):
        return []
    return [c["ref"] for c in caps if isinstance(c, dict) and isinstance(c.get("ref"), str)]


def _producer_ref(
    member: OHMMember,
    trace_id: uuid.UUID | None,
    team_id: str | None,
    fan_item: Any = None,
) -> dict[str, Any]:
    """#728 — the provenance a member's artifacts carry: who wrote it, in which run.

    ``ordinal`` disambiguates a FAN-OUT member, whose sub-runs share one role: without it the
    per-item outputs would be named identically. It is the fan index when the item carries one and
    is otherwise omitted, so a plain single dispatch is unchanged.
    """
    ref: dict[str, Any] = {"producer_kind": "team-member", "member_role": member.role}
    if trace_id is not None:
        ref["team_run_id"] = str(trace_id)
    if team_id is not None:
        ref["team_id"] = team_id
    if isinstance(fan_item, dict) and isinstance(fan_item.get("index"), int):
        ref["ordinal"] = fan_item["index"]
    return ref


def parse_driving_signals(output: Any) -> list[dict[str, Any]]:
    """#642: the member's claims, out of its real harness output (text, or an already-parsed dict).

    A member emits its ``driving_signals`` inside its answer (the GROUNDING_DIRECTIVE asks for a
    JSON object), so they are extracted here rather than assumed to arrive structured. Fail-soft:
    unparseable output yields no claims, which the strict grade then treats as ungrounded — the
    fail-CLOSED direction.
    """
    if isinstance(output, dict):
        raw = output.get("driving_signals")
        return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []
    if not isinstance(output, str) or "driving_signals" not in output:
        return []
    candidates: list[str] = []
    match = re.search(r"\{.*\}", output, re.DOTALL)  # the widest embedded JSON object
    if match is not None:
        candidates.append(match.group(0))
    array = re.search(r'"driving_signals"\s*:\s*(\[.*?\])', output, re.DOTALL)
    if array is not None:
        candidates.append('{"driving_signals": ' + array.group(1) + "}")
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("driving_signals"), list):
            return [s for s in parsed["driving_signals"] if isinstance(s, dict)]
    return []


def refresh_dispatch_args(
    manifest: OHMManifest, inputs: dict[str, Any] | None
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """#602 cost lever: the ``(seed_records, sink_role)`` to thread into the sink member's dispatch
    on a seeded refresh, or ``(None, None)`` for a normal run (default-OFF). The seed rides
    ``inputs["_refresh_seed"]`` (threaded at create by ``thread_refresh_seed``); the sink is the
    single producing member (no other member depends on it — the same member whose deliverable the
    settle-time delta parses). A non-refresh run, an empty/unparseable seed, a team with no single
    sink, or a ``fan_out`` sink threads nothing, so its dispatch is byte-for-byte unchanged."""
    seed = (inputs or {}).get(REFRESH_SEED_KEY)
    if not isinstance(seed, dict):
        return None, None
    records = seed.get("records")
    if not isinstance(records, list) or not records:  # unparseable/empty seed → no carry-forward
        return None, None
    depended = {d for m in manifest.members for d in m.depends_on}
    sinks = [m for m in manifest.members if m.role not in depended]
    if len(sinks) != 1:  # only a single-sink producer carries forward (the settle delta's shape)
        return None, None
    # #602 review Finding 1: never carry-forward into a fan_out sink — the seed would be re-rendered
    # per fan-item (multiplying the input cost), which can INVERT the saving. A fan-out refresh
    # re-derives (the delta still computes at settle); the lever targets a plain single producer.
    if sinks[0].fan_out is not None:
        return None, None
    return [r for r in records if isinstance(r, dict)], sinks[0].role


def _declared_output_keys(member: OHMMember) -> list[str]:
    """The keys this member DECLARED it will hand on (#697), or [] when it declared nothing.

    ``outputs_schema`` is ``{"required": [...]}`` — the same shape ``validate_payload`` reads, so
    the two cannot disagree about what was promised. A member that declares nothing is untouched:
    every team compiled before this change runs exactly as it did."""
    schema = member.outputs_schema or {}
    required = schema.get("required")
    return [k for k in required if isinstance(k, str)] if isinstance(required, list) else []


def _parse_member_object(output: Any) -> dict[str, Any]:
    """The JSON object a member answered with, or {} when it did not answer with one.

    A real model wraps its JSON in prose or a fence, so the object is PEELED rather than parsed
    whole (the same reason ``validate_draft`` peels the drafter's reply). Never raises: a member
    that answered with prose simply declared keys it did not deliver, and the orchestrator fails it
    on its own contract with a readable reason — a parse crash would say nothing."""
    if isinstance(output, dict):
        return output
    if not isinstance(output, str):
        return {}
    match = re.search(r"\{.*\}", output, re.DOTALL)
    if match is None:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def make_harness_dispatch(
    harness: _Harness,
    sub_harnesses: dict[str, dict[str, Any]],
    *,
    trace_id: uuid.UUID | None = None,
    parent_execution_id: uuid.UUID | None = None,
    on_child: Callable[[str, str], None] | None = None,
    on_cost: Callable[[int], None] | None = None,
    workspace_root: str | None = None,
    graph_id: str | None = None,
    team_id: str | None = None,
    precedence_order: list[str] | None = None,
    graph_authoritative: bool = False,
    budget: OHMBudget | None = None,
    refresh_seed_records: list[dict[str, Any]] | None = None,
    refresh_sink_role: str | None = None,
    task: str | None = None,
    answers: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> DispatchFn:
    """Build a ``run_team`` dispatch that runs each member as a real harness execution.

    Run-tree correlation (#471): ``trace_id`` (the team-run root) + ``parent_execution_id`` are
    threaded into every member's harness run so the harness stamps each into the same tree; each
    member's harness execution id is surfaced via ``on_child`` — with the role that produced it
    (#828 item 4) — so the engine records the tree.
    O4 metering (#472): each member's ``total_tokens`` is surfaced via ``on_cost`` so the engine
    accumulates the run's RAW token cost from the harness's own metering (ADR-009)."""

    async def dispatch(member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any) -> Any:
        sub = sub_harnesses.get(member.role)
        # #576: the member's user-set runtime SAFETY CAP (member override > team-wide default,
        # clamped <= the team-pooled total when a budget is present). Sent whenever a cap RESOLVES —
        # a member's OWN max_tokens binds with no team budget; only a team with NEITHER a member cap
        # nor a budget adds zero kwargs and runs unchanged (the tier stands). The harness applies
        # the cap as the per-member token / tool-call ceiling.
        member_max_tokens, member_max_tool_calls = resolve_member_caps(member, budget)
        caps: dict[str, Any] = {}
        if member_max_tokens is not None:
            caps["max_tokens"] = member_max_tokens
        if member_max_tool_calls is not None:
            caps["max_tool_calls"] = member_max_tool_calls
        # #587: the member's resolved on_exhaustion (member-over-team) rides to the harness like the
        # caps. Sent ONLY for the explicit "degrade" — "escalate" is the harness default, so an
        # unchanged team adds zero kwargs (the #576 send-only-when-set pattern; back-compat).
        if resolve_member_on_exhaustion(member, budget) == "degrade":
            caps["on_exhaustion"] = "degrade"
        # #853: the member's structured-output declaration rides the same way — sent only when the
        # member declared it, so an undeclared team adds zero kwargs and is unchanged.
        # #697: a member that DECLARED output keys must return something that parses, or the keys
        # can never be found — so a declared contract asks for the same bounded repair turn.
        declared_keys = _declared_output_keys(member)
        if member.requires_valid_json or declared_keys:
            caps["requires_valid_json"] = True
        result = await harness.execute(
            input_text=render_member_input(
                member,
                envelopes,
                fan_item,
                # #602 cost lever: only the SINK member of a seeded refresh receives its prior
                # records + the carry-forward directive; every other dispatch is unchanged.
                refresh_records=(
                    refresh_seed_records if member.role == refresh_sink_role else None
                ),
                # Contract §TASK (#674): the run's task, delivered to EVERY member verbatim.
                task=task,
                # #846: and the app's intake answers, to every member for the same reason — a
                # downstream member must not have to reconstruct what was assumed from a hand-off.
                answers=answers,
                # #694: the member's OWN resolved capability refs, so the run directive states
                # where its output actually persists rather than asserting the graph for everyone.
                capability_refs=_capability_refs(sub),
            ),
            manifest_inline=sub,
            manifest_ref=(member.manifest_ref if sub is None else None),
            # the member's tools[] is the authoritative ceiling (ADR-032/035 §5) — it caps the
            # harness fail-closed for BOTH the inline AND the manifest_ref path, so a registered
            # manifest_ref harness can never exceed what the member declared (red-team G-A).
            capability_ceiling=list(member.tools),
            **caps,
            parent_execution_id=parent_execution_id,
            trace_id=trace_id,
            # file-native blackboard (#518): the trusted per-run working tree every member's file
            # tools operate on in place (the harness sets it on each file-tool instance's config).
            workspace_root=workspace_root,
            # graph substrate (#524): the per-run graph the graph tools (knowledge-retriever /
            # graph-ingest / find-similar) target — set on each instance's config so the model
            # never invents a UUID. org-scoped at create (cross-org rejected).
            graph_id=graph_id,
            # team-scope blackboard (#513): the stable team identity (team-manifest id) every member
            # shares — the harness writes/reads team-scope memory under it, so concurrent members +
            # future runs of the same team see one blackboard (the adopted-graph world-model).
            team_id=team_id,
            # #728: WHO is writing. An artifact used to record nothing about its producer, so a
            # run's outputs were indistinguishable and — because the lexical document node keys on
            # the filename, which was the constant `inline.txt` — every write in a run collapsed
            # onto ONE node (run dc167d8e landed 7 artifacts and kept 1). Bound here, on the same
            # trusted path as graph_id, so the model can neither supply nor forge its identity.
            producer=_producer_ref(member, trace_id, team_id, fan_item),
            # Hierarchy of Truth (#538): the team's precedence + authoritative flag, bound onto
            # each knowledge-retriever instance so a member's in-loop read is auto-ranked (#514).
            precedence_order=precedence_order,
            graph_authoritative=graph_authoritative,
        )
        status = result.get("status")
        # run-tree (#471): record the child execution id + token cost BEFORE the fail-closed check,
        # so a FAILED member is still surfaced in GET /tree (not an empty []) and its tokens still
        # count. Skipped if the harness omitted an id.
        child_id = result.get("id")
        if on_child is not None and child_id is not None:
            on_child(str(child_id), member.role)
        # O4 metering (#472): accumulate this member's RAW token cost (0 if the harness omitted it)
        if on_cost is not None:
            on_cost(int(result.get("total_tokens") or 0))
        # #587: PARTIAL (on_exhaustion=degrade) is a GOVERNED graceful exhaustion — a flagged
        # partial member result, NOT a failure. It must NOT raise (only a genuine FAILED does); the
        # orchestrator records it "partial" and the team is not cascade-failed by a degrade.
        if status not in ("SUCCEEDED", "PARTIAL"):  # fail-closed — surface the REAL harness error
            detail = result.get("error_message") or result.get("error_type")
            raise HarnessClientError(
                f"member {member.role!r} harness did not succeed: {status}"
                + (f" — {detail}" if detail else "")
            )
        # #642: thread the member's durable step trace (with #641's tool_call_ids) and the claims it
        # made up to the orchestrator, which grades a tool-declaring member on whether each claim
        # resolves to an ok call of its own. A harness response with no `steps` key predates #641
        # and reports no trace — it is threaded as an empty one, so an unproven claim never passes.
        # A harness that reports its claims structurally is taken at its word; otherwise they are
        # parsed out of the member's own answer, where the directive asked it to put them.
        reported = result.get("driving_signals")
        payload: dict[str, Any] = {
            "output": result.get("output"),
            "status": status,
            "steps": result.get("steps") or [],
            "driving_signals": (
                [s for s in reported if isinstance(s, dict)]
                if isinstance(reported, list)
                else parse_driving_signals(result.get("output"))
            ),
        }
        # #697: the member's DECLARED keys join the payload the next member receives. Without this
        # the declaration can never be satisfied — what a producer hands on is this envelope, and
        # its answer sits under "output" as prose. Team run 76620efe: the Reviewer wrote a complete
        # review of a pull request and succeeded; the Commenter, which depends on it, searched the
        # shared knowledge graph for that review, failed five calls, and posted nothing. It held
        # the review and had no name to reach it by.
        #
        # Only the DECLARED keys are lifted, and never over the envelope's own four: a member
        # cannot rename its status or forge its trace by answering with those keys.
        if declared_keys:
            answered = _parse_member_object(result.get("output"))
            for key in declared_keys:
                if key in answered and key not in payload:
                    payload[key] = answered[key]
        return payload

    return dispatch


async def run_team_harness(
    manifest: OHMManifest,
    harness: _Harness,
    *,
    sub_harnesses: dict[str, dict[str, Any]] | None = None,
    gate_decisions: dict[str, str] | None = None,
    completed: dict[str, Any] | None = None,
    trace_id: uuid.UUID | None = None,
    parent_execution_id: uuid.UUID | None = None,
    on_child: Callable[[str, str], None] | None = None,
    on_cost: Callable[[int], None] | None = None,
    cost_so_far: Callable[[], int] | None = None,
    workspace_root: str | None = None,
    graph_id: str | None = None,
    inputs: dict[str, Any] | None = None,
    precedence_order: list[str] | None = None,
    graph_authoritative: bool = False,
    on_checkpoint: CheckpointFn | None = None,
    on_dispatch: DispatchAnnounceFn | None = None,
) -> TeamRunResult:
    """Run a Team Harness member DAG, dispatching each member as a real harness execution.

    ``completed`` (members that already ran in a prior drive) is passed through so a resume past a
    human gate does not re-dispatch already-finished members (their side effects fire once).
    ``trace_id``/``parent_execution_id``/``on_child`` thread + collect the run-tree (#471);
    ``on_cost`` accumulates the run's RAW token cost (#472); ``on_checkpoint`` (#819) makes each
    settled member durable mid-drive; ``on_dispatch`` (#828) fires the instant a member is admitted
    to a dispatch slot, before it runs."""
    # team-scope blackboard (#513): the STABLE team identity is the team-manifest id — derived here
    # (not a separate binding) + threaded to every member so they share one team-scope memory.
    team_id = str(manifest.metadata.id)
    # #585: the running pooled tally feeds run_team's pre-dispatch pooled ceiling gate (ADR-031 D3).
    # Prefer the CALLER's cost_so_far (the engine's tally — it includes prior_cost across a resume,
    # so a resumed run cannot re-spend past the ceiling); else build it from THIS drive's on_cost
    # (the direct/unit path). The caller's on_cost (the DB cost_tokens accumulator) still fires.
    cost_deltas: list[int] = []

    def _on_cost(tokens: int) -> None:
        cost_deltas.append(tokens)
        if on_cost is not None:
            on_cost(tokens)

    pooled_cost = cost_so_far if cost_so_far is not None else (lambda: sum(cost_deltas))
    refresh_records, refresh_sink = refresh_dispatch_args(manifest, inputs)  # #602 cost lever
    dispatch = make_harness_dispatch(
        harness,
        sub_harnesses or {},
        trace_id=trace_id,
        parent_execution_id=parent_execution_id,
        on_child=on_child,
        on_cost=_on_cost,
        workspace_root=workspace_root,
        graph_id=graph_id,
        team_id=team_id,
        precedence_order=precedence_order,
        graph_authoritative=graph_authoritative,
        budget=manifest.budget,  # #576: per-member caps resolve from the team budget + members
        refresh_seed_records=refresh_records,  # #602: the sink's prior records (refresh only)
        refresh_sink_role=refresh_sink,
        task=resolve_run_task(manifest, inputs),  # Contract §TASK (#674): to every member
        answers=resolve_run_answers(inputs),  # #846: the app's intake answers, to every member
    )
    return await run_team(
        manifest,
        dispatch,
        state=inputs,  # #599: user-seeded state for a member's fan_out.over: "$.<key>"
        gate_decisions=gate_decisions,
        completed=completed,
        cost_so_far=pooled_cost,
        on_checkpoint=on_checkpoint,  # #819: per-member durability
        on_dispatch=on_dispatch,  # #828: fires before a member's dispatch runs
    )


# A genuine loop (ADR-043 #552) is interleaved into the acyclic skeleton as ONE condensed node under
# this synthetic role, so ``run_team`` schedules it at its topological position (its downstream
# members run only AFTER it). The node's dispatch expands to the bounded ``run_loop_seam``.
_LOOP_NODE_PREFIX = "__loop__"
_DEFAULT_MAX_ROUNDS = 20  # the conductor's round cap when the manifest declares no max_rounds


def _loop_node_role(index: int) -> str:
    return f"{_LOOP_NODE_PREFIX}{index}"


def _loop_node_index(role: str) -> int | None:
    if role.startswith(_LOOP_NODE_PREFIX):
        try:
            return int(role[len(_LOOP_NODE_PREFIX) :])
        except ValueError:
            return None
    return None


def _condense(
    manifest: OHMManifest, loops: list[OHMLoop]
) -> tuple[list[OHMMember], dict[str, int]]:
    """Build the condensed member DAG: the acyclic skeleton + ONE synthetic node per loop, with
    every ``depends_on`` that points INTO a loop re-pointed to that loop's synthetic node. The
    synthetic node's own ``depends_on`` is the loop's inter-SCC upstream (the importer already
    stripped the intra-loop edges). Pure; the condensed graph is acyclic iff the inter-SCC graph is
    (which the importer guarantees), so ``run_team`` topologically orders it."""
    loop_of_role = {role: i for i, loop in enumerate(loops) for role in loop.members}

    def repoint(deps: list[str]) -> list[str]:
        # a dep into a loop member becomes a dep on that loop's node (de-duplicated, stable order)
        return sorted({_loop_node_role(loop_of_role[d]) if d in loop_of_role else d for d in deps})

    skeleton = [
        m.model_copy(update={"depends_on": repoint(m.depends_on)})
        for m in manifest.skeleton_members()
    ]
    by_role = {m.role: m for m in manifest.members}
    synthetic: list[OHMMember] = []
    for i, loop in enumerate(loops):
        upstream: set[str] = set()
        for role in loop.members:
            for dep in by_role[role].depends_on:
                if dep not in loop.members:  # an inter-SCC upstream edge (intra were stripped)
                    upstream.add(_loop_node_role(loop_of_role[dep]) if dep in loop_of_role else dep)
        synthetic.append(
            OHMMember(
                role=_loop_node_role(i),
                kind="agent",
                manifest_ref="internal:loop-conductor",  # intercepted — never a harness dispatch
                depends_on=sorted(upstream),
            )
        )
    return skeleton + synthetic, loop_of_role


async def run_team_hybrid(
    manifest: OHMManifest,
    harness: _Harness,
    *,
    coordinate: LoopCoordinateFn | None = None,
    done_check_for: Callable[[OHMLoop, dict[str, Any]], DoneCheckFn] | None = None,
    recalibrate: RecalibrateFn | None = None,
    cost_so_far: Callable[[], int] | None = None,
    sub_harnesses: dict[str, dict[str, Any]] | None = None,
    gate_decisions: dict[str, str] | None = None,
    completed: dict[str, Any] | None = None,
    loop_state: dict[str, Any] | None = None,
    trace_id: uuid.UUID | None = None,
    parent_execution_id: uuid.UUID | None = None,
    on_child: Callable[[str, str], None] | None = None,
    on_cost: Callable[[int], None] | None = None,
    workspace_root: str | None = None,
    graph_id: str | None = None,
    inputs: dict[str, Any] | None = None,
    precedence_order: list[str] | None = None,
    graph_authoritative: bool = False,
    on_checkpoint: CheckpointFn | None = None,
    on_dispatch: DispatchAnnounceFn | None = None,
) -> TeamRunResult:
    """Drive a Team Harness whose handoff graph has GENUINE loops (ADR-043 #552): the acyclic
    skeleton runs on ``run_team`` and each loop SCC runs the bounded ``run_loop_seam`` conductor,
    interleaved at its topological position via a condensed node. Upstream→loop→downstream data
    flows through the shared graph/blackboard (every member shares ``graph_id``), so the hybrid only
    has to ORDER the loops correctly. A purely acyclic team (no loops) is delegated unchanged to
    ``run_team_harness``.

    ``coordinate`` (picks the next loop member) + ``done_check_for`` (the CODED done-check per loop)
    are INJECTED — the engine wires the real BYOM coordinator + coverage/artifacts/evaluator check;
    a loop team with either unwired FAILS CLOSED (the team can never satisfy its own done-check).
    A loop that does not converge raises out of its condensed node, so ``run_team`` records it
    failed + BLOCKS its downstream (#551 non-abort), and the run is re-runnable.

    ``on_checkpoint`` (#819) is forwarded to BOTH sides — the skeleton on ``run_team`` and each
    loop's ``run_loop_seam`` — through ``_emit_checkpoint`` below, which is where the condensed
    node is filtered out and the two sides' state is merged."""
    loops = list(manifest.orchestration.loops) if manifest.orchestration else []
    if not loops:  # purely acyclic — the unchanged single-pass DAG path
        return await run_team_harness(
            manifest,
            harness,
            sub_harnesses=sub_harnesses,
            gate_decisions=gate_decisions,
            completed=completed,
            trace_id=trace_id,
            parent_execution_id=parent_execution_id,
            on_child=on_child,
            on_cost=on_cost,
            cost_so_far=cost_so_far,  # #585: the engine's pooled tally (incl. prior_cost on resume)
            workspace_root=workspace_root,
            graph_id=graph_id,
            inputs=inputs,  # #599: user-seeded state for a fan_out.over: "$.<key>"
            precedence_order=precedence_order,
            graph_authoritative=graph_authoritative,
            on_checkpoint=on_checkpoint,  # #819: per-member durability
            on_dispatch=on_dispatch,  # #828: fires before a member's dispatch runs
        )
    if coordinate is None or done_check_for is None:  # fail-closed (ADR-043 invariant)
        raise OHMError("team has loops but no coordinator/done-check wired")

    by_role = {m.role: m for m in manifest.members}
    team_id = str(manifest.metadata.id)
    refresh_records, refresh_sink = refresh_dispatch_args(manifest, inputs)  # #602 cost lever
    real_dispatch = make_harness_dispatch(
        harness,
        sub_harnesses or {},
        trace_id=trace_id,
        parent_execution_id=parent_execution_id,
        on_child=on_child,
        on_cost=on_cost,
        workspace_root=workspace_root,
        graph_id=graph_id,
        team_id=team_id,
        precedence_order=precedence_order,
        graph_authoritative=graph_authoritative,
        budget=manifest.budget,  # #576: per-member caps resolve from the team budget + members
        refresh_seed_records=refresh_records,  # #602: the sink's prior records (refresh only)
        refresh_sink_role=refresh_sink,
        task=resolve_run_task(manifest, inputs),  # Contract §TASK (#674): to every member
        answers=resolve_run_answers(inputs),  # #846: the app's intake answers, to every member
    )
    termination = manifest.orchestration.termination if manifest.orchestration else None
    max_rounds = (termination.max_rounds if termination else None) or _DEFAULT_MAX_ROUNDS
    max_wall = termination.max_wall_seconds if termination else None
    max_cost = manifest.budget.max_tokens_total if manifest.budget else None

    condensed, _ = _condense(manifest, loops)
    loop_results: dict[int, LoopSeamResult] = {}
    in_loop_state = loop_state or {}  # PR-C: prior per-loop checkpoint (resume), by loop index
    out_loop_state: dict[str, Any] = {}  # PR-C: the checkpoint to persist after this drive
    paused_gates: list[str] = []  # PR-C: per-round HITL gate(s) a loop is paused on
    # #819: the two sides of the hybrid checkpoint. The skeleton and each loop accumulate state in
    # SEPARATE dicts (run_team's own + run_loop_seam's own), and neither can see the other — so a
    # snapshot from one side alone is INCOMPLETE. The engine's ``checkpoint`` OVERWRITES the column,
    # so emitting a loop-side snapshot on its own would erase the skeleton members already durable
    # on the row. Both sides therefore emit through ``_emit_checkpoint``, which merges the latest
    # state of the other side back in.
    skeleton_seen: tuple[dict[str, Any], dict[str, str]] = ({}, {})
    loop_results_seen: dict[str, Any] = {}
    loop_status_seen: dict[str, str] = {}

    async def _emit_checkpoint(
        results: dict[str, Any], member_status: dict[str, str], *, from_loop: bool
    ) -> None:
        """Merge the skeleton's and the loops' accumulated state into ONE complete snapshot, with
        the internal condensed node stripped.

        The strip is not cosmetic. ``run_team`` drives the CONDENSED member list, in which each loop
        is one synthetic ``__loop__<i>`` node, and the settle path pops that node only AFTER the run
        returns — so a checkpoint fired mid-drive would carry it. On the row it is both a leak of
        internal state to the API and, worse, a poisoned resume seed: ``_completed_for_resume``
        would seed ``__loop__0``, ``run_team`` would mark the condensed node succeeded without ever
        entering the conductor, the loop member's real output would vanish, and the run would report
        SUCCEEDED. That is #819's own failure reappearing inside the fix for it."""
        nonlocal skeleton_seen
        if from_loop:
            loop_results_seen.update(results)
            loop_status_seen.update(member_status)
        else:
            skeleton_seen = (results, member_status)
        if on_checkpoint is None:
            return
        merged_results = {
            role: value
            for role, value in skeleton_seen[0].items()
            if _loop_node_index(role) is None
        }
        merged_status = {
            role: value
            for role, value in skeleton_seen[1].items()
            if _loop_node_index(role) is None
        }
        merged_results.update(loop_results_seen)
        merged_status.update(loop_status_seen)
        await on_checkpoint(merged_results, merged_status)

    async def _skeleton_checkpoint(results: dict[str, Any], member_status: dict[str, str]) -> None:
        await _emit_checkpoint(results, member_status, from_loop=False)

    async def _loop_checkpoint(results: dict[str, Any], member_status: dict[str, str]) -> None:
        await _emit_checkpoint(results, member_status, from_loop=True)

    async def hybrid_dispatch(
        member: OHMMember, envelopes: list[HandoffEnvelope], fan_item: Any
    ) -> Any:
        i = _loop_node_index(member.role)
        if i is None:  # an ordinary skeleton member — the real harness dispatch
            return await real_dispatch(member, envelopes, fan_item)
        loop = loops[i]
        # seed the loop with any members already delivered in a prior drive (resume / re-run)
        seed = {r: completed[r] for r in loop.members if completed and r in completed}
        # PR-C: resume the round-index + the ORIGINAL wall-clock start ONLY for a PAUSED loop (a
        # mid-loop HITL suspension — continue where it left off; the epoch started_at survives the
        # process restart across a long pause so the wall-clock measures real elapsed time). A loop
        # that halted at a BOUND (max_rounds / wall / cost / no_progress) is a spent attempt — an
        # ADR-042 re-run must RESTART it (round 0, fresh wall-clock), not resume the spent round.
        cp = in_loop_state.get(str(i), {})
        if cp.get("status") == "paused":
            resume_round = int(cp.get("round") or 0)
            started = float(cp["started_at"]) if cp.get("started_at") is not None else time.time()
            # #553: the recalibration COUNT survives a HITL pause/approve cycle so the cap holds
            # across resume (a constant cap=1 is itself stable; only the spent count must persist).
            # NB the anti-repeat DIGEST is intentionally NOT persisted: at cap=1 a 2nd recalibration
            # (where the digest would be compared) never occurs — the cap halts first. If the cap is
            # ever raised, also persist + thread ``resume_last_directive_digest`` here.
            resume_recals = int(cp.get("recalibration_count") or 0)
        else:
            resume_round, started, resume_recals = 0, time.time(), 0
        # #553: the coded done-check writes WHICH gate failed (artifacts / grade) into this shared
        # side-channel; the seam reads it to build the (coded, external) recalibration Diagnostic.
        done_check_diag: dict[str, Any] = {}
        seam = await run_loop_seam(
            loop,
            by_role,
            real_dispatch,
            coordinate,  # type: ignore[arg-type]  # narrowed non-None above
            done_check_for(loop, done_check_diag),  # type: ignore[misc]
            max_rounds=max_rounds,
            max_wall_seconds=max_wall,
            max_cost=max_cost,
            cost_so_far=cost_so_far,
            seed_results=seed or None,
            gate_decisions=gate_decisions,
            resume_from_round=resume_round,
            started_at=started,
            clock=time.time,
            recalibrate=recalibrate,  # #553: None for a non-loop drive → the seam is byte-unchanged
            recalibration_cap=_RECALIBRATION_CAP,
            done_check_diag=done_check_diag,
            resume_recalibrations_used=resume_recals,
            on_checkpoint=_loop_checkpoint,  # #819 decision 4: durable at each round boundary
        )
        loop_results[i] = seam
        out_loop_state[str(i)] = {
            "round": seam.rounds,
            "started_at": started,
            "status": seam.status,
            "recalibration_count": seam.recalibrations_used,  # #553: persist for resume cap enforce
        }
        if seam.status == "paused":  # PR-C: a per-round HITL gate awaits a human decision
            paused_gates.extend(seam.paused_at)
        if seam.status != "converged":  # paused OR a bound halt → block downstream (#551 non-abort)
            raise HarnessClientError(f"loop {i}: {seam.status}")
        return {"loop": i, "status": seam.status, "output": seam.results}

    skeleton = await run_team(
        manifest,
        hybrid_dispatch,
        state=inputs,  # #599: user-seeded state for a skeleton member's fan_out.over: "$.<key>"
        gate_decisions=gate_decisions,
        completed=completed,
        members=condensed,
        cost_so_far=cost_so_far,  # #585: the pooled token gate binds the skeleton members too
        on_checkpoint=_skeleton_checkpoint,  # #819: durable per settled skeleton member
        on_dispatch=on_dispatch,  # #828: fires before a skeleton member's dispatch runs
    )

    # merge each loop's real-member results into the team result; the synthetic node is internal
    for i, seam in loop_results.items():
        skeleton.results.update(seam.results)
        skeleton.member_status.update(seam.member_status)
        skeleton.member_errors.update(seam.member_errors)
        skeleton.envelopes.extend(seam.envelopes)
        if seam.status == "paused":
            continue  # PR-C: a PAUSED loop is NOT a failure — its members are not marked failed
        # a non-converged loop FAILED AS A UNIT — its goal was not met though its members each
        # dispatched. Mark EVERY loop member failed so the run is re-runnable (re-drives the loop,
        # ADR-042 #551); a member that genuinely raised in-loop keeps its own leak-safe error.
        if seam.status != "converged":
            for role in loops[i].members:
                skeleton.member_status[role] = "failed"
                skeleton.member_errors.setdefault(role, f"loop did not converge: {seam.status}")
    for i in loop_results:  # drop the internal condensed node from the surfaced results/status
        skeleton.results.pop(_loop_node_role(i), None)
        skeleton.member_status.pop(_loop_node_role(i), None)
        skeleton.member_errors.pop(_loop_node_role(i), None)
    skeleton.loop_state = out_loop_state  # PR-C: the per-loop checkpoint for the engine to persist
    # PR-C: a per-round HITL gate PAUSES the team (awaiting the human decision) — not a failure; the
    # advance machinery resumes it. Pause takes precedence over a run_team-marked blocked member.
    if paused_gates:
        skeleton.status = "paused"
        skeleton.paused_at = sorted(set(skeleton.paused_at) | set(paused_gates))
        return skeleton
    # the team SUCCEEDS only when every member delivered (ADR-042); any failed/blocked → FAILED
    if any(s in ("failed", "blocked") for s in skeleton.member_status.values()):
        skeleton.status = "failed"
    return skeleton


# ── the BYOM loop coordinator (ADR-043 #552) ───────────────────────────────────────────────────
# The conductor's router: a bounded model turn that ONLY PICKS the next loop member to run. It NEVER
# decides "done" (the coded done-check does) and NEVER grants a capability (capability_ceiling=[]).
# LEAK-SAFETY: the prompt carries the loop's STRUCTURE — each member's role, its ## Handoff routing
# intent, and a produced/not-produced boolean — but NEVER a member's raw output (customer text). The
# CONTENT judgement (has the work met the bar?) is the separate coded evaluator's job, not the
# router's, so no runtime output is ever re-emitted into a model prompt or a log.


def _render_coordinator_prompt(
    loop: OHMLoop, results: dict[str, Any], rounds_left: int, members: list[str] | None = None
) -> str:
    """The coordinator's input — loop structure + a per-member produced flag (NO raw outputs).
    ``members`` (default = every loop member) is the WORK members the router may pick — a per-round
    HITL gate (kind:human) is excluded (the seam handles it), so the router never routes to it."""
    roles = members if members is not None else list(loop.members)
    lines = [
        "You are the COORDINATOR of a team loop. Pick the SINGLE next member to run so the loop",
        "makes progress toward its goal. You do NOT decide when the loop is done and you do NOT do",
        "any member's work — you only route.",
        "",
        f"Rounds left before the loop is force-stopped: {rounds_left}.",
        "Members of this loop (role — its handoff intent — has it produced yet?):",
    ]
    for role in roles:
        intent = loop.routing.get(role, "") or "(no stated intent)"
        produced = "produced" if results.get(role) is not None else "not yet produced"
        lines.append(f"  - {role} — {intent} — {produced}")
    lines += [
        "",
        "Reply with ONLY the role name of the next member to run (exactly as written above).",
        "If you believe the loop's goal is met, reply with the single word DONE — a separate coded",
        "check will confirm or send the loop back to you. Reply with nothing else.",
    ]
    return "\n".join(lines)


def _parse_next_roles(output: Any, *, allowed: set[str]) -> list[str]:
    """Parse the coordinator's reply into the next loop member(s), FAIL-CLOSED. Only declared loop
    members survive (a hallucinated outsider becomes a no-op pick, never a seam abort); DONE / empty
    / unparseable → ``[]`` (the coded done-check then decides). Never logs the model output."""
    if not isinstance(output, str):
        return []
    text = output.strip()
    if not text or text.upper().startswith("DONE"):
        return []
    # accept a bare role, a quoted role, or a leading token; match against declared members only
    picks: list[str] = []
    for token in text.replace(",", " ").replace("\n", " ").split():
        cleaned = token.strip().strip("\"'`.").strip()
        if cleaned in allowed and cleaned not in picks:
            picks.append(cleaned)
    return picks


def _coordinator_subharness(team: OHMManifest) -> dict[str, Any]:
    """A tool-LESS single-agent harness for the coordinator turn (picks-only — no ``capabilities``).
    Binds the team's coordinator BYOM model (role ``coordinator`` → ``evaluator`` → ``primary``), so
    the router runs on the user's own key through the gateway (ADR-008); none declared → the harness
    falls back to the operator model."""
    model = team.model_by_role("coordinator") or team.evaluator_model() or team.primary_model()
    doc: dict[str, Any] = {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "loop-coordinator",
            "owner_organization_id": str(team.metadata.owner_organization_id),
        },
        "capabilities": [],  # picks-only — the router can call NO tool (ADR-043 invariant)
        "prompts": [
            {
                "role": "primary",
                "source": "inline",
                "body": "You route a team loop. Follow the user instruction exactly; reply with "
                "only a role name or DONE.",
            }
        ],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }
    if model is not None:
        doc["models"] = [model.model_dump(mode="json")]
    return doc


def make_loop_coordinator(harness: _Harness, team: OHMManifest) -> LoopCoordinateFn:
    """Build the BYOM loop coordinator (ADR-043 #552) — a bounded, tool-less model turn that picks
    the next loop member. Picks-only (``capability_ceiling=[]``), leak-safe (structure not content),
    fail-closed (an unreachable/garbled router yields ``[]`` → the coded done-check rules)."""
    sub = _coordinator_subharness(team)
    by_role = {m.role: m for m in team.members}

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        # PR-C: route ONLY among work members — a kind:human per-round gate is a structural pause
        # (the seam pauses/renders it), never a coordinator pick, so it can't waste a round.
        work = [r for r in loop.members if not (by_role.get(r) and by_role[r].kind == "human")]
        try:
            out = await harness.execute(
                input_text=_render_coordinator_prompt(loop, results, rounds_left, members=work),
                manifest_inline=sub,
                capability_ceiling=[],  # the router is granted NO capability
            )
        except HarnessClientError:
            return []  # router unreachable → give up this round; the coded done-check decides
        return _parse_next_roles(out.get("output"), allowed=set(work))

    return coordinate


# ── ADR-043 #553: bounded recalibration — the BYOM directive turn ──────────────────────────────
# Engine default cap: ONE recovery attempt before halting (ADR-043: 1-2, max 3). A constant (no
# manifest field), so the cap is stable across a HITL resume; only the recalibration COUNT persists.
_RECALIBRATION_CAP = 1
_RECAL_ACTIONS = ("re-plan", "re-frame-objective", "change-strategy", "re-scope-member", "escalate")


def _recalibration_subharness(team: OHMManifest) -> dict[str, Any]:
    """A tool-LESS single-agent harness for the recalibration turn — the model PICKS one tactic from
    the closed set given a CODED diagnosis (it never diagnoses itself). Same BYOM model + zero
    capabilities as the coordinator (ADR-008 / ADR-043 invariant)."""
    doc = _coordinator_subharness(team)
    doc["metadata"]["name"] = "loop-recalibrator"
    doc["prompts"][0]["body"] = (
        "You recalibrate a STALLED team loop. Given a coded diagnosis, reply with ONE action token "
        "from the allowed set followed by the member roles to retry. Reply with nothing else."
    )
    return doc


def _render_recalibration_prompt(loop: OHMLoop, diag: Diagnostic, work: list[str]) -> str:
    """The recalibrator's input — the CODED, external diagnosis (NO raw member outputs) + the closed
    action menu. Leak-safe: only role names + coverage/grade signals, never any produced content."""
    lines = [
        "A team loop has STALLED (it stopped making progress). Diagnosis (coded, external):",
        f"  - stall kind: {diag.stall_kind}",
        f"  - members not yet produced: {', '.join(diag.missing_members) or '(none)'}",
        f"  - members that FAILED: {', '.join(diag.failed_members) or '(none)'}",
    ]
    if diag.artifacts_landed is not None:
        lines.append(f"  - work persisted to the graph: {'yes' if diag.artifacts_landed else 'no'}")
    if diag.evaluator_score is not None:
        floor = diag.evaluator_floor if diag.evaluator_floor is not None else "?"
        lines.append(f"  - evaluator grade: {diag.evaluator_score} (needs >= {floor})")
    lines += [
        "",
        "Pick ONE recovery action from this CLOSED set:",
        "  - re-plan — redo the approach, same objective",
        "  - re-frame-objective — restate the goal more concretely",
        "  - change-strategy — try a different method",
        "  - re-scope-member — narrow a member's task",
        "  - escalate — give up and ask a human (only when truly stuck)",
        "",
        "Members you may retry: " + (", ".join(work) or "(none)"),
        "Reply with ONLY the action token then the roles to retry (space-separated).",
        "Example: change-strategy " + (work[0] if work else "member"),
    ]
    return "\n".join(lines)


def _parse_recalibration_output(output: Any, *, allowed: set[str]) -> RecalDirective:
    """Parse the recalibrator's reply into ONE directive, FAIL-CLOSED: an unparseable / empty reply,
    NO recognised action, an explicit ``escalate``, OR an AMBIGUOUS reply (two different actions) →
    ``escalate`` (never a silent no-op retry, never first-token-wins over a hedged escalate). Only
    declared work members survive as targets (a hallucinated outsider, and the matched action token
    itself, are dropped). Never logs the model output."""
    if not isinstance(output, str) or not output.strip():
        return RecalDirective(action="escalate", reason="unparseable")
    raw = [t.strip().strip("\"'`.") for t in output.replace(",", " ").split()]
    # normalise each token to the canonical hyphenated form (models emit re_plan / RE-PLAN / …)
    norm = [t.lower().replace("_", "-") for t in raw]
    actions = list(dict.fromkeys(t for t in norm if t in _RECAL_ACTIONS))  # distinct, in order
    # fail-closed: no action, an explicit escalate, OR ambiguity (>1 distinct action) → escalate
    if not actions or "escalate" in actions or len(actions) > 1:
        reason = "no_action" if not actions else "ambiguous_or_escalate"
        return RecalDirective(action="escalate", reason=reason)
    action = actions[0]
    # targets = declared work members, EXCLUDING the matched action token (a role named like an
    # action can't double as both — the collision the closed set would otherwise hide), de-duped
    targets = list(
        dict.fromkeys(r for r, n in zip(raw, norm, strict=True) if r in allowed and n != action)
    )
    return RecalDirective(action=action, reason="byom", member_targets=targets)  # type: ignore[arg-type]


def make_recalibration_coordinator(harness: _Harness, team: OHMManifest) -> RecalibrateFn:
    """Build the BYOM recalibrator (ADR-043 #553) — a bounded, tool-less model turn that, on a loop
    stall, PICKS one tactic from the closed action set given a CODED diagnosis (it never diagnoses
    itself; the model only chooses, the coded done-check still rules). Leak-safe (structure + coded
    signals, never content), fail-closed (an unreachable/garbled router yields ``escalate`` or
    ``None`` → halt-to-human, never a silent retry)."""
    sub = _recalibration_subharness(team)
    by_role = {m.role: m for m in team.members}

    async def recalibrate(loop: OHMLoop, diag: Diagnostic) -> RecalDirective | None:
        # retry only WORK members — a kind:human gate is re-rendered by the seam, never retried
        work = [r for r in loop.members if not (by_role.get(r) and by_role[r].kind == "human")]
        try:
            out = await harness.execute(
                input_text=_render_recalibration_prompt(loop, diag, work),
                manifest_inline=sub,
                capability_ceiling=[],  # the recalibrator is granted NO capability
            )
        except HarnessClientError:
            return None  # router unreachable → halt fail-closed (no_progress), never a silent retry
        return _parse_recalibration_output(out.get("output"), allowed=set(work))

    return recalibrate
