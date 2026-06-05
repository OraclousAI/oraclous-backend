"""Unit: public allow-list policy + forward header injection/stripping (anti-spoof)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_application_gateway_service.domain.auth_policy import is_public
from oraclous_application_gateway_service.services.proxy_service import forward_request_headers
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit


def test_is_public_allow_list() -> None:
    assert is_public("/v1/auth/login")
    assert is_public("/v1/auth")
    assert is_public("/oauth/google/callback")
    assert not is_public("/v1/search")
    assert not is_public("/api/v1/tools")
    assert not is_public("/v1/authz")  # boundary: not under /v1/auth


def _principal() -> Principal:
    return Principal(
        principal_id=uuid.UUID("00000000-0000-0000-0000-0000000000e6"),
        principal_type=PrincipalType.USER,
        organisation_id=uuid.UUID("00000000-0000-0000-0000-00000000050a"),
    )


def _names(out: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {k.decode().lower(): v.decode() for k, v in out}


def test_authenticated_injects_identity_and_strips_spoof() -> None:
    raw = [
        (b"host", b"gw.test"),
        (b"authorization", b"Bearer tok"),
        (b"x-principal-id", b"FORGED"),
        (b"x-organisation-id", b"FORGED-ORG"),
        (b"x-internal-key", b"FORGED-KEY"),
        (b"connection", b"keep-alive"),
        (b"content-type", b"application/json"),
    ]
    out = _names(forward_request_headers(raw, _principal(), internal_key="ik-test"))
    assert "host" not in out  # httpx sets it for the upstream
    assert "connection" not in out  # hop-by-hop dropped
    assert out["authorization"] == "Bearer tok"  # bearer kept (defense-in-depth)
    assert out["content-type"] == "application/json"
    # forged identity replaced by the verified principal
    assert out["x-principal-id"] == "00000000-0000-0000-0000-0000000000e6"
    assert out["x-principal-type"] == "user"
    assert out["x-organisation-id"] == "00000000-0000-0000-0000-00000000050a"
    # forged internal key replaced by the gateway's attestation
    assert out["x-internal-key"] == "ik-test"


def test_public_strips_principal_but_passes_org_hint() -> None:
    raw = [
        (b"x-principal-id", b"FORGED"),
        (b"x-organisation-id", b"hint-org"),
    ]
    out = _names(forward_request_headers(raw, None, internal_key="ik-test"))
    assert "x-principal-id" not in out  # forged principal always stripped
    assert out["x-organisation-id"] == "hint-org"  # login multi-org hint passes through
    assert out["x-internal-key"] == "ik-test"  # attestation injected even on public paths
