"""Retriever-layer organisation isolation, proven at the data layer (Threat T1).

Rebuilt for R3.5 (service #2): the retriever is now the §21 ``RetrievalRepository``
(``oraclous_knowledge_retriever_service.repositories.retrieval_repository``) — the single
Neo4j-driver seam, which binds ``organisation_id`` (from the fail-closed governance context)
AND ``graph_id`` as parameters into *every* read query. This gate proves the same invariant
against the new architecture: an org-A read never reaches org-B's nodes, even under a
deliberately *shared ``graph_id``*.

Stands up real Neo4j (the session-scoped ``neo4j_driver`` fixture in ``tests/conftest.py``),
seeds two organisations' ``:Chunk`` nodes with the cross-bait of the same ``graph_id`` in both
orgs, and asserts that the repository's org-scoped read returns only the caller's org. The writer
half of this gate is in the companion ``test_multi_tenant_writer_org_isolation.py``.
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
_SHARED_GRAPH_ID = "graph-shared"  # deliberate cross-bait


@pytest.fixture
def clean_neo4j(neo4j_driver: Driver) -> Driver:
    """Wipe nodes/rels created by these tests so each test sees a fresh DB."""
    with neo4j_driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    return neo4j_driver


def _seed_two_orgs(driver: Driver) -> None:
    """Both orgs' chunks share one ``graph_id`` — only ``organisation_id`` separates them."""
    with driver.session() as s:
        for org, names in ((_ORG_A, ["Alice", "Anne"]), (_ORG_B, ["Bob", "Beth"])):
            for n in names:
                s.run(
                    "CREATE (c:Chunk {name: $name, text: $text, "
                    "organisation_id: $oid, graph_id: $gid})",
                    name=n,
                    text=f"person {n}",
                    oid=str(org),
                    gid=_SHARED_GRAPH_ID,
                )


def _names(rows: list[dict]) -> set[str]:
    return {r["props"]["name"] for r in rows}


class TestScopedReadDoesNotCrossOrgs:
    """The repository's org-scoping is restrictive, not advisory — under one shared
    ``graph_id``, an org-scoped read sees only its own org."""

    def test_org_a_read_returns_only_org_a(self, clean_neo4j: Driver) -> None:
        _seed_two_orgs(clean_neo4j)
        repo = RetrievalRepository(clean_neo4j, organisation_id=str(_ORG_A))
        rows = repo.fulltext(graph_id=_SHARED_GRAPH_ID, query="person", top_k=100)
        assert _names(rows) == {"Alice", "Anne"}  # cross-bait org B absent

    def test_org_b_read_returns_only_org_b(self, clean_neo4j: Driver) -> None:
        _seed_two_orgs(clean_neo4j)
        repo = RetrievalRepository(clean_neo4j, organisation_id=str(_ORG_B))
        rows = repo.fulltext(graph_id=_SHARED_GRAPH_ID, query="person", top_k=100)
        assert _names(rows) == {"Bob", "Beth"}

    def test_unseen_org_returns_no_rows(self, clean_neo4j: Driver) -> None:
        """A scope with no rows must produce an empty result, not the cross-org rows."""
        _seed_two_orgs(clean_neo4j)
        unseen = uuid.UUID("33333333-3333-3333-3333-333333333333")
        repo = RetrievalRepository(clean_neo4j, organisation_id=str(unseen))
        rows = repo.fulltext(graph_id=_SHARED_GRAPH_ID, query="person", top_k=100)
        assert rows == []
