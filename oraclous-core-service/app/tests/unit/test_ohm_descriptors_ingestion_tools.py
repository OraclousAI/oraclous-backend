"""
[tests] OHM descriptors for ingestion tools — unit — ORAA-74

Story: ORAA-74 / ORA-72
Architecture refs:
  - OHM v1.0 Spec:           https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - Section 7 Portability:   https://oraclous.atlassian.net/wiki/spaces/OP/pages/753728
  - Test Strategy:           https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

Written test-first (ADR-010). Tests that fail against the current codebase are the
intended TDD red state. The implementer must fix each tool's get_ohm_descriptor() to
use the correct OHM CredentialType values and declare scopes where required.

Tests currently skipping (before ORAA-74 implementation):
  All tests — app.tools.implementations module does not yet exist.
  Once ORAA-74 delivers the implementations, tests will run and validate:
  - GDR credential type must be 'oauth_token' (not legacy 'oauth2'), scopes required (T2-M3)
  - PG/MY credential type must be 'connection_string' (not 'database')

Behaviours covered:
  GDR-01  GoogleDriveReader descriptor has kind: "tool"
  GDR-02  GoogleDriveReader OHM descriptor validates as ToolDescriptor
  GDR-03  GoogleDriveReader credential_requirements uses type: "oauth_token"
  GDR-04  GoogleDriveReader oauth_token declares at least one scope (T2-M3)
  GDR-05  GoogleDriveReader oauth_token scopes include the drive.readonly URI
  GDR-06  GoogleDriveReader implementation.handler references the GoogleDriveReader class
  GDR-07  GoogleDriveReader implementation.type is "internal"
  GDR-08  GoogleDriveReader get_kind() returns DescriptorKind.TOOL
  GDR-09  GoogleDriveReader get_plugin_id() returns stable ID "google-drive-reader"
  GDR-10  GoogleDriveReader descriptor id matches get_plugin_id()

  NR-01   NotionReader descriptor has kind: "tool"
  NR-02   NotionReader OHM descriptor validates as ToolDescriptor
  NR-03   NotionReader credential_requirements uses type: "api_key"
  NR-04   NotionReader credential provider is "notion"
  NR-05   NotionReader implementation.handler references the NotionReader class
  NR-06   NotionReader implementation.type is "internal"
  NR-07   NotionReader get_kind() returns DescriptorKind.TOOL
  NR-08   NotionReader get_plugin_id() returns stable ID "notion-reader"
  NR-09   NotionReader descriptor id matches get_plugin_id()

  PG-01   PostgreSQLReader descriptor has kind: "tool"
  PG-02   PostgreSQLReader OHM descriptor validates as ToolDescriptor
  PG-03   PostgreSQLReader credential_requirements uses type: "connection_string"
  PG-04   PostgreSQLReader credential provider is "postgresql"
  PG-05   PostgreSQLReader implementation.handler references the PostgreSQLReader class
  PG-06   PostgreSQLReader implementation.type is "internal"
  PG-07   PostgreSQLReader get_kind() returns DescriptorKind.TOOL
  PG-08   PostgreSQLReader get_plugin_id() returns stable ID "postgresql-reader"
  PG-09   PostgreSQLReader descriptor id matches get_plugin_id()

  MY-01   MySQLReader descriptor has kind: "tool"
  MY-02   MySQLReader OHM descriptor validates as ToolDescriptor
  MY-03   MySQLReader credential_requirements uses type: "connection_string"
  MY-04   MySQLReader credential provider is "mysql"
  MY-05   MySQLReader implementation.handler references the MySQLReader class
  MY-06   MySQLReader implementation.type is "internal"
  MY-07   MySQLReader get_kind() returns DescriptorKind.TOOL
  MY-08   MySQLReader get_plugin_id() returns stable ID "mysql-reader"
  MY-09   MySQLReader descriptor id matches get_plugin_id()
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

try:
    from app.tools.implementations.ingestion.google_drive_reader import GoogleDriveReader
    from app.tools.implementations.ingestion.mysql_reader import MySQLReader
    from app.tools.implementations.ingestion.notion_reader import NotionReader
    from app.tools.implementations.ingestion.postgresql_reader import PostgreSQLReader
except ImportError:
    GoogleDriveReader = None  # type: ignore[assignment,misc]
    MySQLReader = None  # type: ignore[assignment,misc]
    NotionReader = None  # type: ignore[assignment,misc]
    PostgreSQLReader = None  # type: ignore[assignment,misc]


def _parse(data: dict):
    from ohm.schemas import CapabilityDescriptor

    return TypeAdapter(CapabilityDescriptor).validate_python(data)


# ===========================================================================
# Google Drive Reader
# ===========================================================================


# GDR-01  GoogleDriveReader descriptor has kind: "tool"
@pytest.mark.unit
@pytest.mark.skipif(GoogleDriveReader is None, reason="ORAA-74: implementations not yet available")
def test_google_drive_reader_descriptor_kind_is_tool():
    """GoogleDriveReader.get_ohm_descriptor() must return a dict with kind == 'tool'."""
    descriptor = GoogleDriveReader.get_ohm_descriptor()
    assert descriptor.get("kind") == "tool", (
        "GoogleDriveReader OHM descriptor must declare kind='tool'"
    )


# GDR-02  GoogleDriveReader OHM descriptor validates as ToolDescriptor
@pytest.mark.unit
@pytest.mark.skipif(GoogleDriveReader is None, reason="ORAA-74: implementations not yet available")
def test_google_drive_reader_descriptor_validates_as_tool_descriptor():
    """
    GoogleDriveReader.get_ohm_descriptor() must produce a dict that validates as a
    ToolDescriptor via the OHM TypeAdapter.

    FAILS until fixed: the current descriptor uses credential type 'oauth2', which is
    not a valid CredentialType enum value. The implementer must change it to 'oauth_token'
    and add a non-empty scopes list to satisfy T2-M3.
    """
    from ohm.schemas import ToolDescriptor

    descriptor = GoogleDriveReader.get_ohm_descriptor()
    result = _parse(descriptor)
    assert isinstance(result, ToolDescriptor), (
        "GoogleDriveReader OHM descriptor must parse as ToolDescriptor, not "
        f"{type(result).__name__}"
    )


# GDR-03  GoogleDriveReader credential type must be "oauth_token"
@pytest.mark.unit
@pytest.mark.skipif(GoogleDriveReader is None, reason="ORAA-74: implementations not yet available")
def test_google_drive_reader_credential_type_is_oauth_token():
    """
    GoogleDriveReader's credential_requirements must use type='oauth_token' (not 'oauth2').

    FAILS until fixed: the current descriptor uses 'oauth2', which is a legacy value.
    CredentialType.OAUTH_TOKEN serialises to 'oauth_token' in OHM v1.0.
    """
    descriptor = GoogleDriveReader.get_ohm_descriptor()
    creds = descriptor.get("spec", {}).get("credential_requirements", [])
    assert len(creds) >= 1, "GoogleDriveReader must declare at least one credential requirement"
    actual_type = creds[0].get("type")
    assert actual_type == "oauth_token", (
        f"Expected credential type 'oauth_token'; got {actual_type!r}. "
        "OHM CredentialType.OAUTH_TOKEN serialises as 'oauth_token'."
    )


# GDR-04  GoogleDriveReader oauth_token declares at least one scope (T2-M3)
@pytest.mark.unit
@pytest.mark.skipif(GoogleDriveReader is None, reason="ORAA-74: implementations not yet available")
def test_google_drive_reader_oauth_scopes_declared():
    """
    GoogleDriveReader's oauth_token credential_requirement must declare at least one scope.

    T2-M3: oauth_token credentials must explicitly declare scope. An empty or absent
    scopes list is rejected at OHM schema validation time (CredentialRequirement
    model_validator). The implementer must add a non-empty scopes list.

    FAILS until fixed: the current descriptor has no 'scopes' key.
    """
    descriptor = GoogleDriveReader.get_ohm_descriptor()
    creds = descriptor.get("spec", {}).get("credential_requirements", [])
    assert len(creds) >= 1
    scopes = creds[0].get("scopes")
    assert scopes is not None and len(scopes) >= 1, (
        f"GoogleDriveReader oauth_token must declare at least one scope (T2-M3). "
        f"Got scopes={scopes!r}"
    )


# GDR-05  GoogleDriveReader oauth_token scopes include the drive.readonly URI
@pytest.mark.unit
@pytest.mark.skipif(GoogleDriveReader is None, reason="ORAA-74: implementations not yet available")
def test_google_drive_reader_oauth_scopes_include_drive_readonly():
    """
    GoogleDriveReader's OAuth scopes must include 'https://www.googleapis.com/auth/drive.readonly'.

    The legacy ToolDefinition declares this as the required scope. The OHM descriptor
    must carry it forward unchanged. This is the minimum privilege scope for read-only
    Drive access.

    FAILS until fixed: the current descriptor declares no scopes.
    """
    descriptor = GoogleDriveReader.get_ohm_descriptor()
    creds = descriptor.get("spec", {}).get("credential_requirements", [])
    assert len(creds) >= 1
    scopes = creds[0].get("scopes", [])
    expected_scope = "https://www.googleapis.com/auth/drive.readonly"
    assert expected_scope in scopes, (
        f"GoogleDriveReader scopes must include '{expected_scope}'; got {scopes!r}"
    )


# GDR-06  GoogleDriveReader implementation.handler references the GoogleDriveReader class
@pytest.mark.unit
@pytest.mark.skipif(GoogleDriveReader is None, reason="ORAA-74: implementations not yet available")
def test_google_drive_reader_implementation_handler():
    """
    GoogleDriveReader OHM descriptor implementation.handler must reference the
    GoogleDriveReader class by its fully-qualified module path.

    The original executor class is preserved and untouched — the OHM wrapper points
    to it; it does not replace it.
    """
    descriptor = GoogleDriveReader.get_ohm_descriptor()
    handler = descriptor.get("spec", {}).get("implementation", {}).get("handler", "")
    assert "GoogleDriveReader" in handler, (
        f"implementation.handler must reference 'GoogleDriveReader'; got {handler!r}"
    )
    assert "app.tools.implementations.ingestion" in handler, (
        f"handler must include full module path; got {handler!r}"
    )


# GDR-07  GoogleDriveReader implementation.type is "internal"
@pytest.mark.unit
@pytest.mark.skipif(GoogleDriveReader is None, reason="ORAA-74: implementations not yet available")
def test_google_drive_reader_implementation_type_is_internal():
    """GoogleDriveReader OHM descriptor implementation.type must be 'internal'."""
    descriptor = GoogleDriveReader.get_ohm_descriptor()
    impl_type = descriptor.get("spec", {}).get("implementation", {}).get("type")
    assert impl_type == "internal", f"implementation.type must be 'internal'; got {impl_type!r}"


# GDR-08  GoogleDriveReader get_kind() returns DescriptorKind.TOOL
@pytest.mark.unit
@pytest.mark.skipif(GoogleDriveReader is None, reason="ORAA-74: implementations not yet available")
def test_google_drive_reader_get_kind_returns_tool():
    """GoogleDriveReader.get_kind() must return DescriptorKind.TOOL."""
    from app.models.capability_descriptor import DescriptorKind

    assert GoogleDriveReader.get_kind() == DescriptorKind.TOOL


# GDR-09  GoogleDriveReader get_plugin_id() returns "google-drive-reader"
@pytest.mark.unit
@pytest.mark.skipif(GoogleDriveReader is None, reason="ORAA-74: implementations not yet available")
def test_google_drive_reader_get_plugin_id():
    """GoogleDriveReader.get_plugin_id() must return stable ID 'google-drive-reader'."""
    assert GoogleDriveReader.get_plugin_id() == "google-drive-reader"


# GDR-10  GoogleDriveReader descriptor id matches get_plugin_id()
@pytest.mark.unit
@pytest.mark.skipif(GoogleDriveReader is None, reason="ORAA-74: implementations not yet available")
def test_google_drive_reader_descriptor_id_matches_plugin_id():
    """
    The 'id' field in GoogleDriveReader's OHM descriptor must match get_plugin_id().

    Consistency between the plugin registry ID and the descriptor ID is required for
    idempotent sync: sync_plugins_to_registry() uses the plugin_id to avoid duplicates.
    """
    descriptor = GoogleDriveReader.get_ohm_descriptor()
    assert descriptor.get("id") == GoogleDriveReader.get_plugin_id(), (
        f"descriptor['id'] must equal get_plugin_id(). "
        f"Got descriptor id={descriptor.get('id')!r}, "
        f"plugin_id={GoogleDriveReader.get_plugin_id()!r}"
    )


# ===========================================================================
# Notion Reader
# ===========================================================================


# NR-01  NotionReader descriptor has kind: "tool"
@pytest.mark.unit
@pytest.mark.skipif(NotionReader is None, reason="ORAA-74: implementations not yet available")
def test_notion_reader_descriptor_kind_is_tool():
    """NotionReader.get_ohm_descriptor() must return a dict with kind == 'tool'."""
    descriptor = NotionReader.get_ohm_descriptor()
    assert descriptor.get("kind") == "tool", "NotionReader OHM descriptor must declare kind='tool'"


# NR-02  NotionReader OHM descriptor validates as ToolDescriptor
@pytest.mark.unit
@pytest.mark.skipif(NotionReader is None, reason="ORAA-74: implementations not yet available")
def test_notion_reader_descriptor_validates_as_tool_descriptor():
    """
    NotionReader.get_ohm_descriptor() must produce a dict that validates as a
    ToolDescriptor via the OHM TypeAdapter.
    """
    from ohm.schemas import ToolDescriptor

    descriptor = NotionReader.get_ohm_descriptor()
    result = _parse(descriptor)
    assert isinstance(result, ToolDescriptor), (
        "NotionReader OHM descriptor must parse as ToolDescriptor"
    )


# NR-03  NotionReader credential_requirements uses type: "api_key"
@pytest.mark.unit
@pytest.mark.skipif(NotionReader is None, reason="ORAA-74: implementations not yet available")
def test_notion_reader_credential_type_is_api_key():
    """
    NotionReader's credential_requirements must use type='api_key'.

    Notion uses an Internal Integration Token (API key) for authentication.
    The OHM CredentialType.API_KEY serialises as 'api_key'.
    """
    descriptor = NotionReader.get_ohm_descriptor()
    creds = descriptor.get("spec", {}).get("credential_requirements", [])
    assert len(creds) >= 1, "NotionReader must declare at least one credential requirement"
    actual_type = creds[0].get("type")
    assert actual_type == "api_key", f"Expected credential type 'api_key'; got {actual_type!r}"


# NR-04  NotionReader credential provider is "notion"
@pytest.mark.unit
@pytest.mark.skipif(NotionReader is None, reason="ORAA-74: implementations not yet available")
def test_notion_reader_credential_provider_is_notion():
    """NotionReader's credential_requirement must declare provider='notion'."""
    descriptor = NotionReader.get_ohm_descriptor()
    creds = descriptor.get("spec", {}).get("credential_requirements", [])
    assert len(creds) >= 1
    provider = creds[0].get("provider")
    assert provider == "notion", f"Expected provider='notion'; got {provider!r}"


# NR-05  NotionReader implementation.handler references the NotionReader class
@pytest.mark.unit
@pytest.mark.skipif(NotionReader is None, reason="ORAA-74: implementations not yet available")
def test_notion_reader_implementation_handler():
    """
    NotionReader OHM descriptor implementation.handler must reference the
    NotionReader class by its fully-qualified module path.
    """
    descriptor = NotionReader.get_ohm_descriptor()
    handler = descriptor.get("spec", {}).get("implementation", {}).get("handler", "")
    assert "NotionReader" in handler, (
        f"implementation.handler must reference 'NotionReader'; got {handler!r}"
    )
    assert "app.tools.implementations.ingestion" in handler, (
        f"handler must include full module path; got {handler!r}"
    )


# NR-06  NotionReader implementation.type is "internal"
@pytest.mark.unit
@pytest.mark.skipif(NotionReader is None, reason="ORAA-74: implementations not yet available")
def test_notion_reader_implementation_type_is_internal():
    """NotionReader OHM descriptor implementation.type must be 'internal'."""
    descriptor = NotionReader.get_ohm_descriptor()
    impl_type = descriptor.get("spec", {}).get("implementation", {}).get("type")
    assert impl_type == "internal", f"implementation.type must be 'internal'; got {impl_type!r}"


# NR-07  NotionReader get_kind() returns DescriptorKind.TOOL
@pytest.mark.unit
@pytest.mark.skipif(NotionReader is None, reason="ORAA-74: implementations not yet available")
def test_notion_reader_get_kind_returns_tool():
    """NotionReader.get_kind() must return DescriptorKind.TOOL."""
    from app.models.capability_descriptor import DescriptorKind

    assert NotionReader.get_kind() == DescriptorKind.TOOL


# NR-08  NotionReader get_plugin_id() returns "notion-reader"
@pytest.mark.unit
@pytest.mark.skipif(NotionReader is None, reason="ORAA-74: implementations not yet available")
def test_notion_reader_get_plugin_id():
    """NotionReader.get_plugin_id() must return stable ID 'notion-reader'."""
    assert NotionReader.get_plugin_id() == "notion-reader"


# NR-09  NotionReader descriptor id matches get_plugin_id()
@pytest.mark.unit
@pytest.mark.skipif(NotionReader is None, reason="ORAA-74: implementations not yet available")
def test_notion_reader_descriptor_id_matches_plugin_id():
    """The 'id' field in NotionReader's OHM descriptor must match get_plugin_id()."""
    descriptor = NotionReader.get_ohm_descriptor()
    assert descriptor.get("id") == NotionReader.get_plugin_id(), (
        f"descriptor['id'] must equal get_plugin_id(). "
        f"Got descriptor id={descriptor.get('id')!r}, "
        f"plugin_id={NotionReader.get_plugin_id()!r}"
    )


# ===========================================================================
# PostgreSQL Reader
# ===========================================================================


# PG-01  PostgreSQLReader descriptor has kind: "tool"
@pytest.mark.unit
@pytest.mark.skipif(PostgreSQLReader is None, reason="ORAA-74: implementations not yet available")
def test_postgresql_reader_descriptor_kind_is_tool():
    """PostgreSQLReader.get_ohm_descriptor() must return a dict with kind == 'tool'."""
    descriptor = PostgreSQLReader.get_ohm_descriptor()
    assert descriptor.get("kind") == "tool", (
        "PostgreSQLReader OHM descriptor must declare kind='tool'"
    )


# PG-02  PostgreSQLReader OHM descriptor validates as ToolDescriptor
@pytest.mark.unit
@pytest.mark.skipif(PostgreSQLReader is None, reason="ORAA-74: implementations not yet available")
def test_postgresql_reader_descriptor_validates_as_tool_descriptor():
    """
    PostgreSQLReader.get_ohm_descriptor() must produce a dict that validates as a
    ToolDescriptor via the OHM TypeAdapter.

    FAILS until fixed: the current descriptor uses credential type 'database', which is
    not a valid CredentialType enum value. The implementer must change it to
    'connection_string' (CredentialType.CONNECTION_STRING).
    """
    from ohm.schemas import ToolDescriptor

    descriptor = PostgreSQLReader.get_ohm_descriptor()
    result = _parse(descriptor)
    assert isinstance(result, ToolDescriptor), (
        "PostgreSQLReader OHM descriptor must parse as ToolDescriptor"
    )


# PG-03  PostgreSQLReader credential_requirements uses type: "connection_string"
@pytest.mark.unit
@pytest.mark.skipif(PostgreSQLReader is None, reason="ORAA-74: implementations not yet available")
def test_postgresql_reader_credential_type_is_connection_string():
    """
    PostgreSQLReader's credential_requirements must use type='connection_string'.

    FAILS until fixed: the current descriptor uses 'database', which is not a valid
    OHM CredentialType. CredentialType.CONNECTION_STRING serialises as 'connection_string'.
    """
    descriptor = PostgreSQLReader.get_ohm_descriptor()
    creds = descriptor.get("spec", {}).get("credential_requirements", [])
    assert len(creds) >= 1, "PostgreSQLReader must declare at least one credential requirement"
    actual_type = creds[0].get("type")
    assert actual_type == "connection_string", (
        f"Expected credential type 'connection_string'; got {actual_type!r}. "
        "OHM CredentialType.CONNECTION_STRING serialises as 'connection_string'."
    )


# PG-04  PostgreSQLReader credential provider is "postgresql"
@pytest.mark.unit
@pytest.mark.skipif(PostgreSQLReader is None, reason="ORAA-74: implementations not yet available")
def test_postgresql_reader_credential_provider_is_postgresql():
    """PostgreSQLReader's credential_requirement must declare provider='postgresql'."""
    descriptor = PostgreSQLReader.get_ohm_descriptor()
    creds = descriptor.get("spec", {}).get("credential_requirements", [])
    assert len(creds) >= 1
    provider = creds[0].get("provider")
    assert provider == "postgresql", f"Expected provider='postgresql'; got {provider!r}"


# PG-05  PostgreSQLReader implementation.handler references the PostgreSQLReader class
@pytest.mark.unit
@pytest.mark.skipif(PostgreSQLReader is None, reason="ORAA-74: implementations not yet available")
def test_postgresql_reader_implementation_handler():
    """
    PostgreSQLReader OHM descriptor implementation.handler must reference the
    PostgreSQLReader class by its fully-qualified module path.
    """
    descriptor = PostgreSQLReader.get_ohm_descriptor()
    handler = descriptor.get("spec", {}).get("implementation", {}).get("handler", "")
    assert "PostgreSQLReader" in handler, (
        f"implementation.handler must reference 'PostgreSQLReader'; got {handler!r}"
    )
    assert "app.tools.implementations.ingestion" in handler, (
        f"handler must include full module path; got {handler!r}"
    )


# PG-06  PostgreSQLReader implementation.type is "internal"
@pytest.mark.unit
@pytest.mark.skipif(PostgreSQLReader is None, reason="ORAA-74: implementations not yet available")
def test_postgresql_reader_implementation_type_is_internal():
    """PostgreSQLReader OHM descriptor implementation.type must be 'internal'."""
    descriptor = PostgreSQLReader.get_ohm_descriptor()
    impl_type = descriptor.get("spec", {}).get("implementation", {}).get("type")
    assert impl_type == "internal", f"implementation.type must be 'internal'; got {impl_type!r}"


# PG-07  PostgreSQLReader get_kind() returns DescriptorKind.TOOL
@pytest.mark.unit
@pytest.mark.skipif(PostgreSQLReader is None, reason="ORAA-74: implementations not yet available")
def test_postgresql_reader_get_kind_returns_tool():
    """PostgreSQLReader.get_kind() must return DescriptorKind.TOOL."""
    from app.models.capability_descriptor import DescriptorKind

    assert PostgreSQLReader.get_kind() == DescriptorKind.TOOL


# PG-08  PostgreSQLReader get_plugin_id() returns "postgresql-reader"
@pytest.mark.unit
@pytest.mark.skipif(PostgreSQLReader is None, reason="ORAA-74: implementations not yet available")
def test_postgresql_reader_get_plugin_id():
    """PostgreSQLReader.get_plugin_id() must return stable ID 'postgresql-reader'."""
    assert PostgreSQLReader.get_plugin_id() == "postgresql-reader"


# PG-09  PostgreSQLReader descriptor id matches get_plugin_id()
@pytest.mark.unit
@pytest.mark.skipif(PostgreSQLReader is None, reason="ORAA-74: implementations not yet available")
def test_postgresql_reader_descriptor_id_matches_plugin_id():
    """The 'id' field in PostgreSQLReader's OHM descriptor must match get_plugin_id()."""
    descriptor = PostgreSQLReader.get_ohm_descriptor()
    assert descriptor.get("id") == PostgreSQLReader.get_plugin_id(), (
        f"descriptor['id'] must equal get_plugin_id(). "
        f"Got descriptor id={descriptor.get('id')!r}, "
        f"plugin_id={PostgreSQLReader.get_plugin_id()!r}"
    )


# ===========================================================================
# MySQL Reader
# ===========================================================================


# MY-01  MySQLReader descriptor has kind: "tool"
@pytest.mark.unit
@pytest.mark.skipif(MySQLReader is None, reason="ORAA-74: implementations not yet available")
def test_mysql_reader_descriptor_kind_is_tool():
    """MySQLReader.get_ohm_descriptor() must return a dict with kind == 'tool'."""
    descriptor = MySQLReader.get_ohm_descriptor()
    assert descriptor.get("kind") == "tool", "MySQLReader OHM descriptor must declare kind='tool'"


# MY-02  MySQLReader OHM descriptor validates as ToolDescriptor
@pytest.mark.unit
@pytest.mark.skipif(MySQLReader is None, reason="ORAA-74: implementations not yet available")
def test_mysql_reader_descriptor_validates_as_tool_descriptor():
    """
    MySQLReader.get_ohm_descriptor() must produce a dict that validates as a
    ToolDescriptor via the OHM TypeAdapter.

    FAILS until fixed: the current descriptor uses credential type 'database', which is
    not a valid CredentialType enum value. The implementer must change it to
    'connection_string' (CredentialType.CONNECTION_STRING).
    """
    from ohm.schemas import ToolDescriptor

    descriptor = MySQLReader.get_ohm_descriptor()
    result = _parse(descriptor)
    assert isinstance(result, ToolDescriptor), (
        "MySQLReader OHM descriptor must parse as ToolDescriptor"
    )


# MY-03  MySQLReader credential_requirements uses type: "connection_string"
@pytest.mark.unit
@pytest.mark.skipif(MySQLReader is None, reason="ORAA-74: implementations not yet available")
def test_mysql_reader_credential_type_is_connection_string():
    """
    MySQLReader's credential_requirements must use type='connection_string'.

    FAILS until fixed: the current descriptor uses 'database', which is not a valid
    OHM CredentialType. CredentialType.CONNECTION_STRING serialises as 'connection_string'.
    """
    descriptor = MySQLReader.get_ohm_descriptor()
    creds = descriptor.get("spec", {}).get("credential_requirements", [])
    assert len(creds) >= 1, "MySQLReader must declare at least one credential requirement"
    actual_type = creds[0].get("type")
    assert actual_type == "connection_string", (
        f"Expected credential type 'connection_string'; got {actual_type!r}. "
        "OHM CredentialType.CONNECTION_STRING serialises as 'connection_string'."
    )


# MY-04  MySQLReader credential provider is "mysql"
@pytest.mark.unit
@pytest.mark.skipif(MySQLReader is None, reason="ORAA-74: implementations not yet available")
def test_mysql_reader_credential_provider_is_mysql():
    """MySQLReader's credential_requirement must declare provider='mysql'."""
    descriptor = MySQLReader.get_ohm_descriptor()
    creds = descriptor.get("spec", {}).get("credential_requirements", [])
    assert len(creds) >= 1
    provider = creds[0].get("provider")
    assert provider == "mysql", f"Expected provider='mysql'; got {provider!r}"


# MY-05  MySQLReader implementation.handler references the MySQLReader class
@pytest.mark.unit
@pytest.mark.skipif(MySQLReader is None, reason="ORAA-74: implementations not yet available")
def test_mysql_reader_implementation_handler():
    """
    MySQLReader OHM descriptor implementation.handler must reference the
    MySQLReader class by its fully-qualified module path.
    """
    descriptor = MySQLReader.get_ohm_descriptor()
    handler = descriptor.get("spec", {}).get("implementation", {}).get("handler", "")
    assert "MySQLReader" in handler, (
        f"implementation.handler must reference 'MySQLReader'; got {handler!r}"
    )
    assert "app.tools.implementations.ingestion" in handler, (
        f"handler must include full module path; got {handler!r}"
    )


# MY-06  MySQLReader implementation.type is "internal"
@pytest.mark.unit
@pytest.mark.skipif(MySQLReader is None, reason="ORAA-74: implementations not yet available")
def test_mysql_reader_implementation_type_is_internal():
    """MySQLReader OHM descriptor implementation.type must be 'internal'."""
    descriptor = MySQLReader.get_ohm_descriptor()
    impl_type = descriptor.get("spec", {}).get("implementation", {}).get("type")
    assert impl_type == "internal", f"implementation.type must be 'internal'; got {impl_type!r}"


# MY-07  MySQLReader get_kind() returns DescriptorKind.TOOL
@pytest.mark.unit
@pytest.mark.skipif(MySQLReader is None, reason="ORAA-74: implementations not yet available")
def test_mysql_reader_get_kind_returns_tool():
    """MySQLReader.get_kind() must return DescriptorKind.TOOL."""
    from app.models.capability_descriptor import DescriptorKind

    assert MySQLReader.get_kind() == DescriptorKind.TOOL


# MY-08  MySQLReader get_plugin_id() returns "mysql-reader"
@pytest.mark.unit
@pytest.mark.skipif(MySQLReader is None, reason="ORAA-74: implementations not yet available")
def test_mysql_reader_get_plugin_id():
    """MySQLReader.get_plugin_id() must return stable ID 'mysql-reader'."""
    assert MySQLReader.get_plugin_id() == "mysql-reader"


# MY-09  MySQLReader descriptor id matches get_plugin_id()
@pytest.mark.unit
@pytest.mark.skipif(MySQLReader is None, reason="ORAA-74: implementations not yet available")
def test_mysql_reader_descriptor_id_matches_plugin_id():
    """The 'id' field in MySQLReader's OHM descriptor must match get_plugin_id()."""
    descriptor = MySQLReader.get_ohm_descriptor()
    assert descriptor.get("id") == MySQLReader.get_plugin_id(), (
        f"descriptor['id'] must equal get_plugin_id(). "
        f"Got descriptor id={descriptor.get('id')!r}, "
        f"plugin_id={MySQLReader.get_plugin_id()!r}"
    )
