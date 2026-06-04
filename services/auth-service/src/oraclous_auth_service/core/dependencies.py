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
from oraclous_auth_service.repositories.refresh_token_repository import RefreshTokenRepository
from oraclous_auth_service.repositories.user_repository import UserRepository
from oraclous_auth_service.services.auth_service import AuthService

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


def get_auth_service(session: Annotated[AsyncSession, Depends(get_session)]) -> AuthService:
    return AuthService(
        users=UserRepository(session),
        refresh_tokens=RefreshTokenRepository(session),
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
UserClaimsDep = Annotated[dict, Depends(current_user_claims)]
