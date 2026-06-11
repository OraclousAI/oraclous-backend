"""Egress guard for user-supplied LLM base URLs (SSRF). Domain-pure; no network.

The link-local / cloud-metadata range is blocked in BOTH modes; private/loopback are blocked only
when ``allow_private`` is False (multi-tenant). DNS is monkeypatched so the tests are hermetic.
"""

from __future__ import annotations

import socket

import pytest
from oraclous_harness_runtime_service.domain.llm.egress import (
    EgressBlockedError,
    validate_outbound_url,
)

pytestmark = pytest.mark.unit


def _patch_resolution(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    """Map a hostname → a single IP via getaddrinfo, so no real DNS is hit."""

    def fake_getaddrinfo(host, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        ip = mapping.get(host)
        if ip is None:
            raise OSError(f"no fake address for {host!r}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


# --- ALWAYS blocked: link-local / cloud metadata (169.254.0.0/16) ---


@pytest.mark.parametrize("allow_private", [True, False])
def test_blocks_metadata_ip_literal_in_both_modes(allow_private: bool) -> None:
    with pytest.raises(EgressBlockedError):
        validate_outbound_url("http://169.254.169.254/v1", allow_private=allow_private)


@pytest.mark.parametrize("allow_private", [True, False])
@pytest.mark.parametrize("ip", ["169.254.0.1", "169.254.255.254", "169.254.169.254"])
def test_blocks_link_local_range_in_both_modes(allow_private: bool, ip: str) -> None:
    with pytest.raises(EgressBlockedError):
        validate_outbound_url(f"http://{ip}/v1", allow_private=allow_private)


@pytest.mark.parametrize("allow_private", [True, False])
def test_blocks_host_resolving_to_metadata_in_both_modes(
    monkeypatch: pytest.MonkeyPatch, allow_private: bool
) -> None:
    _patch_resolution(monkeypatch, {"evil.example": "169.254.169.254"})
    with pytest.raises(EgressBlockedError):
        validate_outbound_url("https://evil.example/v1", allow_private=allow_private)


# --- public hosts: allowed in either mode ---


@pytest.mark.parametrize("allow_private", [True, False])
def test_allows_public_host(monkeypatch: pytest.MonkeyPatch, allow_private: bool) -> None:
    # A genuinely globally-reachable address (TEST-NET docs ranges are flagged is_private).
    _patch_resolution(monkeypatch, {"my-endpoint.example": "93.184.216.34"})
    validate_outbound_url("https://my-endpoint.example/v1", allow_private=allow_private)


@pytest.mark.parametrize("allow_private", [True, False])
def test_allows_public_ip_literal(allow_private: bool) -> None:
    validate_outbound_url("https://8.8.8.8/v1", allow_private=allow_private)


# --- private/loopback: allowed when allow_private=True, blocked when False ---


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://192.168.1.50/v1",
        "http://10.0.0.5/v1",
        "http://172.16.0.9/v1",
    ],
)
def test_allows_private_ip_when_allow_private(url: str) -> None:
    validate_outbound_url(url, allow_private=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://192.168.1.50/v1",
        "http://10.0.0.5/v1",
        "http://172.16.0.9/v1",
    ],
)
def test_blocks_private_ip_when_not_allow_private(url: str) -> None:
    with pytest.raises(EgressBlockedError):
        validate_outbound_url(url, allow_private=False)


def test_allows_host_docker_internal_when_allow_private(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolution(monkeypatch, {"host.docker.internal": "192.168.65.2"})
    validate_outbound_url("http://host.docker.internal:11434/v1", allow_private=True)


def test_blocks_host_docker_internal_when_not_allow_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, {"host.docker.internal": "192.168.65.2"})
    with pytest.raises(EgressBlockedError):
        validate_outbound_url("http://host.docker.internal:11434/v1", allow_private=False)


def test_allows_localhost_when_allow_private() -> None:
    # `localhost` resolves to loopback; allowed in the trusted single-tenant default.
    validate_outbound_url("http://localhost:11434/v1", allow_private=True)


def test_blocks_localhost_literal_when_not_allow_private() -> None:
    with pytest.raises(EgressBlockedError):
        validate_outbound_url("http://localhost:11434/v1", allow_private=False)


# --- malformed URLs: rejected regardless of mode ---


@pytest.mark.parametrize("allow_private", [True, False])
def test_rejects_non_http_scheme(allow_private: bool) -> None:
    with pytest.raises(EgressBlockedError):
        validate_outbound_url("ftp://example.com/v1", allow_private=allow_private)


@pytest.mark.parametrize("allow_private", [True, False])
def test_rejects_url_with_no_host(allow_private: bool) -> None:
    with pytest.raises(EgressBlockedError):
        validate_outbound_url("http:///v1", allow_private=allow_private)


@pytest.mark.parametrize("allow_private", [True, False])
def test_rejects_unresolvable_host(monkeypatch: pytest.MonkeyPatch, allow_private: bool) -> None:
    _patch_resolution(monkeypatch, {})  # any lookup raises OSError → blocked
    with pytest.raises(EgressBlockedError):
        validate_outbound_url("https://does-not-resolve.example/v1", allow_private=allow_private)


def test_ipv4_mapped_ipv6_metadata_is_blocked() -> None:
    # ::ffff:169.254.169.254 routes to the IPv4 metadata IP — blocked even in allow_private mode.
    with pytest.raises(EgressBlockedError):
        validate_outbound_url("http://[::ffff:169.254.169.254]/v1", allow_private=True)
