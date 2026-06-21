"""EngineRoundtable ORM model (models layer).

A round-table coordinates N actors (agents + humans) over ONE shared transcript, turn by turn. It
adds no execution primitive — the engine drives each agent turn through the harness (like a job) and
each human turn through a pause/respond, appending every result to ``transcript`` and advancing
``current_turn`` until ``max_rounds`` complete. Org-scoped (ADR-006). The lifecycle reuses the job
state machine (QUEUED → RUNNING → SUCCEEDED | ESCALATED-at-a-human-turn | FAILED | CANCELLED).

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineRoundtable(BaseModel):
    __tablename__ = "engine_roundtables"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)  # the seed context for turn 0
    # actors[]: [{role, kind: agent|human, manifest|manifest_ref (agent), prompt (human)}]
    actors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    max_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 0-based across all turns
    current_turn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    # [{turn, role, kind, output}]
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    final_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
