"""Job orchestration spine (ORAA-4 §21 services layer).

A submit creates a QUEUED engine_jobs row and ENQUEUES it (S2): the route returns 202 + the QUEUED
job; a Celery worker later calls execute(), which runs the harness over HTTP and checkpoints the
terminal state. Every state change goes through a CAS transition (row-locked) so a concurrent cancel
can never race the worker. Org from the principal only (ADR-006, fail-closed); one provenance event
per transition (CLAUDE.md 3.7).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector, ProvenanceRecord

from oraclous_execution_engine_service.domain.state import sources_for
from oraclous_execution_engine_service.domain.status_map import map_harness_status
from oraclous_execution_engine_service.models.enums import EngineJobState
from oraclous_execution_engine_service.models.job import EngineJob
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.services.harness_client import (
    HarnessClient,
    HarnessClientError,
    HarnessTimeout,
)

# A queue hand-off: (job_id, organisation_id, user_id) → fire the worker task. Injected for tests.
EnqueueFn = Callable[[uuid.UUID, uuid.UUID, uuid.UUID], None]


class JobError(Exception):
    """A job could not be set up (e.g. no organisation scope). Maps to HTTP 4xx in the route."""


class JobService:
    def __init__(
        self,
        *,
        jobs: JobRepository,
        provenance: ProvenanceCollector,
        harness: HarnessClient | None = None,
        enqueue: EnqueueFn | None = None,
    ) -> None:
        self._jobs = jobs
        self._provenance = provenance
        self._harness = harness  # the worker path needs this
        self._enqueue = enqueue  # the request path needs this

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
        if self._enqueue is None:  # request path must have a queue
            raise JobError("no job queue configured")
        job = await self._jobs.create(
            organisation_id=org_id,
            user_id=principal.principal_id,
            input_text=input_text,
            manifest_inline=manifest_inline,
            manifest_ref=manifest_ref,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
        )
        # The QUEUED row is durable before the (fallible) enqueue — so if provenance or the broker
        # hand-off fails, fail the row instead of orphaning it as a phantom QUEUED job.
        try:
            await self._emit(org_id, principal.principal_id, job.id, "engine.job.submit", "QUEUED")
            self._enqueue(job.id, org_id, principal.principal_id)
        except Exception:
            await self._jobs.transition(
                job.id,
                org_id,
                new_state=EngineJobState.FAILED.value,
                allowed_from=frozenset({EngineJobState.QUEUED.value}),
                error_type="enqueue_failed",
            )
            raise
        return job

    async def execute(self, job_id: uuid.UUID, principal: Principal) -> EngineJob:
        """Run the harness for a QUEUED job + settle the outcome (retry/terminal). Worker entry."""
        org_id = self._require_org(principal)
        if self._harness is None:  # worker path must have a harness client
            raise JobError("no harness client configured")
        job = await self._jobs.get(job_id, org_id)
        if job is None:
            raise JobError("job not found")

        running, started = await self._transition(job, EngineJobState.RUNNING, progress=5)
        if not started:  # cancelled before pickup, or not in a runnable state — leave it alone
            return running

        try:
            result = await self._harness.execute(
                input_text=job.input_text,
                manifest_inline=job.manifest_inline,
                manifest_ref=job.manifest_ref,
                timeout=float(job.timeout_seconds) if job.timeout_seconds else None,
            )
        except HarnessTimeout as exc:
            return await self._settle(
                running,
                EngineJobState.TIMED_OUT,
                error_type="timeout",
                error_message=str(exc)[:2000],
            )
        except HarnessClientError as exc:
            return await self._settle(
                running,
                EngineJobState.FAILED,
                error_type="harness_unreachable",
                error_message=str(exc)[:2000],
            )

        state = map_harness_status(result.get("status", ""))
        return await self._settle(
            running,
            state,
            harness_execution_id=_as_uuid(result.get("id")),
            assignment_id=_assignment_from(result),
            output=result.get("output"),
            error_type=_bounded(result.get("error_type"), 128),
            error_message=_bounded(result.get("error_message"), 2000),
            progress=100 if state is not EngineJobState.ESCALATED else running.progress,
        )

    async def cancel(self, job_id: uuid.UUID, principal: Principal) -> EngineJob:
        """Cancel a QUEUED/RUNNING/ESCALATED job. A terminal job is a no-op (returns as-is)."""
        org_id = self._require_org(principal)
        job = await self._jobs.get(job_id, org_id)
        if job is None:
            raise JobError("job not found")
        cancelled, applied = await self._transition(job, EngineJobState.CANCELLED)
        if applied:
            await self._emit(
                job.organisation_id, job.user_id, job_id, "engine.job.cancel", "CANCELLED"
            )
        return cancelled

    async def reap_stale(self, *, older_than: datetime) -> int:
        """System sweep: a job stuck RUNNING past its lease (worker/DB blip after RUNNING) is timed
        out + retried if eligible. Cross-org maintenance — each row settles under its OWN org.
        Scheduled by Celery Beat in S5."""
        reaped = 0
        for job in await self._jobs.list_stale_running(older_than):
            await self._settle(
                job,
                EngineJobState.TIMED_OUT,
                error_type="lease_expired",
                error_message="worker lease expired with no terminal checkpoint",
            )
            reaped += 1
        return reaped

    async def get(self, job_id: uuid.UUID, principal: Principal) -> EngineJob | None:
        return await self._jobs.get(job_id, self._require_org(principal))

    async def _settle(self, job: EngineJob, outcome: EngineJobState, **fields: Any) -> EngineJob:
        """Apply the run outcome, then re-queue a FAILED/TIMED_OUT job if retries remain."""
        updated, applied = await self._transition(job, outcome, **fields)
        if (
            applied
            and outcome in (EngineJobState.FAILED, EngineJobState.TIMED_OUT)
            and updated.retry_count < updated.max_retries
            and self._enqueue is not None
        ):
            requeued, ok = await self._transition(
                updated,
                EngineJobState.QUEUED,
                retry_count=updated.retry_count + 1,
                error_type=None,
                error_message=None,
            )
            if ok:
                self._enqueue(requeued.id, requeued.organisation_id, requeued.user_id)
                await self._emit(
                    requeued.organisation_id,
                    requeued.user_id,
                    requeued.id,
                    "engine.job.retry",
                    f"attempt {requeued.retry_count}/{requeued.max_retries}",
                )
                return requeued
        await self._emit(job.organisation_id, job.user_id, job.id, "engine.job.run", updated.state)
        return updated

    async def _transition(
        self, job: EngineJob, target: EngineJobState, **fields: Any
    ) -> tuple[EngineJob, bool]:
        """CAS the job into ``target`` if its current state allows it; returns (row, applied)."""
        allowed = frozenset(s.value for s in sources_for(target))
        row, applied = await self._jobs.transition(
            job.id, job.organisation_id, new_state=target.value, allowed_from=allowed, **fields
        )
        return (row or job), applied

    @staticmethod
    def _require_org(principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:
            raise JobError("authenticated principal has no organisation scope")
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


def _bounded(value: Any, limit: int) -> str | None:
    return str(value)[:limit] if value else None


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
