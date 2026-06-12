"""Graph-analytics + community use-cases (ORAA-4 §21 services layer — all business logic) (#303).

Domain orchestration over the in-DB GDS Louvain :class:`CommunityRepository`: detect (the 5-level
multi-resolution hierarchy), list (filter by level + kind), get one (with members), status, and the
graph ``/analytics`` summary. RE-ARCHITECTS the legacy ``GraphAnalyticsService`` — the
``leidenalg``/``igraph`` in-memory pipeline is gone; this layer only orchestrates the repository's
in-DB Louvain.

Authz: owner-gated (a graph in another org/owner is invisible — 404, no leak) on top of the
fail-closed org scope the repository enforces. Neo4j access is the repository's alone; this layer
holds NO Cypher and NO driver (STR004) — it wraps the sync repo calls in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.domain.community import (
    COMMUNITY_DETECT_SOURCE_TYPE,
    DEFAULT_MIN_ENTITIES,
    ENTITY_KIND,
    CommunitiesStatus,
    Community,
    CommunityKind,
    DetectionInProgress,
    DetectionResult,
    GraphAnalytics,
    entity_kinds,
)
from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord
from oraclous_knowledge_graph_service.repositories.community_repository import CommunityRepository
from oraclous_knowledge_graph_service.repositories.job_repository import IngestionJobRepository
from oraclous_knowledge_graph_service.services.community_summarizer import CommunitySummarizer
from oraclous_knowledge_graph_service.services.graph_service import GraphService


class SummarizationUnavailable(Exception):
    """Raised when summarisation is requested but no LLM summarizer is configured. Maps to 503."""


EnqueueFn = Callable[[str, str], object]


def encode_detect_params(*, min_entities: int | None, force_rebuild: bool) -> str:
    """Encode the async-detect request params into the job's ``source_content`` (a JSON string).

    Reuses the existing ``ingestion_jobs.source_content`` column (no migration) so the worker can
    recover the caller's ``min_entities`` (else dropped on the async path) and ``force_rebuild``.
    """
    return json.dumps({"min_entities": min_entities, "force_rebuild": force_rebuild})


def decode_detect_params(source_content: str | None) -> tuple[int | None, bool]:
    """Decode ``(min_entities, force_rebuild)`` from a detect job's ``source_content``; tolerant of
    a missing/garbled payload (older rows / a hand-made job) → ``(None, False)``."""
    if not source_content:
        return None, False
    try:
        data = json.loads(source_content)
        me = data.get("min_entities")
        return (int(me) if me is not None else None), bool(data.get("force_rebuild", False))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, False


class UnknownCommunityKind(Exception):
    """Raised when a caller asks for a community kind the platform does not know. Maps to 400."""


class AnalyticsService:
    """Community detection + graph analytics use-cases. Owner-gated; no Cypher (repo owns Neo4j).

    ``job_repo`` + ``enqueue`` are injected for the async detect path (create + commit a
    ``community_detect`` job row, then enqueue the Celery task). They are optional so unit tests can
    exercise the sync detect / read paths without a broker or DB job table.
    """

    def __init__(
        self,
        *,
        graph_service: GraphService,
        repo: CommunityRepository,
        job_repo: IngestionJobRepository | None = None,
        enqueue: EnqueueFn | None = None,
        summarizer: CommunitySummarizer | None = None,
    ) -> None:
        self._graphs = graph_service
        self._repo = repo
        self._jobs = job_repo
        self._enqueue = enqueue
        self._summarizer = summarizer

    @staticmethod
    def kinds() -> list[CommunityKind]:
        """The community-kind registry (the discovery endpoint). Auth only — no graph scope."""
        return entity_kinds()

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in {k.kind for k in entity_kinds()}:
            raise UnknownCommunityKind(kind)

    async def _own(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Owner gate (raises GraphNotFound → 404). The org scope is enforced in the repository."""
        await self._graphs.get_graph(graph_id=graph_id, user_id=user_id)

    async def detect(
        self,
        *,
        graph_id: uuid.UUID,
        user_id: uuid.UUID,
        min_entities: int | None = None,
        force_rebuild: bool = False,
    ) -> DetectionResult:
        """Run a single GDS Louvain native-dendrogram detection. Owner-gated.

        Skips (status ``"skipped"``, not an error) when: the graph has fewer than ``min_entities``
        entities (too small); the graph EXCEEDS ``community_max_entities`` (would risk OOM on the
        512m Neo4j); communities already exist and ``force_rebuild`` is False (a no-op so re-detect
        doesn't needlessly clear+rebuild); or another detect already holds the per-graph lock.
        Raises ``GdsUnavailableError`` (→503) if the GDS plugin is absent (the repo classifies it).
        """
        await self._own(graph_id=graph_id, user_id=user_id)
        gid = str(graph_id)
        settings = get_settings()
        floor = DEFAULT_MIN_ENTITIES if min_entities is None else min_entities
        entity_count = await asyncio.to_thread(self._repo.count_entities, graph_id=gid)
        if entity_count < floor:
            return self._skipped(
                gid, entity_count, f"entity count {entity_count} < minimum {floor}"
            )
        cap = settings.community_max_entities
        if cap and entity_count > cap:
            return self._skipped(
                gid, entity_count, f"entity count {entity_count} exceeds maximum {cap}"
            )
        if not force_rebuild:
            existing, _, _ = await asyncio.to_thread(self._repo.status, graph_id=gid)
            if existing > 0:
                return self._skipped(
                    gid,
                    entity_count,
                    "communities already detected; pass force_rebuild to rebuild",
                )
        try:
            levels_membership = await asyncio.to_thread(self._repo.detect, graph_id=gid)
        except DetectionInProgress:
            return self._skipped(gid, entity_count, "community detection already in progress")
        per_level = {level: len(groups) for level, groups in levels_membership.items()}
        return DetectionResult(
            graph_id=gid,
            status="completed",
            total_communities=sum(per_level.values()),
            communities_per_level=per_level,
            entities_processed=entity_count,
        )

    @staticmethod
    def _skipped(gid: str, entity_count: int, reason: str) -> DetectionResult:
        return DetectionResult(
            graph_id=gid,
            status="skipped",
            total_communities=0,
            communities_per_level={},
            entities_processed=entity_count,
            reason=reason,
        )

    async def submit_detect(
        self,
        *,
        graph_id: uuid.UUID,
        user_id: uuid.UUID,
        min_entities: int | None = None,
        force_rebuild: bool = False,
    ) -> tuple[IngestionJobRecord | None, DetectionResult | None]:
        """Decide sync vs. async, and dispatch.

        Tiny graphs (≤ ``community_sync_entity_threshold`` entities) detect INLINE under a bounded
        timeout and return a ``DetectionResult`` (the route 200s). If the inline run overruns the
        timeout, or the graph is larger, or no broker is wired, it enqueues a ``community_detect``
        job carrying ``min_entities``/``force_rebuild`` and returns the ``IngestionJobRecord`` (the
        route 202s with ``{job_id,status}``). Owner-gated.

        Returns ``(job, None)`` for async or ``(None, result)`` for sync.
        """
        await self._own(graph_id=graph_id, user_id=user_id)
        gid = str(graph_id)
        settings = get_settings()
        entity_count = await asyncio.to_thread(self._repo.count_entities, graph_id=gid)
        can_async = self._jobs is not None and self._enqueue is not None
        if entity_count <= settings.community_sync_entity_threshold or not can_async:
            try:
                result = await asyncio.wait_for(
                    self.detect(
                        graph_id=graph_id,
                        user_id=user_id,
                        min_entities=min_entities,
                        force_rebuild=force_rebuild,
                    ),
                    timeout=settings.community_sync_timeout_seconds,
                )
                return None, result
            except TimeoutError:
                if not can_async:
                    raise
                # A tiny graph that nonetheless overran the inline budget: fall back to the worker.
        return await self._enqueue_detect(
            graph_id=graph_id, min_entities=min_entities, force_rebuild=force_rebuild
        )

    async def _enqueue_detect(
        self, *, graph_id: uuid.UUID, min_entities: int | None, force_rebuild: bool
    ) -> tuple[IngestionJobRecord, None]:
        # Async: create + COMMIT the job row before enqueuing (the worker is a separate session —
        # mirrors JobService.submit / #267), then enqueue the Celery task with the org id. The
        # request params ride on source_content so the worker can apply them.
        from oraclous_substrate.access import enforced_organisation_id

        assert self._jobs is not None and self._enqueue is not None  # noqa: S101 — guarded by caller
        job = await self._jobs.create(
            graph_id=graph_id,
            source_type=COMMUNITY_DETECT_SOURCE_TYPE,
            filename=None,
            source_content=encode_detect_params(
                min_entities=min_entities, force_rebuild=force_rebuild
            ),
        )
        await self._jobs.commit()
        self._enqueue(str(job.id), enforced_organisation_id())
        return job, None

    async def list_communities(
        self,
        *,
        graph_id: uuid.UUID,
        user_id: uuid.UUID,
        level: int | None = None,
        kind: str = ENTITY_KIND,
        min_entities: int = 1,
    ) -> list[Community]:
        """List the graph's communities, filtered by level + kind. Empty list when none detected."""
        self._validate_kind(kind)
        await self._own(graph_id=graph_id, user_id=user_id)
        return await asyncio.to_thread(
            self._repo.list_communities,
            graph_id=str(graph_id),
            level=level,
            min_entities=min_entities,
        )

    async def get_community(
        self, *, graph_id: uuid.UUID, user_id: uuid.UUID, community_id: str
    ) -> Community | None:
        """One community with its member entities (None → 404 at the route, incl. cross-org ids)."""
        await self._own(graph_id=graph_id, user_id=user_id)
        return await asyncio.to_thread(
            self._repo.get_community, graph_id=str(graph_id), community_id=community_id
        )

    async def status(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> CommunitiesStatus:
        """Detection status: substrate counts FOLDED WITH the latest ``community_detect`` job row.

        The substrate alone is split-brained — a running/failed async detect is invisible, and right
        after the clear it shows ``not_detected`` mid-run. So when no communities exist yet we check
        the latest detect job: ``running`` (pending/running) or ``failed`` (errored). Otherwise
        ``active``/``not_detected`` derive from the substrate. Staleness = the entity count grew
        since detection."""
        await self._own(graph_id=graph_id, user_id=user_id)
        count, levels, entity_count = await asyncio.to_thread(
            self._repo.status, graph_id=str(graph_id)
        )
        if count == 0:
            job_status = await self._latest_detect_job_status(graph_id=graph_id)
            return CommunitiesStatus(
                graph_id=str(graph_id),
                status=job_status,
                communities_count=0,
                levels=[],
                entity_count=entity_count,
                entity_count_at_detection=0,
                is_stale=False,
            )
        # Entities covered by the coarsest level approximate the count at detection time; if the
        # live entity count exceeds it, new ingestion has happened since — communities are stale.
        communities = await asyncio.to_thread(
            self._repo.list_communities,
            graph_id=str(graph_id),
            level=min(levels),
            min_entities=1,
        )
        at_detection = sum(c.entity_count for c in communities)
        return CommunitiesStatus(
            graph_id=str(graph_id),
            status="active",
            communities_count=count,
            levels=levels,
            entity_count=entity_count,
            entity_count_at_detection=at_detection,
            is_stale=entity_count > at_detection,
        )

    async def _latest_detect_job_status(self, *, graph_id: uuid.UUID) -> str:
        """``running``/``failed``/``not_detected`` from the latest ``community_detect`` job row.

        Only consulted when the substrate has no communities — folds an in-flight or just-failed
        async detect into the status so it is not invisibly reported as ``not_detected``."""
        if self._jobs is None:
            return "not_detected"
        job = await self._jobs.latest_by_source_type(
            graph_id, source_type=COMMUNITY_DETECT_SOURCE_TYPE
        )
        if job is None:
            return "not_detected"
        if job.status in ("pending", "running"):
            return "running"
        if job.status == "failed":
            return "failed"
        # A completed job that left no communities (e.g. a skip) reads as not_detected.
        return "not_detected"

    async def analytics(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> GraphAnalytics:
        """Graph statistics (the legacy ``/analytics`` shape), org+graph scoped + owner-gated."""
        await self._own(graph_id=graph_id, user_id=user_id)
        data = await asyncio.to_thread(self._repo.analytics, graph_id=str(graph_id))
        return GraphAnalytics(
            graph_id=str(graph_id),
            node_count=int(data["node_count"]),
            relationship_count=int(data["relationship_count"]),
            entity_count=int(data["entity_count"]),
            density=float(data["density"]),
            avg_degree=float(data["avg_degree"]),
            entity_types=list(data["entity_types"]),
            relationship_types=list(data["relationship_types"]),
            top_entities=list(data["top_entities"]),
            community_count=int(data["community_count"]),
            computed_at=datetime.now(UTC),
        )

    async def summarize(
        self,
        *,
        graph_id: uuid.UUID,
        user_id: uuid.UUID,
        level: int | None = None,
        force: bool = False,
    ) -> int:
        """LLM-summarise the graph's communities (optionally one level). Returns the count done.

        Cost-aware (a real LLM call per community bills): by default it summarises only communities
        that have NO summary yet (``force`` re-summarises all), so a re-run resumes after a partial
        failure and doesn't re-bill. Above ``community_summarize_max_inline`` candidate communities
        the work is too large to block the request — it returns 0 (the caller should use the async
        detect path, which summarises inline on the worker). Owner-gated. Raises
        ``SummarizationUnavailable`` (→503) when no LLM summarizer is configured (``KGS_EXTRACTOR``
        is not ``openai``) — never silently no-ops."""
        await self._own(graph_id=graph_id, user_id=user_id)
        if self._summarizer is None:
            raise SummarizationUnavailable(
                "community summarisation is not configured (set KGS_EXTRACTOR=openai)"
            )
        cap = get_settings().community_summarize_max_inline
        results = await self._summarizer.summarize_graph(
            graph_id=str(graph_id), level=level, force=force, max_communities=cap or None
        )
        return len(results)
