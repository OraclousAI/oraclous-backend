"""Unit: the gateway's dependency-free /health probe answers 200 (no upstream/DB needed)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_application_gateway_service.app.factory import create_app

pytestmark = pytest.mark.unit


async def test_health_is_ok_without_any_substrate() -> None:
    app = create_app(lifespan=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "application-gateway"
