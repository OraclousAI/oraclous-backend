"""Chat persistence ORM models (ORAA-4 §21 models layer) — R6 Slice 6 (ADR-019).

Gateway-owned, org-scoped chat. A **thread** binds to a published-agent slug (resolved via the S4
PublishedAgentRepository) and is **private to its creating member** within the org (reads filter
``organisation_id`` AND ``created_by_user_id``). A **message** is one turn (user / assistant); an
assistant turn links to its harness execution. Tenancy is app-layer (no RLS); the org + user are
always sourced from the verified principal, never the request body.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from oraclous_application_gateway_service.models.base_model import BaseModel


class ChatThread(BaseModel):
    __tablename__ = "chat_threads"
    __table_args__ = (
        Index(
            "ix_chat_threads_member",
            "organisation_id",
            "created_by_user_id",
            "last_message_at",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_by_user_id = Column(UUID(as_uuid=True), nullable=False)
    bound_agent_slug = Column(String, nullable=False)  # the published-agent slug talked to
    title = Column(String, nullable=False, default="New chat")
    last_message_at = Column(DateTime(timezone=True), server_default=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)  # soft-delete tombstone


class ChatMessage(BaseModel):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_chat_messages_role"),
        CheckConstraint("rating IN ('up', 'down')", name="ck_chat_messages_rating"),
        Index("ix_chat_messages_thread", "thread_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id = Column(
        UUID(as_uuid=True), ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False
    )
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)  # denormalised
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    # assistant-turn audit (linked to the harness run); created_at is the SOLE ordering key
    execution_id = Column(UUID(as_uuid=True), nullable=True)
    total_tokens = Column(Integer, nullable=True)
    sources = Column(JSONB(none_as_null=True), nullable=True)
    # member feedback on an assistant turn (thumbs up/down); NULL = no rating yet (#313)
    rating = Column(String, nullable=True)
