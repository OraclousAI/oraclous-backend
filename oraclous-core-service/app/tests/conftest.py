"""
Shared pytest fixtures for oraclous-core-service integration tests.

Provides session-scoped Postgres via testcontainers and function-scoped
async SQLAlchemy sessions with per-test transaction rollback for isolation.
Alembic migrations are applied once per session against the container DB.

Usage in tests:
    async def test_something(async_session):
        ...

For migration-specific tests (reversibility), use the `alembic_runner` fixture.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import asyncpg

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CORE_SERVICE_DIR = Path(__file__).parent.parent.parent  # oraclous-core-service/
APP_DIR = CORE_SERVICE_DIR / "app"


# ---------------------------------------------------------------------------
# Session-scoped Postgres container
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """
    Start an ephemeral Postgres 16 container and yield a sync libpq DSN.

    Prefers TEST_POSTGRES_DSN from the environment (set by stack-up.sh / .stack-env)
    so CI and the ephemeral stack contract both work without change.
    """
    env_dsn = os.environ.get("TEST_POSTGRES_DSN")
    if env_dsn:
        yield env_dsn
        return

    from testcontainers.postgres import PostgresContainer  # type: ignore[import]

    PG_USER = "oraclous"
    PG_PASS = "oraclous"
    PG_DB = "oraclous_test"

    with PostgresContainer("postgres:16", username=PG_USER, password=PG_PASS, dbname=PG_DB) as pg:
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        yield f"postgresql://{PG_USER}:{PG_PASS}@{host}:{port}/{PG_DB}"


@pytest.fixture(scope="session")
def async_postgres_dsn(postgres_dsn: str) -> str:
    """Convert the libpq DSN to an asyncpg DSN."""
    return postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


# ---------------------------------------------------------------------------
# Session-scoped alembic migration
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migrated_db(postgres_dsn: str) -> Iterator[str]:
    """
    Apply `alembic upgrade head` against the container DB and yield the DSN.

    This fixture will fail until the implementer creates the
    capability_descriptor Alembic migration (ORAA-69 acceptance criteria 5).
    That failure is intentional — it is the expected integration-level TDD
    failure after the import-level failure is resolved.
    """
    alembic_ini = APP_DIR / "alembic.ini"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(alembic_ini),
            "-x",
            f"sqlalchemy.url=postgresql+asyncpg://{postgres_dsn.split('postgresql://', 1)[1]}",
            "upgrade",
            "head",
        ],
        cwd=str(APP_DIR),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(APP_DIR)},
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic upgrade head failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    yield postgres_dsn


# ---------------------------------------------------------------------------
# Session-scoped async engine (post-migration)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def async_engine(async_postgres_dsn: str, migrated_db: str) -> AsyncEngine:
    """Async SQLAlchemy engine connected to the migrated test DB."""
    return create_async_engine(async_postgres_dsn, echo=False)


# ---------------------------------------------------------------------------
# Function-scoped async session with per-test rollback
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def async_session(async_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """
    Async session wrapped in a savepoint transaction that is rolled back after
    each test, so tests are fully isolated without a full DB wipe.
    """
    async with async_engine.connect() as conn:
        await conn.begin()
        await conn.begin_nested()  # savepoint

        session_factory = async_sessionmaker(
            bind=conn,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        async with session_factory() as session:
            yield session
            await session.rollback()

        await conn.rollback()


# ---------------------------------------------------------------------------
# Alembic runner fixture for migration reversibility tests
# ---------------------------------------------------------------------------


@pytest.fixture
def alembic_runner(postgres_dsn: str):
    """
    Helper for running alembic commands against the test DB.

    Returned object has .upgrade(rev) and .downgrade(rev) methods.
    Used by migration reversibility tests (D18).
    """

    alembic_ini = APP_DIR / "alembic.ini"
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

    class _Runner:
        def _run(self, *args: str) -> None:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-c",
                    str(alembic_ini),
                    "-x",
                    f"sqlalchemy.url={async_dsn}",
                ] + list(args),
                cwd=str(APP_DIR),
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(APP_DIR)},
            )
            if result.returncode != 0:
                pytest.fail(
                    f"alembic {' '.join(args)} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
                )

        def upgrade(self, rev: str = "head") -> None:
            self._run("upgrade", rev)

        def downgrade(self, rev: str = "-1") -> None:
            self._run("downgrade", rev)

    return _Runner()


# ---------------------------------------------------------------------------
# Migration seed fixture for D17 (legacy tool_definitions → capability_descriptor)
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_migrated_db(postgres_dsn: str, alembic_runner):
    """
    Fixture for D17: verifies AC #3 — existing tool_definitions rows are migrated
    into capability_descriptor with kind=tool.

    Sequence:
      1. downgrade -1  → restores tool_definitions, drops capability_descriptor
      2. seed one tool_definitions row via asyncpg
      3. upgrade head  → migration must forward-fill capability_descriptor

    Yields (postgres_dsn, seeded_id) for the test to assert against.
    Leaves the DB at head on exit so subsequent tests see a clean schema.
    """
    seeded_id = str(uuid.uuid4())

    # 1. Downgrade to the revision before capability_descriptor was introduced
    alembic_runner.downgrade("-1")

    # 2. Seed a minimally valid tool_definitions row
    async def _seed() -> None:
        conn = await asyncpg.connect(postgres_dsn)
        try:
            await conn.execute(
                "INSERT INTO tool_definitions "
                "(id, name, category, type, input_schema, output_schema, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, NOW(), NOW())",
                seeded_id,
                "test-legacy-tool",
                "test-category",
                "test-type",
                json.dumps({"type": "object"}),
                json.dumps({"type": "object"}),
            )
        finally:
            await conn.close()

    # Use an explicit new loop instead of asyncio.run() to avoid clearing the
    # session-scoped event loop that pytest-asyncio manages.  asyncio.run() calls
    # asyncio.set_event_loop(None) on exit, which breaks async tests that run after
    # this fixture in the same session (D19/D20 are affected).
    _loop = asyncio.new_event_loop()
    try:
        _loop.run_until_complete(_seed())
    finally:
        _loop.close()

    # 3. Re-run the migration — must forward-fill capability_descriptor
    alembic_runner.upgrade("head")

    yield postgres_dsn, seeded_id
