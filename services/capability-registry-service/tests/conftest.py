"""capability-registry local test conftest.

Provides the ``postgres_dsn`` fixture (a session-scoped ephemeral Postgres testcontainer) for this
service's integration suite, mirroring the root harness so the suite runs in isolation.

ADR-030: ``capreg_dsns`` derives a NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` DSN (asyncpg) from the
superuser container so the RLS isolation suite exercises the **real runtime role** — proving the
GRANTs are complete and the RLS policy actually bites (a superuser would bypass it). Schema DDL +
RLS enablement run as the superuser owner (3 strict tables via ``enable_rls_on``;
``capability_descriptors`` with ``extra_read_org_id=PLATFORM_ORG_ID`` for the widened-read policy);
the app role only gets SELECT/INSERT/UPDATE/DELETE.
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

# The org that owns the built-in/platform tool catalogue — must equal config.PLATFORM_ORG_ID and the
# migration's literal so the test's widened policy matches the deployed one.
PLATFORM_ORG_ID = "00000000-0000-0000-0000-0000000000a0"

# The capability-registry's org-scoped tables. The three strict ones get the plain policy; the
# widened-read table is RLS-enabled with the platform-org read-widening (matching 0006_enable_rls).
STRICT_RLS_TABLES = ("tool_instances", "executions", "harness_graph_binding", "delivery_state")
WIDENED_READ_TABLE = "capability_descriptors"


MYSQL_IMAGE = "mysql:8.0"


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
    """Create the NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` role + DML GRANTs on the capreg tables.

    Idempotent (the session container is shared). Mirrors ``deploy/postgres-init`` +
    ``core.bootstrap_rls_role`` so the integration suite runs against the same role shape the
    deployed runtime uses. The schema must already exist (the caller applies it as superuser first).
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
async def capreg_dsns(postgres_dsn: str):  # noqa: ANN201
    """Set up the capreg schema + RLS + the oraclous_app role on the shared container, and yield the
    (owner, app) **asyncpg** DSNs (ADR-030).

    Schema DDL, RLS enablement (the three strict tables via ``enable_rls_on``;
    ``capability_descriptors`` with the platform-org read-widening), and the role/GRANT provisioning
    all run as the SUPERUSER owner; the returned app DSN is the NOSUPERUSER runtime role the repos
    use so RLS actually bites. ``drop_all`` first keeps each test isolated.
    """
    import psycopg
    from oraclous_capability_registry_service.models import Base
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
        for table in STRICT_RLS_TABLES:
            pg_schema.enable_rls_on(raw, table)
        # widened read: reads admit caller org OR platform org; writes stay strict (matches 0006).
        pg_schema.enable_rls_on(raw, WIDENED_READ_TABLE, extra_read_org_id=PLATFORM_ORG_ID)
    _provision_app_role(postgres_dsn)
    yield owner_async, app_async


@pytest.fixture(scope="session")
def mysql_dsn() -> Iterator[str]:
    """A ``mysql://`` DSN for an ephemeral MySQL container (for the MySQL connector test)."""
    from testcontainers.mysql import MySqlContainer

    container = MySqlContainer(MYSQL_IMAGE, username=PG_USER, password=PG_PASSWORD, dbname=PG_DB)
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(3306)
        yield f"mysql://{PG_USER}:{PG_PASSWORD}@{host}:{port}/{PG_DB}"
