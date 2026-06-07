"""The durable job worker (R5-S2).

A Celery worker has NO HTTP request, so it (a) carries the durable job's id + organisation_id +
user_id as explicit JSON args — the only channel across the broker — and (b) reconstructs the
principal + binds the org context before running. The engine's own repositories are org-EXPLICIT
(every query carries organisation_id), so the context binding is belt-and-suspenders for any future
substrate-scoped access; the identity it really needs is forwarded to the harness via the same
downstream headers the request path builds. NullPool engine + per-task clients, disposed after the
run (ADR-012 worker invariant).
"""

from __future__ import annotations

import uuid
from typing import Any

from oraclous_governance import (
    OrganisationContext,
    Principal,
    PrincipalType,
    use_organisation_context,
)
from oraclous_substrate import ProvenanceCollector

from oraclous_execution_engine_service.core.auth import build_downstream_headers
from oraclous_execution_engine_service.core.config import get_settings
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.repositories.provenance_sink import PostgresProvenanceSink
from oraclous_execution_engine_service.services.harness_client import HarnessClient
from oraclous_execution_engine_service.services.job_service import JobService
from oraclous_execution_engine_service.tasks.celery_app import AsyncTaskExecutor, celery_app


@celery_app.task(bind=True, name="engine.run_job")
def run_engine_job_task(  # noqa: ANN001, ANN201
    self, job_id: str, organisation_id: str, user_id: str
):  # noqa: ARG001
    return AsyncTaskExecutor.run_async_task(_run_async, job_id, organisation_id, user_id)


async def _run_async(job_id_s: str, org_id_s: str, user_id_s: str) -> dict[str, Any]:
    settings = get_settings()
    job_id, org_id, user_id = uuid.UUID(job_id_s), uuid.UUID(org_id_s), uuid.UUID(user_id_s)
    principal = Principal(
        principal_id=user_id, principal_type=PrincipalType.USER, organisation_id=org_id
    )
    context = OrganisationContext(
        organisation_id=org_id, principal_id=user_id, principal_type=PrincipalType.USER
    )
    with use_organisation_context(context):
        jobs = JobRepository(settings.database_url, worker_pool=True)
        sink = PostgresProvenanceSink(settings.database_url, worker_pool=True)
        harness = HarnessClient(
            settings.harness_runtime_url,
            headers=build_downstream_headers(principal, settings),
            timeout=settings.harness_request_timeout,
        )
        try:
            service = JobService(jobs=jobs, provenance=ProvenanceCollector(sink), harness=harness)
            result = await service.execute(job_id, principal)
            return {"job_id": job_id_s, "state": result.state}
        finally:
            await harness.aclose()
            await jobs.close()
            await sink.close()


def enqueue_job(job_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Fire-and-forget: hand a QUEUED job to the worker over the broker."""
    run_engine_job_task.delay(str(job_id), str(organisation_id), str(user_id))
