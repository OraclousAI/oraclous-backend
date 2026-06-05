"""Edge identity termination (ORAA-4 §21 core layer) — verify ONCE at the gateway.

Reuses the ``oraclous-governance`` ``Principal`` and the SAME claim contract the substrate services
enforce, so the gateway is not a second source of truth: ``dev`` mode maps the fixed ``dev-token``
to the seeded dev principal/org; ``jwt`` mode verifies the real HS256 token against the shared
``JWT_SECRET`` (``type==access``, non-empty ``organisation_id``, ``sub`` a UUID not an email, valid
signature, not expired). Fail-closed: any problem raises ``AuthError`` (→ 401) pre-forward.
"""

from __future__ import annotations

import uuid

from jose import JWTError, jwt
from oraclous_governance import Principal, PrincipalType

from oraclous_application_gateway_service.core.config import get_settings


class AuthError(Exception):
    """Authentication failed. Maps to HTTP 401."""


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


def verify_token(token: str) -> Principal:
    """Resolve a bearer token to an authenticated Principal (dev or jwt mode)."""
    settings = get_settings()
    if settings.GATEWAY_AUTH_MODE == "dev":
        if token != settings.DEV_BEARER:
            raise AuthError("invalid dev bearer token")
        return Principal(
            principal_id=uuid.UUID(settings.DEV_USER_ID),
            principal_type=PrincipalType.USER,
            organisation_id=uuid.UUID(settings.DEV_ORG_ID),
        )
    if not settings.JWT_SECRET:
        raise AuthError("GATEWAY_AUTH_MODE=jwt requires JWT_SECRET")
    try:
        claims = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise AuthError("invalid or expired token") from exc
    return _principal_from_claims(claims)
