"""execution token breakdown (per-model input/output) for priced LLM-spend metering

Revision ID: 0005_execution_token_breakdown
Revises: 0004_loop_checkpoints
Create Date: #252

Adds the per-execution spend breakdown to ``harness_executions``: ``model`` (the OHM primary model
binding, e.g. ``openrouter/openai/gpt-4o-mini``; NULL in fake mode) plus the input/output split of
the metered tokens. The substrate still records RAW token counts only (ADR-009) — pricing is a
read-time layer (``GET /v1/harnesses/spend``), never persisted here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_execution_token_breakdown"
down_revision = "0004_loop_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harness_executions",
        sa.Column("model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "harness_executions",
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "harness_executions",
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("harness_executions", "output_tokens")
    op.drop_column("harness_executions", "input_tokens")
    op.drop_column("harness_executions", "model")
