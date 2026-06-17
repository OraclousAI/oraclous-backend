"""Test fixtures for oraclous-knowledge-graph-service (ORA-18 / A3).

Local fixtures only; the cross-service substrate harness (real Neo4j /
Postgres / Redis) is in ``tests/conftest.py`` at the repo root.

ADR-030 (#353): ``kgs_dsns`` derives a NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` DSN (asyncpg) from
the superuser container so the RLS-backstop isolation test exercises the **real runtime role** —
proving the GRANTs are complete and the RLS policy actually bites (a superuser would bypass it).
Schema DDL + RLS enablement run as the superuser owner; the app role only gets
SELECT/INSERT/UPDATE/DELETE. Mirrors the credential-broker service conftest.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import pytest

POSTGRES_IMAGE = "postgres:16"
PG_USER = "oraclous"
PG_PASSWORD = "oraclous"  # noqa: S105 — ephemeral test container, not a real secret
PG_DB = "oraclous"

# the RLS runtime role (ADR-030 §3) — matches deploy/postgres-init + bootstrap_rls_role.
APP_ROLE = "oraclous_app"
APP_PASSWORD = "app"  # noqa: S105 — ephemeral test-container role, not a real secret

# The KGS's four org-scoped tables (RLS enabled on each — 0007_enable_rls).
RLS_TABLES = ("knowledge_graphs", "ingestion_jobs", "recipes", "entity_resolutions")


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """A libpq DSN for an ephemeral Postgres container (the SUPERUSER owner)."""
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        POSTGRES_IMAGE, username=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        yield f"postgresql://{PG_USER}:{PG_PASSWORD}@{host}:{port}/{PG_DB}"


def _provision_app_role(superuser_libpq_dsn: str) -> None:
    """Create the NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` role + DML GRANTs on the KGS tables.

    Idempotent (the session container is shared). Mirrors ``deploy/postgres-init`` +
    ``core.bootstrap_rls_role`` so the test runs against the same role shape the deployed runtime
    uses. The schema must already exist (the caller applies it as superuser first); GRANTs reference
    the org-scoped tables.
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


def _to_app_userinfo(dsn: str) -> str:
    """Rewrite a DSN's userinfo to the ``oraclous_app`` role (scheme/host/db unchanged)."""
    parts = urlsplit(dsn)
    netloc = f"{APP_ROLE}:{APP_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture
async def kgs_dsns(postgres_dsn: str):  # noqa: ANN201
    """Set up the KGS schema + RLS + the oraclous_app role on the shared container, and yield the
    (owner, app) **asyncpg** DSNs (ADR-030).

    Schema DDL, RLS enablement (``enable_rls_on`` over the four org-scoped tables), and the
    role/GRANT provisioning all run as the SUPERUSER owner; the returned app DSN is the NOSUPERUSER
    runtime role the runtime engines use so RLS actually bites. ``drop_all`` first keeps each test
    isolated.
    """
    import psycopg
    from oraclous_knowledge_graph_service.repositories.models import Base
    from oraclous_substrate.schema import postgres as pg_schema
    from sqlalchemy.ext.asyncio import create_async_engine

    owner_async = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    app_async = _to_app_userinfo(owner_async)

    # schema via SQLAlchemy (asyncpg); RLS DDL via a sync psycopg connection — enable_rls_on speaks
    # the sync DB-API cursor protocol (the same path the Alembic migration uses), not asyncpg.
    setup_engine = create_async_engine(owner_async)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()
    with psycopg.connect(postgres_dsn, autocommit=True) as raw:
        for table in RLS_TABLES:
            pg_schema.enable_rls_on(raw, table)
    _provision_app_role(postgres_dsn)
    yield owner_async, app_async
