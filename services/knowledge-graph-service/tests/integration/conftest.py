"""Integration-test fixtures for the knowledge-graph-service API layer (R3.5-P1-S1).

`async_client` targets the real `create_app()` ASGI app. The DB is NOT touched: tests override the
`get_graph_service` dependency with an in-memory fake (see `test_graph_api.py`), so these run with
no Postgres. The real dev-auth seam (`verify_token`, `get_principal`) IS exercised — 401 paths are
real. Cross-org scoping against a live DB is covered by the docker smoke (tests/smoke/smoke.sh).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def app():
    from oraclous_knowledge_graph_service.app import create_app

    return create_app()


@pytest.fixture
async def async_client(app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
