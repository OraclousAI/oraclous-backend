"""EngineSchedule ORM model (ORAA-4 §21 models layer).

A durable schedule that fires a harness job. Org-scoped (ADR-006). ``cron`` schedules are fired by
Celery Beat (``fire_due``); ``manual`` schedules fire only via the API. ``last_fired_at`` is the
most recent window already fired — the at-least-once dedupe key (with the ``engine_jobs`` (org,
idempotency_key) unique constraint) so a duplicate beat tick never double-fires.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineSchedule(BaseModel):
    __tablename__ = "engine_schedules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    type = Column(String(8), nullable=False)
    cron = Column(String(128), nullable=True)
    # the fired OHM: inline (self-contained) OR a registry ref (a registered kind=harness). One set.
    manifest_inline = Column(JSONB, nullable=True)
    manifest_ref = Column(String(512), nullable=True)
    input_text = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    last_fired_at = Column(DateTime(timezone=True), nullable=True)
