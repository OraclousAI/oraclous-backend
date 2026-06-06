"""DI providers (ORAA-4 §21 core layer) — wiring only.

Resolves the authenticated principal (gateway / dev / jwt), builds the per-request registry client
with the *downstream* identity headers (so the registry sees the same tenant — ADR-018 propagation),
and exposes the harness execution service. Long-lived collaborators (the Postgres repository + the
provenance collector) are opened once in ``lifespan`` and read off ``app.state``.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from oraclous_governance import Principal
from oraclous_substrate import ProvenanceCollector

from oraclous_harness_runtime_service.core.auth import (
    AuthError,
    principal_from_gateway_headers,
    verify_token,
)
from oraclous_harness_runtime_service.core.config import Settings, get_settings
from oraclous_harness_runtime_service.domain.ohm.signatures import TrustStore
from oraclous_harness_runtime_service.repositories.execution_repository import ExecutionRepository
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
)
from oraclous_harness_runtime_service.services.registry_client import RegistryClient

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


def build_downstream_headers(principal: Principal, settings: Settings) -> dict[str, str]:
    """Identity to forward to the registry/broker (ADR-018). dev → a bearer; gateway/jwt → the
    verified principal headers + the shared internal key."""
    if settings.auth_mode == "dev":
        return {"Authorization": f"Bearer {settings.dev_bearer}"}
    headers = {
        "X-Principal-Id": str(principal.principal_id),
        "X-Principal-Type": principal.principal_type.value,
    }
    if principal.organisation_id:
        headers["X-Organisation-Id"] = str(principal.organisation_id)
    if settings.internal_service_key:
        headers["X-Internal-Key"] = settings.internal_service_key
    return headers


async def get_registry_client(
    principal: Annotated[Principal, Depends(get_principal)],
) -> AsyncIterator[RegistryClient]:
    settings = get_settings()
    client = RegistryClient(
        settings.capability_registry_url,
        headers=build_downstream_headers(principal, settings),
    )
    try:
        yield client
    finally:
        await client.aclose()


def get_execution_repository(request: Request) -> ExecutionRepository:
    repo = getattr(request.app.state, "execution_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="harness store unavailable (DATABASE_URL not reachable)",
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


def get_trust_store(request: Request) -> TrustStore:
    store = getattr(request.app.state, "trust_store", None)
    return store if store is not None else TrustStore({})


def get_harness_service(
    registry: Annotated[RegistryClient, Depends(get_registry_client)],
    executions: Annotated[ExecutionRepository, Depends(get_execution_repository)],
    provenance: Annotated[ProvenanceCollector, Depends(get_provenance)],
    trust: Annotated[TrustStore, Depends(get_trust_store)],
) -> HarnessExecutionService:
    settings = get_settings()
    return HarnessExecutionService(
        registry=registry,
        executions=executions,
        provenance=provenance,
        trust=trust,
        require_signature=settings.ohm_require_signature,
        llm_mode=settings.llm_mode,
        max_iterations=settings.max_iterations,
    )


PrincipalDep = Annotated[Principal, Depends(get_principal)]
ExecutionRepositoryDep = Annotated[ExecutionRepository, Depends(get_execution_repository)]
HarnessServiceDep = Annotated[HarnessExecutionService, Depends(get_harness_service)]
