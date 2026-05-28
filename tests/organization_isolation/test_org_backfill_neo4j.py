"""Idempotent organisation backfill of legacy Neo4j nodes + relationships (ORA-24 / D1).

RED until ``backend-implementer`` adds ``oraclous_substrate.migrations.org_backfill``.

Reshape (lift-tag **Reshape**) of the legacy ``graph_id``-only scoping in
``knowledge-graph-builder`` (``setup_community_schema.py``,
``app/scripts/create_vector_indexes.py``): the migration stamps
``organisation_id`` onto every existing node of an org-scoped label and every
relationship of an org-scoped type that does not yet carry one, seeding
``SEED_ORGANISATION_ID`` (ADR-006, T1 — a node a query can reach without an org
is a cross-org read).

Asserted on the real Neo4j harness. Test data is tagged with a per-test marker so
the assertions are deterministic against the session-shared container; the
migration itself scopes globally. Mandatory-org *enforcement* on writes is the
write path (A2 / ORA-17) and Enterprise-only NOT-NULL constraints are out of
scope — this proves the one-time backfill leaves nothing of those labels/types
unscoped and that re-running is a no-op, plus a tested rollback.

Migration contract under test (to be implemented):
  ``backfill_neo4j(driver, *, organisation_id=SEED_ORGANISATION_ID) -> None``
  ``rollback_neo4j(driver) -> None``
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

MARKER_PROP = "_ora24_marker"


def _delete_marked(driver, marker: str) -> None:
    driver.execute_query(f"MATCH (n {{{MARKER_PROP}: $m}}) DETACH DELETE n", m=marker)


@pytest.fixture
def seeded_neo4j(neo4j_driver):
    """Seed one legacy (un-org-scoped) node per org-scoped label + one org-scoped rel.

    Yields ``(driver, schema, marker)``. The marker scopes both cleanup and the
    per-test assertions; nodes/rels are created **without** organisation_id.
    """
    from oraclous_substrate.schema import neo4j as neo4j_schema

    marker = f"ora24-{uuid.uuid4()}"
    _delete_marked(neo4j_driver, marker)
    for label in neo4j_schema.ORG_SCOPED_LABELS:
        neo4j_driver.execute_query(f"CREATE (n:`{label}` {{{MARKER_PROP}: $m}})", m=marker)
    rel_type = neo4j_schema.ORG_SCOPED_RELATIONSHIP_TYPES[0]
    label = neo4j_schema.ORG_SCOPED_LABELS[0]
    neo4j_driver.execute_query(
        f"CREATE (a:`{label}` {{{MARKER_PROP}: $m}})"
        f"-[:`{rel_type}` {{{MARKER_PROP}: $m}}]->"
        f"(b:`{label}` {{{MARKER_PROP}: $m}})",
        m=marker,
    )
    try:
        yield neo4j_driver, neo4j_schema, marker
    finally:
        _delete_marked(neo4j_driver, marker)


def _backfill(driver) -> None:
    from oraclous_substrate.migrations import org_backfill

    org_backfill.backfill_neo4j(driver)


def _marked_node_count(driver, label: str, marker: str, *, org_is_null: bool) -> int:
    predicate = "n.organisation_id IS NULL" if org_is_null else "n.organisation_id IS NOT NULL"
    records, _, _ = driver.execute_query(
        f"MATCH (n:`{label}` {{{MARKER_PROP}: $m}}) WHERE {predicate} RETURN count(n) AS c",
        m=marker,
    )
    return records[0]["c"]


def test_backfill_stamps_seed_org_on_every_node(seeded_neo4j) -> None:
    driver, schema, marker = seeded_neo4j
    from oraclous_substrate.organisation import SEED_ORGANISATION_ID

    _backfill(driver)
    for label in schema.ORG_SCOPED_LABELS:
        records, _, _ = driver.execute_query(
            f"MATCH (n:`{label}` {{{MARKER_PROP}: $m}}) "
            "RETURN collect(DISTINCT n.organisation_id) AS orgs",
            m=marker,
        )
        assert records[0]["orgs"] == [str(SEED_ORGANISATION_ID)], (
            f"{label} nodes not all scoped to the seed org: {records[0]['orgs']}"
        )


def test_backfill_leaves_no_node_unscoped(seeded_neo4j) -> None:
    """AC#2 / T1: no node of an org-scoped label is left without an organisation."""
    driver, schema, marker = seeded_neo4j
    _backfill(driver)
    for label in schema.ORG_SCOPED_LABELS:
        assert _marked_node_count(driver, label, marker, org_is_null=True) == 0, (
            f"{label} still has unscoped node(s) after backfill"
        )


@pytest.mark.security
def test_backfill_stamps_seed_org_on_relationships(seeded_neo4j) -> None:
    """The org-scoped relationship types carry the seed org and none is left unscoped."""
    driver, schema, marker = seeded_neo4j
    from oraclous_substrate.organisation import SEED_ORGANISATION_ID

    _backfill(driver)
    rel_type = schema.ORG_SCOPED_RELATIONSHIP_TYPES[0]
    scoped, _, _ = driver.execute_query(
        f"MATCH ()-[r:`{rel_type}` {{{MARKER_PROP}: $m}}]->() "
        "WHERE r.organisation_id = $org RETURN count(r) AS c",
        m=marker,
        org=str(SEED_ORGANISATION_ID),
    )
    unscoped, _, _ = driver.execute_query(
        f"MATCH ()-[r:`{rel_type}` {{{MARKER_PROP}: $m}}]->() "
        "WHERE r.organisation_id IS NULL RETURN count(r) AS c",
        m=marker,
    )
    assert scoped[0]["c"] >= 1, f"no {rel_type} relationship was scoped to the seed org"
    assert unscoped[0]["c"] == 0, f"{rel_type} has relationship(s) left unscoped"


def test_backfill_is_idempotent(seeded_neo4j) -> None:
    """AC#1: a second backfill neither raises, duplicates nodes, nor re-scopes."""
    driver, schema, marker = seeded_neo4j
    from oraclous_substrate.migrations import org_backfill
    from oraclous_substrate.organisation import SEED_ORGANISATION_ID

    def snapshot() -> dict[str, tuple[int, list]]:
        state: dict[str, tuple[int, list]] = {}
        for label in schema.ORG_SCOPED_LABELS:
            records, _, _ = driver.execute_query(
                f"MATCH (n:`{label}` {{{MARKER_PROP}: $m}}) "
                "RETURN count(n) AS c, collect(DISTINCT n.organisation_id) AS orgs",
                m=marker,
            )
            state[label] = (records[0]["c"], sorted(records[0]["orgs"]))
        return state

    org_backfill.backfill_neo4j(driver)
    before = snapshot()
    org_backfill.backfill_neo4j(driver)  # second run must not raise / duplicate / re-scope
    after = snapshot()
    assert after == before, f"backfill_neo4j is not idempotent (before={before}, after={after})"
    for _label, (_count, orgs) in after.items():
        assert orgs == [str(SEED_ORGANISATION_ID)]


def test_rollback_removes_the_organisation_property(seeded_neo4j) -> None:
    """AC#3: rollback strips organisation_id back off the org-scoped nodes."""
    driver, schema, marker = seeded_neo4j
    from oraclous_substrate.migrations import org_backfill

    org_backfill.backfill_neo4j(driver)
    org_backfill.rollback_neo4j(driver)
    for label in schema.ORG_SCOPED_LABELS:
        assert _marked_node_count(driver, label, marker, org_is_null=False) == 0, (
            f"rollback left organisation_id on {label} nodes"
        )
