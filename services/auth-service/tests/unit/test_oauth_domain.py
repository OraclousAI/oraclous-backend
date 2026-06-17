"""Unit tests for OAuth domain + encryption (PKCE, state, scope merge, AES-256-GCM). No I/O."""

from __future__ import annotations

import base64
import hashlib

import pytest
from oraclous_auth_service.core.encryption import decrypt, encrypt
from oraclous_auth_service.domain.oauth import (
    generate_pkce,
    generate_state,
    is_allowed_redirect_uri,
    merge_scopes,
)

pytestmark = pytest.mark.unit


def test_encryption_round_trips_and_is_nondeterministic() -> None:
    ct1 = encrypt("provider-refresh-token")
    ct2 = encrypt("provider-refresh-token")
    assert ct1 != "provider-refresh-token"  # ciphertext, not plaintext (T-OAUTH-PLAINTEXT)
    assert ct1 != ct2  # fresh nonce each time
    assert decrypt(ct1) == "provider-refresh-token"
    assert decrypt(ct2) == "provider-refresh-token"


def test_encryption_empty() -> None:
    assert encrypt("") == ""
    assert decrypt("") == ""


def test_pkce_s256_challenge_matches_verifier() -> None:
    verifier, challenge = generate_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    assert generate_pkce()[0] != verifier  # fresh each time


def test_state_is_unguessable() -> None:
    s1, s2 = generate_state(), generate_state()
    assert s1 != s2 and len(s1) >= 40


def test_merge_scopes_union_order_stable() -> None:
    assert merge_scopes(["a", "b"], ["b", "c"]) == ["a", "b", "c"]
    assert merge_scopes(None, ["x"]) == ["x"]
    assert merge_scopes(["x"], None) == ["x"]
    assert merge_scopes(None, None) == []


# --- WP-11: redirect_uri allow-list (T-OAUTH open-redirect) -----------------


@pytest.mark.security
def test_redirect_uri_allowlist_exact_match_when_list_present() -> None:
    """A non-empty allow-list is enforced by EXACT match: a listed URI passes, an unlisted one
    (including a prefix/substring of a listed one) is rejected — no open-redirect via prefix."""
    allow = ("https://app.example/oauth/callback", "https://app.example/connect/cb")
    # Allowlisted → permitted (the empty-case flag is irrelevant when a list is present).
    assert is_allowed_redirect_uri(
        "https://app.example/oauth/callback", allow, permissive_when_empty=True
    )
    # Not on the list → rejected, even though dev would be permissive for an EMPTY list.
    assert not is_allowed_redirect_uri(
        "https://evil.example/steal", allow, permissive_when_empty=True
    )
    # Prefix / open-redirect attempts must NOT pass an exact-match list.
    assert not is_allowed_redirect_uri(
        "https://app.example/oauth/callback/../../evil", allow, permissive_when_empty=True
    )
    assert not is_allowed_redirect_uri(
        "https://app.example.evil.com/oauth/callback", allow, permissive_when_empty=True
    )


@pytest.mark.security
def test_redirect_uri_empty_list_policy_is_run_mode_gated() -> None:
    """An EMPTY allow-list is permissive in dev (``permissive_when_empty=True`` → allow any) and
    fail-closed in prod (``permissive_when_empty=False`` → deny every redirect)."""
    assert is_allowed_redirect_uri("https://anything/cb", (), permissive_when_empty=True)
    assert not is_allowed_redirect_uri("https://anything/cb", (), permissive_when_empty=False)
