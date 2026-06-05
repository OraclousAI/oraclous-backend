"""initial capability-registry schema: capability_descriptors

Revision ID: 0001_initial
Revises:
Create Date: R3.5-P5-S1

First migration for the production (docker) deployment. ``capability_descriptors`` is org-scoped
(ADR-006, ORG002); ``descriptor`` holds the OHM manifest JSONB and gets a GIN index so org-scoped
JSONB containment (``@>``) queries — capability matching and descriptor search — are indexed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_NOW = sa.text("now()")

_KIND = pg.ENUM(
    "tool",
    "skill",
    "agent",
    "harness",
    "human_role",
    name="descriptorkind",
    create_type=False,
)


def upgrade() -> None:
    _KIND.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "capability_descriptors",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", _KIND, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("descriptor", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
    )
    op.create_index(
        "ix_capability_descriptors_organisation_id",
        "capability_descriptors",
        ["organisation_id"],
    )
    op.create_index("ix_capability_descriptors_kind", "capability_descriptors", ["kind"])
    op.create_index("ix_capability_descriptors_name", "capability_descriptors", ["name"])
    op.create_index(
        "ix_capability_descriptors_descriptor_gin",
        "capability_descriptors",
        ["descriptor"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_capability_descriptors_descriptor_gin", "capability_descriptors")
    op.drop_index("ix_capability_descriptors_name", "capability_descriptors")
    op.drop_index("ix_capability_descriptors_kind", "capability_descriptors")
    op.drop_index("ix_capability_descriptors_organisation_id", "capability_descriptors")
    op.drop_table("capability_descriptors")
    _KIND.drop(op.get_bind(), checkfirst=True)
