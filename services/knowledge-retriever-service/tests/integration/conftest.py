"""Integration-test fixtures for knowledge-retriever-service (ORAA-56, ORAA-59).

Provides:
- ``async_client``: HTTPX client wired to the KRS FastAPI app.
- ``neo4j_driver``: async Neo4j driver backed by an ephemeral testcontainers
  instance for federation/linked-to traversal tests (ORAA-59).
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
def _krs_neo4j_url() -> Iterator[str]:
    """``bolt://…`` URL for an ephemeral Neo4j container shared across KRS integration tests."""
    from testcontainers.neo4j import Neo4jContainer

    container = Neo4jContainer(NEO4J_IMAGE).with_env("NEO4J_AUTH", "neo4j/password")
    with container:
        yield container.get_connection_url()


@pytest_asyncio.fixture(scope="function")
async def neo4j_driver(_krs_neo4j_url: str) -> AsyncIterator[AsyncDriver]:
    """Async Neo4j driver for KRS integration tests.

    Function-scoped: wipes the graph before each test so federation/LINKED_TO
    tests start from a clean slate and seeding in ``seed_and_cleanup`` is
    deterministic.
    """
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(_krs_neo4j_url, auth=("neo4j", "password"))
    try:
        await driver.verify_connectivity()
        async with driver.session() as session:
            await session.run("MATCH (n) DETACH DELETE n")
        yield driver
    finally:
        await driver.close()


@pytest.fixture
async def async_client():
    """Async HTTPX client wired to the knowledge-retriever-service ASGI app.

    Import is deferred (ORA-48) so pytest --collect-only succeeds during the
    TDD window before the HTTP layer is implemented.
    """
    from httpx import ASGITransport, AsyncClient
    from oraclous_knowledge_retriever_service.app.factory import (  # ORA-48 — RED until impl
        create_app,
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
