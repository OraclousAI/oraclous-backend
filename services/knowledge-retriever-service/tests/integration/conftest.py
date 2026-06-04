"""Integration-test fixtures for the KRS API layer (R3.5).

`async_client` targets the real `create_app()` ASGI app. No Neo4j: tests override the
`get_retrieval_service` dependency with an in-memory fake. The real dev-auth seam IS exercised
(401 paths are real). Live retrieval over real substrate is covered by the docker smoke.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def app():
    from oraclous_knowledge_retriever_service.app import create_app

    return create_app()


@pytest.fixture
async def async_client(app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
