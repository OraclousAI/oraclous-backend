"""Agent-memory request/response DTOs (ORAA-4 §21 schema layer — Pydantic only).

Issue #332 / ADR-027 §1/§4. Shapes mirror the legacy ``app/schemas/memory.py`` (develop@84152635)
verbatim, so a legacy memory client ports unchanged. ``organisation_id`` is never a client field
(ORG001) — org scope is bound from the principal; ``graph_id`` is the path scope on the user-facing
routes and an OPTIONAL body field only on the internal (harness) store route, where an absent id
falls back to the org-default memory graph.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# ==================== ENUMS (legacy verbatim) ====================


class MemoryType(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryScope(StrEnum):
    SESSION = "session"
    USER = "user"
    AGENT = "agent"
    TEAM = "team"
    ORGANIZATION = "organization"


class MemorySource(StrEnum):
    AGENT = "agent"
    USER_FEEDBACK = "user_feedback"
    INGESTION = "ingestion"
    INFERENCE = "inference"


class ContradictionResolution(StrEnum):
    NEW_WINS = "new_wins"
    OLD_WINS = "old_wins"
    UNRESOLVED = "unresolved"
    MERGED = "merged"


class TemporalFilter(StrEnum):
    CURRENT = "current"
    ALL = "all"


# ==================== REQUESTS ====================


class MemoryCreate(BaseModel):
    """POST /api/v1/graphs/{graph_id}/memories — legacy body verbatim."""

    type: MemoryType
    content: str = Field(min_length=1, max_length=10_000)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    scope: MemoryScope = MemoryScope.AGENT
    agent_id: str | None = None
    session_id: str | None = None
    source: MemorySource = MemorySource.AGENT
    valid_from: datetime | None = None

    # Semantic-specific
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    is_negation: bool = False

    # Episodic-specific
    event_type: str | None = None
    user_id: str | None = None

    # Procedural-specific
    category: str | None = None
    trigger_pattern: str | None = None


class InternalMemoryCreate(MemoryCreate):
    """POST /internal/v1/memories — the harness post-run hook's body (ADR-027 §5).

    Carries an OPTIONAL ``graph_id``: when the run has a graph context the harness sends it; when
    absent the service lazily finds-or-creates the org-default memory graph. The org itself is
    NEVER a body field — it is bound from the forwarded principal (ADR-018).
    """

    graph_id: uuid.UUID | None = None


class MemoryUpdate(BaseModel):
    """PATCH /api/v1/graphs/{graph_id}/memories/{memory_id} — supersede (legacy verbatim)."""

    content: str | None = Field(default=None, min_length=1, max_length=10_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = None


# ==================== RESPONSES (legacy verbatim) ====================


class ConflictInfo(BaseModel):
    conflict_memory_id: str
    content: str
    resolution: ContradictionResolution


class MemoryCreateResponse(BaseModel):
    memory_id: str
    importance_score: float
    contradictions_detected: list[ConflictInfo] = []
    entity_linked: str | None = None


class InternalMemoryCreateResponse(MemoryCreateResponse):
    """The internal store response also names the graph the memory landed in (the harness logs it
    but never depends on it — the write is fire-and-forget)."""

    graph_id: uuid.UUID


class MemorySearchResult(BaseModel):
    memory_id: str
    type: MemoryType
    content: str
    importance_score: float
    relevance_score: float
    confidence: float
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    scope: MemoryScope
    agent_id: str | None = None
    session_id: str | None = None
    created_at: datetime | None = None
    last_accessed_at: datetime | None = None
    access_count: int = 0


class MemorySearchResponse(BaseModel):
    memories: list[MemorySearchResult]
    total: int


class MemoryContext(BaseModel):
    context_block: str
    memories_used: list[str]
    token_estimate: int
    retrieval_ms: int


class MemoryUpdateResponse(BaseModel):
    old_memory_id: str
    new_memory_id: str
    superseded_at: datetime


class ConsolidateResponse(BaseModel):
    job_id: str
    message: str
