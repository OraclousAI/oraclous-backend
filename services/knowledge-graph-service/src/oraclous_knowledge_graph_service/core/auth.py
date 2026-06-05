"""Identity seam (ORAA-4 §21 core layer) — pluggable principal verification.

`dev` mode: a fixed bearer resolves to a fixed dev principal, and a `StaticMembershipResolver` maps
it to the single dev organisation (the key-free local seam). `jwt` mode (R3.5-P3): a real HS256 JWT
from the auth-service is decoded with the shared `KGS_JWT_SECRET` per the cross-service
JWT/Principal Contract — the token's `organisation_id` claim becomes the bound org (fail-closed).
The write path
still flows through `oraclous_substrate.access.enforced_organisation_id()` in both modes.
`verify_token` keeps one signature so the mode swap is local.
"""

from __future__ import annotations

import uuid

from jose import JWTError, jwt
from oraclous_governance import MembershipResolver, Principal, PrincipalType

from oraclous_knowledge_graph_service.core.config import get_settings


class AuthError(Exception):
    """Authentication failed. Maps to HTTP 401."""


def principal_from_gateway_headers(
    principal_id: str | None, principal_type: str | None, organisation_id: str | None
) -> Principal:
    """Build a Principal from the gateway's verified identity headers (ADR-018 edge-auth).

    The gateway terminates auth and injects ``X-Principal-Id``/``X-Principal-Type``/
    ``X-Organisation-Id`` (stripping any client-supplied copies); this service trusts them and does
    NOT re-validate a token. Fail-closed if the identity is absent or malformed; the org header is
    REQUIRED (these are org-scoped services — never silently fall back to a default org)."""
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
    """Build a Principal from verified JWT claims, enforcing the Contract (fail-closed)."""
    if claims.get("type") != "access":
        raise AuthError("a user access token is required")
    sub = claims.get("sub") or ""
    if "@" in sub:
        # Legacy tokens used the email as `sub`; the Contract uses a UUID. Reject the old shape.
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
        )
    if not settings.jwt_secret:
        raise AuthError("KGS_AUTH_MODE=jwt requires KGS_JWT_SECRET")
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise AuthError("invalid or expired token") from exc
    return _principal_from_claims(claims)


class StaticMembershipResolver(MembershipResolver):
    """Dev resolver: every principal belongs to the single configured dev organisation.

    Drop-in for the real membership resolver (identity/org service) later — same Protocol.
    """

    def __init__(self, organisation_id: uuid.UUID) -> None:
        self._organisation_id = organisation_id

    async def organisations_for(self, principal: Principal) -> list[uuid.UUID]:
        return [self._organisation_id]
