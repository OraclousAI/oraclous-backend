"""Unit tests for the invitation token domain: gen / hash / constant-time match / expiry. No I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from oraclous_auth_service.domain.invitations import (
    generate_invitation_token,
    hash_token,
    is_expired,
    token_matches,
    token_prefix,
)

pytestmark = pytest.mark.unit


def test_generate_returns_consistent_prefix_and_hash() -> None:
    raw, prefix, h = generate_invitation_token()
    assert prefix == raw[:12]
    assert h == hash_token(raw)
    assert h != raw  # only the hash is stored, never the raw token


def test_tokens_are_unique() -> None:
    assert generate_invitation_token()[0] != generate_invitation_token()[0]


def test_token_matches_is_constant_time_correct() -> None:
    raw, _, h = generate_invitation_token()
    assert token_matches(raw, h)
    assert not token_matches(raw + "x", h)
    assert not token_matches("totally-wrong", h)


def test_prefix_helper() -> None:
    raw, prefix, _ = generate_invitation_token()
    assert token_prefix(raw) == prefix


def test_is_expired() -> None:
    assert is_expired(datetime.now(UTC) - timedelta(seconds=1))
    assert not is_expired(datetime.now(UTC) + timedelta(days=1))
    assert not is_expired(None)
