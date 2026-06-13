"""Domain entities (ORAA-4 §21 domain layer — pure, no I/O)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Graph:
    """A knowledge graph aggregate — the domain view, independent of ORM/HTTP."""

    id: uuid.UUID
    organisation_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: str | None
    status: str
    node_count: int
    relationship_count: int
    created_at: datetime
    updated_at: datetime
    # Reserved-graph marker (#332 / ADR-027 §5): NULL for a user graph, a reserved value (e.g.
    # `agent_memory`) for a system-owned graph. Defaulted so existing constructors are unaffected.
    system_kind: str | None = None
