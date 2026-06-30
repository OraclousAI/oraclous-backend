"""DI providers (core layer) — wiring only.

Resolves the authenticated principal (gateway / dev / jwt), builds the per-request harness client
with the *downstream* identity headers (so the harness sees the same tenant — ADR-018 propagation),
and exposes the job service. The Postgres repository + provenance collector are opened once in
``lifespan`` and read off ``app.state``.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector

from oraclous_execution_engine_service.core.auth import (
    AuthError,
    build_downstream_headers,
    principal_from_gateway_headers,
    verify_token,
)
from oraclous_execution_engine_service.core.config import get_settings
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.repositories.provenance_repository import (
    ProvenanceRepository,
)
from oraclous_execution_engine_service.repositories.roundtable_repository import (
    RoundtableRepository,
)
from oraclous_execution_engine_service.repositories.schedule_repository import ScheduleRepository
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.activity_service import ActivityService
from oraclous_execution_engine_service.services.graph_client import GraphClient
from oraclous_execution_engine_service.services.harness_client import HarnessClient
from oraclous_execution_engine_service.services.job_service import JobService
from oraclous_execution_engine_service.services.roundtable_service import RoundtableService
from oraclous_execution_engine_service.services.schedule_service import ScheduleService
from oraclous_execution_engine_service.services.task_service import TaskService
from oraclous_execution_engine_service.services.team_run_service import TeamRunService
from oraclous_execution_engine_service.tasks.run_tasks import (
    enqueue_adopted_tool,
    enqueue_job,
    enqueue_roundtable,
    enqueue_team_run,
)

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _require_internal_key(provided: str | None) -> None:
    expected = get_settings().internal_service_key
    if not expected or not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="request did not originate at the gateway"
        )


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    x_principal_id: Annotated[str | None, Header()] = None,
    x_principal_type: Annotated[str | None, Header()] = None,
    x_organisation_id: Annotated[str | None, Header()] = None,
    x_internal_key: Annotated[str | None, Header()] = None,
) -> Principal:
    settings = get_settings()
    if settings.auth_mode == "gateway":
        _require_internal_key(x_internal_key)
        try:
            return principal_from_gateway_headers(
                x_principal_id, x_principal_type, x_organisation_id
            )
        except AuthError as exc:
            raise _unauthorized(str(exc)) from exc
    if credentials is None:
        raise _unauthorized("missing bearer token")
    try:
        return await verify_token(credentials.credentials)
    except AuthError as exc:
        raise _unauthorized(str(exc)) from exc


def get_job_repository(request: Request) -> JobRepository:
    repo = getattr(request.app.state, "job_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="engine store unavailable (DATABASE_URL not reachable)",
        )
    return repo


def get_schedule_repository(request: Request) -> ScheduleRepository:
    repo = getattr(request.app.state, "schedule_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="engine store unavailable (DATABASE_URL not reachable)",
        )
    return repo


def get_provenance(request: Request) -> ProvenanceCollector:
    collector = getattr(request.app.state, "provenance", None)
    if collector is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provenance sink unavailable (DATABASE_URL not reachable)",
        )
    return collector


def get_provenance_repository(request: Request) -> ProvenanceRepository:
    repo = getattr(request.app.state, "provenance_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="engine store unavailable (DATABASE_URL not reachable)",
        )
    return repo


def get_activity_service(
    provenance: Annotated[ProvenanceRepository, Depends(get_provenance_repository)],
) -> ActivityService:
    return ActivityService(provenance=provenance)


def get_job_service(
    jobs: Annotated[JobRepository, Depends(get_job_repository)],
    provenance: Annotated[ProvenanceCollector, Depends(get_provenance)],
) -> JobService:
    # The request path submits + cancels (enqueue to the worker); it never runs the harness itself,
    # so no harness client here — the worker builds its own (run_tasks.py).
    return JobService(jobs=jobs, provenance=provenance, enqueue=enqueue_job)


def get_schedule_service(
    schedules: Annotated[ScheduleRepository, Depends(get_schedule_repository)],
    jobs: Annotated[JobRepository, Depends(get_job_repository)],
    team_runs: Annotated[TeamRunRepository, Depends(get_team_run_repository)],
    graphs: Annotated[GraphClient, Depends(get_graph_client)],
    provenance: Annotated[ProvenanceCollector, Depends(get_provenance)],
) -> ScheduleService:
    # the request path registers/lists/deletes AND fires-now (#489): fire-now reuses the Beat fire
    # branch, so it needs ALL enqueue callbacks (harness-job + adopted-tool + #601 team-run)
    # — without enqueue_team_run/team_runs the team branch would create the dedupe row + advance the
    # cursor but never dispatch (a green-but-hollow no-op).
    return ScheduleService(
        schedules=schedules,
        jobs=jobs,
        provenance=provenance,
        enqueue=enqueue_job,
        enqueue_adopted_tool=enqueue_adopted_tool,
        enqueue_team_run=enqueue_team_run,
        team_runs=team_runs,
        graphs=graphs,
    )


def get_roundtable_repository(request: Request) -> RoundtableRepository:
    repo = getattr(request.app.state, "roundtable_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="engine store unavailable (DATABASE_URL not reachable)",
        )
    return repo


def get_roundtable_service(
    roundtables: Annotated[RoundtableRepository, Depends(get_roundtable_repository)],
    provenance: Annotated[ProvenanceCollector, Depends(get_provenance)],
) -> RoundtableService:
    # the request path creates + responds (enqueues the driver); the worker drives via the harness.
    return RoundtableService(
        roundtables=roundtables, provenance=provenance, enqueue=enqueue_roundtable
    )


async def get_harness_client(
    principal: Annotated[Principal, Depends(get_principal)],
) -> AsyncIterator[HarnessClient]:
    # the task-board complete is a request-path call to the harness — forward the caller's identity.
    settings = get_settings()
    client = HarnessClient(
        settings.harness_runtime_url,
        headers=build_downstream_headers(principal, settings),
        timeout=settings.harness_request_timeout,
    )
    try:
        yield client
    finally:
        await client.aclose()


def get_task_service(
    jobs: Annotated[JobRepository, Depends(get_job_repository)],
    harness: Annotated[HarnessClient, Depends(get_harness_client)],
    provenance: Annotated[ProvenanceCollector, Depends(get_provenance)],
) -> TaskService:
    return TaskService(jobs=jobs, harness=harness, provenance=provenance)


def get_team_run_repository(request: Request) -> TeamRunRepository:
    repo = getattr(request.app.state, "team_run_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="engine store unavailable (DATABASE_URL not reachable)",
        )
    return repo


async def get_graph_client(
    principal: Annotated[Principal, Depends(get_principal)],
) -> AsyncIterator[GraphClient]:
    # the request-path create validates a graph-bound run's graph_id org-scoped (#524) — forward the
    # caller's identity so KGS scopes the GET to the same tenant (cross-org graph → 404 → rejected).
    settings = get_settings()
    client = GraphClient(
        settings.knowledge_graph_url,
        headers=build_downstream_headers(principal, settings),
        timeout=settings.knowledge_graph_request_timeout,
    )
    try:
        yield client
    finally:
        await client.aclose()


def get_team_run_service(
    team_runs: Annotated[TeamRunRepository, Depends(get_team_run_repository)],
    graphs: Annotated[GraphClient, Depends(get_graph_client)],
) -> TeamRunService:
    # the request path only validates/creates/advances + ENQUEUES; the worker drives the team
    # (run_tasks.drive_team_run_task), so a large team never blocks the request. No harness here.
    # `graphs` is the request-path KGS existence check for a graph-bound run's graph_id (#524).
    return TeamRunService(team_runs=team_runs, enqueue=enqueue_team_run, graphs=graphs)


PrincipalDep = Annotated[Principal, Depends(get_principal)]
JobRepositoryDep = Annotated[JobRepository, Depends(get_job_repository)]
JobServiceDep = Annotated[JobService, Depends(get_job_service)]
ActivityServiceDep = Annotated[ActivityService, Depends(get_activity_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
ScheduleServiceDep = Annotated[ScheduleService, Depends(get_schedule_service)]
RoundtableServiceDep = Annotated[RoundtableService, Depends(get_roundtable_service)]
TeamRunServiceDep = Annotated[TeamRunService, Depends(get_team_run_service)]
