"""create capability_descriptor table with descriptorkind enum (ORAA-69 / ORA-68)

Migrates existing tool_definitions rows into capability_descriptor as kind=tool.
Reversible: downgrade drops capability_descriptor and the descriptorkind enum;
tool_definitions is untouched so downgrade leaves the schema at the 0001 state.

Revision ID: 0002_create_capability_descriptor
Revises: 0001_create_tool_definitions
Create Date: 2026-06-01

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_capability_descriptor"
down_revision: str | Sequence[str] | None = "0001_create_tool_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Use raw SQL for all DDL to avoid SQLAlchemy Enum / asyncpg interaction issues
    # where op.create_table() emits CREATE TYPE even with create_type=False.

    # Create enum type idempotently — IF NOT EXISTS avoids duplicate_object on retries.
    op.execute(sa.text(
        "DO $$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'descriptorkind') THEN "
        "    CREATE TYPE descriptorkind AS ENUM "
        "      ('tool', 'skill', 'agent', 'harness', 'human_role'); "
        "  END IF; "
        "END; $$"
    ))

    op.execute(sa.text(
        "CREATE TABLE capability_descriptor ("
        "  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
        "  org_id      UUID NOT NULL,"
        "  kind        descriptorkind NOT NULL,"
        "  content_hash VARCHAR(255),"
        "  descriptor  JSONB NOT NULL,"
        "  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        "  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        ")"
    ))

    op.execute(sa.text(
        "CREATE INDEX ix_capability_descriptor_org_id "
        "ON capability_descriptor (org_id)"
    ))
    op.execute(sa.text(
        "CREATE INDEX ix_capability_descriptor_kind "
        "ON capability_descriptor (kind)"
    ))

    # Copy existing tool_definitions rows into capability_descriptor as kind=tool.
    # org_id is set to a sentinel UUID since tool_definitions predates multi-tenancy.
    op.execute(sa.text(
        "INSERT INTO capability_descriptor"
        "  (id, org_id, kind, descriptor, created_at, updated_at) "
        "SELECT"
        "  id,"
        "  '00000000-0000-0000-0000-000000000000'::uuid,"
        "  'tool'::descriptorkind,"
        "  jsonb_build_object("
        "    'kind', 'tool',"
        "    'id', name,"
        "    'metadata', jsonb_build_object("
        "      'name', name,"
        "      'description', COALESCE(description, '')"
        "    ),"
        "    'spec', jsonb_build_object("
        "      'input_schema', input_schema,"
        "      'output_schema', output_schema"
        "    )"
        "  ),"
        "  created_at,"
        "  updated_at "
        "FROM tool_definitions"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP INDEX IF EXISTS ix_capability_descriptor_kind"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS ix_capability_descriptor_org_id"
    ))
    op.execute(sa.text("DROP TABLE IF EXISTS capability_descriptor"))
    op.execute(sa.text("DROP TYPE IF EXISTS descriptorkind"))
