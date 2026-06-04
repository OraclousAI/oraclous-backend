"""FastAPI scaffold tests — knowledge-retriever-service health endpoint (ORAA-56).

Acceptance criterion 1: the service container boots and passes its health check.

These tests pin the expected shape of the KRS FastAPI scaffold:
  - ``oraclous_knowledge_retriever_service.app.factory.create_app`` is importable
  - ``GET /health`` returns HTTP 200 with body ``{"status": "healthy", "version": "..."}``
  - ``GET /health`` response body must not contain server-internal implementation detail

Imports of the not-yet-built seam ``oraclous_knowledge_retriever_service.app``
are function-local per ORA-48 / TST001 — collection succeeds while the module
is absent; each test fails RED at runtime with ``ModuleNotFoundError`` until the
``[impl]`` PR lands.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# App factory importability
# ---------------------------------------------------------------------------


class TestAppFactoryImport:
    """The KRS FastAPI app factory must be importable before wiring."""

    def test_create_app_is_importable(self) -> None:
        """``oraclous_knowledge_retriever_service.app.factory`` exposes ``create_app``."""
        from oraclous_knowledge_retriever_service.app.factory import (  # ORA-48 — RED until impl
            create_app,
        )

        assert callable(create_app)

    def test_create_app_returns_fastapi_instance(self) -> None:
        """``create_app()`` must return a FastAPI application instance."""
        from fastapi import FastAPI
        from oraclous_knowledge_retriever_service.app.factory import (  # ORA-48
            create_app,
        )

        app = create_app()
        assert isinstance(app, FastAPI)

    def test_create_app_sets_service_title(self) -> None:
        """The FastAPI app must identify itself as the knowledge-retriever-service."""
        from oraclous_knowledge_retriever_service.app.factory import (  # ORA-48
            create_app,
        )

        app = create_app()
        assert "knowledge-retriever" in app.title.lower()


# ---------------------------------------------------------------------------
# GET /health — shape and status
# ---------------------------------------------------------------------------


class TestHealthEndpointShape:
    """``GET /health`` must return a well-formed response that Kubernetes can act on."""

    async def _client(self):
        """Build an HTTPX test client against the KRS ASGI app."""
        from httpx import ASGITransport, AsyncClient
        from oraclous_knowledge_retriever_service.app.factory import (  # ORA-48
            create_app,
        )

        app = create_app()
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_health_endpoint_returns_200(self) -> None:
        """``GET /health`` must return HTTP 200 OK."""
        async with await self._client() as client:
            response = await client.get("/health")
        assert response.status_code == 200

    async def test_health_response_status_field_is_healthy(self) -> None:
        """``GET /health`` body must contain ``status`` = ``"healthy"``."""
        async with await self._client() as client:
            response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body.get("status") == "healthy"

    async def test_health_response_includes_version_field(self) -> None:
        """``GET /health`` body must include a non-empty ``version`` field."""
        async with await self._client() as client:
            response = await client.get("/health")
        body = response.json()
        version = body.get("version")
        assert isinstance(version, str) and version.strip()

    async def test_health_response_is_json(self) -> None:
        """``GET /health`` must return ``Content-Type: application/json``."""
        async with await self._client() as client:
            response = await client.get("/health")
        assert "application/json" in response.headers.get("content-type", "")

    async def test_health_response_does_not_expose_internal_paths(self) -> None:
        """``GET /health`` must not leak stack traces or internal file paths."""
        async with await self._client() as client:
            response = await client.get("/health")
        body = response.text
        assert "/Users/" not in body
        assert "Traceback" not in body
        assert "Exception" not in body


# ---------------------------------------------------------------------------
# GET /health — failure modes
# ---------------------------------------------------------------------------


class TestHealthEndpointFailureModes:
    """The health endpoint must refuse unexpected methods gracefully."""

    async def _client(self):
        from httpx import ASGITransport, AsyncClient
        from oraclous_knowledge_retriever_service.app.factory import (  # ORA-48
            create_app,
        )

        app = create_app()
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_health_post_returns_405(self) -> None:
        """``POST /health`` must be refused — the probe is GET-only."""
        async with await self._client() as client:
            response = await client.post("/health", json={})
        assert response.status_code == 405

    async def test_unknown_path_returns_404(self) -> None:
        """Unknown paths must not accidentally return 200."""
        async with await self._client() as client:
            response = await client.get("/no-such-path")
        assert response.status_code == 404
