"""Failure-mode tests for the fail-CLOSED LLM-mode default (ADR-021 §1 / #295).

``llm_mode`` flipped from ``fake`` to ``live``: a deploy that forgets the override runs the REAL
LLM, never the scripted responder by accident. Selecting ``fake`` is valid for dev/CI/smoke but now
fires a loud one-time startup alert (``fake_runtime_active``) in the lifespan, so it can't slip in
silently.
"""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.app.factory import create_app
from oraclous_harness_runtime_service.core import lifespan as lifespan_module
from oraclous_harness_runtime_service.core.config import Settings, get_settings
from oraclous_telemetry import DegradationEvent, register_sink, reset_sinks

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # a non-connecting DSN: the engine is lazy, so the lifespan binds the repos (no real connect)
    monkeypatch.setenv("HARNESS_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.delenv("EXIT_ON_STARTUP_DEGRADE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def captured_alerts():
    events: list[DegradationEvent] = []
    reset_sinks()
    register_sink(events.append)
    yield events
    reset_sinks()


def test_default_llm_mode_is_live(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_LLM_MODE", raising=False)
    assert Settings().llm_mode == "live"


async def test_fake_llm_mode_fires_the_startup_alert(monkeypatch, captured_alerts) -> None:
    monkeypatch.setenv("HARNESS_LLM_MODE", "fake")
    get_settings.cache_clear()
    app = create_app()
    async with lifespan_module.lifespan(app):
        fired = [e for e in captured_alerts if e.code == "fake_runtime_active"]
        assert len(fired) == 1
        assert fired[0].severity == "warning"
        assert fired[0].context["surface"] == "llm"
        assert fired[0].service == "harness-runtime-service"


async def test_live_llm_mode_does_not_fire_the_fake_alert(monkeypatch, captured_alerts) -> None:
    monkeypatch.delenv("HARNESS_LLM_MODE", raising=False)  # default → live
    get_settings.cache_clear()
    app = create_app()
    async with lifespan_module.lifespan(app):
        assert not [e for e in captured_alerts if e.code == "fake_runtime_active"]
