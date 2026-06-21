"""Auth-service local test conftest.

Re-creates the substrate Postgres harness fixture here (mirror of the root
``tests/conftest.py``'s ``postgres_dsn``) so this suite can be run in isolation
via ``uv run pytest services/auth-service/tests`` when the shared pytest
session aborts collection on an unmerged sibling import — the soft-coupling
fallback (applicable until ``oraclous_substrate.access`` imports cleanly).

Only the Postgres fixture is duplicated; Neo4j and Redis aren't reachable from
the auth-service identity store (ADR-012 §1a — auth-service is a distinct
enforcement domain, not the tenant-scoped knowledge substrate).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

POSTGRES_IMAGE = "postgres:16"
PG_USER = "oraclous"
PG_PASSWORD = "oraclous"  # noqa: S105 — ephemeral test container, not a real secret
PG_DB = "oraclous"


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """A libpq DSN for an ephemeral Postgres container."""
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        POSTGRES_IMAGE, username=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        yield f"postgresql://{PG_USER}:{PG_PASSWORD}@{host}:{port}/{PG_DB}"
