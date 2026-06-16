"""Harness↔graph binding DTOs (ORAA-4 §21 schema layer; ADR-029 §6 / Contract §G2).

Pydantic request/response models only. ``organisation_id`` is never an inbound field (ORG001) — it
is resolved from the authenticated principal in the route. The FE labels these "workspace"/"agent";
the wire shapes use the real objects (``harness_id``/``graph_id``).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from oraclous_capability_registry_service.models.enums import DescriptorKind


class CreateBinding(BaseModel):
    """Attach an agent (a ``kind:harness`` capability) to a workspace (a graph)."""

    # Reject unknown fields so the runtime matches the OpenAPI `additionalProperties: false`
    # for this write DTO (a malformed body is a 422, not silently dropped).
    model_config = ConfigDict(extra="forbid")

    harness_id: uuid.UUID
    graph_id: uuid.UUID


class BoundAgent(BaseModel):
    """One agent bound to a workspace — the ``?graph_id=`` projection."""

    harness_id: uuid.UUID
    name: str | None
    kind: DescriptorKind
    summary: str | None


class BoundGraph(BaseModel):
    """One workspace a harness serves — the ``?harness_id=`` projection (live graphs only)."""

    graph_id: uuid.UUID
    name: str
