"""EngineProvenanceEvent ORM model (ORAA-4 §21 models layer; CLAUDE.md §3.7, T7-M1).

The durable sink behind the substrate ``ProvenanceCollector``. Stores the five required provenance
fields per engine event (job.submit / job.run / job.cancel / schedule.fire / task.complete). The
owning job id is embedded in ``resource`` (e.g. ``engine_job:<id>``) so the audit trail
cross-references the engine job + the harness's own rows without a schema coupling.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, String, Text
from sqlalchemy.dialects.postgresql import UUID

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineProvenanceEvent(BaseModel):
    __tablename__ = "engine_provenance"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    principal = Column(String(255), nullable=False)
    action = Column(String(128), nullable=False)
    resource = Column(String(512), nullable=False, index=True)
    outcome = Column(Text, nullable=False)
