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


# --- Round-2 adversarial SSRF regression guards (#329) ------------------------
def test_default_posture_blocks_rfc1918() -> None:
    # The NEW secure default (allow_private=False) must block RFC-1918 — proves the floor is ON when
    # the flag is unset (the config default was flipped from True → False, ADR-025 §1).
    with pytest.raises(EgressBlockedError, match="private/loopback"):
        validate_db_host("10.0.0.5", allow_private=False)


@pytest.mark.parametrize("encoded", ["2130706433", "0x7f000001"])
def test_decimal_hex_ip_encodings_blocked_default_posture(encoded) -> None:
    # Alternate integer/hex encodings of 127.0.0.1 are NOT valid ipaddress literals, so they are not
    # classified as a (public) literal IP. In the default multi-tenant posture they are rejected as
    # a single-label/bare name before any connect — never silently treated as a public host.
    with pytest.raises(EgressBlockedError):
        validate_db_host(encoded, allow_private=False)


def test_encoded_loopback_blocked_when_it_reaches_resolution(monkeypatch) -> None:
    # If an encoded/odd form DOES reach the resolver (e.g. single-label relaxed) and the resolver
    # expands it to loopback, the resolved-IP re-check still blocks it in multi-tenant mode — the
    # guard never trusts the name, only the resolved address.
    import ipaddress

    monkeypatch.setattr(
        tcp_egress, "_resolve_ips", lambda host: [ipaddress.ip_address("127.0.0.1")]
    )
    with pytest.raises(EgressBlockedError, match="resolves to a private/loopback"):
        validate_db_host("weird.example.com", allow_private=False)


def test_mixed_public_private_a_records_are_blocked(monkeypatch) -> None:
    # A hostname whose A-record SET mixes a public and a private IP must be blocked (the guard
    # re-checks EVERY resolved IP, not just the first) — a DNS-rebinding / multi-A SSRF attempt.
    import ipaddress

    monkeypatch.setattr(
        tcp_egress,
        "_resolve_ips",
        lambda host: [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("10.0.0.7")],
    )
    with pytest.raises(EgressBlockedError, match="resolves to a private/loopback"):
        validate_db_host("rebind.example.com", allow_private=False)


def test_ipv6_link_local_literal_blocked_both_modes() -> None:
    for allow in (False, True):
        with pytest.raises(EgressBlockedError, match="link-local/metadata"):
            validate_db_host("fe80::1", allow_private=allow)


def test_ipv4_mapped_private_is_normalized_and_blocked() -> None:
    # ::ffff:10.0.0.1 must route to the IPv4 private address and be blocked in the default posture.
    with pytest.raises(EgressBlockedError, match="private/loopback"):
        validate_db_host("::ffff:10.0.0.1", allow_private=False)


def test_unspecified_addresses_blocked() -> None:
    # 0.0.0.0 / :: are unspecified (route to "all interfaces") — never a valid external DB target;
    # blocked in the default posture (allow_private only relaxes private/loopback, which they are).
    for addr in ("0.0.0.0", "::"):  # noqa: S104 — adversarial test inputs, not a bind address
        with pytest.raises(EgressBlockedError, match="private/loopback"):
            validate_db_host(addr, allow_private=False)


def test_ipv6_imds_fd00_ec2_blocked_in_both_modes() -> None:
    # The AWS IMDS-over-IPv6 endpoint fd00:ec2::254 is a ULA — only the private block catches it,
    # which allow_private relaxes. The dedicated metadata floor blocks it in EITHER mode.
    for allow in (False, True):
        with pytest.raises(EgressBlockedError, match="link-local/metadata"):
            validate_db_host("fd00:ec2::254", allow_private=allow)


def test_trailing_dot_fqdn_blocked_name_not_bypassed() -> None:
    # A fully-qualified trailing-dot name must still hit the blocked-name / suffix / single-label
    # rules (it resolves to the same target). Each form is blocked in multi-tenant mode.
    with pytest.raises(EgressBlockedError, match="not allowed"):
        validate_db_host("localhost.", allow_private=False)
    with pytest.raises(EgressBlockedError):
        validate_db_host("metadata.google.internal.", allow_private=False)
    with pytest.raises(EgressBlockedError, match="single-label"):
        validate_db_host("postgres.", allow_private=False)
