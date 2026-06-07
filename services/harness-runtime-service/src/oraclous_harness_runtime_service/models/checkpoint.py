"""HarnessCheckpoint ORM model (ORAA-4 §21 models layer).

The parkable state of a run paused at a mid-loop HITL gate (R5-S6). Org-scoped (ADR-006), loosely
coupled to the execution by ``execution_id`` (no FK, matching ``harness_assignments``). Everything
stored is safe to persist: ``resume_messages`` is the ALREADY-REDACTED transcript and
``manifest_doc``
is the OHM descriptor (capability refs + credential *ids*, never raw secrets). ``status`` drives the
resume CAS PENDING → APPROVED/DENIED so a decision can be applied exactly once.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from oraclous_harness_runtime_service.models.base_model import BaseModel


class HarnessCheckpoint(BaseModel):
    __tablename__ = "harness_checkpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    execution_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="PENDING")
    manifest_doc = Column(JSONB, nullable=False)  # the sourced OHM document — replays the same run
    resume_messages = Column(JSONB, nullable=False)  # the redacted transcript at the pause
    pending_tool_calls = Column(JSONB, nullable=False)  # not-yet-dispatched calls (gated one first)
    approved_tool_call_id = Column(String(128), nullable=False)
    resume_cursor = Column(JSONB, nullable=False)  # {iteration, tool_calls_made, tokens_used}
    redact_patterns = Column(JSONB, nullable=False)  # list[str] — rebuild redactors identically
