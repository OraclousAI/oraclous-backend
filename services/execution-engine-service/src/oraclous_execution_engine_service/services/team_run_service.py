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
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from oraclous_governance import Principal
from oraclous_ohm.capabilities import assert_subharness_within_ceiling
from oraclous_ohm.errors import OHMCapabilityError, OHMError
from oraclous_ohm.manifest import OHMManifest
from oraclous_ohm.parse import load_ohm

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.repositories.maintenance_repository import (
    EngineMaintenanceRepository,
)
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
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


class TeamRunService:
    def __init__(
        self,
        *,
        team_runs: TeamRunRepository,
        harness: HarnessClient | None = None,
        enqueue: EnqueueFn | None = None,
    ) -> None:
        # The drive runs on the WORKER (like jobs/round-tables): the request path (create/advance)
        # needs `enqueue` (hand the QUEUED run to the broker) but NOT a harness; the worker `drive`
        # needs `harness` but not `enqueue`; the reaper path (reap_stale) needs neither.
        self._team_runs = team_runs
        self._harness = harness
        self._enqueue = enqueue

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
        with org_scope(org):
            claimed, applied = await self._team_runs.transition(
                row.id, org, new_state="RUNNING", allowed_from=frozenset({"QUEUED"})
            )
        if not applied or claimed is None:  # a concurrent driver owns it — no-op
            return claimed or row
        harness = self._harness
        if harness is None:  # only the reaper builds a harness-less service, and it never drives
            raise RuntimeError("team-run drive requires a harness client")
        try:
            result = await run_team_harness(
                team,
                harness,
                sub_harnesses=dict(row.sub_harnesses),
                gate_decisions=dict(row.gate_decisions),
                completed=completed,
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
        with org_scope(org):
            updated, _ = await self._team_runs.transition(
                row.id,
                org,
                new_state=_STATUS_TO_STATE[result.status],
                allowed_from=frozenset({"RUNNING"}),
                results=dict(result.results),
                paused_at=list(result.paused_at),
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
