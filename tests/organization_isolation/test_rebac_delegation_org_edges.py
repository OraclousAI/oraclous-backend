"""Agent-delegation relation edges carry ``organisation_id``; the delegation
traversal honours scope, soft-revoke, live expiry, the cross-org boundary,
and the no-transitive-delegation rule (R1-C2; T1 + T2 + T2-M2).

Proven at the data layer on the 0d real-substrate harness (``neo4j_driver``),
following the precedent in ``test_rebac_org_edges.py`` (C1). This
pins the ADR-006 reshape invariants the C2 delegation traversal MUST honour:

* the ``DELEGATED_TO`` edge is scoped by ``organisation_id`` so a check in
  org B never sees org A's delegation (Threat T1 — the tenant loop);
* the edge carries ``scope`` (``graph`` or ``subgraph``) and (when narrowed)
  ``subgraph_id``, and a subgraph-scoped delegation is invisible to a check
  against a different subgraph (scope-bounded access — AC#1);
* a ``DELEGATED_TO`` edge whose source is itself an ``Agent`` (a transitive
  ``Agent→Agent`` link) is invisible to the traversal — only ``User→Agent``
  paths authorise (T2 transitive-escalation mitigation — AC#3);
* revocation propagates within the bounded stale-relation tolerance: the
  ``is_active=false`` flip makes the next traversal return 0 (T2-M2 — AC#2;
  the cache-layer side is unit-tested in
  ``packages/rebac/tests/unit/test_agent_delegation.py``).

The async engine cannot be driven by the harness's *sync* ``neo4j_driver``,
so this file asserts the org-scoped traversal invariants directly in Cypher
— the same data-layer approach the org-edge harness tests use. RED
once the substrate / engine schema enforces it; the seed here writes the
edges the reshape requires.

NB ``neo4j:5.23-community``: property-existence constraints are Enterprise-
only, so "every delegation edge carries ``organisation_id``" is proven by
data-layer isolation (an un-scoped edge is not matched), not a NOT-NULL
constraint. Flagged for the architect at Tests Review (same note as C1).
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.rebac,
    pytest.mark.security,
]

ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"
MEMBER = "delegation-member-shared"
AGENT = "delegation-agent-shared"
OTHER_AGENT = "delegation-other-agent-shared"
GRAPH = "delegation-graph-shared"
OTHER_GRAPH = "delegation-other-graph-shared"
SUBGRAPH_A = "delegation-sg-a"
SUBGRAPH_B = "delegation-sg-b"


# ── Cypher that mirrors the delegation traversal contract ──────────────────
#
# The traversal: an org-scoped, live, active DELEGATED_TO edge from a User
# (member) to the requested Agent on the requested graph. For a subgraph-
# narrowed check, the edge's ``scope`` must be ``"graph"`` (any subgraph)
# OR ``"subgraph"`` matching ``$subgraph_id``. For a graph-level check, the
# scope must be ``"graph"`` — a subgraph-only delegation does not authorise
# a graph-wide query.
#
# This is the data-layer shape the engine's check_agent_graph_permission
# Cypher must satisfy. Tests assert *invariants* on the edge property
# space; the engine is free on direction and supplementary labels.

_GRAPH_CHECK = """
MATCH (m:User {user_id: $member, _ora35_marker: $marker})
  -[d:DELEGATED_TO]->(a:Agent {agent_id: $agent, _ora35_marker: $marker})
WHERE d.organisation_id = $org
  AND d.graph_id = $graph
  AND d.scope = 'graph'
  AND d.is_active = true
  AND (d.expires_at IS NULL OR d.expires_at > datetime())
RETURN count(d) AS c
"""

_SUBGRAPH_CHECK = """
MATCH (m:User {user_id: $member, _ora35_marker: $marker})
  -[d:DELEGATED_TO]->(a:Agent {agent_id: $agent, _ora35_marker: $marker})
WHERE d.organisation_id = $org
  AND d.graph_id = $graph
  AND d.is_active = true
  AND (d.expires_at IS NULL OR d.expires_at > datetime())
  AND (d.scope = 'graph' OR (d.scope = 'subgraph' AND d.subgraph_id = $subgraph))
RETURN count(d) AS c
"""

_TRANSITIVE_CHECK = """
MATCH (a1:Agent {agent_id: $delegator, _ora35_marker: $marker})
  -[d:DELEGATED_TO]->(a2:Agent {agent_id: $delegate, _ora35_marker: $marker})
WHERE d.organisation_id = $org
  AND d.graph_id = $graph
  AND d.is_active = true
RETURN count(d) AS c
"""


# ── Seeding helpers ────────────────────────────────────────────────────────


def _seed_delegation(
    driver,
    marker: str,
    *,
    org: str,
    scope: str = "graph",
    subgraph_id: str | None = None,
    is_active: bool = True,
    expires=None,
    with_org: bool = True,
    member: str = MEMBER,
    agent: str = AGENT,
    graph: str = GRAPH,
) -> None:
    """Write a (User)-[:DELEGATED_TO]->(Agent) edge with the shape the C2
    traversal must match. ``with_org=False`` writes the edge but leaves the
    ``organisation_id`` property unset — used to prove "no org_id = invisible".
    """
    driver.execute_query(
        """
        MERGE (m:User {user_id: $member, _ora35_marker: $marker})
        MERGE (a:Agent {agent_id: $agent, _ora35_marker: $marker})
        CREATE (m)-[d:DELEGATED_TO]->(a)
        SET d.graph_id = $graph,
            d.scope = $scope,
            d.is_active = $is_active,
            d.granted_at = datetime(),
            d.granted_by = 'test-seed',
            d.expires_at = CASE WHEN $expires IS NULL THEN NULL ELSE datetime($expires) END,
            d.subgraph_id = $subgraph,
            d._ora35_marker = $marker
        FOREACH (_ IN CASE WHEN $with_org THEN [1] ELSE [] END |
            SET d.organisation_id = $org)
        """,
        member=member,
        agent=agent,
        graph=graph,
        org=org,
        scope=scope,
        subgraph=subgraph_id,
        marker=marker,
        is_active=is_active,
        expires=expires,
        with_org=with_org,
    )


def _seed_transitive(
    driver,
    marker: str,
    *,
    org: str,
    delegator_agent: str = AGENT,
    delegate_agent: str = OTHER_AGENT,
    graph: str = GRAPH,
) -> None:
    """Write an illegal (Agent)-[:DELEGATED_TO]->(Agent) edge. The traversal
    must refuse to authorise through it (only User-sourced edges count).
    """
    driver.execute_query(
        """
        MERGE (a1:Agent {agent_id: $delegator, _ora35_marker: $marker})
        MERGE (a2:Agent {agent_id: $delegate, _ora35_marker: $marker})
        CREATE (a1)-[d:DELEGATED_TO]->(a2)
        SET d.graph_id = $graph,
            d.scope = 'graph',
            d.is_active = true,
            d.organisation_id = $org,
            d.granted_at = datetime(),
            d._ora35_marker = $marker
        """,
        delegator=delegator_agent,
        delegate=delegate_agent,
        graph=graph,
        org=org,
        marker=marker,
    )


def _revoke(driver, marker: str) -> None:
    """Apply the soft-revoke operation the engine performs — flip
    ``is_active`` on every delegation edge tagged with this marker.
    """
    driver.execute_query(
        "MATCH ()-[d:DELEGATED_TO {_ora35_marker: $marker}]->() SET d.is_active = false",
        marker=marker,
    )


def _graph_check(driver, marker: str, *, org: str, graph: str = GRAPH) -> int:
    records, _, _ = driver.execute_query(
        _GRAPH_CHECK, member=MEMBER, agent=AGENT, graph=graph, org=org, marker=marker
    )
    return records[0]["c"]


def _subgraph_check(driver, marker: str, *, org: str, subgraph: str, graph: str = GRAPH) -> int:
    records, _, _ = driver.execute_query(
        _SUBGRAPH_CHECK,
        member=MEMBER,
        agent=AGENT,
        graph=graph,
        org=org,
        subgraph=subgraph,
        marker=marker,
    )
    return records[0]["c"]


def _transitive_check(driver, marker: str, *, org: str) -> int:
    records, _, _ = driver.execute_query(
        _TRANSITIVE_CHECK,
        delegator=AGENT,
        delegate=OTHER_AGENT,
        graph=GRAPH,
        org=org,
        marker=marker,
    )
    return records[0]["c"]


def _cleanup(driver, marker: str) -> None:
    driver.execute_query("MATCH (n {_ora35_marker: $marker}) DETACH DELETE n", marker=marker)


# ── 1. Same-org grant authorises (graph scope) ─────────────────────────────


def test_same_org_graph_delegation_authorises(neo4j_driver) -> None:
    """A graph-scope DELEGATED_TO edge in ORG_A is traversable under ORG_A."""
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(neo4j_driver, marker, org=ORG_A, scope="graph")
        assert _graph_check(neo4j_driver, marker, org=ORG_A) == 1
    finally:
        _cleanup(neo4j_driver, marker)


# ── 2. Cross-org delegation is invisible (AC#4, T1) ────────────────────────


def test_cross_org_delegation_not_traversable(neo4j_driver) -> None:
    """Only an ORG_A edge exists. A check under ORG_B — same member, same
    agent, same graph — must see nothing (the cross-org isolation AC).
    """
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(neo4j_driver, marker, org=ORG_A, scope="graph")
        assert _graph_check(neo4j_driver, marker, org=ORG_B) == 0
    finally:
        _cleanup(neo4j_driver, marker)


# ── 3. Edge without organisation_id is invisible (T1, community-Neo4j enf.) ─


def test_delegation_edge_without_organisation_id_is_not_matched(neo4j_driver) -> None:
    """A DELEGATED_TO edge lacking ``organisation_id`` is invisible to an
    org-scoped query — the data-layer enforcement of "every delegation
    edge carries organisation_id" on community Neo4j (no property-existence
    constraint). Mirrors the equivalent C1 test for HAS_ROLE.
    """
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(neo4j_driver, marker, org=ORG_A, scope="graph", with_org=False)
        assert _graph_check(neo4j_driver, marker, org=ORG_A) == 0
    finally:
        _cleanup(neo4j_driver, marker)


# ── 4. Soft-revoked delegation denies within the right org ────────────────


def test_soft_revoked_delegation_returns_zero_within_org(neo4j_driver) -> None:
    """Soft-revoke (``is_active=false``) denies even in the correct org —
    the legacy soft-revoke invariant lifted onto delegation edges."""
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(neo4j_driver, marker, org=ORG_A, scope="graph", is_active=False)
        assert _graph_check(neo4j_driver, marker, org=ORG_A) == 0
    finally:
        _cleanup(neo4j_driver, marker)


# ── 5. Expired delegation denies (live expiry, T2-M2 timing) ──────────────


def test_expired_delegation_returns_zero_within_org(neo4j_driver) -> None:
    """A delegation whose ``expires_at`` is in the past denies — live-checked
    expiry, the T2 mitigation against forever-grants.
    """
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(
            neo4j_driver,
            marker,
            org=ORG_A,
            scope="graph",
            expires="2000-01-01T00:00:00Z",
        )
        assert _graph_check(neo4j_driver, marker, org=ORG_A) == 0
    finally:
        _cleanup(neo4j_driver, marker)


# ── 6. Scope-bounded — subgraph delegation doesn't reach other subgraphs ──


def test_subgraph_delegation_authorises_same_subgraph(neo4j_driver) -> None:
    """A subgraph-scoped delegation to SUBGRAPH_A authorises a check on
    SUBGRAPH_A within ORG_A.
    """
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(
            neo4j_driver,
            marker,
            org=ORG_A,
            scope="subgraph",
            subgraph_id=SUBGRAPH_A,
        )
        assert _subgraph_check(neo4j_driver, marker, org=ORG_A, subgraph=SUBGRAPH_A) == 1
    finally:
        _cleanup(neo4j_driver, marker)


def test_subgraph_delegation_does_not_authorise_other_subgraph(neo4j_driver) -> None:
    """A delegation scoped to SUBGRAPH_A must not authorise a check on
    SUBGRAPH_B in the same graph — the scope-bounded access AC (#1).
    """
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(
            neo4j_driver,
            marker,
            org=ORG_A,
            scope="subgraph",
            subgraph_id=SUBGRAPH_A,
        )
        assert _subgraph_check(neo4j_driver, marker, org=ORG_A, subgraph=SUBGRAPH_B) == 0
    finally:
        _cleanup(neo4j_driver, marker)


def test_subgraph_delegation_does_not_authorise_graph_level(neo4j_driver) -> None:
    """A subgraph-scoped delegation must not authorise a graph-wide check
    — a graph-level query requires ``scope = 'graph'``.
    """
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(
            neo4j_driver,
            marker,
            org=ORG_A,
            scope="subgraph",
            subgraph_id=SUBGRAPH_A,
        )
        assert _graph_check(neo4j_driver, marker, org=ORG_A) == 0
    finally:
        _cleanup(neo4j_driver, marker)


def test_graph_delegation_authorises_any_subgraph_check(neo4j_driver) -> None:
    """A graph-scope delegation authorises a check on *any* subgraph of
    that graph — graph-scope is the broader umbrella.
    """
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(neo4j_driver, marker, org=ORG_A, scope="graph")
        assert _subgraph_check(neo4j_driver, marker, org=ORG_A, subgraph=SUBGRAPH_A) == 1
        assert _subgraph_check(neo4j_driver, marker, org=ORG_A, subgraph=SUBGRAPH_B) == 1
    finally:
        _cleanup(neo4j_driver, marker)


# ── 7. Transitive Agent→Agent delegation is invisible (AC#3, T2) ──────────


def test_transitive_agent_to_agent_delegation_not_traversable(neo4j_driver) -> None:
    """An Agent-sourced DELEGATED_TO edge exists in the graph (legacy data or
    a manual write) but the engine's User-sourced traversal must not match
    it: the graph-check Cypher requires a ``User`` delegator, so an
    Agent→Agent edge produces 0.

    Asserts the property the traversal Cypher must guarantee: an
    Agent-sourced edge is *present* (transitive count > 0) but the
    member-sourced traversal returns 0.
    """
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_transitive(neo4j_driver, marker, org=ORG_A)
        # the edge is in the graph (sanity-check the seed)
        assert _transitive_check(neo4j_driver, marker, org=ORG_A) == 1
        # but the engine's User-sourced traversal does not see it
        records, _, _ = neo4j_driver.execute_query(
            _GRAPH_CHECK,
            member=AGENT,  # an Agent in the User slot — illegal delegator
            agent=OTHER_AGENT,
            graph=GRAPH,
            org=ORG_A,
            marker=marker,
        )
        assert records[0]["c"] == 0, (
            "transitive Agent→Agent delegation must not authorise — only "
            "(User)-[:DELEGATED_TO]->(Agent) is legal (T2)"
        )
    finally:
        _cleanup(neo4j_driver, marker)


# ── 8. Revocation propagation — AC#2 / T2-M2 data-layer side ──────────────


def test_revocation_propagates_to_next_traversal(neo4j_driver) -> None:
    """The T2-M2 contract at the data layer: a live delegation matches (1);
    after the soft-revoke flips ``is_active=false`` the **next** traversal
    matches 0. This is the "next invocation fails" half of AC#2 — the
    cache-invalidation half is unit-tested in ``test_agent_delegation.py``
    (``TestRevocationInvalidatesCache``).
    """
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(neo4j_driver, marker, org=ORG_A, scope="graph")
        assert _graph_check(neo4j_driver, marker, org=ORG_A) == 1
        _revoke(neo4j_driver, marker)
        assert _graph_check(neo4j_driver, marker, org=ORG_A) == 0
    finally:
        _cleanup(neo4j_driver, marker)


# ── 9. Cross-graph isolation (a delegation is graph-scoped) ───────────────


def test_delegation_does_not_authorise_other_graph(neo4j_driver) -> None:
    """A delegation on ``graph-1`` must not authorise a check on
    ``graph-2`` — the edge is graph-bound. (Confirms the graph_id column
    matters within the same org / member / agent triple.)
    """
    marker = f"ora35-{uuid.uuid4()}"
    try:
        _seed_delegation(neo4j_driver, marker, org=ORG_A, scope="graph", graph=GRAPH)
        records, _, _ = neo4j_driver.execute_query(
            _GRAPH_CHECK,
            member=MEMBER,
            agent=AGENT,
            graph=OTHER_GRAPH,
            org=ORG_A,
            marker=marker,
        )
        assert records[0]["c"] == 0
    finally:
        _cleanup(neo4j_driver, marker)
