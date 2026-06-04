"""Integration: service-account machine principals via the real credential store (S4).

The machine-principal path (R1 agent infra) now mints either ``agent`` (default) or
``service_account`` tokens via a principal_type discriminator. Proves, against real Postgres:
create SA credential (internal-key gated) → exchange → SA JWT (principal_type=service_account) →
/me reports it → revoke → /me 401. Agent default behaviour is unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from oraclous_auth_service.app.factory import create_app
from oraclous_auth_service.models import Base
from oraclous_auth_service.repositories.agent_repository import AgentRepository
from oraclous_auth_service.repositories.postgres_credential_store import PostgresCredentialStore
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

_SECRET = "sa-integration-secret"  # noqa: S105 — test signing key
_INTERNAL = "sa-internal-key"  # noqa: S105 — test internal key
_ORG = "00000000-0000-0000-0000-0000000005a4"
_INTERNAL_HDR = {"X-Internal-Key": _INTERNAL}


@pytest.fixture
async def sa_client(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("JWT_SECRET", _SECRET)
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    repo = AgentRepository(PostgresCredentialStore(async_dsn))
    app = create_app(agent_repository=repo, internal_service_key=_INTERNAL)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://auth.test") as c:
        yield c
    await engine.dispose()


def _claims(token: str) -> dict:
    return jwt.decode(token, _SECRET, algorithms=["HS256"])


async def test_service_account_token_lifecycle(sa_client: AsyncClient) -> None:
    created = await sa_client.post(
        "/internal/agent-credentials",
        headers=_INTERNAL_HDR,
        json={
            "organisation_id": _ORG,
            "created_by_user_id": "u-1",
            "principal_type": "service_account",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["principal_type"] == "service_account"
    raw = created.json()["credential"]
    sa_id = created.json()["agent_id"]

    exch = await sa_client.post("/agent-token", json={"credential": raw})
    assert exch.status_code == 200, exch.text
    assert exch.json()["principal_type"] == "service_account"
    claims = _claims(exch.json()["access_token"])
    assert claims["principal_type"] == "service_account"
    assert claims["type"] == "access" and claims["organisation_id"] == _ORG

    token = exch.json()["access_token"]
    me = await sa_client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["principal_type"] == "service_account"

    # revoke -> /me fails closed (T2) even with the still-unexpired token
    assert (
        await sa_client.delete(f"/internal/agent-credentials/{sa_id}", headers=_INTERNAL_HDR)
    ).json()["revoked_count"] == 1
    assert (
        await sa_client.get("/me", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 401


async def test_agent_default_unchanged(sa_client: AsyncClient) -> None:
    created = await sa_client.post(
        "/internal/agent-credentials",
        headers=_INTERNAL_HDR,
        json={"organisation_id": _ORG, "created_by_user_id": "u-2"},
    )
    assert created.json()["principal_type"] == "agent"
    raw = created.json()["credential"]
    exch = await sa_client.post("/agent-token", json={"credential": raw})
    assert _claims(exch.json()["access_token"])["principal_type"] == "agent"


async def test_internal_key_required(sa_client: AsyncClient) -> None:
    # missing / wrong internal key -> 401 (no SA key minted)
    assert (
        await sa_client.post(
            "/internal/agent-credentials",
            json={"organisation_id": _ORG, "created_by_user_id": "u-3"},
        )
    ).status_code == 401
    assert (
        await sa_client.post(
            "/internal/agent-credentials",
            headers={"X-Internal-Key": "sa-internal-keyX"},
            json={"organisation_id": _ORG, "created_by_user_id": "u-3"},
        )
    ).status_code == 401
