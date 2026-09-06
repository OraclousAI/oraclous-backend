"""engine_apps.form — the labels a person edited before saving

Revision ID: 0028_engine_app_form
Revises: 0027_team_run_app_id
Create Date: #938 — a model-drafted, person-edited form for an app converted from a team run

Additive and nullable. NULL means an app that predates this column — every app seeded before #938,
including the shipped Validation Desk — and keeps running exactly as it did under the #932 derived
projection (``domain/apps.form_fields``), computed at read time from what the team already
declares. A non-null ``form`` is the authority instead: the ordered list of fields a person actually
saw and edited, which is what the fold (``domain/app_form.fold``) joins into the team's one
declared input.

No RLS change. This is one column on an already-covered table (migration 0026); the widened READ /
strict WRITE split is unaffected.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0028_engine_app_form"
down_revision = "0027_team_run_app_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("engine_apps", sa.Column("form", pg.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("engine_apps", "form")
