"""Unit: the SSRF egress guard — pure URL/IP classification + the #492 pinned-IP gate (R6)."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from oraclous_capability_registry_service.domain import egress as egress_mod
from oraclous_capability_registry_service.domain.egress import (
    egress_allowed,
    is_private_ip,
    is_public_url,
    pinned_request,
)

pytestmark = pytest.mark.unit


def fake_dns(monkeypatch: pytest.MonkeyPatch, ips: list[str] | Exception) -> None:
    """Stub the guard's async DNS resolve: ``ips`` → resolved addresses; an Exception → raised."""

    class _Loop:
        async def getaddrinfo(self, host: str, port: object, **kw: object) -> list:
            if isinstance(ips, Exception):
                raise ips
            fam = lambda ip: socket.AF_INET6 if ":" in ip else socket.AF_INET  # noqa: E731
            return [(fam(ip), socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    monkeypatch.setattr(egress_mod, "asyncio", SimpleNamespace(get_running_loop=lambda: _Loop()))


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


# ── #492 (ADR-025 §1): egress_allowed returns the PINNED vetted IP — connect to THAT, not the name


async def test_egress_allowed_returns_the_pinned_ip_for_a_vetted_public_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_dns(monkeypatch, ["93.184.216.34"])
    assert await egress_allowed("https://mcp.example.com/rpc") == "93.184.216.34"


async def test_egress_allowed_is_none_when_the_name_resolves_inward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_dns(monkeypatch, ["10.0.0.5"])
    assert await egress_allowed("https://rebind.example.com/rpc") is None
    # ANY resolved private address blocks (ALL must be safe — a dual answer can't smuggle one in)
    fake_dns(monkeypatch, ["93.184.216.34", "10.0.0.5"])
    assert await egress_allowed("https://rebind.example.com/rpc") is None


async def test_egress_allowed_returns_a_literal_public_ip_without_a_dns_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_dns(monkeypatch, RuntimeError("DNS must not be consulted for a literal IP"))
    assert await egress_allowed("https://93.184.216.34/rpc") == "93.184.216.34"


async def test_egress_allowed_is_none_on_an_unresolvable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_dns(monkeypatch, socket.gaierror("nxdomain"))
    assert await egress_allowed("https://nope.example.com/") is None


async def test_egress_allowed_prefers_an_ipv4_pin_when_both_families_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the pin must be dialable everywhere — prefer the v4 address even when v6 sorts first.
    fake_dns(monkeypatch, ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"])
    assert await egress_allowed("https://example.com/") == "93.184.216.34"


def test_pinned_request_swaps_the_host_and_preserves_host_header_and_sni() -> None:
    url, headers, ext = pinned_request("https://api.partner.io:8443/mcp?x=1", "93.184.216.34")
    assert url.host == "93.184.216.34" and url.port == 8443 and url.path == "/mcp"
    assert headers["Host"] == "api.partner.io:8443"  # the ORIGINAL name (with its explicit port)
    assert ext == {"sni_hostname": "api.partner.io"}  # TLS SNI + cert validation target the name


def test_pinned_request_sends_no_sni_extension_for_plain_http() -> None:
    url, headers, ext = pinned_request("http://api.partner.io/mcp", "93.184.216.34")
    assert url.host == "93.184.216.34"
    assert headers["Host"] == "api.partner.io"  # default port → no :port suffix (as httpx sends)
    assert ext == {}
