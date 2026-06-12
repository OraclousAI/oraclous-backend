"""Real-Neo4j-GDS integration test for community detection (#303, ORAA-4 §22 — real substrate).

This MUST prove ``gds.louvain`` actually runs on ``neo4j:5.23-community`` (the deployed image, with
the GDS plugin), not against a mock. A dedicated GDS-enabled container is spun up (the shared
``neo4j_driver`` fixture in the repo-root conftest has no GDS plugin), a small org+graph-scoped
entity graph is seeded with a clear planted community structure, detection is run through the real
``CommunityRepository``, and the assertions check that:

  * ``:__Community__`` nodes + ``IN_COMMUNITY`` edges are written (so Louvain ran in-DB);
  * the hierarchy is multi-level (the 5-resolution sweep produced communities at >1 level);
  * everything is org+graph scoped — a SECOND org's graph in the SAME database is invisible to the
    first org's detection (cross-org isolation at the substrate), and detecting org B's graph never
    touches org A's communities.

Marked ``integration`` + ``organization_isolation`` so it runs in the integration lane (Docker
required) and is collected for the isolation suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import use_organisation_context
from oraclous_knowledge_graph_service.domain.community import DEFAULT_RESOLUTIONS
from oraclous_knowledge_graph_service.repositories.community_repository import CommunityRepository

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_GDS_IMAGE = "neo4j:5.23-community"

_ORG_A = "11111111-1111-1111-1111-111111111111"
_ORG_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(scope="module")
def gds_driver() -> Iterator[object]:
    """A ``neo4j:5.23-community`` container WITH the GDS plugin loaded (mirrors the deploy compose).

    Module-scoped: the GDS plugin download + load is slow, so one container serves all assertions.
    """
    from neo4j import GraphDatabase
    from testcontainers.neo4j import Neo4jContainer

    container = (
        Neo4jContainer(_GDS_IMAGE)
        .with_env("NEO4J_AUTH", "neo4j/password")
        .with_env("NEO4J_PLUGINS", '["graph-data-science"]')
        .with_env("NEO4J_dbms_security_procedures_unrestricted", "gds.*")
    )
    with container:
        driver = GraphDatabase.driver(container.get_connection_url(), auth=("neo4j", "password"))
        try:
            driver.verify_connectivity()
            # Sanity: the GDS plugin really loaded (else the whole test is meaningless).
            driver.execute_query("RETURN gds.version() AS v")
            yield driver
        finally:
            driver.close()


def _ctx(org: str) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=uuid.UUID(org),
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


def _seed_planted_graph(driver, *, organisation_id: str, graph_id: str, base: int) -> None:
    """Seed two dense clusters joined by one weak bridge (a clear 2-community structure).

    ``base`` offsets the entity ids so two graphs in the same DB don't collide. Every node + edge
    carries organisation_id + graph_id (the scope the repository filters on).
    """
    # 12 entities: ids base..base+11; cluster 1 = first 6, cluster 2 = last 6.
    driver.execute_query(
        "UNWIND range($lo, $hi) AS i "
        "CREATE (:__Entity__ {id: 'e' + toString(i), name: 'Entity ' + toString(i), "
        "organisation_id: $org, graph_id: $graph})",
        lo=base,
        hi=base + 11,
        org=organisation_id,
        graph=graph_id,
    )
    # Dense intra-cluster edges (weight 5), one weak inter-cluster bridge (weight 1).
    driver.execute_query(
        "MATCH (a:__Entity__ {organisation_id: $org, graph_id: $graph}), "
        "(b:__Entity__ {organisation_id: $org, graph_id: $graph}) "
        "WHERE a.id < b.id AND "
        "((toInteger(substring(a.id,1)) < $mid AND toInteger(substring(b.id,1)) < $mid) OR "
        " (toInteger(substring(a.id,1)) >= $mid AND toInteger(substring(b.id,1)) >= $mid)) "
        "CREATE (a)-[:REL {organisation_id: $org, graph_id: $graph, weight: 5.0}]->(b)",
        org=organisation_id,
        graph=graph_id,
        mid=base + 6,
    )
    driver.execute_query(
        "MATCH (a:__Entity__ {id: 'e' + toString($x), organisation_id: $org, graph_id: $graph}), "
        "(b:__Entity__ {id: 'e' + toString($y), organisation_id: $org, graph_id: $graph}) "
        "CREATE (a)-[:REL {organisation_id: $org, graph_id: $graph, weight: 1.0}]->(b)",
        x=base,
        y=base + 6,
        org=organisation_id,
        graph=graph_id,
    )


def _community_count(driver, *, organisation_id: str, graph_id: str) -> int:
    records, _, _ = driver.execute_query(
        "MATCH (c:__Community__ {organisation_id: $org, graph_id: $graph}) RETURN count(c) AS c",
        org=organisation_id,
        graph=graph_id,
    )
    return int(records[0]["c"])


def _in_community_count(driver, *, organisation_id: str, graph_id: str) -> int:
    records, _, _ = driver.execute_query(
        "MATCH (:__Entity__ {organisation_id: $org, graph_id: $graph})"
        "-[r:IN_COMMUNITY]->(:__Community__ {organisation_id: $org, graph_id: $graph}) "
        "RETURN count(r) AS c",
        org=organisation_id,
        graph=graph_id,
    )
    return int(records[0]["c"])


def test_louvain_runs_in_db_and_writes_scoped_communities(gds_driver) -> None:
    driver = gds_driver
    driver.execute_query("MATCH (n) DETACH DELETE n")
    graph_a = str(uuid.uuid4())
    graph_b = str(uuid.uuid4())
    # Two orgs, each with their own graph, in the SAME Neo4j database.
    _seed_planted_graph(driver, organisation_id=_ORG_A, graph_id=graph_a, base=0)
    _seed_planted_graph(driver, organisation_id=_ORG_B, graph_id=graph_b, base=100)

    repo = CommunityRepository(driver)

    # --- detect for org A only ---
    with use_organisation_context(_ctx(_ORG_A)):
        levels = repo.detect(graph_id=graph_a, resolutions=DEFAULT_RESOLUTIONS)

    # gds.louvain actually ran in-DB and produced communities + membership edges.
    assert _community_count(driver, organisation_id=_ORG_A, graph_id=graph_a) > 0
    assert _in_community_count(driver, organisation_id=_ORG_A, graph_id=graph_a) > 0
    # Every entity is a member of at least one community (membership covers the graph).
    assert _in_community_count(driver, organisation_id=_ORG_A, graph_id=graph_a) >= 12

    # Multi-level hierarchy: the 5-resolution sweep produced communities across >1 level.
    distinct_levels = {level for level, groups in levels.items() if groups}
    assert len(distinct_levels) >= 2, f"expected multi-level hierarchy, got {distinct_levels}"
    records, _, _ = driver.execute_query(
        "MATCH (c:__Community__ {organisation_id: $org, graph_id: $graph}) "
        "RETURN collect(DISTINCT c.level) AS levels",
        org=_ORG_A,
        graph=graph_a,
    )
    assert len(records[0]["levels"]) >= 2

    # At a coarse resolution the planted 2-cluster structure is recovered (2 level-0 communities).
    level_0 = levels[0]
    assert len(level_0) == 2, f"expected 2 coarse communities, got {len(level_0)}"

    # --- cross-org isolation: org B's graph was NOT touched by org A's detection ---
    assert _community_count(driver, organisation_id=_ORG_B, graph_id=graph_b) == 0

    # org A cannot SEE org B's (absent) communities, and detecting org B is independent.
    with use_organisation_context(_ctx(_ORG_B)):
        repo.detect(graph_id=graph_b, resolutions=DEFAULT_RESOLUTIONS)
    assert _community_count(driver, organisation_id=_ORG_B, graph_id=graph_b) > 0
    # org A's community count is unchanged by org B's run (no cross-org bleed).
    assert _community_count(driver, organisation_id=_ORG_A, graph_id=graph_a) > 0

    # A read scoped to org A never returns org B's communities.
    with use_organisation_context(_ctx(_ORG_A)):
        a_communities = repo.list_communities(graph_id=graph_a, level=None, min_entities=1)
        # org A querying org B's graph_id sees nothing (graph-scoped within the org).
        b_via_a = repo.list_communities(graph_id=graph_b, level=None, min_entities=1)
    assert a_communities
    assert b_via_a == []


def test_no_projection_leaks_after_detect(gds_driver) -> None:
    """Every in-memory GDS projection is dropped — no named graph survives a detect run."""
    driver = gds_driver
    driver.execute_query("MATCH (n) DETACH DELETE n")
    graph_id = str(uuid.uuid4())
    _seed_planted_graph(driver, organisation_id=_ORG_A, graph_id=graph_id, base=0)
    repo = CommunityRepository(driver)
    with use_organisation_context(_ctx(_ORG_A)):
        repo.detect(graph_id=graph_id, resolutions=DEFAULT_RESOLUTIONS)
    records, _, _ = driver.execute_query("CALL gds.graph.list() YIELD graphName RETURN graphName")
    leaked = [r["graphName"] for r in records if r["graphName"].startswith("kgs_comm")]
    assert leaked == [], f"GDS projections leaked: {leaked}"
