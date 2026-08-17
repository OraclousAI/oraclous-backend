"""The graph-delete cascade never crosses an organisation boundary (#817).

Stands up real Neo4j (the session-scoped ``neo4j_driver`` fixture in ``tests/conftest.py``) and
seeds the deliberate cross-bait: two organisations holding nodes that carry the **same
``graph_id`` value**. Org A then deletes that graph. Org B's node must survive.

Why the repository is driven directly rather than through the service. The live path
(``GraphService.delete_graph``) does a Postgres ownership check first, and that check is what keeps
today's exposure low. It is also in a different database and asserts nothing about the Cypher. If
this test ran through the service, the Postgres check would pass the test no matter what the query
said — which is precisely the confusion that let the defect ship. Driving
``GraphWriteRepository.delete_graph_nodes`` directly puts the Cypher predicate, and only the Cypher
predicate, under test (#817 acceptance criterion 4).

Neo4j has no row-level security and that is a recorded decision, not an oversight
(``migrations/versions/0007_enable_rls.py:34-36``). So there is no second line here: the
``organisation_id`` property in the Cypher IS the tenancy control for the graph store.

Threat: T1 (cross-tenant data access). ADR-006, ADR-012 §1b.

RED until ``delete_graph_nodes`` applies ``enforced_organisation_id()``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import use_organisation_context

if TYPE_CHECKING:
    from neo4j import Driver

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation, pytest.mark.security]

_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _ctx(org: uuid.UUID) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=org, principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


def _seed(driver: Driver, *, marker: str, graph_id: str) -> None:
    """One :Chunk per organisation, both stamped with the SAME graph_id (the cross-bait)."""
    driver.execute_query(
        "CREATE (:Chunk {marker: $marker, graph_id: $graph_id, organisation_id: $org_a, "
        "         owner: 'A'}) ",
        marker=marker,
        graph_id=graph_id,
        org_a=str(_ORG_A),
    )
    driver.execute_query(
        "CREATE (:Chunk {marker: $marker, graph_id: $graph_id, organisation_id: $org_b, "
        "         owner: 'B'}) ",
        marker=marker,
        graph_id=graph_id,
        org_b=str(_ORG_B),
    )


def _owners(driver: Driver, *, marker: str) -> list[str]:
    records, _, _ = driver.execute_query(
        "MATCH (n:Chunk {marker: $marker}) RETURN n.owner AS owner ORDER BY owner",
        marker=marker,
    )
    return [r["owner"] for r in records]


def _cleanup(driver: Driver, *, marker: str) -> None:
    driver.execute_query("MATCH (n:Chunk {marker: $marker}) DETACH DELETE n", marker=marker)


def test_org_a_deleting_its_graph_leaves_org_b_nodes_intact(neo4j_driver: Driver) -> None:
    """THE PROOF: a delete issued under org A's context must not reach org B's node."""
    from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
        GraphWriteRepository,
    )

    marker = f"org817-{uuid.uuid4()}"
    graph_id = str(uuid.uuid4())
    _seed(neo4j_driver, marker=marker, graph_id=graph_id)
    assert _owners(neo4j_driver, marker=marker) == ["A", "B"], "seed did not land"

    repo = GraphWriteRepository(neo4j_driver, database=None)
    try:
        with use_organisation_context(_ctx(_ORG_A)):
            deleted = repo.delete_graph_nodes(graph_id=graph_id)

        assert _owners(neo4j_driver, marker=marker) == ["B"], (
            "org B's node was deleted by org A's graph-delete — the cascade is org-blind"
        )
        assert deleted == 1, f"the reported count must exclude the other org's node, got {deleted}"
    finally:
        _cleanup(neo4j_driver, marker=marker)


def test_org_b_can_still_delete_its_own_half_of_the_shared_graph_id(neo4j_driver: Driver) -> None:
    """The filter narrows the blast radius without breaking the feature.

    After org A's delete, org B deleting the same graph_id under its own context must remove its
    own node. A fix that simply refused the overlapping id would pass the test above and break
    real deletes; this pins that it does not.
    """
    from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
        GraphWriteRepository,
    )

    marker = f"org817-{uuid.uuid4()}"
    graph_id = str(uuid.uuid4())
    _seed(neo4j_driver, marker=marker, graph_id=graph_id)

    repo = GraphWriteRepository(neo4j_driver, database=None)
    try:
        with use_organisation_context(_ctx(_ORG_A)):
            repo.delete_graph_nodes(graph_id=graph_id)
        with use_organisation_context(_ctx(_ORG_B)):
            deleted = repo.delete_graph_nodes(graph_id=graph_id)

        assert deleted == 1
        assert _owners(neo4j_driver, marker=marker) == []
    finally:
        _cleanup(neo4j_driver, marker=marker)
