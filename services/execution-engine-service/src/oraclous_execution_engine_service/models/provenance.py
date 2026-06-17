"""EngineProvenanceEvent ORM model (ORAA-4 §21 models layer; CLAUDE.md §3.7, T7-M1).

The durable sink behind the substrate ``ProvenanceCollector``. Stores the five required provenance
fields per engine event (job.submit / job.run / job.cancel / schedule.fire / task.complete). The
owning job id is embedded in ``resource`` (e.g. ``engine_job:<id>``) so the audit trail
cross-references the engine job + the harness's own rows without a schema coupling.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import UUID
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
