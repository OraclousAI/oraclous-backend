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
from datetime import UTC, datetime, timedelta
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
from oraclous_execution_engine_service.repositories.maintenance_repository import (
    EngineMaintenanceRepository,
)
from oraclous_execution_engine_service.repositories.provenance_sink import PostgresProvenanceSink
from oraclous_execution_engine_service.repositories.roundtable_repository import (
    RoundtableRepository,
)
from oraclous_execution_engine_service.repositories.schedule_repository import ScheduleRepository
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.harness_client import HarnessClient
from oraclous_execution_engine_service.services.job_service import JobService
from oraclous_execution_engine_service.services.roundtable_service import RoundtableService
from oraclous_execution_engine_service.services.schedule_service import ScheduleService
from oraclous_execution_engine_service.services.team_run_service import TeamRunService
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
            # enqueue too: a FAILED/TIMED_OUT job under its retry cap is re-queued by the worker.
            service = JobService(
                jobs=jobs,
                provenance=ProvenanceCollector(sink),
                harness=harness,
                enqueue=enqueue_job,
            )
            result = await service.execute(job_id, principal)
            return {"job_id": job_id_s, "state": result.state}
        finally:
            await harness.aclose()
            await jobs.close()
            await sink.close()


@celery_app.task(name="engine.reap_stale")
def reap_stale_running_task() -> dict[str, int]:  # noqa: ANN201
    """Periodic system sweep (scheduled by Celery Beat in S5): time out jobs stuck RUNNING past the
    lease, retrying eligible ones. Closes the worker/DB-blip stranded-RUNNING gap."""
    return AsyncTaskExecutor.run_async_task(_reap_async)


async def _reap_async() -> dict[str, int]:
    settings = get_settings()
    # ADR-030 §3: the per-row settle runs on the ORG-BOUND engine (settings.database_url →
    # oraclous_app in the deployed stack), each transition wrapped in org_scope(row.org) by the
    # service. The cross-org stale-RUNNING ENUMERATION runs on the MAINTENANCE engine
    # (settings.maintenance_url → the owner, which bypasses RLS) — else FORCE'd RLS fails the
    # cross-org read closed and no dead worker's job/round-table is ever found.
    jobs = JobRepository(settings.database_url, worker_pool=True)
    roundtables = RoundtableRepository(settings.database_url, worker_pool=True)
    team_runs = TeamRunRepository(settings.database_url, worker_pool=True)
    sink = PostgresProvenanceSink(settings.database_url, worker_pool=True)
    maintenance = EngineMaintenanceRepository(settings.maintenance_url)
    try:
        collector = ProvenanceCollector(sink)
        older_than = datetime.now(UTC) - timedelta(seconds=settings.running_lease_seconds)
        reaped = await JobService(
            jobs=jobs, provenance=collector, enqueue=enqueue_job, maintenance=maintenance
        ).reap_stale(older_than=older_than)
        # also re-queue round-tables whose driver died mid-turn (re-claim is CAS-idempotent).
        rt_reaped = await RoundtableService(
            roundtables=roundtables,
            provenance=collector,
            enqueue=enqueue_roundtable,
            maintenance=maintenance,
        ).reap_stale(older_than=older_than)
        # and FAIL team runs whose driver died mid-drive (FAIL, not re-queue — no re-execution).
        tr_reaped = await TeamRunService(team_runs=team_runs).reap_stale(
            maintenance, older_than=older_than
        )
        return {
            "reaped": reaped,
            "roundtables_reaped": rt_reaped,
            "team_runs_reaped": tr_reaped,
        }
    finally:
        await jobs.close()
        await roundtables.close()
        await team_runs.close()
        await sink.close()
        await maintenance.close()


@celery_app.task(name="engine.fire_schedules")
def fire_schedules_task() -> dict[str, int]:  # noqa: ANN201
    """Celery Beat tick: fire every enabled cron schedule whose latest window hasn't fired yet."""
    return AsyncTaskExecutor.run_async_task(_fire_schedules_async)


async def _fire_schedules_async() -> dict[str, int]:
    settings = get_settings()
    # ADR-030 §3: the enabled-cron ENUMERATION runs on the MAINTENANCE engine (the owner, bypasses
    # RLS) — else FORCE'd RLS fails the cross-org read closed and no org's cron ever fires. Each due
    # schedule's job-create + cursor-advance + provenance run on the ORG-BOUND engine
    # (settings.database_url → oraclous_app) wrapped in org_scope(sched.org) by the service.
    schedules = ScheduleRepository(settings.database_url, worker_pool=True)
    jobs = JobRepository(settings.database_url, worker_pool=True)
    sink = PostgresProvenanceSink(settings.database_url, worker_pool=True)
    maintenance = EngineMaintenanceRepository(settings.maintenance_url)
    try:
        service = ScheduleService(
            schedules=schedules,
            jobs=jobs,
            provenance=ProvenanceCollector(sink),
            enqueue=enqueue_job,
            maintenance=maintenance,
        )
        fired = await service.fire_due(datetime.now(UTC))
        return {"fired": fired}
    finally:
        await schedules.close()
        await jobs.close()
        await sink.close()
        await maintenance.close()


@celery_app.task(bind=True, name="engine.drive_roundtable")
def drive_roundtable_task(  # noqa: ANN001, ANN201
    self, roundtable_id: str, organisation_id: str, user_id: str
):  # noqa: ARG001
    return AsyncTaskExecutor.run_async_task(
        _drive_roundtable_async, roundtable_id, organisation_id, user_id
    )


async def _drive_roundtable_async(rt_id_s: str, org_id_s: str, user_id_s: str) -> dict[str, Any]:
    settings = get_settings()
    rt_id, org_id, user_id = uuid.UUID(rt_id_s), uuid.UUID(org_id_s), uuid.UUID(user_id_s)
    principal = Principal(
        principal_id=user_id, principal_type=PrincipalType.USER, organisation_id=org_id
    )
    context = OrganisationContext(
        organisation_id=org_id, principal_id=user_id, principal_type=PrincipalType.USER
    )
    with use_organisation_context(context):
        roundtables = RoundtableRepository(settings.database_url, worker_pool=True)
        sink = PostgresProvenanceSink(settings.database_url, worker_pool=True)
        harness = HarnessClient(
            settings.harness_runtime_url,
            headers=build_downstream_headers(principal, settings),
            timeout=settings.harness_request_timeout,
        )
        try:
            service = RoundtableService(
                roundtables=roundtables, provenance=ProvenanceCollector(sink), harness=harness
            )
            result = await service.drive(rt_id, principal)
            return {"roundtable_id": rt_id_s, "state": result.state}
        finally:
            await harness.aclose()
            await roundtables.close()
            await sink.close()


def enqueue_job(job_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Fire-and-forget: hand a QUEUED job to the worker over the broker."""
    run_engine_job_task.delay(str(job_id), str(organisation_id), str(user_id))


def enqueue_roundtable(rt_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Fire-and-forget: hand a round-table to the driver over the broker."""
    drive_roundtable_task.delay(str(rt_id), str(organisation_id), str(user_id))


@celery_app.task(bind=True, name="engine.drive_team_run")
def drive_team_run_task(  # noqa: ANN001, ANN201
    self, team_run_id: str, organisation_id: str, user_id: str
):  # noqa: ARG001
    return AsyncTaskExecutor.run_async_task(
        _drive_team_run_async, team_run_id, organisation_id, user_id
    )


async def _drive_team_run_async(run_id_s: str, org_id_s: str, user_id_s: str) -> dict[str, Any]:
    """Worker: drive a QUEUED team run's member DAG through the harness (mirrors the round-table
    driver). The org-bound engine + identity-propagating harness are built here; the drive is
    single-driver (the CAS QUEUED→RUNNING claim in the service no-ops a redelivery)."""
    settings = get_settings()
    run_id, org_id, user_id = uuid.UUID(run_id_s), uuid.UUID(org_id_s), uuid.UUID(user_id_s)
    principal = Principal(
        principal_id=user_id, principal_type=PrincipalType.USER, organisation_id=org_id
    )
    context = OrganisationContext(
        organisation_id=org_id, principal_id=user_id, principal_type=PrincipalType.USER
    )
    with use_organisation_context(context):
        team_runs = TeamRunRepository(settings.database_url, worker_pool=True)
        harness = HarnessClient(
            settings.harness_runtime_url,
            headers=build_downstream_headers(principal, settings),
            timeout=settings.harness_request_timeout,
        )
        try:
            service = TeamRunService(team_runs=team_runs, harness=harness)
            result = await service.drive(run_id, principal)
            return {"team_run_id": run_id_s, "state": result.state}
        finally:
            await harness.aclose()
            await team_runs.close()


def enqueue_team_run(run_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Fire-and-forget: hand a QUEUED team run to the worker driver over the broker."""
    drive_team_run_task.delay(str(run_id), str(organisation_id), str(user_id))
