"""HarnessGraphBinding ORM (ORAA-4 §21 models layer; ADR-029 §1).

The workspace↔harness binding — a many-to-many *curation* edge owned by the capability registry
(NOT an OHM manifest field, NOT a graph-substrate association). One row binds a ``kind:harness``
capability to a knowledge graph (workspace); it is a visibility/discovery association only and
grants NO data access and changes NO execution route (ADR-029 §2, hard invariant).

Org-scoped (``organisation_id`` NOT NULL — ADR-006/ORG002), stamped from the caller's principal,
never a body field (ORG001). ``harness_capability_id`` FKs the registry's own
``capability_descriptors`` ``ON DELETE CASCADE`` (a deleted harness removes its bindings in-service,
ADR-029 §4). ``graph_id`` is a plain UUID with NO cross-service FK — graphs live in
knowledge-graph-service (separate Alembic lineage; ORAA-4 §3.1 forbids a cross-service FK); a graph
delete cannot cascade here, so dangling rows are tolerated and lazily skipped on read (ADR-029 §4).
``UNIQUE(harness_capability_id, graph_id)`` makes attach idempotent (the repository maps the
IntegrityError to an already-bound success). ``created_by`` is added explicitly (the base mixin only
gives ``created_at``/``updated_at``).
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from oraclous_capability_registry_service.models.base_model import BaseModel


class HarnessGraphBinding(BaseModel):
    __tablename__ = "harness_graph_binding"

    id = Column(UUID(as_uuid=True), primary_key=True)
    harness_capability_id = Column(
        UUID(as_uuid=True),
        ForeignKey("capability_descriptors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # A plain UUID — graphs are owned by knowledge-graph-service (no cross-service FK; ADR-029 §4).
    graph_id = Column(UUID(as_uuid=True), nullable=False)
    organisation_id = Column(UUID(as_uuid=True), nullable=False)
    created_by = Column(UUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("harness_capability_id", "graph_id", name="uq_harness_graph_binding_pair"),
        Index("ix_harness_graph_binding_graph_org", "graph_id", "organisation_id"),
    )
