"""Failing unit tests for the agent JWT issuance (ORA-31 / R1-A2).

What these tests pin (ORA-31 acceptance criteria):

* ``create_agent_token(agent_id, organisation_id)`` returns a JWT signed with the
  service's secret carrying ``principal_type=agent``, ``sub=agent_id``, the
  ``organisation_id`` claim (the ORA-3 auth-side pairing — Option B, ratified),
  and the standard short-life token housekeeping (``iat``, ``exp``, ``jti``).
* The token is short-lived — the SA token's 15-minute window is the documented
  precedent and the legacy ``_SA_TOKEN_EXPIRE_MINUTES`` from
  ``auth-service/app/core/jwt_handler.py``. Pinning the cap here keeps the
  implementer from drifting the reshape.
* The returned ``expires_in`` matches the JWT's ``exp`` claim within tolerance —
  guards against the "token says 60 min, response body says 15 min" footgun.
* ``jti`` is unique across two issuances for the same agent — required for any
  future revocation list keyed by ``jti``.

Behavioural reference (Lift): legacy ``auth-service/app/core/jwt_handler.py``
``create_service_account_token``. Reshape: drop ``tenant_id`` and
``home_graph_id`` (legacy KG-specific claims); add ``organisation_id``; rename
``principal_type`` value from ``service_account`` to ``agent``.

These tests describe behaviour at the function seam — no FastAPI app, no
database. They are RED until ``oraclous_auth_service.core.jwt_handler`` exists.
"""

from __future__ import annotations

import os
import time

import pytest
from jose import jwt

pytestmark = pytest.mark.unit

_AGENT_ID = "agent-1111-aaaa"
_ORG_ID = "org-2222-bbbb"


def _read_token_settings() -> tuple[str, str]:
    """The implementer chooses the loading mechanism; tests pin behaviour, not config wiring.

    The legacy module reads ``settings.JWT_SECRET`` / ``settings.JWT_ALGORITHM``;
    the reshape may use a tighter loader. Both are acceptable as long as the
    test environment can override them. The tests below use environment
    variables; the implementer's module is expected to honour them (the canonical
    seam is ``oraclous_auth_service.core.config.settings``, which reads from env).
    """
    return (
        os.environ.setdefault("JWT_SECRET", "test-secret-for-ora-31-not-production"),
        os.environ.setdefault("JWT_ALGORITHM", "HS256"),
    )


def test_create_agent_token_returns_token_and_expires_in() -> None:
    """``create_agent_token`` returns a ``(token, expires_in_seconds)`` pair."""
    _read_token_settings()
    from oraclous_auth_service.core.jwt_handler import create_agent_token

    token, expires_in = create_agent_token(agent_id=_AGENT_ID, organisation_id=_ORG_ID)

    assert isinstance(token, str) and token.count(".") == 2
    assert isinstance(expires_in, int) and expires_in > 0


def test_agent_token_carries_principal_type_agent_and_org_id() -> None:
    """Decoded claims pin the substrate's authenticated-principal shape (ADR-006)."""
    secret, algorithm = _read_token_settings()
    from oraclous_auth_service.core.jwt_handler import create_agent_token

    token, _ = create_agent_token(agent_id=_AGENT_ID, organisation_id=_ORG_ID)
    claims = jwt.decode(token, secret, algorithms=[algorithm])

    assert claims["sub"] == _AGENT_ID
    assert claims["principal_type"] == "agent"
    assert claims["organisation_id"] == _ORG_ID
    # The legacy SA-only claims must NOT appear on agent tokens — they belong to
    # a different principal type and would mislead substrate consumers.
    assert "tenant_id" not in claims
    assert "home_graph_id" not in claims


def test_agent_token_is_short_lived_capped_at_fifteen_minutes() -> None:
    """``exp`` is within 15 minutes of ``iat`` — short-life precedent from legacy SA tokens."""
    secret, algorithm = _read_token_settings()
    from oraclous_auth_service.core.jwt_handler import create_agent_token

    token, _ = create_agent_token(agent_id=_AGENT_ID, organisation_id=_ORG_ID)
    claims = jwt.decode(token, secret, algorithms=[algorithm])

    assert "iat" in claims and "exp" in claims
    lifetime_seconds = int(claims["exp"]) - int(claims["iat"])
    assert 0 < lifetime_seconds <= 15 * 60


def test_agent_token_expires_in_matches_exp_claim() -> None:
    """The returned ``expires_in`` matches ``exp - now`` within a small jitter."""
    secret, algorithm = _read_token_settings()
    from oraclous_auth_service.core.jwt_handler import create_agent_token

    before = int(time.time())
    token, expires_in = create_agent_token(agent_id=_AGENT_ID, organisation_id=_ORG_ID)
    claims = jwt.decode(token, secret, algorithms=[algorithm])

    # exp is in seconds-since-epoch; expires_in is a duration in seconds.
    # Allow a 2-second jitter for clock granularity at the boundary.
    assert abs(int(claims["exp"]) - (before + expires_in)) <= 2


def test_agent_token_jti_is_unique_per_issuance() -> None:
    """Two tokens for the same agent get distinct ``jti`` claims (revocation invariant)."""
    secret, algorithm = _read_token_settings()
    from oraclous_auth_service.core.jwt_handler import create_agent_token

    t1, _ = create_agent_token(agent_id=_AGENT_ID, organisation_id=_ORG_ID)
    t2, _ = create_agent_token(agent_id=_AGENT_ID, organisation_id=_ORG_ID)

    c1 = jwt.decode(t1, secret, algorithms=[algorithm])
    c2 = jwt.decode(t2, secret, algorithms=[algorithm])

    assert "jti" in c1 and "jti" in c2
    assert c1["jti"] != c2["jti"]


def test_agent_token_rejects_empty_organisation_id() -> None:
    """An empty ``organisation_id`` is a fail-closed error, not a silent empty claim.

    ADR-006: every authenticated context must carry an organisation_id; the
    auth-service refuses to mint a token without one rather than mint one whose
    claim is empty (which the substrate would then have to reject downstream).
    """
    _read_token_settings()
    from oraclous_auth_service.core.jwt_handler import create_agent_token

    with pytest.raises(ValueError):
        create_agent_token(agent_id=_AGENT_ID, organisation_id="")
