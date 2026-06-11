"""Settings env parsing — the JSON-dict fields must tolerate a blank override.

Regression: compose passes ``HARNESS_OHM_TRUST_KEYS: "${HARNESS_OHM_TRUST_KEYS:-}"`` → an empty
string, which pydantic-settings cannot JSON-decode into a ``dict`` — it crashed the service at boot.
"""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.core.config import Settings

pytestmark = pytest.mark.unit


def test_blank_trust_keys_env_is_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_OHM_TRUST_KEYS", "")  # the compose `${VAR:-}` case
    assert Settings().ohm_trust_keys == {}


def test_json_trust_keys_env_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_OHM_TRUST_KEYS", '{"signer-1": "PEM"}')
    assert Settings().ohm_trust_keys == {"signer-1": "PEM"}


def test_unset_trust_keys_defaults_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_OHM_TRUST_KEYS", raising=False)
    assert Settings().ohm_trust_keys == {}


def test_blank_base_urls_falls_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_LLM_BASE_URLS", "")
    base = Settings().llm_base_urls
    assert base["openrouter"].startswith("https://openrouter.ai")
    assert "openai" in base


def test_allow_private_llm_targets_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_ALLOW_PRIVATE_LLM_TARGETS", raising=False)
    assert Settings().allow_private_llm_targets is True


def test_allow_private_llm_targets_env_overrides_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_LLM_TARGETS", "false")
    assert Settings().allow_private_llm_targets is False
