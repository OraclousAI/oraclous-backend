"""Integration-test fixtures for knowledge-graph-service internal API (ORAA-57).

Provides an ``async_client`` HTTP test client fixture that targets the KGS
FastAPI application. All imports of the SUT are function-local (ORA-48 /
TST001) so this conftest can be collected while the internal schema router is
still unwritten.

RED until ``oraclous_knowledge_graph_service.app.create_app()`` is implemented
with the internal schema router mounted.
"""

from __future__ import annotations

import pytest


@pytest.fixture
async def async_client():
    """Async HTTPX client wired to the knowledge-graph-service ASGI app.

    Import is deferred (ORA-48) so pytest --collect-only succeeds during the
    TDD window before the internal schema router is implemented.
    """
    from httpx import ASGITransport, AsyncClient
    from oraclous_knowledge_graph_service.app import create_app  # ORA-48 — RED until impl

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
