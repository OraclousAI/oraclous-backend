"""Ingestion-job domain records (domain layer — pure, no I/O)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class IngestionJobRecord:
    """The job view returned to callers (no source payload)."""

    id: uuid.UUID
    organisation_id: uuid.UUID
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


@dataclass(frozen=True)
class IngestionPayload:
    """The fields the worker needs to process a job (includes the base64 source content)."""

    graph_id: uuid.UUID
    source_type: str
    filename: str | None
    source_content: str | None
    recipe_id: str | None
    valid_from: str | None
    valid_to: str | None
    event_time: str | None
