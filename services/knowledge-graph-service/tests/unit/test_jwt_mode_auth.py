"""KGS jwt-mode `verify_token` — the consumer side of the cross-service JWT/Principal Contract.

Proves KGS accepts an auth-service-shaped access token (sub=UUID, type=access, organisation_id,
plus the shared iss/aud — #356) and fail-closed rejects refresh tokens, legacy email-subject tokens,
org-less tokens, tokens signed with a different secret, AND tokens that violate the iss/aud contract
(missing aud, missing iss, wrong aud, wrong iss). No DB / no network — signs tokens with the shared
secret directly. A correctly-stamped token is ACCEPTED; every non-conforming one is DENIED.
"""

from __future__ import annotations

import time

import pytest
from jose import jwt
from oraclous_governance import (
    DEFAULT_JWT_AUDIENCE,
    DEFAULT_JWT_ISSUER,
    PrincipalType,
)
from oraclous_knowledge_graph_service.core.auth import AuthError, verify_token
from oraclous_knowledge_graph_service.core.config import get_settings

pytestmark = pytest.mark.unit

_SECRET = "kgs-jwt-mode-test-secret"  # noqa: S105 — test signing key
_USER = "11111111-1111-1111-1111-111111111111"
_ORG = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _jwt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGS_AUTH_MODE", "jwt")
    monkeypatch.setenv("KGS_JWT_SECRET", _SECRET)
    # Leave JWT_ISSUER/JWT_AUDIENCE unset so the shared contract defaults apply on both the in-test
    # mint and the verifier — exactly the dev wiring.
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _sign(claims: dict, *, secret: str = _SECRET, with_contract: bool = True) -> str:
    """Sign a token. ``with_contract`` stamps the shared iss/aud + an exp (the default a real
    auth-service token carries); individual tests override pieces to violate the contract."""
    body = dict(claims)
    if with_contract:
        body.setdefault("iss", DEFAULT_JWT_ISSUER)
        body.setdefault("aud", DEFAULT_JWT_AUDIENCE)
        body.setdefault("exp", int(time.time()) + 3600)
    return jwt.encode(body, secret, algorithm="HS256")


async def test_accepts_valid_user_access_token() -> None:
    token = _sign(
        {"sub": _USER, "principal_type": "user", "type": "access", "organisation_id": _ORG}
    )
    principal = await verify_token(token)
    assert str(principal.principal_id) == _USER
    assert principal.principal_type == PrincipalType.USER
    assert principal.organisation_id is not None and str(principal.organisation_id) == _ORG


async def test_rejects_refresh_token() -> None:
    token = _sign(
        {"sub": _USER, "principal_type": "user", "type": "refresh", "organisation_id": _ORG}
    )
    with pytest.raises(AuthError):
        await verify_token(token)


async def test_rejects_legacy_email_subject() -> None:
    token = _sign({"sub": "user@example.com", "type": "access", "organisation_id": _ORG})
    with pytest.raises(AuthError):
        await verify_token(token)


async def test_rejects_missing_organisation_id() -> None:
    token = _sign({"sub": _USER, "type": "access"})
    with pytest.raises(AuthError):
        await verify_token(token)


async def test_rejects_token_signed_with_wrong_secret() -> None:
    token = _sign(
        {"sub": _USER, "type": "access", "organisation_id": _ORG},
        secret="not-the-shared-secret",  # noqa: S106 — deliberately wrong test key
    )
    with pytest.raises(AuthError):
        await verify_token(token)


# --- iss/aud contract enforcement (#356): a non-conforming token is DENIED ---------------------


async def test_rejects_token_missing_aud() -> None:
    """A token with no ``aud`` claim is rejected (require_aud), even when everything else is valid.

    This is the enforcement bite: pre-#356 such a token was accepted."""
    token = jwt.encode(
        {
            "sub": _USER,
            "type": "access",
            "organisation_id": _ORG,
            "iss": DEFAULT_JWT_ISSUER,
            "exp": int(time.time()) + 3600,
        },
        _SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        await verify_token(token)


async def test_rejects_token_missing_iss() -> None:
    """A token with no ``iss`` claim is rejected (require_iss)."""
    token = jwt.encode(
        {
            "sub": _USER,
            "type": "access",
            "organisation_id": _ORG,
            "aud": DEFAULT_JWT_AUDIENCE,
            "exp": int(time.time()) + 3600,
        },
        _SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        await verify_token(token)


async def test_rejects_token_with_wrong_aud() -> None:
    """A token whose ``aud`` is some other service's audience is rejected (verify_aud)."""
    token = _sign(
        {"sub": _USER, "type": "access", "organisation_id": _ORG, "aud": "some-other-audience"}
    )
    with pytest.raises(AuthError):
        await verify_token(token)


async def test_rejects_token_with_wrong_iss() -> None:
    """A token minted by an untrusted issuer is rejected (verify_iss)."""
    token = _sign({"sub": _USER, "type": "access", "organisation_id": _ORG, "iss": "evil-issuer"})
    with pytest.raises(AuthError):
        await verify_token(token)
