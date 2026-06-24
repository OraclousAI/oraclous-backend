"""Ingestion request/response DTOs (schema layer — Pydantic only).

`organisation_id` is never exposed (ORG001) — it is internal scope, not a client field.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord


class BatchIngestItem(BaseModel):
    """One file in a batch/folder ingest (#522). ``path`` is the document identity (the relative
    path in the folder) — re-ingest of the same path REPLACES, so a refresh is idempotent."""

    path: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_type: str = "text"
    recipe_id: str | None = None


class BatchIngestRequest(BaseModel):
    """Land a FOLDER/REPO of content in one call (#522) — one async ingest job per item. At least
    one item (an empty batch is a 422); ``organisation_id`` is never a body field (bound from the
    principal)."""

    items: list[BatchIngestItem] = Field(min_length=1)


class IngestTextRequest(BaseModel):
    content: str = Field(min_length=1)
    filename: str | None = None
    source_type: str = "text"  # text|md|csv|json|... — structured types route to the recipe engine
    recipe_id: str | None = None  # structured only: a stored recipe (else a default is synthesised)
    valid_from: str | None = None  # temporal passthrough (structured) — stamped on entity nodes
    valid_to: str | None = None
    event_time: str | None = None


class InternalIngestRequest(BaseModel):
    """The agent-addressable internal ingest body (Slice C).

    Carries the target ``graph_id`` in the body (the internal route is not nested under a path
    graph id, mirroring the internal SEARCH/SCHEMA endpoints). ``content`` (alias
    ``source_content``) is the inline source text; ``source_type`` defaults to plain text.
    ``organisation_id`` is NEVER a client field (ORG001) — the org is bound from the forwarded
    principal, not the body.
    """

    graph_id: uuid.UUID
    content: str = Field(min_length=1, validation_alias="source_content")
    source_type: str = "text"
    recipe_id: str | None = None

    model_config = {"populate_by_name": True}


class SqlIngestRequest(BaseModel):
    """A relational (SQL) ingest request (#307).

    The connection secret is NEVER in the body — only the broker ``credential_id`` (a stored
    ``connection_string`` the broker resolves). ``organisation_id`` is NEVER a client field (ORG001)
    — the org is server-injected from the principal. ``graph_id`` is the path scope.
    """

    credential_id: str = Field(min_length=1)
    sync_mode: str = "full_snapshot"  # full_snapshot | schema_only
    # The DB schema to introspect (Postgres default: public). It is interpolated into a query as a
    # quoted identifier downstream, so the boundary rejects anything that is NOT a strict SQL
    # identifier (defense in depth alongside the connector's schema allowlist + quote-escaping): a
    # leading letter/underscore then word chars / `$`, bounded length — no quotes, dots, or spaces
    # that could attempt to break out of the identifier.
    schema_name: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_$]*$"
    )
    recipe_id: str | None = None  # a stored recipe; else a default relational recipe is synthesised


class SqlIngestResponse(BaseModel):
    """Synchronous SQL-ingest result: the introspection summary + the projected graph counts."""

    graph_id: uuid.UUID
    dialect: str
    database: str
    schema_name: str
    sync_mode: str
    tables_introspected: int
    nodes_written: int
    edges_written: int
    containers_written: int
    properties_written: int
    units_skipped: int
    warnings: list[str]


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


class BatchIngestResponse(BaseModel):
    """The batch/folder ingest result (#522): one enqueued job per item (poll each via
    ``GET /jobs/{id}``). ``organisation_id`` is never exposed."""

    jobs: list[JobResponse]


class GraphIdName(BaseModel):
    """One accessible graph (id + display name) in the federation accessible-set (#330)."""

    id: uuid.UUID
    name: str


class InternalGraphListResponse(BaseModel):
    """The caller's org's graphs — the exact set a federated query may fan out over (ADR-026).

    Served on the internal plane (X-Internal-Key, ADR-018) for the knowledge-retriever; the org is
    bound from the FORWARDED principal, never a request field.
    """

    graphs: list[GraphIdName]


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
