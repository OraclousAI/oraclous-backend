"""DI providers (ORAA-4 §21 core layer) — wiring only.

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
from oraclous_execution_engine_service.services.harness_client import HarnessClient
from oraclous_execution_engine_service.services.job_service import JobService
from oraclous_execution_engine_service.services.task_service import TaskService
from oraclous_execution_engine_service.tasks.run_tasks import enqueue_job

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


def get_provenance(request: Request) -> ProvenanceCollector:
    collector = getattr(request.app.state, "provenance", None)
    if collector is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provenance sink unavailable (DATABASE_URL not reachable)",
        )
    return collector


def get_job_service(
    jobs: Annotated[JobRepository, Depends(get_job_repository)],
    provenance: Annotated[ProvenanceCollector, Depends(get_provenance)],
) -> JobService:
    # The request path submits + cancels (enqueue to the worker); it never runs the harness itself,
    # so no harness client here — the worker builds its own (run_tasks.py).
    return JobService(jobs=jobs, provenance=provenance, enqueue=enqueue_job)


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


PrincipalDep = Annotated[Principal, Depends(get_principal)]
JobRepositoryDep = Annotated[JobRepository, Depends(get_job_repository)]
JobServiceDep = Annotated[JobService, Depends(get_job_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
