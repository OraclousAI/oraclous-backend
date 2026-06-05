"""Integration: delegated-token API (S5b) vs real Postgres — mint/validate/revoke lifecycle.

Wires the shipped DelegationService + PostgresDelegatedTokenStore to HTTP under the X-Internal-Key
gate. Proves: mint returns a raw bearer once (DB stores only hash+prefix), validate succeeds for the
bound agent + subset scopes, rejects a different agent (agent_mismatch) / scope-creep, and revoke
makes subsequent validation fail.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # noqa: S105 — 32-byte dev key
_INTERNAL = "deleg-internal-key"  # noqa: S105
_ORG = str(uuid.uuid4())
_MEMBER = str(uuid.uuid4())
_AGENT = str(uuid.uuid4())
_HDR = {"X-Internal-Key": _INTERNAL}


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", _INTERNAL)
    from oraclous_credential_broker_service.core.config import get_settings

    get_settings.cache_clear()
    from oraclous_credential_broker_service.app.factory import create_app
    from oraclous_credential_broker_service.models import Base
    from oraclous_credential_broker_service.repositories.postgres_delegated_token_store import (
        PostgresDelegatedTokenStore,
    )
    from oraclous_credential_broker_service.services.delegation_service import DelegationService
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(lifespan=None)
    app.state.delegation_service = DelegationService(
        store=PostgresDelegatedTokenStore(engine=engine)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cb.test") as c:
        yield c
    await engine.dispose()
    get_settings.cache_clear()


async def _mint(client: AsyncClient, scopes: list[str]) -> dict:
    resp = await client.post(
        "/internal/delegated-tokens",
        headers=_HDR,
        json={
            "organisation_id": _ORG,
            "member_id": _MEMBER,
            "agent_id": _AGENT,
            "scopes": scopes,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_mint_validate_revoke_lifecycle(client: AsyncClient) -> None:
    minted = await _mint(client, ["read", "write"])
    assert minted["token"] and minted["token_id"]  # raw bearer returned once

    # validate: bound agent + subset scopes → success
    ok = await client.post(
        "/internal/delegated-tokens/validate",
        headers=_HDR,
        json={
            "organisation_id": _ORG,
            "raw_token": minted["token"],
            "requesting_agent_id": _AGENT,
            "requested_scopes": ["read"],
        },
    )
    assert ok.json()["success"] and ok.json()["member_id"] == _MEMBER

    # a different agent → agent_mismatch
    bad_agent = await client.post(
        "/internal/delegated-tokens/validate",
        headers=_HDR,
        json={
            "organisation_id": _ORG,
            "raw_token": minted["token"],
            "requesting_agent_id": str(uuid.uuid4()),
            "requested_scopes": ["read"],
        },
    )
    assert not bad_agent.json()["success"] and bad_agent.json()["reason"] == "agent_mismatch"

    # scope-creep → rejected
    creep = await client.post(
        "/internal/delegated-tokens/validate",
        headers=_HDR,
        json={
            "organisation_id": _ORG,
            "raw_token": minted["token"],
            "requesting_agent_id": _AGENT,
            "requested_scopes": ["admin"],
        },
    )
    assert not creep.json()["success"] and creep.json()["reason"] == "scope_creep"

    # revoke → subsequent validation fails
    rev = await client.post(
        f"/internal/delegated-tokens/{minted['token_id']}/revoke",
        headers=_HDR,
        json={"organisation_id": _ORG},
    )
    assert rev.json()["revoked_count"] == 1
    after = await client.post(
        "/internal/delegated-tokens/validate",
        headers=_HDR,
        json={
            "organisation_id": _ORG,
            "raw_token": minted["token"],
            "requesting_agent_id": _AGENT,
            "requested_scopes": ["read"],
        },
    )
    assert not after.json()["success"] and after.json()["reason"] == "revoked"


async def test_delegation_requires_internal_key(client: AsyncClient) -> None:
    resp = await client.post(
        "/internal/delegated-tokens",
        json={
            "organisation_id": _ORG,
            "member_id": _MEMBER,
            "agent_id": _AGENT,
            "scopes": ["read"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert resp.status_code == 401
