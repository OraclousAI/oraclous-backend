"""Unit tests for the shared JWT issuer/audience contract (#356).

The single source of truth every issuer + verifier reads. Pins the dev defaults, the env override,
empty-as-unset, and the python-jose enforcement options that make aud/iss/exp REQUIRED + verified.
"""

from __future__ import annotations

import time

import pytest
from jose import jwt
from oraclous_governance import (
    DEFAULT_JWT_AUDIENCE,
    DEFAULT_JWT_ISSUER,
    JWT_REQUIRED_OPTIONS,
    jwt_audience,
    jwt_issuer,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

_SECRET = "jwt-contract-test-secret"  # noqa: S105 — test signing key


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    assert jwt_issuer() == DEFAULT_JWT_ISSUER == "oraclous-auth"
    assert jwt_audience() == DEFAULT_JWT_AUDIENCE == "oraclous-platform"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_ISSUER", "custom-iss")
    monkeypatch.setenv("JWT_AUDIENCE", "custom-aud")
    assert jwt_issuer() == "custom-iss"
    assert jwt_audience() == "custom-aud"


def test_empty_env_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank override must never produce an empty iss/aud (which would weaken enforcement)."""
    monkeypatch.setenv("JWT_ISSUER", "")
    monkeypatch.setenv("JWT_AUDIENCE", "")
    assert jwt_issuer() == DEFAULT_JWT_ISSUER
    assert jwt_audience() == DEFAULT_JWT_AUDIENCE


def test_required_options_make_claims_mandatory() -> None:
    """The options dict turns aud/iss/exp from 'verified IF present' into REQUIRED + verified.

    Proves the enforcement the verifiers rely on: a correct token is accepted; a token missing any
    of aud/iss/exp, or carrying a wrong aud/iss, raises under the shared options."""
    now = int(time.time())

    def mk(claims: dict) -> str:
        return jwt.encode(claims, _SECRET, algorithm="HS256")

    def decode(token: str) -> dict:
        return jwt.decode(
            token,
            _SECRET,
            algorithms=["HS256"],
            audience=DEFAULT_JWT_AUDIENCE,
            issuer=DEFAULT_JWT_ISSUER,
            options=JWT_REQUIRED_OPTIONS,
        )

    good = mk(
        {"sub": "x", "aud": DEFAULT_JWT_AUDIENCE, "iss": DEFAULT_JWT_ISSUER, "exp": now + 3600}
    )
    assert decode(good)["sub"] == "x"

    for bad in (
        {"sub": "x", "iss": DEFAULT_JWT_ISSUER, "exp": now + 3600},  # missing aud
        {"sub": "x", "aud": DEFAULT_JWT_AUDIENCE, "exp": now + 3600},  # missing iss
        {"sub": "x", "aud": DEFAULT_JWT_AUDIENCE, "iss": DEFAULT_JWT_ISSUER},  # missing exp
        {"sub": "x", "aud": "wrong", "iss": DEFAULT_JWT_ISSUER, "exp": now + 3600},  # wrong aud
        {"sub": "x", "aud": DEFAULT_JWT_AUDIENCE, "iss": "wrong", "exp": now + 3600},  # wrong iss
    ):
        with pytest.raises(Exception):  # noqa: B017,PT011 — JWTError/JWTClaimsError family
            decode(mk(bad))
