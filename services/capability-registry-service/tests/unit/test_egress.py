"""Unit: the SSRF egress guard — pure URL/IP classification (R6 MCP-client)."""

from __future__ import annotations

import pytest
from oraclous_capability_registry_service.domain.egress import is_private_ip, is_public_url

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example.com/rpc",  # a real external FQDN
        "http://api.partner.io:8443/mcp",
        "https://93.184.216.34/rpc",  # a literal PUBLIC ip
    ],
)
def test_public_urls_pass_the_structural_gate(url: str) -> None:
    assert is_public_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/rpc",  # loopback name
        "http://127.0.0.1/rpc",  # loopback ip
        "http://10.0.0.5/rpc",  # private
        "http://172.16.4.4/rpc",  # private
        "http://192.168.1.10/rpc",  # private
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local) — classic SSRF
        "http://[::1]/rpc",  # ipv6 loopback
        "http://credential-broker-service:8000/rpc",  # a single-label internal container name
        "http://postgres/rpc",
        "https://evil.internal/rpc",  # *.internal
        "https://thing.cluster.local/rpc",
        "ftp://example.com/rpc",  # non-http scheme
        "file:///etc/passwd",
        "not a url",
        "",
    ],
)
def test_internal_or_malformed_urls_are_blocked(url: str) -> None:
    assert is_public_url(url) is False


@pytest.mark.parametrize(
    ("ip", "private"),
    [
        ("8.8.8.8", False),
        ("93.184.216.34", False),
        ("127.0.0.1", True),
        ("10.1.2.3", True),
        ("169.254.169.254", True),
        ("::1", True),
        ("fc00::1", True),  # ipv6 unique-local
        ("not-an-ip", True),  # fail-closed
    ],
)
def test_is_private_ip(ip: str, private: bool) -> None:
    assert is_private_ip(ip) is private
