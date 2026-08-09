"""Ingestion-job domain records (domain layer — pure, no I/O)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


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
    # #728 provenance — metadata, never part of the name. Defaulted so every existing construction
    # site (and every pre-provenance row) stays valid.
    producer_kind: str | None = None
    team_run_id: uuid.UUID | None = None
    member_role: str | None = None
    execution_id: uuid.UUID | None = None
    team_id: str | None = None
    ordinal: int | None = None
    content_hash: str | None = None


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
    # #728: the worker needs the producer kind to decide the DOCUMENT KEY. An agent artifact keys
    # on its own job id, so two members writing at once cannot merge onto one node; a user upload
    # keeps filename keying, so re-ingesting the same path still REPLACES (#522). None → upload.
    producer_kind: str | None = None
    #: The connector's SourceRef as stored, or None when the job carried no source. Kept as a
    #: plain mapping so the domain layer stays free of the shared model; the worker validates it
    #: back into a ``SourceRef`` at the point it mints.
    source: dict[str, Any] | None = None
