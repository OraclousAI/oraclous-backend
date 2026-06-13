"""Unit: route table longest-prefix match + collision disambiguation + config build."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.core.config import Settings
from oraclous_application_gateway_service.domain.route_table import (
    RouteEntry,
    RouteTable,
    build_route_table,
)

pytestmark = pytest.mark.unit


def _table() -> RouteTable:
    return RouteTable(
        [
            RouteEntry("/api/v1/graphs", "http://kgs:8000"),
            RouteEntry("/api/v1/capabilities", "http://capreg:8000"),
            RouteEntry("/v1/search", "http://krs:8000"),
        ]
    )


def test_exact_prefix_resolves() -> None:
    assert _table().resolve("/v1/search").upstream_url == "http://krs:8000"


def test_subpath_resolves_to_the_prefix_upstream() -> None:
    # /api/v1/graphs/{id}/ontology must route to KGS, not collide with capabilities
    assert _table().resolve("/api/v1/graphs/abc/ontology").upstream_url == "http://kgs:8000"
    assert _table().resolve("/api/v1/capabilities/xyz").upstream_url == "http://capreg:8000"


def test_krs_evaluate_rides_the_graph_prefix() -> None:
    # POST /v1/graph/{id}/evaluate (#331) must reach the knowledge-retriever via /v1/graph —
    # pinned against the REAL _ROUTES (build_route_table over Settings), not a synthetic table,
    # so a prefix re-shuffle in route_table.py cannot silently strand the endpoint (#333).
    settings = Settings()
    entry = build_route_table(settings).resolve("/v1/graph/abc/evaluate")
    assert entry is not None
    assert entry.prefix == "/v1/graph"
    assert entry.upstream_url == settings.KNOWLEDGE_RETRIEVER_URL.rstrip("/")


def test_shared_stem_does_not_cross_route() -> None:
    # both live under /api/v1 — longest-match keeps them distinct
    assert _table().resolve("/api/v1/capabilities").upstream_url == "http://capreg:8000"
    assert _table().resolve("/api/v1/graphs").upstream_url == "http://kgs:8000"


def test_boundary_prevents_false_prefix_match() -> None:
    # /v1/searchable must NOT match the /v1/search prefix
    assert _table().resolve("/v1/searchable") is None


def test_unknown_prefix_is_unresolved() -> None:
    assert _table().resolve("/totally/unknown") is None
    assert _table().resolve("/internal/agent-credentials") is None  # internal plane not edge-routed


def test_build_from_settings_maps_all_upstreams() -> None:
    table = build_route_table(Settings())
    prefixes = {e.prefix for e in table.entries}
    assert {
        "/v1/auth",
        "/credentials",
        "/api/v1/graphs",
        "/v1/search",
        "/v1/federated",
        "/v1/graph",
        "/api/v1/capabilities",
    } <= prefixes
    # base urls come from settings and carry no trailing slash
    assert all(not e.upstream_url.endswith("/") for e in table.entries)
