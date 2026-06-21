"""Shared fixtures for the substrate organisation-isolation suite.

``app_dsn`` exposes a Postgres DSN for a dedicated **NOSUPERUSER / NOBYPASSRLS**
application role, with the A1 schema already applied as the superuser.

Why this exists: Postgres superusers (and ``BYPASSRLS`` roles) bypass row-level
security entirely, and the 0d harness container connects as its bootstrap
superuser. So an RLS-isolation test issued over the raw superuser ``postgres_dsn``
proves nothing — RLS never bites. The A2 enforcement seam's data-layer backstop
is only real for a non-superuser role (ADR-012 precondition; mirrors the RLS
gate's harness note). Tests that assert RLS isolation must use ``app_dsn``.

Schema/DDL (CREATE TABLE, ENABLE/FORCE RLS, policies) is applied as the superuser
here; the NOSUPERUSER role only ever gets SELECT/INSERT/UPDATE/DELETE, so it is
subject to the policies it is meant to be isolated by.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import pytest

_APP_ROLE = "oraclous_app"
_APP_PASSWORD = "app"  # noqa: S105 — ephemeral test-container role, not a real secret


@pytest.fixture(scope="module")
def app_dsn(postgres_dsn: str) -> str:
    """A DSN for a NOSUPERUSER/NOBYPASSRLS role, A1 schema pre-applied."""
    import psycopg
    from oraclous_substrate.schema import postgres as pg_schema

    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        pg_schema.apply(conn)
        with conn.cursor() as cur:
            # Idempotent: the session container is shared across tests.
            cur.execute(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oraclous_app') THEN "
                "CREATE ROLE oraclous_app LOGIN PASSWORD 'app' "
                "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; "
                "END IF; END $$;"
            )
            cur.execute("GRANT USAGE ON SCHEMA public TO oraclous_app")
            cur.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE "
                "ON ALL TABLES IN SCHEMA public TO oraclous_app"
            )

    parts = urlsplit(postgres_dsn)
    netloc = f"{_APP_ROLE}:{_APP_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
