"""EngineJob ORM model (ORAA-4 §21 models layer).

The durable job record: one harness run tracked out-of-request through the engine state machine
(``EngineJobState``). Org-scoped (ADR-006). Columns beyond S1 (retry/timeout/schedule/assignment/
idempotency) exist now so later slices add no ALTERs. ``harness_execution_id`` cross-references the
harness's own run row (no schema coupling — both live in one Postgres, separate tables).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineJob(BaseModel):
    __tablename__ = "engine_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    state = Column(String(16), nullable=False)

    # the OHM to run — exactly one of a registered ref or an inline manifest.
    manifest_ref = Column(String(512), nullable=True)
    manifest_inline = Column(JSONB, nullable=True)
    input_text = Column(Text, nullable=False)

    harness_execution_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    assignment_id = Column(UUID(as_uuid=True), nullable=True)
    schedule_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    retry_count = Column(Integer, nullable=False, default=0)
    max_retries = Column(Integer, nullable=False, default=0)
    timeout_seconds = Column(Integer, nullable=True)
    progress = Column(Integer, nullable=False, default=0)

    output = Column(Text, nullable=True)
    error_type = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)
    idempotency_key = Column(String(255), nullable=True)
