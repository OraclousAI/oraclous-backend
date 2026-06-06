"""Identity seam (ORAA-4 §21 core layer) — pluggable principal verification (harness runtime).

Mirrors the knowledge-retriever seam so the mode swap is local and consistent across services:
`gateway` (ADR-018) trusts the gateway's verified `X-Principal-*`/`X-Organisation-Id` headers gated
by `X-Internal-Key`; `dev` accepts a fixed bearer → a fixed dev principal in the shared dev org;
`jwt` decodes a real HS256 auth-service token, whose `organisation_id` claim becomes the bound scope
(fail-closed). The resolved principal is what the runtime later FORWARDS to the registry/broker on
service-to-service calls (so downstream org-scoping sees the same tenant).
"""

from __future__ import annotations

import uuid

from jose import JWTError, jwt
from oraclous_governance import MembershipResolver, Principal, PrincipalType

from oraclous_harness_runtime_service.core.config import get_settings


class AuthError(Exception):
    """Authentication failed. Maps to HTTP 401."""


def principal_from_gateway_headers(
    principal_id: str | None, principal_type: str | None, organisation_id: str | None
) -> Principal:
    """Build a Principal from the gateway's verified identity headers (ADR-018 edge-auth).

    The gateway terminates auth and injects ``X-Principal-Id``/``X-Principal-Type``/
    ``X-Organisation-Id`` (stripping client-supplied copies); this service trusts them and does NOT
    re-validate a token. Fail-closed if identity is absent/malformed; the org header is REQUIRED
    (the runtime is org-scoped — never silently fall back to a default org)."""
    if not principal_id or not principal_type or not organisation_id:
        raise AuthError("gateway identity headers missing")
    try:
        return Principal(
            principal_id=uuid.UUID(principal_id),
            principal_type=PrincipalType(principal_type),
            organisation_id=uuid.UUID(organisation_id),
        )
    except ValueError as exc:
        raise AuthError("malformed gateway identity headers") from exc


def _principal_from_claims(claims: dict) -> Principal:
    """Build a Principal from verified JWT claims, enforcing the cross-service Contract."""
    if claims.get("type") != "access":
        raise AuthError("a user access token is required")
    sub = claims.get("sub") or ""
    if "@" in sub:
        raise AuthError("legacy email-subject tokens are not accepted")
    organisation_id = claims.get("organisation_id")
    if not organisation_id:
        raise AuthError("token is missing organisation_id")
    try:
        principal_type = PrincipalType(claims.get("principal_type", "user"))
        return Principal(
            principal_id=uuid.UUID(sub),
            principal_type=principal_type,
            organisation_id=uuid.UUID(organisation_id),
        )
    except ValueError as exc:
        raise AuthError("malformed principal claims") from exc


async def verify_token(token: str) -> Principal:
    """Resolve a bearer token to an authenticated Principal. Patchable module-level name."""
    settings = get_settings()
    if settings.auth_mode == "dev":
        if token != settings.dev_bearer:
            raise AuthError("invalid dev bearer token")
        return Principal(
            principal_id=uuid.UUID(settings.dev_user_id),
            principal_type=PrincipalType.USER,
            organisation_id=uuid.UUID(settings.dev_org_id),
        )
    if not settings.jwt_secret:
        raise AuthError("HARNESS_AUTH_MODE=jwt requires HARNESS_JWT_SECRET")
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise AuthError("invalid or expired token") from exc
    return _principal_from_claims(claims)


class StaticMembershipResolver(MembershipResolver):
    """Dev resolver: every principal belongs to the single configured dev organisation."""

    def __init__(self, organisation_id: uuid.UUID) -> None:
        self._organisation_id = organisation_id

    async def organisations_for(self, principal: Principal) -> list[uuid.UUID]:
        return [self._organisation_id]
