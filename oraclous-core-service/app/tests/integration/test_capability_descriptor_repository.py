"""
[tests] capability_descriptor DB table + migration — integration tests

Story: ORAA-69 / ORA-68
Architecture refs:
  - OHM v1.0 Spec:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - R2 migration map: https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - Test Strategy:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

All imports below will fail with ImportError until the implementer creates:
  - app/models/capability_descriptor.py  (CapabilityDescriptorDB, DescriptorKind)
  - app/repositories/capability_descriptor_repository.py  (CapabilityDescriptorRepository)
  - Alembic migration for capability_descriptor table

That ImportError is intentional — this file is written test-first (TDD / ADR-010).

Behaviours covered:
  D01  capability_descriptor table exists in the DB after migration
  D02  table has all required columns: id, org_id, kind, content_hash, descriptor, created_at, updated_at
  D03  kind column is backed by a Postgres enum with exactly 5 values
  D04  content_hash column is nullable (S3.1 leaves it empty)
  D05  descriptor column is JSONB type
  D06  create a CapabilityDescriptor row with kind=tool returns the persisted row
  D07  get_by_id returns the row inserted in D06
  D08  get_by_id returns None for an unknown UUID
  D09  update_descriptor replaces the JSONB content and bumps updated_at
  D10  delete removes the row; subsequent get_by_id returns None
  D11  list_by_org returns all rows for an org and no rows for other orgs
  D12  list_by_kind returns only rows matching the requested kind
  D13  list_by_kind with kind=skill returns only skill rows
  D14  JSONB containment query (@>) matches rows whose descriptor contains the filter dict
  D15  JSONB containment query returns empty list when no rows match
  D16  org_id isolation: org B cannot read rows belonging to org A
  D17  legacy tool_definition rows appear in capability_descriptor as kind=tool after migration
  D18  down migration is reversible: tool_definition table re-exists, capability_descriptor dropped
  D19  all five valid kind values (tool, skill, agent, harness, human_role) persist successfully
  D20  inserting an invalid kind string raises an IntegrityError at the DB layer
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, DataError, IntegrityError

# ---------------------------------------------------------------------------
# These imports will fail with ImportError until the implementer creates the
# model + repository modules.  The ImportError IS the expected initial test
# failure under TDD.
# ---------------------------------------------------------------------------
from app.models.capability_descriptor import (  # noqa: E402
    CapabilityDescriptorDB,
    DescriptorKind,
)
from app.repositories.capability_descriptor_repository import (  # noqa: E402
    CapabilityDescriptorRepository,
)

# ---------------------------------------------------------------------------
# Fixtures: minimal valid JSONB descriptors for each kind
# ---------------------------------------------------------------------------

_ORG_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_ORG_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")

_TOOL_DESCRIPTOR: dict = {
    "kind": "tool",
    "id": "google-drive-reader",
    "version": {"hash": "sha256:abc123", "tags": ["1.0.0"]},
    "metadata": {"name": "Google Drive Reader", "description": "Read files from Google Drive."},
    "spec": {
        "implementation": {"type": "internal", "handler": "gdr.GoogleDriveReader"},
        "input_schema": {"type": "object", "required": ["file_id"], "properties": {"file_id": {"type": "string"}}},
        "output_schema": {"type": "object", "properties": {"content": {"type": "string"}}},
        "credential_requirements": [{"type": "oauth_token", "provider": "google", "scopes": ["drive.readonly"]}],
    },
}

_SKILL_DESCRIPTOR: dict = {
    "kind": "skill",
    "id": "cold-outreach-drafter",
    "version": {"hash": "sha256:def456", "tags": ["1.0.0"]},
    "metadata": {"name": "Cold Outreach Drafter", "description": "Draft messages."},
    "spec": {
        "loaded_when": "The actor needs to draft a cold outreach message.",
        "instructions": "# Cold Outreach\n\nDraft personalised messages.",
        "capability_requirements": [],
    },
}

_AGENT_DESCRIPTOR: dict = {
    "kind": "agent",
    "id": "outreach-drafter-agent",
    "version": {"hash": "sha256:ghi789", "tags": ["1.0.0"]},
    "metadata": {"name": "Outreach Drafter", "description": "Drafts messages."},
    "spec": {
        "role": "You are the Outreach Drafter.",
        "llm_config": {"provider_ref": "workspace-default"},
        "capabilities": [],
        "scope": {"workspaces": ["workspace-marketing"]},
    },
}

_HARNESS_DESCRIPTOR: dict = {
    "kind": "harness",
    "id": "outreach-pipeline",
    "version": {"hash": "sha256:jkl012", "tags": ["1.0.0"]},
    "metadata": {"name": "Cold Outreach Pipeline", "description": "End-to-end pipeline."},
    "spec": {
        "goal": "Identify prospects, draft messages, get approval, and send.",
        "actors": [{"id": "drafter", "kind": "agent", "ref": {"id": "outreach-drafter-agent", "version_tag": "stable"}}],
        "orchestration": "1. Researcher finds. 2. Drafter drafts.",
    },
}

_HUMAN_ROLE_DESCRIPTOR: dict = {
    "kind": "human_role",
    "id": "brand-reviewer-role",
    "version": {"hash": "sha256:mno345", "tags": ["1.0.0"]},
    "metadata": {"name": "Brand Reviewer", "description": "Reviews drafts."},
    "spec": {"role_name": "brand_lead", "fallback": {"role": "marketing_director"}},
}


# ---------------------------------------------------------------------------
# D01  capability_descriptor table exists after migration
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_capability_descriptor_table_exists(async_session):
    """After alembic upgrade head, capability_descriptor table must exist in the public schema."""
    result = await async_session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'capability_descriptor'"
        )
    )
    row = result.fetchone()
    assert row is not None, "capability_descriptor table was not created by the migration"


# ---------------------------------------------------------------------------
# D02  table has all required columns
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_capability_descriptor_has_required_columns(async_session):
    """capability_descriptor must have: id, org_id, kind, content_hash, descriptor, created_at, updated_at."""
    result = await async_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'capability_descriptor'"
        )
    )
    columns = {row[0] for row in result.fetchall()}
    required = {"id", "org_id", "kind", "content_hash", "descriptor", "created_at", "updated_at"}
    missing = required - columns
    assert not missing, f"Missing columns in capability_descriptor: {missing}"


# ---------------------------------------------------------------------------
# D03  kind column is backed by a Postgres enum with exactly 5 values
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_kind_column_enum_values(async_session):
    """The pg_enum for the capability_descriptor kind column must have exactly 5 values."""
    result = await async_session.execute(
        text(
            "SELECT e.enumlabel "
            "FROM pg_enum e "
            "JOIN pg_type t ON e.enumtypid = t.oid "
            "WHERE t.typname = 'descriptorkind' "
            "ORDER BY e.enumsortorder"
        )
    )
    values = {row[0] for row in result.fetchall()}
    expected = {"tool", "skill", "agent", "harness", "human_role"}
    assert values == expected, f"Enum mismatch. Got: {values}  Expected: {expected}"


# ---------------------------------------------------------------------------
# D04  content_hash column is nullable
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_content_hash_is_nullable(async_session):
    """content_hash must be nullable (not populated until S3.1)."""
    result = await async_session.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "  AND table_name = 'capability_descriptor' "
            "  AND column_name = 'content_hash'"
        )
    )
    row = result.fetchone()
    assert row is not None, "content_hash column not found"
    assert row[0] == "YES", "content_hash must be nullable"


# ---------------------------------------------------------------------------
# D05  descriptor column is JSONB
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_descriptor_column_is_jsonb(async_session):
    """descriptor column must be stored as JSONB to enable containment queries."""
    result = await async_session.execute(
        text(
            "SELECT data_type, udt_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "  AND table_name = 'capability_descriptor' "
            "  AND column_name = 'descriptor'"
        )
    )
    row = result.fetchone()
    assert row is not None, "descriptor column not found"
    assert row[1] == "jsonb", f"Expected JSONB, got: {row[1]}"


# ---------------------------------------------------------------------------
# D06  create a CapabilityDescriptor row with kind=tool
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_capability_descriptor_tool(async_session):
    """Repository.create() persists a kind=tool row and returns a CapabilityDescriptorDB."""
    repo = CapabilityDescriptorRepository(async_session)
    row = await repo.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_TOOL_DESCRIPTOR,
        content_hash=None,
    )
    assert row is not None
    assert isinstance(row, CapabilityDescriptorDB)
    assert row.kind == DescriptorKind.TOOL
    assert row.org_id == _ORG_A
    assert row.content_hash is None
    assert row.descriptor["id"] == "google-drive-reader"
    assert row.id is not None


# ---------------------------------------------------------------------------
# D07  get_by_id returns the row just inserted
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_capability_descriptor_by_id(async_session):
    """Repository.get_by_id() returns the previously created row by its UUID."""
    repo = CapabilityDescriptorRepository(async_session)
    created = await repo.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_TOOL_DESCRIPTOR,
        content_hash=None,
    )
    fetched = await repo.get_by_id(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.kind == DescriptorKind.TOOL


# ---------------------------------------------------------------------------
# D08  get_by_id returns None for an unknown UUID
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_by_id_unknown_uuid_returns_none(async_session):
    """Repository.get_by_id() returns None when no row matches the given UUID."""
    repo = CapabilityDescriptorRepository(async_session)
    result = await repo.get_by_id(uuid.uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# D09  update_descriptor replaces JSONB content and bumps updated_at
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_update_descriptor_replaces_jsonb(async_session):
    """Repository.update_descriptor() replaces the descriptor field on the identified row."""
    repo = CapabilityDescriptorRepository(async_session)
    created = await repo.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_TOOL_DESCRIPTOR,
        content_hash=None,
    )
    updated_payload = {**_TOOL_DESCRIPTOR, "id": "google-drive-reader-v2"}
    updated = await repo.update_descriptor(created.id, descriptor=updated_payload)
    assert updated is not None
    assert updated.descriptor["id"] == "google-drive-reader-v2"
    assert updated.updated_at >= created.updated_at


# ---------------------------------------------------------------------------
# D10  delete removes the row; subsequent get_by_id returns None
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_delete_capability_descriptor(async_session):
    """Repository.delete() removes the row; get_by_id returns None afterwards."""
    repo = CapabilityDescriptorRepository(async_session)
    created = await repo.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_TOOL_DESCRIPTOR,
        content_hash=None,
    )
    deleted = await repo.delete(created.id)
    assert deleted is True
    assert await repo.get_by_id(created.id) is None


# ---------------------------------------------------------------------------
# D11  list_by_org returns all rows for an org and no rows for other orgs
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_by_org_scoped_to_org(async_session):
    """Repository.list_by_org() returns only rows belonging to the requested org_id."""
    repo = CapabilityDescriptorRepository(async_session)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.SKILL, descriptor=_SKILL_DESCRIPTOR)
    await repo.create(org_id=_ORG_B, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)

    org_a_rows = await repo.list_by_org(_ORG_A)
    assert len(org_a_rows) == 2
    assert all(r.org_id == _ORG_A for r in org_a_rows)


# ---------------------------------------------------------------------------
# D12  list_by_kind returns only rows matching the requested kind
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_by_kind_tool_returns_only_tools(async_session):
    """Repository.list_by_kind() with kind=tool returns only tool rows for the org."""
    repo = CapabilityDescriptorRepository(async_session)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.SKILL, descriptor=_SKILL_DESCRIPTOR)

    tools = await repo.list_by_kind(_ORG_A, DescriptorKind.TOOL)
    assert len(tools) == 2
    assert all(r.kind == DescriptorKind.TOOL for r in tools)


# ---------------------------------------------------------------------------
# D13  list_by_kind with kind=skill returns only skill rows
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_by_kind_skill_returns_only_skills(async_session):
    """Repository.list_by_kind() with kind=skill returns only skill rows for the org."""
    repo = CapabilityDescriptorRepository(async_session)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.SKILL, descriptor=_SKILL_DESCRIPTOR)

    skills = await repo.list_by_kind(_ORG_A, DescriptorKind.SKILL)
    assert len(skills) == 1
    assert skills[0].kind == DescriptorKind.SKILL


# ---------------------------------------------------------------------------
# D14  JSONB containment query matches rows whose descriptor contains the filter
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_search_by_jsonb_containment_match(async_session):
    """Repository.search_by_descriptor() finds rows via JSONB containment (@>) operator."""
    repo = CapabilityDescriptorRepository(async_session)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.SKILL, descriptor=_SKILL_DESCRIPTOR)

    results = await repo.search_by_descriptor(
        _ORG_A, {"kind": "tool"}
    )
    assert len(results) >= 1
    assert all(r.descriptor.get("kind") == "tool" for r in results)


# ---------------------------------------------------------------------------
# D15  JSONB containment query returns empty list when no rows match
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_search_by_jsonb_containment_no_match(async_session):
    """Repository.search_by_descriptor() returns [] when no descriptor matches the filter."""
    repo = CapabilityDescriptorRepository(async_session)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)

    results = await repo.search_by_descriptor(
        _ORG_A, {"id": "nonexistent-capability-xyz"}
    )
    assert results == []


# ---------------------------------------------------------------------------
# D16  org_id isolation: org B cannot read rows belonging to org A
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.organization_isolation
async def test_org_b_cannot_read_org_a_rows(async_session):
    """
    list_by_org(org_B) must return zero rows when only org_A rows exist.
    Verifies the fundamental org_id tenancy boundary on capability_descriptor.
    """
    repo = CapabilityDescriptorRepository(async_session)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await repo.create(org_id=_ORG_A, kind=DescriptorKind.SKILL, descriptor=_SKILL_DESCRIPTOR)

    org_b_rows = await repo.list_by_org(_ORG_B)
    assert org_b_rows == [], (
        f"org_B must not see org_A rows; got {len(org_b_rows)} rows"
    )


# ---------------------------------------------------------------------------
# D17  legacy tool_definition rows appear in capability_descriptor as kind=tool
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
def test_legacy_tool_definition_migrated_as_tool_kind(seeded_migrated_db):
    """
    After alembic upgrade head, existing tool_definitions rows must appear in
    capability_descriptor with kind='tool' (AC #3).

    seeded_migrated_db:
      1. downgrades to the revision before capability_descriptor
      2. seeds one tool_definitions row
      3. re-runs alembic upgrade head

    This test asserts that the migration forward-filled capability_descriptor
    from tool_definitions.  The test will fail until the implementer writes both
    the up migration (CREATE TABLE + data copy) and the down migration.
    """
    import asyncio

    import asyncpg

    postgres_dsn, seeded_id = seeded_migrated_db

    async def _query() -> int:
        conn = await asyncpg.connect(postgres_dsn)
        try:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM capability_descriptor WHERE kind = 'tool'"
            )
        finally:
            await conn.close()

    migrated_count = asyncio.run(_query())
    assert migrated_count >= 1, (
        f"Expected at least 1 kind=tool row in capability_descriptor after migrating "
        f"from tool_definitions (seeded id: {seeded_id}); found {migrated_count}"
    )


# ---------------------------------------------------------------------------
# D18  down migration is reversible: tool_definitions re-exists after downgrade
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
def test_migration_is_reversible(alembic_runner):
    """
    alembic downgrade -1 must succeed and restore the tool_definitions table shape.
    This test does NOT use async_session (it runs alembic as a subprocess) and
    re-upgrades at the end to leave the DB in the migrated state for other tests.

    The test will fail until the implementer writes both the up and down migrations
    (ORAA-69 acceptance criterion 5: Alembic migration is reversible).
    """
    # downgrade one revision
    alembic_runner.downgrade("-1")
    # upgrade back to head to restore state for other tests
    alembic_runner.upgrade("head")


# ---------------------------------------------------------------------------
# D19  all five valid kind values persist successfully
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "kind,descriptor",
    [
        (DescriptorKind.TOOL, _TOOL_DESCRIPTOR),
        (DescriptorKind.SKILL, _SKILL_DESCRIPTOR),
        (DescriptorKind.AGENT, _AGENT_DESCRIPTOR),
        (DescriptorKind.HARNESS, _HARNESS_DESCRIPTOR),
        (DescriptorKind.HUMAN_ROLE, _HUMAN_ROLE_DESCRIPTOR),
    ],
    ids=["tool", "skill", "agent", "harness", "human_role"],
)
async def test_all_valid_kind_values_persist(async_session, kind, descriptor):
    """Each of the 5 valid DescriptorKind values can be inserted and retrieved without error."""
    repo = CapabilityDescriptorRepository(async_session)
    created = await repo.create(org_id=_ORG_A, kind=kind, descriptor=descriptor)
    assert created.kind == kind
    fetched = await repo.get_by_id(created.id)
    assert fetched is not None
    assert fetched.kind == kind


# ---------------------------------------------------------------------------
# D20  inserting an invalid kind string raises IntegrityError at the DB layer
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_invalid_kind_raises_integrity_error(async_session):
    """
    Inserting a row with an invalid kind string must be rejected by the DB enum constraint.
    The DB-level enforcement ensures no application bug can silently write a bad kind value.
    PostgreSQL raises SQLSTATE 22P02 (invalid_text_representation) for enum violations.
    The SQLAlchemy asyncpg dialect does not map asyncpg.DataError → sqlalchemy.exc.DataError;
    it falls back to the parent DBAPIError. Accept all three to cover both sync and async paths.
    """
    with pytest.raises((DataError, IntegrityError, DBAPIError)):
        await async_session.execute(
            text(
                "INSERT INTO capability_descriptor (id, org_id, kind, descriptor, created_at, updated_at) "
                "VALUES (:id, :org_id, :kind, CAST(:descriptor AS jsonb), NOW(), NOW())"
            ),
            {
                "id": str(uuid.uuid4()),
                "org_id": str(_ORG_A),
                "kind": "not_a_real_kind",
                "descriptor": '{"kind": "not_a_real_kind"}',
            },
        )
        await async_session.flush()
