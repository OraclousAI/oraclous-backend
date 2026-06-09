"""Chat surface shapes (ORAA-4 §21 schema layer) — the member console chat plane."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from oraclous_application_gateway_service.schema.published_agent_schemas import SLUG_PATTERN


class StartThreadRequest(BaseModel):
    agent_slug: str = Field(pattern=SLUG_PATTERN)  # the published agent this thread talks to
    title: str | None = None


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1)


class ThreadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bound_agent_slug: str
    title: str
    last_message_at: datetime | None = None
    created_at: datetime | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    execution_id: uuid.UUID | None = None
    total_tokens: int | None = None
    created_at: datetime | None = None


class ChatTurnOut(BaseModel):
    """A turn's outcome: the assistant message on success; on a HITL escalation, a `pending` status
    + the execution id (no completed answer is stored); a coarse `failed` otherwise."""

    status: str  # succeeded | pending | failed
    message: MessageOut | None = None
    execution_id: uuid.UUID | None = None
