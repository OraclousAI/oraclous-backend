"""Community request/response DTOs (ORAA-4 §21 schema layer — Pydantic only, no logic) (#303).

The wire contract for the community + analytics surface, mirroring the legacy shapes
(``community_schemas.Community``, the ``/analytics`` body, the detect ``{job_id,status}``).
``organisation_id`` is NEVER an inbound field — it is resolved from the principal context. The
response builders are pure ``of(...)`` adapters off the domain objects.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from oraclous_knowledge_graph_service.domain.community import (
    CommunitiesStatus,
    Community,
    CommunityKind,
    DetectionResult,
    GraphAnalytics,
)


class CommunityKindResponse(BaseModel):
    kind: str
    display_name: str
    community_label: str
    member_label: str
    hierarchical: bool
    detection_supported: bool

    @classmethod
    def of(cls, k: CommunityKind) -> CommunityKindResponse:
        return cls(
            kind=k.kind,
            display_name=k.display_name,
            community_label=k.community_label,
            member_label=k.member_label,
            hierarchical=k.hierarchical,
            detection_supported=k.detection_supported,
        )


class CommunityMemberResponse(BaseModel):
    entity_id: str
    entity_name: str
    entity_type: str


class CommunityResponse(BaseModel):
    community_id: str
    kind: str
    level: int
    label: str
    size: int
    summary: str | None = None
    summary_keywords: list[str] | None = None
    summary_excerpt: str | None = None
    summary_model: str | None = None
    summary_at: datetime | None = None
    # "llm" for a real model summary, "fallback" for the member-derived degrade (provenance).
    summary_source: str | None = None
    weight: float | None = None
    parent_id: str | None = None
    members: list[CommunityMemberResponse] = Field(default_factory=list)

    @classmethod
    def of(cls, c: Community) -> CommunityResponse:
        return cls(
            community_id=c.community_id,
            kind=c.kind,
            level=c.level,
            label=_derive_label(c.community_id, c.summary),
            size=c.entity_count,
            summary=c.summary,
            summary_keywords=c.summary_keywords,
            summary_excerpt=c.summary_excerpt,
            summary_model=c.summary_model,
            summary_at=c.summary_at,
            summary_source=c.summary_source,
            weight=c.weight,
            parent_id=c.parent_id,
            members=[
                CommunityMemberResponse(
                    entity_id=m.entity_id, entity_name=m.entity_name, entity_type=m.entity_type
                )
                for m in c.members
            ],
        )


class DetectRequest(BaseModel):
    """Body for ``POST /communities/detect``. Both fields optional (sensible defaults)."""

    min_entities: int | None = Field(default=None, ge=0)
    force_rebuild: bool = False


class DetectAcceptedResponse(BaseModel):
    """202 body when detection is enqueued async (matches the legacy ``{job_id,status}`` shape)."""

    job_id: str
    status: str


class DetectionResultResponse(BaseModel):
    """Sync detection outcome (small graphs run inline)."""

    graph_id: str
    status: str
    total_communities: int
    communities_per_level: dict[int, int]
    entities_processed: int
    reason: str | None = None

    @classmethod
    def of(cls, r: DetectionResult) -> DetectionResultResponse:
        return cls(
            graph_id=r.graph_id,
            status=r.status,
            total_communities=r.total_communities,
            communities_per_level=r.communities_per_level,
            entities_processed=r.entities_processed,
            reason=r.reason,
        )


class CommunitiesStatusResponse(BaseModel):
    graph_id: str
    status: str
    communities_count: int
    levels: list[int]
    entity_count: int
    entity_count_at_detection: int
    is_stale: bool

    @classmethod
    def of(cls, s: CommunitiesStatus) -> CommunitiesStatusResponse:
        return cls(
            graph_id=s.graph_id,
            status=s.status,
            communities_count=s.communities_count,
            levels=s.levels,
            entity_count=s.entity_count,
            entity_count_at_detection=s.entity_count_at_detection,
            is_stale=s.is_stale,
        )


class SummarizeResponse(BaseModel):
    """Outcome of a summarise run.

    ``status`` is ``"completed"`` when the run finished inline, or ``"deferred"`` when the candidate
    count exceeded the inline cap and NONE ran (the caller should use the async detect path, which
    summarises on the worker) — so a capped run is DISTINGUISHABLE from a completed one that had
    nothing to do (both could otherwise show ``summarized=0``). ``deferred`` carries the candidate
    count skipped on a deferral.
    """

    graph_id: str
    summarized: int
    status: str = "completed"
    deferred: int = 0


class AnalyticsResponse(BaseModel):
    graph_id: str
    node_count: int
    relationship_count: int
    entity_count: int
    density: float
    avg_degree: float
    entity_types: list[dict]
    relationship_types: list[dict]
    top_entities: list[dict]
    community_count: int
    computed_at: datetime

    @classmethod
    def of(cls, a: GraphAnalytics) -> AnalyticsResponse:
        return cls(
            graph_id=a.graph_id,
            node_count=a.node_count,
            relationship_count=a.relationship_count,
            entity_count=a.entity_count,
            density=a.density,
            avg_degree=a.avg_degree,
            entity_types=a.entity_types,
            relationship_types=a.relationship_types,
            top_entities=a.top_entities,
            community_count=a.community_count,
            computed_at=a.computed_at,
        )


def _derive_label(community_id: str, summary: str | None) -> str:
    """A short human-readable label: first sentence of the summary (legacy ``_derive_label``), else
    a synthetic ``Community <short-id>``."""
    if summary:
        cleaned = summary.strip()
        for terminator in (". ", "\n"):
            idx = cleaned.find(terminator)
            if idx > 0:
                cleaned = cleaned[:idx]
                break
        return cleaned[:80]
    short = community_id.replace("community_", "")[:8]
    return f"Community {short}"
