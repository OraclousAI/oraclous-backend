"""Job orchestration spine (ORAA-4 §21 services layer).

Turns a submit into a durable run: create a QUEUED engine_jobs row, then run the harness over HTTP
and checkpoint the terminal state. In S1 the run is synchronous in-request (submit calls execute
inline); S2 moves execute to a Celery worker (submit enqueues + returns QUEUED). execute is the
reusable core both paths call. Org from the principal only (ADR-006, fail-closed); one provenance
event per transition (CLAUDE.md 3.7).
"""

from __future__ import annotations

import uuid
from typing import Any

from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_execution_engine_service.domain.status_map import map_harness_status
from oraclous_execution_engine_service.models.enums import EngineJobState
from oraclous_execution_engine_service.models.job import EngineJob
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClient,
    HarnessClientError,
)


class JobError(Exception):
    """A job could not be set up (e.g. no organisation scope). Maps to HTTP 4xx in the route."""


class JobService:
    def __init__(
        self, *, jobs: JobRepository, harness: HarnessClient, provenance: ProvenanceCollector
    ) -> None:
        self._jobs = jobs
        self._harness = harness
        self._provenance = provenance

    async def submit(
        self,
        *,
        principal: Principal,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        manifest_ref: str | None = None,
        max_retries: int = 0,
        timeout_seconds: int | None = None,
    ) -> EngineJob:
        org_id = self._require_org(principal)
        job = await self._jobs.create(
            organisation_id=org_id,
            user_id=principal.principal_id,
            input_text=input_text,
            manifest_inline=manifest_inline,
            manifest_ref=manifest_ref,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
        )
        await self._emit(org_id, principal, job.id, "engine.job.submit", "QUEUED")
        # S1: run synchronously in-request. S2 replaces this with an enqueue + worker.
        return await self.execute(job.id, principal)

    async def execute(self, job_id: uuid.UUID, principal: Principal) -> EngineJob:
        """Run the harness for a QUEUED job + checkpoint terminal state. Reused by the worker."""
        org_id = self._require_org(principal)
        job = await self._jobs.get(job_id, org_id)
        if job is None:
            raise JobError("job not found")
        await self._jobs.update(job_id, org_id, state=EngineJobState.RUNNING.value)

        try:
            result = await self._harness.execute(
                input_text=job.input_text,
                manifest_inline=job.manifest_inline,
                manifest_ref=job.manifest_ref,
            )
        except HarnessClientError as exc:
            updated = await self._jobs.update(
                job_id,
                org_id,
                state=EngineJobState.FAILED.value,
                error_type="harness_unreachable",
                error_message=str(exc)[:2000],
            )
            await self._emit(org_id, principal, job_id, "engine.job.run", "FAILED")
            return updated or job

        state = map_harness_status(result.get("status", ""))
        updated = await self._jobs.update(
            job_id,
            org_id,
            state=state.value,
            harness_execution_id=_as_uuid(result.get("id")),
            assignment_id=_assignment_from(result),
            output=result.get("output"),
            error_type=result.get("error_type"),
            error_message=result.get("error_message"),
            progress=100 if state is not EngineJobState.ESCALATED else job.progress,
        )
        await self._emit(org_id, principal, job_id, "engine.job.run", state.value)
        return updated or job

    async def get(self, job_id: uuid.UUID, principal: Principal) -> EngineJob | None:
        return await self._jobs.get(job_id, self._require_org(principal))

    @staticmethod
    def _require_org(principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:
            raise JobError("authenticated principal has no organisation scope")
        return principal.organisation_id

    async def _emit(
        self, org_id: uuid.UUID, principal: Principal, job_id: uuid.UUID, action: str, outcome: str
    ) -> None:
        await self._provenance.emit(
            ProvenanceRecord(
                organisation_id=str(org_id),
                principal=str(principal.principal_id),
                action=action,
                resource=f"engine_job:{job_id}",
                outcome=outcome,
            )
        )


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, TypeError):
        return None


def _assignment_from(result: dict[str, Any]) -> uuid.UUID | None:
    """A human-actor escalation embeds the assignment id in a GATE step's detail (S4 reads it)."""
    if result.get("error_type") != "human_assignment":
        return None
    for step in result.get("steps", []):
        if step.get("kind") == "gate" and step.get("status") == "assigned":
            return _as_uuid(step.get("detail"))
    return None
