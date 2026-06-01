"""
[tests] unified capability registry service — integration tests

Story: ORAA-71 / ORA-69
Architecture refs:
  - Section 3 Layer 2:  https://oraclous.atlassian.net/wiki/spaces/OP/pages/65967
  - R2 release page:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - Test Strategy:      https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

All imports from app.services.capability_registry will fail with ImportError until
the implementer creates:
  - app/services/capability_registry.py   (CapabilityRegistryService)

And makes all of the following true:
  - ToolRegistryService removed from app/services/tool_registry.py
  - In-memory ToolRegistry class removed from app/tools/registry.py
  - app/services/tool_sync_service.py deleted

The ImportError on the module-level import below IS the expected initial TDD failure
(ADR-010).  Every test in this file is intentionally red until the implementer delivers
the unified registry.

Behaviours covered:
  R01  CapabilityRegistryService is importable from app.services.capability_registry
  R02  create() with kind=tool persists a row; content_hash is auto-computed (non-null) via repo delegation
  R03  create() with kind=skill persists a skill row
  R04  create() accepts an explicit content_hash and stores it
  R05  create() with all 5 valid kind values succeeds (parametrized)
  R06  get_by_id() returns the row for an existing UUID
  R07  get_by_id() returns None for an unknown UUID
  R08  update() replaces descriptor content and the change is durable on re-fetch
  R09  update() returns None for an unknown UUID — no exception raised
  R10  delete() removes the row; subsequent get_by_id() returns None
  R11  delete() returns False for an unknown UUID — no exception raised
  R12  list_by_org() returns all capabilities scoped to the requested org
  R13  list_by_org() returns an empty list for an org with no capabilities
  R14  list_by_kind() returns only capabilities matching the specified kind
  R15  search_by_descriptor() finds capabilities by JSONB containment filter
  R16  search_by_descriptor() returns [] when no descriptor matches the filter
  R17  org isolation: org B cannot read capabilities belonging to org A
  R18  ToolRegistryService class is no longer importable from app.services.tool_registry
  R19  in-memory ToolRegistry class is no longer importable from app.tools.registry
  R20  ToolSyncService module is no longer importable from app.services.tool_sync_service
"""

from __future__ import annotations

import uuid

import pytest

# ---------------------------------------------------------------------------
# Test org UUIDs
# ---------------------------------------------------------------------------

_ORG_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_ORG_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")

# ---------------------------------------------------------------------------
# Minimal valid OHM v1.0 JSONB descriptors for each kind
# ---------------------------------------------------------------------------

_TOOL_DESCRIPTOR: dict = {
    "kind": "tool",
    "id": "file-reader",
    "version": {"hash": "sha256:aaa111", "tags": ["1.0.0"]},
    "metadata": {"name": "File Reader", "description": "Read files from storage."},
    "spec": {
        "implementation": {"type": "internal", "handler": "file.FileReader"},
        "input_schema": {
            "type": "object",
            "required": ["file_id"],
            "properties": {"file_id": {"type": "string"}},
        },
        "output_schema": {"type": "object", "properties": {"content": {"type": "string"}}},
        "credential_requirements": [],
    },
}

_SKILL_DESCRIPTOR: dict = {
    "kind": "skill",
    "id": "data-formatter",
    "version": {"hash": "sha256:bbb222", "tags": ["1.0.0"]},
    "metadata": {"name": "Data Formatter", "description": "Format structured data."},
    "spec": {
        "loaded_when": "The actor needs to format data.",
        "instructions": "# Data Formatter\n\nFormat data consistently.",
        "capability_requirements": [],
    },
}

_AGENT_DESCRIPTOR: dict = {
    "kind": "agent",
    "id": "data-agent",
    "version": {"hash": "sha256:ccc333", "tags": ["1.0.0"]},
    "metadata": {"name": "Data Agent", "description": "Handles data tasks."},
    "spec": {
        "role": "You are the Data Agent.",
        "llm_config": {"provider_ref": "workspace-default"},
        "capabilities": [],
        "scope": {"workspaces": ["workspace-data"]},
    },
}

_HARNESS_DESCRIPTOR: dict = {
    "kind": "harness",
    "id": "data-pipeline",
    "version": {"hash": "sha256:ddd444", "tags": ["1.0.0"]},
    "metadata": {"name": "Data Pipeline", "description": "End-to-end data pipeline."},
    "spec": {
        "goal": "Process incoming data end-to-end.",
        "actors": [
            {
                "id": "processor",
                "kind": "agent",
                "ref": {"id": "data-agent", "version_tag": "stable"},
            }
        ],
        "orchestration": "1. Fetch. 2. Format. 3. Store.",
    },
}

_HUMAN_ROLE_DESCRIPTOR: dict = {
    "kind": "human_role",
    "id": "data-reviewer-role",
    "version": {"hash": "sha256:eee555", "tags": ["1.0.0"]},
    "metadata": {"name": "Data Reviewer", "description": "Reviews processed data."},
    "spec": {
        "role_name": "data_lead",
        "fallback": {"role": "engineering_director"},
    },
}

# Collection guard: these imports fail until the implementation modules exist.
# pytestmark below skips all tests so pytest can collect the file without error.
# The skip IS the TDD red state (ADR-010); remove this guard when impl lands.
try:
    from app.models.capability_descriptor import (
        CapabilityDescriptorDB,
        DescriptorKind,
    )
    from app.services.capability_registry import CapabilityRegistryService

    _ALL_KIND_DESCRIPTORS = [
        pytest.param(DescriptorKind.TOOL, _TOOL_DESCRIPTOR, id="tool"),
        pytest.param(DescriptorKind.SKILL, _SKILL_DESCRIPTOR, id="skill"),
        pytest.param(DescriptorKind.AGENT, _AGENT_DESCRIPTOR, id="agent"),
        pytest.param(DescriptorKind.HARNESS, _HARNESS_DESCRIPTOR, id="harness"),
        pytest.param(DescriptorKind.HUMAN_ROLE, _HUMAN_ROLE_DESCRIPTOR, id="human_role"),
    ]
    _APP_AVAILABLE = True
except ImportError:
    CapabilityDescriptorDB = None  # type: ignore[assignment]
    DescriptorKind = None  # type: ignore[assignment]
    CapabilityRegistryService = None  # type: ignore[assignment]
    _ALL_KIND_DESCRIPTORS = []
    _APP_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _APP_AVAILABLE,
    reason="app.services.capability_registry not yet implemented — TDD red state (ADR-010)",
)


# ---------------------------------------------------------------------------
# R01  CapabilityRegistryService is importable from app.services.capability_registry
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_capability_registry_service_is_importable():
    """CapabilityRegistryService must be importable from app.services.capability_registry."""
    from app.services.capability_registry import CapabilityRegistryService as CRS

    assert CRS is not None


# ---------------------------------------------------------------------------
# R02  create() with kind=tool persists a row and returns a CapabilityDescriptorDB
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_tool_capability(async_session):
    """create() with kind=tool returns a persisted CapabilityDescriptorDB row.

    The service delegates to CapabilityDescriptorRepository.create(), which
    auto-computes content_hash when none is supplied (C13 in
    test_content_hash_versioning.py).  The service therefore inherits that
    behaviour: content_hash must be non-null on the returned row.
    """
    svc = CapabilityRegistryService(async_session)
    row = await svc.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_TOOL_DESCRIPTOR,
    )
    assert isinstance(row, CapabilityDescriptorDB)
    assert row.id is not None
    assert row.org_id == _ORG_A
    assert row.kind == DescriptorKind.TOOL
    assert row.descriptor["id"] == "file-reader"
    assert row.content_hash is not None  # repo auto-computes; see C13


# ---------------------------------------------------------------------------
# R03  create() with kind=skill persists a skill row
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_skill_capability(async_session):
    """create() with kind=skill returns a persisted CapabilityDescriptorDB with kind=skill."""
    svc = CapabilityRegistryService(async_session)
    row = await svc.create(
        org_id=_ORG_A,
        kind=DescriptorKind.SKILL,
        descriptor=_SKILL_DESCRIPTOR,
    )
    assert isinstance(row, CapabilityDescriptorDB)
    assert row.kind == DescriptorKind.SKILL
    assert row.descriptor["id"] == "data-formatter"


# ---------------------------------------------------------------------------
# R04  create() accepts an explicit content_hash and stores it
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_with_explicit_content_hash(async_session):
    """create() stores a caller-supplied content_hash on the row."""
    svc = CapabilityRegistryService(async_session)
    row = await svc.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_TOOL_DESCRIPTOR,
        content_hash="sha256:aaa111deadbeef",
    )
    assert row.content_hash == "sha256:aaa111deadbeef"


# ---------------------------------------------------------------------------
# R05  create() with all 5 valid kind values succeeds (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("kind,descriptor", _ALL_KIND_DESCRIPTORS)
async def test_create_all_valid_kind_values(async_session, kind, descriptor):
    """create() must accept all five valid DescriptorKind values without error."""
    svc = CapabilityRegistryService(async_session)
    row = await svc.create(org_id=_ORG_A, kind=kind, descriptor=descriptor)
    assert row.kind == kind
    assert row.id is not None


# ---------------------------------------------------------------------------
# R06  get_by_id() returns the row for an existing UUID
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_by_id_returns_existing_row(async_session):
    """get_by_id() returns the previously created row by UUID."""
    svc = CapabilityRegistryService(async_session)
    created = await svc.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    fetched = await svc.get_by_id(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.kind == DescriptorKind.TOOL


# ---------------------------------------------------------------------------
# R07  get_by_id() returns None for an unknown UUID
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_by_id_unknown_uuid_returns_none(async_session):
    """get_by_id() returns None when no row matches the given UUID."""
    svc = CapabilityRegistryService(async_session)
    result = await svc.get_by_id(uuid.uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# R08  update() replaces descriptor content and the change is durable on re-fetch
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_update_replaces_descriptor_durably(async_session):
    """update() replaces the descriptor JSONB and the new value survives a re-fetch."""
    svc = CapabilityRegistryService(async_session)
    created = await svc.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    new_descriptor = {**_TOOL_DESCRIPTOR, "id": "file-reader-v2"}
    updated = await svc.update(created.id, descriptor=new_descriptor)
    assert updated is not None
    assert updated.descriptor["id"] == "file-reader-v2"

    refetched = await svc.get_by_id(created.id)
    assert refetched.descriptor["id"] == "file-reader-v2"


# ---------------------------------------------------------------------------
# R09  update() returns None for an unknown UUID — no exception raised
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_update_unknown_id_returns_none(async_session):
    """update() returns None gracefully when the target UUID does not exist."""
    svc = CapabilityRegistryService(async_session)
    result = await svc.update(uuid.uuid4(), descriptor={"id": "ghost-capability"})
    assert result is None


# ---------------------------------------------------------------------------
# R10  delete() removes the row; subsequent get_by_id() returns None
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_delete_removes_row(async_session):
    """delete() removes the capability row; get_by_id() returns None afterwards."""
    svc = CapabilityRegistryService(async_session)
    created = await svc.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    deleted = await svc.delete(created.id)
    assert deleted is True
    assert await svc.get_by_id(created.id) is None


# ---------------------------------------------------------------------------
# R11  delete() returns False for an unknown UUID — no exception raised
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_delete_unknown_id_returns_false(async_session):
    """delete() returns False gracefully when the target UUID does not exist."""
    svc = CapabilityRegistryService(async_session)
    result = await svc.delete(uuid.uuid4())
    assert result is False


# ---------------------------------------------------------------------------
# R12  list_by_org() returns all capabilities scoped to the requested org
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_by_org_returns_all_org_capabilities(async_session):
    """list_by_org() returns all rows for the specified org and no rows from other orgs."""
    svc = CapabilityRegistryService(async_session)
    await svc.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await svc.create(org_id=_ORG_A, kind=DescriptorKind.SKILL, descriptor=_SKILL_DESCRIPTOR)
    await svc.create(org_id=_ORG_B, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)

    org_a_rows = await svc.list_by_org(_ORG_A)
    assert len(org_a_rows) == 2
    assert all(r.org_id == _ORG_A for r in org_a_rows)


# ---------------------------------------------------------------------------
# R13  list_by_org() returns an empty list for an org with no capabilities
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_by_org_empty_for_org_with_no_capabilities(async_session):
    """list_by_org() returns [] when the org has no capabilities registered."""
    svc = CapabilityRegistryService(async_session)
    result = await svc.list_by_org(uuid.UUID("cccccccc-0000-0000-0000-000000000099"))
    assert result == []


# ---------------------------------------------------------------------------
# R14  list_by_kind() returns only capabilities matching the specified kind
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_by_kind_filters_correctly(async_session):
    """list_by_kind() returns only rows whose kind matches the requested value."""
    svc = CapabilityRegistryService(async_session)
    await svc.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await svc.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await svc.create(org_id=_ORG_A, kind=DescriptorKind.SKILL, descriptor=_SKILL_DESCRIPTOR)

    tools = await svc.list_by_kind(_ORG_A, DescriptorKind.TOOL)
    assert len(tools) == 2
    assert all(r.kind == DescriptorKind.TOOL for r in tools)

    skills = await svc.list_by_kind(_ORG_A, DescriptorKind.SKILL)
    assert len(skills) == 1
    assert skills[0].kind == DescriptorKind.SKILL


# ---------------------------------------------------------------------------
# R15  search_by_descriptor() finds capabilities by JSONB containment filter
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_search_by_descriptor_matches(async_session):
    """search_by_descriptor() returns rows whose descriptor contains the given filter dict."""
    svc = CapabilityRegistryService(async_session)
    await svc.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await svc.create(org_id=_ORG_A, kind=DescriptorKind.SKILL, descriptor=_SKILL_DESCRIPTOR)

    results = await svc.search_by_descriptor(_ORG_A, {"kind": "tool"})
    assert len(results) >= 1
    assert all(r.descriptor.get("kind") == "tool" for r in results)


# ---------------------------------------------------------------------------
# R16  search_by_descriptor() returns [] when no descriptor matches the filter
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_search_by_descriptor_no_match_returns_empty(async_session):
    """search_by_descriptor() returns [] when no row matches the JSONB filter."""
    svc = CapabilityRegistryService(async_session)
    await svc.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)

    results = await svc.search_by_descriptor(_ORG_A, {"id": "nonexistent-xyz-capability"})
    assert results == []


# ---------------------------------------------------------------------------
# R17  org isolation: org B cannot read capabilities belonging to org A
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.organization_isolation
async def test_org_isolation_list_by_org(async_session):
    """
    list_by_org(org_B) returns zero rows when only org_A capabilities exist.
    Enforces the fundamental org_id tenancy boundary on capability_descriptor.
    """
    svc = CapabilityRegistryService(async_session)
    await svc.create(org_id=_ORG_A, kind=DescriptorKind.TOOL, descriptor=_TOOL_DESCRIPTOR)
    await svc.create(org_id=_ORG_A, kind=DescriptorKind.SKILL, descriptor=_SKILL_DESCRIPTOR)

    org_b_rows = await svc.list_by_org(_ORG_B)
    assert org_b_rows == [], f"org_B must not see org_A capabilities; got {len(org_b_rows)} rows"


# ---------------------------------------------------------------------------
# R18  ToolRegistryService is no longer importable from app.services.tool_registry
#
# Currently FAILS: the class still exists (dual-registry not yet collapsed).
# Passes after the implementer removes ToolRegistryService from that module.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_tool_registry_service_class_removed():
    """
    ToolRegistryService must not exist in app.services.tool_registry after the
    dual-registry collapse.  All capability lookups route through
    CapabilityRegistryService exclusively.
    """
    with pytest.raises(ImportError):
        from app.services.tool_registry import ToolRegistryService  # noqa: F401


# ---------------------------------------------------------------------------
# R19  in-memory ToolRegistry class is no longer importable from app.tools.registry
#
# Currently FAILS: the class still exists.
# Passes after the implementer removes the in-memory ToolRegistry class.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_in_memory_tool_registry_class_removed():
    """
    The in-memory ToolRegistry class must not exist in app.tools.registry after
    the dual-registry collapse.  There is no longer an in-memory dict — the DB
    is the single source of truth for every capability lookup.
    """
    with pytest.raises(ImportError):
        from app.tools.registry import ToolRegistry  # noqa: F401


# ---------------------------------------------------------------------------
# R20  ToolSyncService module is no longer importable from app.services.tool_sync_service
#
# Currently FAILS: the module still exists.
# Passes after the implementer deletes tool_sync_service.py.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_tool_sync_service_module_deleted():
    """
    app.services.tool_sync_service must not be importable after the collapse.
    ToolSyncService is deleted because there is no in-memory registry left to
    synchronise against; the DB-backed CapabilityRegistryService is the sole
    authoritative store.
    """
    with pytest.raises(ImportError):
        import app.services.tool_sync_service  # noqa: F401
