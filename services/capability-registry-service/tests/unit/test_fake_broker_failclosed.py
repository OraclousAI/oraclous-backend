"""Failure-mode tests for the fail-CLOSED credential-broker default (ADR-021 §1 / #295).

The literal default flipped from ``fake`` to ``real``: a deploy that forgets the override talks to
the REAL broker, never silently fakes credential resolution. Selecting ``fake`` is still valid for
dev/CI/smoke, but it now fires a loud one-time startup alert (``fake_runtime_active``) at the build
site so it can never happen by accident.
"""

from __future__ import annotations

import pytest
from oraclous_capability_registry_service.core.config import Settings
from oraclous_capability_registry_service.core.lifespan import build_credential_broker
from oraclous_capability_registry_service.services.credential_client import (
    FakeCredentialBroker,
    RealCredentialBroker,
)
from oraclous_telemetry import DegradationEvent, register_sink, reset_sinks

pytestmark = pytest.mark.unit

_DSN = "postgresql+asyncpg://u:p@localhost:5432/db"


@pytest.fixture
def captured_alerts():
    events: list[DegradationEvent] = []
    reset_sinks()
    register_sink(events.append)
    yield events
    reset_sinks()


def _settings(**over) -> Settings:
    base = {"DATABASE_URL": _DSN, "INTERNAL_SERVICE_KEY": "k"}
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_default_mode_is_real_and_selects_the_real_broker(monkeypatch, captured_alerts) -> None:
    # No explicit mode → the fail-closed default. delenv so a key-free CI env can't mask it.
    monkeypatch.delenv("CREDENTIAL_BROKER_MODE", raising=False)
    settings = _settings()
    assert settings.CREDENTIAL_BROKER_MODE == "real"
    broker = build_credential_broker(settings)
    assert isinstance(broker, RealCredentialBroker)
    # the real path is silent — the fake alert only fires when fake is selected
    assert not [e for e in captured_alerts if e.code == "fake_runtime_active"]


def test_fake_mode_selected_fires_the_startup_alert(captured_alerts) -> None:
    settings = _settings(CREDENTIAL_BROKER_MODE="fake")
    broker = build_credential_broker(settings)
    assert isinstance(broker, FakeCredentialBroker)
    fired = [e for e in captured_alerts if e.code == "fake_runtime_active"]
    assert len(fired) == 1
    assert fired[0].severity == "warning"
    assert fired[0].context["surface"] == "credential_broker"
    assert fired[0].service == "capability-registry-service"
