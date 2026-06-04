"""DI providers for the user-identity routes (ORAA-4 §21 core layer) — wiring only.

The session comes from the app-state sessionmaker (wired by `core/lifespan` in production, or by the
test fixtures), committed on success via `session_scope`. `get_auth_service` assembles the
repositories + service for one request; `current_user_claims` decodes the bearer for authenticated
routes. Exposed as `Annotated[...]` aliases (B008-clean) for route signatures.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_auth_service.core.database import session_scope
from oraclous_auth_service.core.jwt_handler import decode_token
from oraclous_auth_service.repositories.invitation_repository import InvitationRepository
from oraclous_auth_service.repositories.oauth_repository import (
    OAuthAccountRepository,
    OAuthStateRepository,
)
from oraclous_auth_service.repositories.org_member_repository import OrgMemberRepository
from oraclous_auth_service.repositories.organisation_repository import OrganisationRepository
from oraclous_auth_service.repositories.refresh_token_repository import RefreshTokenRepository
from oraclous_auth_service.repositories.user_repository import UserRepository
from oraclous_auth_service.services.auth_service import AuthService
from oraclous_auth_service.services.invitation_service import InvitationService
from oraclous_auth_service.services.oauth_provider_client import HttpxProviderClient
from oraclous_auth_service.services.oauth_service import OAuthService
from oraclous_auth_service.services.org_service import OrgService

_bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    maker = getattr(request.app.state, "sessionmaker", None)
    if maker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="identity store unavailable (DATABASE_URL not configured)",
        )
    async for session in session_scope(maker):
        yield session


def get_org_service(session: Annotated[AsyncSession, Depends(get_session)]) -> OrgService:
    return OrgService(
        organisations=OrganisationRepository(session),
        members=OrgMemberRepository(session),
    )


def get_invitation_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InvitationService:
    return InvitationService(
        invitations=InvitationRepository(session),
        members=OrgMemberRepository(session),
        organisations=OrganisationRepository(session),
    )


def get_oauth_service(
    session: Annotated[AsyncSession, Depends(get_session)], request: Request
) -> OAuthService:
    # The provider HTTP client is injectable via app.state (tests set a fake; prod uses httpx).
    client = getattr(request.app.state, "oauth_provider_client", None) or HttpxProviderClient()
    org_service = OrgService(
        organisations=OrganisationRepository(session),
        members=OrgMemberRepository(session),
    )
    auth_service = AuthService(
        users=UserRepository(session),
        refresh_tokens=RefreshTokenRepository(session),
        orgs=org_service,
    )
    return OAuthService(
        users=UserRepository(session),
        orgs=org_service,
        auth=auth_service,
        accounts=OAuthAccountRepository(session),
        states=OAuthStateRepository(session),
        client=client,
    )


def get_auth_service(session: Annotated[AsyncSession, Depends(get_session)]) -> AuthService:
    return AuthService(
        users=UserRepository(session),
        refresh_tokens=RefreshTokenRepository(session),
        orgs=OrgService(
            organisations=OrganisationRepository(session),
            members=OrgMemberRepository(session),
        ),
    )


def current_user_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict:
    """Decode the bearer into claims and require a `user` access token. 401 on any failure."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_token(credentials.credentials)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if claims.get("type") != "access" or claims.get("principal_type") != "user":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a user access token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
OrgServiceDep = Annotated[OrgService, Depends(get_org_service)]
InvitationServiceDep = Annotated[InvitationService, Depends(get_invitation_service)]
OAuthServiceDep = Annotated[OAuthService, Depends(get_oauth_service)]
UserClaimsDep = Annotated[dict, Depends(current_user_claims)]
