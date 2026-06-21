"""Human task board (services layer).

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

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.domain.state import sources_for
from oraclous_execution_engine_service.domain.status_map import map_harness_status
from oraclous_execution_engine_service.models.enums import EngineJobState
from oraclous_execution_engine_service.models.job import EngineJob
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClient,
    HarnessClientError,
)


class TaskError(Exception):
    """A task could not be listed/completed (missing, not open, or harness-rejected). HTTP 4xx."""


def _bounded(value: object, limit: int) -> str | None:
    return str(value)[:limit] if value else None


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
        # ADR-030 §3: bind the org so the read runs with the GUC set (else FORCE'd RLS → zero rows).
        with org_scope(org_id):
            return await self._jobs.list_for_org(org_id, state=EngineJobState.ESCALATED.value)

    async def complete(self, job_id: uuid.UUID, principal: Principal, output: str) -> EngineJob:
        org_id = self._require_org(principal)
        # ADR-030 §3: bind the org for the whole request-path op so the row-read + the CAS
        # transition + provenance run with the GUC set on the org-bound engine (T1-M1).
        with org_scope(org_id):
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
            updated, applied = await self._jobs.transition(
                job.id,
                org_id,
                new_state=EngineJobState.SUCCEEDED.value,
                allowed_from=allowed,
                output=output,
                progress=100,
            )
            if updated is None or not applied:
                # the harness run is committed SUCCEEDED, but our job moved under us (a concurrent
                # cancel / terminal). Surface the split honestly instead of a fake SUCCEEDED + 200.
                # (A reconciliation sweep to re-drive such rows is a follow-up; harness-first is the
                # least-bad ordering since the common failure — harness down — raises before this.)
                # ``updated is None`` is unreachable when ``applied`` (transition returns the row);
                # it narrows ``updated`` to non-None for the return below.
                raise TaskError(
                    "job state changed during completion; the harness run already settled"
                )
            await self._emit(
                org_id, principal.principal_id, job.id, "engine.task.complete", "SUCCEEDED"
            )
        return updated

    async def approve(
        self,
        job_id: uuid.UUID,
        principal: Principal,
        decision: str,
        decision_reason: str | None = None,
    ) -> EngineJob:
        """Resolve a mid-loop HITL approval task. The job carries a harness execution but NO
        assignment (entrypoint human tasks carry an assignment → use complete()). APPROVED resumes
        the harness loop (the gated tool runs); DENIED terminates it FAILED. Harness-first, then a
        CAS flip — a chained HITL re-pause leaves the job ESCALATED for the next approval."""
        org_id = self._require_org(principal)
        # ADR-030 §3: bind the org for the whole request-path op so the row-read + the CAS
        # transition + provenance run with the GUC set on the org-bound engine (T1-M1).
        with org_scope(org_id):
            job = await self._jobs.get(job_id, org_id)
            if job is None:
                raise TaskError("task not found")
            if (
                job.state != EngineJobState.ESCALATED.value
                or job.harness_execution_id is None
                or job.assignment_id is not None
            ):
                raise TaskError("job is not a mid-loop HITL approval task")
            try:
                result = await self._harness.resume(
                    job.harness_execution_id, decision, decision_reason
                )
            except HarnessClientError as exc:
                raise TaskError(f"harness rejected the resume: {exc}") from exc

            target = map_harness_status(result.get("status", ""))
            if target is EngineJobState.ESCALATED:
                # a chained HITL pause re-escalated the run; the job stays ESCALATED (next approve).
                await self._emit(
                    org_id, principal.principal_id, job.id, "engine.task.approve", "re-escalated"
                )
                return job
            allowed = frozenset(s.value for s in sources_for(target))
            updated, applied = await self._jobs.transition(
                job.id,
                org_id,
                new_state=target.value,
                allowed_from=allowed,
                output=result.get("output"),
                error_type=_bounded(result.get("error_type"), 128),
                error_message=_bounded(result.get("error_message"), 2000),
                progress=100,
            )
            if updated is None or not applied:
                # the harness already settled, but our job moved (concurrent cancel) — honest error.
                # ``updated is None`` is unreachable when ``applied`` — it narrows for the return.
                raise TaskError(
                    "job state changed during approval; the harness run already settled"
                )
            await self._emit(
                org_id, principal.principal_id, job.id, "engine.task.approve", decision
            )
        return updated

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
