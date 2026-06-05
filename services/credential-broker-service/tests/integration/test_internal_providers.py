"""Integration: the internal-key gate on the provider catalogue endpoint (S2). No DB needed."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_INTERNAL = "s2-internal-key"  # noqa: S105 — test internal key


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", _INTERNAL)
    from oraclous_credential_broker_service.core.config import get_settings

    get_settings.cache_clear()
    from oraclous_credential_broker_service.app.factory import create_app

    app = create_app(lifespan=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cb.test") as c:
        yield c
    get_settings.cache_clear()


async def test_missing_internal_key_is_401(client: AsyncClient) -> None:
    assert (await client.get("/internal/providers")).status_code == 401


async def test_wrong_internal_key_is_401(client: AsyncClient) -> None:
    resp = await client.get("/internal/providers", headers={"X-Internal-Key": _INTERNAL + "x"})
    assert resp.status_code == 401


async def test_valid_internal_key_returns_catalogue(client: AsyncClient) -> None:
    resp = await client.get("/internal/providers", headers={"X-Internal-Key": _INTERNAL})
    assert resp.status_code == 200
    providers = resp.json()["providers"]
    assert set(providers) == {"google", "notion", "github"}
    assert "drive" in providers["google"]
