"""
[tests] OHM descriptors for ingestion tools — integration — ORAA-74

Story: ORAA-74 / ORA-72
Architecture refs:
  - OHM v1.0 Spec:           https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - Section 7 Portability:   https://oraclous.atlassian.net/wiki/spaces/OP/pages/753728
  - Test Strategy:           https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

Blocked-by resolved: ORAA-73 (plugin registration infrastructure) is done.
These tests verify that each of the 4 ingestion tools:
  1. is discoverable in the plugin registry after `import app.tools`
  2. has a get_ohm_descriptor() that validates against the OHM spec
  3. can be synced to the capability_descriptor table via sync_plugins_to_registry()
  4. once stored, the persisted descriptor also validates against OHM spec

Tests currently failing (before ORAA-74 implementation):
  I-GDR-02  GoogleDriveReader discovered descriptor fails OHM validation — type "oauth2"
  I-GDR-03  GoogleDriveReader synced descriptor fails OHM spec validation
  I-PG-02   PostgreSQLReader discovered descriptor fails OHM validation — type "database"
  I-PG-03   PostgreSQLReader synced descriptor fails OHM spec validation
  I-MY-02   MySQLReader discovered descriptor fails OHM validation — type "database"
  I-MY-03   MySQLReader synced descriptor fails OHM spec validation

Behaviours covered:
  I-GDR-01  GoogleDriveReader appears in discover_registered_plugins() after app.tools import
  I-GDR-02  GoogleDriveReader's discovered descriptor validates as OHM ToolDescriptor
  I-GDR-03  GoogleDriveReader persisted to registry; stored descriptor validates against OHM

  I-NR-01   NotionReader appears in discover_registered_plugins() after app.tools import
  I-NR-02   NotionReader's discovered descriptor validates as OHM ToolDescriptor
  I-NR-03   NotionReader persisted to registry; stored descriptor validates against OHM

  I-PG-01   PostgreSQLReader appears in discover_registered_plugins() after app.tools import
  I-PG-02   PostgreSQLReader's discovered descriptor validates as OHM ToolDescriptor
  I-PG-03   PostgreSQLReader persisted to registry; stored descriptor validates against OHM

  I-MY-01   MySQLReader appears in discover_registered_plugins() after app.tools import
  I-MY-02   MySQLReader's discovered descriptor validates as OHM ToolDescriptor
  I-MY-03   MySQLReader persisted to registry; stored descriptor validates against OHM

  I-ALL-01  All 4 ingestion tools present in plugin registry after a single app.tools import
  I-ALL-02  All 4 ingestion tool descriptors validate as OHM ToolDescriptors
  I-ALL-03  sync_plugins_to_registry() persists all 4 tools to capability_descriptor
  I-ALL-04  All 4 persisted rows carry kind=TOOL
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import TypeAdapter


def _parse(data: dict):
    from ohm.schemas import CapabilityDescriptor

    return TypeAdapter(CapabilityDescriptor).validate_python(data)


_ORG_ORAA74 = uuid.UUID("74747474-0000-0000-0000-000000000074")

_INGESTION_PLUGIN_IDS = frozenset(
    {
        "google-drive-reader",
        "notion-reader",
        "postgresql-reader",
        "mysql-reader",
    }
)


def _get_plugin(plugin_id: str):
    """Return the plugin class for the given plugin_id, or None if not found."""
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.tools.plugin import discover_registered_plugins

    return next(
        (p for p in discover_registered_plugins() if p.get_plugin_id() == plugin_id),
        None,
    )


# ===========================================================================
# I-GDR  Google Drive Reader
# ===========================================================================


# I-GDR-01  GoogleDriveReader appears in plugin registry
@pytest.mark.integration
def test_google_drive_reader_discoverable_via_plugin():
    """
    After `import app.tools`, GoogleDriveReader must appear in
    discover_registered_plugins() with plugin_id 'google-drive-reader'.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.tools.plugin import discover_registered_plugins

    discovered_ids = {p.get_plugin_id() for p in discover_registered_plugins()}
    assert "google-drive-reader" in discovered_ids, (
        f"'google-drive-reader' not found in plugin registry: {discovered_ids!r}"
    )


# I-GDR-02  GoogleDriveReader discovered descriptor validates as OHM ToolDescriptor
@pytest.mark.integration
def test_google_drive_reader_discovered_descriptor_validates():
    """
    The descriptor returned by the discovered GoogleDriveReader plugin must validate
    as an OHM ToolDescriptor.

    FAILS until fixed: the current descriptor uses credential type 'oauth2', which is
    not a valid CredentialType. The implementer must change it to 'oauth_token' and
    add the required scopes to satisfy T2-M3.
    """
    from ohm.schemas import ToolDescriptor

    plugin = _get_plugin("google-drive-reader")
    assert plugin is not None, "'google-drive-reader' plugin not registered"
    descriptor = plugin.get_ohm_descriptor()
    result = _parse(descriptor)
    assert isinstance(result, ToolDescriptor), (
        f"Discovered GoogleDriveReader descriptor must validate as ToolDescriptor; "
        f"got {type(result).__name__}"
    )


# I-GDR-03  GoogleDriveReader synced descriptor validates against OHM spec
@pytest.mark.integration
async def test_google_drive_reader_synced_descriptor_validates(async_session):
    """
    After sync_plugins_to_registry(), the descriptor stored in capability_descriptor
    for 'google-drive-reader' must parse as a valid OHM ToolDescriptor.

    FAILS until fixed: if the raw descriptor stored by sync_plugins_to_registry()
    carries the invalid credential type, the OHM TypeAdapter must reject it, confirming
    the implementation is incomplete. The implementer must fix the descriptor first.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.models.capability_descriptor import CapabilityDescriptorDB
    from app.tools.plugin import sync_plugins_to_registry
    from ohm.schemas import ToolDescriptor

    rows = await sync_plugins_to_registry(_ORG_ORAA74, async_session)
    gdrive_row = next((r for r in rows if r.descriptor.get("id") == "google-drive-reader"), None)
    assert gdrive_row is not None, (
        "No capability_descriptor row found for 'google-drive-reader' after sync"
    )
    assert isinstance(gdrive_row, CapabilityDescriptorDB)
    parsed = _parse(gdrive_row.descriptor)
    assert isinstance(parsed, ToolDescriptor), (
        "Stored Google Drive Reader descriptor must parse as ToolDescriptor"
    )


# ===========================================================================
# I-NR  Notion Reader
# ===========================================================================


# I-NR-01  NotionReader appears in plugin registry
@pytest.mark.integration
def test_notion_reader_discoverable_via_plugin():
    """
    After `import app.tools`, NotionReader must appear in discover_registered_plugins()
    with plugin_id 'notion-reader'.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.tools.plugin import discover_registered_plugins

    discovered_ids = {p.get_plugin_id() for p in discover_registered_plugins()}
    assert "notion-reader" in discovered_ids, (
        f"'notion-reader' not found in plugin registry: {discovered_ids!r}"
    )


# I-NR-02  NotionReader discovered descriptor validates as OHM ToolDescriptor
@pytest.mark.integration
def test_notion_reader_discovered_descriptor_validates():
    """
    The descriptor returned by the discovered NotionReader plugin must validate as
    an OHM ToolDescriptor.
    """
    from ohm.schemas import ToolDescriptor

    plugin = _get_plugin("notion-reader")
    assert plugin is not None, "'notion-reader' plugin not registered"
    descriptor = plugin.get_ohm_descriptor()
    result = _parse(descriptor)
    assert isinstance(result, ToolDescriptor), (
        "Discovered NotionReader descriptor must validate as ToolDescriptor"
    )


# I-NR-03  NotionReader synced descriptor validates against OHM spec
@pytest.mark.integration
async def test_notion_reader_synced_descriptor_validates(async_session):
    """
    After sync_plugins_to_registry(), the descriptor stored for 'notion-reader'
    must parse as a valid OHM ToolDescriptor.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.models.capability_descriptor import CapabilityDescriptorDB
    from app.tools.plugin import sync_plugins_to_registry
    from ohm.schemas import ToolDescriptor

    rows = await sync_plugins_to_registry(_ORG_ORAA74, async_session)
    notion_row = next((r for r in rows if r.descriptor.get("id") == "notion-reader"), None)
    assert notion_row is not None, (
        "No capability_descriptor row found for 'notion-reader' after sync"
    )
    assert isinstance(notion_row, CapabilityDescriptorDB)
    parsed = _parse(notion_row.descriptor)
    assert isinstance(parsed, ToolDescriptor), (
        "Stored Notion Reader descriptor must parse as ToolDescriptor"
    )


# ===========================================================================
# I-PG  PostgreSQL Reader
# ===========================================================================


# I-PG-01  PostgreSQLReader appears in plugin registry
@pytest.mark.integration
def test_postgresql_reader_discoverable_via_plugin():
    """
    After `import app.tools`, PostgreSQLReader must appear in
    discover_registered_plugins() with plugin_id 'postgresql-reader'.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.tools.plugin import discover_registered_plugins

    discovered_ids = {p.get_plugin_id() for p in discover_registered_plugins()}
    assert "postgresql-reader" in discovered_ids, (
        f"'postgresql-reader' not found in plugin registry: {discovered_ids!r}"
    )


# I-PG-02  PostgreSQLReader discovered descriptor validates as OHM ToolDescriptor
@pytest.mark.integration
def test_postgresql_reader_discovered_descriptor_validates():
    """
    The descriptor returned by the discovered PostgreSQLReader plugin must validate
    as an OHM ToolDescriptor.

    FAILS until fixed: the current descriptor uses credential type 'database', which is
    not a valid CredentialType. The implementer must change it to 'connection_string'.
    """
    from ohm.schemas import ToolDescriptor

    plugin = _get_plugin("postgresql-reader")
    assert plugin is not None, "'postgresql-reader' plugin not registered"
    descriptor = plugin.get_ohm_descriptor()
    result = _parse(descriptor)
    assert isinstance(result, ToolDescriptor), (
        "Discovered PostgreSQLReader descriptor must validate as ToolDescriptor"
    )


# I-PG-03  PostgreSQLReader synced descriptor validates against OHM spec
@pytest.mark.integration
async def test_postgresql_reader_synced_descriptor_validates(async_session):
    """
    After sync_plugins_to_registry(), the descriptor stored for 'postgresql-reader'
    must parse as a valid OHM ToolDescriptor.

    FAILS until fixed: 'database' is not a valid CredentialType in the OHM spec.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.models.capability_descriptor import CapabilityDescriptorDB
    from app.tools.plugin import sync_plugins_to_registry
    from ohm.schemas import ToolDescriptor

    rows = await sync_plugins_to_registry(_ORG_ORAA74, async_session)
    pg_row = next((r for r in rows if r.descriptor.get("id") == "postgresql-reader"), None)
    assert pg_row is not None, (
        "No capability_descriptor row found for 'postgresql-reader' after sync"
    )
    assert isinstance(pg_row, CapabilityDescriptorDB)
    parsed = _parse(pg_row.descriptor)
    assert isinstance(parsed, ToolDescriptor), (
        "Stored PostgreSQL Reader descriptor must parse as ToolDescriptor"
    )


# ===========================================================================
# I-MY  MySQL Reader
# ===========================================================================


# I-MY-01  MySQLReader appears in plugin registry
@pytest.mark.integration
def test_mysql_reader_discoverable_via_plugin():
    """
    After `import app.tools`, MySQLReader must appear in discover_registered_plugins()
    with plugin_id 'mysql-reader'.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.tools.plugin import discover_registered_plugins

    discovered_ids = {p.get_plugin_id() for p in discover_registered_plugins()}
    assert "mysql-reader" in discovered_ids, (
        f"'mysql-reader' not found in plugin registry: {discovered_ids!r}"
    )


# I-MY-02  MySQLReader discovered descriptor validates as OHM ToolDescriptor
@pytest.mark.integration
def test_mysql_reader_discovered_descriptor_validates():
    """
    The descriptor returned by the discovered MySQLReader plugin must validate as
    an OHM ToolDescriptor.

    FAILS until fixed: the current descriptor uses credential type 'database', which is
    not a valid CredentialType. The implementer must change it to 'connection_string'.
    """
    from ohm.schemas import ToolDescriptor

    plugin = _get_plugin("mysql-reader")
    assert plugin is not None, "'mysql-reader' plugin not registered"
    descriptor = plugin.get_ohm_descriptor()
    result = _parse(descriptor)
    assert isinstance(result, ToolDescriptor), (
        "Discovered MySQLReader descriptor must validate as ToolDescriptor"
    )


# I-MY-03  MySQLReader synced descriptor validates against OHM spec
@pytest.mark.integration
async def test_mysql_reader_synced_descriptor_validates(async_session):
    """
    After sync_plugins_to_registry(), the descriptor stored for 'mysql-reader'
    must parse as a valid OHM ToolDescriptor.

    FAILS until fixed: 'database' is not a valid CredentialType in the OHM spec.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.models.capability_descriptor import CapabilityDescriptorDB
    from app.tools.plugin import sync_plugins_to_registry
    from ohm.schemas import ToolDescriptor

    rows = await sync_plugins_to_registry(_ORG_ORAA74, async_session)
    mysql_row = next((r for r in rows if r.descriptor.get("id") == "mysql-reader"), None)
    assert mysql_row is not None, "No capability_descriptor row found for 'mysql-reader' after sync"
    assert isinstance(mysql_row, CapabilityDescriptorDB)
    parsed = _parse(mysql_row.descriptor)
    assert isinstance(parsed, ToolDescriptor), (
        "Stored MySQL Reader descriptor must parse as ToolDescriptor"
    )


# ===========================================================================
# I-ALL  Cross-tool invariants
# ===========================================================================


# I-ALL-01  All 4 ingestion tools present in plugin registry
@pytest.mark.integration
def test_all_four_ingestion_tools_discoverable():
    """
    All 4 ingestion tools (google-drive-reader, notion-reader, postgresql-reader,
    mysql-reader) must appear in discover_registered_plugins() after `import app.tools`.

    This is the central ORAA-74 registration invariant: all 4 tools self-register
    via plugin_registry.register() at import time without requiring any modification
    to core factory or init files.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.tools.plugin import discover_registered_plugins

    discovered_ids = {p.get_plugin_id() for p in discover_registered_plugins()}
    missing = _INGESTION_PLUGIN_IDS - discovered_ids
    assert not missing, (
        f"The following ingestion tool plugins are not registered: {missing!r}. "
        "Each must call plugin_registry.register() at module scope."
    )


# I-ALL-02  All 4 ingestion tool descriptors validate as OHM ToolDescriptors
@pytest.mark.integration
@pytest.mark.parametrize("plugin_id", sorted(_INGESTION_PLUGIN_IDS))
def test_each_ingestion_tool_descriptor_validates(plugin_id: str):
    """
    Each of the 4 ingestion tools' OHM descriptor must validate as a ToolDescriptor.

    FAILS until fixed for: google-drive-reader (oauth2 + no scopes),
    postgresql-reader (database), mysql-reader (database).
    """
    from ohm.schemas import ToolDescriptor

    plugin = _get_plugin(plugin_id)
    assert plugin is not None, f"Plugin '{plugin_id}' not found in registry"
    descriptor = plugin.get_ohm_descriptor()
    result = _parse(descriptor)
    assert isinstance(result, ToolDescriptor), (
        f"Descriptor for '{plugin_id}' must validate as ToolDescriptor"
    )


# I-ALL-03  sync_plugins_to_registry() persists all 4 tools to capability_descriptor
@pytest.mark.integration
async def test_sync_persists_all_four_ingestion_tools(async_session):
    """
    sync_plugins_to_registry() must write a capability_descriptor row for each of
    the 4 ingestion tools.

    Confirms that the plugin → DB pipeline works end-to-end for all 4 wrapped tools.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.tools.plugin import sync_plugins_to_registry

    rows = await sync_plugins_to_registry(_ORG_ORAA74, async_session)
    persisted_ids = {r.descriptor.get("id") for r in rows}
    missing = _INGESTION_PLUGIN_IDS - persisted_ids
    assert not missing, (
        f"The following ingestion tools were not persisted to the registry: {missing!r}"
    )


# I-ALL-04  All 4 persisted rows carry kind=TOOL
@pytest.mark.integration
async def test_all_synced_ingestion_tools_have_kind_tool(async_session):
    """
    Every capability_descriptor row created by sync_plugins_to_registry() for
    the 4 ingestion tools must carry kind == DescriptorKind.TOOL.
    """
    import app.tools  # noqa: F401 — triggers auto-discovery of all shipped tool plugins
    from app.models.capability_descriptor import DescriptorKind
    from app.tools.plugin import sync_plugins_to_registry

    rows = await sync_plugins_to_registry(_ORG_ORAA74, async_session)
    ingestion_rows = [r for r in rows if r.descriptor.get("id") in _INGESTION_PLUGIN_IDS]
    assert len(ingestion_rows) == 4, f"Expected 4 ingestion tool rows; got {len(ingestion_rows)}"
    for row in ingestion_rows:
        assert row.kind == DescriptorKind.TOOL, (
            f"Row for '{row.descriptor.get('id')}' has kind={row.kind!r}; "
            "expected DescriptorKind.TOOL"
        )
