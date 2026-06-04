"""Shared integration fixtures: a live auth-service app over a real Postgres (testcontainers).

Builds the FastAPI app with a real async sessionmaker against the ephemeral Postgres (`postgres_dsn`
from the suite conftest), creating all tables via ``Base.metadata.create_all``. No mocks below the
route — exercises real SQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_auth_service.app.factory import create_app
from oraclous_auth_service.models import Base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class _FakeAgentRepo:
    """create_app needs an agent repo; the user/org routes never touch it (all calls are inert)."""

    async def create_agent(self, **_: object) -> tuple[str, object]:  # pragma: no cover
        return "", object()

    async def validate_credential(self, _: str) -> str | None:  # pragma: no cover
        return None

    async def revoke_agent(self, _: str) -> int:  # pragma: no cover
        return 0

    async def organisation_id_for(self, _: str) -> str | None:  # pragma: no cover
        return None

    async def principal_type_for(self, _: str) -> str | None:  # pragma: no cover
        return None


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("JWT_SECRET", "integration-test-secret")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    engine = create_async_engine(postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    # Per-test isolation: the Postgres container is session-scoped, so drop + recreate gives each
    # test a clean schema (otherwise reused emails/slugs collide across tests).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(agent_repository=_FakeAgentRepo(), internal_service_key="x")
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://auth.test") as c:
        yield c
    await engine.dispose()
