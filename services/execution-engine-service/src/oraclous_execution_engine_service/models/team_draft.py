"""EngineTeamDraft ORM model (models layer).

A team DRAFT is an editable, org-scoped OHM v1.1 Team Harness that has not (yet) been run: the
compile → review → refine loop's persistence home (#635, the re-scoped C-1(b)). A draft is what
the console loads into the roster review, refines with typed ops, and finally GOes by POSTing its
``manifest`` + ``sub_harnesses`` to ``/v1/engine/team-runs`` — the draft itself never executes.
``version`` increments on every write (PUT full-replace and each applied refine) so a client can
detect a concurrent edit. Org-scoped (ADR-006); the org-GUC backstop (ADR-030) is enabled on this
table in migration 0022.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from typing import Any

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineTeamDraft(BaseModel):
    __tablename__ = "engine_team_drafts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # the user-facing draft name (the manifest carries its own metadata.name; this one is the
    # list/label handle and may differ)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # the OHM v1.1 Team Harness manifest (metadata.kind == "team") being drafted
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # role -> the generated single-agent sub-harness OHM for that member (what GO passes inline)
    sub_harnesses: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # bumped on every write (replace / applied refine) — the client's concurrent-edit signal
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
