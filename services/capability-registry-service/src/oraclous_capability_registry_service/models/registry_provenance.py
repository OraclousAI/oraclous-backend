"""RegistryProvenance ORM model (models layer; CLAUDE.md §3.7, 11 September ruling).

The durable sink behind the substrate ``ProvenanceCollector`` for this service's own dispatch and
refusal events (``capability.invoke`` / ``capability.refused``). Strictly org-isolated, like
``executions`` — not a widened-read table. This is NOT the registry's own operational execution
state (``executions`` / ``models/execution.py``); it is the §3.7 audit record for every capability
invocation and every pre-dispatch refusal (``services/tool_execution_service.py``).

Mirrors the engine's ``EngineProvenanceEvent`` (five required T7-M1 fields) plus the three
nullable extension fields the 24 August ruling added to ``ProvenanceRecord``: ``context``
(structured per-call detail), ``input_hash`` / ``output_hash`` (``sha256:<hex>`` content
fingerprints — the raw payload is never stored, CLAUDE.md §11).

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_capability_registry_service.models.base_model import BaseModel


class RegistryProvenance(BaseModel):
    __tablename__ = "registry_provenance"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    principal: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
