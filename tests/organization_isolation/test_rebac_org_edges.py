"""ReBAC relation edges carry organisation_id; cross-org checks return 0
(AC#2).

Proven at the data layer on the 0d real-substrate harness (``neo4j_driver``),
following the precedent in ``test_neo4j_org_scoping.py``. This pins the
ADR-006 reshape invariant the extracted ``oraclous_rebac`` engine MUST honour:
the Phase B HAS_ROLE traversal is scoped by ``organisation_id`` on the edge, so
an identical (user, graph) under a different organisation resolves to 0
(deny) — closing the tenant loop (Threat T1).

The engine-object behaviour (cache→Phase B→Phase A, fail-closed, soft-revoke,
cache invalidation) is pinned at unit level in
``packages/rebac/tests/unit/``; the substrate seam wiring is covered separately.
The async engine cannot be driven by the harness's *sync* ``neo4j_driver``, so
this test asserts the org-scoped traversal invariant directly in Cypher — the
same data-layer approach the schema harness test uses. RED once the substrate /
engine schema enforces it; the seed here writes the edges the reshape requires.

NB ``neo4j:5.23-community``: property-existence constraints are Enterprise-only,
so "every edge carries organisation_id" is proven by data-layer isolation (an
edge lacking it is not matched by an org-scoped query), not a NOT-NULL
constraint. Flagged for the architect at Tests Review.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation, pytest.mark.rebac]

ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"
USER = "rebac-user-shared"
GRAPH = "rebac-graph-shared"

# The Phase B HAS_ROLE traversal, reshaped to filter on organisation_id (the
# edge property). ``count > 0`` is the legacy authorisation signal; 0 == deny.
_CHECK = """
MATCH (u:User {user_id: $user, _ora34_marker: $marker})
  -[hr:HAS_ROLE]->(r:Role {_ora34_marker: $marker})
WHERE hr.organisation_id = $org
  AND hr.graph_id = $graph
  AND hr.is_active = true
  AND (hr.expires_at IS NULL OR hr.expires_at > datetime())
RETURN count(hr) AS c
"""


def _seed(
    driver, marker: str, *, org: str, is_active: bool = True, expires=None, with_org: bool = True
):
    driver.execute_query(
        """
        MERGE (u:User {user_id: $user, _ora34_marker: $marker})
        MERGE (r:Role {graph_id: $graph, name: 'owner', _ora34_marker: $marker})
        CREATE (u)-[hr:HAS_ROLE]->(r)
        SET hr.graph_id = $graph,
            hr.is_active = $is_active,
            hr.expires_at = CASE WHEN $expires IS NULL THEN NULL ELSE datetime($expires) END,
            hr._ora34_marker = $marker
        FOREACH (_ IN CASE WHEN $with_org THEN [1] ELSE [] END |
            SET hr.organisation_id = $org)
        """,
        user=USER,
        graph=GRAPH,
        org=org,
        marker=marker,
        is_active=is_active,
        expires=expires,
        with_org=with_org,
    )


def _count(driver, marker: str, *, org: str) -> int:
    records, _, _ = driver.execute_query(_CHECK, user=USER, graph=GRAPH, org=org, marker=marker)
    return records[0]["c"]


def _cleanup(driver, marker: str) -> None:
    driver.execute_query("MATCH (n {_ora34_marker: $marker}) DETACH DELETE n", marker=marker)


def test_same_org_grant_authorises(neo4j_driver) -> None:
    """A HAS_ROLE edge scoped to ORG_A authorises a check made under ORG_A."""
    marker = f"ora34-{uuid.uuid4()}"
    try:
        _seed(neo4j_driver, marker, org=ORG_A)
        assert _count(neo4j_driver, marker, org=ORG_A) == 1
    finally:
        _cleanup(neo4j_driver, marker)


def test_cross_org_check_returns_zero(neo4j_driver) -> None:
    """The AC: identical (user, graph) under a *different* org resolves to 0.

    Only an ORG_A edge exists. A check under ORG_B — same principal, same
    resource — must see nothing, because the edge's organisation_id isolates it.
    """
    marker = f"ora34-{uuid.uuid4()}"
    try:
        _seed(neo4j_driver, marker, org=ORG_A)
        assert _count(neo4j_driver, marker, org=ORG_B) == 0
    finally:
        _cleanup(neo4j_driver, marker)


def test_edge_without_organisation_id_is_not_matched(neo4j_driver) -> None:
    """An edge lacking organisation_id is invisible to an org-scoped query.

    This is how "every relation edge carries organisation_id" is enforced on
    community Neo4j (no property-existence constraint): an un-scoped edge can
    never satisfy a check.
    """
    marker = f"ora34-{uuid.uuid4()}"
    try:
        _seed(neo4j_driver, marker, org=ORG_A, with_org=False)
        assert _count(neo4j_driver, marker, org=ORG_A) == 0
    finally:
        _cleanup(neo4j_driver, marker)


def test_soft_revoked_edge_returns_zero_within_org(neo4j_driver) -> None:
    """Soft-revoke (is_active=false) denies even in the correct org."""
    marker = f"ora34-{uuid.uuid4()}"
    try:
        _seed(neo4j_driver, marker, org=ORG_A, is_active=False)
        assert _count(neo4j_driver, marker, org=ORG_A) == 0
    finally:
        _cleanup(neo4j_driver, marker)


def test_expired_grant_returns_zero_within_org(neo4j_driver) -> None:
    """Live expiry: a grant whose expires_at is in the past denies."""
    marker = f"ora34-{uuid.uuid4()}"
    try:
        _seed(neo4j_driver, marker, org=ORG_A, expires="2000-01-01T00:00:00Z")
        assert _count(neo4j_driver, marker, org=ORG_A) == 0
    finally:
        _cleanup(neo4j_driver, marker)
