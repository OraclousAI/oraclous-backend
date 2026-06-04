"""KGS jwt-mode `verify_token` — the consumer side of the cross-service JWT/Principal Contract.

Proves KGS accepts an auth-service-shaped access token (sub=UUID, type=access, organisation_id) and
fail-closed rejects refresh tokens, legacy email-subject tokens, org-less tokens, and tokens signed
with a different secret. No DB / no network — signs tokens with the shared secret directly.
"""

from __future__ import annotations

import pytest
from jose import jwt
from oraclous_governance import PrincipalType
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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _sign(claims: dict, *, secret: str = _SECRET) -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


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
