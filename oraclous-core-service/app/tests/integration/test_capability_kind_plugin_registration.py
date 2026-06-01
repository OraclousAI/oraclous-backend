"""
[tests] capability-kind plugin registration — integration — ORAA-73

Story: ORAA-73 / ORA-71
Architecture refs:
  - Section 7 Portability Story: https://oraclous.atlassian.net/wiki/spaces/OP/pages/753728
  - R2 release page:             https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - Test Strategy:               https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

All imports from app.tools.plugin will fail with ImportError until the implementer creates:
  - app/tools/plugin.py         (CapabilityKindPlugin, discover_registered_plugins,
                                  plugin_registry, sync_plugins_to_registry)
  - Reshapes app/tools/__init__.py to use plugin auto-discovery
  - Reshapes app/tools/factory.py to remove hard-coded executor additions
  - Each of the 4 shipped tools self-registers via plugin_registry.register()

The ImportError on the module-level import below IS the expected initial TDD failure (ADR-010).
Every test in this file is intentionally red until the implementer delivers the above.

Behaviours covered:
  P06  GoogleDriveReader is discoverable via discover_registered_plugins() after app.tools import
  P07  NotionReader is discoverable via discover_registered_plugins() after app.tools import
  P08  PostgreSQLReader is discoverable via discover_registered_plugins() after app.tools import
  P09  MySQLReader is discoverable via discover_registered_plugins() after app.tools import
  P10  sync_plugins_to_registry(org_id, session) persists a mock plugin to capability_descriptor
  P11  Persisted row has kind matching the plugin's declared DescriptorKind
  P12  sync_plugins_to_registry() is idempotent: a second call does not create duplicate rows
"""

from __future__ import annotations

import uuid

import pytest

import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind

# ---------------------------------------------------------------------------
# This import will fail with ImportError until the implementer creates
# app/tools/plugin.py.  The ImportError IS the expected initial TDD failure.
# ---------------------------------------------------------------------------
from app.tools.plugin import (  # noqa: E402
    CapabilityKindPlugin,
    discover_registered_plugins,
    plugin_registry,
    sync_plugins_to_registry,
)

# ---------------------------------------------------------------------------
# Test org UUID
# ---------------------------------------------------------------------------

_ORG_PLUGIN_TEST = uuid.UUID("dddddddd-0000-0000-0000-000000000073")

# ---------------------------------------------------------------------------
# Minimal OHM-format descriptors for mock fixtures
# ---------------------------------------------------------------------------

_MOCK_TOOL_DESCRIPTOR: dict = {
    "kind": "tool",
    "id": "mock-integration-tool",
    "version": {"hash": "sha256:inttest001", "tags": ["0.0.1"]},
    "metadata": {
        "name": "Mock Integration Tool",
        "description": "Fixture-only mock tool for ORAA-73 integration tests.",
    },
    "spec": {
        "implementation": {"type": "internal", "handler": "tests.mock.MockIntegrationTool"},
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "properties": {}},
        "credential_requirements": [],
    },
}

_MOCK_SKILL_DESCRIPTOR: dict = {
    "kind": "skill",
    "id": "mock-integration-skill",
    "version": {"hash": "sha256:inttest002", "tags": ["0.0.1"]},
    "metadata": {
        "name": "Mock Integration Skill",
        "description": "Fixture-only mock skill for ORAA-73 integration tests.",
    },
    "spec": {
        "loaded_when": "Never — fixture only.",
        "instructions": "# Mock Skill\nDoes nothing.",
        "capability_requirements": [],
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_tool_plugin():
    """Register a mock tool plugin; unregister on teardown."""

    class MockIntegrationTool(CapabilityKindPlugin):
        @classmethod
        def get_ohm_descriptor(cls) -> dict:
            return _MOCK_TOOL_DESCRIPTOR

        @classmethod
        def get_kind(cls) -> DescriptorKind:
            return DescriptorKind.TOOL

        @classmethod
        def get_plugin_id(cls) -> str:
            return "mock-integration-tool"

    plugin_registry.register(MockIntegrationTool)
    yield MockIntegrationTool
    plugin_registry.unregister(MockIntegrationTool)


@pytest.fixture
def mock_skill_plugin():
    """Register a mock skill plugin; unregister on teardown."""

    class MockIntegrationSkill(CapabilityKindPlugin):
        @classmethod
        def get_ohm_descriptor(cls) -> dict:
            return _MOCK_SKILL_DESCRIPTOR

        @classmethod
        def get_kind(cls) -> DescriptorKind:
            return DescriptorKind.SKILL

        @classmethod
        def get_plugin_id(cls) -> str:
            return "mock-integration-skill"

    plugin_registry.register(MockIntegrationSkill)
    yield MockIntegrationSkill
    plugin_registry.unregister(MockIntegrationSkill)


# ---------------------------------------------------------------------------
# P06  GoogleDriveReader is discoverable via the plugin mechanism
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_google_drive_reader_discoverable_via_plugin():
    """
    After `import app.tools`, GoogleDriveReader must appear in
    discover_registered_plugins().

    Fails until GoogleDriveReader self-registers via plugin_registry.register()
    and app/tools/__init__.py triggers auto-discovery on import.
    """
    discovered_names = {p.__name__ for p in discover_registered_plugins()}
    assert "GoogleDriveReader" in discovered_names, (
        f"GoogleDriveReader not found in discovered plugins: {discovered_names!r}. "
        "GoogleDriveReader must call plugin_registry.register() at module scope."
    )


# ---------------------------------------------------------------------------
# P07  NotionReader is discoverable via the plugin mechanism
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_notion_reader_discoverable_via_plugin():
    """
    After `import app.tools`, NotionReader must appear in discover_registered_plugins().
    """
    discovered_names = {p.__name__ for p in discover_registered_plugins()}
    assert "NotionReader" in discovered_names, (
        f"NotionReader not found in discovered plugins: {discovered_names!r}. "
        "NotionReader must call plugin_registry.register() at module scope."
    )


# ---------------------------------------------------------------------------
# P08  PostgreSQLReader is discoverable via the plugin mechanism
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_postgresql_reader_discoverable_via_plugin():
    """
    After `import app.tools`, PostgreSQLReader must appear in discover_registered_plugins().
    """
    discovered_names = {p.__name__ for p in discover_registered_plugins()}
    assert "PostgreSQLReader" in discovered_names, (
        f"PostgreSQLReader not found in discovered plugins: {discovered_names!r}. "
        "PostgreSQLReader must call plugin_registry.register() at module scope."
    )


# ---------------------------------------------------------------------------
# P09  MySQLReader is discoverable via the plugin mechanism
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_mysql_reader_discoverable_via_plugin():
    """
    After `import app.tools`, MySQLReader must appear in discover_registered_plugins().
    """
    discovered_names = {p.__name__ for p in discover_registered_plugins()}
    assert "MySQLReader" in discovered_names, (
        f"MySQLReader not found in discovered plugins: {discovered_names!r}. "
        "MySQLReader must call plugin_registry.register() at module scope."
    )


# ---------------------------------------------------------------------------
# P10  sync_plugins_to_registry() persists a mock plugin to capability_descriptor
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_mock_plugin_persisted_to_capability_descriptor(
    async_session, mock_tool_plugin
):
    """
    sync_plugins_to_registry(org_id, session) must write the mock plugin's OHM descriptor
    to the capability_descriptor table and return a CapabilityDescriptorDB row.

    This is the core acceptance criterion: a capability kind registered via the plugin
    mechanism appears in the DB without any factory.py or __init__.py modification.
    """
    rows = await sync_plugins_to_registry(_ORG_PLUGIN_TEST, async_session)
    assert len(rows) >= 1, "sync_plugins_to_registry() must return at least one persisted row"

    mock_rows = [r for r in rows if r.descriptor.get("id") == "mock-integration-tool"]
    assert len(mock_rows) == 1, (
        f"Expected exactly one row for mock-integration-tool; got {len(mock_rows)}"
    )
    row = mock_rows[0]
    assert isinstance(row, CapabilityDescriptorDB)
    assert row.org_id == _ORG_PLUGIN_TEST
    assert row.descriptor["id"] == "mock-integration-tool"


# ---------------------------------------------------------------------------
# P11  Persisted row has kind matching the plugin's declared DescriptorKind
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_persisted_plugin_row_kind_matches_declared_kind(
    async_session, mock_tool_plugin
):
    """
    The capability_descriptor row written by sync_plugins_to_registry() must have
    kind == the DescriptorKind returned by the plugin's get_kind() method.
    """
    rows = await sync_plugins_to_registry(_ORG_PLUGIN_TEST, async_session)
    mock_row = next(
        (r for r in rows if r.descriptor.get("id") == "mock-integration-tool"), None
    )
    assert mock_row is not None, "mock-integration-tool row not found after sync"
    assert mock_row.kind == DescriptorKind.TOOL, (
        f"Expected kind=tool; got {mock_row.kind!r}. "
        "The persisted kind must match the plugin's get_kind() return value."
    )


# ---------------------------------------------------------------------------
# P12  sync_plugins_to_registry() is idempotent: no duplicate rows on second call
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sync_plugins_idempotent_no_duplicates(async_session, mock_tool_plugin):
    """
    Calling sync_plugins_to_registry() twice for the same org must not create duplicate
    rows in capability_descriptor.

    Idempotency is required because the service may sync on every startup; duplicate rows
    would corrupt list_by_kind() and search_by_descriptor() results.
    """
    from app.services.capability_registry import CapabilityRegistryService

    await sync_plugins_to_registry(_ORG_PLUGIN_TEST, async_session)
    await sync_plugins_to_registry(_ORG_PLUGIN_TEST, async_session)

    svc = CapabilityRegistryService(async_session)
    all_rows = await svc.list_by_org(_ORG_PLUGIN_TEST)
    mock_rows = [r for r in all_rows if r.descriptor.get("id") == "mock-integration-tool"]

    assert len(mock_rows) == 1, (
        f"Expected exactly 1 row for mock-integration-tool after two sync calls; "
        f"got {len(mock_rows)}.  sync_plugins_to_registry() must be idempotent."
    )


# ---------------------------------------------------------------------------
# P11b  A skill-kind plugin synced with kind=skill persists as kind=skill
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_skill_plugin_persisted_with_kind_skill(async_session, mock_skill_plugin):
    """
    A plugin declaring get_kind() == DescriptorKind.SKILL must persist with kind=skill
    in capability_descriptor — confirming the plugin mechanism handles all 5 kinds,
    not just tools.
    """
    rows = await sync_plugins_to_registry(_ORG_PLUGIN_TEST, async_session)
    skill_rows = [r for r in rows if r.descriptor.get("id") == "mock-integration-skill"]
    assert len(skill_rows) == 1, (
        f"Expected one skill row for mock-integration-skill; got {len(skill_rows)}"
    )
    assert skill_rows[0].kind == DescriptorKind.SKILL, (
        f"Expected kind=skill; got {skill_rows[0].kind!r}"
    )
