"""EngineJob ORM model (ORAA-4 §21 models layer).

The durable job record: one harness run tracked out-of-request through the engine state machine
(``EngineJobState``). Org-scoped (ADR-006). Columns beyond S1 (retry/timeout/schedule/assignment/
idempotency) exist now so later slices add no ALTERs. ``harness_execution_id`` cross-references the
harness's own run row (no schema coupling — both live in one Postgres, separate tables).

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineJob(BaseModel):
    __tablename__ = "engine_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)

    # the OHM to run — exactly one of a registered ref or an inline manifest.
    manifest_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    manifest_inline: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)

    harness_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
