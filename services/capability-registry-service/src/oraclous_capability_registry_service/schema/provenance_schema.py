"""Registry provenance read-surface DTOs (schema layer; #826, the 11 September ruling).

ONE event shape, additive to the engine's ``ActivityEvent``/``ActivityResponse``
(``execution-engine-service/schema/engine_schemas.py``): ``principal``, ``context``,
``input_hash``, and ``output_hash`` are newly exposed here. ``GET /api/v1/provenance``
(``routes/provenance_routes.py``) returns ``ProvenanceListResponse``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ProvenanceEvent(BaseModel):
    """One ``registry_provenance`` row (read-only projection). Org-scoped to the caller — never
    another tenant's row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: str
    resource: str
    outcome: str
    created_at: datetime | None
    principal: str
    context: dict[str, Any] | None
    input_hash: str | None
    output_hash: str | None


class ProvenanceListResponse(BaseModel):
    """The org's most-recent provenance events, newest-first (capped by ``limit``). ``total`` is the
    org's full event count, independent of the page ``limit`` — never merely ``len(events)``."""

    events: list[ProvenanceEvent]
    total: int
