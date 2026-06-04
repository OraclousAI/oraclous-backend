"""Integration-test fixtures for knowledge-retriever-service API layer (ORAA-56).

Provides an ``async_client`` HTTP test client fixture that targets the KRS
FastAPI application. All imports of the SUT are function-local (ORA-48 / TST001)
so this conftest can be collected while the HTTP application layer is still
unwritten.

RED until ``oraclous_knowledge_retriever_service.app.factory.create_app()`` is
implemented.
"""

from __future__ import annotations

import pytest


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
