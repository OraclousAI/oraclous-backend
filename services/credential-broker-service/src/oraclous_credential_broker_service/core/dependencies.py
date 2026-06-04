"""DI providers (ORAA-4 §21 core layer) — wiring only.

Repositories/services are opened once in `core/lifespan` and resolved per request from `app.state`.
The X-Internal-Key verifier gates the service-to-service endpoints (fail-closed, constant-time).
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from oraclous_credential_broker_service.core.config import get_settings
from oraclous_credential_broker_service.repositories.credential_repository import (
    CredentialRepository,
)
from oraclous_credential_broker_service.services.delegation_service import DelegationService


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


CredentialRepositoryDep = Annotated[CredentialRepository, Depends(get_credential_repository)]
DelegationServiceDep = Annotated[DelegationService, Depends(get_delegation_service)]
