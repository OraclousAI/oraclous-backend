"""HarnessExecution ORM model (ORAA-4 §21 models layer).

One row per harness run: the resolved harness identity, the terminal status, the final output, and a
JSON step trace. Org-scoped (ADR-006). The registry keeps its own per-tool execution rows; this row
is the harness-level record and cross-references them through the provenance trail.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_harness_runtime_service.models.base_model import BaseModel


class HarnessExecution(BaseModel):
    __tablename__ = "harness_executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    harness_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    harness_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input: Mapped[str] = mapped_column(Text, nullable=False)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-execution LLM spend breakdown (#252). ``model`` is the OHM primary model binding (e.g.
    # ``openrouter/openai/gpt-4o-mini``; NULL in fake mode); input/output split the metered tokens
    # so spend can be priced honestly at read time (output costs ~3-4× input). ADR-009 stays
    # intact — these are RAW token counts, never a price.
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
