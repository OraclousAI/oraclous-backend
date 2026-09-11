"""Unit: the pure edge-protection helpers — XFF trust boundary + Content-Length fast-path."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.domain.edge_protection import (
    client_ip,
    content_length_exceeds,
    is_exempt_client,
    is_malformed_path,
    is_rate_limit_exempt,
    parse_exempt_networks,
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


# ---- #850: the e2e overlay's client exemption ---------------------------------------------------


def test_exempt_networks_default_to_nothing() -> None:
    assert parse_exempt_networks("") == ()
    assert parse_exempt_networks(" , ,") == ()
    assert is_exempt_client("127.0.0.1", ()) is False  # nothing configured → nobody is exempt


def test_exempt_networks_parse_a_cidr_list_and_match_by_membership() -> None:
    networks = parse_exempt_networks("127.0.0.0/8, ::1/128,172.16.0.0/12")
    assert is_exempt_client("127.0.0.1", networks) is True
    assert is_exempt_client("::1", networks) is True
    assert is_exempt_client("172.21.0.1", networks) is True  # the docker bridge gateway
    assert is_exempt_client("8.8.8.8", networks) is False
    assert is_exempt_client("192.168.65.1", networks) is False  # not listed → still limited


def test_an_unparseable_or_missing_peer_is_never_exempt() -> None:
    networks = parse_exempt_networks("0.0.0.0/0")  # even the widest list
    assert is_exempt_client("", networks) is False
    assert is_exempt_client("not-an-ip", networks) is False


def test_a_malformed_cidr_fails_closed_at_parse_time() -> None:
    # a typo must surface at startup, never be skipped (and never widen the list)
    with pytest.raises(ValueError, match="EDGE_RATE_LIMIT_EXEMPT_CIDRS"):
        parse_exempt_networks("127.0.0.0/8,loopback")
