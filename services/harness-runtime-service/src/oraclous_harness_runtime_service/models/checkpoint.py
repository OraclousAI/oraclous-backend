"""HarnessCheckpoint ORM model (ORAA-4 §21 models layer).

The parkable state of a run paused at a mid-loop HITL gate (R5-S6). Org-scoped (ADR-006), loosely
coupled to the execution by ``execution_id`` (no FK, matching ``harness_assignments``). Everything
stored is safe to persist: ``resume_messages`` is the ALREADY-REDACTED transcript and
``manifest_doc``
is the OHM descriptor (capability refs + credential *ids*, never raw secrets). ``status`` drives the
resume CAS PENDING → APPROVED/DENIED so a decision can be applied exactly once.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_harness_runtime_service.models.base_model import BaseModel


class HarnessCheckpoint(BaseModel):
    __tablename__ = "harness_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    # the sourced OHM document — replays the same run
    manifest_doc: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # the redacted transcript at the pause
    resume_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    # not-yet-dispatched calls (gated one first)
    pending_tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    approved_tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # {iteration, tool_calls_made, tokens_used}
    resume_cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # list[str] — rebuild redactors identically
    redact_patterns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
