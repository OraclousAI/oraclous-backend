"""Request/response DTOs (schema layer — Pydantic only, no logic, no persistence).

`organisation_id` is never an inbound field (ORG001) — it is resolved from the authenticated
principal context, never trusted from the body.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from oraclous_knowledge_graph_service.domain.graph import Graph


class CreateGraphRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class UpdateGraphRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class GraphResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: str
    node_count: int
    relationship_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, g: Graph) -> GraphResponse:
        return cls(
            id=g.id,
            name=g.name,
            description=g.description,
            status=g.status,
            node_count=g.node_count,
            relationship_count=g.relationship_count,
            created_at=g.created_at,
            updated_at=g.updated_at,
        )


class GraphGrantRequest(BaseModel):
    """Cross-org grant body (#446): the graph owner shares a READ with another org's user."""

    grantee_organisation_id: uuid.UUID
    grantee_user_id: uuid.UUID
    level: str = "read"  # read-only for this slice (write/admin deferred)


class GraphGrantResponse(BaseModel):
    graph_id: uuid.UUID
    grantee_organisation_id: uuid.UUID
    grantee_user_id: uuid.UUID
    level: str
    granted: bool
