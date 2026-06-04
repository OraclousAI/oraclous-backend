"""Unit tests for R3.5-P3-S1 user identity: JWT contract claims + password domain.

No DB — these pin the cross-service JWT/Principal Contract (the claims KGS/KRS verify) and the
password policy. The full register/login/refresh flow against real Postgres is the integration test.
"""

from __future__ import annotations

import pytest
from jose import jwt
from oraclous_auth_service.core.jwt_handler import (
    create_agent_token,
    create_user_refresh_token,
    create_user_token,
)
from oraclous_auth_service.domain.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password_strength,
    verify_password,
)

pytestmark = pytest.mark.unit

_SECRET = "unit-test-secret"  # noqa: S105 — test signing key, not a secret
_USER = "11111111-1111-1111-1111-111111111111"
_ORG = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", _SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")


def _decode(token: str) -> dict:
    return jwt.decode(token, _SECRET, algorithms=["HS256"])


# --- JWT/Principal Contract ---------------------------------------------------
def test_user_access_token_carries_contract_claims() -> None:
    token, expires_in = create_user_token(user_id=_USER, organisation_id=_ORG, email="a@b.com")
    claims = _decode(token)
    assert claims["sub"] == _USER
    assert claims["principal_type"] == "user"
    assert claims["type"] == "access"  # the anti-refresh-replay claim
    assert claims["organisation_id"] == _ORG
    assert claims["email"] == "a@b.com"
    assert "jti" in claims and expires_in > 0


def test_refresh_token_is_typed_refresh_and_bound_to_jti() -> None:
    token, _ = create_user_refresh_token(
        user_id=_USER, organisation_id=_ORG, email="a@b.com", jti="the-jti"
    )
    claims = _decode(token)
    assert claims["type"] == "refresh"
    assert claims["jti"] == "the-jti"


def test_agent_token_is_access_typed_for_the_verifier() -> None:
    # agent tokens must carry type=access or the KGS/KRS verifier would reject them
    token, _ = create_agent_token(agent_id="agent-1", organisation_id=_ORG)
    claims = _decode(token)
    assert claims["principal_type"] == "agent"
    assert claims["type"] == "access"
    assert claims["organisation_id"] == _ORG


def test_empty_org_is_refused_fail_closed() -> None:
    with pytest.raises(ValueError, match="organisation_id"):
        create_user_token(user_id=_USER, organisation_id="", email="a@b.com")


# --- password domain ----------------------------------------------------------
def test_password_round_trips() -> None:
    h = hash_password("Sup3rStrong")
    assert h != "Sup3rStrong"
    assert verify_password("Sup3rStrong", h)
    assert not verify_password("wrong", h)
    assert not verify_password("anything", None)  # OAuth-only user


@pytest.mark.parametrize("bad", ["short1A", "alllowercase1", "ALLUPPERCASE1", "NoDigitsHere"])
def test_weak_passwords_rejected(bad: str) -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password_strength(bad)


def test_strong_password_accepted() -> None:
    validate_password_strength("GoodPass1")  # no raise
