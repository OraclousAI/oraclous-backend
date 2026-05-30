"""Agent JWT issuance (ORA-31 / R1-A2).

Reshaped from the legacy ``auth-service/app/core/jwt_handler.create_service_account_token``:

* ``principal_type`` claim value is ``"agent"`` (was ``"service_account"``).
* The token carries ``organisation_id`` (the ORA-3 auth-side pairing) — no
  ``tenant_id`` and no ``home_graph_id`` (legacy SA-only claims, dropped per
  reshape rules).
* The 15-minute cap is preserved as the documented short-life precedent.

The function is fail-closed: an empty ``organisation_id`` is rejected before
encoding so the substrate never has to reject an empty-claim token downstream.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from jose import jwt

from oraclous_auth_service.core.config import get_settings


def create_agent_token(*, agent_id: str, organisation_id: str) -> tuple[str, int]:
    """Issue a short-lived agent JWT.

    Returns ``(access_token, expires_in_seconds)``. The JWT carries
    ``sub=agent_id``, ``principal_type="agent"``, ``organisation_id``,
    ``iat``, ``exp`` and a unique ``jti`` (revocation-list key).
    """
    if not agent_id:
        raise ValueError("agent_id is required")
    if not organisation_id:
        # ADR-006 fail-closed: every authenticated context must carry an
        # organisation_id, and we refuse to mint a token without one rather
        # than emit a token with an empty claim that the substrate would then
        # have to reject downstream.
        raise ValueError("organisation_id is required")

    settings = get_settings()
    ttl_seconds = settings.agent_token_ttl_minutes * 60
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)

    payload = {
        "sub": agent_id,
        "principal_type": "agent",
        "organisation_id": organisation_id,
        "iat": issued_at,
        "exp": expires_at,
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, ttl_seconds


def decode_token(token: str) -> dict:
    """Decode and verify a JWT issued by this service.

    Raises ``jose.JWTError`` (or a subclass) on any signature / expiry / format
    failure. Callers translate that into the appropriate HTTP response.
    """
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
