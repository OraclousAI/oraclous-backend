"""DeliveryState ORM (models layer) — the persisted half of the deliver-back clean-delta (#515, O7).

One row per ``(organisation_id, repo, ref, path)`` holding the last-written ``content_hash``, so a
recurring deliver computes the minimal diff (the changed files) instead of clobbering the tree. Each
row also carries the whole-delivery ``delivery_key`` so an identical re-deliver is recognised and
dedupes to a NO_OP (the repository checks for an existing ``(organisation_id, delivery_key)`` before
recording — a per-row UNIQUE on the key would wrongly reject the 2nd..Nth file of the SAME delivery,
which all share that key, so the dedup is a scoped check, not a per-row constraint).

Org-scoped (``organisation_id`` NOT NULL — ADR-006/ORG002), stamped from the caller's principal,
never a body field (ORG001); under the registry RLS backstop. No ``from __future__ import
annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at mapper configuration time.
"""

import uuid

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_capability_registry_service.models.base_model import BaseModel


class DeliveryState(BaseModel):
    __tablename__ = "delivery_state"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_key: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        # one row per delivered file → a re-record updates only the changed file (the minimal diff)
        UniqueConstraint(
            "organisation_id", "repo", "ref", "path", name="uq_delivery_state_org_repo_ref_path"
        ),
        Index("ix_delivery_state_org_repo_ref", "organisation_id", "repo", "ref"),
        # the dedup-check lookup: has this org already delivered this exact content set?
        Index("ix_delivery_state_org_delivery_key", "organisation_id", "delivery_key"),
    )
