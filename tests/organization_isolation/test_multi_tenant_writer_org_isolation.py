"""Writer-layer organisation isolation, proven at the data layer
(ORA-18 / Epic A3, on the ORA-12 substrate harness — writer half).

Stands up real Neo4j (via the session-scoped ``neo4j_driver`` fixture in
``tests/conftest.py``), seeds two organisations' nodes with the deliberate
cross-bait of the *same ``graph_id`` value in both orgs*, and exercises the
new ``OrganisationScopedKGWriter`` boundary, asserting that no writer
configured for org A ever lays down a node or relationship carrying any other
organisation's id.

Threat: T1. The organisation-id boundary is enforced at the data layer here,
above the legacy ``graph_id`` boundary, so that even when two organisations
happen to use overlapping ``graph_id`` values the data cannot cross.

Module placement: ``solution-architect`` ratified Option B (split) on
31 May 2026 — the writer lives in ``knowledge-graph-service``. The retriever
half of this gate is in the companion file
``test_multi_tenant_retriever_org_isolation.py`` in this directory. Both
consume the substrate seam ``oraclous_substrate.access`` per
[ADR-012](https://oraclous.atlassian.net/wiki/spaces/OP/pages/2490396) §1
(`scoped_write_node` is the sole Neo4j write control — Neo4j has no RLS
WITH-CHECK backstop, so this test proves the control).

RED until:
  * ``oraclous_governance.context`` exposes ``OrganisationContext`` (ORA-14)
  * ``oraclous_knowledge_graph_service.multi_tenant`` exposes
    ``OrganisationScopedKGWriter`` (ORA-18 impl)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from neo4j import Driver

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]


_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SHARED_GRAPH_ID = "graph-shared"  # deliberate cross-bait
_PRINCIPAL = uuid.UUID("99999999-9999-9999-9999-999999999999")


def _context(organisation_id: uuid.UUID):
    """Local import keeps the not-yet-built seam out of module-level collection
    (ORA-48 / TST001)."""
    from oraclous_governance.context import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=organisation_id,
        principal_id=_PRINCIPAL,
        principal_type=PrincipalType.USER,
    )


def _writer_cls():
    from oraclous_knowledge_graph_service.multi_tenant import (
        OrganisationScopedKGWriter,
    )

    return OrganisationScopedKGWriter


# ---------------------------------------------------------------------------
# Test doubles (avoid neo4j-graphrag dep at integration-test time)
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    id: str
    label: str
    properties: dict[str, Any] | None = None


@dataclass
class _Rel:
    type: str
    start_node_id: str
    end_node_id: str
    properties: dict[str, Any] | None = None


@dataclass
class _Graph:
    nodes: list[_Node] = field(default_factory=list)
    relationships: list[_Rel] = field(default_factory=list)


class _DirectCypherWriter:
    """Stand-in base writer that MERGE-writes nodes through the live driver.

    The writer-under-test wraps this and is responsible for injecting
    ``organisation_id`` / ``graph_id`` on the graph before this is called —
    so anything missing here is a wrapper bug.
    """

    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    async def run(self, graph: _Graph) -> None:
        with self.driver.session() as s:
            for n in graph.nodes:
                s.run(
                    f"MERGE (x:`{n.label}` {{id: $id}}) SET x += $props",
                    id=n.id,
                    props=n.properties or {},
                )
            for r in graph.relationships:
                s.run(
                    f"MATCH (a {{id: $a}}), (b {{id: $b}}) "
                    f"MERGE (a)-[r:`{r.type}`]->(b) SET r += $props",
                    a=r.start_node_id,
                    b=r.end_node_id,
                    props=r.properties or {},
                )


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
# Writer: scoped writes carry the right organisation_id, never the other one
# ---------------------------------------------------------------------------


class TestWriterStampsCorrectOrganisationId:
    """The wrapper's job is to stamp ``organisation_id`` on every node and
    relationship before the base writer touches Neo4j. This test proves the
    stamp is present and correct in the actual database."""

    async def test_writer_for_org_a_writes_nodes_carrying_only_org_a(
        self, clean_neo4j: Driver
    ) -> None:
        Writer = _writer_cls()
        writer = Writer(
            base_writer=_DirectCypherWriter(clean_neo4j),
            context=_context(_ORG_A),
            graph_id=_SHARED_GRAPH_ID,
        )
        graph = _Graph(
            nodes=[
                _Node(id="a1", label="Person", properties={"name": "Alice"}),
                _Node(id="a2", label="Person", properties={"name": "Anne"}),
            ]
        )

        await writer.run(graph)

        records, _, _ = clean_neo4j.execute_query(
            "MATCH (n:Person) RETURN n.organisation_id AS org, n.name AS name"
        )
        rows = {(r["org"], r["name"]) for r in records}
        assert rows == {(str(_ORG_A), "Alice"), (str(_ORG_A), "Anne")}

    async def test_two_orgs_write_under_same_graph_id_remain_partitioned(
        self, clean_neo4j: Driver
    ) -> None:
        """Cross-bait: both orgs share ``graph_id``. Org A's read must not see
        org B's row even though ``graph_id`` collides."""
        Writer = _writer_cls()
        for org, name in ((_ORG_A, "Alice"), (_ORG_B, "Bob")):
            writer = Writer(
                base_writer=_DirectCypherWriter(clean_neo4j),
                context=_context(org),
                graph_id=_SHARED_GRAPH_ID,
            )
            await writer.run(
                _Graph(nodes=[_Node(id=name.lower(), label="Person", properties={"name": name})])
            )

        # Org A's view
        a_rows, _, _ = clean_neo4j.execute_query(
            "MATCH (n:Person) WHERE n.organisation_id = $oid RETURN n.name AS name",
            oid=str(_ORG_A),
        )
        assert {r["name"] for r in a_rows} == {"Alice"}

        # Org B's view
        b_rows, _, _ = clean_neo4j.execute_query(
            "MATCH (n:Person) WHERE n.organisation_id = $oid RETURN n.name AS name",
            oid=str(_ORG_B),
        )
        assert {r["name"] for r in b_rows} == {"Bob"}

    async def test_writer_for_org_a_cannot_be_tricked_via_node_properties(
        self, clean_neo4j: Driver
    ) -> None:
        """A node whose properties carry a foreign ``organisation_id`` MUST be
        rewritten to org A's id before persistence (T1)."""
        Writer = _writer_cls()
        writer = Writer(
            base_writer=_DirectCypherWriter(clean_neo4j),
            context=_context(_ORG_A),
            graph_id=_SHARED_GRAPH_ID,
        )
        graph = _Graph(
            nodes=[
                _Node(
                    id="trick",
                    label="Person",
                    properties={
                        "name": "Mallory",
                        "organisation_id": str(_ORG_B),
                    },
                )
            ]
        )

        await writer.run(graph)

        records, _, _ = clean_neo4j.execute_query(
            "MATCH (n:Person {id: 'trick'}) RETURN n.organisation_id AS org"
        )
        assert records[0]["org"] == str(_ORG_A)

    async def test_relationships_carry_writer_organisation_id_not_caller_value(
        self, clean_neo4j: Driver
    ) -> None:
        Writer = _writer_cls()
        writer = Writer(
            base_writer=_DirectCypherWriter(clean_neo4j),
            context=_context(_ORG_A),
            graph_id=_SHARED_GRAPH_ID,
        )
        graph = _Graph(
            nodes=[
                _Node(id="a", label="Person", properties={"name": "Alice"}),
                _Node(id="b", label="Person", properties={"name": "Bob"}),
            ],
            relationships=[
                _Rel(
                    type="KNOWS",
                    start_node_id="a",
                    end_node_id="b",
                    properties={"organisation_id": str(_ORG_B)},
                )
            ],
        )

        await writer.run(graph)

        records, _, _ = clean_neo4j.execute_query(
            "MATCH ()-[r:KNOWS]->() RETURN r.organisation_id AS org"
        )
        assert records[0]["org"] == str(_ORG_A)
