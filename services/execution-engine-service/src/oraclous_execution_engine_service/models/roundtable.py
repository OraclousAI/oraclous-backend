"""EngineRoundtable ORM model (ORAA-4 §21 models layer).

A round-table coordinates N actors (agents + humans) over ONE shared transcript, turn by turn. It
adds no execution primitive — the engine drives each agent turn through the harness (like a job) and
each human turn through a pause/respond, appending every result to ``transcript`` and advancing
``current_turn`` until ``max_rounds`` complete. Org-scoped (ADR-006). The lifecycle reuses the job
state machine (QUEUED → RUNNING → SUCCEEDED | ESCALATED-at-a-human-turn | FAILED | CANCELLED).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineRoundtable(BaseModel):
    __tablename__ = "engine_roundtables"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    topic = Column(Text, nullable=False)  # the seed context for turn 0
    # actors[]: [{role, kind: agent|human, manifest|manifest_ref (agent), prompt (human)}]
    actors = Column(JSONB, nullable=False)
    max_rounds = Column(Integer, nullable=False, default=1)
    current_turn = Column(Integer, nullable=False, default=0)  # 0-based across all turns
    state = Column(String(16), nullable=False, default="QUEUED")
    transcript = Column(JSONB, nullable=False, default=list)  # [{turn, role, kind, output}]
    final_output = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
