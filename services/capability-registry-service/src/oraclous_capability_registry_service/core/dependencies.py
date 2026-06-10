"""DI providers (ORAA-4 §21 core layer) — wiring only.

The repository is opened once in ``core/lifespan`` and resolved per request from ``app.state``. The
X-Internal-Key verifier gates service-to-service endpoints (fail-closed, constant-time). The caller
organisation is resolved from the authenticated principal (ORG001 — never the request body).
"""

from __future__ import annotations

import secrets
import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from oraclous_governance import Principal, org_role_at_least

from oraclous_capability_registry_service.core.auth import (
    AuthError,
    principal_from_gateway_headers,
    verify_token,
)
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.repositories.execution_repository import (
    ExecutionRepository,
)
from oraclous_capability_registry_service.repositories.instance_repository import InstanceRepository
from oraclous_capability_registry_service.services.capability_registry_service import (
    CapabilityRegistryService,
)
from oraclous_capability_registry_service.services.credential_client import CredentialBrokerPort
from oraclous_capability_registry_service.services.instance_manager import InstanceManager
from oraclous_capability_registry_service.services.mcp_import_service import McpImportService
from oraclous_capability_registry_service.services.tool_execution_service import (
    ToolExecutionService,
)
from oraclous_capability_registry_service.services.validation_service import ValidationService

_bearer = HTTPBearer(auto_error=False)


def get_capability_repository(request: Request) -> CapabilityRepository:
    repo = getattr(request.app.state, "capability_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="capability store unavailable (DATABASE_URL not configured)",
        )
    return repo


def get_instance_repository(request: Request) -> InstanceRepository:
    repo = getattr(request.app.state, "instance_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="instance store unavailable (DATABASE_URL not configured)",
        )
    return repo


def get_capability_registry_service(
    repo: Annotated[CapabilityRepository, Depends(get_capability_repository)],
) -> CapabilityRegistryService:
    return CapabilityRegistryService(repository=repo)


def get_mcp_import_service(
    repo: Annotated[CapabilityRepository, Depends(get_capability_repository)],
) -> McpImportService:
    return McpImportService(capabilities=repo)


def get_instance_manager(
    instances: Annotated[InstanceRepository, Depends(get_instance_repository)],
    capabilities: Annotated[CapabilityRepository, Depends(get_capability_repository)],
) -> InstanceManager:
    return InstanceManager(instances=instances, capabilities=capabilities)


def get_validation_service(
    instances: Annotated[InstanceRepository, Depends(get_instance_repository)],
    capabilities: Annotated[CapabilityRepository, Depends(get_capability_repository)],
) -> ValidationService:
    return ValidationService(instances=instances, capabilities=capabilities)


def get_execution_repository(request: Request) -> ExecutionRepository:
    repo = getattr(request.app.state, "execution_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="execution store unavailable (DATABASE_URL not configured)",
        )
    return repo


def get_credential_broker(request: Request) -> CredentialBrokerPort:
    broker = getattr(request.app.state, "credential_broker", None)
    if broker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="credential broker unavailable",
        )
    return broker


def get_tool_execution_service(
    instances: Annotated[InstanceRepository, Depends(get_instance_repository)],
    capabilities: Annotated[CapabilityRepository, Depends(get_capability_repository)],
    executions: Annotated[ExecutionRepository, Depends(get_execution_repository)],
    broker: Annotated[CredentialBrokerPort, Depends(get_credential_broker)],
) -> ToolExecutionService:
    return ToolExecutionService(
        instances=instances, capabilities=capabilities, executions=executions, broker=broker
    )


async def verify_internal_key(x_internal_key: Annotated[str | None, Header()] = None) -> None:
    """Gate service-to-service endpoints on ``X-Internal-Key`` (constant-time, fail-closed 401)."""
    expected = get_settings().INTERNAL_SERVICE_KEY
    if (
        not expected
        or x_internal_key is None
        or not secrets.compare_digest(x_internal_key, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal service key",
        )


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    x_principal_id: Annotated[str | None, Header()] = None,
    x_principal_type: Annotated[str | None, Header()] = None,
    x_organisation_id: Annotated[str | None, Header()] = None,
    x_principal_org_role: Annotated[str | None, Header()] = None,
    x_internal_key: Annotated[str | None, Header()] = None,
) -> Principal:
    """Resolve the authenticated principal (org + user come from here).

    In ``gateway`` mode (ADR-018) the gateway terminates auth and injects the verified
    X-Principal-*/X-Organisation-Id headers, gated by X-Internal-Key — no token validation here.
    In ``dev``/``jwt`` mode the bearer token is resolved directly.
    """
    if get_settings().AUTH_MODE == "gateway":
        # Reuse the existing fail-closed constant-time internal-key gate (401 if not from gateway).
        await verify_internal_key(x_internal_key)
        try:
            principal = principal_from_gateway_headers(
                x_principal_id, x_principal_type, x_organisation_id, x_principal_org_role
            )
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
    elif credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    else:
        try:
            principal = await verify_token(credentials.credentials)
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
    if principal.organisation_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token has no organisation scope"
        )
    return principal


async def get_organisation_id(
    principal: Annotated[Principal, Depends(get_principal)],
) -> uuid.UUID:
    """Resolve the caller org from the authenticated principal (ORG001 — never the body)."""
    assert principal.organisation_id is not None  # noqa: S101 — guaranteed by get_principal
    return principal.organisation_id


CapabilityRepositoryDep = Annotated[CapabilityRepository, Depends(get_capability_repository)]
InstanceRepositoryDep = Annotated[InstanceRepository, Depends(get_instance_repository)]
CapabilityRegistryServiceDep = Annotated[
    CapabilityRegistryService, Depends(get_capability_registry_service)
]
McpImportServiceDep = Annotated[McpImportService, Depends(get_mcp_import_service)]
InstanceManagerDep = Annotated[InstanceManager, Depends(get_instance_manager)]
ValidationServiceDep = Annotated[ValidationService, Depends(get_validation_service)]
ToolExecutionServiceDep = Annotated[ToolExecutionService, Depends(get_tool_execution_service)]
ExecutionRepositoryDep = Annotated[ExecutionRepository, Depends(get_execution_repository)]


async def require_admin(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    """An org-admin action (the supply-chain HITL approval of an imported MCP tool). Asserts the
    gateway-forwarded ``org_role`` ranks at least admin (owner ≥ admin), fail-closed — a None/member
    role is 403. Reuses the canonical R7-SEC S2 rank predicate; the role is trust-asserted by the
    gateway, never client-set."""
    if not org_role_at_least(principal.org_role, minimum="admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this operation requires an organisation admin",
        )
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]
AdminDep = Annotated[Principal, Depends(require_admin)]
OrganisationIdDep = Annotated[uuid.UUID, Depends(get_organisation_id)]
