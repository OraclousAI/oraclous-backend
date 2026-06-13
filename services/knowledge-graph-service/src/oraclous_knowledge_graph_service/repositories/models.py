"""ORM table models (ORAA-4 §21 repositories layer — the only home for `__tablename__`).

Lifted/reshaped from legacy `develop@84152635 knowledge-graph-builder/app/models/graph.py`.
Every row is anchored on `organisation_id` (ADR-006) in addition to the legacy `user_id` owner.
S1 needs `knowledge_graphs`; `ingestion_jobs` is declared now (so the Alembic baseline is whole)
and used from S2.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint, func, text
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
    # Reserved-graph marker (#332 / ADR-027 §5). NULL for a user-created graph; a reserved value
    # (e.g. `agent_memory`) marks a system-owned graph that a user can never create or address by
    # name. The org-scoped partial unique index below makes "at most ONE per (org, system_kind)" a
    # DB invariant, so the lazy find-or-create is race-safe (a concurrent first run that loses the
    # insert race re-reads the winner rather than creating a duplicate).
    system_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    node_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    relationship_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # At most one system graph of each kind per org (the agent-memory default-graph race guard).
        Index(
            "uq_knowledge_graph_system_kind",
            "organisation_id",
            "system_kind",
            unique=True,
            postgresql_where=text("system_kind IS NOT NULL"),
        ),
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


class EntityResolution(Base):
    """Audit row for a HITL resolution verdict on a `SAME_AS_CANDIDATE` pair (#279).

    Governance-relevant: WHO approved/rejected WHICH pair, WHEN. Org-scoped (ADR-006). The
    `(organisation_id, graph_id, candidate_id)` unique key makes the decision idempotent — a
    re-submit of the SAME verdict is a no-op replay; a DIFFERENT verdict by a second reviewer is a
    conflict the service rejects (409) rather than silently overriding.
    """

    __tablename__ = "entity_resolutions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    # For an in-graph verdict, the single graph. For a CROSS-GRAPH verdict (#330), the pair is
    # canonicalised: `graph_id` is the lexicographically-smaller of the two graph ids and
    # `other_graph_id` the larger, so a verdict from EITHER direction keys the SAME row.
    graph_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # The pair's SECOND graph on a cross-graph verdict; NULL for an in-graph verdict (ADR-026).
    other_graph_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # The stable, unordered candidate-pair id (sha256 of the sorted endpoint node-id pair).
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_id_a: Mapped[str] = mapped_column(String(128), nullable=False)
    node_id_b: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # approve | reject
    # On an in-graph approve: the surviving canonical node id (a fold happened). On a cross-graph
    # approve: NULL — a cross-graph approve LINKS (both nodes survive in their own graphs), so there
    # is no single canonical survivor. Null on reject.
    canonical_node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "graph_id",
            "candidate_id",
            name="uq_entity_resolution_candidate",
        ),
    )


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
