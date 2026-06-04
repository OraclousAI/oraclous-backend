"""Integration: the OAuth login flow against a FAKE provider + real Postgres (S5, key-free).

login → authorize URL (PKCE challenge + state) → callback → user upsert + app JWT (in body) +
provider tokens stored ENCRYPTED at rest; single-use state (replay → 400); unknown provider → 503.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_auth_service.app.factory import create_app
from oraclous_auth_service.core.encryption import decrypt
from oraclous_auth_service.models import Base
from oraclous_auth_service.models.oauth_model import OAuthAccount
from oraclous_auth_service.services.oauth_service import ProfileInfo, TokenSet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


class _FakeProviderClient:
    """A fake ProviderClient — no real HTTP, no keys."""

    async def exchange_code(self, provider, *, code, code_verifier, redirect_uri) -> TokenSet:
        assert code and code_verifier and redirect_uri  # the flow wired the PKCE verifier through
        return TokenSet(
            access_token="fake-access-token",  # noqa: S106 — fake provider token, not a secret
            refresh_token="fake-refresh-token",  # noqa: S106 — fake provider token, not a secret
            scopes=["email", "profile"],
            expires_in=3600,
        )

    async def fetch_userinfo(self, provider, token) -> ProfileInfo:
        return ProfileInfo(external_id="ext-1", email="oauthuser@ex.com", name="OAuth User")


class _FakeAgentRepo:
    async def create_agent(self, **_):  # pragma: no cover
        return "", object()

    async def validate_credential(self, _):  # pragma: no cover
        return None

    async def revoke_agent(self, _):  # pragma: no cover
        return 0

    async def organisation_id_for(self, _):  # pragma: no cover
        return None

    async def principal_type_for(self, _):  # pragma: no cover
        return None


@pytest.fixture
async def oauth_ctx(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker]]:
    monkeypatch.setenv("JWT_SECRET", "oauth-integration-secret")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_SECRET", "google-client-secret")
    engine = create_async_engine(postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(agent_repository=_FakeAgentRepo(), internal_service_key="x")
    app.state.sessionmaker = maker
    app.state.oauth_provider_client = _FakeProviderClient()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://auth.test") as c:
        yield c, maker
    await engine.dispose()


async def _begin(client: AsyncClient) -> str:
    login = await client.get(
        "/oauth/google/login", params={"redirect_uri": "https://app.example/cb"}
    )
    assert login.status_code == 200, login.text
    q = parse_qs(urlparse(login.json()["authorize_url"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0]
    assert q["client_id"] == ["google-client-id"]
    return q["state"][0]


async def test_full_login_flow_creates_user_and_app_tokens(oauth_ctx) -> None:
    client, maker = oauth_ctx
    state = await _begin(client)
    cb = await client.get("/oauth/google/callback", params={"code": "auth-code", "state": state})
    assert cb.status_code == 200, cb.text
    body = cb.json()
    assert body["access_token"] and body["refresh_token"]  # app JWT in the BODY, not the URL
    assert body["email"] == "oauthuser@ex.com"

    # the OAuth-created user can use the app token
    me = await client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200 and me.json()["organisation_id"]

    # provider tokens are stored ENCRYPTED at rest (T-OAUTH-PLAINTEXT)
    async with maker() as s:
        acct = (await s.execute(select(OAuthAccount))).scalars().first()
    assert acct is not None
    assert acct.access_token_enc != "fake-access-token"  # noqa: S105 — asserting it is encrypted
    assert decrypt(acct.access_token_enc) == "fake-access-token"
    assert decrypt(acct.refresh_token_enc) == "fake-refresh-token"


async def test_state_is_single_use(oauth_ctx) -> None:
    client, _ = oauth_ctx
    state = await _begin(client)
    first = await client.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert first.status_code == 200
    replay = await client.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert replay.status_code == 400  # consumed state cannot be replayed


async def test_invalid_state_rejected(oauth_ctx) -> None:
    client, _ = oauth_ctx
    bad = await client.get(
        "/oauth/google/callback", params={"code": "c", "state": "not-a-real-state"}
    )
    assert bad.status_code == 400


async def test_unconfigured_provider_is_503(oauth_ctx) -> None:
    client, _ = oauth_ctx
    # github has no client credentials in env -> 503, service still up
    resp = await client.get("/oauth/github/login", params={"redirect_uri": "https://app/cb"})
    assert resp.status_code == 503
