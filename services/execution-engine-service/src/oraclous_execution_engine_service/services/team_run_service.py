"""Team-run service (ORAA-4 §21 services layer) — the durable, reachable entry point for running an
OHM v1.1 Team Harness.

This is the front door the orchestrator (``oraclous_ohm.orchestrate.run_team``) lacked: a request
creates a ``engine_team_runs`` row, the service drives the member DAG through the REAL harness
(``run_team_harness`` → ``HarnessClient.execute`` per member, the typed hand-off envelopes threaded)
and persists the outcome. A human gate pauses the run durably (state ``PAUSED`` + ``paused_at``); a
later ``advance`` records the decision and re-drives past it. A member whose harness does not
succeed fails the run (fail-closed).

The drive is synchronous in the request path (it reuses the per-request, identity-propagating
``HarnessClient`` — ADR-018). Moving the drive onto the Celery worker (like jobs/round-tables) so a
long team run returns 202 immediately is a follow-up; the durable row + the QUEUED/RUNNING claim are
already shaped for it (the worker would call ``_drive`` exactly as the request path does here).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from oraclous_governance import Principal
from oraclous_ohm.errors import OHMError
from oraclous_ohm.manifest import OHMManifest
from oraclous_ohm.parse import load_ohm

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClient,
    HarnessClientError,
)
from oraclous_execution_engine_service.services.team_run import run_team_harness

# orchestrator status -> persisted team-run state
_STATUS_TO_STATE = {"completed": "SUCCEEDED", "paused": "PAUSED", "rejected": "REJECTED"}


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
        harness: HarnessClient,
    ) -> None:
        self._team_runs = team_runs
        self._harness = harness

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

    async def create_and_run(
        self,
        principal: Principal,
        *,
        manifest: dict,
        sub_harnesses: dict[str, dict],
        gate_decisions: Mapping[str, str],
    ) -> EngineTeamRun:
        org = self._org(principal)
        team = self._load_team(manifest)  # validate BEFORE persisting
        with org_scope(org):  # bind the org-GUC so the RLS-backstopped INSERT is admitted (ADR-030)
            row = await self._team_runs.create(
                organisation_id=org,
                user_id=principal.principal_id,
                manifest=manifest,
                sub_harnesses=sub_harnesses,
                gate_decisions=dict(gate_decisions),
            )
        return await self._drive(row, team, org)

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
        """Record a human gate decision on a PAUSED run and re-drive past the now-decided gate."""
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
        team = self._load_team(claimed.manifest)
        return await self._drive(claimed, team, org)

    async def _drive(self, row: EngineTeamRun, team: OHMManifest, org: uuid.UUID) -> EngineTeamRun:
        """Claim the run RUNNING, drive the member DAG through the harness, persist the outcome.

        Every DB op binds the org-GUC (``org_scope``) so the RLS backstop admits it (ADR-030); the
        harness drive runs OUTSIDE the binding (it is an HTTP call, not a DB op). NB: ``advance``
        re-drives the full DAG from the start — resuming from the gate (skipping completed members)
        is a follow-up once the per-member results are replayed as cached inputs."""
        with org_scope(org):
            claimed, applied = await self._team_runs.transition(
                row.id, org, new_state="RUNNING", allowed_from=frozenset({"QUEUED"})
            )
        if not applied or claimed is None:  # a concurrent driver owns it — no-op
            return claimed or row
        try:
            result = await run_team_harness(
                team,
                self._harness,
                sub_harnesses=dict(row.sub_harnesses),
                gate_decisions=dict(row.gate_decisions),
            )
        except HarnessClientError as exc:
            with org_scope(org):
                updated, _ = await self._team_runs.transition(
                    row.id,
                    org,
                    new_state="FAILED",
                    allowed_from=frozenset({"RUNNING"}),
                    error_message=str(exc)[:2000],
                )
            return updated or claimed
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
