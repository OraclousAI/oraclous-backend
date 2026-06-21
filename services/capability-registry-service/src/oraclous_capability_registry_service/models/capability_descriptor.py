"""CapabilityDescriptor ORM (models layer; reshape of legacy
``oraclous-core-service/app/models/capability_descriptor.py``).

The unified registry: a *tool* is a descriptor of ``kind=tool``. Every row is org-scoped
(``organisation_id`` NOT NULL — ADR-006, ORG002); the legacy ``org_id`` is renamed to the
canonical ``organisation_id``. ``descriptor`` holds the OHM manifest JSONB; ``content_hash`` is its
canonical SHA-256; ``name`` is denormalised from ``descriptor.metadata.name`` for search.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_capability_registry_service.models.base_model import BaseModel
from oraclous_capability_registry_service.models.enums import DescriptorKind


class CapabilityDescriptor(BaseModel):
    __tablename__ = "capability_descriptors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    kind: Mapped[DescriptorKind] = mapped_column(
        SAEnum(
            DescriptorKind,
            name="descriptorkind",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    descriptor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # supply-chain approval gate (R6 MCP-import): "active" (executable — the default for every
    # built-in / first-party registration) | "pending_approval" (an imported external MCP tool an
    # admin has not yet approved) | "rejected" (an admin declined the imported tool — terminal). A
    # non-active MCP tool is refused at execution (fail-closed). Free-form String (no DB enum), so
    # adding the "rejected" value needs no migration.
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
