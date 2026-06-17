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
from oraclous_auth_service.core.jwt_handler import decode_token
from oraclous_auth_service.models import Base
from oraclous_auth_service.models.audit_model import AuthAuditLog
from oraclous_auth_service.models.oauth_model import OAuthAccount
from oraclous_auth_service.services.oauth_connect_sink import ConnectSinkError
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


async def test_providers_lists_only_configured(oauth_ctx) -> None:
    client, _ = oauth_ctx
    # only google has credentials in this fixture; github/notion are omitted (no secrets exposed).
    resp = await client.get("/oauth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"providers": ["google"]}


# --- G1: provider connect (authenticated) — lands a broker credential, mints no session ---
_DRIVE = "https://www.googleapis.com/auth/drive.readonly"


class _FakeConnectSink:
    """Fake ConnectSink — captures the connect call, returns a deterministic credential id. Set
    ``raises`` to make the next call raise (to exercise the broker-failure → 502 mapping)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.raises: Exception | None = None

    async def oauth_connect(self, *, organisation_id, user_id, provider, name, token) -> str:
        if self.raises is not None:
            raise self.raises
        self.calls.append(
            {
                "organisation_id": organisation_id,
                "user_id": user_id,
                "provider": provider,
                "name": name,
                "token": token,
            }
        )
        return f"cred-{provider}"


@pytest.fixture
async def connect_ctx(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, _FakeConnectSink, async_sessionmaker]]:
    monkeypatch.setenv("JWT_SECRET", "oauth-connect-secret")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_SECRET", "google-client-secret")
    engine = create_async_engine(postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    sink = _FakeConnectSink()
    app = create_app(agent_repository=_FakeAgentRepo(), internal_service_key="x")
    app.state.sessionmaker = maker
    app.state.oauth_provider_client = _FakeProviderClient()
    app.state.oauth_connect_sink = sink
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://auth.test") as c:
        yield c, sink, maker
    await engine.dispose()


async def _login_bearer(client: AsyncClient) -> str:
    """Run the login flow to get an authenticated user bearer for the connect routes."""
    login = await client.get("/oauth/google/login", params={"redirect_uri": "https://app/cb"})
    state = parse_qs(urlparse(login.json()["authorize_url"]).query)["state"][0]
    cb = await client.get("/oauth/google/callback", params={"code": "c", "state": state})
    return cb.json()["access_token"]


async def test_connect_lands_a_broker_credential_for_the_caller(connect_ctx) -> None:
    client, sink, maker = connect_ctx
    bearer = await _login_bearer(client)
    # the principal the route MUST forward is exactly the bearer's claims, never the request body
    claims = decode_token(bearer)
    known_user, known_org = claims["sub"], claims["organisation_id"]
    hdr = {"Authorization": f"Bearer {bearer}"}
    # begin → authorize URL carries the requested tool scopes + a single-use state
    begin = await client.post(
        "/oauth/google/connect",
        json={"redirect_uri": "https://app/connect/cb", "scopes": [_DRIVE]},
        headers=hdr,
    )
    assert begin.status_code == 200, begin.text
    q = parse_qs(urlparse(begin.json()["authorize_url"]).query)
    assert q["scope"] == [_DRIVE]  # the requested tool scope, not the default login scopes
    state = q["state"][0]
    # complete → lands the token via the sink, returns the credential id
    done = await client.post(
        "/oauth/google/connect/complete",
        json={"code": "auth-code", "state": state},
        headers=hdr,
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["provider"] == "google" and body["credential_id"] == "cred-google"
    # the sink got the AUTHENTICATED principal — pinned to the bearer (not the body or state row)
    assert len(sink.calls) == 1
    call = sink.calls[0]
    assert call["user_id"] == known_user and call["organisation_id"] == known_org
    assert call["provider"] == "google" and call["name"] == "google (connected)"
    # the full token dict the broker's resolver depends on is forwarded intact
    tok = call["token"]
    assert tok["access_token"] == "fake-access-token"  # noqa: S105 — fake provider token
    assert tok["refresh_token"] == "fake-refresh-token"  # noqa: S105 — fake provider token
    assert tok["scopes"] == ["email", "profile"]
    assert isinstance(tok["expires_at"], str) and tok["expires_at"]  # present ISO-8601 expiry
    # the connect emits an immutable audit row tied to the bearer's actor + org (§3.7-adjacent)
    async with maker() as s:
        rows = (
            (await s.execute(select(AuthAuditLog).where(AuthAuditLog.event == "oauth.connect")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].actor_id == known_user and rows[0].organisation_id == known_org
    assert rows[0].target == "google"


async def test_connect_requires_auth(connect_ctx) -> None:
    client, _, _ = connect_ctx
    # both begin and complete are authenticated — no bearer → 401, no broker call
    begin = await client.post(
        "/oauth/google/connect", json={"redirect_uri": "https://app/cb", "scopes": []}
    )
    assert begin.status_code == 401
    complete = await client.post("/oauth/google/connect/complete", json={"code": "c", "state": "s"})
    assert complete.status_code == 401


async def test_connect_maps_broker_failure_to_502(connect_ctx) -> None:
    client, sink, _ = connect_ctx
    sink.raises = ConnectSinkError("credential broker unavailable")  # broker down / rejecting
    hdr = {"Authorization": f"Bearer {await _login_bearer(client)}"}
    begin = await client.post(
        "/oauth/google/connect", json={"redirect_uri": "https://app/cb", "scopes": []}, headers=hdr
    )
    state = parse_qs(urlparse(begin.json()["authorize_url"]).query)["state"][0]
    done = await client.post(
        "/oauth/google/connect/complete", json={"code": "c", "state": state}, headers=hdr
    )
    # a downstream broker failure is a deliberate 502, never a leaked 500 (no broker detail)
    assert done.status_code == 502, done.text


# --- WP-11: redirect_uri allow-list end-to-end (T-OAUTH open-redirect) -------
_ALLOWED_REDIRECT = "https://app.example/oauth/callback"


@pytest.fixture
async def allowlisted_ctx(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """An OAuth app with a CONFIGURED per-provider redirect allow-list (opts the dev flow into
    enforcement via ``OAUTH_GOOGLE_REDIRECT_URIS``) so the reject/pass behaviour is exercised
    end-to-end through the real route + service + provider config."""
    monkeypatch.setenv("JWT_SECRET", "oauth-redirect-secret")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_SECRET", "google-client-secret")
    monkeypatch.setenv("OAUTH_GOOGLE_REDIRECT_URIS", _ALLOWED_REDIRECT)
    engine = create_async_engine(postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(agent_repository=_FakeAgentRepo(), internal_service_key="x")
    app.state.sessionmaker = maker
    app.state.oauth_provider_client = _FakeProviderClient()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://auth.test") as c:
        yield c
    await engine.dispose()


@pytest.mark.security
async def test_login_rejects_non_allowlisted_redirect_uri(allowlisted_ctx) -> None:
    """A client-supplied ``redirect_uri`` not on the configured allow-list is rejected with a
    generic 400 (no oracle revealing the allowed set), and the PKCE handshake never begins."""
    client = allowlisted_ctx
    resp = await client.get(
        "/oauth/google/login", params={"redirect_uri": "https://evil.example/steal"}
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.security
async def test_login_allows_allowlisted_redirect_uri(allowlisted_ctx) -> None:
    """An allowlisted ``redirect_uri`` passes: the authorize URL is built and carries that exact
    redirect (the dev/local OAuth flow keeps working when its callback is declared)."""
    client = allowlisted_ctx
    resp = await client.get("/oauth/google/login", params={"redirect_uri": _ALLOWED_REDIRECT})
    assert resp.status_code == 200, resp.text
    q = parse_qs(urlparse(resp.json()["authorize_url"]).query)
    assert q["redirect_uri"] == [_ALLOWED_REDIRECT]


@pytest.mark.security
async def test_connect_begin_rejects_non_allowlisted_redirect_uri(allowlisted_ctx) -> None:
    """The authenticated connect-begin enforces the same allow-list as login (both take a
    client-supplied redirect_uri). A non-allowlisted redirect is a generic 400."""
    client = allowlisted_ctx
    # authenticate via the (allowlisted) login flow first
    login = await client.get("/oauth/google/login", params={"redirect_uri": _ALLOWED_REDIRECT})
    state = parse_qs(urlparse(login.json()["authorize_url"]).query)["state"][0]
    cb = await client.get("/oauth/google/callback", params={"code": "c", "state": state})
    hdr = {"Authorization": f"Bearer {cb.json()['access_token']}"}
    resp = await client.post(
        "/oauth/google/connect",
        json={"redirect_uri": "https://evil.example/steal", "scopes": []},
        headers=hdr,
    )
    assert resp.status_code == 400, resp.text
