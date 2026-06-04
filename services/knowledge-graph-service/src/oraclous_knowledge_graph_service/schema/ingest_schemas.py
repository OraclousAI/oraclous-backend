"""Ingestion request/response DTOs (ORAA-4 §21 schema layer — Pydantic only).

`organisation_id` is never exposed (ORG001) — it is internal scope, not a client field.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord


class IngestTextRequest(BaseModel):
    content: str = Field(min_length=1)
    filename: str | None = None
    source_type: str = "text"  # text|md|csv|json|... — structured types route to the recipe engine
    recipe_id: str | None = None  # structured only: a stored recipe (else a default is synthesised)
    valid_from: str | None = None  # temporal passthrough (structured) — stamped on entity nodes
    valid_to: str | None = None
    event_time: str | None = None


class JobResponse(BaseModel):
    id: uuid.UUID
    graph_id: uuid.UUID
    source_type: str
    filename: str | None
    status: str
    progress: int
    error_message: str | None
    extracted_entities: int
    extracted_relationships: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, job: IngestionJobRecord) -> JobResponse:
        return cls(
            id=job.id,
            graph_id=job.graph_id,
            source_type=job.source_type,
            filename=job.filename,
            status=job.status,
            progress=job.progress,
            error_message=job.error_message,
            extracted_entities=job.extracted_entities,
            extracted_relationships=job.extracted_relationships,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


class LabelCount(BaseModel):
    label: str
    count: int


class RelTypeCount(BaseModel):
    type: str
    count: int


class SchemaResponse(BaseModel):
    graph_id: uuid.UUID
    labels: list[LabelCount]
    relationships: list[RelTypeCount]
