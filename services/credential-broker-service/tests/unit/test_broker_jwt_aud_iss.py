"""Credential-broker jwt-mode `verify_token` — iss/aud contract enforcement (#356).

A downstream verifier (one of seven) proving the SAME contract the gateway enforces: a token minted
by the real auth-service issuer is ACCEPTED; a token missing aud, missing iss, with a wrong aud, or
a wrong iss is DENIED. The broker decodes locally with the shared secret; both sides read the shared
``oraclous_governance`` contract, so issuer and verifier agree.
"""

from __future__ import annotations

import time

import pytest
from jose import jwt
from oraclous_credential_broker_service.core.auth import AuthError, verify_token
from oraclous_credential_broker_service.core.config import get_settings
from oraclous_governance import DEFAULT_JWT_AUDIENCE, DEFAULT_JWT_ISSUER, PrincipalType

pytestmark = pytest.mark.unit

_SECRET = "broker-jwt-aud-iss-test-secret"  # noqa: S105 — test signing key
_USER = "11111111-1111-1111-1111-111111111111"
_ORG = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _jwt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("JWT_SECRET", _SECRET)  # consumed by both the broker verify + auth mint
    # The broker's pydantic settings require these to construct (unrelated to the token contract).
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleQ==")  # noqa: S105 — test value
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "broker-jwt-aud-iss-test-internal-key")
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _sign(claims: dict) -> str:
    return jwt.encode(claims, _SECRET, algorithm="HS256")


async def test_accepts_real_auth_service_user_token() -> None:
    """A token minted by the real auth-service issuer is accepted by the broker verifier."""
    from oraclous_auth_service.core.jwt_handler import create_user_token

    token, _ = create_user_token(user_id=_USER, organisation_id=_ORG, email="a@b.test")
    principal = await verify_token(token)
    assert str(principal.principal_id) == _USER
    assert principal.principal_type == PrincipalType.USER
    assert principal.organisation_id is not None and str(principal.organisation_id) == _ORG


async def test_rejects_token_missing_aud() -> None:
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
        await verify_token(token)


async def test_rejects_token_missing_iss() -> None:
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
        await verify_token(token)


async def test_rejects_token_with_wrong_aud() -> None:
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
        await verify_token(token)


async def test_rejects_token_with_wrong_iss() -> None:
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
        await verify_token(token)
