"""Neo4j connection + startup schema (core layer — connection setup).

A single SYNC `neo4j.Driver` opened as the `kgs_writer` role from `KGS_NEO4J_*`. The
neo4j_graphrag `Neo4jWriter` is a sync-driver component, so the write path is sync (awaited via the
async wrapper). `ensure_schema` applies the unified-graph-model indexes idempotently at startup.

Index choice: COMPOSITE RANGE INDEXES on `(organisation_id, graph_id)` — these are available on
Neo4j Community (composite *uniqueness* constraints are Enterprise-only). Idempotent dedup comes
from the writer's MERGE on deterministic, globally-unique node ids; the index makes org-scoped reads
cheap (the org id is the leading key, so a scoped read hits the backing range index — the legacy
"no separate graph_id index" optimisation, with organisation_id now outermost).
"""

from __future__ import annotations

from neo4j import AsyncDriver, AsyncGraphDatabase, Driver, GraphDatabase

from oraclous_knowledge_graph_service.core.config import Settings

# Labels the write path materialises (lexical Document/Chunk + Source + extracted entities), plus
# the code-graph labels (#305): :File / :Dependency / :CodeModule carry (organisation_id, graph_id)
# but NOT the __Entity__ label, so a scoped read/MERGE on them would label-scan without their own
# composite scope index. (Function/Class/Variable DO carry __Entity__, so kgs_entity_scope already
# covers their org-scope seek; the per-symbol MERGE/seek on qualified_name is indexed separately
# below.) Mirrors what the legacy code_parser_service indexed.
_SCOPE_INDEXES: tuple[tuple[str, str], ...] = (
    ("kgs_document_scope", "Document"),
    ("kgs_chunk_scope", "Chunk"),
    ("kgs_source_scope", "Source"),
    ("kgs_entity_scope", "__Entity__"),
    ("kgs_file_scope", "File"),
    ("kgs_dependency_scope", "Dependency"),
    ("kgs_codemodule_scope", "CodeModule"),
    ("kgs_memory_scope", "Memory"),
)

# Agent-memory indexes (#332 / ADR-027 §1, following the #305 composite-key pattern): the scoped
# lookup index covers the recall filter shape (scope/type/validity inside one org+graph), the
# content-hash index backs the store-time dedup seek, and the fulltext index backs keyword recall
# (the legacy `memory_content_idx`, renamed into the kgs_* namespace — referenced by name in
# MemoryRepository.fulltext_candidates). Deliberately NO vector index on :Memory — a label-wide
# vector index cannot be org-scoped on Community (the #305 finding); semantic recall is an
# org+graph-scoped brute-force cosine in the repository instead.
_MEMORY_INDEXES: tuple[str, ...] = (
    "CREATE INDEX kgs_memory_lookup IF NOT EXISTS FOR (n:Memory) "
    "ON (n.organisation_id, n.graph_id, n.scope, n.memory_type, n.valid_to)",
    "CREATE INDEX kgs_memory_content_hash IF NOT EXISTS FOR (n:Memory) "
    "ON (n.organisation_id, n.graph_id, n.content_hash)",
    # organisation_id is in the indexed fields (ORG005) so the index itself can express org-scoped
    # Lucene queries; the repository's bound `m.organisation_id = $organisation_id` WHERE filter
    # remains the enforcement either way (no result ever leaves the caller's org).
    "CREATE FULLTEXT INDEX kgs_memory_content IF NOT EXISTS FOR (m:Memory) "
    "ON EACH [m.content, m.organisation_id]",
)

# Content-hash dedup uniqueness (WP-11, ADR-027 §1): the store-time dedup was a best-effort
# read-then-write (find_by_content_hash → create) with a TOCTOU window — two concurrent stores of
# identical content could both miss and create duplicate CURRENT nodes. We make "at most one CURRENT
# memory per (org, graph, content_hash, type, scope)" a DB-enforced invariant.
#
# Neo4j Community (the deployed `neo4j:5.23-community`) supports only SINGLE-PROPERTY uniqueness
# constraints — composite/node-key constraints are Enterprise-only — and no constraint can be made
# partial (conditioned on `valid_to IS NULL`). So the dedup key is materialised as ONE deterministic
# property, `current_dedup_key`, that is SET only while a node is current and REMOVEd the instant a
# node leaves the current set (supersede / contradiction-invalidate / soft-delete / merge). A
# uniqueness constraint ignores nodes where the property is null, so historical (superseded /
# deleted) duplicates are permitted while at most one CURRENT node per key is allowed — exactly the
# dedup invariant, expressible on Community. `IF NOT EXISTS` keeps it idempotent at startup.
_MEMORY_CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT kgs_memory_current_dedup IF NOT EXISTS "
    "FOR (m:Memory) REQUIRE m.current_dedup_key IS UNIQUE",
)

# Composite indexes on the delta/MERGE keys the code pipeline seeks on, so a re-ingest seeks the
# backing range index instead of label-scanning (#305): the :File delta seeks (org, graph, path)
# and each symbol MERGE seeks (org, graph, qualified_name). Leading org id keeps the seek tenant-
# scoped. Mirrors the legacy code_parser_service `File.path` + symbol `qualified_name` indexes.
_CODE_KEY_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("kgs_file_path", "File", "path"),
    ("kgs_function_qname", "Function", "qualified_name"),
    ("kgs_class_qname", "Class", "qualified_name"),
    ("kgs_variable_qname", "Variable", "qualified_name"),
    ("kgs_dependency_name", "Dependency", "name"),
    ("kgs_codemodule_name", "CodeModule", "name"),
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


async def make_neo4j_async_driver(settings: Settings) -> AsyncDriver:
    """An async Neo4j driver for the ReBAC engine (it requires ``neo4j.AsyncDriver``); the
    data/write path stays on the sync driver. Separate from ``make_neo4j_driver`` so a ReBAC bind
    failure never degrades graph CRUD/ingestion."""
    if not settings.neo4j_uri:
        raise Neo4jUnconfiguredError("KGS_NEO4J_URI is not set")
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    await driver.verify_connectivity()
    return driver


def ensure_schema(driver: Driver, *, database: str | None = None) -> None:
    """Apply the org-scoped range indexes idempotently (CREATE INDEX ... IF NOT EXISTS)."""
    for name, label in _SCOPE_INDEXES:
        stmt = (
            f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.organisation_id, n.graph_id)"
        )
        driver.execute_query(stmt, database_=database)
    # Code-graph delta/MERGE-key composite indexes (org, graph, <key>) so re-ingest seeks (#305).
    for name, label, key in _CODE_KEY_INDEXES:
        driver.execute_query(
            f"CREATE INDEX {name} IF NOT EXISTS "
            f"FOR (n:{label}) ON (n.organisation_id, n.graph_id, n.{key})",
            database_=database,
        )
    # Temporal (bitemporal) range index — supports valid-time reads (composite, Community-safe).
    driver.execute_query(
        "CREATE INDEX kgs_entity_valid_time IF NOT EXISTS "
        "FOR (n:__Entity__) ON (n.organisation_id, n.graph_id, n.valid_from, n.valid_to)",
        database_=database,
    )
    # Agent-memory recall/dedup indexes (#332 / ADR-027 §1).
    for stmt in _MEMORY_INDEXES:
        driver.execute_query(stmt, database_=database)
    # Content-hash dedup uniqueness constraint (WP-11) — single-property, Community-safe.
    for stmt in _MEMORY_CONSTRAINTS:
        driver.execute_query(stmt, database_=database)
