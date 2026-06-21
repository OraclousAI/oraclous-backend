"""Failure-mode tests for readiness-reflecting health (ADR-021).

Simulate the critical-store (Postgres) bind failure at startup → the degradation alert fires and
``/health`` reflects degraded while ``/readyz`` 503s; a healthy startup reports the live status.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_credential_broker_service.app.factory import create_app
from oraclous_credential_broker_service.core import lifespan as lifespan_module
from oraclous_telemetry import DegradationEvent, register_sink, reset_sinks

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _capture_alerts():
    events: list[DegradationEvent] = []
    reset_sinks()
    register_sink(events.append)
    yield events
    reset_sinks()


async def _get(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(path)


def _set_required_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("ENCRYPTION_KEY", "x" * 32)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "k")


async def test_store_bind_failure_reports_degraded_and_alerts(monkeypatch, _capture_alerts):
    monkeypatch.delenv("EXIT_ON_STARTUP_DEGRADE", raising=False)
    _set_required_env(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("postgres down")

    # the envelope build (first store touch in the try) raises → the degrade branch nulls state
    monkeypatch.setattr(lifespan_module, "OrgDataKeyRepository", _boom)
    app = create_app()

    async with lifespan_module.lifespan(app):
        assert any(
            e.code == "store_bind_failed" and e.context.get("store") == "postgres"
            for e in _capture_alerts
        )
        health = await _get(app, "/health")
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"
        ready = await _get(app, "/readyz")
        assert ready.status_code == 503
        assert ready.json()["status"] == "degraded"


async def test_healthy_startup_reports_healthy(monkeypatch):
    # /health is liveness; the route only reads app.state.credential_repository. Bypass the real
    # store wiring by setting a present critical store directly (no lifespan needed for ok path).
    app = create_app()
    app.state.credential_repository = object()
    health = await _get(app, "/health")
    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
    ready = await _get(app, "/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "healthy"
