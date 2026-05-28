"""Substrate test harness (ORA-12 / 0d).

Session-scoped, auto-torn-down real Neo4j, Postgres and Redis instances via
testcontainers, so integration / organization_isolation tests assert at the
data layer rather than against mocks. Containers start lazily — only when a
test requests the fixture — so unit tests stay fast and Docker-free.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from neo4j import Driver
    from redis import Redis

NEO4J_IMAGE = "neo4j:5.23-community"
POSTGRES_IMAGE = "postgres:16"
REDIS_IMAGE = "redis:7.4-alpine"
PG_USER = "oraclous"
PG_PASSWORD = "oraclous"  # noqa: S105 — ephemeral test container, not a real secret
PG_DB = "oraclous"


@pytest.fixture(scope="session")
def neo4j_driver() -> Iterator[Driver]:
    """A connected Neo4j driver backed by an ephemeral container."""
    from neo4j import GraphDatabase
    from testcontainers.neo4j import Neo4jContainer

    container = Neo4jContainer(NEO4J_IMAGE).with_env("NEO4J_AUTH", "neo4j/password")
    with container:
        driver = GraphDatabase.driver(container.get_connection_url(), auth=("neo4j", "password"))
        try:
            driver.verify_connectivity()
            yield driver
        finally:
            driver.close()


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
def redis_client() -> Iterator[Redis]:
    """A connected Redis client backed by an ephemeral container."""
    import redis as redis_lib
    from testcontainers.redis import RedisContainer

    container = RedisContainer(REDIS_IMAGE)
    with container:
        client = redis_lib.Redis(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(6379)),
            decode_responses=True,
        )
        try:
            yield client
        finally:
            client.close()
