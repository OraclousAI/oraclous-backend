"""Unit: RealCredentialBroker speaks the broker's /internal contract (mocked transport).

Proves oauth_token resolves via /internal/runtime-token, non-OAuth resolves the decrypted payload
via /internal/resolve-credential (mapped credential_id + X-Internal-Key), and failures map to
CredentialResolutionError.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from oraclous_capability_registry_service.services.credential_client import (
    CredentialResolutionError,
    RealCredentialBroker,
)

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _broker(handler) -> RealCredentialBroker:
    return RealCredentialBroker(
        base_url="http://broker", internal_key="k", transport=httpx.MockTransport(handler)
    )


async def test_oauth_resolves_via_runtime_token() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["key"] = req.headers.get("X-Internal-Key")
        return httpx.Response(200, json={"success": True, "access_token": "tok", "scopes": ["s"]})

    out = await _broker(handler).resolve(
        organisation_id=_ORG,
        user_id=_USER,
        requirement={"type": "oauth_token", "provider": "google", "scopes": ["s"]},
    )
    assert seen["path"] == "/internal/runtime-token"
    assert seen["key"] == "k"
    assert out.payload["access_token"] == "tok"  # noqa: S105 — fake test token


async def test_oauth_insufficient_scopes_raises_with_login_url() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": False,
                "error_code": "oauth_insufficient_scopes",
                "login_url": "https://login",
                "missing_scopes": ["x"],
            },
        )

    with pytest.raises(CredentialResolutionError) as ei:
        await _broker(handler).resolve(
            organisation_id=_ORG,
            user_id=_USER,
            requirement={"type": "oauth_token", "provider": "google", "scopes": ["x"]},
        )
    assert ei.value.error_code == "oauth_insufficient_scopes"
    assert ei.value.login_url == "https://login"


async def test_connection_string_resolves_via_resolve_credential() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(
            200,
            json={
                "credential_id": str(uuid.uuid4()),
                "provider": "postgresql",
                "cred_type": "raw",
                "credential": {"connection_string": "postgresql://h/db"},
            },
        )

    out = await _broker(handler).resolve(
        organisation_id=_ORG,
        user_id=_USER,
        requirement={"type": "connection_string", "provider": "postgresql"},
        credential_id="cred-1",
    )
    assert seen["path"] == "/internal/resolve-credential"
    assert out.payload == {"connection_string": "postgresql://h/db"}


async def test_non_oauth_without_mapped_id_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(CredentialResolutionError) as ei:
        await _broker(handler).resolve(
            organisation_id=_ORG,
            user_id=_USER,
            requirement={"type": "connection_string", "provider": "postgresql"},
            credential_id=None,
        )
    assert ei.value.error_code == "credential_not_mapped"


async def test_unknown_credential_404_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "credential not found"})

    with pytest.raises(CredentialResolutionError) as ei:
        await _broker(handler).resolve(
            organisation_id=_ORG,
            user_id=_USER,
            requirement={"type": "connection_string"},
            credential_id="missing",
        )
    assert ei.value.error_code == "credential_not_found"
