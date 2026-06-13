"""Federated cross-graph retrieval against REAL Neo4j (#330 / ADR-026) — the no-new-access gate.

Stands up the shared substrate Neo4j (testcontainers), seeds THREE graphs under org A and ONE
juicy graph under org B (including the cross-bait of an org-B node stamped with an org-A graph
id), then drives the REAL ``FederatedRetrievalService`` over the REAL ``RetrievalRepository``
fan-out. The mandatory invariant: every mode returns results labeled from org A's graphs and
NEVER org B's — federation aggregates exactly what the caller can already read, no more. Plus:
the explicit-subset fail-closed reject, per-graph/total caps against real data, the embedder-off
clean degrade, and the federated neighborhood fetch.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_retriever_service.services.embedder import HashingEmbedder
from oraclous_knowledge_retriever_service.services.federated_service import (
    FederatedAccessError,
    FederatedRetrievalService,
)
from oraclous_knowledge_retriever_service.services.graph_registry_client import GraphInfo

if TYPE_CHECKING:
    from neo4j import Driver

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_G1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"  # org A
_G2 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"  # org A
_G3 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3"  # org A (empty of matches)
_G4 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb4"  # org B — must NEVER appear in org-A results

_EMBEDDER = HashingEmbedder(dim=64)

_A_GRAPHS = [
    GraphInfo(id=_G1, name="research"),
    GraphInfo(id=_G2, name="sales"),
    GraphInfo(id=_G3, name="ops"),
]


class _StubRegistry:
    """The enumeration seam, stubbed: returns a fixed accessible set (the live HTTP client is
    covered by its own unit suite; the KGS endpoint by the KGS suite)."""

    def __init__(self, graphs: list[GraphInfo]) -> None:
        self._graphs = graphs

    async def accessible_graphs(self, principal) -> list[GraphInfo]:
        return list(self._graphs)


def _ctx(org: uuid.UUID):
    return use_organisation_context(
        OrganisationContext(
            organisation_id=org, principal_id=org, principal_type=PrincipalType.USER
        )
    )


def _service(driver, graphs=None, *, embedder=None, **caps) -> FederatedRetrievalService:
    return FederatedRetrievalService(
        driver,
        embedder or _EMBEDDER,
        _StubRegistry(_A_GRAPHS if graphs is None else graphs),
        max_graphs=caps.get("max_graphs", 20),
        max_per_graph_k=caps.get("max_per_graph_k", 25),
        max_total=caps.get("max_total", 200),
        max_subgraph_nodes=caps.get("max_subgraph_nodes", 500),
    )


def _seed(driver: Driver) -> None:
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
        # org A — entities + embedded chunks in g1/g2, an unrelated g3
        for org, gid, suffix in ((_ORG_A, _G1, "g1"), (_ORG_A, _G2, "g2"), (_ORG_B, _G4, "g4")):
            s.run(
                "CREATE (e:Person:__Entity__ {id: $eid, name: 'ada lovelace', "
                "canonical_name: 'Ada Lovelace', aliases: ['Ada'], "
                "organisation_id: $oid, graph_id: $gid}) "
                "CREATE (c:Chunk {text: 'ada lovelace wrote the first program (' + $suffix + ')', "
                "embedding: $vec, organisation_id: $oid, graph_id: $gid}) "
                "CREATE (c)-[:MENTIONS {organisation_id: $oid, graph_id: $gid}]->(e)",
                eid=f"ada-{suffix}",
                oid=str(org),
                gid=gid,
                suffix=suffix,
                vec=_EMBEDDER.embed("ada lovelace wrote the first program"),
            )
        # the CROSS-BAIT: an org-B node deliberately stamped with org-A's g1 graph id, matchable by
        # EVERY mode — it is BOTH a canonical :__Entity__ (entity + neighborhood) AND a :Chunk with
        # text (fulltext) AND a real embedding (semantic + hybrid). Only the organisation_id
        # predicate separates it from org A's data, so it proves the org bind on EVERY branch.
        s.run(
            "CREATE (e:Person:__Entity__:Chunk {id: 'ada-bait', name: 'ada lovelace', "
            "canonical_name: 'Ada Lovelace (bait)', aliases: ['Ada'], "
            "text: 'ada lovelace wrote the first program (bait)', embedding: $vec, "
            "organisation_id: $oid, graph_id: $gid})",
            oid=str(_ORG_B),
            gid=_G1,
            vec=_EMBEDDER.embed("ada lovelace wrote the first program"),
        )
        # g3 content that should not match an 'ada' query
        s.run(
            "CREATE (e:Person:__Entity__ {id: 'zen-g3', name: 'zenith', "
            "canonical_name: 'Zenith', organisation_id: $oid, graph_id: $gid})",
            oid=str(_ORG_A),
            gid=_G3,
        )


@pytest.fixture
def seeded(neo4j_driver: Driver) -> Driver:
    _seed(neo4j_driver)
    return neo4j_driver


async def _search(driver, org=_ORG_A, **kw):
    graphs = kw.pop("graphs", None)
    defaults = dict(
        principal=None, query="ada", mode="entity", graph_ids=None, per_graph_k=10, total_k=50
    )
    defaults.update(kw)
    with _ctx(org):
        return await _service(driver, graphs).search(**defaults)


# ── the mandatory no-new-access invariant ────────────────────────────────────────────────────


async def test_entity_search_returns_org_a_graphs_and_never_org_b(seeded) -> None:
    out = await _search(seeded, mode="entity")
    sources = {r["source_graph_id"] for r in out["results"]}
    assert sources == {_G1, _G2}  # both org-A hits, labeled
    names = {r["source_graph_name"] for r in out["results"]}
    assert names == {"research", "sales"}
    # the org-B bait shares graph_id _G1 — the org predicate must keep it out
    assert all("bait" not in str(r["properties"]) for r in out["results"])
    assert all(r["properties"].get("organisation_id") != str(_ORG_B) for r in out["results"])


@pytest.mark.parametrize("mode", ["entity", "fulltext", "semantic", "hybrid"])
async def test_cross_org_bait_is_excluded_from_every_mode(seeded, mode) -> None:
    # THE mandatory isolation proof. The bait is :__Entity__ + :Chunk + embedding + text, stamped
    # with org-A's _G1 graph_id but owned by org B — so it is matchable by entity, fulltext,
    # semantic AND hybrid. Only the in-query org predicate (bound on EVERY fan-out branch) keeps it
    # out. Org A's query must never surface it in ANY mode.
    out = await _search(seeded, mode=mode, query="ada lovelace first program")
    # every hit is from an org-A graph, owned by org A
    assert all(r["source_graph_id"] in {_G1, _G2} for r in out["results"])
    assert all(r["properties"].get("organisation_id") == str(_ORG_A) for r in out["results"])
    # the bait specifically — by its property id, its tell-tale name, its org — is absent (the id
    # property survives in properties even though the result `id` is the Neo4j elementId)
    assert all(r["properties"].get("id") != "ada-bait" for r in out["results"])
    assert all("bait" not in str(r["properties"]) for r in out["results"])
    assert all(r["properties"].get("organisation_id") != str(_ORG_B) for r in out["results"])


async def test_semantic_search_is_org_scoped_and_labeled(seeded) -> None:
    out = await _search(seeded, mode="semantic", query="ada lovelace first program")
    assert out["meta"]["semantic_degraded"] is False
    sources = {r["source_graph_id"] for r in out["results"]}
    assert sources == {_G1, _G2}
    assert all(r["properties"].get("organisation_id") != str(_ORG_B) for r in out["results"])
    assert all(r["properties"]["score"] > 0.5 for r in out["results"])
    assert all("embedding" not in r["properties"] for r in out["results"])  # never echoed


async def test_fulltext_and_hybrid_are_org_scoped(seeded) -> None:
    ful = await _search(seeded, mode="fulltext")
    assert {r["source_graph_id"] for r in ful["results"]} == {_G1, _G2}
    hyb = await _search(seeded, mode="hybrid")
    assert {r["source_graph_id"] for r in hyb["results"]} == {_G1, _G2}
    assert all("rrf_score" in r["properties"] for r in hyb["results"])


async def test_org_b_caller_sees_only_its_own_graph(seeded) -> None:
    out = await _search(seeded, org=_ORG_B, graphs=[GraphInfo(id=_G4, name="b-graph")])
    assert {r["source_graph_id"] for r in out["results"]} == {_G4}
    assert all(r["properties"]["organisation_id"] == str(_ORG_B) for r in out["results"])


@pytest.mark.parametrize("mode", ["entity", "fulltext", "semantic", "hybrid"])
async def test_poisoned_registry_cannot_leak_another_orgs_graph(seeded, mode) -> None:
    # Defence in depth across EVERY mode: even if the enumeration seam handed org A a graph it does
    # not own (_G4, org B's), every fan-out branch still binds organisation_id in-query — org B's
    # rows are unreachable on the entity, fulltext, semantic AND hybrid paths.
    poisoned = [*_A_GRAPHS, GraphInfo(id=_G4, name="stolen")]
    out = await _search(seeded, graphs=poisoned, mode=mode, query="ada lovelace first program")
    assert _G4 not in {r["source_graph_id"] for r in out["results"]}
    assert all(r["properties"]["organisation_id"] == str(_ORG_A) for r in out["results"])


# ── fail-closed subset + caps ────────────────────────────────────────────────────────────────


async def test_subset_with_an_inaccessible_id_rejects_the_whole_query(seeded) -> None:
    with _ctx(_ORG_A), pytest.raises(FederatedAccessError):
        await _service(seeded).search(
            principal=None,
            query="ada",
            mode="entity",
            graph_ids=[uuid.UUID(_G1), uuid.UUID(_G4)],  # one owned + one foreign
            per_graph_k=10,
            total_k=50,
        )


async def test_caps_bound_per_graph_and_total_against_real_data(seeded) -> None:
    # seed extra entities in g1 so per_graph_k bites
    with seeded.session() as s:
        for i in range(5):
            s.run(
                "CREATE (:Thing:__Entity__ {id: $eid, name: $name, canonical_name: $name, "
                "organisation_id: $oid, graph_id: $gid})",
                eid=f"ada-extra-{i}",
                name=f"ada variant {i}",
                oid=str(_ORG_A),
                gid=_G1,
            )
    out = await _search(seeded, mode="entity", per_graph_k=2, total_k=3)
    per_graph: dict[str, int] = {}
    for r in out["results"]:
        per_graph[r["source_graph_id"]] = per_graph.get(r["source_graph_id"], 0) + 1
    assert all(count <= 2 for count in per_graph.values())  # per-graph k respected
    assert len(out["results"]) <= 3  # total cap respected


async def test_max_graphs_cap_truncates_and_reports_skipped(seeded) -> None:
    with _ctx(_ORG_A):
        svc = _service(seeded, max_graphs=2)
        out = await svc.search(
            principal=None, query="ada", mode="entity", graph_ids=None, per_graph_k=10, total_k=50
        )
    assert [g["id"] for g in out["meta"]["graphs_queried"]] == [_G1, _G2]
    assert out["meta"]["graphs_skipped"] == [_G3]


# ── degrade ──────────────────────────────────────────────────────────────────────────────────


async def test_embedder_off_degrades_semantic_but_fulltext_still_works(seeded) -> None:
    class _Broken:
        dim = 64

        def embed(self, text: str) -> list[float]:
            raise RuntimeError("embedder is off")

    with _ctx(_ORG_A):
        svc = _service(seeded, embedder=_Broken())
        sem = await svc.search(
            principal=None, query="ada", mode="semantic", graph_ids=None, per_graph_k=10, total_k=50
        )
        ful = await svc.search(
            principal=None, query="ada", mode="fulltext", graph_ids=None, per_graph_k=10, total_k=50
        )
    assert sem["results"] == [] and sem["meta"]["semantic_degraded"] is True
    assert {r["source_graph_id"] for r in ful["results"]} == {_G1, _G2}  # lexical path unharmed


# ── federated neighborhood ───────────────────────────────────────────────────────────────────


async def test_neighborhood_fetch_is_labeled_and_never_crosses_graphs(seeded) -> None:
    with _ctx(_ORG_A):
        out = await _service(seeded).neighborhood(
            principal=None, query="ada", graph_ids=None, entities_per_graph=5, limit_per_graph=20
        )
    node_sources = {n["source_graph_id"] for n in out["nodes"]}
    assert node_sources == {_G1, _G2}
    # the matched entities AND their 1-hop chunks arrive, labeled per graph
    types = {n["type"] for n in out["nodes"]}
    assert {"Person", "Chunk"} <= types
    # every edge stays inside one org-A graph (federation never fabricates cross-graph edges)
    assert out["edges"], "expected the MENTIONS edges around the matched entities"
    node_graph = {n["id"]: n["source_graph_id"] for n in out["nodes"]}
    for e in out["edges"]:
        assert e["source_graph_id"] in {_G1, _G2}
        assert node_graph[e["source"]] == node_graph[e["target"]] == e["source_graph_id"]


# ── end-to-end enumeration seam (real KGS GET /internal/v1/graphs → Postgres → fan-out) ────────


_DEV_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")  # the KGS+KRS dev-auth org
_DEV_USER = uuid.UUID("00000000-0000-0000-0000-0000000000d5")


async def test_enumeration_seam_end_to_end_through_real_kgs_and_postgres(
    neo4j_driver, postgres_async_dsn, monkeypatch
) -> None:
    """The accessible set is NOT mocked here: a real KRS GraphRegistryClient calls the real KGS
    ``GET /internal/v1/graphs`` (mounted over ASGITransport), which reads the real Postgres
    `knowledge_graphs` registry; the resolved ids then drive a real Neo4j fan-out. Proves the whole
    seam — enumeration → org-scoped registry → fan-out — binds to the caller's org with no mock in
    the loop, and a graph in ANOTHER org is neither enumerated nor reachable."""
    import httpx
    from oraclous_knowledge_graph_service.app import create_app
    from oraclous_knowledge_graph_service.core.config import get_settings as kgs_get_settings
    from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository
    from oraclous_knowledge_graph_service.repositories.models import Base
    from oraclous_knowledge_retriever_service.services.graph_registry_client import (
        GraphRegistryClient,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    # Pin KGS to dev auth for this app instance (the lru_cached settings may hold a neighbouring
    # test's gateway-mode config); dev mode forwards the fixed bearer the registry client sends.
    monkeypatch.setenv("KGS_AUTH_MODE", "dev")
    kgs_get_settings.cache_clear()

    other_org = uuid.UUID("99999999-9999-9999-9999-999999999999")
    gid_other = uuid.uuid4()

    # 1. Real Postgres registry: the dev-org graph (enumerable) + a foreign-org graph (must not be).
    engine = create_async_engine(postgres_async_dsn)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with sessionmaker() as session:
            repo = GraphRepository(session)
            with _ctx(_DEV_ORG):
                kept = await repo.create(user_id=_DEV_USER, name="federated-seam", description=None)
            with _ctx(other_org):
                await repo.create(user_id=uuid.uuid4(), name="foreign", description=None)
            await session.commit()

        # 2. Seed Neo4j: an entity in the kept (dev-org) graph + bait in the foreign one.
        with neo4j_driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")
            s.run(
                "CREATE (e:Person:__Entity__ {id: 'seam-ada', name: 'ada lovelace', "
                "canonical_name: 'Ada Lovelace', organisation_id: $oid, graph_id: $gid})",
                oid=str(_DEV_ORG),
                gid=str(kept.id),
            )
            s.run(
                "CREATE (e:Person:__Entity__ {id: 'seam-bait', name: 'ada lovelace', "
                "canonical_name: 'Bait', organisation_id: $oid, graph_id: $gid})",
                oid=str(other_org),
                gid=str(gid_other),
            )

        # 3. Mount the real KGS app and point its app-scoped sessionmaker at the test engine — so
        #    the REAL get_graph_service → bind_org_context → GraphRepository chain runs unchanged
        #    over real Postgres (dev auth resolves the org from the forwarded principal; no mock).
        app = create_app()
        app.state.sessionmaker = sessionmaker
        app.state.neo4j_driver = None  # graph CRUD falls back to stored counts; we read ids only
        transport = httpx.ASGITransport(app=app)
        kgs_client = httpx.AsyncClient(
            transport=transport, base_url="http://kgs", follow_redirects=False
        )

        # 4. The REAL KRS registry client → the real KGS endpoint (dev mode forwards the bearer).
        registry = GraphRegistryClient(
            client=kgs_client, auth_mode="dev", dev_bearer="dev-token", internal_service_key=None
        )
        accessible = await registry.accessible_graphs(principal=None)
        accessible_ids = {g.id for g in accessible}
        assert str(kept.id) in accessible_ids  # the dev-org graph was enumerated…
        assert str(gid_other) not in accessible_ids  # …the foreign-org graph was NOT

        # 5. Drive the real fan-out over the enumerated set (real Neo4j), bound to the dev org.
        svc = FederatedRetrievalService(
            neo4j_driver,
            _EMBEDDER,
            registry,
            max_graphs=20,
            max_per_graph_k=25,
            max_total=200,
            max_subgraph_nodes=500,
        )
        with _ctx(_DEV_ORG):
            out = await svc.search(
                principal=None,
                query="ada",
                mode="entity",
                graph_ids=None,
                per_graph_k=10,
                total_k=50,
            )
        sources = {r["source_graph_id"] for r in out["results"]}
        assert sources == {str(kept.id)}  # only the dev-org graph's hit
        assert all(r["properties"]["organisation_id"] == str(_DEV_ORG) for r in out["results"])
        await kgs_client.aclose()
    finally:
        await engine.dispose()
        kgs_get_settings.cache_clear()  # don't leak the pinned dev mode to neighbouring tests
