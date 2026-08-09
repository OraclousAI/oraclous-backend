"""credential default_for — the org's designated default credential per purpose (#724)

Revision ID: 0005_credential_default_for
Revises: 0004_enable_rls
Create Date: 2026-08-06

A platform-side model call over customer data (KGS entity extraction, the KRS evaluation judge)
has no manifest to name a credential the way a team member does. Before #724 those calls used a
key of the platform's, so the customer neither chose the model that read their data nor saw what
it cost. They now ask the broker for the ORGANISATION's default instead.

``default_for`` names the purpose a credential is the default for (today only ``"model"``); NULL
for an ordinary credential. A PARTIAL unique index enforces one default per
``(organisation_id, default_for)`` while leaving unlimited NULL rows, which is what keeps the
constraint compatible with every existing credential.

Additive and non-destructive: every existing row gets NULL, so nothing is designated until a user
picks one, and the fail-closed refusal is the behaviour until they do.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_credential_default_for"
down_revision = "0004_enable_rls"
branch_labels = None
depends_on = None

_INDEX = "uq_user_credentials_org_default_for"


def upgrade() -> None:
    op.add_column("user_credentials", sa.Column("default_for", sa.String(), nullable=True))
    op.create_index("ix_user_credentials_default_for", "user_credentials", ["default_for"])
    # Partial unique: one default per purpose per org, unlimited non-default (NULL) rows.
    op.create_index(
        _INDEX,
        "user_credentials",
        ["organisation_id", "default_for"],
        unique=True,
        postgresql_where=sa.text("default_for IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="user_credentials")
    op.drop_index("ix_user_credentials_default_for", table_name="user_credentials")
    op.drop_column("user_credentials", "default_for")
