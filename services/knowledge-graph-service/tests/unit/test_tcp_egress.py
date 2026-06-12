"""TCP egress guard unit tests (#307, ORAA-4 §22 failure modes — the DECIDED Option B check).

BLOCK in multi-tenant mode: 169.254.169.254 (cloud metadata) / 127.0.0.1 (loopback) / 10.x
(RFC-1918) / a bare `postgres` single-label / `localhost` / `metadata.google.internal`. ALLOW a
public IP / host. In single-tenant `allow_private` mode, ALLOW 192.168.x (private). An unresolvable
host fail-closes. The cloud-metadata range is blocked in EITHER mode.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.domain import tcp_egress
from oraclous_knowledge_graph_service.domain.tcp_egress import (
    EgressBlockedError,
    validate_db_host,
)

pytestmark = pytest.mark.unit


# --- literal IPs: classified directly, no DNS --------------------------------
def test_block_cloud_metadata_in_both_modes() -> None:
    for allow in (False, True):
        with pytest.raises(EgressBlockedError, match="link-local/metadata"):
            validate_db_host("169.254.169.254", allow_private=allow)


def test_block_loopback_multitenant() -> None:
    with pytest.raises(EgressBlockedError, match="private/loopback"):
        validate_db_host("127.0.0.1", allow_private=False)


def test_block_rfc1918_10_multitenant() -> None:
    with pytest.raises(EgressBlockedError, match="private/loopback"):
        validate_db_host("10.1.2.3", allow_private=False)


def test_allow_public_literal_ip_returns_it() -> None:
    # A public literal IP is allowed and returned as the pinned address.
    assert validate_db_host("93.184.216.34", allow_private=False) == "93.184.216.34"


def test_single_tenant_allows_private_192_168() -> None:
    # allow_private (single-tenant/dev) lets a user ingest from a local/internal DB.
    assert validate_db_host("192.168.1.50", allow_private=True) == "192.168.1.50"


def test_single_tenant_allows_loopback_but_still_blocks_metadata() -> None:
    assert validate_db_host("127.0.0.1", allow_private=True) == "127.0.0.1"
    with pytest.raises(EgressBlockedError):
        validate_db_host("169.254.169.254", allow_private=True)


def test_ipv4_mapped_ipv6_metadata_is_normalized_and_blocked() -> None:
    # ::ffff:169.254.169.254 must route to the IPv4 metadata address and be blocked.
    with pytest.raises(EgressBlockedError, match="link-local/metadata"):
        validate_db_host("::ffff:169.254.169.254", allow_private=False)


# --- hostname rules (multi-tenant) -------------------------------------------
def test_block_localhost_name_multitenant() -> None:
    with pytest.raises(EgressBlockedError, match="not allowed"):
        validate_db_host("localhost", allow_private=False)


def test_block_metadata_google_internal_multitenant() -> None:
    # `metadata.google.internal` matches BOTH the blocked-name set AND the `.internal` suffix;
    # either rejection is correct.
    with pytest.raises(EgressBlockedError):
        validate_db_host("metadata.google.internal", allow_private=False)


def test_block_bare_single_label_postgres_multitenant() -> None:
    with pytest.raises(EgressBlockedError, match="single-label"):
        validate_db_host("postgres", allow_private=False)


def test_block_internal_suffix_multitenant() -> None:
    with pytest.raises(EgressBlockedError, match="internal suffix"):
        validate_db_host("db.cluster.local", allow_private=False)


def test_single_tenant_allows_bare_postgres_container() -> None:
    # In single-tenant mode the bare `postgres` container name is allowed; it resolves locally.
    def _fake_resolve(host: str):
        import ipaddress

        return [ipaddress.ip_address("172.18.0.2")]  # a docker-network private IP

    # allow_private relaxes the private-IP block too, so the resolved private IP is allowed.
    monkeypatched = _fake_resolve
    orig = tcp_egress._resolve_ips
    tcp_egress._resolve_ips = monkeypatched  # type: ignore[assignment]
    try:
        assert validate_db_host("postgres", allow_private=True) == "172.18.0.2"
    finally:
        tcp_egress._resolve_ips = orig  # type: ignore[assignment]


# --- DNS resolution re-check + fail-closed -----------------------------------
def test_public_name_resolving_inward_is_blocked(monkeypatch) -> None:
    import ipaddress

    monkeypatch.setattr(tcp_egress, "_resolve_ips", lambda host: [ipaddress.ip_address("10.0.0.5")])
    with pytest.raises(EgressBlockedError, match="resolves to a private/loopback"):
        validate_db_host("evil.example.com", allow_private=False)


def test_public_name_resolving_public_is_allowed_and_pinned(monkeypatch) -> None:
    import ipaddress

    monkeypatch.setattr(
        tcp_egress, "_resolve_ips", lambda host: [ipaddress.ip_address("93.184.216.34")]
    )
    assert validate_db_host("db.example.com", allow_private=False) == "93.184.216.34"


def test_unresolvable_host_fails_closed() -> None:
    # A genuinely unresolvable FQDN raises (fail-closed) rather than being treated as safe.
    with pytest.raises(EgressBlockedError, match="cannot resolve|no usable address"):
        validate_db_host("nonexistent-host-307.example.invalid", allow_private=False)


def test_empty_host_rejected() -> None:
    with pytest.raises(EgressBlockedError, match="empty"):
        validate_db_host("", allow_private=True)
