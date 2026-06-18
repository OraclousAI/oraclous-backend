"""Identity seam (ORAA-4 §21 core layer) — pluggable principal verification.

Mirrors the KGS/KRS/credential-broker seam so the capability registry scopes every descriptor
read/write by the authenticated principal's organisation (ORG001: org comes from the token, never
the request body). `dev` mode: a fixed bearer → fixed dev principal+org. `jwt` mode: a real HS256
token from the identity/auth service, decoded with the shared `JWT_SECRET` per the JWT/Principal
Contract — the token's `organisation_id` claim becomes the bound scope (fail-closed).
"""

from __future__ import annotations

import uuid

from jose import JWTError, jwt
from oraclous_governance import (
    JWT_REQUIRED_OPTIONS,
    Principal,
    PrincipalType,
    jwt_audience,
    jwt_issuer,
)

from oraclous_capability_registry_service.core.config import get_settings


class AuthError(Exception):
    """Authentication failed. Maps to HTTP 401."""


def principal_from_gateway_headers(
    principal_id: str | None,
    principal_type: str | None,
    organisation_id: str | None,
    org_role: str | None = None,
) -> Principal:
    """Build a Principal from the gateway's verified identity headers (ADR-018 edge-auth).

    The gateway terminates auth and injects ``X-Principal-Id``/``X-Principal-Type``/
    ``X-Organisation-Id`` (+ ``X-Principal-Org-Role`` since R7-SEC S2), stripping client copies;
    this service trusts them and does NOT re-validate a token. Fail-closed if the identity is absent
    or malformed; the org header is REQUIRED (org-scoped services never fall back to a default org).
    ``org_role`` is optional (absent on agent/service tokens; ``None`` never satisfies an admin
    gate)."""
    if not principal_id or not principal_type or not organisation_id:
        raise AuthError("gateway identity headers missing")
    try:
        return Principal(
            principal_id=uuid.UUID(principal_id),
            principal_type=PrincipalType(principal_type),
            organisation_id=uuid.UUID(organisation_id),
            org_role=org_role,
        )
    except ValueError as exc:
        raise AuthError("malformed gateway identity headers") from exc


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
        claims = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            audience=jwt_audience(),
            issuer=jwt_issuer(),
            options=JWT_REQUIRED_OPTIONS,
        )
    except JWTError as exc:
        raise AuthError("invalid or expired token") from exc
    return _principal_from_claims(claims)
