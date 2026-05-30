"""Retriever-layer organisation isolation, proven at the data layer
(ORA-18 / Epic A3, on the ORA-12 substrate harness — retriever half).

Acceptance criterion (ORA-18): "Retriever-level
``@pytest.mark.organization_isolation`` tests on 0d prove no cross-org
retrieval."

Stands up real Neo4j (via the session-scoped ``neo4j_driver`` fixture in
``tests/conftest.py``), seeds two organisations' nodes with the deliberate
cross-bait of the *same ``graph_id`` value in both orgs*, and exercises the
new ``OrganisationScopedVectorCypherRetriever`` Cypher boundary, asserting
that no org-A query ever reaches org-B's nodes and vice versa.

Threat: T1. The organisation-id boundary is enforced at the data layer here,
above the legacy ``graph_id`` boundary, so that even when two organisations
happen to use overlapping ``graph_id`` values the data cannot cross.

Module placement: ``solution-architect`` ratified Option B (split) on
31 May 2026 — the three retrievers live in ``knowledge-retriever-service``.
The writer half of this gate is in the companion file
``test_multi_tenant_writer_org_isolation.py`` in this directory. Both consume
the substrate seam ``oraclous_substrate.access`` per
[ADR-012](https://oraclous.atlassian.net/wiki/spaces/OP/pages/2490396) §1.

RED until:
  * ``oraclous_governance.context`` exposes ``OrganisationContext`` (ORA-14)
  * ``oraclous_knowledge_retriever_service.multi_tenant`` exposes
    ``OrganisationScopedVectorCypherRetriever`` (ORA-18 impl)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from neo4j import Driver

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]


_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SHARED_GRAPH_ID = "graph-shared"  # deliberate cross-bait


def _cypher_retriever_cls():
    """Local import keeps the not-yet-built seam out of module-level collection
    (ORA-48 / TST001)."""
    from oraclous_knowledge_retriever_service.multi_tenant import (
        OrganisationScopedVectorCypherRetriever,
    )

    return OrganisationScopedVectorCypherRetriever


# ---------------------------------------------------------------------------
# Fixture: clean slate per test
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_neo4j(neo4j_driver: Driver) -> Driver:
    """Wipe nodes/rels created by these tests so each test sees a fresh DB."""
    with neo4j_driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    return neo4j_driver


# ---------------------------------------------------------------------------
# Cypher path: scoped retrieval never crosses org boundary
# ---------------------------------------------------------------------------


class TestScopedCypherDoesNotCrossOrgs:
    """Drive the new ``OrganisationScopedVectorCypherRetriever``'s scoping
    primitive against a Neo4j seeded with both orgs under one ``graph_id``."""

    async def _seed_two_orgs(self, driver: Driver) -> None:
        with driver.session() as s:
            for org, names in (
                (_ORG_A, ["Alice", "Anne"]),
                (_ORG_B, ["Bob", "Beth"]),
            ):
                for n in names:
                    s.run(
                        "CREATE (e:Entity {name: $name, organisation_id: $oid, graph_id: $gid})",
                        name=n,
                        oid=str(org),
                        gid=_SHARED_GRAPH_ID,
                    )

    async def test_scoped_query_for_org_a_returns_only_org_a_entities(
        self, clean_neo4j: Driver
    ) -> None:
        """The scoped query is the user's template with the wrapper's WHERE
        clause spliced in. We execute it against real Neo4j and assert the
        cross-bait org B rows are absent."""
        await self._seed_two_orgs(clean_neo4j)
        Retriever = _cypher_retriever_cls()

        scoped_query = Retriever.build_scoped_query("MATCH (node:Entity) RETURN node.name AS name")
        records, _, _ = clean_neo4j.execute_query(
            scoped_query,
            parameters_={
                "organisation_id": str(_ORG_A),
                "graph_id": _SHARED_GRAPH_ID,
            },
        )
        names = {r["name"] for r in records}
        assert names == {"Alice", "Anne"}
        # Cross-bait must not leak under shared graph_id
        assert "Bob" not in names and "Beth" not in names

    async def test_scoped_query_for_org_b_returns_only_org_b_entities(
        self, clean_neo4j: Driver
    ) -> None:
        await self._seed_two_orgs(clean_neo4j)
        Retriever = _cypher_retriever_cls()

        scoped_query = Retriever.build_scoped_query("MATCH (node:Entity) RETURN node.name AS name")
        records, _, _ = clean_neo4j.execute_query(
            scoped_query,
            parameters_={
                "organisation_id": str(_ORG_B),
                "graph_id": _SHARED_GRAPH_ID,
            },
        )
        names = {r["name"] for r in records}
        assert names == {"Bob", "Beth"}
        assert "Alice" not in names and "Anne" not in names

    async def test_scoped_query_with_no_matching_org_returns_no_rows(
        self, clean_neo4j: Driver
    ) -> None:
        """A scope with no rows must produce an empty result, not the cross-org
        rows (i.e. the WHERE clause is restrictive, not advisory)."""
        await self._seed_two_orgs(clean_neo4j)
        Retriever = _cypher_retriever_cls()
        unseen_org = uuid.UUID("33333333-3333-3333-3333-333333333333")

        scoped_query = Retriever.build_scoped_query("MATCH (node:Entity) RETURN node.name AS name")
        records, _, _ = clean_neo4j.execute_query(
            scoped_query,
            parameters_={
                "organisation_id": str(unseen_org),
                "graph_id": _SHARED_GRAPH_ID,
            },
        )
        assert records == []
