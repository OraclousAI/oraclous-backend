"""Substrate Neo4j primitives carry organisation_id (A1, AC#3).

RED until `backend-implementer` adds `oraclous_substrate.schema.neo4j`.

Reshape (lift-tag **Reshape**) of the legacy ``graph_id`` scoping in
``setup_community_schema.py``, ``app/scripts/create_vector_indexes.py`` and
``app/services/graph_node_service.py`` — where node/relationship indexes were
keyed on ``graph_id`` — to add ``organisation_id`` as the outer scope.

NB the harness image is ``neo4j:5.23-community``. Property-*existence*
constraints (``REQUIRE ... IS NOT NULL``) are a Neo4j **Enterprise** feature,
so AC#3 ("nodes + relationships carry organisation_id; ... indexes include it")
is proven here via (a) org-scoped indexes and (b) data-layer property isolation,
*not* a NOT-NULL constraint. Flagged for the architect at Tests Review: the
mandatory-org enforcement on writes is either Enterprise-only or belongs to the
write path (A2).
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(scope="module")
def applied_neo4j(neo4j_driver):
    from oraclous_substrate.schema import neo4j as neo4j_schema

    neo4j_schema.apply(neo4j_driver)
    return neo4j_driver, neo4j_schema


def _index_records(driver) -> list[dict]:
    records, _, _ = driver.execute_query("SHOW INDEXES")
    return [r.data() for r in records]


def test_substrate_declares_org_scoped_labels(applied_neo4j) -> None:
    _driver, schema = applied_neo4j
    assert len(schema.ORG_SCOPED_LABELS) > 0


def test_each_org_scoped_label_has_an_index_including_organisation_id(applied_neo4j) -> None:
    driver, schema = applied_neo4j
    org_prop = getattr(schema, "ORG_PROPERTY", "organisation_id")
    indexes = _index_records(driver)
    for label in schema.ORG_SCOPED_LABELS:
        matching = [
            idx
            for idx in indexes
            if label in (idx.get("labelsOrTypes") or [])
            and org_prop in (idx.get("properties") or [])
        ]
        assert matching, f"no index on label {label!r} includes {org_prop!r}; indexes={indexes}"


def test_at_least_one_relationship_index_includes_organisation_id(applied_neo4j) -> None:
    """The legacy relationship indexes (in_community_graph_id, …) reshape to org-scoped."""
    driver, schema = applied_neo4j
    org_prop = getattr(schema, "ORG_PROPERTY", "organisation_id")
    rel_indexes = [
        idx
        for idx in _index_records(driver)
        if idx.get("entityType") == "RELATIONSHIP" and org_prop in (idx.get("properties") or [])
    ]
    assert rel_indexes, "no RELATIONSHIP index includes organisation_id"


def test_nodes_are_isolated_by_organisation_property(applied_neo4j) -> None:
    """A query filtered by organisation_id never returns another org's node."""
    driver, schema = applied_neo4j
    label = schema.ORG_SCOPED_LABELS[0]
    marker = f"ora16-{uuid.uuid4()}"
    try:
        driver.execute_query(
            f"CREATE (:`{label}` {{organisation_id: $org, _ora16_marker: $marker}})",
            org=ORG_A,
            marker=marker,
        )
        driver.execute_query(
            f"CREATE (:`{label}` {{organisation_id: $org, _ora16_marker: $marker}})",
            org=ORG_B,
            marker=marker,
        )
        records, _, _ = driver.execute_query(
            f"MATCH (n:`{label}` {{_ora16_marker: $marker}}) "
            "WHERE n.organisation_id = $org RETURN count(n) AS c",
            marker=marker,
            org=ORG_A,
        )
        assert records[0]["c"] == 1  # org A's filtered query sees only org A's node
    finally:
        driver.execute_query("MATCH (n {_ora16_marker: $marker}) DETACH DELETE n", marker=marker)


def test_relationships_carry_and_isolate_by_organisation_property(applied_neo4j) -> None:
    driver, schema = applied_neo4j
    label = schema.ORG_SCOPED_LABELS[0]
    marker = f"ora16-{uuid.uuid4()}"
    try:
        for org in (ORG_A, ORG_B):
            driver.execute_query(
                f"CREATE (a:`{label}` {{_ora16_marker: $marker, organisation_id: $org}})"
                f"-[:RELATED {{organisation_id: $org, _ora16_marker: $marker}}]->"
                f"(b:`{label}` {{_ora16_marker: $marker, organisation_id: $org}})",
                org=org,
                marker=marker,
            )
        records, _, _ = driver.execute_query(
            "MATCH ()-[r:RELATED {_ora16_marker: $marker}]->() "
            "WHERE r.organisation_id = $org RETURN count(r) AS c",
            marker=marker,
            org=ORG_A,
        )
        assert records[0]["c"] == 1
    finally:
        driver.execute_query("MATCH (n {_ora16_marker: $marker}) DETACH DELETE n", marker=marker)


def test_apply_is_idempotent(applied_neo4j) -> None:
    """Re-running apply() does not raise and does not create duplicate org-scoped indexes.

    A real redeploy path: the count of org-scoped indexes per label/type must be stable
    across a second apply (Neo4j ``CREATE INDEX`` without ``IF NOT EXISTS`` raises on a
    duplicate, so this also pins that apply() guards against re-creation).
    """
    driver, schema = applied_neo4j
    org_prop = getattr(schema, "ORG_PROPERTY", "organisation_id")

    def org_index_counts() -> dict[str, int]:
        counts: dict[str, int] = {}
        for idx in _index_records(driver):
            if org_prop not in (idx.get("properties") or []):
                continue
            for label in idx.get("labelsOrTypes") or []:
                counts[label] = counts.get(label, 0) + 1
        return counts

    before = org_index_counts()
    schema.apply(driver)  # second apply on the already-applied schema must not raise
    after = org_index_counts()

    assert after == before, (
        "apply() is not idempotent: re-applying changed the per-label org-scoped index "
        f"counts (before={before}, after={after})"
    )
