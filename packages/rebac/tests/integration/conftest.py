"""Integration fixtures for the ReBAC engine-resolver adapter end-to-end suite.

These fixtures spin up an ephemeral Neo4j container and expose an *async*
``neo4j.AsyncDriver`` — the engine is async and the substrate harness's shared
``neo4j_driver`` is sync (the engine integration suite worked around this by
asserting in raw Cypher; the adapter end-to-end suite needs the engine actually wired up, so an
async driver is unavoidable).

Known duplication: this spins up a second Neo4j container alongside the
substrate harness's. The clean fix is to extract a session-scoped
``neo4j_url`` fixture in ``tests/conftest.py`` and let both sync and async
drivers share the same container — a small ``devops-implementer`` follow-up,
flagged in the PR description for be-test-reviewer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from neo4j import AsyncDriver

NEO4J_IMAGE = "neo4j:5.23-community"


@pytest.fixture(scope="session")
def rebac_neo4j_url() -> Iterator[str]:
    """A ``bolt://...`` URL pointing at an ephemeral Neo4j container.

    Session-scoped so a single container backs every adapter integration test.
    The URL is what the async driver fixture (and any future async user)
    consumes — the container itself is hidden behind this fixture.
    """
    from testcontainers.neo4j import Neo4jContainer

    container = Neo4jContainer(NEO4J_IMAGE).with_env("NEO4J_AUTH", "neo4j/password")
    with container:
        yield container.get_connection_url()


@pytest_asyncio.fixture(scope="function")
async def rebac_async_driver(rebac_neo4j_url: str) -> AsyncIterator[AsyncDriver]:
    """A connected ``neo4j.AsyncDriver`` for the adapter e2e suite.

    Function-scoped so each test seeds and tears down its own graph state —
    the container is shared (session-scoped via ``rebac_neo4j_url``), only
    the driver and the data are per-test.
    """
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(rebac_neo4j_url, auth=("neo4j", "password"))
    try:
        await driver.verify_connectivity()
        # Clean slate per test: the e2e tests assert exact-presence /
        # exact-absence and cross-org isolation, which a dirty graph would
        # silently corrupt.
        async with driver.session() as session:
            await session.run("MATCH (n) DETACH DELETE n")
        yield driver
    finally:
        await driver.close()


class _NullAsyncRedis:
    """A no-op async Redis stand-in for tests that exercise engine internals
    without the cache.

    The engine treats a missing/erroring cache as best-effort: cache miss →
    fall through to Neo4j; cache write failure → log and continue. This stub
    makes that behaviour deterministic for the integration suite, isolating
    the test from a Redis dependency that adds no signal to the adapter
    contract (the cache pinning lives in the engine's unit suite).
    """

    async def get(self, key: str) -> None:  # noqa: ARG002 — signature parity with redis.asyncio
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:  # noqa: ARG002
        return True

    async def delete(self, key: str) -> int:  # noqa: ARG002
        return 0


@pytest.fixture(scope="function")
def null_async_redis() -> _NullAsyncRedis:
    """An async Redis stand-in (see ``_NullAsyncRedis``)."""
    return _NullAsyncRedis()
