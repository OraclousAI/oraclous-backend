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
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from oraclous_knowledge_graph_service.domain.community import (
    DEFAULT_MIN_ENTITIES,
    DEFAULT_RESOLUTIONS,
    ENTITY_KIND,
    CommunitiesStatus,
    Community,
    CommunityKind,
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


# The synthetic ``ingestion_jobs.source_type`` for an async community-detection job. Reuses the
# existing job table + worker pattern (no new migration) — the row tracks detect progress/status.
COMMUNITY_DETECT_SOURCE_TYPE = "community_detect"

# Above this entity count, a detect runs ASYNC via Celery (the GDS sweep is heavy); at or below it,
# the route runs it inline so a small graph gets immediate results (the legacy "sync if small").
SYNC_DETECT_ENTITY_THRESHOLD = 2_000

EnqueueFn = Callable[[str, str], object]


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
        resolutions: tuple[float, ...] = DEFAULT_RESOLUTIONS,
    ) -> DetectionResult:
        """Run GDS Louvain detection across the 5 resolutions. Skips (not errors) when the graph has
        fewer than ``min_entities`` entities — too small to be worth clustering. Raises
        ``GdsUnavailableError`` (→503) if the GDS plugin is absent (the repo classifies it)."""
        await self._own(graph_id=graph_id, user_id=user_id)
        gid = str(graph_id)
        floor = DEFAULT_MIN_ENTITIES if min_entities is None else min_entities
        entity_count = await asyncio.to_thread(self._repo.count_entities, graph_id=gid)
        if entity_count < floor:
            return DetectionResult(
                graph_id=gid,
                status="skipped",
                total_communities=0,
                communities_per_level={},
                entities_processed=entity_count,
                reason=f"entity count {entity_count} < minimum {floor}",
            )
        levels_membership = await asyncio.to_thread(
            self._repo.detect, graph_id=gid, resolutions=resolutions
        )
        per_level = {level: len(groups) for level, groups in levels_membership.items()}
        return DetectionResult(
            graph_id=gid,
            status="completed",
            total_communities=sum(per_level.values()),
            communities_per_level=per_level,
            entities_processed=entity_count,
        )

    async def submit_detect(
        self,
        *,
        graph_id: uuid.UUID,
        user_id: uuid.UUID,
        min_entities: int | None = None,
    ) -> tuple[IngestionJobRecord | None, DetectionResult | None]:
        """Decide sync vs. async, and dispatch.

        Small graphs (≤ ``SYNC_DETECT_ENTITY_THRESHOLD`` entities) detect INLINE and return a
        ``DetectionResult`` (the route 200s). Larger graphs enqueue a ``community_detect`` job and
        return the ``IngestionJobRecord`` (the route 202s with ``{job_id,status}``). Owner-gated.

        Returns ``(job, None)`` for async or ``(None, result)`` for sync.
        """
        await self._own(graph_id=graph_id, user_id=user_id)
        gid = str(graph_id)
        entity_count = await asyncio.to_thread(self._repo.count_entities, graph_id=gid)
        run_sync = (
            entity_count <= SYNC_DETECT_ENTITY_THRESHOLD
            or self._jobs is None
            or self._enqueue is None
        )
        if run_sync:
            result = await self.detect(
                graph_id=graph_id, user_id=user_id, min_entities=min_entities
            )
            return None, result
        # Async: create + COMMIT the job row before enqueuing (the worker is a separate session —
        # mirrors JobService.submit / #267), then enqueue the Celery task with the org id.
        from oraclous_substrate.access import enforced_organisation_id

        job = await self._jobs.create(
            graph_id=graph_id,
            source_type=COMMUNITY_DETECT_SOURCE_TYPE,
            filename=None,
            source_content=None,
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
        """Detection status derived live from the substrate (no Postgres status column in the new
        build). ``not_detected`` when no community nodes exist; else ``active``. Staleness = the
        entity count grew since detection (level-0 communities cover fewer entities than now)."""
        await self._own(graph_id=graph_id, user_id=user_id)
        count, levels, entity_count = await asyncio.to_thread(
            self._repo.status, graph_id=str(graph_id)
        )
        if count == 0:
            return CommunitiesStatus(
                graph_id=str(graph_id),
                status="not_detected",
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
        self, *, graph_id: uuid.UUID, user_id: uuid.UUID, level: int | None = None
    ) -> int:
        """LLM-summarise the graph's communities (optionally one level). Returns the count done.

        Owner-gated. Raises ``SummarizationUnavailable`` (→503) when no LLM summarizer is configured
        (``KGS_EXTRACTOR`` is not ``openai``) — never silently no-ops, so a caller cannot mistake an
        unconfigured platform for "0 communities"."""
        await self._own(graph_id=graph_id, user_id=user_id)
        if self._summarizer is None:
            raise SummarizationUnavailable(
                "community summarisation is not configured (set KGS_EXTRACTOR=openai)"
            )
        results = await self._summarizer.summarize_graph(graph_id=str(graph_id), level=level)
        return len(results)
