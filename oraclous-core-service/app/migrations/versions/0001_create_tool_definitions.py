"""create tool_definitions table (initial legacy schema)

Revision ID: 0001_create_tool_definitions
Revises:
Create Date: 2026-06-01

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "0001_create_tool_definitions"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_definitions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.String(50), server_default="1.0.0"),
        sa.Column("icon", sa.String(255), nullable=True),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("capabilities", JSONB(), nullable=True),
        sa.Column("tags", ARRAY(sa.String()), nullable=True),
        sa.Column("input_schema", JSONB(), nullable=False),
        sa.Column("output_schema", JSONB(), nullable=False),
        sa.Column("configuration_schema", JSONB(), nullable=True),
        sa.Column("credential_requirements", JSONB(), nullable=True),
        sa.Column("dependencies", ARRAY(sa.String()), nullable=True),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column("documentation_url", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.create_index("ix_tool_definitions_name", "tool_definitions", ["name"])
    op.create_index("ix_tool_definitions_category", "tool_definitions", ["category"])
    op.create_index("ix_tool_definitions_type", "tool_definitions", ["type"])


def downgrade() -> None:
    op.drop_index("ix_tool_definitions_type", table_name="tool_definitions")
    op.drop_index("ix_tool_definitions_category", table_name="tool_definitions")
    op.drop_index("ix_tool_definitions_name", table_name="tool_definitions")
    op.drop_table("tool_definitions")
