"""chat_threads + chat_messages — the R6 gateway chat persistence (ADR-019, Slice 6)

Revision ID: 0003_chat
Revises: 0002_published_agents
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_chat"
down_revision = "0002_published_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_threads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bound_agent_slug", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default="New chat"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_threads_organisation_id", "chat_threads", ["organisation_id"])
    op.create_index(
        "ix_chat_threads_member",
        "chat_threads",
        ["organisation_id", "created_by_user_id", "last_message_at"],
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organisation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("sources", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["thread_id"], ["chat_threads.id"], ondelete="CASCADE"),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_chat_messages_role"),
    )
    op.create_index("ix_chat_messages_organisation_id", "chat_messages", ["organisation_id"])
    op.create_index("ix_chat_messages_thread", "chat_messages", ["thread_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_thread", table_name="chat_messages")
    op.drop_index("ix_chat_messages_organisation_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_threads_member", table_name="chat_threads")
    op.drop_index("ix_chat_threads_organisation_id", table_name="chat_threads")
    op.drop_table("chat_threads")
