"""Gateway jwt-mode `verify_token` — iss/aud contract enforcement (#356).

The gateway is the platform's sole external surface and the primary user-token verifier. These tests
prove the cross-service contract end-to-end: a token minted by the REAL auth-service issuer
(`create_user_token`, which stamps the shared iss/aud) is ACCEPTED by the gateway verifier — so the
issuer and this verifier agree — while every non-conforming token is DENIED:

  missing aud → 401, missing iss → 401, wrong aud → 401, wrong iss → 401.

No network: the gateway decodes the token locally with the shared secret. Both sides read the same
``oraclous_governance`` contract (default iss/aud), so a single source of truth is exercised.
"""

from __future__ import annotations

import time

import pytest
from jose import jwt
from oraclous_application_gateway_service.core.auth import AuthError, verify_token
from oraclous_application_gateway_service.core.config import get_settings
from oraclous_governance import DEFAULT_JWT_AUDIENCE, DEFAULT_JWT_ISSUER, PrincipalType

pytestmark = pytest.mark.unit

_SECRET = "gateway-jwt-aud-iss-test-secret"  # noqa: S105 — test signing key
_USER = "11111111-1111-1111-1111-111111111111"
_ORG = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _jwt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Gateway in jwt mode + the auth-service mint share ONE secret + the default iss/aud contract.
    monkeypatch.setenv("GATEWAY_AUTH_MODE", "jwt")
    monkeypatch.setenv("JWT_SECRET", _SECRET)  # consumed by both the gateway verify + auth mint
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _sign(claims: dict) -> str:
    """Sign a raw token with the shared secret (used for the deny cases that hand-craft claims)."""
    return jwt.encode(claims, _SECRET, algorithm="HS256")


def test_accepts_real_auth_service_user_token() -> None:
    """A token minted by the real auth-service issuer is accepted by the gateway verifier — the
    issuer and verifier agree on the iss/aud contract (cross-service interop)."""
    from oraclous_auth_service.core.jwt_handler import create_user_token

    token, _ = create_user_token(user_id=_USER, organisation_id=_ORG, email="a@b.test")
    principal = verify_token(token)
    assert str(principal.principal_id) == _USER
    assert principal.principal_type == PrincipalType.USER
    assert principal.organisation_id is not None and str(principal.organisation_id) == _ORG


def test_rejects_token_missing_aud() -> None:
    token = _sign(
        {
            "sub": _USER,
            "type": "access",
            "organisation_id": _ORG,
            "iss": DEFAULT_JWT_ISSUER,
            "exp": int(time.time()) + 3600,
        }
    )
    with pytest.raises(AuthError):
        verify_token(token)


def test_rejects_token_missing_iss() -> None:
    token = _sign(
        {
            "sub": _USER,
            "type": "access",
            "organisation_id": _ORG,
            "aud": DEFAULT_JWT_AUDIENCE,
            "exp": int(time.time()) + 3600,
        }
    )
    with pytest.raises(AuthError):
        verify_token(token)


def test_rejects_token_with_wrong_aud() -> None:
    token = _sign(
        {
            "sub": _USER,
            "type": "access",
            "organisation_id": _ORG,
            "iss": DEFAULT_JWT_ISSUER,
            "aud": "some-other-service",
            "exp": int(time.time()) + 3600,
        }
    )
    with pytest.raises(AuthError):
        verify_token(token)


def test_rejects_token_with_wrong_iss() -> None:
    token = _sign(
        {
            "sub": _USER,
            "type": "access",
            "organisation_id": _ORG,
            "iss": "evil-issuer",
            "aud": DEFAULT_JWT_AUDIENCE,
            "exp": int(time.time()) + 3600,
        }
    )
    with pytest.raises(AuthError):
        verify_token(token)
