"""EngineProvenanceEvent ORM model (models layer; CLAUDE.md §3.7, T7-M1).

The durable sink behind the substrate ``ProvenanceCollector``. Stores the five required provenance
fields per engine event (job.submit / job.run / job.cancel / schedule.fire / task.complete). The
owning job id is embedded in ``resource`` (e.g. ``engine_job:<id>``) so the audit trail
cross-references the engine job + the harness's own rows without a schema coupling.

#826 (24 August / 11 September 2026 rulings): three additive, NULLABLE columns extend the record —
``context`` (structured per-call detail, e.g. a team-run member's role), ``input_hash`` /
``output_hash`` (``sha256:<hex>`` content fingerprints, never the raw payload — CLAUDE.md §11). All
three are nullable: a lifecycle event (no input/output) has nothing to attest. The five original
columns are unchanged and stay required — the widening is purely additive.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineProvenanceEvent(BaseModel):
    __tablename__ = "engine_provenance"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    principal: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    # #826: additive extension fields — nullable, never swept into the required-field check.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
