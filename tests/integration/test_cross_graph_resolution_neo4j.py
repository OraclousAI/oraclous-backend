"""Cross-graph SAME_AS resolution against REAL Neo4j (#330 / ADR-026) — the substrate half.

Seeds TWO graphs under org A (overlapping entities: an exact canonical-key twin + a near-name)
and ONE bait graph under org B with the same names, then drives the REAL repository surface the
HITL pipeline uses: candidate generation writes `SAME_AS_CANDIDATE` edges carrying BOTH graph
ids; an approve LINKS (`SAME_AS`, both nodes survive — never a cross-graph fold); a reject
suppresses (`NOT_SAME_AS`) and re-generation skips the pair; and a cross-ORG pair is unmatchable
at the Cypher level — the org predicate on both endpoints makes org B's twin invisible. (The
owner-gate + audit half lives in the KGS unit suite; the HTTP layer in the KGS integration
suite.)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
)
from oraclous_knowledge_graph_service.services.cross_graph_resolution import (
    generate_cross_graph_pairs,
)
from oraclous_knowledge_graph_service.services.embedder import HashingEmbedder

if TYPE_CHECKING:
    from neo4j import Driver

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_G1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"  # org A
_G2 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"  # org A
_G3 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb3"  # org B (bait: same entity names)

# 'acme international corp' vs 'acme international corp ltd' — hashing-embedder cosine
# 3/sqrt(12) ≈ 0.866: inside the [0.85, 1) candidate band, so the embedding stage flags it.
_NAME_EXACT = "acme corp"
_NAME_NEAR_A = "acme international corp"
_NAME_NEAR_B = "acme international corp ltd"


def _entity(s, *, eid: str, name: str, org: uuid.UUID, gid: str) -> None:
    s.run(
        "CREATE (:Company:__Entity__ {id: $eid, name: $name, canonical_name: $name, "
        "aliases: [$name], organisation_id: $oid, graph_id: $gid})",
        eid=eid,
        name=name,
        oid=str(org),
        gid=gid,
    )


@pytest.fixture
def seeded(neo4j_driver: Driver) -> Driver:
    with neo4j_driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
        _entity(s, eid="g1-acme", name=_NAME_EXACT, org=_ORG_A, gid=_G1)
        _entity(s, eid="g1-near", name=_NAME_NEAR_A, org=_ORG_A, gid=_G1)
        _entity(s, eid="g2-acme", name=_NAME_EXACT, org=_ORG_A, gid=_G2)
        _entity(s, eid="g2-near", name=_NAME_NEAR_B, org=_ORG_A, gid=_G2)
        # an attached edge so we can prove an approve-link never re-points/folds anything
        s.run(
            "MATCH (e:__Entity__ {id: 'g1-acme'}) "
            "CREATE (c:Chunk {text: 'about acme', organisation_id: $oid, graph_id: $gid})"
            "-[:MENTIONS {organisation_id: $oid, graph_id: $gid}]->(e)",
            oid=str(_ORG_A),
            gid=_G1,
        )
        # org-B bait: the SAME names in another org
        _entity(s, eid="g3-acme", name=_NAME_EXACT, org=_ORG_B, gid=_G3)
    return neo4j_driver


def _repo(driver: Driver) -> GraphWriteRepository:
    return GraphWriteRepository(driver)


def _generate_and_write(repo: GraphWriteRepository, org: uuid.UUID, gid_a: str, gid_b: str):
    entities_a = repo.cross_graph_entities(graph_id=gid_a, organisation_id=str(org), limit=1000)
    entities_b = repo.cross_graph_entities(graph_id=gid_b, organisation_id=str(org), limit=1000)
    candidates, warnings = generate_cross_graph_pairs(
        graph_id_a=gid_a,
        entities_a=entities_a,
        graph_id_b=gid_b,
        entities_b=entities_b,
        candidate_threshold=0.85,
        embedder=HashingEmbedder(dim=512),
        limit=100,
    )
    written = repo.write_cross_graph_candidates(
        organisation_id=str(org),
        pairs=[
            {
                "id_a": c.node_id_a,
                "graph_id_a": c.graph_id_a,
                "id_b": c.node_id_b,
                "graph_id_b": c.graph_id_b,
                "score": c.score,
                "method": c.method,
            }
            for c in candidates
        ],
    )
    return candidates, written, warnings


def _edges(driver: Driver, rel: str) -> list[dict]:
    with driver.session() as s:
        return [
            r.data()
            for r in s.run(
                f"MATCH (a)-[r:{rel}]-(b) WHERE a.id < b.id "  # noqa: S608 — test-fixed rel type
                "RETURN a.id AS a, b.id AS b, properties(r) AS props"
            )
        ]


def test_generation_writes_candidates_with_both_graph_ids(seeded) -> None:
    repo = _repo(seeded)
    candidates, written, warnings = _generate_and_write(repo, _ORG_A, _G1, _G2)
    assert warnings == []
    by_pair = {(c.node_id_a, c.node_id_b): c for c in candidates}
    exact = by_pair[("g1-acme", "g2-acme")]
    assert exact.method == "canonical_key" and exact.score == 1.0
    near = by_pair[("g1-near", "g2-near")]
    assert near.method == "embedding" and 0.85 <= near.score < 1.0
    assert written == 2
    edges = _edges(seeded, "SAME_AS_CANDIDATE")
    assert len(edges) == 2
    for e in edges:
        # BOTH graph ids carried on the candidate edge (ADR-026)
        assert e["props"]["graph_id_a"] == _G1 and e["props"]["graph_id_b"] == _G2
        assert e["props"]["cross_graph"] is True
        assert e["props"]["organisation_id"] == str(_ORG_A)


def test_cross_org_pairs_are_impossible_at_the_substrate(seeded) -> None:
    repo = _repo(seeded)
    # 1. An org-A write naming org-B's twin matches nothing (org predicate on BOTH endpoints).
    written = repo.write_cross_graph_candidates(
        organisation_id=str(_ORG_A),
        pairs=[
            {
                "id_a": "g1-acme",
                "graph_id_a": _G1,
                "id_b": "g3-acme",
                "graph_id_b": _G3,
                "score": 1.0,
                "method": "canonical_key",
            }
        ],
    )
    assert written == 0
    assert _edges(seeded, "SAME_AS_CANDIDATE") == []
    # 2. Enumeration is org-scoped: org A scanning org B's graph id sees nothing.
    assert repo.cross_graph_entities(graph_id=_G3, organisation_id=str(_ORG_A), limit=10) == []
    # 3. The verdict lookup cannot resolve a cross-org pair either.
    assert (
        repo.candidate_endpoints_pair(
            organisation_id=str(_ORG_A),
            graph_id_a=_G1,
            node_id_a="g1-acme",
            graph_id_b=_G3,
            node_id_b="g3-acme",
        )
        is None
    )


def test_approve_links_both_nodes_survive_and_nothing_folds(seeded) -> None:
    repo = _repo(seeded)
    _generate_and_write(repo, _ORG_A, _G1, _G2)
    endpoints = repo.candidate_endpoints_pair(
        organisation_id=str(_ORG_A),
        graph_id_a=_G1,
        node_id_a="g1-acme",
        graph_id_b=_G2,
        node_id_b="g2-acme",
    )
    assert endpoints is not None and endpoints["id_a"] == "g1-acme"

    linked = repo.link_candidate(
        organisation_id=str(_ORG_A),
        graph_id_a=_G1,
        node_id_a="g1-acme",
        graph_id_b=_G2,
        node_id_b="g2-acme",
    )
    assert linked is True
    same_as = _edges(seeded, "SAME_AS")
    assert len(same_as) == 1
    props = same_as[0]["props"]
    assert props["graph_id_a"] == _G1 and props["graph_id_b"] == _G2
    assert props["confidence"] == 1.0 and props["cross_graph"] is True
    # the approved candidate edge is gone; the near-name pair is still pending review
    pending = _edges(seeded, "SAME_AS_CANDIDATE")
    assert [(e["a"], e["b"]) for e in pending] == [("g1-near", "g2-near")]
    # BOTH nodes survive in their own graphs — a link, never a cross-graph fold
    with seeded.session() as s:
        rows = s.run(
            "MATCH (e:__Entity__) WHERE e.id IN ['g1-acme', 'g2-acme'] "
            "RETURN e.id AS id, e.graph_id AS gid ORDER BY id"
        ).data()
    assert [(r["id"], r["gid"]) for r in rows] == [("g1-acme", _G1), ("g2-acme", _G2)]
    # the g1 node's MENTIONS edge is untouched (no re-pointing happened)
    with seeded.session() as s:
        count = s.run(
            "MATCH (:Chunk)-[m:MENTIONS]->(e:__Entity__ {id: 'g1-acme'}) RETURN count(m) AS c"
        ).single()["c"]
    assert count == 1


def test_reversed_direction_regeneration_does_not_duplicate_the_edge(seeded) -> None:
    # The candidate MERGE canonicalises direction by node id, so generating A×B then B×A writes
    # ONE SAME_AS_CANDIDATE edge per pair, not two. (Pre-fix: the directed MERGE `(a)->(b)` and
    # `(b)->(a)` were distinct edges, so the reverse pass duplicated.)
    repo = _repo(seeded)
    _generate_and_write(repo, _ORG_A, _G1, _G2)  # A × B
    _generate_and_write(repo, _ORG_A, _G2, _G1)  # B × A (reversed) — must not duplicate
    edges = _edges(seeded, "SAME_AS_CANDIDATE")
    pairs = {tuple(sorted((e["a"], e["b"]))) for e in edges}
    assert len(edges) == len(pairs)  # no duplicate per pair
    assert pairs == {("g1-acme", "g2-acme"), ("g1-near", "g2-near")}


def test_verdicted_pairs_are_reported_for_response_filtering(seeded) -> None:
    repo = _repo(seeded)
    _generate_and_write(repo, _ORG_A, _G1, _G2)
    # approve the exact pair, reject the near pair — both then count as verdicted
    repo.link_candidate(
        organisation_id=str(_ORG_A),
        graph_id_a=_G1,
        node_id_a="g1-acme",
        graph_id_b=_G2,
        node_id_b="g2-acme",
    )
    repo.suppress_candidate_pair(
        organisation_id=str(_ORG_A),
        graph_id_a=_G1,
        node_id_a="g1-near",
        graph_id_b=_G2,
        node_id_b="g2-near",
    )
    verdicted = set(
        repo.verdicted_cross_graph_pairs(
            organisation_id=str(_ORG_A), graph_id_a=_G1, graph_id_b=_G2
        )
    )
    assert verdicted == {
        tuple(sorted(("g1-acme", "g2-acme"))),
        tuple(sorted(("g1-near", "g2-near"))),
    }
    # a cross-org probe finds nothing (org predicate on both endpoints)
    assert (
        repo.verdicted_cross_graph_pairs(
            organisation_id=str(_ORG_A), graph_id_a=_G1, graph_id_b=_G3
        )
        == []
    )


def test_pending_cross_graph_read_surface_lists_the_queue(seeded) -> None:
    repo = _repo(seeded)
    _generate_and_write(repo, _ORG_A, _G1, _G2)
    pending = repo.pending_cross_graph_candidates(
        organisation_id=str(_ORG_A), graph_id=_G1, limit=100
    )
    pairs = {tuple(sorted((p["id_a"], p["id_b"]))) for p in pending}
    assert pairs == {("g1-acme", "g2-acme"), ("g1-near", "g2-near")}
    # both graph ids are carried on each pending row (ADR-026)
    for p in pending:
        assert {p["graph_id_a"], p["graph_id_b"]} == {_G1, _G2}
    # org-scoped: org B cannot read org A's pending queue
    assert (
        repo.pending_cross_graph_candidates(organisation_id=str(_ORG_B), graph_id=_G1, limit=100)
        == []
    )


def test_reject_suppresses_and_regeneration_skips_the_pair(seeded) -> None:
    repo = _repo(seeded)
    _generate_and_write(repo, _ORG_A, _G1, _G2)
    suppressed = repo.suppress_candidate_pair(
        organisation_id=str(_ORG_A),
        graph_id_a=_G1,
        node_id_a="g1-near",
        graph_id_b=_G2,
        node_id_b="g2-near",
    )
    assert suppressed is True
    not_same = _edges(seeded, "NOT_SAME_AS")
    assert len(not_same) == 1
    assert not_same[0]["props"]["graph_id_a"] == _G1
    assert not_same[0]["props"]["graph_id_b"] == _G2
    # the rejected pair left the queue…
    pending = {(e["a"], e["b"]) for e in _edges(seeded, "SAME_AS_CANDIDATE")}
    assert ("g1-near", "g2-near") not in pending
    # …and a re-generation does NOT resurrect it (the NOT_SAME_AS skip)
    _generate_and_write(repo, _ORG_A, _G1, _G2)
    pending = {(e["a"], e["b"]) for e in _edges(seeded, "SAME_AS_CANDIDATE")}
    assert ("g1-near", "g2-near") not in pending
    assert ("g1-acme", "g2-acme") in pending  # the undecided pair is still flagged
