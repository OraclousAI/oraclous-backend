"""Gateway fail-closed secret tests (WP-1, T6 / ADR-008).

In prod (``RUN_MODE=prod``) a missing/empty INTERNAL_SERVICE_KEY must raise at Settings build, and a
wildcard CORS allow-list is illegal. In dev (and with RUN_MODE unset — the running local docker
stack) the dev defaults still apply and "*" CORS is allowed, unchanged from before.

Settings is built directly (not via the lru-cached get_settings) so each case sees its own env.
"""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.core.config import Settings
from oraclous_governance import MissingSecretError

pytestmark = [pytest.mark.unit, pytest.mark.security]


# --- prod: missing/empty INTERNAL_SERVICE_KEY fails closed ------------------


def test_prod_missing_internal_service_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    monkeypatch.delenv("INTERNAL_SERVICE_KEY", raising=False)
    # give a concrete CORS list so the failure is attributable to the key, not CORS
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "https://app.test")
    with pytest.raises(MissingSecretError):
        Settings()


def test_prod_empty_internal_service_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "")
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "https://app.test")
    with pytest.raises(MissingSecretError):
        Settings()


def test_prod_missing_jwt_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "real-internal-key")
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "https://app.test")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(MissingSecretError):
        Settings()


# --- prod: CORS wildcard is illegal -----------------------------------------


def test_prod_wildcard_cors_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "real-internal-key")
    monkeypatch.setenv("JWT_SECRET", "real-jwt-secret")
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "*")
    with pytest.raises(MissingSecretError):
        Settings()


def test_prod_explicit_cors_and_secrets_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "real-internal-key")
    monkeypatch.setenv("JWT_SECRET", "real-jwt-secret")
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "https://app.test")
    s = Settings()
    assert s.INTERNAL_SERVICE_KEY == "real-internal-key"
    assert s.cors_origins == ["https://app.test"]


# --- dev / unset: unchanged behaviour ---------------------------------------


def test_dev_mode_boots_with_dev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "dev")
    monkeypatch.delenv("INTERNAL_SERVICE_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("GATEWAY_CORS_ORIGINS", raising=False)
    s = Settings()
    assert s.INTERNAL_SERVICE_KEY == "dev-internal-key"
    assert s.cors_origins == ["*"]
    assert s.JWT_SECRET is None  # dev leaves JWT_SECRET None (jwt-mode path handles it)


def test_unset_run_mode_boots_with_dev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The running local docker stack may not set RUN_MODE — it must keep booting with "*" CORS.
    monkeypatch.delenv("RUN_MODE", raising=False)
    monkeypatch.delenv("INTERNAL_SERVICE_KEY", raising=False)
    monkeypatch.delenv("GATEWAY_CORS_ORIGINS", raising=False)
    s = Settings()
    assert s.INTERNAL_SERVICE_KEY == "dev-internal-key"
    assert s.cors_origins == ["*"]
