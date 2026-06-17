"""Credential-broker local test conftest.

Provides the ``postgres_dsn`` fixture (a session-scoped ephemeral Postgres testcontainer) for this
service's integration suite, mirroring the root harness so the suite runs in isolation.

ADR-030: ``app_async_dsn`` derives a NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` DSN (asyncpg) from the
superuser container so the integration suite exercises the **real runtime role** — proving the
GRANTs are complete and the RLS policy actually bites (a superuser would bypass it). Schema DDL +
RLS enablement run as the superuser owner; the app role only gets SELECT/INSERT/UPDATE/DELETE.
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

# The credential-broker's four org-scoped tables (RLS enabled on each — 0004_enable_rls).
RLS_TABLES = ("user_credentials", "webhook_secrets", "delegated_tokens", "org_data_keys")


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
    """Create the NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` role + DML GRANTs on the broker tables.

    Idempotent (the session container is shared). Mirrors ``deploy/postgres-init`` +
    ``core.bootstrap_rls_role`` so the integration suite runs against the same role shape the
    deployed runtime uses. The schema must already exist (the caller applies it as superuser first);
    GRANTs reference the org-scoped tables.
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
async def broker_dsns(postgres_dsn: str):  # noqa: ANN201
    """Set up the broker schema + RLS + the oraclous_app role on the shared container, and yield
    the (owner, app) **asyncpg** DSNs (ADR-030).

    Schema DDL, RLS enablement (``enable_rls_on`` over the four org-scoped tables), and the
    role/GRANT provisioning all run as the SUPERUSER owner; the returned app DSN is the NOSUPERUSER
    runtime role the repos use so RLS actually bites. ``drop_all`` first keeps each test isolated.
    """
    import psycopg
    from oraclous_credential_broker_service.models import Base
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


def make_test_envelope():  # noqa: ANN201
    """A fully-working envelope for tests (ADR-020): a LocalKmsProvider with a fresh KEK + an
    in-memory DEK store (no DB needed). encrypt → v2, decrypt → polymorphic (v2 via the DEK, v1 via
    the legacy single key). Used wherever a service constructor now needs ``envelope=``."""
    import base64
    import os
    from types import SimpleNamespace

    from oraclous_credential_broker_service.core.envelope import LocalKmsProvider
    from oraclous_credential_broker_service.core.security import decrypt_secret
    from oraclous_credential_broker_service.services.envelope_service import EnvelopeService

    class _MemDekRepo:
        def __init__(self) -> None:
            self._rows: dict = {}

        async def get_for_org(self, *, organisation_id):  # noqa: ANN001, ANN202
            return self._rows.get(organisation_id)

        async def create(self, *, organisation_id, wrapped_dek, kek_provider, kek_key_id):  # noqa: ANN001, ANN202
            row = SimpleNamespace(
                organisation_id=organisation_id,
                wrapped_dek=wrapped_dek,
                kek_provider=kek_provider,
                kek_key_id=kek_key_id,
            )
            self._rows[organisation_id] = row
            return row

    kek = base64.b64encode(os.urandom(32)).decode("ascii")
    return EnvelopeService(
        kms=LocalKmsProvider(kek), dek_repo=_MemDekRepo(), legacy_decrypt=decrypt_secret
    )


@pytest.fixture
def test_envelope():  # noqa: ANN201
    """A fresh working envelope per test (ADR-020); inject where a service now needs envelope."""
    return make_test_envelope()
