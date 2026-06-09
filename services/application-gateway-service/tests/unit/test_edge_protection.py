"""Unit: the pure edge-protection helpers — XFF trust boundary + Content-Length fast-path."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.domain.edge_protection import (
    client_ip,
    content_length_exceeds,
    is_malformed_path,
    is_rate_limit_exempt,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "path",
    [
        "/v1/tools/../../etc/passwd",  # a traversal segment
        "/v1/../admin",
        "/v1/tools\x00.json",  # NUL byte
        "/v1\\tools",  # backslash
    ],
)
def test_malformed_paths_are_rejected(path: str) -> None:
    assert is_malformed_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/v1/agents/my-agent/invoke",
        "/health",
        "/v1/tools",
        "/v1/a..b/x",  # `..` only INSIDE a segment is not traversal — must not false-positive
    ],
)
def test_legitimate_paths_pass(path: str) -> None:
    assert is_malformed_path(path) is False


def test_xff_ignored_at_default_trust_zero() -> None:
    # default (0 trusted proxies): XFF is attacker-controlled and ignored; key on the socket peer
    assert client_ip("9.9.9.9", "1.1.1.1, 2.2.2.2", trusted_proxy_count=0) == "9.9.9.9"
    assert client_ip("9.9.9.9", None, trusted_proxy_count=0) == "9.9.9.9"


def test_xff_count_from_the_right_with_trusted_proxies() -> None:
    chain = "client, proxyA, proxyB"  # our infra appended proxyA (left) ... proxyB (right-most)
    # 1 trusted hop: strip the right-most (proxyB), the client is the next from the right (proxyA)
    assert client_ip("10.0.0.9", chain, trusted_proxy_count=1) == "proxyA"
    # 2 trusted hops: the real client
    assert client_ip("10.0.0.9", chain, trusted_proxy_count=2) == "client"


def test_chain_shorter_than_trust_count_falls_back_to_peer() -> None:
    # never read the left-most / a spoofed value when the chain is too short
    assert client_ip("10.0.0.9", "only-one", trusted_proxy_count=3) == "10.0.0.9"


def test_missing_peer_is_empty_string() -> None:
    assert client_ip(None, None, trusted_proxy_count=0) == ""


def test_rate_limit_exempt_paths() -> None:
    assert is_rate_limit_exempt("/health") is True
    assert is_rate_limit_exempt("/health/upstreams") is True
    assert is_rate_limit_exempt("/v1/openapi.json") is True
    assert is_rate_limit_exempt("/docs") is True
    assert is_rate_limit_exempt("/v1/auth/login") is False
    assert is_rate_limit_exempt("/api/v1/tools") is False
    # boundary: a suffixed look-alike must NOT inherit the exemption (else it bypasses the limiter)
    assert is_rate_limit_exempt("/healthz") is False
    assert is_rate_limit_exempt("/docs-evil") is False
    assert is_rate_limit_exempt("/v1/openapi.json.x") is False


def test_content_length_exceeds() -> None:
    assert content_length_exceeds("1048577", 1048576) is True
    assert content_length_exceeds("1048576", 1048576) is False  # equal is within the cap
    assert content_length_exceeds("0", 10) is False
    assert content_length_exceeds(None, 10) is False  # missing -> defer to the byte counter
    assert content_length_exceeds("not-a-number", 10) is False  # unparseable -> defer
