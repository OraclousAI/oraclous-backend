"""Integration: the full user-identity flow against real Postgres (testcontainers, ORA-12 harness).

Drives the live FastAPI app (no mocks below the route) over httpx ASGITransport against a real
Postgres: register → login → refresh-rotation + reuse-detection → `/me` revocation re-check. Proves
the §22 "real endpoints vs real substrate" gate for Slice 1.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_auth_service.app.factory import create_app
from oraclous_auth_service.models import Base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


class _FakeAgentRepo:
    """create_app needs an agent repo; the user routes never touch it (all calls are inert)."""

    async def create_agent(self, **_: object) -> tuple[str, object]:  # pragma: no cover
        return "", object()

    async def validate_credential(self, _: str) -> str | None:  # pragma: no cover
        return None

    async def revoke_agent(self, _: str) -> int:  # pragma: no cover
        return 0

    async def organisation_id_for(self, _: str) -> str | None:  # pragma: no cover
        return None


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("JWT_SECRET", "integration-test-secret")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    engine = create_async_engine(postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(agent_repository=_FakeAgentRepo(), internal_service_key="x")
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://auth.test") as c:
        yield c
    await engine.dispose()


async def test_register_login_refresh_me_flow(client: AsyncClient) -> None:
    # register (email is normalised to lowercase)
    r = await client.post(
        "/v1/auth/register", json={"email": "Alice@Ex.com", "password": "GoodPass1"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "alice@ex.com"
    access, refresh = body["access_token"], body["refresh_token"]
    assert access and refresh

    # duplicate registration → 409
    dup = await client.post(
        "/v1/auth/register", json={"email": "alice@ex.com", "password": "GoodPass1"}
    )
    assert dup.status_code == 409

    # /me with the access token carries a real organisation_id
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["principal_type"] == "user" and me.json()["organisation_id"]

    # login works; bad password and unknown email are the SAME generic 401 (no enumeration)
    assert (
        await client.post(
            "/v1/auth/login", json={"email": "alice@ex.com", "password": "GoodPass1"}
        )
    ).status_code == 200
    assert (
        await client.post(
            "/v1/auth/login", json={"email": "alice@ex.com", "password": "WrongPass1"}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/v1/auth/login", json={"email": "nobody@ex.com", "password": "GoodPass1"}
        )
    ).status_code == 401

    # refresh rotates to a NEW refresh token
    rotated = await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    assert rotated.status_code == 200
    new_refresh = rotated.json()["refresh_token"]
    assert new_refresh != refresh

    # reusing the OLD (rotated) refresh is detected → 401 AND kills the whole family
    assert (
        await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    ).status_code == 401
    # so even the freshly-rotated token is now revoked
    assert (
        await client.post("/v1/auth/refresh", json={"refresh_token": new_refresh})
    ).status_code == 401


async def test_me_rejects_missing_and_refresh_tokens(client: AsyncClient) -> None:
    assert (await client.get("/v1/auth/me")).status_code == 401
    reg = {"email": "bob@ex.com", "password": "GoodPass1"}
    r = await client.post("/v1/auth/register", json=reg)
    refresh = r.json()["refresh_token"]
    # a refresh token must not authorise a user route (type != access)
    bad = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert bad.status_code == 401


async def test_weak_password_rejected_at_register(client: AsyncClient) -> None:
    r = await client.post("/v1/auth/register", json={"email": "weak@ex.com", "password": "weak"})
    assert r.status_code == 422
