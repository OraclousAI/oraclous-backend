"""ORM table models (ORAA-4 §21 repositories layer — the only home for `__tablename__`).

Lifted/reshaped from legacy `develop@84152635 knowledge-graph-builder/app/models/graph.py`.
Every row is anchored on `organisation_id` (ADR-006) in addition to the legacy `user_id` owner.
S1 needs `knowledge_graphs`; `ingestion_jobs` is declared now (so the Alembic baseline is whole)
and used from S2.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class KnowledgeGraph(Base):
    __tablename__ = "knowledge_graphs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    node_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    relationship_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Recipe(Base):
    """Stored ingestion recipe (ADR-022). Versioned by (id, version) — a new version is a new row,
    never an UPDATE. Tenant-scoped by organisation_id (legacy used graph_id; R3.5 scopes by org)."""

    __tablename__ = "recipes"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, index=True
    )
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    shape_signature: Mapped[str] = mapped_column(Text, nullable=False)
    concern: Mapped[str] = mapped_column(String(255), nullable=False)
    recipe_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    authored_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    graph_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)  # text|pdf|csv|json|code
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipe_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # structured: optional
    valid_from: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # temporal passthrough
    valid_to: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_entities: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extracted_relationships: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
