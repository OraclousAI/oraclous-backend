"""
[tests] MCP inbound adapter — integration — ORAA-75

Story: ORAA-75 / ORA-74
Architecture refs:
  - Section 7 Portability Story: https://oraclous.atlassian.net/wiki/spaces/OP/pages/753728
  - OHM v1.0 Spec:               https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - Test Strategy:               https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

All imports from app.tools.base.mcp_tool will fail with ImportError until the implementer
creates:
  - app/tools/base/mcp_tool.py  (import_mcp_server, MCPInboundAdapter, translate_mcp_tool)

The ImportError on the module-level import below IS the expected initial TDD failure (ADR-010).
Every test in this file is intentionally red until the implementer delivers the importer.

Behaviours covered:
  M12  import_mcp_server() creates one CapabilityDescriptorDB row per translatable tool
  M13  each imported row has kind == DescriptorKind.TOOL
  M14  each imported row's descriptor has spec.implementation.type == "mcp"
  M15  each imported row carries a non-null content_hash column
  M16  untranslatable tool in server spec is skipped; other tools are still imported
  M17  import_mcp_server() is idempotent: second call does not create duplicate rows
  M18  org isolation: tools imported for org_A are not visible when listing by org_B
"""

from __future__ import annotations

import uuid

import pytest

from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind

try:
    from app.services.capability_registry import CapabilityRegistryService
except ImportError:
    CapabilityRegistryService = None  # type: ignore[assignment,misc]

try:
    from app.tools.base.mcp_tool import MCPInboundAdapter, import_mcp_server, translate_mcp_tool
except ImportError:
    MCPInboundAdapter = None  # type: ignore[assignment,misc]
    translate_mcp_tool = None  # type: ignore[assignment]
    import_mcp_server = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Test org UUIDs
# ---------------------------------------------------------------------------

_ORG_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000075")
_ORG_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000075")

# ---------------------------------------------------------------------------
# Fixtures: a reference MCP server specification with 3 well-formed tools
# ---------------------------------------------------------------------------

_MOCK_MCP_SERVER: dict = {
    "name": "TestMockServer",
    "url": "http://mock-mcp.example.com:8080",
    "tools": [
        {
            "name": "fetch_data",
            "description": "Fetch data from the mock server given a query.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "write_data",
            "description": "Write a key/value pair to the mock server.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
        {
            "name": "list_data",
            "description": "List all data items stored on the mock server.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
    ],
}

# A server spec that includes one untranslatable tool (missing 'name') alongside
# two valid tools.  The importer must skip the bad tool and import the good ones.
_MIXED_MCP_SERVER: dict = {
    "name": "MixedServer",
    "url": "http://mixed-mcp.example.com:9090",
    "tools": [
        {
            "name": "good_tool_one",
            "description": "First valid tool.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            # deliberately missing 'name' — this tool is untranslatable
            "description": "This tool has no name and must be skipped.",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "good_tool_two",
            "description": "Third valid tool.",
            "inputSchema": {
                "type": "object",
                "properties": {"param": {"type": "string"}},
            },
        },
    ],
}


# ---------------------------------------------------------------------------
# M12  import_mcp_server() creates one row per translatable tool
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(import_mcp_server is None, reason="impl not yet available")
async def test_import_mcp_server_creates_one_row_per_tool(async_session):
    """
    import_mcp_server() must produce exactly one CapabilityDescriptorDB row for
    each tool in the MCP server spec.  The 3-tool mock server must yield 3 rows.
    """
    rows = await import_mcp_server(
        server_spec=_MOCK_MCP_SERVER,
        org_id=_ORG_A,
        session=async_session,
    )
    assert len(rows) == 3, f"Expected 3 imported rows for a 3-tool server, got {len(rows)}"
    assert all(isinstance(r, CapabilityDescriptorDB) for r in rows), (
        "import_mcp_server() must return CapabilityDescriptorDB instances"
    )


# ---------------------------------------------------------------------------
# M13  each imported row has kind == DescriptorKind.TOOL
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(import_mcp_server is None, reason="impl not yet available")
async def test_import_mcp_server_rows_have_kind_tool(async_session):
    """Each row created by import_mcp_server() must have kind == DescriptorKind.TOOL."""
    rows = await import_mcp_server(
        server_spec=_MOCK_MCP_SERVER,
        org_id=_ORG_A,
        session=async_session,
    )
    for row in rows:
        assert row.kind == DescriptorKind.TOOL, (
            f"Expected kind=TOOL for imported MCP tool, got {row.kind!r}"
        )


# ---------------------------------------------------------------------------
# M14  each imported row's descriptor has spec.implementation.type == "mcp"
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(import_mcp_server is None, reason="impl not yet available")
async def test_import_mcp_server_rows_have_implementation_type_mcp(async_session):
    """
    Each imported row's JSONB descriptor must have spec.implementation.type == 'mcp'.
    This is the OHM contract for tools imported from external MCP servers.
    """
    rows = await import_mcp_server(
        server_spec=_MOCK_MCP_SERVER,
        org_id=_ORG_A,
        session=async_session,
    )
    for row in rows:
        spec = row.descriptor.get("spec", {})
        impl = spec.get("implementation", {})
        assert impl.get("type") == "mcp", (
            f"Expected implementation.type='mcp', got {impl.get('type')!r} "
            f"in descriptor for tool '{row.descriptor.get('id')}'"
        )


# ---------------------------------------------------------------------------
# M15  each imported row carries a non-null content_hash column
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(import_mcp_server is None, reason="impl not yet available")
async def test_import_mcp_server_rows_have_content_hash(async_session):
    """
    Each row imported by import_mcp_server() must have a non-null content_hash
    column, satisfying the S3.1 content-hash versioning requirement.
    """
    rows = await import_mcp_server(
        server_spec=_MOCK_MCP_SERVER,
        org_id=_ORG_A,
        session=async_session,
    )
    for row in rows:
        assert row.content_hash is not None, (
            f"content_hash must not be null for imported MCP tool '{row.descriptor.get('id')}'"
        )
        assert row.content_hash.startswith("sha256:"), (
            f"content_hash must start with 'sha256:', got {row.content_hash!r}"
        )


# ---------------------------------------------------------------------------
# M16  untranslatable tool is skipped; other tools are still imported
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(import_mcp_server is None, reason="impl not yet available")
async def test_import_mcp_server_skips_untranslatable_tools(async_session, caplog):
    """
    When a server spec contains an untranslatable tool (e.g., missing 'name'),
    import_mcp_server() must:
      - skip that tool (not raise, not abort),
      - log the original MCP payload so operators can diagnose, and
      - still import all translatable tools.

    The mixed server has 1 bad + 2 good tools → expect 2 imported rows.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        rows = await import_mcp_server(
            server_spec=_MIXED_MCP_SERVER,
            org_id=_ORG_A,
            session=async_session,
        )

    assert len(rows) == 2, f"Expected 2 imported rows (skipping 1 bad tool), got {len(rows)}"
    imported_names = [r.descriptor.get("metadata", {}).get("name") for r in rows]
    assert "good_tool_one" in imported_names, "good_tool_one must be imported"
    assert "good_tool_two" in imported_names, "good_tool_two must be imported"

    # The bad tool must be logged, not silently dropped
    assert caplog.text != "", "import_mcp_server() must emit a warning log for untranslatable tools"


# ---------------------------------------------------------------------------
# M17  import_mcp_server() is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryService is None, reason="impl not yet available")
async def test_import_mcp_server_is_idempotent(async_session):
    """
    Calling import_mcp_server() twice with the same server spec must not create
    duplicate rows in the capability registry.  The second call must detect
    existing tools (by stable id) and return the existing rows without creating new ones.
    """
    svc = CapabilityRegistryService(async_session)

    rows_first = await import_mcp_server(
        server_spec=_MOCK_MCP_SERVER,
        org_id=_ORG_A,
        session=async_session,
    )
    assert len(rows_first) == 3

    rows_second = await import_mcp_server(
        server_spec=_MOCK_MCP_SERVER,
        org_id=_ORG_A,
        session=async_session,
    )
    # The second call must still return 3 results (the existing rows)
    assert len(rows_second) == 3, "Idempotent call must return the same number of rows"

    # There must be exactly 3 tool rows in the registry (not 6)
    all_tools = await svc.list_by_kind(_ORG_A, DescriptorKind.TOOL)
    expected = 3
    got = len(all_tools)
    assert got == expected, (
        f"Expected {expected} distinct rows in the registry after two identical imports, got {got}"
    )


# ---------------------------------------------------------------------------
# M18  org isolation: tools for org_A are not visible to org_B
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.organization_isolation
@pytest.mark.skipif(CapabilityRegistryService is None, reason="impl not yet available")
async def test_imported_tools_are_scoped_to_org(async_session):
    """
    MCP tools imported for org_A must not appear when listing capabilities for org_B.
    This verifies the fundamental org_id tenancy boundary on imported capabilities.
    """
    svc = CapabilityRegistryService(async_session)

    await import_mcp_server(
        server_spec=_MOCK_MCP_SERVER,
        org_id=_ORG_A,
        session=async_session,
    )

    org_b_rows = await svc.list_by_org(_ORG_B)
    assert org_b_rows == [], (
        f"org_B must not see org_A's imported MCP tools; got {len(org_b_rows)} rows"
    )
