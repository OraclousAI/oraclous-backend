"""Organisation-scoped Neo4j substrate schema (ORA-16 / A1, AC#3).

Reshape of the legacy ``graph_id``-scoped indexes in
``knowledge-graph-builder`` (``entity_temporal_idx``, ``rel_temporal_idx``,
the ``in_community_*`` relationship indexes, …): ``organisation_id`` becomes the
outermost scope above ``graph_id`` on both node and relationship indexes.

NB the harness/runtime image is ``neo4j:5.23-community``. Property-*existence*
constraints (``REQUIRE … IS NOT NULL``) are an Enterprise feature, so mandatory
org presence is enforced by the write path (A2/ORA-17), not a DB constraint here;
A1 provides the org-scoped indexes + data-layer scoping (ADR-006).

``apply(driver)`` takes a neo4j driver and is idempotent via
``CREATE INDEX … IF NOT EXISTS`` — a second run neither raises nor duplicates an
index. Identifiers below are trusted module constants, never request input.
"""

from __future__ import annotations

ORG_PROPERTY = "organisation_id"

# Node labels that carry tenant data and so must be organisation-scoped (reshaped
# from the legacy graph-content labels). The first is used as the canonical label.
ORG_SCOPED_LABELS: tuple[str, ...] = ("__Entity__", "__Community__", "__Contradiction__", "Chunk")

# Relationship types whose legacy graph_id indexes reshape to org-scoped indexes.
ORG_SCOPED_RELATIONSHIP_TYPES: tuple[str, ...] = ("IN_COMMUNITY",)


def _index_name(token: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in token).strip("_").lower()
    return f"{cleaned}_org_idx"


def apply(driver) -> None:
    """Create the organisation-scoped node + relationship indexes (idempotently)."""
    for label in ORG_SCOPED_LABELS:
        driver.execute_query(
            f"CREATE INDEX {_index_name(label)} IF NOT EXISTS "
            f"FOR (n:`{label}`) ON (n.{ORG_PROPERTY}, n.graph_id)"
        )
    for rel_type in ORG_SCOPED_RELATIONSHIP_TYPES:
        driver.execute_query(
            f"CREATE INDEX {_index_name(rel_type)} IF NOT EXISTS "
            f"FOR ()-[r:`{rel_type}`]-() ON (r.{ORG_PROPERTY})"
        )
