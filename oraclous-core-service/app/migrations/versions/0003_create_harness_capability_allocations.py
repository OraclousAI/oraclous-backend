"""create harness_capability_allocations table (ORAA-77 / T2-M3)

Per-harness capability allocation table. Replaces the workflow-bound
tool_instance concept with per-harness allocations enforced at T2-M3.

Revision ID: 0003_harness_capability_allocations
Revises: 0002_capability_descriptor
Create Date: 2026-06-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_harness_capability_allocations"
down_revision: str | Sequence[str] | None = "0002_capability_descriptor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "CREATE TABLE harness_capability_allocations ("
            "  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            "  org_id       UUID NOT NULL,"
            "  harness_id   UUID NOT NULL,"
            "  capability_id UUID NOT NULL,"
            "  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ")"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_harness_capability_allocations_org_id "
            "ON harness_capability_allocations (org_id)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_harness_capability_allocations_harness_id "
            "ON harness_capability_allocations (harness_id)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_harness_capability_allocations_harness_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_harness_capability_allocations_org_id"))
    op.execute(sa.text("DROP TABLE IF EXISTS harness_capability_allocations"))
