"""Community-detection repository (ORAA-4 §21 repos layer — the ONLY GDS/Neo4j access; #303).

RE-ARCHITECTS the legacy in-memory ``leidenalg``/``igraph`` community pipeline
(``knowledge-graph-builder/app/tasks/community_tasks.py``) onto in-DB Neo4j GDS Louvain: the graph
never leaves the database. Detection projects the org+graph-scoped ``:__Entity__`` subgraph ONCE
into an in-memory GDS graph (Cypher projection — Community-safe, no native-projection licence), runs
a SINGLE ``CALL gds.louvain.stream`` with ``includeIntermediateCommunities: true``, and maps each
node's native dendrogram (``intermediateCommunityIds``) onto ``:__Community__`` nodes +
``IN_COMMUNITY``/``PARENT_COMMUNITY`` edges through MERGE. Leiden is Enterprise-only and is
deliberately NOT used.

The hierarchy (see ``domain.community``) is Louvain's NATIVE dendrogram — exactly as many levels as
the algorithm converged to, not a fixed 5. The earlier ``w ** resolution`` sweep produced five
IDENTICAL partitions on uniform-weight graphs (the dominant case) chained by meaningless parent
edges; it is gone. The live-verified array ordering (``neo4j:5.23-community`` GDS 2.11) is
``intermediateCommunityIds[0]`` = finest, last = coarsest; ``domain.dendrogram_to_levels`` maps it
so level 0 is the coarsest, and ``domain.dendrogram_parent_links`` reads the parent straight off the
array (true containment, not a vote). A flat convergence emits one honest level with no parents.

Per-(org,graph) mutual exclusion: detection is a destructive clear+rebuild, so two concurrent runs
on the same (org, graph) would corrupt each other. A Redis ``SET NX EX`` lock (the same Redis the
Celery spine uses) is held across the whole clear→detect→write window and released in ``finally``;
a caller that finds the lock held gets a clean "already in progress" skip rather than racing. Both
the inline (request) path and the Celery worker funnel through ``detect``, so both are gated.

Tenant safety (the §21/T1 contract):
  * Every read AND write is scoped to ``organisation_id`` + ``graph_id``. ``organisation_id`` is
    sourced LIVE from the bound governance context via ``enforced_organisation_id()`` (fail-closed)
    NEVER from a caller argument — so a caller can never read or write another org's communities,
    and an injected ``organisation_id`` cannot redirect a write. Mirrors
    ``OrganisationScopedKGWriter``'s unconditional stamp (``multi_tenant.py``).
  * The Cypher projection's source/target patterns carry org + graph as bound parameters, so the
    in-memory GDS graph can never contain another tenant's data.
  * The in-memory projection is ALWAYS dropped in a ``finally`` (no leak across runs/tenants).

All Cypher uses bound parameters (never interpolated): injection-safe. Sync driver (the Celery
worker / ``asyncio.to_thread`` callers wrap it), matching the rest of the write path.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TypedDict

from neo4j import Driver
from oraclous_substrate.access import ORGANISATION_ID_PROPERTY, enforced_organisation_id

from oraclous_knowledge_graph_service.core.redis import RedisLockClient
from oraclous_knowledge_graph_service.domain.community import (
    COMMUNITY_LABEL,
    ENTITY_KIND,
    ENTITY_LABEL,
    IN_COMMUNITY_REL,
    PARENT_COMMUNITY_REL,
    Community,
    CommunityMember,
    DetectionInProgress,
    GdsUnavailableError,
    dendrogram_parent_links,
    dendrogram_to_levels,
    make_community_id,
)

logger = logging.getLogger(__name__)

# A unique-per-call GDS in-memory graph name keeps concurrent detections (different tenants/graphs)
# from colliding on the named projection.
_GDS_GRAPH_PREFIX = "kgs_comm"

_ALGORITHM = "louvain"


class GraphAnalyticsRow(TypedDict):
    """The ``analytics`` read shape (the legacy ``/analytics`` payload). Typed so the service layer
    sees each field's real type rather than a uniform ``object`` value."""

    node_count: int
    relationship_count: int
    entity_count: int
    density: float
    avg_degree: float
    entity_types: list[dict[str, Any]]
    relationship_types: list[dict[str, Any]]
    top_entities: list[dict[str, Any]]
    community_count: int


# Per-(org,graph) detect lock. Held across the destructive clear→detect→write window so two runs on
# the same graph can't corrupt each other; the TTL is a safety net so a crashed run self-heals.
_DETECT_LOCK_TTL_SECONDS = 15 * 60


def _detect_lock_key(*, organisation_id: str, graph_id: str) -> str:
    return f"kgs:community_detect:{organisation_id}:{graph_id}"


def _is_gds_missing(exc: Exception) -> bool:
    """True when the GDS error means the plugin/procedure is ABSENT (vs. a real runtime failure)."""
    msg = str(exc).lower()
    if "no procedure with the name" in msg and "gds" in msg:
        return True
    return "procedurenotfound" in msg.replace(".", "")


class CommunityRepository:
    """In-DB GDS Louvain community detection + the community read surface. Holds the Neo4j driver.

    The only module that touches GDS / the ``:__Community__`` graph; the service + summarizer layers
    consume its typed results without any Cypher of their own.
    """

    def __init__(
        self,
        driver: Driver,
        *,
        database: str | None = None,
        lock_client: RedisLockClient | None = None,
    ) -> None:
        self._driver = driver
        self._database = database
        # A sync ``redis.Redis`` (or compatible) used for the per-(org,graph) detect lock. ``None``
        # disables locking (no Redis configured) — detection still runs, but two concurrent runs on
        # the same graph are no longer mutually excluded (degraded, logged once at acquire).
        self._lock = lock_client

    def _org(self) -> str:
        """The bound organisation id (fail-closed). Sourced live — never a caller argument."""
        return enforced_organisation_id()

    # ── detection (GDS Louvain) ───────────────────────────────────────────────────────────────

    def count_entities(self, *, graph_id: str) -> int:
        """Org+graph-scoped count of ``:__Entity__`` nodes (the detect pre-flight)."""
        records, _, _ = self._driver.execute_query(
            f"MATCH (e:{ENTITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE e.organisation_id = $organisation_id "
            "RETURN count(e) AS c",
            graph_id=graph_id,
            organisation_id=self._org(),
            database_=self._database,
        )
        return int(records[0]["c"]) if records else 0

    def detect(self, *, graph_id: str) -> dict[int, dict[str, list[str]]]:
        """Run a SINGLE GDS Louvain dendrogram and MERGE the community graph.

        Returns level → {gds_community_key: [member entity ids]} so the caller can drive
        summarisation. Steps (all in-DB, under a per-(org,graph) Redis lock): acquire lock → clear
        prior communities → project the org+graph subgraph ONCE → stream Louvain with
        ``includeIntermediateCommunities`` → map the native dendrogram to levels (0 = coarsest)
        → read parent links straight off the array (true containment) → MERGE community nodes +
        ``IN_COMMUNITY``/``PARENT_COMMUNITY`` edges → release lock.

        Raises :class:`DetectionInProgress` (→ caller surfaces "already in progress") if another run
        holds the lock for this (org, graph). Raises :class:`GdsUnavailableError` (→503) if GDS is
        absent.
        """
        org = self._org()
        token = self._acquire_lock(organisation_id=org, graph_id=graph_id)
        if token is None:
            raise DetectionInProgress(graph_id)
        try:
            # Content-derived ids mean a changed membership would strand old nodes; clear first so a
            # re-detect is a clean rebuild for this org+graph. The lock guarantees no concurrent run
            # observes the half-cleared state.
            self._clear_communities(graph_id=graph_id, organisation_id=org)

            try:
                rows = self._louvain_dendrogram(graph_id=graph_id, organisation_id=org)
            except Exception as exc:  # noqa: BLE001 — classify GDS-missing vs. a real failure
                if _is_gds_missing(exc):
                    raise GdsUnavailableError(
                        "community detection unavailable: the Neo4j Graph Data Science (gds.*) "
                        "procedures are not loaded on this database"
                    ) from exc
                raise

            levels_membership = dendrogram_to_levels(rows)
            parents = dendrogram_parent_links(rows)
            self._write_communities(
                graph_id=graph_id,
                organisation_id=org,
                levels_membership=levels_membership,
                parents=parents,
            )
            return levels_membership
        finally:
            self._release_lock(organisation_id=org, graph_id=graph_id, token=token)

    def _louvain_dendrogram(
        self, *, graph_id: str, organisation_id: str
    ) -> list[tuple[str, list[int]]]:
        """Project the org+graph subgraph ONCE and stream Louvain's native dendrogram.

        Returns one ``(entity_id, intermediate_community_ids)`` per node — the per-iteration
        dendrogram exactly as GDS emits it (``intermediateCommunityIds[0]`` finest → last coarsest).
        The projection is named uniquely per call and dropped in a ``finally`` (no leak even if the
        stream raises). Edge weight is the stored ``r.weight`` (defaulting to 1.0) — NO exponent
        sweep. Entity identity is the domain ``id`` prop (structured ingest) or the stable elementId
        fallback (extraction-built entities carry no ``id``) — the ``coalesce(e.id, elementId(e))``
        contract.
        """
        graph_name = f"{_GDS_GRAPH_PREFIX}_{uuid.uuid4().hex}"
        try:
            # Cypher projection: org+graph scoped (T1). OPTIONAL MATCH keeps isolated entities so
            # singletons still get a community. gds.util.asNode(...).id is carried so a streamed
            # node maps back to the entity id we MERGE membership against.
            self._driver.execute_query(
                f"MATCH (src:{ENTITY_LABEL} {{graph_id: $graph_id}}) "
                "WHERE src.organisation_id = $organisation_id "
                f"OPTIONAL MATCH (src)-[r {{graph_id: $graph_id}}]-(tgt:{ENTITY_LABEL} "
                "{graph_id: $graph_id}) "
                "WHERE tgt.organisation_id = $organisation_id "
                "WITH src, tgt, coalesce(r.weight, 1.0) AS w "
                "WITH gds.graph.project($graph_name, src, tgt, "
                "{ relationshipProperties: { weight: w } }, "
                "{ undirectedRelationshipTypes: ['*'] }) AS g "
                "RETURN g.graphName AS name",
                graph_name=graph_name,
                graph_id=graph_id,
                organisation_id=organisation_id,
                database_=self._database,
            )
            records, _, _ = self._driver.execute_query(
                "CALL gds.louvain.stream($graph_name, "
                "{ includeIntermediateCommunities: true, "
                "relationshipWeightProperty: 'weight', concurrency: 1 }) "
                "YIELD nodeId, communityId, intermediateCommunityIds "
                "RETURN coalesce(gds.util.asNode(nodeId).id, "
                "elementId(gds.util.asNode(nodeId))) AS entity_id, "
                "communityId AS community, intermediateCommunityIds AS dendro",
                graph_name=graph_name,
                database_=self._database,
            )
        finally:
            self._drop_projection(graph_name)

        rows: list[tuple[str, list[int]]] = []
        for r in records:
            eid = r["entity_id"]
            if eid is None:
                continue
            # When GDS converges flat it may return a null dendrogram array; fall back to the single
            # final communityId so the node still lands in one honest level.
            dendro = r["dendro"]
            ids = [int(x) for x in dendro] if dendro else [int(r["community"])]
            rows.append((str(eid), ids))
        return rows

    # ── per-(org,graph) detect lock (Redis SET NX EX) ──────────────────────────────────────────

    def _acquire_lock(self, *, organisation_id: str, graph_id: str) -> str | None:
        """Try to take the per-(org,graph) detect lock. Returns the lock token, or ``None`` if held.

        ``None`` Redis (unconfigured) returns a sentinel token so detection still proceeds (the lock
        is best-effort); a Redis error at acquire degrades the same way (logged, not fatal).
        """
        if self._lock is None:
            return "no-lock"
        key = _detect_lock_key(organisation_id=organisation_id, graph_id=graph_id)
        token = uuid.uuid4().hex
        try:
            acquired = self._lock.set(key, token, nx=True, ex=_DETECT_LOCK_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 — lock is advisory; a Redis fault must not block
            logger.warning("community detect lock acquire failed (%s) — proceeding unlocked", exc)
            return "no-lock"
        return token if acquired else None

    def _release_lock(self, *, organisation_id: str, graph_id: str, token: str | None) -> None:
        """Release the lock iff we still own it (token match), so a TTL-expired-then-retaken lock is
        not released out from under another run. No-op for the unlocked sentinel / no Redis."""
        if self._lock is None or token in (None, "no-lock"):
            return
        key = _detect_lock_key(organisation_id=organisation_id, graph_id=graph_id)
        try:
            current = self._lock.get(key)
            held = current.decode() if isinstance(current, bytes) else current
            if held == token:
                self._lock.delete(key)
        except Exception as exc:  # noqa: BLE001 — release is cleanup; let the TTL reap it otherwise
            logger.warning("community detect lock release skipped (%s)", exc)

    def _drop_projection(self, graph_name: str) -> None:
        """Drop the named in-memory GDS graph (advisory ``failIfMissing=false``). Never raises."""
        try:
            self._driver.execute_query(
                "CALL gds.graph.drop($graph_name, false) YIELD graphName",
                graph_name=graph_name,
                database_=self._database,
            )
        except Exception as exc:  # noqa: BLE001 — drop is cleanup; a failure must not mask the run
            logger.warning("gds.graph.drop('%s') skipped: %s", graph_name, exc)

    # ── community writes (MERGE the :__Community__ graph) ──────────────────────────────────────

    def _clear_communities(self, *, graph_id: str, organisation_id: str) -> None:
        """Detach-delete this org+graph's community nodes (membership/parent edges go with them)."""
        self._driver.execute_query(
            f"MATCH (c:{COMMUNITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE c.organisation_id = $organisation_id DETACH DELETE c",
            graph_id=graph_id,
            organisation_id=organisation_id,
            database_=self._database,
        )

    def _write_communities(
        self,
        *,
        graph_id: str,
        organisation_id: str,
        levels_membership: dict[int, dict[str, list[str]]],
        parents: dict[int, dict[str, str | None]],
    ) -> None:
        """MERGE every community node + its IN_COMMUNITY / PARENT_COMMUNITY edges (org+graph).

        The community id is the deterministic SHA-256 of ``graph|level|sorted-members`` (16-char
        scheme). ``organisation_id``/``graph_id``/``transaction_time`` are stamped unconditionally
        from the bound scope — a caller cannot override them (the injected-scope writer contract).
        Parent ids translate the dendrogram's ``parent_key`` (level-1 community key) back to its
        deterministic id via the level-1 membership.
        """
        now = datetime.now(UTC)
        # Write COARSEST-FIRST (ascending level) so a parent node exists before its children MERGE
        # their PARENT_COMMUNITY edge to it — the dendrogram map inserts finest-first, so insertion
        # order would write children before parents and drop every parent edge.
        for level in sorted(levels_membership):
            groups = levels_membership[level]
            total = sum(len(m) for m in groups.values()) or 1
            parent_links = parents.get(level, {})
            for gds_key, members in groups.items():
                cid = make_community_id(graph_id=graph_id, level=level, member_ids=members)
                parent_key = parent_links.get(gds_key)
                parent_id = (
                    make_community_id(
                        graph_id=graph_id,
                        level=level - 1,
                        member_ids=levels_membership[level - 1][parent_key],
                    )
                    if parent_key is not None
                    and level > 0
                    and parent_key in levels_membership.get(level - 1, {})
                    else None
                )
                self._merge_community(
                    graph_id=graph_id,
                    organisation_id=organisation_id,
                    community_id=cid,
                    level=level,
                    members=members,
                    weight=len(members) / total,
                    parent_id=parent_id,
                    now=now,
                )

    def _merge_community(
        self,
        *,
        graph_id: str,
        organisation_id: str,
        community_id: str,
        level: int,
        members: list[str],
        weight: float,
        parent_id: str | None,
        now: datetime,
    ) -> None:
        """MERGE one community node, its member edges, and (if any) the parent edge."""
        self._driver.execute_query(
            f"MERGE (c:{COMMUNITY_LABEL} {{community_id: $community_id, graph_id: $graph_id, "
            f"{ORGANISATION_ID_PROPERTY}: $organisation_id}}) "
            "SET c.level = $level, c.kind = $kind, "
            "c.algorithm = $algorithm, c.entity_count = $entity_count, c.weight = $weight, "
            "c.parent_id = $parent_id, c.status = 'active', c.transaction_time = $now, "
            "c.last_updated = $now",
            community_id=community_id,
            graph_id=graph_id,
            organisation_id=organisation_id,
            level=level,
            kind=ENTITY_KIND,
            algorithm=_ALGORITHM,
            entity_count=len(members),
            weight=weight,
            parent_id=parent_id,
            now=now,
            database_=self._database,
        )
        # Member edges — UNWIND the ids, match each entity (id or elementId), MERGE the membership.
        self._driver.execute_query(
            f"MATCH (c:{COMMUNITY_LABEL} {{community_id: $community_id, graph_id: $graph_id}}) "
            "WHERE c.organisation_id = $organisation_id "
            "UNWIND $members AS eid "
            f"MATCH (e:{ENTITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE e.organisation_id = $organisation_id "
            "AND coalesce(e.id, elementId(e)) = eid "
            f"MERGE (e)-[m:{IN_COMMUNITY_REL} {{graph_id: $graph_id, level: $level}}]->(c) "
            f"SET m.{ORGANISATION_ID_PROPERTY} = $organisation_id",
            community_id=community_id,
            graph_id=graph_id,
            organisation_id=organisation_id,
            members=members,
            level=level,
            database_=self._database,
        )
        if parent_id is not None:
            self._driver.execute_query(
                f"MATCH (child:{COMMUNITY_LABEL} {{community_id: $child_id, graph_id: $graph_id}}) "
                "WHERE child.organisation_id = $organisation_id "
                f"MATCH (parent:{COMMUNITY_LABEL} "
                "{community_id: $parent_id, graph_id: $graph_id}) "
                "WHERE parent.organisation_id = $organisation_id "
                f"MERGE (child)-[p:{PARENT_COMMUNITY_REL} {{graph_id: $graph_id}}]->(parent) "
                f"SET p.{ORGANISATION_ID_PROPERTY} = $organisation_id",
                child_id=community_id,
                parent_id=parent_id,
                graph_id=graph_id,
                organisation_id=organisation_id,
                database_=self._database,
            )

    def set_summary(
        self,
        *,
        graph_id: str,
        community_id: str,
        summary: str,
        summary_keywords: list[str],
        summary_excerpt: str,
        summary_model: str | None,
        summary_source: str,
    ) -> bool:
        """Persist a community's summary fields (org+graph scoped). Returns True if it landed.

        ``summary_keywords`` is a Neo4j list; ``summary_at`` is stamped server-side.
        ``summary_source`` is ``"llm"`` for a real model answer or ``"fallback"`` for the
        member-derived degrade; on a fallback ``summary_model`` is ``None`` so a reader can never
        mistake a degraded summary for a real one. Scoped so a caller cannot touch another org."""
        records, _, _ = self._driver.execute_query(
            f"MATCH (c:{COMMUNITY_LABEL} {{community_id: $community_id, graph_id: $graph_id}}) "
            "WHERE c.organisation_id = $organisation_id "
            "SET c.summary = $summary, c.summary_keywords = $summary_keywords, "
            "c.summary_excerpt = $summary_excerpt, c.summary_model = $summary_model, "
            "c.summary_source = $summary_source, c.summary_at = datetime() "
            "RETURN c.community_id AS id",
            community_id=community_id,
            graph_id=graph_id,
            organisation_id=self._org(),
            summary=summary,
            summary_keywords=summary_keywords,
            summary_excerpt=summary_excerpt,
            summary_model=summary_model,
            summary_source=summary_source,
            database_=self._database,
        )
        return bool(records)

    # ── community reads ───────────────────────────────────────────────────────────────────────

    def list_communities(
        self,
        *,
        graph_id: str,
        level: int | None,
        min_entities: int,
        only_unsummarized: bool = False,
    ) -> list[Community]:
        """Org+graph-scoped community list, optionally filtered by level; ordered level, size.

        ``only_unsummarized`` adds a ``summary IS NULL OR summary_source = 'fallback'`` filter so
        the summarizer can resume after a partial failure WITHOUT re-billing the communities that
        already have a REAL (llm) summary — but a fallback-degraded placeholder (non-null summary,
        but ``summary_source='fallback'``, so it never reached a model) is retried on the default
        resume so it gets a real summary, rather than being stuck as a degraded placeholder forever.
        """
        cypher = (
            f"MATCH (c:{COMMUNITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE c.organisation_id = $organisation_id AND c.entity_count >= $min_entities "
        )
        # ``dict[str, Any]`` (not ``object``) so the ``**params`` splat resolves against the neo4j
        # ``execute_query(**kwargs: Any)`` overload — an ``object``-valued dict does not. Values are
        # bound query parameters either way; the call shape (top-level kwargs) is unchanged.
        params: dict[str, Any] = {
            "graph_id": graph_id,
            "organisation_id": self._org(),
            "min_entities": min_entities,
        }
        if level is not None:
            cypher += "AND c.level = $level "
            params["level"] = level
        if only_unsummarized:
            # Re-summarise both the never-summarised AND the fallback-degraded (a non-null
            # placeholder that never reached a model), but never the real (llm) summaries.
            cypher += "AND (c.summary IS NULL OR c.summary_source = 'fallback') "
        cypher += (
            "RETURN c.community_id AS community_id, c.kind AS kind, c.level AS level, "
            "c.entity_count AS entity_count, c.status AS status, "
            "c.weight AS weight, c.parent_id AS parent_id, c.summary AS summary, "
            "c.summary_keywords AS summary_keywords, c.summary_excerpt AS summary_excerpt, "
            "c.summary_model AS summary_model, c.summary_at AS summary_at, "
            "c.summary_source AS summary_source "
            "ORDER BY c.level ASC, c.entity_count DESC"
        )
        records, _, _ = self._driver.execute_query(cypher, database_=self._database, **params)
        return [self._row_to_community(r) for r in records]

    def get_community(
        self, *, graph_id: str, community_id: str, member_limit: int = 100
    ) -> Community | None:
        """One community + up to ``member_limit`` member entities (org+graph scoped). None if absent
        in this org (so a cross-org id is invisible — 404 at the route)."""
        records, _, _ = self._driver.execute_query(
            f"MATCH (c:{COMMUNITY_LABEL} {{community_id: $community_id, graph_id: $graph_id}}) "
            "WHERE c.organisation_id = $organisation_id "
            "RETURN c.community_id AS community_id, c.kind AS kind, c.level AS level, "
            "c.entity_count AS entity_count, c.status AS status, "
            "c.weight AS weight, c.parent_id AS parent_id, c.summary AS summary, "
            "c.summary_keywords AS summary_keywords, c.summary_excerpt AS summary_excerpt, "
            "c.summary_model AS summary_model, c.summary_at AS summary_at, "
            "c.summary_source AS summary_source",
            community_id=community_id,
            graph_id=graph_id,
            organisation_id=self._org(),
            database_=self._database,
        )
        if not records:
            return None
        community = self._row_to_community(records[0])
        members = self._members_of(graph_id=graph_id, community_id=community_id, limit=member_limit)
        return Community(**{**community.__dict__, "members": members})

    def _members_of(self, *, graph_id: str, community_id: str, limit: int) -> list[CommunityMember]:
        records, _, _ = self._driver.execute_query(
            f"MATCH (e:{ENTITY_LABEL} {{graph_id: $graph_id}})-[:{IN_COMMUNITY_REL}]->"
            f"(c:{COMMUNITY_LABEL} {{community_id: $community_id, graph_id: $graph_id}}) "
            "WHERE e.organisation_id = $organisation_id AND c.organisation_id = $organisation_id "
            "RETURN coalesce(e.id, elementId(e)) AS entity_id, "
            "coalesce(e.name, e.text, '') AS entity_name, labels(e) AS labels "
            "LIMIT $limit",
            graph_id=graph_id,
            community_id=community_id,
            organisation_id=self._org(),
            limit=limit,
            database_=self._database,
        )
        return [
            CommunityMember(
                entity_id=r["entity_id"],
                entity_name=r["entity_name"],
                entity_type=_pick_type(r["labels"]),
            )
            for r in records
        ]

    def members_with_relationships(
        self, *, graph_id: str, community_id: str, member_limit: int, rel_limit: int
    ) -> tuple[list[CommunityMember], list[dict[str, str]]]:
        """Members + the relationships between them (for the LLM summary prompt). Org+graph."""
        members = self._members_of(graph_id=graph_id, community_id=community_id, limit=member_limit)
        member_ids = [m.entity_id for m in members]
        if not member_ids:
            return members, []
        records, _, _ = self._driver.execute_query(
            f"MATCH (a:{ENTITY_LABEL} {{graph_id: $graph_id}})-[r]->"
            f"(b:{ENTITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE a.organisation_id = $organisation_id AND b.organisation_id = $organisation_id "
            "AND coalesce(a.id, elementId(a)) IN $ids AND coalesce(b.id, elementId(b)) IN $ids "
            "RETURN coalesce(a.name, '') AS src, type(r) AS rel, coalesce(b.name, '') AS tgt "
            "LIMIT $rel_limit",
            graph_id=graph_id,
            organisation_id=self._org(),
            ids=member_ids,
            rel_limit=rel_limit,
            database_=self._database,
        )
        rels = [{"src": r["src"], "rel": r["rel"], "tgt": r["tgt"]} for r in records]
        return members, rels

    def status(self, *, graph_id: str) -> tuple[int, list[int], int]:
        """(community_count, sorted distinct levels, entity_count) for this org+graph — the status
        signal the service turns into a CommunitiesStatus."""
        records, _, _ = self._driver.execute_query(
            f"MATCH (c:{COMMUNITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE c.organisation_id = $organisation_id "
            "RETURN count(c) AS cnt, collect(DISTINCT c.level) AS levels",
            graph_id=graph_id,
            organisation_id=self._org(),
            database_=self._database,
        )
        cnt = int(records[0]["cnt"]) if records else 0
        levels = sorted(int(level) for level in (records[0]["levels"] if records else []))
        return cnt, levels, self.count_entities(graph_id=graph_id)

    def analytics(self, *, graph_id: str, top_n: int = 10) -> GraphAnalyticsRow:
        """Org+graph-scoped graph statistics (the legacy ``/analytics`` shape): node/rel/entity
        counts, label + relationship-type breakdowns, density, avg degree, top entities by degree,
        and the community count. All bound params; one method, several scoped reads."""
        org = self._org()
        counts, _, _ = self._driver.execute_query(
            "MATCH (n {graph_id: $graph_id}) WHERE n.organisation_id = $organisation_id "
            "RETURN count(n) AS node_count",
            graph_id=graph_id,
            organisation_id=org,
            database_=self._database,
        )
        node_count = int(counts[0]["node_count"]) if counts else 0
        rels, _, _ = self._driver.execute_query(
            "MATCH (s {graph_id: $graph_id})-[r]->(e {graph_id: $graph_id}) "
            "WHERE s.organisation_id = $organisation_id AND e.organisation_id = $organisation_id "
            "RETURN count(r) AS rel_count",
            graph_id=graph_id,
            organisation_id=org,
            database_=self._database,
        )
        rel_count = int(rels[0]["rel_count"]) if rels else 0
        ent, _, _ = self._driver.execute_query(
            f"MATCH (e:{ENTITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE e.organisation_id = $organisation_id RETURN count(e) AS c",
            graph_id=graph_id,
            organisation_id=org,
            database_=self._database,
        )
        entity_count = int(ent[0]["c"]) if ent else 0
        label_records, _, _ = self._driver.execute_query(
            f"MATCH (e:{ENTITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE e.organisation_id = $organisation_id "
            "UNWIND [l IN labels(e) WHERE NOT l STARTS WITH '__'] AS label "
            "RETURN label AS label, count(*) AS count ORDER BY count DESC LIMIT $top_n",
            graph_id=graph_id,
            organisation_id=org,
            top_n=top_n,
            database_=self._database,
        )
        rel_type_records, _, _ = self._driver.execute_query(
            f"MATCH (s:{ENTITY_LABEL} {{graph_id: $graph_id}})-[r]->"
            f"(e:{ENTITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE s.organisation_id = $organisation_id AND e.organisation_id = $organisation_id "
            "RETURN type(r) AS type, count(r) AS count ORDER BY count DESC LIMIT $top_n",
            graph_id=graph_id,
            organisation_id=org,
            top_n=top_n,
            database_=self._database,
        )
        top_entity_records, _, _ = self._driver.execute_query(
            f"MATCH (e:{ENTITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE e.organisation_id = $organisation_id "
            f"OPTIONAL MATCH (e)-[r {{graph_id: $graph_id}}]-(:{ENTITY_LABEL} "
            "{graph_id: $graph_id}) "
            "WITH e, count(r) AS degree "
            "RETURN coalesce(e.id, elementId(e)) AS entity_id, coalesce(e.name, '') AS name, "
            "degree ORDER BY degree DESC LIMIT $top_n",
            graph_id=graph_id,
            organisation_id=org,
            top_n=top_n,
            database_=self._database,
        )
        comm_count, _, _ = self._driver.execute_query(
            f"MATCH (c:{COMMUNITY_LABEL} {{graph_id: $graph_id}}) "
            "WHERE c.organisation_id = $organisation_id RETURN count(c) AS c",
            graph_id=graph_id,
            organisation_id=org,
            database_=self._database,
        )
        return {
            "node_count": node_count,
            "relationship_count": rel_count,
            "entity_count": entity_count,
            "density": (rel_count / (node_count * (node_count - 1))) if node_count > 1 else 0.0,
            "avg_degree": (2 * rel_count / node_count) if node_count > 0 else 0.0,
            "entity_types": [
                {"label": r["label"], "count": int(r["count"])} for r in label_records
            ],
            "relationship_types": [
                {"type": r["type"], "count": int(r["count"])} for r in rel_type_records
            ],
            "top_entities": [
                {
                    "entity_id": r["entity_id"],
                    "name": r["name"],
                    "degree": int(r["degree"]),
                }
                for r in top_entity_records
            ],
            "community_count": int(comm_count[0]["c"]) if comm_count else 0,
        }

    @staticmethod
    def _row_to_community(r: Mapping[str, Any]) -> Community:
        keywords = r.get("summary_keywords")
        summary_at = r.get("summary_at")
        return Community(
            community_id=r["community_id"],
            kind=r.get("kind") or ENTITY_KIND,
            level=int(r["level"]) if r.get("level") is not None else 0,
            entity_count=int(r["entity_count"]) if r.get("entity_count") is not None else 0,
            status=r.get("status") or "active",
            weight=float(r["weight"]) if r.get("weight") is not None else None,
            parent_id=r.get("parent_id"),
            summary=r.get("summary"),
            summary_keywords=list(keywords) if keywords else None,
            summary_excerpt=r.get("summary_excerpt"),
            summary_model=r.get("summary_model"),
            summary_at=(
                summary_at.to_native()
                if summary_at is not None and hasattr(summary_at, "to_native")
                else summary_at
            ),
            summary_source=r.get("summary_source"),
        )


def _pick_type(labels: list[str]) -> str:
    """The first non-bookkeeping label is the entity's domain type (legacy _pick_entity_type)."""
    for label in labels:
        if not label.startswith("__"):
            return label
    return "Entity"
