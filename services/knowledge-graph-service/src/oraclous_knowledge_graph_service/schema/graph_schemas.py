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
    # #724: pin THIS graph to one of the org's model credentials, overriding the org default for
    # every model call over its content. Omit to leave the pin untouched; "" clears it back to the
    # org default. An id only — the secret stays in the broker and is resolved per call.
    model_credential_id: str | None = Field(default=None, max_length=64)


class GraphResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    model_credential_id: str | None = None  # #724: the graph's pinned model credential, if any
    status: str
    node_count: int
    relationship_count: int
    created_at: datetime
    updated_at: datetime
    # #736 / ADR-051: the list is the ORGANISATION's workspaces, so a row must say whether the
    # caller owns it — that is what separates "mine" from "the organisation's" in the console, and
    # what tells it which rows offer rename/delete. Derived from the graph's own `user_id`: a
    # response field, never stored, never a second query.
    is_owner: bool

    @classmethod
    def of(cls, g: Graph, *, viewer_user_id: uuid.UUID) -> GraphResponse:
        return cls(
            id=g.id,
            name=g.name,
            description=g.description,
            model_credential_id=g.model_credential_id,
            status=g.status,
            node_count=g.node_count,
            relationship_count=g.relationship_count,
            created_at=g.created_at,
            updated_at=g.updated_at,
            is_owner=g.user_id == viewer_user_id,
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
