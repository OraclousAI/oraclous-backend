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
from oraclous_ohm.errors import OHMCapabilityError, OHMError
from oraclous_ohm.gate_battery import (
    OHMGateCheck,
    UnknownBattery,
    evaluate_gate,
    is_battery_reference,
    resolve_battery,
)
from oraclous_ohm.manifest import OHMLoop, OHMManifest
from oraclous_ohm.orchestrate import DoneCheckFn
from oraclous_ohm.parse import load_ohm

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.repositories.maintenance_repository import (
    EngineMaintenanceRepository,
)
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
}

# (team_run_id, organisation_id, user_id) -> None — hands a QUEUED run to the worker (broker).
EnqueueFn = Callable[[uuid.UUID, uuid.UUID, uuid.UUID], None]


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
        sum(1 for s in ms.values() if s in ("succeeded", "skipped"))
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

    async def create(
        self,
        principal: Principal,
        *,
        manifest: dict,
        sub_harnesses: dict[str, dict],
        gate_decisions: Mapping[str, str],
        workspace_root: str | None = None,
        graph_id: str | None = None,
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
        with org_scope(org):  # bind the org-GUC so the RLS-backstopped INSERT is admitted (ADR-030)
            row = await self._team_runs.create(
                organisation_id=org,
                user_id=principal.principal_id,
                manifest=manifest,
                sub_harnesses=sub_harnesses,
                gate_decisions=dict(gate_decisions),
                workspace_root=workspace_root,
                graph_id=graph_id,
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
        (#551): seed only the members whose terminal status is "succeeded" — so a re-run re-runs the
        FAILED + BLOCKED members (their results are None, never a real output) while the succeeded
        ones are reused. Back-compat: an in-flight PAUSED run created before member_status existed
        carries an empty member_status; fall back to all results so its gate-resume still reuses the
        pre-gate members (which are the only ones with a result on a PAUSED row)."""
        member_status = row.member_status or {}
        if not member_status:
            return dict(row.results or {})  # pre-ADR-042 resume semantics (in-flight PAUSED rows)
        return {
            role: row.results[role]
            for role, status in member_status.items()
            if status == "succeeded" and role in (row.results or {})
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
            }

    def _make_loop_done_check(
        self,
        team: OHMManifest,
        run_id: uuid.UUID,
        graph_id: str | None,
        loop: OHMLoop,
        artifacts_baseline: int = 0,
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
            # 1. coverage floor — a failed loop member is results[role]=None, so it fails here
            if not all(results.get(r) is not None for r in loop.members):
                return False
            # 2. landed artifacts on the bound graph — THIS run's output actually persisted (run-
            # scoped: require growth past the pre-run baseline, not a graph-scoped non-empty count)
            if graph_id is not None and self._artifacts is not None:
                try:
                    arts = await self._artifacts.list_artifacts(graph_id)
                except ArtifactsClientError:
                    return False  # inconclusive read → not-yet-converged (fail-closed)
                if len(arts) <= artifacts_baseline:
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
                if score is None or not _cmp(score, op, floor):
                    return False
            return True

        return done_check

    async def advance(
        self, team_run_id: uuid.UUID, principal: Principal, gate_decisions: Mapping[str, str]
    ) -> EngineTeamRun:
        """Request path: record a human gate decision on a PAUSED run, return it to QUEUED, and
        re-enqueue the worker to drive past the now-decided gate (202). The worker re-uses the
        persisted results (G-D), so completed members are not re-executed on resume."""
        org = self._org(principal)
        row = await self.get(team_run_id, principal)
        if row.state != "PAUSED":
            raise TeamRunError(f"team run is {row.state}, not PAUSED — cannot advance", 409)
        merged = {**row.gate_decisions, **gate_decisions}
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

        def done_check_for(loop: OHMLoop) -> DoneCheckFn:
            return self._make_loop_done_check(
                team, row.id, row.graph_id, loop, artifacts_baseline=artifacts_baseline
            )

        try:
            result = await run_team_hybrid(
                team,
                harness,
                coordinate=coordinate,
                done_check_for=done_check_for if has_loops else None,
                # the live team-pooled spend (skeleton + every loop) for the loop cost bound
                cost_so_far=lambda: prior_cost + sum(cost_deltas),
                sub_harnesses=dict(row.sub_harnesses),
                gate_decisions=dict(row.gate_decisions),
                completed=completed,
                trace_id=root_execution_id,
                parent_execution_id=root_execution_id,
                on_child=child_ids.append,
                on_cost=cost_deltas.append,
                workspace_root=row.workspace_root,  # file-native (#518): the run's working tree
                graph_id=row.graph_id,  # graph substrate (#524): the run's bound graph
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
            )
        return updated or claimed

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
