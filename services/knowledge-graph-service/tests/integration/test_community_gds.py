"""Real-Neo4j-GDS integration test for community detection (#303 — real substrate).

This MUST prove ``gds.louvain`` actually runs on ``neo4j:5.23-community`` (the deployed image, with
the GDS plugin), not against a mock. A dedicated GDS-enabled container is spun up (the shared
``neo4j_driver`` fixture in the repo-root conftest has no GDS plugin), org+graph-scoped entity
graphs are seeded, detection runs through the real ``CommunityRepository``, and we check that:

  * ``:__Community__`` nodes + ``IN_COMMUNITY`` edges are written (so Louvain ran in-DB);
  * the level count is HONEST — the native dendrogram depth, NOT five duplicate levels — even on a
    UNIFORM-weight graph (the dominant case the old ``w ** resolution`` got wrong) (TEST-11);
  * per-level community counts are monotone (non-decreasing finest→coarsest) AND parent containment
    holds: every child community's members are a subset of its parent (TEST-12);
  * edge cases survive: ≥3 entities with NO edges, two disconnected components, a single entity
    (TEST-14);
  * cross-org isolation holds at the substrate;
  * no GDS projection survives a detect, even when Louvain raises mid-run (TEST-15).

Marked ``integration`` + ``organization_isolation`` so it runs in the integration lane (Docker
required) and is collected for the isolation suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import use_organisation_context
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


def _seed_uniform_two_clusters(driver, *, organisation_id: str, graph_id: str, base: int) -> None:
    """Two dense clusters joined by one bridge — ALL edges weight 1.0 (UNIFORM weights).

    This is the dominant real-world case (the only system weight is ``len(rels)``, almost always 1).
    The old ``w ** resolution`` sweep gave the IDENTICAL partition at every exponent here, so it
    emitted five duplicate levels; the native dendrogram must instead emit an HONEST level count.
    """
    driver.execute_query(
        "UNWIND range($lo, $hi) AS i "
        "CREATE (:__Entity__ {id: 'e' + toString(i), name: 'Entity ' + toString(i), "
        "organisation_id: $org, graph_id: $graph})",
        lo=base,
        hi=base + 11,
        org=organisation_id,
        graph=graph_id,
    )
    # Dense intra-cluster edges, all weight 1.0 (UNIFORM), one weak inter-cluster bridge weight 1.0.
    driver.execute_query(
        "MATCH (a:__Entity__ {organisation_id: $org, graph_id: $graph}), "
        "(b:__Entity__ {organisation_id: $org, graph_id: $graph}) "
        "WHERE a.id < b.id AND "
        "((toInteger(substring(a.id,1)) < $mid AND toInteger(substring(b.id,1)) < $mid) OR "
        " (toInteger(substring(a.id,1)) >= $mid AND toInteger(substring(b.id,1)) >= $mid)) "
        "CREATE (a)-[:REL {organisation_id: $org, graph_id: $graph, weight: 1.0}]->(b)",
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


def _make_entities(driver, *, organisation_id: str, graph_id: str, ids: list[str]) -> None:
    driver.execute_query(
        "UNWIND $ids AS i "
        "CREATE (:__Entity__ {id: i, name: i, organisation_id: $org, graph_id: $graph})",
        ids=ids,
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


def test_uniform_weight_graph_gets_honest_level_count(gds_driver) -> None:
    """TEST-11 (the test that would have caught the blocker): a UNIFORM-weight graph must NOT emit
    five duplicate levels — the level count is the native dendrogram depth, an honest number."""
    driver = gds_driver
    driver.execute_query("MATCH (n) DETACH DELETE n")
    graph_id = str(uuid.uuid4())
    _seed_uniform_two_clusters(driver, organisation_id=_ORG_A, graph_id=graph_id, base=0)

    repo = CommunityRepository(driver)
    with use_organisation_context(_ctx(_ORG_A)):
        levels = repo.detect(graph_id=graph_id)

    # Louvain genuinely ran in-DB and produced communities + membership covering every entity.
    assert _community_count(driver, organisation_id=_ORG_A, graph_id=graph_id) > 0
    assert _in_community_count(driver, organisation_id=_ORG_A, graph_id=graph_id) >= 12

    # The HONEST count: a small uniform graph converges to a shallow dendrogram — emphatically NOT
    # five duplicate levels. (Empirically GDS converges flat or to ~2 levels here.)
    persisted_levels, _, _ = driver.execute_query(
        "MATCH (c:__Community__ {organisation_id: $org, graph_id: $graph}) "
        "RETURN collect(DISTINCT c.level) AS levels",
        org=_ORG_A,
        graph=graph_id,
    )
    n_levels = len(persisted_levels[0]["levels"])
    assert n_levels == len(levels)
    assert 1 <= n_levels <= 3, f"uniform graph should give an honest shallow depth, got {n_levels}"
    assert n_levels != 5, "five levels on a uniform graph is the degenerate sweep bug"

    # The planted 2-cluster structure is recovered at the coarsest level (level 0 = coarsest).
    count_rows, _, _ = driver.execute_query(
        "MATCH (c:__Community__ {organisation_id: $org, graph_id: $graph}) "
        "RETURN c.level AS level, count(c) AS n",
        org=_ORG_A,
        graph=graph_id,
    )
    counts_by_level = {int(r["level"]): int(r["n"]) for r in count_rows}
    assert counts_by_level[min(counts_by_level)] == 2


def test_dendrogram_monotone_and_parent_containment(gds_driver) -> None:
    """TEST-12: per-level community count monotone (non-decreasing finest→coarsest) AND every
    child community's members ⊆ its parent's members (true containment off the dendrogram)."""
    driver = gds_driver
    driver.execute_query("MATCH (n) DETACH DELETE n")
    graph_id = str(uuid.uuid4())
    # A nested weight-decay hierarchy that genuinely produces a multi-level dendrogram (verified in
    # the live probe): 16 nodes, intra-pair 100, intra-quad 10, intra-half 1, inter-half 0.02.
    _make_entities(
        driver,
        organisation_id=_ORG_A,
        graph_id=graph_id,
        ids=[f"e{i}" for i in range(16)],
    )
    driver.execute_query(
        "MATCH (a:__Entity__ {organisation_id: $org, graph_id: $graph}), "
        "(b:__Entity__ {organisation_id: $org, graph_id: $graph}) "
        "WHERE a.id < b.id "
        "WITH a, b, toInteger(substring(a.id,1)) AS ai, toInteger(substring(b.id,1)) AS bi "
        "WITH a, b, CASE "
        "  WHEN ai/2 = bi/2 THEN 100.0 WHEN ai/4 = bi/4 THEN 10.0 "
        "  WHEN ai/8 = bi/8 THEN 1.0 ELSE 0.02 END AS w "
        "CREATE (a)-[:REL {organisation_id: $org, graph_id: $graph, weight: w}]->(b)",
        org=_ORG_A,
        graph=graph_id,
    )

    repo = CommunityRepository(driver)
    with use_organisation_context(_ctx(_ORG_A)):
        levels = repo.detect(graph_id=graph_id)

    # Monotone: community count is non-decreasing from coarsest (level 0) toward finest.
    counts = [len(levels[lvl]) for lvl in sorted(levels)]
    assert counts == sorted(counts), f"per-level counts not monotone: {counts}"

    # Parent containment: every PARENT_COMMUNITY edge connects a child whose members are a subset of
    # the parent's members (read live from the written graph).
    rows, _, _ = driver.execute_query(
        "MATCH (child:__Community__ {organisation_id: $org, graph_id: $graph})"
        "-[:PARENT_COMMUNITY]->(parent:__Community__ {organisation_id: $org, graph_id: $graph}) "
        "MATCH (child)<-[:IN_COMMUNITY]-(ce:__Entity__) "
        "WITH child, parent, collect(DISTINCT ce.id) AS child_members "
        "MATCH (parent)<-[:IN_COMMUNITY]-(pe:__Entity__) "
        "RETURN child.community_id AS child, collect(DISTINCT pe.id) AS parent_members, "
        "child_members",
        org=_ORG_A,
        graph=graph_id,
    )
    assert rows, "expected at least one PARENT_COMMUNITY edge in a multi-level dendrogram"
    for r in rows:
        assert set(r["child_members"]).issubset(set(r["parent_members"])), (
            f"child {r['child']} members not contained in its parent"
        )


def test_edge_cases_edgeless_disconnected_single(gds_driver) -> None:
    """TEST-14: ≥3 entities with ZERO edges, two disconnected components, and a 1-entity graph."""
    driver = gds_driver
    repo = CommunityRepository(driver)

    # (a) edgeless: 3 entities, no relationships → edgeless projection; each is its own community.
    driver.execute_query("MATCH (n) DETACH DELETE n")
    g_edgeless = str(uuid.uuid4())
    _make_entities(driver, organisation_id=_ORG_A, graph_id=g_edgeless, ids=["a", "b", "c"])
    with use_organisation_context(_ctx(_ORG_A)):
        repo.detect(graph_id=g_edgeless)
    assert _community_count(driver, organisation_id=_ORG_A, graph_id=g_edgeless) >= 1
    assert _in_community_count(driver, organisation_id=_ORG_A, graph_id=g_edgeless) == 3

    # (b) two disconnected components: {a,b,c} clique and {d,e,f} clique, no bridge.
    driver.execute_query("MATCH (n) DETACH DELETE n")
    g_disc = str(uuid.uuid4())
    _make_entities(
        driver, organisation_id=_ORG_A, graph_id=g_disc, ids=["a", "b", "c", "d", "e", "f"]
    )
    driver.execute_query(
        "MATCH (x:__Entity__ {organisation_id: $org, graph_id: $graph}), "
        "(y:__Entity__ {organisation_id: $org, graph_id: $graph}) "
        "WHERE x.id < y.id AND ((x.id IN ['a','b','c'] AND y.id IN ['a','b','c']) OR "
        "(x.id IN ['d','e','f'] AND y.id IN ['d','e','f'])) "
        "CREATE (x)-[:REL {organisation_id: $org, graph_id: $graph, weight: 1.0}]->(y)",
        org=_ORG_A,
        graph=g_disc,
    )
    with use_organisation_context(_ctx(_ORG_A)):
        levels = repo.detect(graph_id=g_disc)
    # The two components resolve to two communities at the coarsest level.
    assert len(levels[min(levels)]) == 2
    assert _in_community_count(driver, organisation_id=_ORG_A, graph_id=g_disc) == 6

    # (c) single entity: one community, one membership edge, no parent.
    driver.execute_query("MATCH (n) DETACH DELETE n")
    g_one = str(uuid.uuid4())
    _make_entities(driver, organisation_id=_ORG_A, graph_id=g_one, ids=["solo"])
    with use_organisation_context(_ctx(_ORG_A)):
        repo.detect(graph_id=g_one)
    assert _community_count(driver, organisation_id=_ORG_A, graph_id=g_one) == 1
    assert _in_community_count(driver, organisation_id=_ORG_A, graph_id=g_one) == 1
    parents, _, _ = driver.execute_query(
        "MATCH (:__Community__ {organisation_id: $org, graph_id: $graph})"
        "-[p:PARENT_COMMUNITY]->() RETURN count(p) AS c",
        org=_ORG_A,
        graph=g_one,
    )
    assert int(parents[0]["c"]) == 0


def test_cross_org_isolation(gds_driver) -> None:
    driver = gds_driver
    driver.execute_query("MATCH (n) DETACH DELETE n")
    graph_a = str(uuid.uuid4())
    graph_b = str(uuid.uuid4())
    _seed_uniform_two_clusters(driver, organisation_id=_ORG_A, graph_id=graph_a, base=0)
    _seed_uniform_two_clusters(driver, organisation_id=_ORG_B, graph_id=graph_b, base=100)
    repo = CommunityRepository(driver)

    with use_organisation_context(_ctx(_ORG_A)):
        repo.detect(graph_id=graph_a)
    # org B untouched by org A's detection.
    assert _community_count(driver, organisation_id=_ORG_B, graph_id=graph_b) == 0
    with use_organisation_context(_ctx(_ORG_B)):
        repo.detect(graph_id=graph_b)
    assert _community_count(driver, organisation_id=_ORG_A, graph_id=graph_a) > 0
    # A read scoped to org A never returns org B's communities.
    with use_organisation_context(_ctx(_ORG_A)):
        a_communities = repo.list_communities(graph_id=graph_a, level=None, min_entities=1)
        b_via_a = repo.list_communities(graph_id=graph_b, level=None, min_entities=1)
    assert a_communities
    assert b_via_a == []


def test_no_projection_leaks_after_detect(gds_driver) -> None:
    """Every in-memory GDS projection is dropped — no named graph survives a detect run."""
    driver = gds_driver
    driver.execute_query("MATCH (n) DETACH DELETE n")
    graph_id = str(uuid.uuid4())
    _seed_uniform_two_clusters(driver, organisation_id=_ORG_A, graph_id=graph_id, base=0)
    repo = CommunityRepository(driver)
    with use_organisation_context(_ctx(_ORG_A)):
        repo.detect(graph_id=graph_id)
    records, _, _ = driver.execute_query("CALL gds.graph.list() YIELD graphName RETURN graphName")
    leaked = [r["graphName"] for r in records if r["graphName"].startswith("kgs_comm")]
    assert leaked == [], f"GDS projections leaked: {leaked}"


def test_projection_dropped_even_when_louvain_raises(gds_driver) -> None:
    """TEST-15: the projection succeeds but the Louvain stream raises — the ``finally`` drop must
    still fire, so NO ``kgs_comm`` projection survives the failed run."""
    driver = gds_driver
    driver.execute_query("MATCH (n) DETACH DELETE n")
    graph_id = str(uuid.uuid4())
    _seed_uniform_two_clusters(driver, organisation_id=_ORG_A, graph_id=graph_id, base=0)
    repo = CommunityRepository(driver)

    real_execute = driver.execute_query
    state = {"projected": False}

    def flaky_execute(query, *args, **kwargs):
        # Let the projection succeed, then blow up on the louvain stream.
        if "gds.graph.project" in query:
            state["projected"] = True
            return real_execute(query, *args, **kwargs)
        if "gds.louvain.stream" in query:
            raise RuntimeError("simulated louvain failure mid-run")
        return real_execute(query, *args, **kwargs)

    class _FlakyDriver:
        execute_query = staticmethod(flaky_execute)

    repo._driver = _FlakyDriver()  # type: ignore[assignment]  # swap the driver, keep the repo logic
    with use_organisation_context(_ctx(_ORG_A)):
        with pytest.raises(RuntimeError):
            repo.detect(graph_id=graph_id)
    assert state["projected"], "the projection should have been created before the failure"

    # Use the real driver to confirm no projection survived the failed run.
    records, _, _ = real_execute("CALL gds.graph.list() YIELD graphName RETURN graphName")
    leaked = [r["graphName"] for r in records if r["graphName"].startswith("kgs_comm")]
    assert leaked == [], f"a projection survived a failed detect: {leaked}"
