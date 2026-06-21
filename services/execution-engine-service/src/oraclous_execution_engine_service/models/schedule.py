"""EngineSchedule ORM model (ORAA-4 §21 models layer).

A durable schedule that fires a harness job. Org-scoped (ADR-006). ``cron`` schedules are fired by
Celery Beat (``fire_due``); ``manual`` schedules fire only via the API. ``last_fired_at`` is the
most recent window already fired — the at-least-once dedupe key (with the ``engine_jobs`` (org,
idempotency_key) unique constraint) so a duplicate beat tick never double-fires.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineSchedule(BaseModel):
    __tablename__ = "engine_schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(8), nullable=False)
    cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # the fired OHM: inline (self-contained) OR a registry ref (a registered kind=harness). One set.
    manifest_inline: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    manifest_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # WHAT this schedule fires (#489). ``harness_job`` (default, matching the migration
    # server_default so old rows read clean) fires a durable harness engine_job from the inline/ref
    # manifest; ``adopted_tool_run`` fires a capability-registry instance /execute (``instance_id``
    # + ``input_data``, no manifest).
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="harness_job")
    # the curated/adopted capability-registry instance to execute (adopted_tool_run only)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # the input payload forwarded to the instance /execute (adopted_tool_run only)
    input_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
