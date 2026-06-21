"""Team-run service (ORAA-4 §21 services layer) — the durable, reachable entry point for running an
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
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
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
from oraclous_ohm.manifest import OHMManifest
from oraclous_ohm.parse import load_ohm

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.repositories.maintenance_repository import (
    EngineMaintenanceRepository,
)
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.evaluate_client import EvaluateClient
from oraclous_execution_engine_service.services.harness_client import HarnessClient
from oraclous_execution_engine_service.services.team_run import run_team_harness

# orchestrator status -> persisted team-run state
_STATUS_TO_STATE = {"completed": "SUCCEEDED", "paused": "PAUSED", "rejected": "REJECTED"}

# (team_run_id, organisation_id, user_id) -> None — hands a QUEUED run to the worker (broker).
EnqueueFn = Callable[[uuid.UUID, uuid.UUID, uuid.UUID], None]


class TeamRunError(Exception):
    """A client-facing team-run failure carrying an HTTP status (mapped in the route)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


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
    completion = (
        (100 if row.state == "SUCCEEDED" else 0)
        if total == 0
        else min(100, round(100 * len(row.results or {}) / total))
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


class TeamRunService:
    def __init__(
        self,
        *,
        team_runs: TeamRunRepository,
        harness: HarnessClient | None = None,
        enqueue: EnqueueFn | None = None,
        evaluate: EvaluateClient | None = None,
    ) -> None:
        # The drive runs on the WORKER (like jobs/round-tables): the request path (create/advance)
        # needs `enqueue` (hand the QUEUED run to the broker) but NOT a harness; the worker `drive`
        # needs `harness` but not `enqueue`; the reaper path (reap_stale) needs neither. `evaluate`
        # (the flow judge, #477) is the worker's gate grader — None ⇒ no gate eval (the run still
        # completes; the gate is simply not graded).
        self._team_runs = team_runs
        self._harness = harness
        self._enqueue = enqueue
        self._evaluate = evaluate

    def _org(self, principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:  # fail-closed tenancy (ADR-006)
            raise TeamRunError("authenticated principal has no organisation scope", 403)
        return principal.organisation_id

    def _load_team(self, document: dict) -> OHMManifest:
        try:
            manifest = load_ohm(document)
        except OHMError as exc:  # malformed / invalid OHM is a 422, not a 500
            raise TeamRunError(f"invalid OHM manifest: {exc}", 422) from exc
        if not manifest.is_team():
            raise TeamRunError("manifest is not a Team Harness (metadata.kind must be 'team')", 422)
        if not manifest.members:
            raise TeamRunError("a Team Harness must declare at least one member", 422)
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
                    f"success_criteria references an undeclared battery: {exc}", 422
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
                raise TeamRunError(f"sub_harness for unknown member role '{role}'", 422)
            try:
                sub = load_ohm(sub_doc)
            except OHMError as exc:
                raise TeamRunError(f"invalid sub_harness for '{role}': {exc}", 422) from exc
            try:
                assert_subharness_within_ceiling(member, sub)
            except OHMCapabilityError as exc:
                raise TeamRunError(
                    f"sub_harness for '{role}' exceeds its tools ceiling: {exc}", 422
                ) from exc

    async def create(
        self,
        principal: Principal,
        *,
        manifest: dict,
        sub_harnesses: dict[str, dict],
        gate_decisions: Mapping[str, str],
    ) -> EngineTeamRun:
        """Request path: validate + persist a QUEUED run + hand it to the worker (202). The drive
        runs on the worker so a large team (30 agents) never blocks/times out the HTTP request."""
        org = self._org(principal)
        team = self._load_team(manifest)  # validate BEFORE persisting
        self._enforce_member_ceilings(team, sub_harnesses)  # ADR-032/035 §5 — fail-closed ceiling
        with org_scope(org):  # bind the org-GUC so the RLS-backstopped INSERT is admitted (ADR-030)
            row = await self._team_runs.create(
                organisation_id=org,
                user_id=principal.principal_id,
                manifest=manifest,
                sub_harnesses=sub_harnesses,
                gate_decisions=dict(gate_decisions),
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
        return await self._drive(row, team, org, completed=dict(row.results))

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
            if is_battery_reference(success_criteria):
                # battery: resolved + iterated ENGINE-side; only each check's PROSE rubric leaves
                # the engine to core/evaluate (the battery token would 422 at KRS).
                async def _invoke(check: OHMGateCheck, output: str) -> float:
                    resp = await self._evaluate.evaluate(  # type: ignore[union-attr]
                        target_ref=f"{run_id}/{check.name}",
                        target_output=output,
                        success_criteria=check.rubric or "",
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
        # resume (only the not-yet-completed members re-dispatch, so their cost is counted once).
        cost_deltas: list[int] = []
        prior_cost = int(row.cost_tokens or 0)
        try:
            result = await run_team_harness(
                team,
                harness,
                sub_harnesses=dict(row.sub_harnesses),
                gate_decisions=dict(row.gate_decisions),
                completed=completed,
                trace_id=root_execution_id,
                parent_execution_id=root_execution_id,
                on_child=child_ids.append,
                on_cost=cost_deltas.append,
            )
        except Exception as exc:  # noqa: BLE001 — never strand the run in RUNNING (G-C); fail closed
            # ANY in-process drive error (harness failure, decode, network, bug) -> FAILED, not a
            # stuck RUNNING row. Return the FAILED row to the caller.
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
        with org_scope(org):
            updated, _ = await self._team_runs.transition(
                row.id,
                org,
                new_state=_STATUS_TO_STATE[result.status],
                allowed_from=frozenset({"RUNNING"}),
                results=dict(result.results),
                paused_at=list(result.paused_at),
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
