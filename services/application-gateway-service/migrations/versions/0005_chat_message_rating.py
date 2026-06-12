"""chat_messages.rating — per-message thumbs up/down feedback (#313)

Revision ID: 0005_chat_message_rating
Revises: 0004_webhook_subscriptions
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_chat_message_rating"
down_revision = "0004_webhook_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("rating", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_chat_messages_rating", "chat_messages", "rating IN ('up', 'down')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_chat_messages_rating", "chat_messages", type_="check")
    op.drop_column("chat_messages", "rating")
