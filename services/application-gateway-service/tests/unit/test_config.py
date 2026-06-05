"""Unit: gateway settings — CORS origins parsing + upstream defaults."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.core.config import Settings

pytestmark = pytest.mark.unit


def test_cors_origins_splits_comma_separated() -> None:
    s = Settings(GATEWAY_CORS_ORIGINS="https://a.test, https://b.test ,")
    assert s.cors_origins == ["https://a.test", "https://b.test"]


def test_cors_origins_wildcard_default() -> None:
    assert Settings().cors_origins == ["*"]


def test_upstream_defaults_point_at_substrate_service_names() -> None:
    s = Settings()
    assert s.AUTH_SERVICE_URL.endswith("auth-service:8000")
    assert s.CAPABILITY_REGISTRY_URL.endswith("capability-registry-service:8000")
    assert s.GATEWAY_AUTH_MODE == "dev"
