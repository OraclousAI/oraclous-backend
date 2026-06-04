"""JWT issuance for every principal type (ORA-31 / R1-A2 · extended R3.5-P3-S1).

One issuer, one shared secret (HS256), one decode path — the cross-service JWT/Principal Contract
(`oraclous-knowledge/flows/interface-contracts.md`). Every token carries `organisation_id` (ADR-006
fail-closed) and a `type` claim (`"access"` vs `"refresh"`) so a refresh token can never be replayed
as an access token (closes the legacy refresh-reuse gap, T-JWT-TYPE). KGS/KRS `jwt`-mode verifiers
decode with the same secret and reject any token whose `type != "access"`, whose `sub` looks like an
email (legacy email-sub rejection), or whose `organisation_id` is empty.

Principal-type → claim:
  user             sub=user_id          principal_type="user"            type=access  (+email)
  user (refresh)   sub=user_id          principal_type="user"            type=refresh (+jti row)
  agent            sub=agent_id         principal_type="agent"           type=access
  service_account  sub=sa_id            principal_type="service_account" type=access

All functions are fail-closed: an empty ``organisation_id`` is rejected before encoding so the
substrate never has to reject an empty-claim token downstream.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from jose import jwt

from oraclous_auth_service.core.config import get_settings


def _encode(
    payload: dict,
    *,
    ttl_seconds: int,
    principal_type: str,
    kind: str,
    organisation_id: str,
    sub: str,
    jti: str | None = None,
) -> tuple[str, int, str]:
    """Build + sign a token from the shared claim skeleton. Returns (token, ttl, jti)."""
    if not sub:
        raise ValueError("sub is required")
    if not organisation_id:
        # ADR-006 fail-closed: refuse to mint a token with an empty org claim.
        raise ValueError("organisation_id is required")
    issued_at = datetime.now(UTC)
    token_jti = jti or str(uuid.uuid4())
    claims = {
        "sub": sub,
        "principal_type": principal_type,
        "type": kind,
        "organisation_id": organisation_id,
        "iat": issued_at,
        "exp": issued_at + timedelta(seconds=ttl_seconds),
        "jti": token_jti,
        **payload,
    }
    settings = get_settings()
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, ttl_seconds, token_jti


def create_user_token(*, user_id: str, organisation_id: str, email: str) -> tuple[str, int]:
    """Issue a short-lived user ACCESS JWT. Returns ``(access_token, expires_in_seconds)``."""
    ttl = get_settings().user_access_token_ttl_minutes * 60
    token, expires_in, _ = _encode(
        {"email": email},
        ttl_seconds=ttl,
        principal_type="user",
        kind="access",
        organisation_id=organisation_id,
        sub=user_id,
    )
    return token, expires_in


def create_user_refresh_token(
    *, user_id: str, organisation_id: str, email: str, jti: str
) -> tuple[str, int]:
    """Issue a long-lived user REFRESH JWT bound to ``jti`` (the rotation-family key in Postgres).

    The `type="refresh"` claim makes it unusable as an access token; rotation/reuse-detection is
    enforced by the refresh_tokens table (the jti must be `active`).
    """
    ttl = get_settings().refresh_token_ttl_days * 24 * 3600
    token, expires_in, _ = _encode(
        {"email": email},
        ttl_seconds=ttl,
        principal_type="user",
        kind="refresh",
        organisation_id=organisation_id,
        sub=user_id,
        jti=jti,
    )
    return token, expires_in


def create_agent_token(*, agent_id: str, organisation_id: str) -> tuple[str, int]:
    """Issue a short-lived agent JWT.

    Returns ``(access_token, expires_in_seconds)``. The JWT carries
    ``sub=agent_id``, ``principal_type="agent"``, ``organisation_id``,
    ``iat``, ``exp`` and a unique ``jti`` (revocation-list key).
    """
    if not agent_id:
        raise ValueError("agent_id is required")
    ttl = get_settings().agent_token_ttl_minutes * 60
    token, expires_in, _ = _encode(
        {},
        ttl_seconds=ttl,
        principal_type="agent",
        kind="access",
        organisation_id=organisation_id,
        sub=agent_id,
    )
    return token, expires_in


def decode_token(token: str) -> dict:
    """Decode and verify a JWT issued by this service.

    Raises ``jose.JWTError`` (or a subclass) on any signature / expiry / format
    failure. Callers translate that into the appropriate HTTP response.
    """
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
