"""Published-agent ORM model (ORAA-4 §21 models layer) — R6 Slice 4 (ADR-019).

An org publishes one of its agents under a public ``slug`` that external callers reach through an
integration key. The row binds the slug to ``bound_capability_ref`` — the capability/harness
descriptor id (the OHM) the harness runs on invoke. Org-scoped: ``slug`` is unique *within* an org,
so two orgs can publish the same slug without collision, and resolution is always ``(org, slug)``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from oraclous_application_gateway_service.models.base_model import BaseModel


class PublishedAgent(BaseModel):
    __tablename__ = "published_agents"
    __table_args__ = (
        UniqueConstraint("organisation_id", "slug", name="uq_published_agents_org_slug"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    slug = Column(String, nullable=False, index=True)
    # the capability/harness descriptor id the harness runs on invoke (manifest_ref)
    bound_capability_ref = Column(String, nullable=False)
    display_name = Column(String, nullable=True)
    description = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")  # 'active' | 'unpublished'
