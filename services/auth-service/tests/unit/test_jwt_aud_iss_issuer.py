"""Auth-service issuer side of the iss/aud contract (#356).

Proves the auth-service STAMPS the shared iss/aud on every token type and that its own
``decode_token`` REQUIRES + verifies them (the auth-service decodes its own tokens on refresh, so it
holds to the same contract every other verifier enforces):

  * every mint (user / refresh / agent / service-account) carries the shared iss + aud
  * a round-tripped real token is accepted by decode_token
  * a token missing aud, missing iss, with a wrong aud, or a wrong iss is rejected by decode_token
"""

from __future__ import annotations

import os
import time

import pytest
from jose import jwt
from oraclous_governance import DEFAULT_JWT_AUDIENCE, DEFAULT_JWT_ISSUER

pytestmark = [pytest.mark.unit, pytest.mark.security]

_SECRET = "auth-iss-aud-test-secret"  # noqa: S105 — test signing key
_USER = "11111111-1111-1111-1111-111111111111"
_ORG = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _jwt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", _SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)


def _raw_claims(token: str) -> dict:
    # Inspect claims WITHOUT verifying aud/iss (we are asserting they are present + correct).
    return jwt.decode(
        token,
        _SECRET,
        algorithms=["HS256"],
        options={"verify_aud": False, "verify_signature": True},
    )


def test_every_mint_stamps_iss_and_aud() -> None:
    from oraclous_auth_service.core.jwt_handler import (
        create_agent_token,
        create_service_account_token,
        create_user_refresh_token,
        create_user_token,
    )

    tokens = [
        create_user_token(user_id=_USER, organisation_id=_ORG, email="a@b.test")[0],
        create_user_refresh_token(user_id=_USER, organisation_id=_ORG, email="a@b.test", jti="j1")[
            0
        ],
        create_agent_token(agent_id=_USER, organisation_id=_ORG)[0],
        create_service_account_token(service_account_id=_USER, organisation_id=_ORG)[0],
    ]
    for token in tokens:
        claims = _raw_claims(token)
        assert claims["iss"] == DEFAULT_JWT_ISSUER
        assert claims["aud"] == DEFAULT_JWT_AUDIENCE


def test_decode_token_accepts_round_tripped_real_token() -> None:
    from oraclous_auth_service.core.jwt_handler import create_user_token, decode_token

    token, _ = create_user_token(user_id=_USER, organisation_id=_ORG, email="a@b.test")
    claims = decode_token(token)
    assert claims["sub"] == _USER
    assert claims["organisation_id"] == _ORG


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"iss": DEFAULT_JWT_ISSUER}, id="missing-aud"),
        pytest.param({"aud": DEFAULT_JWT_AUDIENCE}, id="missing-iss"),
        pytest.param({"aud": "wrong", "iss": DEFAULT_JWT_ISSUER}, id="wrong-aud"),
        pytest.param({"aud": DEFAULT_JWT_AUDIENCE, "iss": "evil"}, id="wrong-iss"),
    ],
)
def test_decode_token_rejects_non_conforming(bad: dict) -> None:
    """decode_token (the auth-service's own verifier) denies a token that breaks the iss/aud
    contract — not merely a presence assert: a non-conforming token RAISES."""
    from jose import JWTError
    from oraclous_auth_service.core.jwt_handler import decode_token

    os.environ["JWT_SECRET"] = _SECRET
    claims = {
        "sub": _USER,
        "type": "access",
        "organisation_id": _ORG,
        "exp": int(time.time()) + 3600,
        **bad,
    }
    token = jwt.encode(claims, _SECRET, algorithm="HS256")
    with pytest.raises(JWTError):
        decode_token(token)
