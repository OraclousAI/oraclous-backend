"""Neo4j connection + read-side index bootstrap (ORAA-4 §21 core layer — connection setup).

A single sync `neo4j.Driver` opened as the read role (ORAA-53) from `KRS_NEO4J_*`. `ensure_schema`
creates the fulltext index over `:Chunk(text)` idempotently so fulltext works (Community
supports fulltext indexes). All reads are org-scoped in-query (Community lacks RLS).
"""

from __future__ import annotations

from neo4j import Driver, GraphDatabase

from oraclous_knowledge_retriever_service.core.config import Settings


class Neo4jUnconfiguredError(RuntimeError):
    """KRS_NEO4J_URI is not set — retrieval is unavailable."""


def make_neo4j_driver(settings: Settings) -> Driver:
    if not settings.neo4j_uri:
        raise Neo4jUnconfiguredError("KRS_NEO4J_URI is not set")
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    driver.verify_connectivity()
    return driver


def ensure_schema(driver: Driver, settings: Settings, *, database: str | None = None) -> None:
    # organisation_id is indexed alongside text (ADR-006 / ORG005): the index is org-aware, and
    # every fulltext query still post-filters node.organisation_id (Community has no RLS backstop).
    driver.execute_query(
        f"CREATE FULLTEXT INDEX {settings.chunk_fulltext_index} IF NOT EXISTS "
        "FOR (c:Chunk) ON EACH [c.text, c.organisation_id]",
        database_=database,
    )
