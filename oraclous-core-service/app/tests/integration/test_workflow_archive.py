"""
[tests] workflow archive table — integration — ORAA-78

Story: ORAA-78 / ORA-77
Architecture refs:
  - ADR-005 (retire workflow_service / pipeline_generator):
      https://oraclous.atlassian.net/wiki/spaces/OP/pages/753772
  - Test Strategy: https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

The implementer must create an Alembic migration that:
  1. Creates an archived_workflows table mirroring the workflows table plus
     an archived_at timestamp column
  2. Copies all existing rows from workflows into archived_workflows
  3. Drops the original workflows table (and all dependent FK constraints)

These tests are intentionally red until that migration is created and applied.

Behaviours covered:
  A01  archived_workflows table exists in the database after migration
  A02  archived_workflows has all required columns including archived_at
  A03  archived_workflows allows direct insertion (write-path smoke test)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Required columns — mirrors core WorkflowDB fields plus the archive timestamp
# ---------------------------------------------------------------------------

_REQUIRED_COLUMNS = {
    "id",
    "name",
    "description",
    "owner_id",
    "nodes",
    "edges",
    "status",
    "archived_at",  # added by the archive migration; not present in the original table
}


# ---------------------------------------------------------------------------
# A01  archived_workflows table exists
#
# Currently FAILS: no archive migration has been created yet.
# Passes after the implementer creates and applies the Alembic migration.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_archived_workflows_table_exists(async_session):
    """
    archived_workflows table must exist in the database after the archive migration
    is applied.  The test queries information_schema so it does not depend on any
    SQLAlchemy model for the archive table (the model is intentionally absent —
    the archive is a write-once historical record, not an active domain model).
    """
    result = await async_session.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'archived_workflows'"
        )
    )
    count = result.scalar_one()
    assert count == 1, (
        "archived_workflows table does not exist — the implementer must create "
        "the archive Alembic migration before deleting the workflow source files"
    )


# ---------------------------------------------------------------------------
# A02  archived_workflows has required columns including archived_at
#
# Currently FAILS: table does not exist.
# Passes after migration creates the table with the expected schema.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_archived_workflows_has_required_columns(async_session):
    """
    archived_workflows must carry all core workflow columns plus archived_at.
    archived_at is the sentinel that proves this is an archival copy and not
    a simple table rename — it must be populated by the migration itself
    (not left NULL) to satisfy the data-preservation requirement.
    """
    result = await async_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'archived_workflows'"
        )
    )
    present = {row[0] for row in result.fetchall()}
    missing = _REQUIRED_COLUMNS - present
    assert not missing, (
        f"archived_workflows is missing required columns: {sorted(missing)}. "
        f"Columns present: {sorted(present)}"
    )


# ---------------------------------------------------------------------------
# A03  archived_workflows allows direct insertion (write-path smoke)
#
# Currently FAILS: table does not exist.
# Passes after migration creates the table with compatible column types.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_archived_workflows_allows_insertion(async_session):
    """
    Confirm the archive table accepts a minimally valid row.
    This smoke test verifies that the column types and NOT NULL constraints
    are compatible with real workflow data so the migration's COPY step will
    not fail on type or constraint violations.
    """
    row_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    archived_at = datetime.now(timezone.utc)

    await async_session.execute(
        text(
            "INSERT INTO archived_workflows "
            "(id, name, owner_id, nodes, edges, status, archived_at) "
            "VALUES (:id, :name, :owner_id, :nodes::jsonb, :edges::jsonb, :status, :archived_at)"
        ),
        {
            "id": str(row_id),
            "name": "smoke-test-archived-workflow",
            "owner_id": str(owner_id),
            "nodes": "[]",
            "edges": "[]",
            "status": "ARCHIVED",
            "archived_at": archived_at,
        },
    )

    result = await async_session.execute(
        text("SELECT name, archived_at FROM archived_workflows WHERE id = :id"),
        {"id": str(row_id)},
    )
    row = result.fetchone()
    assert row is not None, (
        f"Inserted row with id={row_id} not found in archived_workflows"
    )
    assert row[0] == "smoke-test-archived-workflow"
    assert row[1] is not None, "archived_at must not be NULL"
