"""Identity seam (ORAA-4 §21 core layer) — pluggable principal verification.

Mirrors the KGS/KRS seam so the credential-broker scopes every credential read/write by the
authenticated principal's organisation (ORG001: org comes from the token, never the request body).
`dev` mode: a fixed bearer → fixed dev principal+org. `jwt` mode: a real HS256 token from the
identity/auth service, decoded with the shared `JWT_SECRET` per the JWT/Principal Contract — the
token's `organisation_id` claim becomes the bound scope (fail-closed).
"""

from __future__ import annotations

import uuid

from jose import JWTError, jwt
from oraclous_governance import Principal, PrincipalType

from oraclous_credential_broker_service.core.config import get_settings


class AuthError(Exception):
    """Authentication failed. Maps to HTTP 401."""


def organisation_id_from_gateway_headers(organisation_id: str | None) -> uuid.UUID:
    """Resolve the bound org from the gateway's verified ``X-Organisation-Id`` header (ADR-018).

    The gateway terminates auth and injects the org id (stripping any client-supplied copy); the
    user-facing edge trusts it and does NOT re-validate a token. Credential rows are org-scoped, so
    only the org is needed here. Fail-closed if absent or malformed."""
    if not organisation_id:
        raise AuthError("gateway organisation header missing")
    try:
        return uuid.UUID(organisation_id)
    except ValueError as exc:
        raise AuthError("malformed gateway organisation header") from exc


def principal_id_from_gateway_headers(principal_id: str | None) -> uuid.UUID:
    """Resolve the authenticated user from the gateway's verified ``X-Principal-Id`` (ADR-018).

    The gateway terminates auth and injects the principal id (stripping any client-supplied copy);
    the user-facing edge trusts it. Credentials are personal, so the caller can only ever act on
    their own — the user id is taken from here, never from the request body/query. Fail-closed."""
    if not principal_id:
        raise AuthError("gateway principal header missing")
    try:
        return uuid.UUID(principal_id)
    except ValueError as exc:
        raise AuthError("malformed gateway principal header") from exc


def _principal_from_claims(claims: dict) -> Principal:
    if claims.get("type") != "access":
        raise AuthError("an access token is required")
    sub = claims.get("sub") or ""
    if "@" in sub:
        raise AuthError("legacy email-subject tokens are not accepted")
    organisation_id = claims.get("organisation_id")
    if not organisation_id:
        raise AuthError("token is missing organisation_id")
    try:
        return Principal(
            principal_id=uuid.UUID(sub),
            principal_type=PrincipalType(claims.get("principal_type", "user")),
            organisation_id=uuid.UUID(organisation_id),
        )
    except ValueError as exc:
        raise AuthError("malformed principal claims") from exc


async def verify_token(token: str) -> Principal:
    """Resolve a bearer token to an authenticated Principal. Patchable module-level name."""
    settings = get_settings()
    if settings.AUTH_MODE == "dev":
        if token != settings.DEV_BEARER:
            raise AuthError("invalid dev bearer token")
        return Principal(
            principal_id=uuid.UUID(settings.DEV_USER_ID),
            principal_type=PrincipalType.USER,
            organisation_id=uuid.UUID(settings.DEV_ORG_ID),
        )
    if not settings.JWT_SECRET:
        raise AuthError("AUTH_MODE=jwt requires JWT_SECRET")
    try:
        claims = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise AuthError("invalid or expired token") from exc
    return _principal_from_claims(claims)
