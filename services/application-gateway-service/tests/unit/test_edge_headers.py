"""Unit: public allow-list policy + forward/response header hardening (anti-spoof, R7-SEC S1)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_application_gateway_service.domain.auth_policy import is_public
from oraclous_application_gateway_service.services.proxy_service import (
    forward_request_headers,
    response_headers,
)
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


def test_forward_injects_verified_org_role_and_strips_a_forged_one() -> None:
    # R7-SEC S2: the role is trust-asserted — a client forging a higher role is overwritten with the
    # verified principal's role (the gateway propagates it so an upstream may role-gate later).
    admin = Principal(
        principal_id=uuid.UUID("00000000-0000-0000-0000-0000000000e6"),
        principal_type=PrincipalType.USER,
        organisation_id=uuid.UUID("00000000-0000-0000-0000-00000000050a"),
        org_role="admin",
    )
    raw = [(b"x-principal-org-role", b"owner")]  # the client forges 'owner'
    out = _names(forward_request_headers(raw, admin, internal_key="ik"))
    assert out["x-principal-org-role"] == "admin"  # the verified role, never the forged 'owner'
    # a roleless principal injects no role header (None never reaches an upstream gate)
    out2 = _names(forward_request_headers([], _principal(), internal_key="ik"))
    assert "x-principal-org-role" not in out2


def test_a_forged_cross_org_assertion_can_never_set_the_upstream_tenant() -> None:
    # the cross-org leak (T1): a client forging another org's id must NOT reach the upstream — the
    # gateway always overwrites X-Organisation-Id with the VERIFIED principal's org.
    other_org = "00000000-0000-0000-0000-000000000bad"
    raw = [(b"x-organisation-id", other_org.encode()), (b"x-principal-id", b"victim")]
    out = _names(forward_request_headers(raw, _principal(), internal_key="ik"))
    assert (
        out["x-organisation-id"] == "00000000-0000-0000-0000-00000000050a"
    )  # verified, not forged
    assert out["x-principal-id"] == "00000000-0000-0000-0000-0000000000e6"


def test_response_headers_strip_fingerprinting_and_reflected_trust_headers() -> None:
    # anti-fingerprinting (ORAA-279) + a reflect-guard: an upstream response carrying
    # `server`/`x-powered-by` or echoing a trust header must never reach the client.
    raw = [
        (b"content-type", b"application/json"),
        (b"server", b"uvicorn"),
        (b"x-powered-by", b"flask"),
        (b"x-internal-key", b"leaked-attestation"),
        (b"x-organisation-id", b"leaked-org"),
        (b"x-principal-id", b"leaked-principal"),
        (b"x-custom-ok", b"keepme"),
        (b"transfer-encoding", b"chunked"),
    ]
    out = {k.lower(): v for k, v in response_headers(raw)}
    assert "server" not in out and "x-powered-by" not in out  # no fingerprint leak
    assert "x-internal-key" not in out  # never reflect the attestation
    assert "x-organisation-id" not in out and "x-principal-id" not in out  # never reflect trust
    assert "transfer-encoding" not in out  # hop-by-hop still dropped
    assert out["content-type"] == "application/json" and out["x-custom-ok"] == "keepme"  # kept
