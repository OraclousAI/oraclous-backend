"""Unit: FakeCredentialBroker resolves each credential type deterministically (key-free)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_capability_registry_service.services.credential_client import (
    CredentialResolutionError,
    FakeCredentialBroker,
)

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


async def _resolve(req: dict):
    broker = FakeCredentialBroker(fake_db_dsn="postgresql://x/y")
    return await broker.resolve(organisation_id=_ORG, user_id=_USER, requirement=req)


async def test_connection_string_returns_the_fake_dsn() -> None:
    out = await _resolve({"type": "connection_string", "provider": "postgresql"})
    assert out.payload == {"connection_string": "postgresql://x/y"}


async def test_oauth_token_returns_access_token_and_scopes() -> None:
    out = await _resolve({"type": "oauth_token", "provider": "google", "scopes": ["s1"]})
    assert out.payload["access_token"] == "fake-google-access-token"  # noqa: S105 — fake test token
    assert out.payload["scopes"] == ["s1"]


async def test_api_key_returns_a_key() -> None:
    out = await _resolve({"type": "api_key", "provider": "notion"})
    assert out.payload == {"api_key": "fake-notion-api-key"}


async def test_unknown_type_raises() -> None:
    with pytest.raises(CredentialResolutionError):
        await _resolve({"type": "wat"})
