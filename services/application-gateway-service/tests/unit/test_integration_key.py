"""Unit: the integration-key crypto-shape helpers (mint / prefix / constant-time verify)."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.domain.integration_key import (
    PREFIX_LEN,
    hash_token,
    is_integration_key,
    mint_key,
    prefix_of,
    verify_key,
)

pytestmark = pytest.mark.unit


def test_mint_round_trips() -> None:
    minted = mint_key("oak")
    assert minted.plaintext.startswith("oak-")
    assert minted.key_prefix == prefix_of(minted.plaintext)
    assert len(minted.key_prefix) == PREFIX_LEN
    assert minted.key_hash == hash_token(minted.plaintext)
    # the stored hash verifies the plaintext, and only the plaintext
    assert verify_key(minted.plaintext, minted.key_hash) is True
    assert verify_key(minted.plaintext + "x", minted.key_hash) is False


def test_mint_is_unique() -> None:
    a, b = mint_key(), mint_key()
    assert a.key_prefix != b.key_prefix
    assert a.key_hash != b.key_hash


def test_is_integration_key() -> None:
    assert is_integration_key("oak-abc")
    assert is_integration_key("oag-abc")
    assert not is_integration_key("eyJ...jwt")
    assert not is_integration_key("dev-token")  # the dev bearer is NOT an integration key


def test_prefix_of_handles_malformed() -> None:
    assert prefix_of("not-a-key") is None
    assert prefix_of("oak-tooshort") is None  # prefix shorter than PREFIX_LEN
    assert prefix_of(mint_key().plaintext) is not None


def test_invalid_scheme_rejected() -> None:
    with pytest.raises(ValueError, match="scheme"):
        mint_key("xxx")
