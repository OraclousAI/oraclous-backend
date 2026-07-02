"""adopted-tool-run atomic dispatch claim — exactly-once under concurrent copies (#501-#1 hardening)

The #489 adopted path dedupes the ENQUEUE (the ``(org, idempotency_key)`` row), and the #501
worker guard short-circuits a redelivery whose ``execution_id`` is already stamped. But that guard
is a non-atomic read-then-execute: the #501 lost-window reaper can enqueue a SECOND, independent
broker message for the same ``run_id`` while the original is still queued, so under queue backlog
two copies can run CONCURRENTLY on the ``--concurrency=2`` worker, both read ``execution_id IS
NULL``, and call the non-idempotent registry ``/execute`` — a double draft. This adds an atomic
CLAIM so exactly
one concurrent copy proceeds to execute:

* ``engine_adopted_tool_runs.dispatched_at`` (timestamptz, nullable) — stamped by a conditional
  ``UPDATE … WHERE execution_id IS NULL AND (dispatched_at IS NULL OR dispatched_at < now-lease)
  RETURNING id``. The one copy whose UPDATE returns a row won the claim and executes; the others
  short-circuit. A claim that never stamps ``execution_id`` (a crash mid-execute) becomes
  re-claimable once it ages past the lease, so the lost-window reaper still recovers it. No backfill
  (NULL = unclaimed, exactly today's behaviour).

Also adds a PARTIAL index on the unstamped rows the reaper scans (``WHERE execution_id IS NULL``),
so its cross-org lease sweep is an index scan, not a seq scan, as the ledger grows.

The table has been RLS-enabled + granted to ``oraclous_app`` since its create (0009); a new column
inherits the table GRANT, so NO new grant / rls_coverage change is needed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_adopted_run_dispatch_claim"
down_revision = "0019_schedule_last_settled_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "engine_adopted_tool_runs",
        sa.Column("dispatched_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    # the reaper enumerates unstamped rows across orgs — a partial index keeps that an index scan.
    op.create_index(
        "ix_engine_adopted_tool_runs_unstamped",
        "engine_adopted_tool_runs",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("execution_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_engine_adopted_tool_runs_unstamped", table_name="engine_adopted_tool_runs")
    op.drop_column("engine_adopted_tool_runs", "dispatched_at")
