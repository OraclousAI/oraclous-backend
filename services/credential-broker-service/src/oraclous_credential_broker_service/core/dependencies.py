"""DI providers (ORAA-4 §21 core layer) — wiring only.

Repositories/services are opened once in `core/lifespan` and resolved per request from `app.state`.
The X-Internal-Key verifier gates the service-to-service endpoints (fail-closed, constant-time).
"""

from __future__ import annotations

import secrets
import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from oraclous_credential_broker_service.core.auth import AuthError, verify_token
from oraclous_credential_broker_service.core.config import get_settings
from oraclous_credential_broker_service.repositories.credential_repository import (
    CredentialRepository,
)
from oraclous_credential_broker_service.services.credential_service import CredentialService
from oraclous_credential_broker_service.services.delegation_service import DelegationService

_bearer = HTTPBearer(auto_error=False)


def get_credential_repository(request: Request) -> CredentialRepository:
    repo = getattr(request.app.state, "credential_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="credential store unavailable (DATABASE_URL not configured)",
        )
    return repo


def get_delegation_service(request: Request) -> DelegationService:
    svc = getattr(request.app.state, "delegation_service", None)
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="delegation store unavailable (DATABASE_URL not configured)",
        )
    return svc


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


def get_credential_service(
    repo: Annotated[CredentialRepository, Depends(get_credential_repository)],
) -> CredentialService:
    return CredentialService(repository=repo)


CredentialRepositoryDep = Annotated[CredentialRepository, Depends(get_credential_repository)]
CredentialServiceDep = Annotated[CredentialService, Depends(get_credential_service)]
DelegationServiceDep = Annotated[DelegationService, Depends(get_delegation_service)]
OrganisationIdDep = Annotated[uuid.UUID, Depends(get_organisation_id)]
