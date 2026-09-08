"""#643 + #949 — cross-organisation isolation holds on the SEMANTIC read path, whatever the
embedder identity involved (Threat T1).

Mirrors `test_multi_tenant_retriever_org_isolation.py`'s own shape (a shared `graph_id`
deliberate cross-bait between two orgs), extended for #949's C3: org A and org B each hold chunks
under BOTH the hashing and the openai identity, so a mismatch on `embedder_id` and a mismatch on
`organisation_id` are never confused with each other — an org-scoped query must never surface
another organisation's chunk, regardless of which identity it queries.

`RetrievalRepository.semantic` does not yet take an `embedder_id` parameter. The seam use below is
FUNCTION-LOCAL (`.claude/rules/tests-seam-imports.md`); this file hard-fails RED with `TypeError`
until the `[impl]` lands.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from oraclous_knowledge_retriever_service.repositories.retrieval_repository import (
    RetrievalRepository,
)

if TYPE_CHECKING:
    from neo4j import Driver

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SHARED_GRAPH_ID = "graph-shared-semantic"  # deliberate cross-bait

_HASHING_ID = "hashing:512"
_OPENAI_ID = "openai:text-embedding-3-small:512"


@pytest.fixture
def clean_neo4j(neo4j_driver: Driver) -> Driver:
    with neo4j_driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    return neo4j_driver


def _embed(text: str, dim: int = 512) -> list[float]:
    from oraclous_embedding import HashingEmbedder  # noqa: PLC0415

    (vector,) = HashingEmbedder(dim=dim).embed([text])
    return vector


def _seed(driver: Driver) -> None:
    """Both orgs hold a chunk under EACH identity, all under the SAME `graph_id` — the strongest
    cross-bait: an org-scope leak and an identity leak would otherwise be indistinguishable."""
    with driver.session() as s:
        for org, name, identity in (
            (_ORG_A, "Alice", _HASHING_ID),
            (_ORG_A, "Anne", _OPENAI_ID),
            (_ORG_B, "Bob", _HASHING_ID),
            (_ORG_B, "Beth", _OPENAI_ID),
        ):
            s.run(
                "CREATE (c:Chunk {name: $name, text: $text, organisation_id: $oid, "
                "graph_id: $gid, embedding: $e, embedder_id: $eid})",
                name=name,
                text=f"person {name}",
                oid=str(org),
                gid=_SHARED_GRAPH_ID,
                e=_embed(f"person {name}"),
                eid=identity,
            )


def _names(rows: list[dict]) -> set[str]:
    return {r["props"]["name"] for r in rows}


class TestSemanticScopedReadDoesNotCrossOrgs:
    def test_org_a_hashing_query_sees_only_org_a_hashing_chunk(self, clean_neo4j: Driver) -> None:
        _seed(clean_neo4j)
        repo = RetrievalRepository(clean_neo4j, organisation_id=str(_ORG_A))
        rows = repo.semantic(
            graph_id=_SHARED_GRAPH_ID,
            qvec=_embed("person Alice"),
            top_k=100,
            embedder_id=_HASHING_ID,
        )
        assert _names(rows) == {"Alice"}  # cross-org AND cross-identity bait both absent

    def test_org_a_openai_query_sees_only_org_a_openai_chunk(self, clean_neo4j: Driver) -> None:
        _seed(clean_neo4j)
        repo = RetrievalRepository(clean_neo4j, organisation_id=str(_ORG_A))
        rows = repo.semantic(
            graph_id=_SHARED_GRAPH_ID,
            qvec=_embed("person Anne"),
            top_k=100,
            embedder_id=_OPENAI_ID,
        )
        assert _names(rows) == {"Anne"}

    def test_org_b_hashing_query_sees_only_org_b_hashing_chunk(self, clean_neo4j: Driver) -> None:
        _seed(clean_neo4j)
        repo = RetrievalRepository(clean_neo4j, organisation_id=str(_ORG_B))
        rows = repo.semantic(
            graph_id=_SHARED_GRAPH_ID,
            qvec=_embed("person Bob"),
            top_k=100,
            embedder_id=_HASHING_ID,
        )
        assert _names(rows) == {"Bob"}

    def test_org_b_openai_query_sees_only_org_b_openai_chunk(self, clean_neo4j: Driver) -> None:
        _seed(clean_neo4j)
        repo = RetrievalRepository(clean_neo4j, organisation_id=str(_ORG_B))
        rows = repo.semantic(
            graph_id=_SHARED_GRAPH_ID,
            qvec=_embed("person Beth"),
            top_k=100,
            embedder_id=_OPENAI_ID,
        )
        assert _names(rows) == {"Beth"}

    def test_an_unseen_org_gets_no_rows_under_any_identity(self, clean_neo4j: Driver) -> None:
        _seed(clean_neo4j)
        unseen = uuid.UUID("33333333-3333-3333-3333-333333333333")
        repo = RetrievalRepository(clean_neo4j, organisation_id=str(unseen))
        for identity in (_HASHING_ID, _OPENAI_ID):
            rows = repo.semantic(
                graph_id=_SHARED_GRAPH_ID,
                qvec=_embed("person Alice"),
                top_k=100,
                embedder_id=identity,
            )
            assert rows == []
