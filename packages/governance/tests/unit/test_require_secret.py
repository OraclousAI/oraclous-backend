"""Unit tests for the fail-closed secret helper (WP-1, T6 / ADR-008)."""

from __future__ import annotations

import pytest
from oraclous_governance import MissingSecretError, is_prod, require_secret, run_mode

pytestmark = [pytest.mark.unit, pytest.mark.security]


def test_unset_run_mode_defaults_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUN_MODE", raising=False)
    assert run_mode() == "dev"
    assert is_prod() is False


def test_run_mode_prod_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    assert run_mode() == "prod"
    assert is_prod() is True


def test_run_mode_is_case_insensitive_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "  PROD ")
    assert is_prod() is True


def test_unset_run_mode_returns_dev_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUN_MODE", raising=False)
    monkeypatch.delenv("MY_SECRET", raising=False)
    assert require_secret("MY_SECRET", dev_default="dev-fallback") == "dev-fallback"


def test_dev_mode_returns_dev_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "dev")
    monkeypatch.delenv("MY_SECRET", raising=False)
    assert require_secret("MY_SECRET", dev_default="dev-fallback") == "dev-fallback"


def test_dev_mode_empty_string_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "dev")
    monkeypatch.setenv("MY_SECRET", "")
    assert require_secret("MY_SECRET", dev_default="dev-fallback") == "dev-fallback"


def test_explicit_value_wins_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUN_MODE", raising=False)
    monkeypatch.setenv("MY_SECRET", "real-value")
    assert require_secret("MY_SECRET", dev_default="dev-fallback") == "real-value"


def test_explicit_value_wins_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    monkeypatch.setenv("MY_SECRET", "real-value")
    assert require_secret("MY_SECRET", dev_default="dev-fallback") == "real-value"


def test_prod_missing_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_MODE", "prod")
    monkeypatch.delenv("MY_SECRET", raising=False)
    with pytest.raises(MissingSecretError):
        require_secret("MY_SECRET", dev_default="dev-fallback")


def test_prod_empty_string_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Covers the `os.environ.get(name) or _DEV_KEY` bug: an empty prod env var must NOT
    # silently fall back to the dev default.
    monkeypatch.setenv("RUN_MODE", "prod")
    monkeypatch.setenv("MY_SECRET", "")
    with pytest.raises(MissingSecretError):
        require_secret("MY_SECRET", dev_default="dev-fallback")
