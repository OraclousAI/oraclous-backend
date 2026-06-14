"""Integration: runtime OAuth-token resolution + refresh (S3) vs real Postgres + a fake provider.

Proves: resolve a stored token, refresh a near-expiry token in place (re-encrypted), scope-shortfall
returns missing_scopes + a login_url, and an unknown provider returns TOKEN_NOT_FOUND. Internal-key
gated. Key-free (the provider refresh is a fake client; the stored token is created via the repo).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # noqa: S105 — 32-byte dev key
_INTERNAL = "rt-internal-key"  # noqa: S105
_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000000d5")
_HDR = {"X-Internal-Key": _INTERNAL}


class _FakeRefreshClient:
    def __init__(self) -> None:
        self.calls = 0

    async def refresh(self, *, provider: str, refresh_token: str) -> dict:
        self.calls += 1
        return {
            "access_token": "REFRESHED-token",
            "refresh_token": "new-refresh",
            "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }


@pytest.fixture
async def ctx(
    postgres_dsn: str, test_envelope, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, object]]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    for k, v in {
        "DATABASE_URL": async_dsn,
        "ENCRYPTION_KEY": _KEY,
        "INTERNAL_SERVICE_KEY": _INTERNAL,
    }.items():
        monkeypatch.setenv(k, v)
    from oraclous_credential_broker_service.core.config import get_settings

    get_settings.cache_clear()
    from oraclous_credential_broker_service.app.factory import create_app
    from oraclous_credential_broker_service.models import Base
    from oraclous_credential_broker_service.repositories.credential_repository import (
        CredentialRepository,
    )
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    repo = CredentialRepository(async_dsn, encrypt=test_envelope.encrypt)
    fake = _FakeRefreshClient()
    app = create_app(lifespan=None)
    app.state.credential_repository = repo
    app.state.envelope_service = test_envelope
    app.state.refresh_client = fake
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cb.test") as c:
        yield c, fake, repo
    await repo.close()
    await engine.dispose()
    get_settings.cache_clear()


async def _seed(repo, **overrides) -> None:
    """Create an OAuth credential directly via the repo (bypasses the user-auth CRUD path)."""
    from oraclous_credential_broker_service.schema.credential_schema import CreateCredential

    cred = {
        "access_token": "stored-token",
        "refresh_token": "stored-refresh",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    cred.update(overrides)
    await repo.create_credential(
        CreateCredential(
            tool_id=uuid.uuid4(),
            user_id=_USER,
            name="g",
            provider="google",
            cred_type="oauth",
            credential=cred,
        ),
        _ORG,
        _USER,
    )


def _rt(provider: str = "google", scopes: list[str] | None = None) -> dict:
    body = {"organisation_id": str(_ORG), "user_id": str(_USER), "provider": provider}
    if scopes is not None:
        body["required_scopes"] = scopes
    return body


async def test_resolves_a_valid_stored_token(ctx) -> None:
    client, fake, repo = ctx
    await _seed(repo)
    resp = await client.post("/internal/runtime-token", json=_rt(), headers=_HDR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] and body["access_token"] == "stored-token"  # noqa: S105 — test assertion, not a secret
    assert fake.calls == 0  # not near expiry → no refresh


async def test_refreshes_a_near_expiry_token_in_place(ctx) -> None:
    client, fake, repo = ctx
    await _seed(repo, expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    resp = await client.post("/internal/runtime-token", json=_rt(), headers=_HDR)
    assert resp.status_code == 200 and resp.json()["success"]
    assert resp.json()["access_token"] == "REFRESHED-token"  # noqa: S105 — the refreshed grant
    assert fake.calls == 1
    # a second call now finds the refreshed (far-future) token → no further refresh
    again = await client.post("/internal/runtime-token", json=_rt(), headers=_HDR)
    assert again.json()["access_token"] == "REFRESHED-token"  # noqa: S105 — refreshed grant
    assert fake.calls == 1


async def test_scope_shortfall_returns_missing_scopes(ctx) -> None:
    client, _, repo = ctx
    await _seed(repo)
    resp = await client.post(
        "/internal/runtime-token",
        json=_rt(scopes=["https://www.googleapis.com/auth/gmail.send"]),
        headers=_HDR,
    )
    body = resp.json()
    assert not body["success"]
    assert body["error_code"] == "oauth_insufficient_scopes"
    assert body["missing_scopes"] == ["https://www.googleapis.com/auth/gmail.send"]
    assert "/oauth/google/login" in body["login_url"]


async def test_unknown_provider_is_token_not_found(ctx) -> None:
    client, _, repo = ctx
    await _seed(repo)
    resp = await client.post("/internal/runtime-token", json=_rt(provider="notion"), headers=_HDR)
    assert resp.json()["error_code"] == "oauth_token_not_found"


async def test_ensure_data_source_access_uses_catalogue_scopes(ctx) -> None:
    client, _, repo = ctx
    await _seed(repo)  # stored token has drive.readonly
    resp = await client.post(
        "/internal/ensure-data-source-access",
        json={
            "organisation_id": str(_ORG),
            "user_id": str(_USER),
            "provider": "google",
            "data_source": "drive",
        },
        headers=_HDR,
    )
    assert resp.json()["success"]  # drive's required scope is satisfied


async def test_requires_internal_key(ctx) -> None:
    client, _, repo = ctx
    assert (await client.post("/internal/runtime-token", json=_rt())).status_code == 401


# --- G1: oauth-connect bridge (a connected provider grant → a resolvable broker credential) ---
def _connect(provider: str = "google", **token_overrides) -> dict:
    token = {
        "access_token": "connected-token",
        "refresh_token": "connected-refresh",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    token.update(token_overrides)
    return {
        "organisation_id": str(_ORG),
        "user_id": str(_USER),
        "provider": provider,
        "name": f"{provider} (connected)",
        "token": token,
    }


async def test_oauth_connect_lands_a_resolvable_credential(ctx) -> None:
    """G1 bridge: oauth-connect lands a resolver-findable credential (by org/user/provider)."""
    client, fake, _ = ctx
    connected = await client.post("/internal/oauth-connect", json=_connect(), headers=_HDR)
    assert connected.status_code == 200, connected.text
    assert "credential_id" in connected.json()
    resolved = await client.post("/internal/runtime-token", json=_rt(), headers=_HDR)
    assert resolved.status_code == 200 and resolved.json()["success"]
    assert resolved.json()["access_token"] == "connected-token"  # noqa: S105 — test assertion
    assert fake.calls == 0  # far-future expiry → no refresh


async def test_oauth_connect_rotates_in_place(ctx) -> None:
    """Re-connecting a provider upserts (rotates the token), never duplicates the credential."""
    client, _, _ = ctx
    first = await client.post("/internal/oauth-connect", json=_connect(), headers=_HDR)
    second = await client.post(
        "/internal/oauth-connect",
        json=_connect(access_token="rotated-token"),  # noqa: S106 — test token
        headers=_HDR,
    )
    assert first.json()["credential_id"] == second.json()["credential_id"]  # same row (upsert)
    resolved = await client.post("/internal/runtime-token", json=_rt(), headers=_HDR)
    assert resolved.json()["access_token"] == "rotated-token"  # noqa: S105 — rotated grant


async def test_oauth_connect_requires_internal_key(ctx) -> None:
    client, _, _ = ctx
    assert (await client.post("/internal/oauth-connect", json=_connect())).status_code == 401
