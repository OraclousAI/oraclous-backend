"""Unit tests for AES-256-GCM credential encryption (S0). No DB."""

from __future__ import annotations

import base64
import json

import pytest

pytestmark = pytest.mark.unit

_DEV_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # noqa: S105 — 32-byte test key


@pytest.fixture(autouse=True)
def _enc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", _DEV_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "k")
    from oraclous_credential_broker_service.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_encrypt_decrypt_round_trips_dict() -> None:
    from oraclous_credential_broker_service.core.security import decrypt_secret, encrypt_secret

    secret = {"access_token": "abc", "refresh_token": "def", "scopes": ["email"]}
    ct = encrypt_secret(secret)
    # ciphertext is hex and does NOT contain the plaintext JSON
    assert json.dumps(secret) not in ct
    assert "access_token" not in ct
    assert decrypt_secret(ct) == secret


def test_nonce_is_fresh_each_time() -> None:
    from oraclous_credential_broker_service.core.security import encrypt_secret

    assert encrypt_secret("same") != encrypt_secret("same")


def test_wrong_key_length_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from oraclous_credential_broker_service.core.config import get_settings
    from oraclous_credential_broker_service.core.security import encrypt_secret

    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(b"tooshort").decode())
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="32 bytes"):
        encrypt_secret("x")
    get_settings.cache_clear()
