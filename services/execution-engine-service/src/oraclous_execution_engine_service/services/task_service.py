"""Human task board (ORAA-4 §21 services layer).

The engine's task board is its own ESCALATED ``engine_jobs`` (each parked job carries the harness
``assignment_id``). Completing a task drives the harness over HTTP — it marks the assignment
COMPLETED + flips its execution ESCALATED→SUCCEEDED with the human's output — then the engine flips
its own job ESCALATED→SUCCEEDED. Org from the principal only (ADR-006); a provenance event per
completion (§3.7).
"""

from __future__ import annotations

import uuid

from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_execution_engine_service.domain.state import sources_for
from oraclous_execution_engine_service.models.enums import EngineJobState
from oraclous_execution_engine_service.models.job import EngineJob
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClient,
    HarnessClientError,
)


class TaskError(Exception):
    """A task could not be listed/completed (missing, not open, or harness-rejected). HTTP 4xx."""


class TaskService:
    def __init__(
        self, *, jobs: JobRepository, harness: HarnessClient, provenance: ProvenanceCollector
    ) -> None:
        self._jobs = jobs
        self._harness = harness
        self._provenance = provenance

    async def list_tasks(self, principal: Principal) -> list[EngineJob]:
        """The open task board: the org's ESCALATED jobs (each carries a harness assignment)."""
        org_id = self._require_org(principal)
        return await self._jobs.list_for_org(org_id, state=EngineJobState.ESCALATED.value)

    async def complete(self, job_id: uuid.UUID, principal: Principal, output: str) -> EngineJob:
        org_id = self._require_org(principal)
        job = await self._jobs.get(job_id, org_id)
        if job is None:
            raise TaskError("task not found")
        if job.state != EngineJobState.ESCALATED.value or job.assignment_id is None:
            raise TaskError("job is not an open human task")
        # complete the harness assignment first (flips the harness's own run); then settle ours.
        try:
            await self._harness.complete_assignment(job.assignment_id, output)
        except HarnessClientError as exc:
            raise TaskError(f"harness rejected the completion: {exc}") from exc
        allowed = frozenset(s.value for s in sources_for(EngineJobState.SUCCEEDED))
        updated, _ = await self._jobs.transition(
            job.id,
            org_id,
            new_state=EngineJobState.SUCCEEDED.value,
            allowed_from=allowed,
            output=output,
            progress=100,
        )
        await self._emit(
            org_id, principal.principal_id, job.id, "engine.task.complete", "SUCCEEDED"
        )
        return updated or job

    @staticmethod
    def _require_org(principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:
            raise TaskError("authenticated principal has no organisation scope")
        return principal.organisation_id

    async def _emit(
        self,
        org_id: uuid.UUID,
        principal_id: uuid.UUID,
        job_id: uuid.UUID,
        action: str,
        outcome: str,
    ) -> None:
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=str(principal_id),
                action=action,
                resource=f"engine_job:{job_id}",
                outcome=outcome,
            )
        )
