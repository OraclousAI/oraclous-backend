"""Shared integration fixtures: a live auth-service app over a real Postgres (testcontainers).

Builds the FastAPI app with a real async sessionmaker against the ephemeral Postgres (`postgres_dsn`
from the suite conftest), creating all tables via ``Base.metadata.create_all``. No mocks below the
route — exercises real SQL.

ADR-030 Slice 1: the ``client`` identity sessionmaker runs as the NOSUPERUSER/NOBYPASSRLS
``oraclous_app`` role (the deployed runtime role for the identity engine) — proving the no-bound-org
identity flows (register / login / refresh / member / invitation / oauth / audit) still work under
the RLS runtime role and never fail-close. Schema DDL + RLS enablement run as the SUPERUSER owner
(``postgres_dsn``); the app role only gets SELECT/INSERT/UPDATE/DELETE. The identity tables are not
RLS-enabled (they are reached before an org is bound — see rls_coverage exclusions), so the role
binds the empty GUC harmlessly; what this proves is that the role + grants are complete for login.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_auth_service.app.factory import create_app
from oraclous_auth_service.models import Base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# the RLS runtime role (ADR-030 §3) — matches deploy/postgres-init + core.bootstrap_rls_role.
APP_ROLE = "oraclous_app"
APP_PASSWORD = "app"  # noqa: S105 — ephemeral test-container role, not a real secret


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


def _provision_app_role(superuser_libpq_dsn: str) -> None:
    """Create the NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` role + DML GRANTs on all public tables.

    Idempotent (the session container is shared). Mirrors ``deploy/postgres-init`` +
    ``core.bootstrap_rls_role`` so the integration suite runs against the same role shape the
    deployed identity engine uses. The schema must already exist (the caller applies it as superuser
    first); the broad grant covers the non-RLS identity tables the login flows read.
    """
    import psycopg

    with psycopg.connect(superuser_libpq_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN "
            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}' "
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; END IF; END $$;"
        )
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        cur.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
        )


def _to_app_userinfo(async_dsn: str) -> str:
    """Rewrite an asyncpg DSN's userinfo to the ``oraclous_app`` role (scheme/host/db unchanged)."""
    parts = urlsplit(async_dsn)
    netloc = f"{APP_ROLE}:{APP_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("JWT_SECRET", "integration-test-secret")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    owner_async = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    app_async = _to_app_userinfo(owner_async)

    # Per-test isolation: the Postgres container is session-scoped, so drop + recreate gives each
    # test a clean schema (otherwise reused emails/slugs collide across tests). Schema DDL runs as
    # the SUPERUSER owner; then provision the oraclous_app role + grants so the identity engine
    # below can connect as it (ADR-030 §3 — no-bound-org login flows run under the runtime role).
    owner_engine = create_async_engine(owner_async)
    async with owner_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await owner_engine.dispose()
    _provision_app_role(postgres_dsn)

    app = create_app(agent_repository=_FakeAgentRepo(), internal_service_key="x")
    # The identity sessionmaker runs as oraclous_app — the deployed identity-engine role.
    app_engine = create_async_engine(app_async)
    app.state.sessionmaker = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://auth.test") as c:
        yield c
    await app_engine.dispose()
