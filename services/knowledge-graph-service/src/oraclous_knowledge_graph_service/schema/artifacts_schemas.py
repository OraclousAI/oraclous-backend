"""/v1/artifacts response schemas (#543, ADR-041) — the unified artifact read/serve surface.

A team's outputs live on Oraclous (graph-indexed); these are the shapes served by /v1/artifacts.
``organisation_id`` is NEVER exposed (ORG001) — it is internal scope, not a client field. The list
returns summaries (no verbatim content); a single GET serves the content.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord


class ArtifactSummary(BaseModel):
    """One artifact in the list — metadata only (no verbatim content)."""

    id: uuid.UUID
    graph_id: uuid.UUID
    filename: str | None
    source_type: str
    status: str
    extracted_entities: int
    extracted_relationships: int
    created_at: datetime
    # #728: who produced this artifact. NULL on a user upload and on every row that predates the
    # provenance columns; ``organisation_id`` stays internal (ORG001) and is still never exposed.
    producer_kind: str | None = None
    team_run_id: uuid.UUID | None = None
    member_role: str | None = None
    execution_id: uuid.UUID | None = None
    team_id: str | None = None
    ordinal: int | None = None
    content_hash: str | None = None

    @classmethod
    def of(cls, rec: IngestionJobRecord) -> ArtifactSummary:
        return cls(
            id=rec.id,
            graph_id=rec.graph_id,
            filename=rec.filename,
            source_type=rec.source_type,
            status=rec.status,
            extracted_entities=rec.extracted_entities,
            extracted_relationships=rec.extracted_relationships,
            created_at=rec.created_at,
            producer_kind=rec.producer_kind,
            team_run_id=rec.team_run_id,
            member_role=rec.member_role,
            execution_id=rec.execution_id,
            team_id=rec.team_id,
            ordinal=rec.ordinal,
            content_hash=rec.content_hash,
        )


class ArtifactDetail(ArtifactSummary):
    """A single artifact + its verbatim ingested ``content`` (the served file)."""

    content: str | None

    @classmethod
    def of_with_content(cls, rec: IngestionJobRecord, content: str | None) -> ArtifactDetail:
        base = ArtifactSummary.of(rec).model_dump()
        return cls(**base, content=content)
