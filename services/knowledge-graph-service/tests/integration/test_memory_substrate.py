"""Real-Neo4j integration tests for the agent-memory vertical (#332 / ADR-027, ORAA-4 §22).

Drives the REAL ``MemoryService`` → ``MemoryRepository`` → Neo4j path against the shared
testcontainer (repo-root ``tests/conftest.py``), with ``ensure_schema`` applying the real indexes —
the fulltext recall in these tests runs through the actual ``kgs_memory_content`` index, not a
mock. Embeddings use controlled fake vectors (the embedder seam) so similarity behaviour is exact.

Pinned here:
  * store → search round-trip over REAL fulltext (+ hybrid vector signal), with the access bump;
  * decay ranking — an old-unaccessed memory ranks below a fresh one; the access bump (lazy
    recompute) resurfaces it on the next search;
  * content-hash dedup;
  * contradiction detection — CONTRADICTS edge written, old memory invalidated, new wins;
  * the supersede chain (PATCH semantics) — temporal versioning + current/all reads;
  * similarity consolidation — a controlled near-duplicate pair merges (winner absorbs importance,
    capped), the distinct memory survives;
  * CROSS-ORG ISOLATION — org B sees nothing of org A at BOTH the graph gate and the substrate;
  * the token budget in /context is respected.

Marked ``integration`` + ``organization_isolation`` (Docker required, §9.3).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_graph_service.core.neo4j import ensure_schema
from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.repositories.memory_repository import MemoryRepository
from oraclous_knowledge_graph_service.schema.memory_schemas import (
    MemoryCreate,
    MemoryScope,
    MemorySource,
    MemoryType,
    MemoryUpdate,
)
from oraclous_knowledge_graph_service.services.memory_service import (
    GraphNotVisible,
    MemoryService,
)
from oraclous_knowledge_graph_service.tasks.memory_tasks import run_consolidation
from oraclous_substrate.access import enforced_organisation_id

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_ORG_A = "11111111-1111-1111-1111-11111111111a"
_ORG_B = "22222222-2222-2222-2222-22222222222b"


def _ctx(org: str) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=uuid.UUID(org),
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


class _FakeGraphs:
    """In-memory stand-in for the Postgres GraphRepository — org-scoped like the real one (reads
    the BOUND org context), so the service's graph gate behaves identically without Postgres."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], Graph] = {}

    def register(self, org: str, graph_id: uuid.UUID, name: str = "g") -> None:
        now = datetime.now(UTC)
        self._rows[(org, str(graph_id))] = Graph(
            id=graph_id,
            organisation_id=uuid.UUID(org),
            user_id=uuid.uuid4(),
            name=name,
            description=None,
            status="active",
            node_count=0,
            relationship_count=0,
            created_at=now,
            updated_at=now,
        )

    async def get(self, graph_id: uuid.UUID) -> Graph | None:
        return self._rows.get((enforced_organisation_id(), str(graph_id)))

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        name: str,
        description: str | None,
        system_kind: str | None = None,
    ) -> Graph:
        graph_id = uuid.uuid4()
        self.register(enforced_organisation_id(), graph_id, name=name)
        return self._rows[(enforced_organisation_id(), str(graph_id))]

    async def find_or_create_system_graph(
        self, *, user_id: uuid.UUID, system_kind: str, name: str, description: str | None
    ) -> Graph:
        org = enforced_organisation_id()
        for (row_org, _), row in sorted(self._rows.items()):
            if row_org == org and row.system_kind == system_kind:
                return row
        graph_id = uuid.uuid4()
        now = datetime.now(UTC)
        self._rows[(org, str(graph_id))] = Graph(
            id=graph_id,
            organisation_id=uuid.UUID(org),
            user_id=user_id,
            name=name,
            description=description,
            status="active",
            node_count=0,
            relationship_count=0,
            created_at=now,
            updated_at=now,
            system_kind=system_kind,
        )
        return self._rows[(org, str(graph_id))]


class _ControlledEmbedder:
    """Deterministic table-driven embedder (controlled vectors for similarity assertions).
    Unknown texts get a fixed fallback vector so hybrid recall always has a query vector."""

    dim = 3

    def __init__(self, table: dict[str, list[float]], fallback: list[float] | None = None) -> None:
        self._table = table
        self._fallback = fallback or [0.0, 0.0, 1.0]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._table.get(t, self._fallback) for t in texts]


@pytest.fixture(scope="module")
def memory_driver() -> Iterator[Any]:
    """A real ``neo4j:5.23-community`` container (module-scoped, mirroring
    ``test_code_ingestion_substrate.py`` — the repo-root session fixture is not reachable from
    the services/ test path) with the REAL KGS schema (incl. the memory fulltext index) online."""
    from neo4j import GraphDatabase
    from testcontainers.neo4j import Neo4jContainer

    container = Neo4jContainer("neo4j:5.23-community").with_env("NEO4J_AUTH", "neo4j/password")
    with container:
        driver = GraphDatabase.driver(container.get_connection_url(), auth=("neo4j", "password"))
        try:
            driver.verify_connectivity()
            ensure_schema(driver)
            driver.execute_query("CALL db.awaitIndexes()")
            yield driver
        finally:
            driver.close()


def _service(
    driver: Any,
    graphs: _FakeGraphs,
    *,
    embedder: _ControlledEmbedder | None = None,
) -> MemoryService:
    def repo_factory(graph_id: str) -> MemoryRepository:
        return MemoryRepository(driver, graph_id=graph_id)

    return MemoryService(
        graphs=graphs,  # type: ignore[arg-type] — duck-typed org-scoped repo
        repo_factory=repo_factory,
        embedder=embedder,
        enqueue_consolidation=lambda g, o: "job-1",
    )


def _create(content: str, **overrides: Any) -> MemoryCreate:
    defaults: dict[str, Any] = {
        "type": MemoryType.SEMANTIC,
        "content": content,
        "confidence": 0.8,
        "scope": MemoryScope.AGENT,
        "source": MemorySource.AGENT,
    }
    defaults.update(overrides)
    return MemoryCreate(**defaults)


def _backdate(driver: Any, org: str, graph_id: uuid.UUID, memory_id: str, days: int) -> None:
    """Test seeding: push a memory's last-access into the past (org+graph-scoped)."""
    past = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    driver.execute_query(
        "MATCH (m:Memory {organisation_id: $org, graph_id: $graph, memory_id: $mid}) "
        "SET m.last_accessed_at = datetime($past), m.ingested_at = datetime($past), "
        "m.importance_score = m.base_importance",
        org=org,
        graph=str(graph_id),
        mid=memory_id,
        past=past,
    )


def _node(driver: Any, org: str, graph_id: uuid.UUID, memory_id: str) -> dict[str, Any] | None:
    records, _, _ = driver.execute_query(
        "MATCH (m:Memory {organisation_id: $org, graph_id: $graph, memory_id: $mid}) "
        "RETURN properties(m) AS p, labels(m) AS labels",
        org=org,
        graph=str(graph_id),
        mid=memory_id,
    )
    if not records:
        return None
    out = dict(records[0]["p"])
    out["labels"] = sorted(records[0]["labels"])
    return out


# ---------------------------------------------------------------- round-trip


async def test_store_search_roundtrip_real_fulltext_and_access_bump(memory_driver) -> None:  # noqa: ANN001
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    table = {
        "User prefers dark mode in the dashboard": [1.0, 0.0, 0.0],
        "The deploy window is Friday afternoon": [0.0, 1.0, 0.0],
        "dark mode": [0.96, 0.28, 0.0],  # near the dark-mode memory
    }
    svc = _service(memory_driver, graphs, embedder=_ControlledEmbedder(table))

    with use_organisation_context(_ctx(_ORG_A)):
        r1 = await svc.store(
            graph_id=graph_id,
            req=_create(
                "User prefers dark mode in the dashboard",
                subject="user",
                predicate="prefers",
                object="dark mode",
                agent_id="agent-1",
                session_id="sess-1",
            ),
        )
        await svc.store(
            graph_id=graph_id,
            req=_create("The deploy window is Friday afternoon", type=MemoryType.EPISODIC),
        )

        res = await svc.search(graph_id=graph_id, query="dark mode", limit=10)
        assert res.total >= 1
        top = res.memories[0]
        assert top.memory_id == r1.memory_id
        assert top.type is MemoryType.SEMANTIC
        assert top.relevance_score > 0
        assert top.agent_id == "agent-1" and top.session_id == "sess-1"

    # the hit was access-bumped (lazy decay write side): count=1 and importance re-persisted.
    node = _node(memory_driver, _ORG_A, graph_id, r1.memory_id)
    assert node is not None
    assert node["access_count"] == 1
    assert node["labels"] == ["Memory", "Semantic"]
    assert node["embedding"] == [1.0, 0.0, 0.0]  # the REAL stored vector (controlled)


async def test_store_dedups_on_content_hash(memory_driver) -> None:  # noqa: ANN001
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs)
    with use_organisation_context(_ctx(_ORG_A)):
        first = await svc.store(graph_id=graph_id, req=_create("Build cache lives on volume V1"))
        dup = await svc.store(graph_id=graph_id, req=_create("build CACHE lives  on volume v1"))
    assert dup.memory_id == first.memory_id  # normalised-hash dedup, no second node


# ---------------------------------------------------------------- decay ranking


async def test_old_unaccessed_ranks_below_fresh_and_access_bump_resurfaces(memory_driver) -> None:  # noqa: ANN001
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs, embedder=None)  # fulltext-only → deterministic text leg

    with use_organisation_context(_ctx(_ORG_A)):
        # OLD but intrinsically important (user_feedback → base 1.0), unaccessed for 300 days.
        old = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Release pipeline must page the on-call crew",
                type=MemoryType.EPISODIC,
                source=MemorySource.USER_FEEDBACK,
            ),
        )
        _backdate(memory_driver, _ORG_A, graph_id, old.memory_id, days=300)
        # FRESH agent memory (base 0.63 at confidence 0.7).
        fresh = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Release pipeline now skips the canary stage",
                type=MemoryType.EPISODIC,
                confidence=0.7,
            ),
        )

        # 1st search: the old memory has decayed to ~0 (e^(−0.05·300)) → ranks BELOW the fresh one.
        first = await svc.search(graph_id=graph_id, query="pipeline", limit=10)
        order1 = [m.memory_id for m in first.memories]
        assert order1.index(old.memory_id) > order1.index(fresh.memory_id)

        # The search itself bumped both (lazy recompute on access): the old memory's window reset
        # and its higher base importance (1.0 vs 0.63) now dominates → it RESURFACES on re-search.
        second = await svc.search(graph_id=graph_id, query="pipeline", limit=10)
        order2 = [m.memory_id for m in second.memories]
        assert order2.index(old.memory_id) < order2.index(fresh.memory_id)

    # and the bump PERSISTED the recomputed importance (no cron — the write happened on access).
    node = _node(memory_driver, _ORG_A, graph_id, old.memory_id)
    assert node is not None
    assert node["access_count"] == 2
    assert float(node["importance_score"]) > 0.9  # ≈ 1.0·e^0 + boost, capped at 1.0


# ---------------------------------------------------------------- contradictions


async def test_contradiction_detected_new_wins_old_invalidated(memory_driver) -> None:  # noqa: ANN001
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs)

    with use_organisation_context(_ctx(_ORG_A)):
        old = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Customer tier is gold",
                subject="customer-7",
                predicate="has_tier",
                object="gold",
            ),
        )
        new = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Customer tier is silver",
                subject="customer-7",
                predicate="has_tier",
                object="silver",
            ),
        )

    assert [c.conflict_memory_id for c in new.contradictions_detected] == [old.memory_id]
    assert new.contradictions_detected[0].resolution.value == "new_wins"

    # substrate truth: CONTRADICTS edge new→old, and the old memory is invalidated (valid_to set).
    records, _, _ = memory_driver.execute_query(
        "MATCH (n:Memory {memory_id: $new})-[r:CONTRADICTS]->(o:Memory {memory_id: $old}) "
        "RETURN r.resolution AS resolution, o.valid_to AS valid_to",
        new=new.memory_id,
        old=old.memory_id,
    )
    assert len(records) == 1
    assert records[0]["resolution"] == "new_wins"
    assert records[0]["valid_to"] is not None


# ---------------------------------------------------------------- supersede chain


async def test_supersede_chain_and_temporal_reads(memory_driver) -> None:  # noqa: ANN001
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs)

    with use_organisation_context(_ctx(_ORG_A)):
        v1 = await svc.store(graph_id=graph_id, req=_create("Quota ceiling is 100 requests"))
        up1 = await svc.supersede(
            graph_id=graph_id,
            memory_id=v1.memory_id,
            req=MemoryUpdate(content="Quota ceiling is 200 requests", reason="raised"),
        )
        up2 = await svc.supersede(
            graph_id=graph_id,
            memory_id=up1.new_memory_id,
            req=MemoryUpdate(content="Quota ceiling is 400 requests"),
        )

        current = await svc.search(graph_id=graph_id, query="quota ceiling", limit=10)
        all_versions = await svc.search(
            graph_id=graph_id, query="quota ceiling", temporal="all", limit=10
        )

    # current sees ONLY the head of the chain; 'all' sees every version.
    assert [m.memory_id for m in current.memories] == [up2.new_memory_id]
    assert {m.memory_id for m in all_versions.memories} == {
        v1.memory_id,
        up1.new_memory_id,
        up2.new_memory_id,
    }
    # the chain is walkable end-to-end via SUPERSEDES, and superseding a superseded node 404s.
    records, _, _ = memory_driver.execute_query(
        "MATCH (head:Memory {memory_id: $head})-[:SUPERSEDES*2]->(tail:Memory {memory_id: $tail}) "
        "RETURN count(*) AS chains",
        head=up2.new_memory_id,
        tail=v1.memory_id,
    )
    assert records[0]["chains"] == 1
    from oraclous_knowledge_graph_service.services.memory_service import MemoryNotFound

    with use_organisation_context(_ctx(_ORG_A)):
        with pytest.raises(MemoryNotFound):
            await svc.supersede(
                graph_id=graph_id, memory_id=v1.memory_id, req=MemoryUpdate(content="stale write")
            )


# ---------------------------------------------------------------- consolidation


async def test_consolidation_merges_near_duplicate_pair(memory_driver) -> None:  # noqa: ANN001
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    table = {
        "Standup is at nine thirty": [1.0, 0.0, 0.0],
        "Standup happens at 9:30": [0.999, 0.0447, 0.0],  # cosine ≈ 0.999 → near-duplicate
        "Postgres lives on host db-2": [0.0, 1.0, 0.0],  # orthogonal → must survive
    }
    svc = _service(memory_driver, graphs, embedder=_ControlledEmbedder(table))

    with use_organisation_context(_ctx(_ORG_A)):
        keep = await svc.store(
            graph_id=graph_id,
            req=_create("Standup is at nine thirty", source=MemorySource.USER_FEEDBACK),
        )  # base 1.0 → the cluster winner
        dup = await svc.store(graph_id=graph_id, req=_create("Standup happens at 9:30"))
        other = await svc.store(graph_id=graph_id, req=_create("Postgres lives on host db-2"))

        repo = MemoryRepository(memory_driver, graph_id=str(graph_id))
        stats = run_consolidation(repo, threshold=0.92, max_memories=100)

    assert stats["merged"] == 1 and stats["clusters"] == 1
    winner = _node(memory_driver, _ORG_A, graph_id, keep.memory_id)
    loser = _node(memory_driver, _ORG_A, graph_id, dup.memory_id)
    survivor = _node(memory_driver, _ORG_A, graph_id, other.memory_id)
    assert winner is not None and loser is not None and survivor is not None
    assert winner.get("valid_to") is None  # winner stays current
    assert loser.get("valid_to") is not None  # loser invalidated
    assert survivor.get("valid_to") is None  # the distinct memory was NOT touched
    assert float(winner["base_importance"]) == 1.0  # 1.0 + 0.72 absorbed, capped at 1.0
    records, _, _ = memory_driver.execute_query(
        "MATCH (w:Memory {memory_id: $w})-[r:SUPERSEDES {reason: 'consolidation'}]->"
        "(l:Memory {memory_id: $l}) RETURN count(r) AS edges",
        w=keep.memory_id,
        l=dup.memory_id,
    )
    assert records[0]["edges"] == 1


# ---------------------------------------------------------------- cross-org isolation


async def test_cross_org_isolation_org_b_sees_nothing_of_org_a(memory_driver) -> None:  # noqa: ANN001
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs)

    with use_organisation_context(_ctx(_ORG_A)):
        secret = await svc.store(
            graph_id=graph_id, req=_create("Org A acquisition target is ACME Corp")
        )

    # 1) the graph GATE: B cannot even address A's graph (org-scoped lookup → 404-mapped error).
    with use_organisation_context(_ctx(_ORG_B)):
        with pytest.raises(GraphNotVisible):
            await svc.search(graph_id=graph_id, query="acquisition", limit=10)

    # 2) the SUBSTRATE: even if a confused deputy passed the gate (graph registered under B too),
    #    every Cypher is org-scoped — B still reads and writes nothing of A's.
    graphs.register(_ORG_B, graph_id)
    with use_organisation_context(_ctx(_ORG_B)):
        leak = await svc.search(graph_id=graph_id, query="acquisition", limit=10)
        assert leak.total == 0 and leak.memories == []
        ctx_block = await svc.context(graph_id=graph_id, query="acquisition")
        assert secret.memory_id not in ctx_block.memories_used
        from oraclous_knowledge_graph_service.services.memory_service import MemoryNotFound

        with pytest.raises(MemoryNotFound):  # B cannot invalidate A's memory either
            await svc.delete(graph_id=graph_id, memory_id=secret.memory_id)

    # A still sees its memory, untouched.
    with use_organisation_context(_ctx(_ORG_A)):
        mine = await svc.search(graph_id=graph_id, query="acquisition", limit=10)
        assert [m.memory_id for m in mine.memories] == [secret.memory_id]


# ---------------------------------------------------------------- context budget


async def test_context_block_respects_token_budget(memory_driver) -> None:  # noqa: ANN001
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs)

    with use_organisation_context(_ctx(_ORG_A)):
        for i in range(10):
            await svc.store(
                graph_id=graph_id,
                req=_create(
                    f"Inventory rule {i}: warehouse {i} replenishes stock keeping unit "
                    f"families every {i + 2} days with a safety buffer of {i * 3} units"
                ),
            )
        ctx = await svc.context(graph_id=graph_id, query="inventory warehouse", max_tokens=150)

    assert ctx.token_estimate <= 150
    assert 0 < len(ctx.memories_used) < 10  # the budget cut the list short
    assert ctx.context_block.startswith("## Relevant Memory")
    assert "**Facts:**" in ctx.context_block


# ---------------------------------------------------------------- Lucene safety (#332 HIGH-2)


@pytest.mark.parametrize(
    "adversarial",
    [
        'unbalanced "quote',
        "tier:gold AND status:active",
        "(group OR other",
        "wildcard* fuzzy~ ?:",
        "C:\\path\\to\\thing",
        "trailing AND",
        "NOT alone",
    ],
)
async def test_adversarial_query_never_500s_on_search_and_context(
    memory_driver, adversarial
) -> None:  # noqa: ANN001
    """A raw Lucene-metacharacter query must NEVER crash recall (it would parse-error → 500). It is
    sanitised to a safe literal query; the worst case degrades to zero results, never an error."""
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs, embedder=None)  # fulltext-only → exercises the index path

    with use_organisation_context(_ctx(_ORG_A)):
        await svc.store(graph_id=graph_id, req=_create("Customer tier is gold and active"))
        # neither call raises — the sanitiser makes the index query parseable.
        res = await svc.search(graph_id=graph_id, query=adversarial, limit=10)
        ctx = await svc.context(graph_id=graph_id, query=adversarial)
    assert res.total >= 0  # a valid response object, no exception
    assert ctx.context_block.startswith("## Relevant Memory")


async def test_recall_path_with_quoted_query_returns_matches(memory_driver) -> None:  # noqa: ANN001
    """The sanitiser preserves the literal TERMS — a query carrying special chars around real words
    still recalls the matching memory rather than silently returning nothing."""
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs, embedder=None)
    with use_organisation_context(_ctx(_ORG_A)):
        await svc.store(graph_id=graph_id, req=_create("Deploy window is Friday afternoon"))
        res = await svc.search(graph_id=graph_id, query='"Friday" AND afternoon', limit=10)
    assert res.total >= 1
    assert "Friday" in res.memories[0].content


# ---------------------------------------------------------------- contradiction compatibility


async def test_two_negations_of_different_objects_are_not_contradictions(memory_driver) -> None:  # noqa: ANN001
    """`X is-not Y` and `X is-not Z` are COMPATIBLE (X is neither) — storing the second must NOT
    flag a contradiction nor invalidate the first (#332 MED contradiction over-invalidation)."""
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs, embedder=None)
    with use_organisation_context(_ctx(_ORG_A)):
        first = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Customer is not gold tier",
                subject="cust-1",
                predicate="has_tier",
                object="gold",
                is_negation=True,
            ),
        )
        second = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Customer is not silver tier",
                subject="cust-1",
                predicate="has_tier",
                object="silver",
                is_negation=True,
            ),
        )
    assert second.contradictions_detected == []  # compatible negations
    assert _node(memory_driver, _ORG_A, graph_id, first.memory_id).get("valid_to") is None


async def test_assertion_vs_negation_of_different_objects_is_not_a_contradiction(
    memory_driver,
) -> None:  # noqa: ANN001
    """`X is Z` and `X is-not Y` (DIFFERENT objects) are compatible — no contradiction flagged."""
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs, embedder=None)
    with use_organisation_context(_ctx(_ORG_A)):
        await svc.store(
            graph_id=graph_id,
            req=_create(
                "Customer is not gold",
                subject="cust-2",
                predicate="has_tier",
                object="gold",
                is_negation=True,
            ),
        )
        clash = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Customer is silver",
                subject="cust-2",
                predicate="has_tier",
                object="silver",
                is_negation=False,
            ),
        )
    assert clash.contradictions_detected == []


async def test_same_object_negation_flip_is_a_contradiction(memory_driver) -> None:  # noqa: ANN001
    """`X is Y` then `X is-not Y` (SAME object, flipped negation) IS a contradiction — new wins."""
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs, embedder=None)
    with use_organisation_context(_ctx(_ORG_A)):
        asserted = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Customer is gold",
                subject="cust-3",
                predicate="has_tier",
                object="gold",
                is_negation=False,
            ),
        )
        denied = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Customer is not gold after all",
                subject="cust-3",
                predicate="has_tier",
                object="gold",
                is_negation=True,
            ),
        )
    assert [c.conflict_memory_id for c in denied.contradictions_detected] == [asserted.memory_id]
    assert _node(memory_driver, _ORG_A, graph_id, asserted.memory_id)["valid_to"] is not None


# ---------------------------------------------------------------- supersede preservation/clearing


async def test_supersede_preserves_about_links_and_clears_stale_triple(memory_driver) -> None:  # noqa: ANN001
    """The successor inherits the predecessor's ABOUT entity edge, and does NOT carry the stale
    subject/predicate/object (the MemoryUpdate body supplies none) (#332 MED supersede)."""
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs, embedder=None)
    # Seed an entity the semantic memory's subject will link to (ABOUT).
    memory_driver.execute_query(
        "CREATE (e:__Entity__ {organisation_id: $org, graph_id: $graph, id: 'ent-billing', "
        "name: 'billing'})",
        org=_ORG_A,
        graph=str(graph_id),
    )
    with use_organisation_context(_ctx(_ORG_A)):
        v1 = await svc.store(
            graph_id=graph_id,
            req=_create(
                "billing runs nightly",
                subject="billing",
                predicate="runs",
                object="nightly",
            ),
        )
        # the store linked ABOUT → entity; now supersede with new content (no triple supplied).
        up = await svc.supersede(
            graph_id=graph_id,
            memory_id=v1.memory_id,
            req=MemoryUpdate(content="billing runs hourly now", reason="reschedule"),
        )
    new_node = _node(memory_driver, _ORG_A, graph_id, up.new_memory_id)
    assert new_node is not None
    # stale triple cleared on the successor (the body supplied none).
    assert "subject" not in new_node and "predicate" not in new_node and "object" not in new_node
    # ABOUT entity context preserved onto the successor.
    records, _, _ = memory_driver.execute_query(
        "MATCH (m:Memory {memory_id: $new})-[:ABOUT]->(e:__Entity__ {id: 'ent-billing'}) "
        "RETURN count(*) AS links",
        new=up.new_memory_id,
    )
    assert records[0]["links"] == 1


# ---------------------------------------------------------------- dedup type+scope keying


async def test_same_content_different_type_does_not_dedup(memory_driver) -> None:  # noqa: ANN001
    """Identical content stored as a SEMANTIC must not fold into an existing EPISODIC — the dedup
    key includes memory_type+scope (#332 MED content-hash dedup)."""
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs, embedder=None)
    with use_organisation_context(_ctx(_ORG_A)):
        epi = await svc.store(
            graph_id=graph_id,
            req=_create("System upgraded to v2", type=MemoryType.EPISODIC),
        )
        sem = await svc.store(
            graph_id=graph_id,
            req=_create("System upgraded to v2", type=MemoryType.SEMANTIC),
        )
        # but a true re-store of the SAME type+scope DOES dedup.
        epi_again = await svc.store(
            graph_id=graph_id,
            req=_create("System upgraded to v2", type=MemoryType.EPISODIC),
        )
    assert sem.memory_id != epi.memory_id  # distinct nodes — not deduped across types
    assert epi_again.memory_id == epi.memory_id  # same type+scope → deduped


# ---------------------------------------------------------------- consolidation partition (HIGH-1)


async def test_consolidation_never_merges_across_partitions(memory_driver) -> None:  # noqa: ANN001
    """Two memories with near-identical embeddings but in DIFFERENT (type, scope, agent) partitions
    must NOT consolidate — even at cosine ≈ 1.0 (#332 HIGH-1 consolidation over-merge)."""
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    same_vec = [1.0, 0.0, 0.0]
    table = {
        "Episodic note about the deploy": same_vec,
        "Semantic fact about the deploy": same_vec,  # identical vector, different TYPE
        "Agent B note about the deploy": same_vec,  # identical vector, different AGENT
    }
    svc = _service(memory_driver, graphs, embedder=_ControlledEmbedder(table))
    with use_organisation_context(_ctx(_ORG_A)):
        epi = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Episodic note about the deploy", type=MemoryType.EPISODIC, agent_id="agent-A"
            ),
        )
        sem = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Semantic fact about the deploy", type=MemoryType.SEMANTIC, agent_id="agent-A"
            ),
        )
        other_agent = await svc.store(
            graph_id=graph_id,
            req=_create(
                "Agent B note about the deploy", type=MemoryType.EPISODIC, agent_id="agent-B"
            ),
        )
        repo = MemoryRepository(memory_driver, graph_id=str(graph_id))
        stats = run_consolidation(repo, threshold=0.92, max_memories=100)
    assert stats["merged"] == 0 and stats["clusters"] == 0  # cross-partition → nothing merges
    for mid in (epi.memory_id, sem.memory_id, other_agent.memory_id):
        assert _node(memory_driver, _ORG_A, graph_id, mid).get("valid_to") is None  # all survive


async def test_consolidation_winner_currency_skips_dead_winner(memory_driver) -> None:  # noqa: ANN001
    """merge_memories only folds into a CURRENT winner: if the winner was invalidated between the
    candidate fetch and the merge, the loser is NOT invalidated under a dead winner (#332 MED)."""
    graphs = _FakeGraphs()
    graph_id = uuid.uuid4()
    graphs.register(_ORG_A, graph_id)
    svc = _service(memory_driver, graphs, embedder=None)
    with use_organisation_context(_ctx(_ORG_A)):
        winner = await svc.store(graph_id=graph_id, req=_create("Primary note", agent_id="a"))
        loser = await svc.store(graph_id=graph_id, req=_create("Duplicate note", agent_id="a"))
    repo = MemoryRepository(memory_driver, graph_id=str(graph_id), organisation_id=_ORG_A)
    # Kill the winner (simulate it racing away), then attempt the merge.
    from datetime import datetime as _dt

    repo.soft_delete(memory_id=winner.memory_id, now=_dt.now(UTC))
    merged = repo.merge_memories(
        winner_id=winner.memory_id, loser_ids=[loser.memory_id], now=_dt.now(UTC)
    )
    assert merged == 0  # no fold into a dead winner
    assert _node(memory_driver, _ORG_A, graph_id, loser.memory_id).get("valid_to") is None
