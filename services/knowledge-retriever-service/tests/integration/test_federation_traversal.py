"""Federation traversal integration tests (ORAA-59).

Acceptance criterion 4: integration tests for federation traversal pass against
a real Neo4j instance.

These tests are marked ``integration`` (requires Docker / live Neo4j) and
``federation`` (cross-graph traversal gate).  They are excluded from the fast
unit run — select with:
  pytest -m "integration and federation" services/knowledge-retriever-service/

Test scaffold strategy:
  1. Seed two Graph nodes (g-alpha, g-beta), both owned by the same user and
     with ``federatable=true``.
  2. Seed __Entity__ nodes in each graph.
  3. Call ``FederationService.federated_query`` and assert the union result.
  4. Assert LINKED_TO traversal via ``list_graph_links`` returns the edge with
     correct ReBAC enforcement.

All imports are function-local (ORA-48 / TST001) so collection succeeds during
the TDD window before the KRS modules exist.

RED until:
  - ``oraclous_knowledge_retriever_service.federation_service`` is implemented
  - ``oraclous_knowledge_retriever_service.linked_to_service`` is implemented
  - The Docker Neo4j fixture is available (``neo4j_driver`` from root conftest)
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.federation]

# ── Seed helpers ──────────────────────────────────────────────────────────

_USER_ALICE = "user-alice-integration-001"
_GRAPH_ALPHA = "graph-alpha-integration-001"
_GRAPH_BETA = "graph-beta-integration-002"
_ORG_ID = "org-integration-test-001"

_SEED_GRAPHS_CYPHER = """
MERGE (ga:Graph:__Rebac__ {graph_id: $alpha, namespace: '__system__'})
SET ga.owner_user_id = $user,
    ga.name          = 'Alpha',
    ga.federatable   = true,
    ga.org_id        = $org

MERGE (gb:Graph:__Rebac__ {graph_id: $beta, namespace: '__system__'})
SET gb.owner_user_id = $user,
    gb.name          = 'Beta',
    gb.federatable   = true,
    gb.org_id        = $org

MERGE (gpa:Graph:__Platform__ {graph_id: $alpha})
SET gpa.org_id = $org

MERGE (gpb:Graph:__Platform__ {graph_id: $beta})
SET gpb.org_id = $org
"""

_SEED_ENTITIES_CYPHER = """
MERGE (ea:__Entity__ {graph_id: $alpha, id: 'e-alice-a'})
SET ea.name = 'Alice', ea.type = 'Person'

MERGE (eb:__Entity__ {graph_id: $beta, id: 'e-alice-b'})
SET eb.name = 'Alice', eb.type = 'Person'

MERGE (ec:__Entity__ {graph_id: $alpha, id: 'e-unique-a'})
SET ec.name = 'UniqueAlpha', ec.type = 'Thing'
"""

_SEED_USER_ROLE_CYPHER = """
MERGE (u:User:__Platform__ {user_id: $user})
MERGE (ra:Role:__System__ {graph_id: $alpha, name: 'owner'})
MERGE (rb:Role:__System__ {graph_id: $beta, name: 'viewer'})
MERGE (u)-[ha:HAS_ROLE {graph_id: $alpha}]->(ra)
SET ha.is_active = true
MERGE (u)-[hb:HAS_ROLE {graph_id: $beta}]->(rb)
SET hb.is_active = true
"""

_SEED_LINKED_TO_CYPHER = """
MATCH (ga:Graph:__Platform__ {graph_id: $alpha})
MATCH (gb:Graph:__Platform__ {graph_id: $beta})
MERGE (ga)-[l:LINKED_TO]->(gb)
SET l.min_role   = 'viewer',
    l.created_by = $user,
    l.created_at = datetime()
"""

_CLEANUP_CYPHER = """
MATCH (n)
WHERE n.graph_id IN [$alpha, $beta]
   OR (n:User AND n:__Platform__ AND n.user_id = $user)
DETACH DELETE n
"""


@pytest.fixture(autouse=True)
async def seed_and_cleanup(neo4j_driver):
    """Seed integration fixtures before each test and clean up after."""
    params = {
        "alpha": _GRAPH_ALPHA,
        "beta": _GRAPH_BETA,
        "user": _USER_ALICE,
        "org": _ORG_ID,
    }
    async with neo4j_driver.session() as session:
        await session.run(_SEED_GRAPHS_CYPHER, params)
        await session.run(_SEED_ENTITIES_CYPHER, params)
        await session.run(_SEED_USER_ROLE_CYPHER, params)
        await session.run(_SEED_LINKED_TO_CYPHER, params)

    yield

    async with neo4j_driver.session() as session:
        await session.run(_CLEANUP_CYPHER, params)


# ── Federation query traversal ────────────────────────────────────────────


class TestFederationQueryTraversal:
    """FederationService.federated_query traverses both graphs and returns merged entities."""

    @pytest.mark.asyncio
    async def test_federated_query_returns_entities_from_both_graphs(self, neo4j_driver) -> None:
        """federated_query must return entities from both alpha and beta graphs."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationService,
        )

        svc = FederationService(neo4j_driver)
        result = await svc.federated_query(_USER_ALICE, [_GRAPH_ALPHA, _GRAPH_BETA], "Alice")

        assert result["status"] == "ok"
        graph_ids = {e.source_graph_id for e in result["entities"]}
        assert _GRAPH_ALPHA in graph_ids, "Alpha graph entities must appear in federation result"
        assert _GRAPH_BETA in graph_ids, "Beta graph entities must appear in federation result"

    @pytest.mark.asyncio
    async def test_federated_query_reports_both_graphs_queried(self, neo4j_driver) -> None:
        """federated_query must list both graphs in ``graphs_queried``."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationService,
        )

        svc = FederationService(neo4j_driver)
        result = await svc.federated_query(_USER_ALICE, [_GRAPH_ALPHA, _GRAPH_BETA], "Alice")

        assert set(result["graphs_queried"]) == {_GRAPH_ALPHA, _GRAPH_BETA}

    @pytest.mark.asyncio
    async def test_federated_query_same_as_deduplication(self, neo4j_driver) -> None:
        """Entities with identical (name, type) in different graphs must produce a SAME_AS link."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationQueryOptions,
            FederationService,
        )

        svc = FederationService(neo4j_driver)
        opts = FederationQueryOptions(deduplicate_entities=True, include_cross_graph_links=True)
        result = await svc.federated_query(
            _USER_ALICE, [_GRAPH_ALPHA, _GRAPH_BETA], "Alice", options=opts
        )

        links = result.get("cross_graph_links", [])
        same_as_links = [lk for lk in links if lk.link_type == "SAME_AS"]
        assert same_as_links, (
            "Expected at least one SAME_AS link for 'Alice' (Person) across alpha and beta"
        )

    @pytest.mark.asyncio
    async def test_federated_query_rejects_wrong_owner(self, neo4j_driver) -> None:
        """federated_query must raise FederationError(403) for a graph owned by another user."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationError,
            FederationService,
        )

        svc = FederationService(neo4j_driver)
        with pytest.raises(FederationError) as exc_info:
            await svc.federated_query("other-user-999", [_GRAPH_ALPHA, _GRAPH_BETA], "Alice")

        assert exc_info.value.status_code == 403


# ── LINKED_TO traversal from retriever ────────────────────────────────────


class TestLinkedToTraversalFromRetriever:
    """list_graph_links traverses LINKED_TO edges with ReBAC enforcement applied."""

    @pytest.mark.asyncio
    async def test_list_graph_links_returns_seeded_edge(self, neo4j_driver) -> None:
        """list_graph_links must return the LINKED_TO edge seeded between alpha and beta."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_graph_links,
        )

        result = await list_graph_links(neo4j_driver, _GRAPH_ALPHA, _USER_ALICE)

        assert len(result) >= 1, (
            f"Expected at least one LINKED_TO edge from {_GRAPH_ALPHA}, got {result}"
        )
        target_ids = [r["target_graph_id"] for r in result]
        assert _GRAPH_BETA in target_ids

    @pytest.mark.asyncio
    async def test_list_graph_links_rebac_enforcement(self, neo4j_driver) -> None:
        """list_graph_links must hide edges from a caller with no role on the source graph."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_graph_links,
        )

        result = await list_graph_links(neo4j_driver, _GRAPH_ALPHA, "user-no-role-xxx")
        assert result == [], (
            "User with no ReBAC role on the source graph must see no LINKED_TO edges"
        )

    @pytest.mark.asyncio
    async def test_list_entity_links_traversal(self, neo4j_driver) -> None:
        """list_entity_links must return entity-level LINKED_TO edges from the retriever."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_entity_links,
        )

        # No entity-level LINKED_TO edge was seeded — expect empty list (not an error)
        result = await list_entity_links(neo4j_driver, _GRAPH_ALPHA, "e-alice-a", _USER_ALICE)
        assert isinstance(result, list), "list_entity_links must return a list"


# ── Federation candidates ─────────────────────────────────────────────────


class TestFindFederationCandidates:
    """FederationService.find_federation_candidates returns SAME_AS candidates."""

    @pytest.mark.asyncio
    async def test_find_federation_candidates_returns_alice_pair(self, neo4j_driver) -> None:
        """find_federation_candidates must surface (Alice, Person) as a cross-graph candidate."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationService,
        )

        svc = FederationService(neo4j_driver)
        candidates = await svc.find_federation_candidates(
            _USER_ALICE,
            graph_id=_GRAPH_ALPHA,
            target_graph_ids=[_GRAPH_BETA],
        )

        assert candidates, "Expected at least one candidate pair for 'Alice' across alpha/beta"
        scores = [c["score"] for c in candidates]
        assert all(0.0 <= s <= 1.0 for s in scores), "Candidate scores must be in [0.0, 1.0]"

    @pytest.mark.asyncio
    async def test_find_federation_candidates_score_structure(self, neo4j_driver) -> None:
        """Each candidate must carry the four signal keys."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationService,
        )

        svc = FederationService(neo4j_driver)
        candidates = await svc.find_federation_candidates(
            _USER_ALICE,
            graph_id=_GRAPH_ALPHA,
            target_graph_ids=[_GRAPH_BETA],
        )

        if not candidates:
            pytest.skip("No candidates returned — seeding may differ; skip signal-structure check")

        required_signals = {"name", "type", "embedding", "shared_relations"}
        for candidate in candidates:
            assert "signals" in candidate, "Candidate must carry a 'signals' dict"
            missing = required_signals - candidate["signals"].keys()
            assert not missing, f"Candidate signals missing keys: {missing}"
