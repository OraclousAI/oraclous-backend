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

import importlib
import sys
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


@pytest.fixture(autouse=True)
def _reset_bcrypt_worker_pool() -> Iterator[None]:
    """Tear down the shared bcrypt worker pool after every test (issue #1029 hygiene fix).

    ``oraclous_auth_service.core.password_hashing.get_executor()`` lazily creates and caches ONE
    module-global ``ThreadPoolExecutor``. In production that is correct — the module is imported
    once and the pool is torn down via ``atexit`` at real process exit. In this test process it is
    not: every test that exercises ``bcrypt_hash``/``bcrypt_verify`` spins its worker threads up,
    and ``test_password_hashing_pool.py``'s ``importlib.reload`` of the module abandons whichever
    executor was cached at the time, orphaning its threads outright. Nothing in the test run ever
    reaches real process exit, so those threads just accumulate — measured at 9 live
    ``bcrypt-worker-*`` threads still running after ``services/auth-service/tests/unit`` alone,
    which is exactly the kind of thing that can tip a marginal CI runner's thread/process budget
    over the edge for an unrelated, later test in the same ``pytest -m unit`` process.

    Shut the current executor down (waiting for its threads to actually exit) and reload the
    module so the next test starts from a clean, uncached pool — mirroring the reset the pool test
    itself already does around its own reload.
    """
    yield

    module = sys.modules.get("oraclous_auth_service.core.password_hashing")
    if module is None:
        return
    module.get_executor().shutdown(wait=True)
    importlib.reload(module)
