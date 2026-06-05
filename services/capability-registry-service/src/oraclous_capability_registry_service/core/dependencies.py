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

from oraclous_capability_registry_service.core.auth import AuthError, verify_token
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.services.capability_registry_service import (
    CapabilityRegistryService,
)

_bearer = HTTPBearer(auto_error=False)


def get_capability_repository(request: Request) -> CapabilityRepository:
    repo = getattr(request.app.state, "capability_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="capability store unavailable (DATABASE_URL not configured)",
        )
    return repo


def get_capability_registry_service(
    repo: Annotated[CapabilityRepository, Depends(get_capability_repository)],
) -> CapabilityRegistryService:
    return CapabilityRegistryService(repository=repo)


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


async def get_organisation_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> uuid.UUID:
    """Resolve the caller org from the authenticated principal (ORG001 — never the body)."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
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
    return principal.organisation_id


CapabilityRepositoryDep = Annotated[CapabilityRepository, Depends(get_capability_repository)]
CapabilityRegistryServiceDep = Annotated[
    CapabilityRegistryService, Depends(get_capability_registry_service)
]
OrganisationIdDep = Annotated[uuid.UUID, Depends(get_organisation_id)]
