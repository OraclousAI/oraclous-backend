"""Substrate test harness (ORA-12 / 0d).

Session-scoped, auto-torn-down real Neo4j, Postgres and Redis instances via
testcontainers, so integration / organization_isolation / security tests
assert at the data layer rather than against mocks. Containers start lazily —
only when a test requests a fixture — so unit tests stay fast and Docker-free.

The harness exposes both sync and async surfaces over the **same** underlying
container so adversarial / engine-driven suites that need an async driver
(``ReBACEngine``, async SQLAlchemy) share the container with the existing
Cypher-level ORA-34 / ORA-35 fixtures rather than spawning duplicates.

Public fixtures:

* ``neo4j_driver`` — session-scoped sync ``neo4j.Driver`` (legacy + ORA-34/35)
* ``neo4j_url`` — session-scoped ``bolt://`` URL for the same container
* ``neo4j_async_driver`` — function-scoped ``neo4j.AsyncDriver`` with a
  per-test ``MATCH (n) DETACH DELETE n`` so adversarial tests assert on a
  clean graph (ORA-37 R1 gate)
* ``postgres_dsn`` — session-scoped libpq DSN (``postgresql://…``) for psycopg
* ``postgres_async_dsn`` — session-scoped asyncpg DSN
  (``postgresql+asyncpg://…``) for async SQLAlchemy engines
* ``redis_client`` — session-scoped sync ``redis.Redis``
* ``redis_url`` — session-scoped ``redis://…`` URL for the same container
* ``redis_async_client`` — function-scoped async ``redis.asyncio.Redis``
  with a per-test ``FLUSHDB`` so cache-invalidation assertions are not
  poisoned by neighbouring tests (ORA-37 R1 gate)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from neo4j import AsyncDriver, Driver
    from redis import Redis
    from redis.asyncio import Redis as AsyncRedis
    from testcontainers.neo4j import Neo4jContainer
    from testcontainers.redis import RedisContainer

NEO4J_IMAGE = "neo4j:5.23-community"
POSTGRES_IMAGE = "postgres:16"
REDIS_IMAGE = "redis:7.4-alpine"
PG_USER = "oraclous"
PG_PASSWORD = "oraclous"  # noqa: S105 — ephemeral test container, not a real secret
PG_DB = "oraclous"


# ── Neo4j ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def _neo4j_container() -> Iterator[Neo4jContainer]:
    """Private: the underlying ephemeral Neo4j container.

    Split out so sync + async drivers share one container — the alternative
    (two containers per session) doubles startup time and RAM for no signal.
    """
    from testcontainers.neo4j import Neo4jContainer

    container = Neo4jContainer(NEO4J_IMAGE).with_env("NEO4J_AUTH", "neo4j/password")
    with container:
        yield container


@pytest.fixture(scope="session")
def neo4j_url(_neo4j_container: Neo4jContainer) -> str:
    """``bolt://…`` URL for the shared Neo4j container."""
    return _neo4j_container.get_connection_url()


@pytest.fixture(scope="session")
def neo4j_driver(_neo4j_container: Neo4jContainer) -> Iterator[Driver]:
    """A connected sync ``neo4j.Driver`` backed by the shared container."""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(_neo4j_container.get_connection_url(), auth=("neo4j", "password"))
    try:
        driver.verify_connectivity()
        yield driver
    finally:
        driver.close()


@pytest_asyncio.fixture(scope="function")
async def neo4j_async_driver(neo4j_url: str) -> AsyncIterator[AsyncDriver]:
    """A connected async ``neo4j.AsyncDriver`` over the shared container.

    Wipes the graph per test so adversarial assertions on exact presence /
    absence are not poisoned by neighbouring tests. Function-scoped (the
    container is session-scoped — only the driver + data churn per test).
    """
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(neo4j_url, auth=("neo4j", "password"))
    try:
        await driver.verify_connectivity()
        async with driver.session() as session:
            await session.run("MATCH (n) DETACH DELETE n")
        yield driver
    finally:
        await driver.close()


# ── Postgres ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """A libpq DSN for an ephemeral Postgres container (usable by psycopg)."""
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        POSTGRES_IMAGE, username=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        yield f"postgresql://{PG_USER}:{PG_PASSWORD}@{host}:{port}/{PG_DB}"


@pytest.fixture(scope="session")
def postgres_async_dsn(postgres_dsn: str) -> str:
    """An asyncpg DSN (``postgresql+asyncpg://…``) for the same container.

    Async SQLAlchemy engines reject the bare ``postgresql://`` scheme; this
    fixture is the trivial conversion. Same container as ``postgres_dsn`` so
    a test using both sees one Postgres instance.
    """
    return postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


# ── Redis ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def _redis_container() -> Iterator[RedisContainer]:
    """Private: the underlying ephemeral Redis container."""
    from testcontainers.redis import RedisContainer

    container = RedisContainer(REDIS_IMAGE)
    with container:
        yield container


@pytest.fixture(scope="session")
def redis_url(_redis_container: RedisContainer) -> str:
    """``redis://…`` URL for the shared Redis container (db 0)."""
    host = _redis_container.get_container_host_ip()
    port = int(_redis_container.get_exposed_port(6379))
    return f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def redis_client(_redis_container: RedisContainer) -> Iterator[Redis]:
    """A connected sync ``redis.Redis`` over the shared container."""
    import redis as redis_lib

    client = redis_lib.Redis(
        host=_redis_container.get_container_host_ip(),
        port=int(_redis_container.get_exposed_port(6379)),
        decode_responses=True,
    )
    try:
        yield client
    finally:
        client.close()


@pytest_asyncio.fixture(scope="function")
async def redis_async_client(redis_url: str) -> AsyncIterator[AsyncRedis]:
    """A connected async ``redis.asyncio.Redis`` over the shared container.

    ``FLUSHDB`` per test so a cached entry from a neighbouring test cannot
    mask (or fabricate) the invalidation behaviour adversarial tests assert.
    Function-scoped.
    """
    from redis import asyncio as aioredis

    client = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await client.flushdb()
        yield client
    finally:
        await client.aclose()
