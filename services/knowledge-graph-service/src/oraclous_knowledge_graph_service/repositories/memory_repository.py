"""Agent-memory Neo4j repository (ORAA-4 §21 repositories layer — the only Neo4j access).

Issue #332 / ADR-027 §1. ``:Memory(:Episodic|:Semantic|:Procedural)`` nodes carry the unified-graph
scope stamps: ``organisation_id`` is injected UNCONDITIONALLY from the bound governance context via
``oraclous_substrate.access.enforced_organisation_id()`` at write time (fail-closed, the
injected-scope writer guarantee — a caller/body value can never redirect a write to another
tenant), ``graph_id`` from the already-gated path scope. Every read/write is org+graph scoped with
bound parameters (never interpolated) — the subtype label is the ONE interpolation, drawn from a
closed three-entry map keyed by the validated ``MemoryType`` enum.

Lazy decay (ADR-027 §2): ``bump_access`` is the legacy ``_bump_access`` Cypher pattern with the
per-type λ — each access re-stamps ``last_accessed_at``, increments ``access_count`` and persists
the recomputed Ebbinghaus importance in one write. NO decay cron.

Recall (ADR-027 §3): fulltext candidates come from the ``kgs_memory_content`` fulltext index;
vector candidates are an org+graph-scoped BRUTE-FORCE cosine over stored embeddings via ``reduce``
(the retriever's #308 pattern) — deliberately NO label-wide vector index (the #305 finding: a
shared-label vector index cannot be org-scoped on Community, so a scoped brute force over the
composite range index is both safe and fast at per-graph cardinalities).

Sync driver calls throughout (the service wraps them in ``asyncio.to_thread``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from neo4j import Driver
from oraclous_substrate.access import enforced_organisation_id

# Closed label map — the ONLY interpolated fragment in this module. Keys are the validated
# MemoryType enum values; anything else raises before touching Cypher.
_TYPE_LABEL: dict[str, str] = {
    "episodic": "Episodic",
    "semantic": "Semantic",
    "procedural": "Procedural",
}

# The properties returned for ranking/serialisation (single source for both candidate queries).
_RETURN_FIELDS = (
    "m.memory_id AS memory_id, m.memory_type AS memory_type, m.content AS content, "
    "m.base_importance AS base_importance, m.importance_score AS importance_score, "
    "m.access_count AS access_count, m.confidence AS confidence, m.scope AS scope, "
    "m.agent_id AS agent_id, m.session_id AS session_id, m.valid_from AS valid_from, "
    "m.valid_to AS valid_to, m.ingested_at AS ingested_at, m.last_accessed_at AS last_accessed_at"
)


def _subtype_label(memory_type: str) -> str:
    label = _TYPE_LABEL.get(memory_type)
    if label is None:  # validated upstream by the MemoryType enum — defence in depth
        raise ValueError(f"unknown memory type at write boundary: {memory_type!r}")
    return label


def _dt(value: Any) -> datetime | None:
    """neo4j.time.DateTime → aware datetime (the driver type carries ``to_native``)."""
    if value is None:
        return None
    native = value.to_native() if hasattr(value, "to_native") else value
    if isinstance(native, datetime) and native.tzinfo is None:
        return native.replace(tzinfo=UTC)
    return native


def _row(rec: Any) -> dict[str, Any]:
    d = dict(rec)
    for key in ("valid_from", "valid_to", "ingested_at", "last_accessed_at"):
        if key in d:
            d[key] = _dt(d[key])
    return d


def enumerate_memory_graphs(
    driver: Driver, *, database: str | None = None, limit: int | None = None
) -> list[tuple[str, str]]:
    """Every distinct (organisation_id, graph_id) owning current :Memory nodes — the bounded
    enumeration the consolidation sweep dispatcher fans out over (#305 pattern; module-level read so
    the beat task needs no request-scoped org context)."""
    cypher = (
        "MATCH (m:Memory) WHERE m.valid_to IS NULL "
        "RETURN DISTINCT m.organisation_id AS org, m.graph_id AS graph"
    )
    if limit is not None:
        cypher += " LIMIT $limit"
    records, _, _ = driver.execute_query(cypher, limit=limit, database_=database)
    return [(r["org"], r["graph"]) for r in records]


class MemoryRepository:
    """Org+graph-scoped CRUD/recall over :Memory nodes. Holds the (sync) Neo4j driver."""

    def __init__(
        self,
        driver: Driver,
        *,
        graph_id: str,
        organisation_id: str | None = None,
        database: str | None = None,
    ) -> None:
        self._driver = driver
        self._graph_id = graph_id
        # Workers pass the org explicitly (bound into context by the task); requests resolve it
        # lazily from the bound context at call time — fail-closed either way.
        self._organisation_id = organisation_id
        self._database = database

    def _org(self) -> str:
        return self._organisation_id or enforced_organisation_id()

    def _run(self, cypher: str, **params: Any) -> list[Any]:
        records, _, _ = self._driver.execute_query(
            cypher,
            organisation_id=self._org(),
            graph_id=self._graph_id,
            database_=self._database,
            **params,
        )
        return records

    # ------------------------------------------------------------------ store

    def find_by_content_hash(self, content_hash: str) -> dict[str, Any] | None:
        records = self._run(
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "content_hash: $content_hash}) WHERE m.valid_to IS NULL "
            "RETURN m.memory_id AS memory_id, m.importance_score AS importance_score LIMIT 1",
            content_hash=content_hash,
        )
        return dict(records[0]) if records else None

    def create(
        self,
        *,
        memory_id: str,
        memory_type: str,
        content: str,
        content_hash: str,
        base_importance: float,
        confidence: float,
        scope: str,
        source: str,
        agent_id: str,
        session_id: str,
        valid_from: datetime,
        now: datetime,
        embedding: list[float] | None,
        extra: dict[str, Any],
    ) -> None:
        """Create one typed memory node. ``organisation_id`` is stamped from the bound context
        inside ``_run`` (injected scope — never a caller value); ``extra`` carries the closed
        type-specific property set built by the service (bound parameters, never interpolated)."""
        label = _subtype_label(memory_type)
        self._run(
            f"CREATE (m:Memory:{label} {{"
            "  memory_id: $memory_id, organisation_id: $organisation_id, graph_id: $graph_id,"
            "  memory_type: $memory_type, content: $content, content_hash: $content_hash,"
            "  importance_score: $base_importance, base_importance: $base_importance,"
            "  access_count: 0, last_accessed_at: datetime($now),"
            "  valid_from: datetime($valid_from),"
            "  valid_to: null, ingested_at: datetime($now), updated_at: datetime($now),"
            "  source: $source, agent_id: $agent_id, session_id: $session_id,"
            "  confidence: $confidence, scope: $scope, embedding: $embedding })"
            " SET m += $extra",
            memory_id=memory_id,
            memory_type=memory_type,
            content=content,
            content_hash=content_hash,
            base_importance=base_importance,
            confidence=confidence,
            scope=scope,
            source=source,
            agent_id=agent_id,
            session_id=session_id,
            valid_from=valid_from.isoformat(),
            now=now.isoformat(),
            embedding=embedding,
            extra=extra,
        )

    # --------------------------------------------------- contradictions / linking

    def find_contradictions(
        self, *, memory_id: str, subject: str, predicate: str, object_: str, is_negation: bool
    ) -> list[dict[str, Any]]:
        """Current semantic memories with the same subject+predicate but a DIFFERENT object — or
        the same object with a flipped negation (an explicit "X is-NOT-Y" vs "X is-Y") — i.e. the
        statements the new memory contradicts (ADR-027 §1)."""
        records = self._run(
            "MATCH (m:Memory:Semantic {organisation_id: $organisation_id, graph_id: $graph_id}) "
            "WHERE m.valid_to IS NULL AND m.memory_id <> $memory_id "
            "  AND m.subject = $subject AND m.predicate = $predicate "
            "  AND (m.object <> $object OR m.is_negation <> $is_negation) "
            "RETURN m.memory_id AS memory_id, m.content AS content LIMIT 10",
            memory_id=memory_id,
            subject=subject,
            predicate=predicate,
            object=object_,
            is_negation=is_negation,
        )
        return [dict(r) for r in records]

    def record_contradiction(
        self, *, new_id: str, old_id: str, resolution: str, now: datetime
    ) -> None:
        """CONTRADICTS edge new→old; under ``new_wins`` the old memory is invalidated (valid_to)."""
        self._run(
            "MATCH (new_m:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: $new_id}), "
            "(old_m:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: $old_id}) "
            "MERGE (new_m)-[:CONTRADICTS {detected_at: datetime($now), resolution: $resolution}]"
            "->(old_m) "
            "SET old_m.valid_to = CASE WHEN $resolution = 'new_wins' THEN datetime($now) "
            "ELSE old_m.valid_to END",
            new_id=new_id,
            old_id=old_id,
            resolution=resolution,
            now=now.isoformat(),
        )

    def link_to_entity(self, *, memory_id: str, subject: str) -> str | None:
        """ABOUT edge to the org+graph's __Entity__ whose name matches the semantic subject."""
        records = self._run(
            "MATCH (e:__Entity__ {organisation_id: $organisation_id, graph_id: $graph_id}) "
            "WHERE toLower(e.name) = toLower($subject) "
            "WITH e LIMIT 1 "
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: $memory_id}) "
            "MERGE (m)-[:ABOUT {confidence: m.confidence}]->(e) "
            "RETURN e.id AS entity_id",
            memory_id=memory_id,
            subject=subject,
        )
        return records[0]["entity_id"] if records else None

    # ------------------------------------------------------------------ recall

    def fulltext_candidates(
        self,
        *,
        query: str,
        memory_type: str | None,
        scope: str | None,
        temporal: str,
        min_confidence: float,
        scopes: list[str] | None = None,
        include_types: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fulltext hits over the ``kgs_memory_content`` index, org+graph filtered, with the raw
        Lucene ``text_score`` (the service normalises it before ranking)."""
        records = self._run(
            "CALL db.index.fulltext.queryNodes('kgs_memory_content', $query) "
            "YIELD node AS m, score AS text_score "
            "WHERE m.organisation_id = $organisation_id AND m.graph_id = $graph_id "
            "  AND m.confidence >= $min_confidence "
            "  AND ($memory_type IS NULL OR m.memory_type = $memory_type) "
            "  AND ($scope IS NULL OR m.scope = $scope) "
            "  AND ($scopes IS NULL OR m.scope IN $scopes) "
            "  AND ($include_types IS NULL OR m.memory_type IN $include_types) "
            "  AND ($temporal = 'all' OR m.valid_to IS NULL) "
            f"RETURN {_RETURN_FIELDS}, text_score "
            "ORDER BY text_score DESC LIMIT $limit",
            query=query,
            memory_type=memory_type,
            scope=scope,
            scopes=scopes,
            include_types=include_types,
            temporal=temporal,
            min_confidence=min_confidence,
            limit=limit,
        )
        return [_row(r) for r in records]

    def vector_candidates(
        self,
        *,
        query_vector: list[float],
        memory_type: str | None,
        scope: str | None,
        temporal: str,
        min_confidence: float,
        scopes: list[str] | None = None,
        include_types: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Org+graph-scoped brute-force cosine over stored embeddings (no label-wide vector index —
        the #305 finding). Embeddings are L2-normalised by the embedder, so dot = cosine."""
        records = self._run(
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id}) "
            "WHERE m.embedding IS NOT NULL AND size(m.embedding) = size($qvec) "
            "  AND m.confidence >= $min_confidence "
            "  AND ($memory_type IS NULL OR m.memory_type = $memory_type) "
            "  AND ($scope IS NULL OR m.scope = $scope) "
            "  AND ($scopes IS NULL OR m.scope IN $scopes) "
            "  AND ($include_types IS NULL OR m.memory_type IN $include_types) "
            "  AND ($temporal = 'all' OR m.valid_to IS NULL) "
            "WITH m, reduce(s = 0.0, i IN range(0, size(m.embedding) - 1) | "
            "s + m.embedding[i] * $qvec[i]) AS vector_score "
            f"RETURN {_RETURN_FIELDS}, vector_score "
            "ORDER BY vector_score DESC LIMIT $limit",
            qvec=query_vector,
            memory_type=memory_type,
            scope=scope,
            scopes=scopes,
            include_types=include_types,
            temporal=temporal,
            min_confidence=min_confidence,
            limit=limit,
        )
        return [_row(r) for r in records]

    def bump_access(self, *, memory_ids: list[str], now: datetime) -> None:
        """Lazy decay recompute on access (legacy ``_bump_access`` Cypher pattern, per-type λ):
        one write re-stamps ``last_accessed_at``, increments ``access_count`` and persists the
        Ebbinghaus importance — I(t) decayed from ``base_importance`` over the time since the LAST
        access, plus the capped log access boost. ``log`` is Neo4j's natural log (= ln)."""
        if not memory_ids:
            return
        self._run(
            "UNWIND $memory_ids AS mid "
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: mid}) "
            "WITH m, CASE m.memory_type WHEN 'episodic' THEN 0.05 WHEN 'procedural' THEN 0.01 "
            "ELSE 0.005 END AS lam, "
            "duration.inSeconds(m.last_accessed_at, datetime($now)).seconds / 86400.0 AS days "
            "WITH m, m.base_importance * exp(-lam * CASE WHEN days < 0 THEN 0.0 ELSE days END) "
            "AS decayed, m.access_count + 1 AS new_count "
            "WITH m, new_count, decayed + CASE WHEN 0.05 * log(1.0 + new_count) > 0.3 THEN 0.3 "
            "ELSE 0.05 * log(1.0 + new_count) END AS bumped "
            "SET m.access_count = new_count, m.last_accessed_at = datetime($now), "
            "m.importance_score = CASE WHEN bumped > 1.0 THEN 1.0 ELSE bumped END",
            memory_ids=memory_ids,
            now=now.isoformat(),
        )

    # ------------------------------------------------------------ supersede / delete

    def get_current(self, memory_id: str) -> dict[str, Any] | None:
        records = self._run(
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: $memory_id}) WHERE m.valid_to IS NULL "
            f"RETURN {_RETURN_FIELDS} LIMIT 1",
            memory_id=memory_id,
        )
        return _row(records[0]) if records else None

    def supersede(
        self,
        *,
        old_id: str,
        new_id: str,
        memory_type: str,
        content: str,
        content_hash: str,
        confidence: float,
        base_importance: float,
        embedding: list[float] | None,
        content_changed: bool,
        reason: str,
        now: datetime,
    ) -> bool:
        """Temporal versioning (ADR-027 §1): close the old node (valid_to) and create the successor
        carrying the old node's full property set (labels included, via the closed type map) with
        the changed fields overridden, linked ``(new)-[:SUPERSEDES]->(old)``. A stale embedding is
        never kept: a content change either carries the fresh vector or clears it."""
        label = _subtype_label(memory_type)
        set_embedding = (
            "new.embedding = $embedding, " if (embedding is not None or content_changed) else ""
        )
        records = self._run(
            "MATCH (old:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: $old_id}) WHERE old.valid_to IS NULL "
            "SET old.valid_to = datetime($now), old.updated_at = datetime($now) "
            f"CREATE (new:Memory:{label}) SET new = properties(old), "
            "  new.memory_id = $new_id, new.content = $content, new.content_hash = $content_hash, "
            "  new.confidence = $confidence, new.importance_score = $base_importance, "
            "  new.base_importance = $base_importance, new.access_count = 0, "
            f"  {set_embedding}"
            "  new.last_accessed_at = datetime($now), new.valid_from = datetime($now), "
            "  new.valid_to = null, new.ingested_at = datetime($now), "
            "  new.updated_at = datetime($now) "
            "CREATE (new)-[:SUPERSEDES {reason: $reason, superseded_at: datetime($now)}]->(old) "
            "RETURN new.memory_id AS memory_id",
            old_id=old_id,
            new_id=new_id,
            content=content,
            content_hash=content_hash,
            confidence=confidence,
            base_importance=base_importance,
            embedding=embedding,
            reason=reason,
            now=now.isoformat(),
        )
        return bool(records)

    def soft_delete(self, *, memory_id: str, now: datetime) -> bool:
        records = self._run(
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: $memory_id}) WHERE m.valid_to IS NULL "
            "SET m.valid_to = datetime($now), m.updated_at = datetime($now) "
            "RETURN m.memory_id AS memory_id",
            memory_id=memory_id,
            now=now.isoformat(),
        )
        return bool(records)

    def hard_delete(self, *, memory_id: str) -> bool:
        records = self._run(
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: $memory_id}) "
            "WITH m, m.memory_id AS deleted DETACH DELETE m RETURN deleted",
            memory_id=memory_id,
        )
        return bool(records)

    # ------------------------------------------------------------ consolidation

    def list_current_with_embeddings(self, *, limit: int) -> list[dict[str, Any]]:
        """Bounded fetch of current memories carrying embeddings (the consolidation candidates)."""
        records = self._run(
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id}) "
            "WHERE m.valid_to IS NULL AND m.embedding IS NOT NULL "
            "RETURN m.memory_id AS memory_id, m.embedding AS embedding, "
            "m.base_importance AS base_importance "
            "ORDER BY m.importance_score DESC LIMIT $limit",
            limit=limit,
        )
        return [dict(r) for r in records]

    def merge_memories(self, *, winner_id: str, loser_ids: list[str], now: datetime) -> int:
        """Apply one merge cluster: losers are invalidated + SUPERSEDES-linked from the winner;
        the winner absorbs the losers' base_importance (capped 1.0) and inherits their ABOUT
        edges (the legacy consolidation write, now driven by true similarity clusters)."""
        if not loser_ids:
            return 0
        records = self._run(
            "MATCH (winner:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: $winner_id}) "
            "UNWIND $loser_ids AS lid "
            "MATCH (loser:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: lid}) WHERE loser.valid_to IS NULL "
            "SET loser.valid_to = datetime($now), loser.updated_at = datetime($now), "
            "winner.base_importance = CASE WHEN winner.base_importance + loser.base_importance "
            "> 1.0 THEN 1.0 ELSE winner.base_importance + loser.base_importance END "
            "CREATE (winner)-[:SUPERSEDES {reason: 'consolidation', "
            "superseded_at: datetime($now)}]->(loser) "
            "WITH winner, loser "
            "OPTIONAL MATCH (loser)-[:ABOUT]->(e) "
            "FOREACH (_ IN CASE WHEN e IS NOT NULL THEN [1] ELSE [] END | "
            "MERGE (winner)-[:ABOUT]->(e)) "
            "RETURN count(DISTINCT loser) AS merged",
            winner_id=winner_id,
            loser_ids=loser_ids,
            now=now.isoformat(),
        )
        return int(records[0]["merged"]) if records else 0
