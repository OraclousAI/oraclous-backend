"""Auth-service fail-closed secret tests (WP-1, T6 / ADR-008).

In prod (``RUN_MODE=prod``) a missing/empty JWT_SECRET / INTERNAL_SERVICE_KEY (config) or
OAUTH_ENC_KEY (encryption) must raise rather than silently boot with a publicly-known default. In
dev (and with RUN_MODE unset — the running local docker stack) the dev defaults still apply so the
service boots key-free, unchanged from before.
"""

from __future__ import annotations

import pytest
from oraclous_auth_service.core import encryption
from oraclous_auth_service.core.config import get_settings
from oraclous_governance import MissingSecretError

pytestmark = [pytest.mark.unit, pytest.mark.security]

_SECRET_ENVS = ("JWT_SECRET", "INTERNAL_SERVICE_KEY", "OAUTH_ENC_KEY")


def _clear_secret_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SECRET_ENVS:
        monkeypatch.delenv(name, raising=False)


# --- prod: missing secret fails closed --------------------------------------


@pytest.mark.parametrize("secret", ["JWT_SECRET", "INTERNAL_SERVICE_KEY"])
def test_prod_missing_config_secret_raises(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    _clear_secret_envs(monkeypatch)
    # supply the OTHER config secret so the failure is attributable to `secret`
    other = "INTERNAL_SERVICE_KEY" if secret == "JWT_SECRET" else "JWT_SECRET"  # noqa: S105
    monkeypatch.setenv(other, "real-other-value")
    with pytest.raises(MissingSecretError):
        get_settings()


@pytest.mark.parametrize("secret", ["JWT_SECRET", "INTERNAL_SERVICE_KEY"])
def test_prod_empty_config_secret_raises(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    _clear_secret_envs(monkeypatch)
    other = "INTERNAL_SERVICE_KEY" if secret == "JWT_SECRET" else "JWT_SECRET"  # noqa: S105
    monkeypatch.setenv(other, "real-other-value")
    monkeypatch.setenv(secret, "")
    with pytest.raises(MissingSecretError):
        get_settings()


def test_prod_missing_oauth_enc_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    monkeypatch.delenv("OAUTH_ENC_KEY", raising=False)
    with pytest.raises(MissingSecretError):
        encryption.encrypt("secret-token")


def test_prod_empty_oauth_enc_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # The `or _DEV_KEY` bug: an empty OAUTH_ENC_KEY in prod must NOT reach the dev key.
    monkeypatch.setenv("RUN_MODE", "prod")
    monkeypatch.setenv("OAUTH_ENC_KEY", "")
    with pytest.raises(MissingSecretError):
        encryption.encrypt("secret-token")


# --- dev / unset: still boots key-free (unchanged behaviour) ----------------


def test_dev_mode_boots_with_dev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "dev")
    _clear_secret_envs(monkeypatch)
    settings = get_settings()
    assert settings.jwt_secret == "change-me-in-production"  # noqa: S105
    assert settings.internal_service_key == "dev-internal-key"  # noqa: S105


def test_unset_run_mode_boots_with_dev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The running local docker stack may not set RUN_MODE — it must keep booting.
    monkeypatch.delenv("RUN_MODE", raising=False)
    _clear_secret_envs(monkeypatch)
    settings = get_settings()
    assert settings.jwt_secret == "change-me-in-production"  # noqa: S105
    assert settings.internal_service_key == "dev-internal-key"  # noqa: S105


def test_unset_run_mode_encryption_uses_dev_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUN_MODE", raising=False)
    monkeypatch.delenv("OAUTH_ENC_KEY", raising=False)
    # round-trips with the in-source dev key, key-free
    assert encryption.decrypt(encryption.encrypt("hello")) == "hello"
