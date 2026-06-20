"""Cross-org ReBAC grant END-TO-END through the API GATEWAY — NO fakes (#446, ADR-004).

The security gate the whole #446 work exists to prove, exercised exactly as two real users would,
entirely through the gateway (`:8006`):

  1. Org A's owner creates a knowledge graph (real KGS, real Neo4j).
  2. Org B's user names that graph in a federated search → **403 DENIED** (it is outside org B's
     home accessible set, and no grant exists — fail-closed, no existence oracle).
  3. Org A's owner shares a READ on the graph with org B's user — `POST /api/v1/graphs/{id}/grants`
     — which records a real ReBAC `HAS_ROLE` relation (real engine, real Neo4j).
  4. Org B's user repeats the same federated search → **200 ADMITTED** (the ReBAC engine now
     authorises the cross-org traversal; the granted graph enters the fan-out scope).

This is the first request path on which the ReBAC engine actually fires: before #446 it was built
but wired into zero paths, so cross-org reads fell straight through it. Nothing is mocked; the only
assertions are on what org B's user observes through the API. The deny→allow flip across the single
grant call IS the proof the gate mediates the read.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.security]


def _federated_search(client: httpx.Client, graph_id: str) -> httpx.Response:
    """Org B names org A's graph explicitly in a federated search (the cross-org admission path)."""
    return client.post(
        "/v1/federated/search",
        json={"query": "anything", "mode": "entity", "graph_ids": [graph_id]},
    )


def test_a_cross_org_read_is_denied_until_granted_then_admitted(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    owner = register("Org A Owner")  # org A
    grantee = register("Org B User")  # a different org → org B
    assert owner["org_id"] != grantee["org_id"], (
        "registration must place the two users in distinct orgs"
    )

    owner_c = gateway_client(owner["token"])
    grantee_c = gateway_client(grantee["token"])

    # (1) org A owns a graph
    g = owner_c.post("/api/v1/graphs", json={"name": "org-a-shared-kb", "description": "x"})
    assert g.status_code == 201, g.text
    graph_id = g.json()["id"]

    # (2) BEFORE any grant: org B naming org A's graph is fail-closed denied (not in its accessible
    #     set, no grant). 403, and the message must not confirm the graph exists (no oracle).
    before = _federated_search(grantee_c, graph_id)
    assert before.status_code == 403, (
        f"expected 403 before grant, got {before.status_code}: {before.text}"
    )
    assert graph_id not in before.text, (
        "denial must not echo the target graph id (enumeration oracle)"
    )

    # (3) org A shares a READ with org B's user — records the ReBAC HAS_ROLE relation
    grant = owner_c.post(
        f"/api/v1/graphs/{graph_id}/grants",
        json={
            "grantee_organisation_id": grantee["org_id"],
            "grantee_user_id": grantee["user_id"],
            "level": "read",
        },
    )
    assert grant.status_code == 201, f"grant failed: {grant.status_code} {grant.text}"
    assert grant.json()["granted"] is True

    # (4) AFTER the grant: the SAME call is now admitted — the gate opened via the ReBAC relation.
    after = _federated_search(grantee_c, graph_id)
    assert after.status_code == 200, (
        f"expected 200 after grant, got {after.status_code}: {after.text}"
    )
    # The granted foreign graph is now part of the resolved fan-out scope.
    assert graph_id in after.text, "the granted graph should appear in the federated response scope"


def test_only_the_owner_can_share_a_graph(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The grant is owner-gated: a non-owner (another org's user) cannot share a graph it does not
    own — the endpoint 404s (no leak that the graph exists), so a grant can never be self-issued."""
    owner = register("Org A Owner")
    outsider = register("Org B User")
    owner_c = gateway_client(owner["token"])
    outsider_c = gateway_client(outsider["token"])

    graph_id = owner_c.post("/api/v1/graphs", json={"name": "private-kb"}).json()["id"]

    # the outsider tries to grant THEMSELVES read on a graph they do not own → 404 (owner gate)
    stolen = outsider_c.post(
        f"/api/v1/graphs/{graph_id}/grants",
        json={
            "grantee_organisation_id": outsider["org_id"],
            "grantee_user_id": outsider["user_id"],
            "level": "read",
        },
    )
    assert stolen.status_code == 404, (
        f"a non-owner grant must 404, got {stolen.status_code}: {stolen.text}"
    )
