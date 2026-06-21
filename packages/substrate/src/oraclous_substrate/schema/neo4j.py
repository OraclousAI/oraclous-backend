"""Organisation-scoped Neo4j substrate schema (A1, AC#3).

Reshape of the legacy ``graph_id``-scoped indexes in
``knowledge-graph-builder`` (``entity_temporal_idx``, ``rel_temporal_idx``,
the ``in_community_*`` relationship indexes, …): ``organisation_id`` becomes the
outermost scope above ``graph_id`` on both node and relationship indexes.

NB the harness/runtime image is ``neo4j:5.23-community``. Property-*existence*
constraints (``REQUIRE … IS NOT NULL``) are an Enterprise feature, so mandatory
org presence is enforced by the write path (A2), not a DB constraint here;
A1 provides the org-scoped indexes + data-layer scoping (ADR-006).

``apply(driver)`` takes a neo4j driver and is idempotent via
``CREATE INDEX … IF NOT EXISTS`` — a second run neither raises nor duplicates an
index. Identifiers below are trusted module constants, never request input.

The label and relationship-type sets are derived from the canonical YAML at
``org_scoped_labels.yaml`` at module-import time; the lint guardrail
in ``tools.lint.check_org_scoping`` reads the same file at lint time. Adding
an entry to the YAML extends both this module's ``apply()`` coverage and the
ORG003 recognition set with no other code change.
"""

from __future__ import annotations

from .org_scoped_labels import CANONICAL_YAML_PATH, load

ORG_PROPERTY = "organisation_id"

_SPEC = load(CANONICAL_YAML_PATH)

ORG_SCOPED_LABELS: tuple[str, ...] = _SPEC.labels
ORG_SCOPED_RELATIONSHIP_TYPES: tuple[str, ...] = _SPEC.relationship_types


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
