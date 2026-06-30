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
from oraclous_execution_engine_service.services.artifacts_client import ArtifactsClient
from oraclous_execution_engine_service.services.evaluate_client import EvaluateClient
from oraclous_execution_engine_service.services.harness_client import HarnessClient
from oraclous_execution_engine_service.services.job_service import JobService
from oraclous_execution_engine_service.services.registry_client import RegistryClient
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
    team_runs = TeamRunRepository(settings.database_url, worker_pool=True)
    try:
        service = ScheduleService(
            schedules=schedules,
            jobs=jobs,
            provenance=ProvenanceCollector(sink),
            enqueue=enqueue_job,
            enqueue_adopted_tool=enqueue_adopted_tool,
            enqueue_team_run=enqueue_team_run,  # #601: the standing-team fire branch dispatch
            team_runs=team_runs,
            maintenance=maintenance,
        )
        now = datetime.now(UTC)
        # #598: resume L3-paused standing teams whose period window has rolled FIRST, so a just-
        # resumed schedule re-enters the enabled-cron set + fires its new window in this same tick.
        resumed = await service.resume_budget_paused(now)
        fired = await service.fire_due(now)
        return {"fired": fired, "resumed": resumed}
    finally:
        await schedules.close()
        await jobs.close()
        await sink.close()
        await maintenance.close()
        await team_runs.close()


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


@celery_app.task(bind=True, name="engine.run_adopted_tool")
def run_adopted_tool_task(  # noqa: ANN001, ANN201
    self,
    run_id: str,
    instance_id: str,
    input_data: dict[str, Any],
    organisation_id: str,
    user_id: str,
):  # noqa: ARG001
    return AsyncTaskExecutor.run_async_task(
        _run_adopted_tool_async, run_id, instance_id, input_data, organisation_id, user_id
    )


async def _run_adopted_tool_async(
    run_id_s: str, instance_id_s: str, input_data: dict[str, Any], org_id_s: str, user_id_s: str
) -> dict[str, Any]:
    """Worker: dispatch an ADOPTED_TOOL_RUN schedule fire to the capability-registry (#489).

    The engine already wrote the idempotency row (in ScheduleService._fire_adopted_tool) BEFORE this
    task was enqueued — so reaching this worker means the dispatch is NOT a duplicate. The worker
    reconstructs the schedule-owner principal (no SYSTEM principal exists — the auto-fire acts as
    the owner), binds the org context, calls the registry instance /execute over HTTP with the SAME
    downstream identity headers the request path builds (so the registry sees the right tenant), and
    stamps the registry ExecutionOut.id back onto the run row. NullPool engine + a per-task client,
    disposed after (ADR-012 worker invariant)."""
    settings = get_settings()
    run_id = uuid.UUID(run_id_s)
    instance_id, org_id, user_id = (
        uuid.UUID(instance_id_s),
        uuid.UUID(org_id_s),
        uuid.UUID(user_id_s),
    )
    principal = Principal(
        principal_id=user_id, principal_type=PrincipalType.USER, organisation_id=org_id
    )
    context = OrganisationContext(
        organisation_id=org_id, principal_id=user_id, principal_type=PrincipalType.USER
    )
    with use_organisation_context(context):
        jobs = JobRepository(settings.database_url, worker_pool=True)
        registry = RegistryClient(
            settings.capability_registry_url,
            headers=build_downstream_headers(principal, settings),
            timeout=settings.capability_registry_request_timeout,
        )
        try:
            result = await registry.execute(instance_id, input_data)
            execution_id = result.get("id")
            if execution_id is not None:
                await jobs.set_adopted_execution_id(run_id, org_id, uuid.UUID(str(execution_id)))
            return {"run_id": run_id_s, "execution_id": execution_id}
        finally:
            await registry.aclose()
            await jobs.close()


def enqueue_roundtable(rt_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Fire-and-forget: hand a round-table to the driver over the broker."""
    drive_roundtable_task.delay(str(rt_id), str(organisation_id), str(user_id))


def enqueue_adopted_tool(
    run_id: uuid.UUID,
    instance_id: uuid.UUID,
    input_data: dict[str, Any],
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Fire-and-forget: hand an ADOPTED_TOOL_RUN schedule fire to the registry-execute worker over
    the broker (#489). NOT awaited inline in the Beat sweep — a slow/down registry must never block
    the cross-org tick (the dedupe row is already written, so the dispatch is non-duplicate)."""
    run_adopted_tool_task.delay(
        str(run_id), str(instance_id), input_data, str(organisation_id), str(user_id)
    )


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
        headers = build_downstream_headers(principal, settings)
        harness = HarnessClient(
            settings.harness_runtime_url,
            headers=headers,
            timeout=settings.harness_request_timeout,
        )
        # the flow judge — grade the completed run at the gate (#477); same identity headers, so KRS
        # server-stamps the verdict's org from THIS run's principal (H2).
        evaluate = EvaluateClient(
            settings.knowledge_retriever_url,
            headers=headers,
            timeout=settings.evaluate_request_timeout,
        )
        # ADR-043 #552: the coded loop done-check reads the graph's LANDED artifacts (org-scoped by
        # the same downstream headers) to confirm a loop's work persisted before it can converge.
        artifacts = ArtifactsClient(settings.knowledge_graph_url, headers=headers)
        # #601: a SCHEDULED team-run's settled cost accrues into its schedule's per-cadence
        # accumulator (the #598 cap reads it) — the worker drive is where cost settles.
        schedules = ScheduleRepository(settings.database_url, worker_pool=True)
        try:
            service = TeamRunService(
                team_runs=team_runs,
                harness=harness,
                evaluate=evaluate,
                artifacts=artifacts,
                schedules=schedules,
            )
            result = await service.drive(run_id, principal)
            return {"team_run_id": run_id_s, "state": result.state}
        finally:
            await harness.aclose()
            await evaluate.aclose()
            await artifacts.aclose()
            await team_runs.close()
            await schedules.close()


def enqueue_team_run(run_id: uuid.UUID, organisation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Fire-and-forget: hand a QUEUED team run to the worker driver over the broker."""
    drive_team_run_task.delay(str(run_id), str(organisation_id), str(user_id))
