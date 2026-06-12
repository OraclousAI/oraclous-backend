"""Failure-mode tests for readiness-reflecting health (ADR-021 / ORAA-297).

Hard DoD per ADR-021: simulate a critical-store bind failure at startup and assert (a) the
degradation alert fires and (b) ``/health`` reflects degraded while ``/readyz`` 503s; a healthy
startup reports ``ok``. The critical store is Postgres (the sessionmaker); Redis is fail-open.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_auth_service.app.factory import create_app
from oraclous_auth_service.core import lifespan as lifespan_module
from oraclous_telemetry import DegradationEvent, register_sink, reset_sinks

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _capture_alerts():
    events: list[DegradationEvent] = []
    reset_sinks()
    register_sink(events.append)
    yield events
    reset_sinks()


def _app():
    # The agent repository is irrelevant to /health; a bare object satisfies the factory.
    return create_app(agent_repository=object(), internal_service_key="k")


async def _get(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(path)


async def test_store_bind_failure_reports_degraded_and_alerts(monkeypatch, _capture_alerts):
    monkeypatch.delenv("EXIT_ON_STARTUP_DEGRADE", raising=False)

    def _boom():
        raise RuntimeError("postgres down")

    monkeypatch.setattr(lifespan_module, "make_engine", _boom)
    app = _app()

    async with lifespan_module.lifespan(app):
        # the alert fired during startup
        assert any(
            e.code == "store_bind_failed" and e.context.get("store") == "postgres"
            for e in _capture_alerts
        )
        # /health is liveness — 200, body degraded
        health = await _get(app, "/health")
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"
        # /readyz is readiness — 503 while degraded
        ready = await _get(app, "/readyz")
        assert ready.status_code == 503
        assert ready.json()["status"] == "degraded"


async def test_healthy_startup_reports_ok(monkeypatch):
    class _FakeEngine:
        async def dispose(self):
            return None

    monkeypatch.setattr(lifespan_module, "make_engine", _FakeEngine)
    monkeypatch.setattr(lifespan_module, "make_sessionmaker", lambda _engine: object())
    app = _app()

    async with lifespan_module.lifespan(app):
        health = await _get(app, "/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        ready = await _get(app, "/readyz")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ok"
