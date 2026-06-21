"""DI providers (core layer) — wiring only.

Repositories/services are opened once in `core/lifespan` and resolved per request from `app.state`.
The X-Internal-Key verifier gates the service-to-service endpoints (fail-closed, constant-time).
"""

from __future__ import annotations

import secrets
import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from oraclous_credential_broker_service.core.auth import (
    AuthError,
    organisation_id_from_gateway_headers,
    principal_id_from_gateway_headers,
    verify_token,
)
from oraclous_credential_broker_service.core.config import get_settings
from oraclous_credential_broker_service.repositories.credential_repository import (
    CredentialRepository,
)
from oraclous_credential_broker_service.repositories.webhook_secret_repository import (
    WebhookSecretRepository,
)
from oraclous_credential_broker_service.services.credential_broker_service import (
    CredentialBrokerService,
)
from oraclous_credential_broker_service.services.credential_service import CredentialService
from oraclous_credential_broker_service.services.delegation_service import DelegationService
from oraclous_credential_broker_service.services.envelope_service import EnvelopeService
from oraclous_credential_broker_service.services.refresh_client import HttpxRefreshClient
from oraclous_credential_broker_service.services.webhook_secret_service import WebhookSecretService

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
    x_organisation_id: Annotated[str | None, Header()] = None,
    x_internal_key: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    """Resolve the caller org from the authenticated principal (ORG001 — never the body)."""
    settings = get_settings()
    # gateway mode (ADR-018): trust the gateway's verified X-Organisation-Id header, gated by the
    # existing constant-time X-Internal-Key verifier — no token validation at the edge.
    if settings.AUTH_MODE == "gateway":
        await verify_internal_key(x_internal_key)
        try:
            return organisation_id_from_gateway_headers(x_organisation_id)
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
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


async def get_principal_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    x_principal_id: Annotated[str | None, Header()] = None,
    x_internal_key: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    """Resolve the authenticated user from the principal — credentials are personal, so a caller
    can only ever act on their own (the user id is never taken from the request body/query)."""
    settings = get_settings()
    if settings.AUTH_MODE == "gateway":
        await verify_internal_key(x_internal_key)
        try:
            return principal_id_from_gateway_headers(x_principal_id)
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
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
    return principal.principal_id


def get_webhook_secret_repository(request: Request) -> WebhookSecretRepository:
    repo = getattr(request.app.state, "webhook_secret_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook-secret store unavailable (DATABASE_URL not configured)",
        )
    return repo


def get_envelope_service(request: Request) -> EnvelopeService:
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="envelope-encryption seam unavailable (DATABASE_URL not configured)",
        )
    return envelope


def get_webhook_secret_service(
    repo: Annotated[WebhookSecretRepository, Depends(get_webhook_secret_repository)],
    envelope: Annotated[EnvelopeService, Depends(get_envelope_service)],
) -> WebhookSecretService:
    return WebhookSecretService(repository=repo, envelope=envelope)


def get_credential_service(
    repo: Annotated[CredentialRepository, Depends(get_credential_repository)],
    envelope: Annotated[EnvelopeService, Depends(get_envelope_service)],
) -> CredentialService:
    return CredentialService(repository=repo, envelope=envelope)


def get_credential_broker_service(
    request: Request,
    repo: Annotated[CredentialRepository, Depends(get_credential_repository)],
    envelope: Annotated[EnvelopeService, Depends(get_envelope_service)],
) -> CredentialBrokerService:
    # The provider refresh client is injectable via app.state (tests set a fake; prod uses httpx).
    client = getattr(request.app.state, "refresh_client", None) or HttpxRefreshClient()
    return CredentialBrokerService(credentials=repo, refresh_client=client, envelope=envelope)


CredentialRepositoryDep = Annotated[CredentialRepository, Depends(get_credential_repository)]
CredentialServiceDep = Annotated[CredentialService, Depends(get_credential_service)]
CredentialBrokerServiceDep = Annotated[
    CredentialBrokerService, Depends(get_credential_broker_service)
]
DelegationServiceDep = Annotated[DelegationService, Depends(get_delegation_service)]
WebhookSecretServiceDep = Annotated[WebhookSecretService, Depends(get_webhook_secret_service)]
OrganisationIdDep = Annotated[uuid.UUID, Depends(get_organisation_id)]
PrincipalUserIdDep = Annotated[uuid.UUID, Depends(get_principal_user_id)]
