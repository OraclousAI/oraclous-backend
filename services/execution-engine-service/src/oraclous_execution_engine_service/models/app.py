"""EngineApp ORM model (models layer).

An APP is a team behind a short form: someone fills in the request, presses Run, and gets a result
without ever opening the plan behind it (#932). Two kinds live in this one table — the ones Oraclous
provides (owned by the platform organisation, readable by everyone) and the ones an organisation
made for itself — and which kind a row is comes from its ``organisation_id``, never from a column.

It holds its OWN COPY of the team's documents rather than pointing at a team draft. Three reasons,
each a fact about the shipped code:

* a draft's old versions are not retained (``replace``/``refine`` overwrite in place), so there is
  no version to pin;
* since #695/ADR-050 D3 a saved draft keeps only its members' ``manifest_ref``s to registry agents
  that are editable in place, so an app pointing at one would silently change under its users;
* a platform-owned app cannot point at a draft belonging to some other organisation.

The copy also makes a run self-contained: ``_resolve_member_manifests`` short-circuits on inline
sub-harnesses, so running a platform app in a freshly created organisation needs no cross-org
registry read.

Org-scoped (ADR-006), and the ONE engine table whose RLS read is WIDENED to a second, fixed
organisation (migration 0026): reads admit the caller's org OR the platform org, writes stay
strictly the caller's. That asymmetry is the whole mechanism by which an Oraclous-provided app
reaches every organisation without a copy seeded per tenant. Only the app row is shared — every
run, input, result and artifact it produces is stamped with the CALLER's organisation and read
strictly.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from typing import Any

from sqlalchemy import Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineApp(BaseModel):
    __tablename__ = "engine_apps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # the owner. PLATFORM_ORG_ID for an Oraclous-provided app; the caller's org for one made here.
    # `origin` is DERIVED from this at read time and is deliberately not a column — a create body
    # can then never assert "platform", and the value can never drift from what RLS enforces.
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # the tile's title (the manifest carries its own metadata.name; this is the user-facing handle)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # the one-line description under the title on the tile
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # a stable human handle for deep links, so the console need not carry a hardcoded uuid the way
    # VITE_DESK_TEAM_DRAFT_ID did. NULL for an app that never needed one.
    slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # the FROZEN team documents — credential-scrubbed at freeze (domain/app_freeze.py), because
    # every organisation can read a platform row.
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # role -> that member's single-agent sub-harness, held INLINE so a run resolves nothing.
    # server_default mirrors migration 0026 — a column default that lives only in Python is absent
    # from any write that does not go through the ORM, and the raw-SQL policy tests are exactly
    # such a write.
    sub_harnesses: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # sha256 over the canonical documents. The startup seed compares it so an unchanged app is not
    # rewritten on every boot (and concurrent replicas booting together are a no-op).
    manifest_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # bumped only when the documents actually change. NOT a draft version: an app is pinned, so
    # this moves on a deliberate republish or a seed whose content really moved.
    pinned_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # Where this app came from, for the deferred convert-a-team-into-an-app path (#932 defers it;
    # the columns ship nullable now so that lands as a service change rather than a migration).
    source_team_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_team_draft_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # the draft's `version` when the copy was taken — the only thing that can answer "has the team
    # moved on since?", and only ever as a boolean signal, since the old version itself is gone.
    source_draft_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # whose model key a run spends. Only "caller" is accepted today; "owner" needs credential
    # delegation, a billing answer and an operator-separation review (ADR-008 / CLAUDE.md §3.6).
    credentials_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="caller", server_default="caller"
    )

    __table_args__ = (
        Index(
            "uq_engine_apps_org_slug",
            "organisation_id",
            "slug",
            unique=True,
            postgresql_where=text("slug IS NOT NULL"),
        ),
    )
