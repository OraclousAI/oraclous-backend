"""Unit tests for the provider/scope/error domain (S2). No I/O."""

from __future__ import annotations

import pytest
from oraclous_credential_broker_service.domain.errors import OAuthErrorCode
from oraclous_credential_broker_service.domain.providers import (
    SUPPORTED_PROVIDERS,
    data_sources_for,
    is_supported,
    required_scopes_for,
)
from oraclous_credential_broker_service.domain.scopes import has_required, missing_scopes

pytestmark = pytest.mark.unit


def test_supported_providers() -> None:
    assert set(SUPPORTED_PROVIDERS) == {"google", "notion", "github"}
    assert is_supported("google") and not is_supported("dropbox")


def test_data_sources_and_scopes() -> None:
    assert set(data_sources_for("google")) == {"drive", "docs", "sheets"}
    assert required_scopes_for("google", "drive") == [
        "https://www.googleapis.com/auth/drive.readonly"
    ]
    # union across all of a provider's data sources
    google_union = required_scopes_for("google")
    assert "https://www.googleapis.com/auth/documents.readonly" in google_union
    assert required_scopes_for("notion", "pages") == []  # notion uses workspace permissions
    assert required_scopes_for("unknown") == []


def test_scope_subset() -> None:
    assert missing_scopes(["a", "b"], ["a"]) == ["b"]
    assert has_required(["repo"], ["repo", "read:user"])
    assert not has_required(["repo"], ["read:user"])
    assert has_required([], None) and has_required(None, None)


def test_error_codes() -> None:
    assert OAuthErrorCode.INSUFFICIENT_SCOPES.value == "oauth_insufficient_scopes"
    assert OAuthErrorCode.TOKEN_EXPIRED.value == "oauth_token_expired"
