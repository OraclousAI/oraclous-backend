"""Integration tests — KRS health endpoint (ORAA-56).

Acceptance criterion 1 (integration layer): the running KRS FastAPI application
responds to ``GET /health`` with HTTP 200 and a well-formed JSON body.

These tests exercise the full ASGI request/response cycle via HTTPX's
``ASGITransport`` — no Docker container is required for unit/integration, but
the same endpoint shape is what the Dockerfile HEALTHCHECK probes.

All imports are function-local per ORA-48 / TST001.
RED until ``create_app`` is implemented.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


class TestKRSHealthEndpointIntegration:
    """Full ASGI cycle through the KRS health endpoint."""

    async def test_health_returns_200(self, async_client) -> None:
        """``GET /health`` must return HTTP 200 — Kubernetes liveness passes."""
        response = await async_client.get("/health")
        assert response.status_code == 200, (
            f"Expected 200 from GET /health, got {response.status_code}\nBody: {response.text}"
        )

    async def test_health_status_is_healthy(self, async_client) -> None:
        """``GET /health`` body must carry ``status: healthy``."""
        response = await async_client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"

    async def test_health_version_is_present(self, async_client) -> None:
        """``GET /health`` body must carry a non-empty ``version`` string."""
        response = await async_client.get("/health")
        body = response.json()
        version = body.get("version")
        assert isinstance(version, str) and version.strip(), (
            f"health.version must be a non-empty string; got {version!r}"
        )

    async def test_health_response_content_type_is_json(self, async_client) -> None:
        """``GET /health`` must return ``Content-Type: application/json``."""
        response = await async_client.get("/health")
        assert "application/json" in response.headers.get("content-type", "")

    async def test_health_body_contains_only_expected_keys(self, async_client) -> None:
        """``GET /health`` body must not leak unexpected keys.

        The minimal contract is ``{status, version}``. Additional monitoring
        keys (e.g. ``dependencies``) are allowed, but sensitive internals
        (file paths, environment variables, stack traces) must not appear.
        """
        response = await async_client.get("/health")
        body = response.json()
        assert "status" in body
        assert "version" in body
        # Sensitive leaks
        for key in ("traceback", "env", "secret", "password", "token"):
            assert key not in body, f"Health response must not expose {key!r}"
