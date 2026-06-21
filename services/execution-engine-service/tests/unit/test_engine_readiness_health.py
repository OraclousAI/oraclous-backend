"""Failure-mode tests for readiness-reflecting health (ADR-021).

Simulate the critical-store (Postgres) bind failure at startup → the degradation alert fires and
``/health`` reflects degraded while ``/readyz`` 503s; a healthy startup reports ``ok``.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_execution_engine_service.app.factory import create_app
from oraclous_execution_engine_service.core import lifespan as lifespan_module
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


async def test_store_bind_failure_reports_degraded_and_alerts(monkeypatch, _capture_alerts):
    monkeypatch.delenv("EXIT_ON_STARTUP_DEGRADE", raising=False)

    def _boom(*_a, **_k):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(lifespan_module, "JobRepository", _boom)
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


async def test_healthy_startup_reports_ok():
    # /health is liveness; the route only reads app.state.job_repository.
    app = create_app()
    app.state.job_repository = object()
    health = await _get(app, "/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    ready = await _get(app, "/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
