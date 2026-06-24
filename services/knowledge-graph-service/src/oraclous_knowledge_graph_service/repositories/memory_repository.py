"""Agent-memory Neo4j repository (repositories layer — the only Neo4j access).

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

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from neo4j import Driver
from neo4j.exceptions import ConstraintError, Neo4jError
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_graph_service.domain.memory_decay import sanitize_lucene_query

logger = logging.getLogger(__name__)

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
    "m.valid_to AS valid_to, m.ingested_at AS ingested_at, m.last_accessed_at AS last_accessed_at, "
    "m.team_id AS team_id"  # team-scope blackboard (#513): for the team's per-team read isolation
)


def _subtype_label(memory_type: str) -> str:
    label = _TYPE_LABEL.get(memory_type)
    if label is None:  # validated upstream by the MemoryType enum — defence in depth
        raise ValueError(f"unknown memory type at write boundary: {memory_type!r}")
    return label


def _dedup_key(
    *, organisation_id: str, graph_id: str, content_hash: str, memory_type: str, scope: str
) -> str:
    """The deterministic single-property dedup key backing the WP-11 uniqueness constraint
    (kgs_memory_current_dedup). Encodes the full dedup tuple (org, graph, content_hash, type, scope)
    that ``find_by_content_hash`` matches on, so the constraint enforces exactly that
    read-then-write invariant — but only over CURRENT nodes (REMOVEd when a node leaves the set)."""
    raw = "\x1f".join((organisation_id, graph_id, content_hash, memory_type, scope))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class MemoryDedupConflict(RuntimeError):
    """A CREATE raced another store of identical content (org, graph, content_hash, type, scope) and
    lost the uniqueness constraint (WP-11). The service treats this as already-stored: re-read by
    content hash and return the surviving node."""


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

    def find_by_content_hash(
        self, content_hash: str, *, memory_type: str, scope: str
    ) -> dict[str, Any] | None:
        """A CURRENT memory with the same (content_hash, memory_type, scope) — the dedup match.

        memory_type and scope are part of the dedup key (#332 MED) so storing a semantic with the
        same text as an existing episodic (or the same text under a different scope) is NOT folded
        into the wrong node — only a true re-store of the same kind/scope dedups."""
        records = self._run(
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "content_hash: $content_hash, memory_type: $memory_type, scope: $scope}) "
            "WHERE m.valid_to IS NULL "
            "RETURN m.memory_id AS memory_id, m.importance_score AS importance_score LIMIT 1",
            content_hash=content_hash,
            memory_type=memory_type,
            scope=scope,
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
        type-specific property set built by the service (bound parameters, never interpolated).

        The current-node ``current_dedup_key`` is stamped here so the WP-11 uniqueness constraint
        enforces the content-hash dedup invariant; a concurrent store of identical content that
        loses the constraint raises ``MemoryDedupConflict`` (the service re-reads the survivor)."""
        label = _subtype_label(memory_type)
        dedup_key = _dedup_key(
            organisation_id=self._org(),
            graph_id=self._graph_id,
            content_hash=content_hash,
            memory_type=memory_type,
            scope=scope,
        )
        try:
            self._run(
                f"CREATE (m:Memory:{label} {{"
                "  memory_id: $memory_id, organisation_id: $organisation_id, graph_id: $graph_id,"
                "  memory_type: $memory_type, content: $content, content_hash: $content_hash,"
                "  current_dedup_key: $dedup_key,"
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
                dedup_key=dedup_key,
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
        except ConstraintError as exc:  # lost the dedup race — a current twin already exists
            raise MemoryDedupConflict(content_hash) from exc

    # --------------------------------------------------- contradictions / linking

    def find_contradictions(
        self, *, memory_id: str, subject: str, predicate: str, object_: str, is_negation: bool
    ) -> list[dict[str, Any]]:
        """Current semantic memories the NEW memory genuinely contradicts (ADR-027 §1, #332 MED).

        Same subject+predicate, and EITHER:
          * a same-object negation FLIP — ``X is Y`` vs ``X is-not Y`` (one asserts the object, the
            other denies the SAME object); OR
          * a different-object clash between TWO NON-negated assertions — ``X is Y`` vs ``X is Z``
            (X can hold only one value of this predicate).

        Deliberately NOT contradictions: two negations of different objects (``X is-not Y`` vs
        ``X is-not Z`` are compatible — X is neither), and an assertion-vs-negation of DIFFERENT
        objects (``X is Z`` vs ``X is-not Y`` are compatible). ``coalesce`` defends against legacy
        rows that pre-date ``is_negation`` (treated as non-negated assertions)."""
        records = self._run(
            "MATCH (m:Memory:Semantic {organisation_id: $organisation_id, graph_id: $graph_id}) "
            "WHERE m.valid_to IS NULL AND m.memory_id <> $memory_id "
            "  AND m.subject = $subject AND m.predicate = $predicate "
            "  AND ( "
            "    (m.object = $object AND coalesce(m.is_negation, false) <> $is_negation) "
            "    OR (m.object <> $object AND coalesce(m.is_negation, false) = false "
            "        AND $is_negation = false) "
            "  ) "
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
            "ELSE old_m.valid_to END "
            # When new wins, the old node leaves the current set — clear its dedup key so the
            # constraint stops counting it (WP-11). A non-new_wins resolution leaves it current.
            "FOREACH (_ IN CASE WHEN $resolution = 'new_wins' THEN [1] ELSE [] END | "
            "  REMOVE old_m.current_dedup_key)",
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
        Lucene ``text_score`` (the service normalises it before ranking).

        The raw query is sanitised to a safe literal term query (Lucene metacharacters escaped,
        bare boolean keywords de-cased) so any user/LLM input is parseable (#332 HIGH-2); an empty
        query after sanitisation yields zero candidates without touching the index. As a final
        guard, an unparseable query (a Lucene parse fault that slips through) degrades to zero
        results with a warning rather than a 500."""
        safe = sanitize_lucene_query(query)
        if not safe:
            return []
        try:
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
                query=safe,
                memory_type=memory_type,
                scope=scope,
                scopes=scopes,
                include_types=include_types,
                temporal=temporal,
                min_confidence=min_confidence,
                limit=limit,
            )
        except Neo4jError as exc:  # defence in depth: never let a query parse-error surface as 500
            logger.warning("memory fulltext query degraded to no results (unparseable): %s", exc)
            return []
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
        candidate_cap: int = 1_000,
    ) -> list[dict[str, Any]]:
        """Org+graph-scoped brute-force cosine over stored embeddings (no label-wide vector index —
        the #305 finding). Embeddings are L2-normalised by the embedder, so dot = cosine.

        A pre-cosine candidate cap (#332 MED) bounds the brute force: the filtered, current/temporal
        set is first cut to the ``candidate_cap`` most-recently-accessed memories, and cosine is
        computed only over THAT bounded set — so per-query cost stays bounded as the graph grows
        instead of scaling with the whole org+graph memory count."""
        records = self._run(
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id}) "
            "WHERE m.embedding IS NOT NULL AND size(m.embedding) = size($qvec) "
            "  AND m.confidence >= $min_confidence "
            "  AND ($memory_type IS NULL OR m.memory_type = $memory_type) "
            "  AND ($scope IS NULL OR m.scope = $scope) "
            "  AND ($scopes IS NULL OR m.scope IN $scopes) "
            "  AND ($include_types IS NULL OR m.memory_type IN $include_types) "
            "  AND ($temporal = 'all' OR m.valid_to IS NULL) "
            # Bound the brute force: take the most-recent `candidate_cap` first, score only those.
            "WITH m ORDER BY m.last_accessed_at DESC LIMIT $candidate_cap "
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
            candidate_cap=candidate_cap,
        )
        return [_row(r) for r in records]

    def bump_access(self, *, memory_ids: list[str], now: datetime) -> None:
        """Lazy decay recompute on access (legacy ``_bump_access`` Cypher pattern, per-type λ):
        one write re-stamps ``last_accessed_at``, increments ``access_count`` and persists the
        Ebbinghaus importance — I(t) decayed from ``base_importance`` over the time since the LAST
        access, plus the capped log access boost. ``log`` is Neo4j's natural log (= ln).

        The ``access_count + 1`` is a read-then-write, so two concurrent recalls of the same memory
        can race and lose one increment (a TOCTOU). This is BEST-EFFORT BY DESIGN (#332 LOW): the
        count feeds only an approximate decay boost, never correctness/authz, so an occasional
        undercount is acceptable and not worth a per-node lock on the read path."""
        if not memory_ids:
            return
        self._run(
            "UNWIND $memory_ids AS mid "
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: mid}) "
            # Per-type λ (DECAY_LAMBDA, kept in lockstep with domain/memory_decay): episodic 0.05,
            # semantic 0.005, procedural 0.01; an UNKNOWN type (only legacy/corrupt data) falls back
            # to 0.01 — the SAME default as the Python `_DEFAULT_LAMBDA`, so the lazy bump and the
            # read-time recompute never drift (#332 LOW).
            "WITH m, CASE m.memory_type WHEN 'episodic' THEN 0.05 WHEN 'semantic' THEN 0.005 "
            "WHEN 'procedural' THEN 0.01 ELSE 0.01 END AS lam, "
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
        scope: str,
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
        never kept: a content change either carries the fresh vector or clears it.

        The successor (#332 MED supersede):
          * inherits the predecessor's ABOUT entity links (the entity context survives a content
            update), and
          * does NOT carry the predecessor's stale ``subject``/``predicate``/``object``/
            ``is_negation`` — the ``MemoryUpdate`` body supplies none of them, so a content change
            that no longer matches the old triple would otherwise leave the successor with a wrong
            relation. They are cleared; a re-store with the new triple re-establishes them."""
        label = _subtype_label(memory_type)
        set_embedding = (
            "new.embedding = $embedding, " if (embedding is not None or content_changed) else ""
        )
        new_dedup_key = _dedup_key(
            organisation_id=self._org(),
            graph_id=self._graph_id,
            content_hash=content_hash,
            memory_type=memory_type,
            scope=scope,
        )
        records = self._run(
            "MATCH (old:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: $old_id}) WHERE old.valid_to IS NULL "
            # The old node leaves the current set: clear its dedup key so the constraint no longer
            # counts it (else an unchanged-content supersede would collide with itself).
            "SET old.valid_to = datetime($now), old.updated_at = datetime($now) "
            "REMOVE old.current_dedup_key "
            f"CREATE (new:Memory:{label}) SET new = properties(old), "
            "  new.memory_id = $new_id, new.content = $content, new.content_hash = $content_hash, "
            # `new = properties(old)` copied the (now-removed) old key shape; restamp the
            # successor's own dedup key from its (possibly unchanged) content_hash + type + scope.
            "  new.current_dedup_key = $new_dedup_key, "
            "  new.confidence = $confidence, new.importance_score = $base_importance, "
            "  new.base_importance = $base_importance, new.access_count = 0, "
            f"  {set_embedding}"
            "  new.last_accessed_at = datetime($now), new.valid_from = datetime($now), "
            "  new.valid_to = null, new.ingested_at = datetime($now), "
            "  new.updated_at = datetime($now) "
            # The MemoryUpdate body supplies no triple — drop the stale subject/predicate/object so
            # the successor never carries a relation that no longer matches its content.
            "REMOVE new.subject, new.predicate, new.object, new.is_negation "
            "CREATE (new)-[:SUPERSEDES {reason: $reason, superseded_at: datetime($now)}]->(old) "
            # Preserve the predecessor's ABOUT entity context onto the successor. Per matched edge
            # `e` is a bound node, so FOREACH over a present-or-empty guard merges the link (the
            # working pattern from `merge_memories`); the predecessor's confidence carries across.
            "WITH old, new "
            "OPTIONAL MATCH (old)-[ab:ABOUT]->(e) "
            "FOREACH (_ IN CASE WHEN e IS NOT NULL THEN [1] ELSE [] END | "
            "  MERGE (new)-[nab:ABOUT]->(e) SET nab.confidence = ab.confidence) "
            "RETURN DISTINCT new.memory_id AS memory_id",
            old_id=old_id,
            new_id=new_id,
            content=content,
            content_hash=content_hash,
            new_dedup_key=new_dedup_key,
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
            # Leaving the current set: clear the dedup key so a later re-store of the same content
            # is not blocked by this now-deleted node (WP-11).
            "REMOVE m.current_dedup_key "
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
        """Bounded fetch of current memories carrying embeddings (the consolidation candidates).

        Returns the partition keys (``memory_type``, ``scope``, ``agent_id``) alongside the vector
        so consolidation can cluster STRICTLY within a (type, scope, agent) partition — an episodic
        can never absorb a semantic, a session/agent-scoped memory can never invalidate an
        organisation-scoped one, and agent A's memory never absorbs agent B's (#332 HIGH-1)."""
        records = self._run(
            "MATCH (m:Memory {organisation_id: $organisation_id, graph_id: $graph_id}) "
            "WHERE m.valid_to IS NULL AND m.embedding IS NOT NULL "
            "RETURN m.memory_id AS memory_id, m.embedding AS embedding, "
            "m.base_importance AS base_importance, m.memory_type AS memory_type, "
            "m.scope AS scope, m.agent_id AS agent_id "
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
            # Winner-currency guard (#332 MED): only fold into a CURRENT winner (valid_to IS NULL —
            # not superseded/soft-deleted). If the winner raced away between the candidate fetch and
            # this write, the MATCH yields nothing and the whole merge is a no-op (returns 0), so a
            # loser is never invalidated under a dead winner; the next pass reselects.
            "MATCH (winner:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: $winner_id}) WHERE winner.valid_to IS NULL "
            "UNWIND $loser_ids AS lid "
            "MATCH (loser:Memory {organisation_id: $organisation_id, graph_id: $graph_id, "
            "memory_id: lid}) WHERE loser.valid_to IS NULL "
            "SET loser.valid_to = datetime($now), loser.updated_at = datetime($now), "
            "winner.base_importance = CASE WHEN winner.base_importance + loser.base_importance "
            "> 1.0 THEN 1.0 ELSE winner.base_importance + loser.base_importance END "
            # Losers leave the current set — clear their dedup key (WP-11) so a later re-store of
            # the absorbed content is not blocked by a consolidated-away node.
            "REMOVE loser.current_dedup_key "
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
