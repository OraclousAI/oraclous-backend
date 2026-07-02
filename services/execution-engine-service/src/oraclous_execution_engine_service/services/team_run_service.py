"""Team-run service (services layer) — the durable, reachable entry point for running an
OHM v1.1 Team Harness.

This is the front door the orchestrator (``oraclous_ohm.orchestrate.run_team``) lacked. The request
path (``create``/``advance``) validates + persists a ``engine_team_runs`` row + ENQUEUES it (202);
the WORKER (``drive``, called from ``run_tasks.drive_team_run_task``) claims it QUEUED→RUNNING and
drives the member DAG through the REAL harness (``run_team_harness`` → ``HarnessClient.execute`` per
member, the typed hand-off envelopes threaded), persisting the outcome — so a 30-agent team never
blocks/times out the request (same async pattern as jobs/round-tables). A human gate pauses the run
durably (state ``PAUSED`` + ``paused_at``); ``advance`` records the decision, returns it to QUEUED,
and re-enqueues the worker to resume past it (re-using persisted results — G-D). A member whose
harness does not succeed fails the run (fail-closed); a stranded RUNNING run is swept by the reaper.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oraclous_governance import Principal
from oraclous_ohm.capabilities import assert_subharness_within_ceiling
from oraclous_ohm.dag import revision_invalidation_set
from oraclous_ohm.errors import OHMCapabilityError, OHMError
from oraclous_ohm.gate import GateDecision, gate_verb
from oraclous_ohm.gate_battery import (
    OHMGateCheck,
    UnknownBattery,
    evaluate_gate,
    is_battery_reference,
    resolve_battery,
)
from oraclous_ohm.manifest import OHMLoop, OHMManifest
from oraclous_ohm.orchestrate import DoneCheckFn, TeamRunResult
from oraclous_ohm.parse import load_ohm

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.domain import verdict_consumption as vc
from oraclous_execution_engine_service.domain.refresh import (
    REFRESH_SEED_KEY,
    compute_delta,
    parse_records,
)
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.repositories.maintenance_repository import (
    EngineMaintenanceRepository,
)
from oraclous_execution_engine_service.repositories.schedule_repository import ScheduleRepository
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.artifacts_client import (
    ArtifactsClient,
    ArtifactsClientError,
)
from oraclous_execution_engine_service.services.evaluate_client import (
    EvaluateClient,
    EvaluateClientError,
)
from oraclous_execution_engine_service.services.graph_client import GraphClient, GraphClientError
from oraclous_execution_engine_service.services.harness_client import HarnessClient
from oraclous_execution_engine_service.services.team_run import (
    make_loop_coordinator,
    make_recalibration_coordinator,
    run_team_hybrid,
)

# orchestrator status -> persisted team-run state. ADR-042 (#551): "failed" (one or more members
# did not deliver — the non-aborting failure path now records per-member status instead of raising)
# maps to FAILED; the failed+blocked members are re-runnable (POST .../rerun).
_STATUS_TO_STATE = {
    "completed": "SUCCEEDED",
    "paused": "PAUSED",
    "rejected": "REJECTED",
    "failed": "FAILED",
    # #585 (ADR-031 §D3): a pooled-budget halt is a GOVERNED terminal, NOT FAILED — a controlled
    # halt (like the max_wall deadline), healthy, whose "budget_skipped" members are un-attempted.
    "cost_budget": "COST_BUDGET",
}

# (team_run_id, organisation_id, user_id) -> None — hands a QUEUED run to the worker (broker).
EnqueueFn = Callable[[uuid.UUID, uuid.UUID, uuid.UUID], None]

# #604 closed-loop verdict-consumption (ADR-048 dec 5). The bounds that make the loop TERMINATE
# (three independent, fail-closed guards — a closed loop MUST end): the re-dispatch ceiling,
# the livelock score-improvement epsilon, and the #585 pool (COST_BUDGET, decided in the domain).
_MAX_RE_DISPATCHES = 3
_LIVELOCK_EPSILON = 0.02
# the member_status marker that FORCES a member to re-run on a re_task (it is not "succeeded"/
# "partial", so ``_completed_for_resume`` does NOT seed it → the member re-dispatches).
_RE_TASK_MARKER = "re_task"
# the sentinel role stamped in ``paused_at`` when a settled run is ESCALATED on its verdict — a
# human-readable marker on the surface (distinguishable from a mid-drive gate role). The CONTROL
# decision (does ``advance`` re-task vs record a gate?) keys off ``escalation_kind`` below, NOT this
# member-role list — so a tenant that names a member the sentinel cannot hijack the resume path.
_VERDICT_ESCALATION_ROLE = "__verdict_escalation__"
# the ``escalation_kind`` value that marks a verdict-escalation PAUSE (the Q3 resume discriminator).
_VERDICT_ESCALATION_KIND = "verdict"


class TeamRunError(Exception):
    """A client-facing team-run failure carrying an HTTP status (mapped in the route).

    ``error_type`` is a leak-safe machine token (never a value) the route surfaces in a STRUCTURED
    422 detail so the gateway maps it to VALIDATION_FAILED + a field-level issue (#483
    Option A) instead of the misleading MALFORMED_REQUEST a free-string detail falls back to. Only
    used for 422s; other statuses keep a plain string detail."""

    def __init__(
        self, message: str, status_code: int = 400, *, error_type: str = "team_run_invalid"
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


#: operator-configured org-scoped workspaces root (MUST match the capability-registry sandbox guard,
#: #517). A team's ``workspace_root`` is validated fail-fast at create against ``<root>/<org>`` so a
#: bad root is a clear 422 here, not a confusing mid-run member failure (defense-in-depth: the
#: registry tool-level guard remains the authoritative boundary). Override per deployment.
_WORKSPACES_ROOT = Path(
    os.environ.get("ORACLOUS_WORKSPACES_ROOT", "/tmp/oraclous-agent-workspaces")  # noqa: S108
)


def _validate_workspace_root(organisation_id: uuid.UUID, workspace_root: str) -> None:
    """Fail-fast org-scoped check (mirrors #517): ``workspace_root`` MUST resolve to the org's
    workspaces root (``WORKSPACES_ROOT/<org>``) or a path under it. A system path, a path outside
    the root, or another org's subtree raises a 422. The org segment is the authenticated org (never
    user input), so a tenant cannot target another tenant's tree."""
    org_root = (_WORKSPACES_ROOT / str(organisation_id)).resolve()
    candidate = Path(workspace_root).resolve()
    if candidate != org_root and org_root not in candidate.parents:
        raise TeamRunError(
            "workspace_root must resolve under the org's allowed workspaces root",
            422,
            error_type="invalid_workspace_root",
        )


@dataclass(frozen=True)
class TeamRunStatus:
    """The O4 light-status value object (ADR-037 Decision 5 / #472) the route maps to its DTO."""

    team_run_id: uuid.UUID
    organisation_id: uuid.UUID
    healthy: bool
    state: str
    progress: int
    last_run_at: datetime | None
    last_outcome: str
    cost_tokens: int


def _verdict_score(verdict: Any) -> float | None:
    """A 0–1 attainment from a stored verdict (#477): a prose Verdict's ``score``, or a battery
    verdict's passed-fraction over its checks. ``None`` when absent/unparseable (fail-closed)."""
    if not isinstance(verdict, dict):
        return None
    score = verdict.get("score")
    if isinstance(score, int | float) and not isinstance(score, bool):
        return max(0.0, min(1.0, float(score)))
    checks = verdict.get("check_verdicts")
    if isinstance(checks, list) and checks:
        passed = sum(1 for c in checks if isinstance(c, dict) and c.get("passed"))
        return passed / len(checks)
    return None


def _sink_roles(team: OHMManifest, results: dict[str, Any]) -> set[str]:
    """#604: the deliverable-producer (sink) members — those nothing depends on, present in
    ``results``. re_task forces these to re-run so a SUCCEEDED-but-below-threshold run regenerates
    output. (Mirrors #602's sink identification.)"""
    depended = {d for m in team.members for d in m.depends_on}
    return {m.role for m in team.members if m.role not in depended and m.role in results}


def _verdict_reason(verdict: Any) -> str | None:  # noqa: ANN401
    """The grader's leak-safe reason (a quality assessment of the output, not the output itself),
    truncated — carried into the re_task revision directive so the member has feedback."""
    if isinstance(verdict, dict) and isinstance(verdict.get("reason"), str):
        return verdict["reason"][:200]
    return None


def _append_revision_directive(
    manifest: dict[str, Any], faulted: set[str], note: str
) -> dict[str, Any]:
    """Append a revision ``note`` to each faulted member's subgoal (+ the ``handoff_objective`` of
    every producer feeding a faulted member, so a hand-off-rendered sink sees it too) in a DEEP COPY
    of the manifest — the re-dispatched task DIFFERS from the prior (never a blind re-run). Deep-
    copied via a JSON round-trip (the manifest is JSONB). Shared by the #604 verdict re_task and the
    ADR-046 (#578) human revise; the caller supplies the (bounded) ``note`` phrasing."""
    revised: dict[str, Any] = json.loads(json.dumps(manifest, default=str))
    members = revised.get("members", [])
    for member in members:
        if isinstance(member, dict) and member.get("role") in faulted:
            member["subgoal"] = (member.get("subgoal") or "") + note
    # review F1-team_run: a faulted member RENDERS its objective from an upstream producer's
    # ``handoff_objective`` (``render_member_input``'s ``objective_slice`` takes PRECEDENCE over the
    # member's static subgoal in a ## Handoff pipeline). So the subgoal note alone never reaches the
    # harness input for a handoff-driven sink — a blind identical re-run. Also append the note to
    # the (non-empty) ``handoff_objective`` of every producer feeding a faulted member, so it lands
    # in the sink's rendered input (the empty-handoff path the subgoal note already covers; on the
    # non-empty path this closes the override gap).
    faulted_deps = {
        dep
        for member in members
        if isinstance(member, dict) and member.get("role") in faulted
        for dep in (member.get("depends_on") or [])
    }
    for member in members:
        if (
            isinstance(member, dict)
            and member.get("role") in faulted_deps
            and member.get("handoff_objective")
        ):
            member["handoff_objective"] = member["handoff_objective"] + note
    return revised


def _revise_manifest(
    manifest: dict[str, Any], faulted: set[str], reason: str | None, attempt: int
) -> dict[str, Any]:
    """#604: the VERDICT re_task directive — the prior output graded below threshold. Bounded by
    MAX_RE_DISPATCHES (attempt) so the subgoal cannot grow unboundedly."""
    note = (
        f" [Re-task attempt {attempt}] The prior output was graded BELOW the success threshold"
        + (f": {reason}" if reason else "")
        + ". Revise your output to address this."
    )
    return _append_revision_directive(manifest, faulted, note)


def _human_revision_note(feedback: str, revision_round: int) -> str:
    """ADR-046 (#578): the HUMAN revise directive threaded into the invalidated producers — the
    reviewer's own words (bounded so repeated revisions can't grow the subgoal unboundedly; the
    max_revisions cap bounds the count). A data-only directive, never a tool/capability change."""
    trimmed = feedback.strip()[:500]
    return (
        f" [Revision {revision_round}] A human reviewer sent this back for revision"
        + (f": {trimmed}" if trimmed else "")
        + ". Revise your output to address this feedback."
    )


def _decisions_to_jsonb(decisions: Mapping[str, Any]) -> dict[str, str]:
    """Normalize a gate-decision map (``GateDecision`` | a v1 bare string | a persisted value) to
    the JSONB shape stored on ``gate_decisions`` — the bare VERB per gate (``approve`` | ``revise``
    | ``reject``). The verb is all ``run_team`` needs (re-pause / cross / terminate); a ``revise``'s
    ``feedback`` + ``edited_payload`` are consumed at advance time (threaded into the manifest /
    seeded into results), NOT persisted here — so the stored shape stays a bare string, identical to
    v1 (fully back-compatible; existing rows + tests unchanged)."""
    out: dict[str, str] = {}
    for role, decision in decisions.items():
        verb = gate_verb(decision)
        if verb is not None:
            out[role] = verb
    return out


# ADR-046 (#578): the default cap on how many times a human gate may be REVISED before the run
# fail-closes to terminal REJECTED (a team may override via OHMTermination.max_revisions). The
# pooled #585 OHMBudget is the outer bound regardless.
_MAX_REVISIONS_DEFAULT = 3


def _resolve_max_revisions(team: OHMManifest) -> int:
    term = team.orchestration.termination if team.orchestration else None
    configured = term.max_revisions if term is not None else None
    return int(configured) if configured else _MAX_REVISIONS_DEFAULT


def _member_completion_progress(row: EngineTeamRun) -> int:
    """Goal-attainment progress (ADR-037 Decision 5), 0–100. Base = member completion (the fraction
    of declared members whose node reached a terminal result). When a flow-evaluation verdict is
    stored (#477), the evaluator partial is the PRIMARY signal, capped by member completion so it
    never reports ahead of the work actually done. Fail-closed: no/unparseable verdict → pure member
    completion; no members → 100 only once SUCCEEDED."""
    members = row.manifest.get("members", []) if isinstance(row.manifest, dict) else []
    total = len(members)
    # ADR-042 (#551): count DELIVERED members, NOT len(results). The non-aborting failure path now
    # populates results[role]=None for a failed/blocked member, so len(results) would count it as
    # complete and report a FAILED run at ~100%. Base completion on per-member status (succeeded,
    # plus a declared-no-op "skipped"); fall back to len(results) only for pre-ADR-042 rows with no
    # recorded member_status (mirrors the _completed_for_resume back-compat).
    ms = row.member_status or {}
    delivered = (
        # #587: a "partial" (degraded) member DELIVERED its best-effort output (downstream consumed
        # it) — count it delivered, like succeeded/skipped; else a SUCCEEDED degrade run is <100%.
        sum(1 for s in ms.values() if s in ("succeeded", "skipped", "partial"))
        if ms
        else len(row.results or {})
    )
    completion = (
        (100 if row.state == "SUCCEEDED" else 0)
        if total == 0
        else min(100, round(100 * delivered / total))
    )
    score = _verdict_score(row.verdict)
    if score is None:
        return completion
    return min(round(100 * score), completion)  # the evaluator partial, capped by work-done


def _grade_target(team: OHMManifest, results: dict[str, Any]) -> str:
    """Reduce the per-member results to ONE string to grade — the team's terminal (sink) members'
    output (the roles no other member depends on). One sink → its output; several → a deterministic
    JSON of the sink subset; none identifiable → a JSON of all results (fail-safe, never empty)."""
    depended = {d for m in team.members for d in m.depends_on}
    sinks = [m.role for m in team.members if m.role not in depended and m.role in results]
    if len(sinks) == 1:
        out = results.get(sinks[0])
        return out if isinstance(out, str) else json.dumps(out, default=str, sort_keys=True)
    chosen = {r: results[r] for r in (sinks or list(results))}
    return json.dumps(chosen, default=str, sort_keys=True)


def _refresh_records(team: OHMManifest, results: dict[str, Any]) -> list[dict[str, Any]] | None:
    """#602: the producing (sink) member's deliverable parsed into records for the 5-way delta. A
    single sink → its output UNWRAPPED (the engine wraps a member's dispatch result as
    ``{"output": <raw>, ...}``) → parsed as a JSON record array. Multiple/no sinks, or a deliverable
    that is not a JSON record-set → None (no per-record delta)."""
    depended = {d for m in team.members for d in m.depends_on}
    sinks = [m.role for m in team.members if m.role not in depended and m.role in results]
    if len(sinks) != 1:
        return None
    out: Any = results.get(sinks[0])
    if isinstance(out, dict) and "output" in out:  # unwrap the producer's wrapped dispatch result
        out = out["output"]
    return parse_records(out)


def thread_refresh_seed(
    seed_team: OHMManifest, seed_results: dict[str, Any], inputs: dict[str, Any] | None
) -> dict[str, Any]:
    """#602/#544: thread a prior SUCCEEDED run's producing-member records into ``inputs`` under the
    reserved ``_refresh_seed`` key (the #599 state seam). At dispatch ``refresh_dispatch_args``
    renders these into the SINK member's harness input with the carry-forward directive (the cost
    lever — the member skips re-deriving unchanged records); at settle they are the baseline the
    5-way delta is computed against. The shared threading both the request path (``_seed_refresh``)
    and the scheduled recurring-refresh path
    (``schedule_service._fire_team_run``) reuse — each caller owns the seed-run VALIDATION (request
    path → 422; a Beat fire → fail-open to a cold build). A seed whose deliverable is not records
    parses to None → thread ``[]`` but flag it, so the delta never treats an UNPARSEABLE seed as a
    genuinely-empty one (which would misreport every fresh record as spuriously ``added``)."""
    seed_records = _refresh_records(seed_team, seed_results)
    return {
        **(inputs or {}),
        REFRESH_SEED_KEY: {
            "records": seed_records or [],
            "id_field": "id",
            "seed_records_parsed": seed_records is not None,
        },
    }


_CONVERGENCE_RE = re.compile(r"\s*evaluator\s*(>=|<=|==|>|<)\s*([0-9]*\.?[0-9]+)\s*\Z")


def _parse_convergence(expr: str) -> tuple[str, float]:
    """Parse a loop convergence threshold ``"evaluator>=0.8"`` → ``(">=", 0.8)``. Raises
    ``ValueError`` on a present-but-malformed expr — the caller (only invoked for a NON-empty expr)
    fail-closes that to not-converged, so a typo'd threshold never silently passes on coverage
    alone. An ABSENT threshold is handled by the caller (skip the evaluator gate), not here."""
    m = _CONVERGENCE_RE.match(expr)
    if m is None:
        raise ValueError(f"unparseable convergence threshold: {expr!r}")
    return m.group(1), float(m.group(2))


def _cmp(score: float, op: str, floor: float) -> bool:
    """Apply a parsed convergence comparator (fail-closed: an unknown op is False)."""
    return {
        ">=": score >= floor,
        "<=": score <= floor,
        "==": score == floor,
        ">": score > floor,
        "<": score < floor,
    }.get(op, False)


def _loop_grade_target(loop: OHMLoop, results: dict[str, Any]) -> str:
    """Reduce a loop's per-member results to ONE string to grade — a deterministic JSON of the loop
    members' outputs (never empty; the coverage floor already guaranteed each produced)."""
    chosen = {r: results.get(r) for r in loop.members}
    return json.dumps(chosen, default=str, sort_keys=True)


def _pre_run_artifact_count(artifacts: list[dict[str, Any]], run_created_at: datetime) -> int:
    """The RUN-SCOPED landed-artifacts baseline (CTO fast-follow): the count of artifacts that
    existed on the bound graph BEFORE this run was created. Keyed off ``run_created_at`` (the run's
    creation time, set once at create), NOT the current count — so it is IMMUTABLE across drives and
    an ADR-042 re-run is never wrongly blocked by its OWN prior drive's artifacts (which carry a
    created_at at/after the run). The coded done-check then requires NEW artifacts past this, so a
    warm / adopted graph's pre-existing artifacts cannot vacuously satisfy convergence. An artifact
    with a missing/unparseable timestamp is treated as this run's (not baseline) — safe for the
    common fresh-per-run graph (baseline 0)."""
    ref = run_created_at if run_created_at.tzinfo else run_created_at.replace(tzinfo=UTC)
    count = 0
    for artifact in artifacts:
        raw = artifact.get("created_at")
        if not isinstance(raw, str):
            continue
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < ref:
            count += 1
    return count


class TeamRunService:
    def __init__(
        self,
        *,
        team_runs: TeamRunRepository,
        harness: HarnessClient | None = None,
        enqueue: EnqueueFn | None = None,
        evaluate: EvaluateClient | None = None,
        graphs: GraphClient | None = None,
        artifacts: ArtifactsClient | None = None,
        schedules: ScheduleRepository | None = None,
    ) -> None:
        # The drive runs on the WORKER (like jobs/round-tables): the request path (create/advance)
        # needs `enqueue` (hand the QUEUED run to the broker) but NOT a harness; the worker `drive`
        # needs `harness` but not `enqueue`; the reaper path (reap_stale) needs neither. `evaluate`
        # (the flow judge, #477) is the worker's gate grader — None ⇒ no gate eval (the run still
        # completes; the gate is simply not graded). `graphs` (#524) is the request path's KGS
        # existence check for a graph-bound run — None ⇒ no fail-fast check (KGS RLS still scopes
        # the tools mid-run), so a wired client is how a cross-org graph_id is rejected at create.
        self._team_runs = team_runs
        self._harness = harness
        self._enqueue = enqueue
        self._evaluate = evaluate
        self._graphs = graphs
        self._artifacts = artifacts
        # #601: the worker drive accrues a SCHEDULED run's settled cost into its schedule's
        # per-cadence accumulator (the #598 cap reads it). None on the request path (create/advance
        # never settle cost) — a non-scheduled run accrues nothing.
        self._schedules = schedules

    def _org(self, principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:  # fail-closed tenancy (ADR-006)
            raise TeamRunError("authenticated principal has no organisation scope", 403)
        return principal.organisation_id

    def _load_team(self, document: dict) -> OHMManifest:
        try:
            manifest = load_ohm(document)
        except OHMError as exc:  # malformed / invalid OHM is a 422, not a 500
            raise TeamRunError(
                f"invalid OHM manifest: {exc}", 422, error_type="invalid_manifest"
            ) from exc
        if not manifest.is_team():
            raise TeamRunError(
                "manifest is not a Team Harness (metadata.kind must be 'team')",
                422,
                error_type="not_a_team",
            )
        if not manifest.members:
            raise TeamRunError(
                "a Team Harness must declare at least one member", 422, error_type="no_members"
            )
        # fail-fast (#479): a `battery:<name>` success_criteria must name a DECLARED battery —
        # resolve now so an undeclared one is a 422 at create, not an UnknownBattery that strands
        # the run at grade time. (The gate uses success_criteria for the single-pass DAG.)
        if manifest.orchestration is not None and is_battery_reference(
            manifest.orchestration.success_criteria
        ):
            try:
                resolve_battery(manifest, manifest.orchestration.success_criteria)
            except UnknownBattery as exc:
                raise TeamRunError(
                    f"success_criteria references an undeclared battery: {exc}",
                    422,
                    error_type="undeclared_battery",
                ) from exc
        return manifest

    def _enforce_member_ceilings(
        self, team: OHMManifest, sub_harnesses: Mapping[str, dict]
    ) -> None:
        """Fail-closed (ADR-032/035 §5): each provided sub-harness may only declare capabilities
        WITHIN its member's ``tools`` ceiling — the harness builds its policy ceiling from the
        sub-harness's own ``capabilities[]``, so an unchecked sub-harness would let a client widen a
        member past what it declared. Reject (422) any sub-harness that exceeds its member's ceiling
        or names an unknown role."""
        by_role = {m.role: m for m in team.members}
        for role, sub_doc in sub_harnesses.items():
            member = by_role.get(role)
            if member is None:
                raise TeamRunError(
                    f"sub_harness for unknown member role '{role}'",
                    422,
                    error_type="unknown_member_role",
                )
            try:
                sub = load_ohm(sub_doc)
            except OHMError as exc:
                raise TeamRunError(
                    f"invalid sub_harness for '{role}': {exc}",
                    422,
                    error_type="invalid_sub_harness",
                ) from exc
            try:
                assert_subharness_within_ceiling(member, sub)
            except OHMCapabilityError as exc:
                raise TeamRunError(
                    f"sub_harness for '{role}' exceeds its tools ceiling: {exc}",
                    422,
                    error_type="ceiling_exceeded",
                ) from exc

    async def _validate_graph_id(self, organisation_id: uuid.UUID, graph_id: str) -> None:
        """Fail-fast org-scoped check (#524, ADR-040 Decision 7): the bound ``graph_id`` MUST exist
        in the caller's organisation. The KGS GET is org-scoped by the engine's downstream headers,
        so a graph the org does not own returns 404 → a clear 422 here, not a confusing mid-run
        member failure (defense-in-depth: the registry tool-level KGS RLS remains authoritative).
        With no ``graphs`` client wired the check is skipped (RLS still scopes the tools)."""
        if self._graphs is None:
            return
        try:
            exists = await self._graphs.graph_exists(graph_id)
        except GraphClientError as exc:  # KGS unreachable / inconclusive — fail closed (not admit)
            raise TeamRunError(
                "could not validate graph_id against the knowledge-graph-service",
                502,
                error_type="graph_validation_failed",
            ) from exc
        if not exists:
            raise TeamRunError(
                "graph_id does not exist in your organisation",
                422,
                error_type="invalid_graph_id",
            )

    async def _seed_refresh(
        self,
        organisation_id: uuid.UUID,
        seed_from_run_id: uuid.UUID,
        inputs: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """#602: validate the named seed run (fail-fast 422) + thread its records into ``inputs``
        under the reserved ``_refresh_seed`` key (the #599 state seam); at dispatch the sink member
        receives them to carry-forward unchanged records (the cost lever) and at settle they are the
        delta baseline. The seed run must belong to the caller's org (a cross-org id → 422, never a
        tenant leak) and be SUCCEEDED (a partial/failed prior has an incomplete ledger)."""
        with org_scope(organisation_id):
            seed_row = await self._team_runs.get(seed_from_run_id, organisation_id)
        if seed_row is None:
            raise TeamRunError(
                "seed_from_run_id does not name a run in your organisation",
                422,
                error_type="invalid_seed_run",
            )
        if seed_row.state != "SUCCEEDED":
            raise TeamRunError(
                f"seed_from_run_id must be a SUCCEEDED run (is {seed_row.state})",
                422,
                error_type="invalid_seed_run",
            )
        return thread_refresh_seed(self._load_team(seed_row.manifest), seed_row.results, inputs)

    def _refresh_delta(
        self, team: OHMManifest, row: EngineTeamRun, result: TeamRunResult
    ) -> dict[str, Any]:
        """#602: the first-class 5-way what-changed delta, computed at settle comparing this run's
        records to the seed run's (threaded at create under ``_refresh_seed``). Only called for a
        completed refresh run. A deliverable that is not a JSON record-set yields an explicit note,
        never a false empty delta."""
        seed_meta = (row.inputs or {}).get(REFRESH_SEED_KEY) or {}
        seed_records = seed_meta.get("records") or []
        id_field = seed_meta.get("id_field") or "id"
        fresh = _refresh_records(team, result.results)
        if fresh is None:
            return {
                "seed_from_run_id": str(row.seed_from_run_id),
                "records_parsed": False,
                "note": "the refresh deliverable is not a JSON record-set; no per-record delta",
            }
        delta = compute_delta(seed_records, fresh, id_field=id_field)
        delta["seed_from_run_id"] = str(row.seed_from_run_id)
        # surface an UNPARSEABLE seed (records default []): every fresh record then reads ``added``,
        # so the consumer can tell that from a genuinely-empty prior ledger (never silent).
        delta["seed_records_parsed"] = seed_meta.get("seed_records_parsed", True)
        return delta

    async def create(
        self,
        principal: Principal,
        *,
        manifest: dict,
        sub_harnesses: dict[str, dict],
        gate_decisions: Mapping[str, GateDecision],
        workspace_root: str | None = None,
        graph_id: str | None = None,
        inputs: dict[str, Any] | None = None,
        seed_from_run_id: uuid.UUID | None = None,
    ) -> EngineTeamRun:
        """Request path: validate + persist a QUEUED run + hand it to the worker (202). The drive
        runs on the worker so a large team (30 agents) never blocks/times out the HTTP request."""
        org = self._org(principal)
        team = self._load_team(manifest)  # validate BEFORE persisting
        self._enforce_member_ceilings(team, sub_harnesses)  # ADR-032/035 §5 — fail-closed ceiling
        if (
            workspace_root is not None
        ):  # file-native (#518): org-scoped, fail-fast 422 (not mid-run)
            _validate_workspace_root(org, workspace_root)
        if (
            graph_id is not None
        ):  # graph substrate (#524): org-scoped, fail-fast 422 (cross-org graph rejected here)
            await self._validate_graph_id(org, graph_id)
        if seed_from_run_id is not None:  # #602 seeded-refresh: fail-fast 422 + thread the seed in
            inputs = await self._seed_refresh(org, seed_from_run_id, inputs)
        with org_scope(org):  # bind the org-GUC so the RLS-backstopped INSERT is admitted (ADR-030)
            row = await self._team_runs.create(
                organisation_id=org,
                user_id=principal.principal_id,
                manifest=manifest,
                sub_harnesses=sub_harnesses,
                gate_decisions=_decisions_to_jsonb(gate_decisions),
                workspace_root=workspace_root,
                graph_id=graph_id,
                inputs=inputs,
                seed_from_run_id=seed_from_run_id,
            )
        if self._enqueue is not None:
            self._enqueue(row.id, org, principal.principal_id)
        return row  # QUEUED — the worker drives it

    async def drive(self, team_run_id: uuid.UUID, principal: Principal) -> EngineTeamRun:
        """Worker entrypoint: claim the QUEUED run and drive its member DAG through the harness. A
        resume re-uses the persisted results (G-D — completed members not re-run). Single-driver:
        a redelivered task that finds it no longer QUEUED no-ops (the CAS claim in ``_drive``)."""
        org = self._org(principal)
        with org_scope(org):
            row = await self._team_runs.get(team_run_id, org)
        if row is None:
            raise TeamRunError("team run not found", 404)
        team = self._load_team(row.manifest)
        return await self._drive(row, team, org, completed=self._completed_for_resume(row))

    @staticmethod
    def _completed_for_resume(row: EngineTeamRun) -> dict[str, Any]:
        """Which members to SEED on a (re-)drive so they are not re-dispatched (G-D). ADR-042
        (#551): seed the members whose terminal status is "succeeded" OR "partial" — so a re-run
        re-runs the FAILED + BLOCKED members (their results are None, never a real output) while the
        succeeded ones AND the #587 DEGRADED ("partial") members (which FINISHED with best-effort
        output, terminal/done) are reused — never re-dispatched (no token re-spend, no side-effect
        re-fire). Back-compat: an in-flight PAUSED run created before member_status existed carries
        an empty member_status; fall back to all results so its gate-resume reuses the pre-gate
        members (which are the only ones with a result on a PAUSED row)."""
        member_status = row.member_status or {}
        if not member_status:
            return dict(row.results or {})  # pre-ADR-042 resume semantics (in-flight PAUSED rows)
        return {
            role: row.results[role]
            for role, status in member_status.items()
            if status in ("succeeded", "partial") and role in (row.results or {})
        }

    async def get(self, team_run_id: uuid.UUID, principal: Principal) -> EngineTeamRun:
        org = self._org(principal)
        with org_scope(org):
            row = await self._team_runs.get(team_run_id, org)
        if row is None:
            raise TeamRunError("team run not found", 404)
        return row

    async def status(self, team_run_id: uuid.UUID, principal: Principal) -> TeamRunStatus:
        """O4 light status (ADR-037 Decision 5 / #472): a one-glance health/progress/cost view.
        Reads through the SAME request-path org-scoped ``get`` (H3 — NOT the cross-org maintenance
        reader), so a cross-org id is a 404, never a leak. ``progress`` is goal-attainment by member
        completion of the run-tree (0–100), replacing the old hardcoded 5/100."""
        row = await self.get(team_run_id, principal)
        return TeamRunStatus(
            team_run_id=row.id,
            organisation_id=row.organisation_id,
            healthy=row.state
            != "FAILED",  # FAILED is unhealthy; QUEUED/RUNNING/PAUSED/SUCCEEDED ok
            state=row.state,
            progress=_member_completion_progress(row),
            last_run_at=row.created_at,
            last_outcome=row.state,
            cost_tokens=int(row.cost_tokens or 0),
        )

    # ── #604 closed-loop verdict-consumption (ADR-048 decision 5) ─────────────────────────────────
    async def _consume_verdict(
        self, row: EngineTeamRun, team: OHMManifest, org: uuid.UUID
    ) -> EngineTeamRun:
        """Branch a just-settled run on its STORED verdict — STORE_ONLY (unchanged), RE_TASK
        (autonomous re-dispatch of the faulted members, revised objective), or ESCALATE (PAUSED
        for HITL). Fail-closed decision (``domain.verdict_consumption``). Runs POST-settle in the
        worker ``_drive`` so the verdict + member_status are durable before any re-dispatch."""
        action = vc.decide_action(
            row.verdict,
            run_state=row.state,
            re_dispatch_count=int(row.re_dispatch_count or 0),
            last_verdict_score=row.last_verdict_score,
            last_verdict_fingerprint=row.last_verdict_fingerprint,
            max_re_dispatches=_MAX_RE_DISPATCHES,
            livelock_epsilon=_LIVELOCK_EPSILON,
        )
        if action == vc.RE_TASK:
            return await self._re_task(row, team, org)
        if action == vc.ESCALATE:
            return await self._escalate_verdict(row, org)
        return row  # STORE_ONLY — the run cleared (or has no gate); unchanged

    async def _re_task(
        self, row: EngineTeamRun, team: OHMManifest, org: uuid.UUID
    ) -> EngineTeamRun:
        """#604 re_task: CAS the settled terminal → QUEUED + re-enqueue the faulted members with a
        REVISED objective (never a blind identical re-run), drawing the ACCUMULATING #585 pool (the
        persisted ``cost_tokens`` is never reset, so the pool bounds the loop). Compensates the
        re-dispatch enqueue QUEUED→FAILED on a broker fault (the #620 phantom-QUEUED class — a
        re-dispatch orphan would be worse)."""
        faulted = self._faulted_roles(row, team)
        if not faulted:
            # review F2: nothing to re-run (a degenerate/cyclic manifest with no sink in results). A
            # re-drive would seed every member complete and re-settle with the IDENTICAL verdict,
            # burning one drive + its tokens before the livelock guard halts it. Escalate now
            # (fail-closed — the same terminal the bound would reach, without the wasted re-drive).
            return await self._escalate_verdict(row, org)
        count = int(row.re_dispatch_count or 0)
        manifest = _revise_manifest(row.manifest, faulted, _verdict_reason(row.verdict), count + 1)
        member_status = {
            role: (_RE_TASK_MARKER if role in faulted else status)
            for role, status in (row.member_status or {}).items()
        }
        for role in faulted:  # a faulted role with no prior status still force-re-runs
            member_status.setdefault(role, _RE_TASK_MARKER)
        loop = vc.next_loop_state(row.verdict, count)
        with org_scope(org):  # CAS terminal→QUEUED (single-driver; a redelivered settle is a no-op)
            claimed, applied = await self._team_runs.transition(
                row.id,
                org,
                new_state="QUEUED",
                # only a SUCCEEDED run carries a below-threshold verdict (the sole re_task trigger);
                # FAILED is defensive. NOT COST_BUDGET — decide_action STORE_ONLYs a pool-exhausted
                # run, so re_task-ing into an empty pool is unreachable, and refused if it isn't.
                allowed_from=frozenset({"SUCCEEDED", "FAILED"}),
                manifest=manifest,  # the revised objective(s) on the faulted members
                member_status=member_status,  # faulted → _RE_TASK_MARKER (force re-run)
                error_message=None,
                re_dispatch_count=loop["re_dispatch_count"],
                last_verdict_score=loop["last_verdict_score"],
                last_verdict_fingerprint=loop["last_verdict_fingerprint"],
            )
        if not applied or claimed is None:
            return row
        if self._enqueue is not None:
            try:
                self._enqueue(row.id, org, row.user_id)
            except Exception:
                # review F1-org_scope: the compensation MUST bind the org-GUC — under the RLS
                # backstop (ADR-030) an unscoped UPDATE fail-closes to a silent no-op, leaving the
                # run phantom-QUEUED (the exact #620 wedge this compensation exists to prevent).
                with org_scope(org):
                    await self._team_runs.transition(
                        row.id,
                        org,
                        new_state="FAILED",
                        allowed_from=frozenset({"QUEUED"}),
                        error_message="re_dispatch enqueue failed",
                    )
                raise
        return claimed

    async def _escalate_verdict(self, row: EngineTeamRun, org: uuid.UUID) -> EngineTeamRun:
        """#604 escalate: PAUSE a settled below-threshold run for HITL. ``escalation_kind``=verdict
        is the CONTROL marker ``advance`` keys off to re-task (never a blind re-drive of the seeded
        run — Q3); the ``paused_at`` sentinel is a human-readable surface marker only. NO enqueue —
        a human resumes via ``advance``."""
        with org_scope(org):
            claimed, applied = await self._team_runs.transition(
                row.id,
                org,
                new_state="PAUSED",
                allowed_from=frozenset({"SUCCEEDED", "FAILED"}),
                paused_at=[_VERDICT_ESCALATION_ROLE],
                escalation_kind=_VERDICT_ESCALATION_KIND,
            )
        return claimed if applied and claimed is not None else row

    def _faulted_roles(self, row: EngineTeamRun, team: OHMManifest) -> set[str]:
        """The members re_task forces to re-run: the FAILED/BLOCKED members (member_status) PLUS the
        SINK (deliverable-producer) member(s) — so a SUCCEEDED-but-below-threshold run (all members
        'succeeded') still REGENERATES its output instead of re-seeding everything complete (a no-op
        spin the pool would drain)."""
        failed = {r for r, s in (row.member_status or {}).items() if s in ("failed", "blocked")}
        return failed | _sink_roles(team, row.results or {})

    async def _resume_verdict_escalation(
        self, row: EngineTeamRun, org: uuid.UUID, principal: Principal
    ) -> EngineTeamRun:
        """#604 Q3: a human advancing a VERDICT-escalated PAUSED run re-tasks the faulted members
        a FRESH loop counter (a human-initiated attempt) — NEVER a blind re-drive of the seeded-
        run. PAUSED→QUEUED + re-enqueue (the shipped resume path), compensated on a fault."""
        team = self._load_team(row.manifest)
        faulted = self._faulted_roles(row, team)
        if (
            not faulted
        ):  # review F2 (symmetry): nothing to re-run — leave it PAUSED (a no-op advance)
            return row
        manifest = _revise_manifest(row.manifest, faulted, "a human re-opened the run", 1)
        member_status = {
            role: (_RE_TASK_MARKER if role in faulted else status)
            for role, status in (row.member_status or {}).items()
        }
        for role in faulted:
            member_status.setdefault(role, _RE_TASK_MARKER)
        with org_scope(org):
            claimed, applied = await self._team_runs.transition(
                row.id,
                org,
                new_state="QUEUED",
                allowed_from=frozenset({"PAUSED"}),
                manifest=manifest,
                member_status=member_status,
                paused_at=[],  # clear the escalation sentinel
                escalation_kind=None,  # clear the verdict-escalation control marker
                re_dispatch_count=0,  # a fresh human attempt (the livelock counter resets)
                last_verdict_score=None,
                last_verdict_fingerprint=None,
            )
        if not applied or claimed is None:
            return await self.get(row.id, principal)
        if self._enqueue is not None:
            try:
                self._enqueue(row.id, org, principal.principal_id)
            except Exception:
                # review F1-org_scope: bind the org-GUC or the RLS backstop no-ops the compensation
                # and leaves the resume phantom-QUEUED (#620).
                with org_scope(org):
                    await self._team_runs.transition(
                        row.id,
                        org,
                        new_state="FAILED",
                        allowed_from=frozenset({"QUEUED"}),
                        error_message="re_dispatch enqueue failed",
                    )
                raise
        return claimed

    async def _grade_gate(
        self, team: OHMManifest, run_id: uuid.UUID, result: Any
    ) -> dict[str, Any] | None:
        """Grade a COMPLETED run at the ``success_criteria`` gate (#477). PRODUCES + returns the
        verdict dict; the caller STORES it on the SUCCEEDED row. This NEVER branches the run state
        and NEVER enqueues — consuming the verdict (re-dispatch/termination) is E8 (ADR-037 §4).
        Fail-closed: any grader error → a recorded ``pass=false`` verdict, and the run still
        SUCCEEDS (the run's own success is independent of the grader being reachable)."""
        if self._evaluate is None or team.orchestration is None:
            return None
        success_criteria = team.orchestration.success_criteria
        if not success_criteria:  # no gate declared → nothing to grade
            return None
        try:
            grade_target = _grade_target(
                team, result.results
            )  # inside the try → reducer errors too
            # BYOM judge (ADR-037 / BYOM-judge): a role="evaluator" model declares the per-org judge
            # credential — thread it so KRS grades with the user's OWN key (None → operator key).
            # The engine never holds the key (ADR-008): it passes only the credential_id + binding.
            ev_model = team.evaluator_model()
            judge_credential_id: str | None = None
            judge_model: str | None = None
            if ev_model is not None:
                cid = ev_model.config.get("credential_id")
                judge_credential_id = str(cid) if cid else None
                judge_model = ev_model.binding
            if is_battery_reference(success_criteria):
                # battery: resolved + iterated ENGINE-side; only each check's PROSE rubric leaves
                # the engine to core/evaluate (the battery token would 422 at KRS).
                async def _invoke(check: OHMGateCheck, output: str) -> float:
                    resp = await self._evaluate.evaluate(  # type: ignore[union-attr]
                        target_ref=f"{run_id}/{check.name}",
                        target_output=output,
                        success_criteria=check.rubric or "",
                        judge_credential_id=judge_credential_id,
                        judge_model=judge_model,
                    )
                    raw = resp.get("score", 0.0)
                    return float(raw) if isinstance(raw, int | float) else 0.0

                battery_verdict = await evaluate_gate(
                    team, grade_target, evaluate=_invoke, gate="success_criteria"
                )
                return battery_verdict.model_dump() if battery_verdict is not None else None
            return await self._evaluate.evaluate(
                target_ref=str(run_id),
                target_output=grade_target,
                success_criteria=success_criteria,
                judge_credential_id=judge_credential_id,
                judge_model=judge_model,
            )
        except Exception as exc:  # noqa: BLE001 — ANY grader-side failure fails CLOSED, never strands
            # The grade runs OUTSIDE _drive's try/except, so an escaping error would fail the Celery
            # task and strand the run RUNNING. The contract (docstring) is absolute: a grader error
            # → a recorded pass=false verdict and the run STILL SUCCEEDS. So catch everything here —
            # EvaluateRejected/EvaluateClientError, an UnknownBattery from a stray battery ref, a
            # decode/shape bug, anything — the run's success is independent of the grader.
            return {  # fail-closed verdict; the run still SUCCEEDS (state unchanged)
                "pass": False,
                "score": 0.0,
                "recommended_action": "escalate_human",
                "reason": f"grader unavailable ({type(exc).__name__})",
                # #604: a grader OUTAGE is NOT a real below-threshold grade — the run's success is
                # independent of the grader being reachable, so verdict-consumption must NOT branch
                # on it (no escalate/re-dispatch on a transient grader blip). The marker tells
                # ``decide_action`` to STORE_ONLY (the run stays SUCCEEDED, the contract at :819).
                "grader_unavailable": True,
            }

    def _make_loop_done_check(
        self,
        team: OHMManifest,
        run_id: uuid.UUID,
        graph_id: str | None,
        loop: OHMLoop,
        artifacts_baseline: int = 0,
        diag: dict[str, Any] | None = None,
    ) -> DoneCheckFn:
        """The CODED authority a loop must satisfy to converge (ADR-043 #552) — the team can NEVER
        satisfy its own done-check (the coordinator only routes; THIS decides). Three coded gates,
        each fail-closed: (1) COVERAGE — every loop member produced a non-None output; (2) LANDED
        ARTIFACTS — the work actually persisted on the bound graph (not merely claimed); (3) the
        separate-evaluator GRADE clears the declared convergence threshold. An absent threshold
        leaves coverage+artifacts as the floor; a malformed one never converges. The artifacts gate
        is RUN-scoped: ``artifacts_baseline`` is the graph's count BEFORE this run, so the gate
        requires NEW artifacts past it (a warm/adopted graph's own artifacts can't satisfy it)."""

        async def done_check(results: dict[str, Any]) -> bool:
            if diag is not None:  # #553: a FRESH diagnosis each round — never a stale key from a
                diag.clear()  # prior round (the gates below write conditionally + early-return)
            # 1. coverage floor — a failed loop member is results[role]=None, so it fails here
            if not all(results.get(r) is not None for r in loop.members):
                return False
            # 2. landed artifacts on the bound graph — THIS run's output actually persisted (run-
            # scoped: require growth past the pre-run baseline, not a graph-scoped non-empty count)
            if graph_id is not None and self._artifacts is not None:
                try:
                    arts = await self._artifacts.list_artifacts(graph_id)
                except ArtifactsClientError:
                    if diag is not None:  # #553: feed the stall diagnosis (inconclusive read)
                        diag["artifacts_ok"] = False
                    return False  # inconclusive read → not-yet-converged (fail-closed)
                landed = len(arts) > artifacts_baseline
                if diag is not None:  # #553: WHICH gate failed → the recalibration Diagnostic
                    diag["artifacts_ok"] = landed
                if not landed:
                    return False
            # 3. separate-evaluator grade vs the convergence threshold (if declared)
            conv = team.orchestration.termination.convergence if team.orchestration else None
            if conv and conv.strip():
                try:
                    op, floor = _parse_convergence(conv)
                except ValueError:
                    return False  # present-but-malformed → fail-closed (never silently passes)
                # a declared threshold with NO prose rubric can never be graded → NEVER converge
                # (fail-closed defense-in-depth; the OHMOrchestration validator rejects this combo
                # at load, so this is unreachable via a loaded manifest — but never fall through to
                # True on a declared-but-ungradable threshold).
                criteria = (team.orchestration.success_criteria or "").strip()
                if not criteria:
                    return False
                if self._evaluate is None:
                    return False  # a threshold declared but no judge wired → cannot confirm
                ev_model = team.evaluator_model()
                judge_credential_id = (
                    str(ev_model.config.get("credential_id"))
                    if ev_model and ev_model.config.get("credential_id")
                    else None
                )
                judge_model = ev_model.binding if ev_model else None
                try:
                    verdict = await self._evaluate.evaluate(
                        target_ref=f"{run_id}/loop",
                        target_output=_loop_grade_target(loop, results),
                        success_criteria=criteria,
                        judge_credential_id=judge_credential_id,
                        judge_model=judge_model,
                    )
                except EvaluateClientError:  # unreachable / rejected → not-converged (fail-closed)
                    return False
                score = _verdict_score(verdict)
                if diag is not None:  # #553: the scalar grade vs floor → recalibration Diagnostic
                    diag["evaluator_score"] = score
                    diag["evaluator_floor"] = floor
                if score is None or not _cmp(score, op, floor):
                    return False
            return True

        return done_check

    async def advance(
        self,
        team_run_id: uuid.UUID,
        principal: Principal,
        gate_decisions: Mapping[str, GateDecision],
    ) -> EngineTeamRun:
        """Request path: record a human gate decision on a PAUSED run, return it to QUEUED, and
        re-enqueue the worker to drive past the now-decided gate (202). The worker re-uses the
        persisted results (G-D), so completed members are not re-executed on resume. ADR-046 (#578):
        a ``revise`` decision routes to ``_advance_revision`` (re-run the invalidated producer
        sub-tree with feedback, re-pause); approve/reject are the unchanged cross/terminate path."""
        org = self._org(principal)
        row = await self.get(team_run_id, principal)
        if row.state != "PAUSED":
            raise TeamRunError(f"team run is {row.state}, not PAUSED — cannot advance", 409)
        # #604 Q3 guard: a run PAUSED on a VERDICT escalation must NEVER be blindly re-driven — its
        # members are seeded-complete, so a plain resume re-grades the identical output and re-
        # escalates. A human resume goes THROUGH verdict-consumption: force the faulted members to
        # re-run with a fresh loop counter (a human-initiated re_task). The discriminator is the
        # dedicated ``escalation_kind`` column, NOT ``paused_at`` list-equality — so a tenant that
        # names a member the sentinel role cannot hijack this path (review F1-sentinel).
        if row.escalation_kind == _VERDICT_ESCALATION_KIND:
            return await self._resume_verdict_escalation(row, org, principal)
        # normalize once (the DTO already parses GateDecision, but a direct/test caller may pass a
        # bare string or a persisted dict) so the revise branch + the persist share one shape.
        decisions = {
            role: (d if isinstance(d, GateDecision) else GateDecision.model_validate(d))
            for role, d in gate_decisions.items()
        }
        if any(d.decision == "revise" for d in decisions.values()):
            return await self._advance_revision(row, org, principal, decisions)
        merged = {**row.gate_decisions, **_decisions_to_jsonb(decisions)}
        with org_scope(org):
            claimed, applied = await self._team_runs.transition(
                team_run_id,
                org,
                new_state="QUEUED",
                allowed_from=frozenset({"PAUSED"}),
                gate_decisions=merged,
            )
        if not applied or claimed is None:  # lost the race (already advanced) — return current
            return await self.get(team_run_id, principal)
        if self._enqueue is not None:
            self._enqueue(team_run_id, org, principal.principal_id)
        return claimed  # QUEUED — the worker drives the resume

    async def _advance_revision(
        self,
        row: EngineTeamRun,
        org: uuid.UUID,
        principal: Principal,
        decisions: dict[str, GateDecision],
    ) -> EngineTeamRun:
        """ADR-046 (#578): apply one or more ``revise`` decisions to a PAUSED run. For each revised
        gate: bound by ``max_revisions`` (fail-close the whole run to terminal REJECTED past it);
        compute the invalidation set (the gate's producer sub-tree up to the nearest approved gate);
        mark those members to re-run (the #604 ``_RE_TASK_MARKER`` — excluded from the resume seed,
        so exactly they re-dispatch) with the human's feedback threaded into their subgoal (reusing
        the #604 ``_append_revision_directive``). ``edited_payload`` short-circuits: seed the edited
        value as the gate's producer result instead of re-running. Then CAS PAUSED→QUEUED — the
        worker re-drives, and ``run_team`` re-pauses at the still-``revise`` gate with the fresh
        output (approve/reject in the mixed advance are persisted alongside, unchanged)."""
        team = self._load_team(row.manifest)
        max_revisions = _resolve_max_revisions(team)
        rounds = {role: int(count) for role, count in (row.revision_rounds or {}).items()}
        merged = {**row.gate_decisions, **_decisions_to_jsonb(decisions)}
        invalidation: set[str] = set()
        manifest = row.manifest
        edited_seeds: dict[str, Any] = {}
        for role, gd in decisions.items():
            if gd.decision != "revise":
                continue  # approve/reject in a mixed advance ride along in ``merged``, unchanged
            rounds[role] = rounds.get(role, 0) + 1
            if rounds[role] > max_revisions:  # §4 bound — fail-closed, the whole run is REJECTED
                return await self._reject_revisions_exhausted(row, org, role, max_revisions)
            gate = team.member_by_role(role)
            if gd.edited_payload is not None and gate is not None and len(gate.depends_on) == 1:
                # override (§3): the human supplies the deliverable verbatim — seed it as the gate's
                # SOLE producer's result (do not re-run) and re-pause to confirm. Scoped to a
                # single-producer gate: with >1 producer the one edit can't disambiguate WHICH
                # deliverable it replaces, so that falls through to a normal feedback re-run below
                # (never clobber a sibling producer's real output with the edit).
                edited_seeds[gate.depends_on[0]] = gd.edited_payload
            else:
                slice_ = revision_invalidation_set(team.members, role, merged)
                invalidation |= slice_
                manifest = _append_revision_directive(
                    manifest, slice_, _human_revision_note(gd.feedback, rounds[role])
                )
        # a producer the human EDITED wins over a re-run: if it also fell in another gate's
        # invalidation set, keep the verbatim edit (don't re-dispatch it away, §3).
        invalidation -= set(edited_seeds)
        # mark the invalidation set to re-run (excluded from the resume seed); the rest is reused
        member_status = {
            role: (_RE_TASK_MARKER if role in invalidation else status)
            for role, status in (row.member_status or {}).items()
        }
        for role in invalidation:
            member_status.setdefault(role, _RE_TASK_MARKER)
        results = {**(row.results or {}), **edited_seeds}
        with org_scope(org):
            claimed, applied = await self._team_runs.transition(
                row.id,
                org,
                new_state="QUEUED",
                allowed_from=frozenset({"PAUSED"}),
                gate_decisions=merged,  # keeps the "revise" verb so run_team re-pauses at the gate
                revision_rounds=rounds,
                member_status=member_status,
                results=results,
                manifest=manifest,  # the feedback threaded into the invalidated producers' subgoal
            )
        if not applied or claimed is None:  # lost the race — return current
            return await self.get(row.id, principal)
        if self._enqueue is not None:
            try:
                self._enqueue(row.id, org, principal.principal_id)
            except Exception:
                # review F1-org_scope (mirrors #604): bind the org-GUC or the RLS backstop no-ops
                # the compensation and leaves the resume phantom-QUEUED (#620).
                with org_scope(org):
                    await self._team_runs.transition(
                        row.id,
                        org,
                        new_state="FAILED",
                        allowed_from=frozenset({"QUEUED"}),
                        error_message="re_dispatch enqueue failed",
                    )
                raise
        return claimed

    async def _reject_revisions_exhausted(
        self, row: EngineTeamRun, org: uuid.UUID, gate_role: str, max_revisions: int
    ) -> EngineTeamRun:
        """ADR-046 §4: the revision loop is bounded — once a gate is revised MORE than
        ``max_revisions`` times, fail-close the run to terminal REJECTED (never an unbounded human
        loop). CAS PAUSED→REJECTED with a leak-safe exhaustion message."""
        with org_scope(org):
            claimed, applied = await self._team_runs.transition(
                row.id,
                org,
                new_state="REJECTED",
                allowed_from=frozenset({"PAUSED"}),
                error_message=(
                    f"revision limit reached on gate '{gate_role}': more than {max_revisions} "
                    "revisions requested — run rejected"
                )[:2000],
            )
        return claimed if applied and claimed is not None else row

    async def rerun(self, team_run_id: uuid.UUID, principal: Principal) -> EngineTeamRun:
        """ADR-042 (#551): RE-RUN a FAILED run from the durable team state — re-drive only the
        FAILED + BLOCKED members (keeping the SUCCEEDED ones, which are NOT re-run), until every
        member delivers and the run is SUCCEEDED. Transitions FAILED→QUEUED + re-enqueues the
        worker, whose ``drive`` seeds the succeeded members via ``_completed_for_resume`` so only
        the failures re-dispatch. 409 if the run is not FAILED or has nothing re-runnable (no failed
        member to recover — a no-op). Org-scoped: a cross-org id is a 404 (via ``get``)."""
        org = self._org(principal)
        row = await self.get(team_run_id, principal)
        if row.state != "FAILED":
            raise TeamRunError(
                f"team run is {row.state}, not FAILED — cannot re-run",
                409,
                error_type="not_failed",
            )
        rerunnable = [s for s in (row.member_status or {}).values() if s in ("failed", "blocked")]
        if not rerunnable:  # a FAILED run with no recorded member failure (e.g. a hard drive crash)
            raise TeamRunError(
                "team run has no failed or blocked members to re-run",
                409,
                error_type="nothing_to_rerun",
            )
        with org_scope(org):  # CAS FAILED→QUEUED so a concurrent re-run does not double-drive
            claimed, applied = await self._team_runs.transition(
                team_run_id,
                org,
                new_state="QUEUED",
                allowed_from=frozenset({"FAILED"}),
                error_message=None,  # clear the prior failure summary; the re-drive records afresh
            )
        if not applied or claimed is None:  # lost the race (already re-queued) — return current
            return await self.get(team_run_id, principal)
        if self._enqueue is not None:
            self._enqueue(team_run_id, org, principal.principal_id)
        return claimed  # QUEUED — the worker re-drives the failed+blocked members

    async def _drive(
        self,
        row: EngineTeamRun,
        team: OHMManifest,
        org: uuid.UUID,
        *,
        completed: dict[str, Any] | None = None,
    ) -> EngineTeamRun:
        """Claim the run RUNNING, drive the member DAG through the harness, persist the outcome.

        Every DB op binds the org-GUC (``org_scope``) so the RLS backstop admits it (ADR-030); the
        harness drive runs OUTSIDE the binding (it is an HTTP call, not a DB op). ``completed``
        seeds already-finished members on a resume so they are not re-dispatched (G-D)."""
        # run-tree (#471): this run's tree root = trace_id threaded to every member. Minted = the
        # run's own id on first drive; STABLE across resume (read-if-already-set), so a resumed run
        # keeps the same tree. Persisted on the RUNNING claim so it is durable before any dispatch.
        root_execution_id = row.root_execution_id or row.id
        with org_scope(org):
            claimed, applied = await self._team_runs.transition(
                row.id,
                org,
                new_state="RUNNING",
                allowed_from=frozenset({"QUEUED"}),
                root_execution_id=root_execution_id,
            )
        if not applied or claimed is None:  # a concurrent driver owns it — no-op
            return claimed or row
        harness = self._harness
        if harness is None:  # only the reaper builds a harness-less service, and it never drives
            raise RuntimeError("team-run drive requires a harness client")
        # accumulate this drive's child execution ids onto any recorded by a prior (resumed) drive
        # (`or []` — a freshly-built / pre-migration row may carry NULL before the DB default fires)
        child_ids: list[str] = list(row.child_execution_ids or [])
        # O4 metering (#472): this drive's per-member token costs, summed onto the prior cost on
        # resume (succeeded members are not re-dispatched, so their cost is counted once). NB on an
        # ADR-042 re-run a FAILED member's first-attempt tokens are already in prior_cost and its
        # re-dispatch adds more — both are REAL spend, so the accumulated total intentionally counts
        # every attempt of a re-run member (it is not a double-count of the same work).
        cost_deltas: list[int] = []
        prior_cost = int(row.cost_tokens or 0)
        # ADR-043 #552: a team with genuine loops drives the conductor — the BYOM coordinator (picks
        # the next loop member) + the CODED done-check (coverage + landed-artifacts + evaluator)
        # are wired ONLY for a loop team; a purely acyclic team runs the unchanged single-pass DAG.
        has_loops = bool(team.orchestration and team.orchestration.loops)
        coordinate = make_loop_coordinator(harness, team) if has_loops else None
        # ADR-043 #553: on a loop STALL the conductor runs ONE bounded recalibration before halt —
        # a BYOM directive turn that picks a tactic from the closed set over the CODED diagnosis (it
        # never self-grades). None for an acyclic team → the seam is byte-unchanged (#552).
        recalibrate = make_recalibration_coordinator(harness, team) if has_loops else None
        # run-scoped landed-artifacts gate (CTO fast-follow): the artifacts that existed on the
        # graph BEFORE this run was CREATED (keyed off row.created_at, so it is IMMUTABLE across an
        # ADR-042 re-run — a resumed loop is never blocked by its own prior drive's artifacts). The
        # coded done-check then requires NEW artifacts past it (a warm/adopted graph's pre-existing
        # artifacts can't vacuously satisfy convergence). Fail-soft to 0 (fresh-per-run graph).
        artifacts_baseline = 0
        if has_loops and row.graph_id is not None and self._artifacts is not None:
            with contextlib.suppress(ArtifactsClientError):
                arts = await self._artifacts.list_artifacts(row.graph_id)
                artifacts_baseline = _pre_run_artifact_count(arts, row.created_at)

        def done_check_for(loop: OHMLoop, diag: dict[str, Any]) -> DoneCheckFn:
            return self._make_loop_done_check(
                team, row.id, row.graph_id, loop, artifacts_baseline=artifacts_baseline, diag=diag
            )

        try:
            result = await run_team_hybrid(
                team,
                harness,
                coordinate=coordinate,
                done_check_for=done_check_for if has_loops else None,
                recalibrate=recalibrate,
                # the live team-pooled spend (skeleton + every loop) for the loop cost bound
                cost_so_far=lambda: prior_cost + sum(cost_deltas),
                sub_harnesses=dict(row.sub_harnesses),
                gate_decisions=dict(row.gate_decisions),
                completed=completed,
                # PR-C: the prior per-loop checkpoint, so a resumed loop continues at a round
                # boundary (round counter + original wall-clock start) not restarting. `or {}`
                # tolerates a pre-PR-C / pre-migration row whose column is still NULL.
                loop_state=dict(row.loop_state or {}),
                trace_id=root_execution_id,
                parent_execution_id=root_execution_id,
                on_child=child_ids.append,
                on_cost=cost_deltas.append,
                workspace_root=row.workspace_root,  # file-native (#518): the run's working tree
                graph_id=row.graph_id,  # graph substrate (#524): the run's bound graph
                inputs=row.inputs,  # #599: user-seeded state for a member's fan_out.over: "$.<key>"
                # Hierarchy of Truth (#538/#514): the team's declared precedence, read off the
                # MANIFEST (no persisted column — rides off `team`), bound onto each retriever
                # instance so a member's in-loop retrieval is auto-ranked.
                precedence_order=(
                    list(team.precedence.order)
                    if team.precedence is not None and team.precedence.order
                    else None
                ),
                graph_authoritative=(
                    team.precedence is not None and team.precedence.graph == "authoritative"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — never strand the run in RUNNING (G-C); fail closed
            # ANY in-process drive error (harness failure, decode, network, bug) -> FAILED, not a
            # stuck RUNNING row. Return the FAILED row to the caller. NB this path does NOT record
            # per-member member_status (it stays {}), so the run is NOT re-runnable (rerun → 409
            # nothing_to_rerun). That is intentional for a TEAM-level hard fail — most notably a
            # max_wall_seconds timeout (OHMError from run_team): re-running the same DAG would just
            # time out again, so the recovery is a fresh POST, not a per-member re-drive (ADR-042).
            with org_scope(org):
                updated, _ = await self._team_runs.transition(
                    row.id,
                    org,
                    new_state="FAILED",
                    allowed_from=frozenset({"RUNNING"}),
                    error_message=str(exc)[:2000] or type(exc).__name__,
                    child_execution_ids=child_ids,  # record what was dispatched before the failure
                    cost_tokens=prior_cost + sum(cost_deltas),  # ...and what it cost
                )
            await self._accrue_schedule_cost(
                row, org, sum(cost_deltas)
            )  # #601: cost even on FAILED
            return updated or claimed
        except BaseException as exc:
            # NOT a normal error — task cancellation (ASGI client disconnect / worker SIGTERM) or
            # system exit, which are BaseException (not Exception) in 3.12 and would otherwise skip
            # the handler above and strand the row RUNNING. Best-effort mark FAILED (shielded so the
            # cancellation does not abort the write), then PROPAGATE — never swallow a cancellation.
            # If shutdown races us, the reaper sweeps the stale RUNNING row (the durable backstop).
            with contextlib.suppress(BaseException), org_scope(org):
                await asyncio.shield(
                    self._team_runs.transition(
                        row.id,
                        org,
                        new_state="FAILED",
                        allowed_from=frozenset({"RUNNING"}),
                        error_message=f"cancelled mid-drive: {type(exc).__name__}",
                    )
                )
            raise
        # flow-evaluation gate (#477): grade ONLY a completed run; PRODUCE + STORE the verdict on
        # the SUCCEEDED row. The run STATE is NOT branched on the verdict and NOTHING is enqueued
        # off it — consuming it (re-dispatch / termination) is E8 (ADR-037 §4). A grader failure
        # yields a fail-closed verdict and the run still SUCCEEDS (handled inside _grade_gate).
        verdict = (
            await self._grade_gate(team, row.id, result) if result.status == "completed" else None
        )
        # #602 seeded-refresh: the first-class 5-way delta, beside the verdict (None on a normal
        # run — default-OFF — or a non-completed run). Gated on seed_from_run_id so a normal run is
        # unchanged.
        refresh_delta = (
            self._refresh_delta(team, row, result)
            if row.seed_from_run_id is not None and result.status == "completed"
            else None
        )
        # ADR-042 (#551): a producing run is SUCCEEDED only when EVERY member delivered. A "failed"
        # result (one or more members FAILED/BLOCKED, the rest still ran) records each member's
        # terminal status so the failed+blocked members are re-runnable; surface a leak-safe summary
        # of which members failed (the per-member detail, never an upstream body) as error_message.
        member_status = dict(result.member_status)
        failed_summary: str | None = None
        if result.status == "failed":
            failed = sorted(r for r, s in member_status.items() if s == "failed")
            blocked = sorted(r for r, s in member_status.items() if s == "blocked")
            # surface each failed member's leak-safe detail (the harness error / dispatch error the
            # orchestrator recorded) so a FAILED run is debuggable + the re-run target is clear
            detail = "; ".join(
                f"{r}: {result.member_errors[r]}" for r in failed if result.member_errors.get(r)
            )
            failed_summary = (
                f"team run incomplete: {len(failed)} member(s) failed, {len(blocked)} blocked — "
                f"re-runnable. failures: {detail or ', '.join(failed) or 'none'}"
            )[:2000]
        with org_scope(org):
            updated, _ = await self._team_runs.transition(
                row.id,
                org,
                new_state=_STATUS_TO_STATE[result.status],
                allowed_from=frozenset({"RUNNING"}),
                results=dict(result.results),
                paused_at=list(result.paused_at),
                member_status=member_status,  # ADR-042: per-member result (drives re-run target)
                error_message=failed_summary,  # None unless a member failed/blocked
                child_execution_ids=child_ids,  # the member executions that form this run's tree
                cost_tokens=prior_cost + sum(cost_deltas),  # O4: the run's accumulated token cost
                verdict=verdict,  # the gate verdict (None unless completed); state stays unchanged
                refresh_delta=refresh_delta,  # #602: the 5-way delta (None on a non-refresh run)
                loop_state=dict(result.loop_state),  # PR-C: the per-loop checkpoint (resume cursor)
            )
        await self._accrue_schedule_cost(row, org, sum(cost_deltas))  # #601: per-cadence accrual
        # #604 closed-loop verdict-consumption (ADR-048 dec 5): branch the just-settled run on its
        # stored verdict — STORE_ONLY (unchanged) / RE_TASK (re-dispatch faulted members) / ESCALATE
        # (PAUSED for HITL). No-op unless a completed run graded below threshold with a re-task/
        # escalate action; the #585 pool + livelock + MAX ceiling bound the loop (fail-closed).
        consumed = await self._consume_verdict(updated or claimed, team, org)
        # #544/#625: a SUCCEEDED scheduled fire becomes the SEED for the next refresh — but
        # stamp on the FINAL POST-VERDICT state, AFTER _consume_verdict. A completed-but-below-
        # threshold fire that verdict-consumption RE_TASKs (→QUEUED) or ESCALATEs (→PAUSED) must NOT
        # clobber the schedule's prior GOOD seed; only a run STILL SUCCEEDED (STORE_ONLY — a
        # passing/absent gate) is a valid seed. Best-effort like the accrual.
        if consumed is not None and consumed.state == "SUCCEEDED":
            await self._stamp_schedule_seed(row, org)
        return consumed

    async def _accrue_schedule_cost(self, row: EngineTeamRun, org: uuid.UUID, delta: int) -> None:
        """#601: accrue THIS DRIVE's RAW-token cost (the delta, NOT the cumulative ``cost_tokens``)
        into the originating schedule's per-cadence accumulator, so a resume past a pause never
        double-counts. No-op for a direct (non-scheduled) run or when no schedule repo is wired.

        BEST-EFFORT: the run is ALREADY settled terminal at the call site — a transient accrual
        failure must not error the worker task (the retry could not re-accrue; the under-count is a
        bounded read-side inaccuracy, not a correctness hazard). So it is suppressed."""
        if row.schedule_id is None or self._schedules is None:
            return
        with contextlib.suppress(Exception), org_scope(org):
            await self._schedules.accrue_recurring_cost(row.schedule_id, org, delta)

    async def _stamp_schedule_seed(self, row: EngineTeamRun, org: uuid.UUID) -> None:
        """#544: stamp this SUCCEEDED scheduled fire as the schedule's seed for the NEXT fire (a
        recurring refresh carries forward its records — the #602 seeded-refresh delta on a cron).
        No-op for a direct (non-scheduled) run or when no schedule repo is wired. BEST-EFFORT
        (suppressed): the run is already settled terminal; a stamp failure just re-seeds from the
        older run next fire — a bounded staleness, never a correctness hazard."""
        if row.schedule_id is None or self._schedules is None:
            return
        with contextlib.suppress(Exception), org_scope(org):
            await self._schedules.set_last_settled_run(row.schedule_id, org, row.id)

    async def reap_stale(
        self, maintenance: EngineMaintenanceRepository, *, older_than: datetime
    ) -> int:
        """Fail team runs stuck RUNNING past the lease (a driver that died mid-drive, where no
        in-process except ran). Cross-org ENUMERATION is on the maintenance/owner engine; each FAIL
        is org-bound (``org_scope``) on the org engine — the ADR-030 §3 carve. We FAIL (not
        re-queue) so a stranded run does not silently re-execute its members; re-POST if wanted."""
        stale = await maintenance.list_stale_team_runs(older_than)
        reaped = 0
        for row in stale:
            with org_scope(row.organisation_id):
                _, applied = await self._team_runs.transition(
                    row.id,
                    row.organisation_id,
                    new_state="FAILED",
                    allowed_from=frozenset({"RUNNING"}),
                    error_message="reaped: stale RUNNING past lease (driver died mid-drive)",
                )
            reaped += int(applied)
        return reaped
