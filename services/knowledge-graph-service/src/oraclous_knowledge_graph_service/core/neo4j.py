"""Neo4j connection + startup schema (ORAA-4 §21 core layer — connection setup).

A single SYNC `neo4j.Driver` opened as the `kgs_writer` role (ORAA-53) from `KGS_NEO4J_*`. The
neo4j_graphrag `Neo4jWriter` is a sync-driver component, so the write path is sync (awaited via the
async wrapper). `ensure_schema` applies the unified-graph-model indexes idempotently at startup.

Index choice: COMPOSITE RANGE INDEXES on `(organisation_id, graph_id)` — these are available on
Neo4j Community (composite *uniqueness* constraints are Enterprise-only). Idempotent dedup comes
from the writer's MERGE on deterministic, globally-unique node ids; the index makes org-scoped reads
cheap (the org id is the leading key, so a scoped read hits the backing range index — the legacy
"no separate graph_id index" optimisation, with organisation_id now outermost).
"""

from __future__ import annotations

from neo4j import Driver, GraphDatabase

from oraclous_knowledge_graph_service.core.config import Settings

# Labels the write path materialises (lexical Document/Chunk + Source + extracted entities).
_SCOPE_INDEXES: tuple[tuple[str, str], ...] = (
    ("kgs_document_scope", "Document"),
    ("kgs_chunk_scope", "Chunk"),
    ("kgs_source_scope", "Source"),
    ("kgs_entity_scope", "__Entity__"),
)


class Neo4jUnconfiguredError(RuntimeError):
    """KGS_NEO4J_URI is not set — the ingestion/substrate path is unavailable."""


def make_neo4j_driver(settings: Settings) -> Driver:
    if not settings.neo4j_uri:
        raise Neo4jUnconfiguredError("KGS_NEO4J_URI is not set")
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    driver.verify_connectivity()
    return driver


def ensure_schema(driver: Driver, *, database: str | None = None) -> None:
    """Apply the org-scoped range indexes idempotently (CREATE INDEX ... IF NOT EXISTS)."""
    for name, label in _SCOPE_INDEXES:
        stmt = (
            f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.organisation_id, n.graph_id)"
        )
        driver.execute_query(stmt, database_=database)
