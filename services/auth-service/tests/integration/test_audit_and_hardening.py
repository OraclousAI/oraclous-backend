"""Integration: S6 hardening — audit log writes (real Postgres).

Proves register/login emit immutable audit rows (actor + org + event). Constant-time internal-key
rejection is covered in the service-account suite; CORS allowlist is unit-covered by the factory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_auth_service.app.factory import create_app
from oraclous_auth_service.models import Base
from oraclous_auth_service.models.audit_model import AuthAuditLog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


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
async def audit_ctx(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker]]:
    monkeypatch.setenv("JWT_SECRET", "audit-integration-secret")
    engine = create_async_engine(postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(agent_repository=_FakeAgentRepo(), internal_service_key="x")
    app.state.sessionmaker = maker
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://auth.test") as c:
        yield c, maker
    await engine.dispose()


async def test_register_and_login_write_audit_rows(audit_ctx) -> None:
    client, maker = audit_ctx
    cred = {"email": "audit@ex.com", "password": "GoodPass1"}
    assert (await client.post("/v1/auth/register", json=cred)).status_code == 201
    assert (await client.post("/v1/auth/login", json=cred)).status_code == 200

    async with maker() as s:
        rows = (
            (await s.execute(select(AuthAuditLog).order_by(AuthAuditLog.created_at)))
            .scalars()
            .all()
        )
    events = [row.event for row in rows]
    assert "user.register" in events and "user.login" in events
    # the audit rows carry the actor + org (not anonymous)
    for row in rows:
        assert row.actor_type == "user" and row.actor_id and row.organisation_id
