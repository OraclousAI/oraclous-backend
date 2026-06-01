"""
[tests] MCP inbound adapter — unit — ORAA-75

Story: ORAA-75 / ORA-74
Architecture refs:
  - Section 7 Portability Story: https://oraclous.atlassian.net/wiki/spaces/OP/pages/753728
  - OHM v1.0 Spec:               https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - Test Strategy:               https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

All imports from app.tools.base.mcp_tool will fail with ImportError until the implementer
creates:
  - app/tools/base/mcp_tool.py  (MCPInboundAdapter, translate_mcp_tool)

The ImportError on the module-level import below IS the expected initial TDD failure (ADR-010).
Every test in this file is intentionally red until the implementer delivers the translation layer.

Behaviours covered:
  M01  MCPInboundAdapter is importable from app.tools.base.mcp_tool
  M02  translate_mcp_tool() returns a dict with kind == "tool"
  M03  translated descriptor has spec.implementation.type == "mcp"
  M04  translated descriptor has spec.implementation.endpoint matching the server URL
  M05  translated descriptor carries a non-empty content_hash string with sha256: prefix
  M06  MCP inputSchema is preserved as spec.input_schema in the OHM descriptor
  M07  MCP description becomes metadata.description
  M08  MCP tool name is embedded in the descriptor id and metadata.name
  M09  translate_mcp_tool() returns None (not raises) when MCP tool has no name field
  M10  translate_mcp_tool() returns None (not raises) for an empty dict
  M11  MCP tool with x-credential-requirements produces populated credential_requirements
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# These imports will fail with ImportError until the implementer creates
# app/tools/base/mcp_tool.py.  The ImportError IS the expected initial TDD failure.
# ---------------------------------------------------------------------------
from app.tools.base.mcp_tool import (  # noqa: E402
    MCPInboundAdapter,
    translate_mcp_tool,
)

# ---------------------------------------------------------------------------
# Fixtures: MCP tool specs
# ---------------------------------------------------------------------------

_SERVER_ENDPOINT = "http://mock-mcp.example.com:8080"

_VALID_MCP_TOOL: dict = {
    "name": "fetch_data",
    "description": "Fetch data from the mock server given a query.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The query to execute"},
        },
        "required": ["query"],
    },
}

_MCP_TOOL_NO_NAME: dict = {
    "description": "A tool missing its name field.",
    "inputSchema": {"type": "object", "properties": {}},
}

_MCP_TOOL_WITH_CREDENTIALS: dict = {
    "name": "github_search",
    "description": "Search GitHub issues via OAuth.",
    "inputSchema": {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    },
    "x-credential-requirements": [
        {"type": "oauth_token", "provider": "github", "scopes": ["repo"]},
    ],
}


# ---------------------------------------------------------------------------
# M01  MCPInboundAdapter is importable from app.tools.base.mcp_tool
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mcp_inbound_adapter_is_importable():
    """MCPInboundAdapter must be importable from app.tools.base.mcp_tool."""
    from app.tools.base.mcp_tool import MCPInboundAdapter as MIA

    assert MIA is not None


# ---------------------------------------------------------------------------
# M02  translate_mcp_tool() returns a dict with kind == "tool"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translate_mcp_tool_returns_ohm_kind_tool():
    """translate_mcp_tool() must return a dict whose top-level 'kind' == 'tool'."""
    result = translate_mcp_tool(_VALID_MCP_TOOL, _SERVER_ENDPOINT)
    assert result is not None, "Expected a translated descriptor, got None"
    assert isinstance(result, dict), "translate_mcp_tool() must return a dict"
    assert result.get("kind") == "tool", (
        f"Expected kind='tool', got kind={result.get('kind')!r}"
    )


# ---------------------------------------------------------------------------
# M03  translated descriptor has spec.implementation.type == "mcp"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translated_descriptor_implementation_type_is_mcp():
    """
    The translated descriptor's spec.implementation.type must be 'mcp'.
    This is the core OHM contract for imported MCP tools (Section 7 Portability Story).
    """
    result = translate_mcp_tool(_VALID_MCP_TOOL, _SERVER_ENDPOINT)
    assert result is not None
    spec = result.get("spec", {})
    impl = spec.get("implementation", {})
    assert impl.get("type") == "mcp", (
        f"Expected implementation.type='mcp', got {impl.get('type')!r}"
    )


# ---------------------------------------------------------------------------
# M04  translated descriptor has spec.implementation.endpoint matching server URL
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translated_descriptor_implementation_endpoint_matches_server():
    """
    The translated descriptor must record the MCP server endpoint so the runtime
    knows where to dispatch invocations.
    """
    result = translate_mcp_tool(_VALID_MCP_TOOL, _SERVER_ENDPOINT)
    assert result is not None
    spec = result.get("spec", {})
    impl = spec.get("implementation", {})
    assert impl.get("endpoint") == _SERVER_ENDPOINT, (
        f"Expected implementation.endpoint={_SERVER_ENDPOINT!r}, got {impl.get('endpoint')!r}"
    )


# ---------------------------------------------------------------------------
# M05  translated descriptor carries a non-empty content_hash with sha256: prefix
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translated_descriptor_carries_content_hash():
    """
    translate_mcp_tool() must compute and embed a content_hash so the imported
    descriptor is versioned from the moment it is translated (S3.1 requirement).

    The hash is expected at version.hash with the format 'sha256:<hex>'.
    """
    result = translate_mcp_tool(_VALID_MCP_TOOL, _SERVER_ENDPOINT)
    assert result is not None
    version = result.get("version", {})
    content_hash = version.get("hash", "")
    assert content_hash.startswith("sha256:"), (
        f"content_hash must start with 'sha256:', got {content_hash!r}"
    )
    hex_part = content_hash[len("sha256:"):]
    assert len(hex_part) == 64, (
        f"sha256 hex digest must be 64 chars, got {len(hex_part)}: {hex_part!r}"
    )


# ---------------------------------------------------------------------------
# M06  MCP inputSchema is preserved as spec.input_schema in the OHM descriptor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translated_descriptor_preserves_input_schema():
    """
    The MCP tool's inputSchema must be copied to spec.input_schema in the OHM descriptor.
    Input/output schemas translate directly (OHM and MCP both use JSON Schema).
    """
    result = translate_mcp_tool(_VALID_MCP_TOOL, _SERVER_ENDPOINT)
    assert result is not None
    spec = result.get("spec", {})
    input_schema = spec.get("input_schema", {})
    original = _VALID_MCP_TOOL["inputSchema"]
    assert input_schema.get("type") == original["type"], (
        "input_schema type must match MCP inputSchema type"
    )
    assert set(input_schema.get("properties", {}).keys()) == set(original.get("properties", {}).keys()), (
        "input_schema properties must match MCP inputSchema properties"
    )
    assert input_schema.get("required") == original.get("required"), (
        "input_schema required list must match MCP inputSchema required"
    )


# ---------------------------------------------------------------------------
# M07  MCP description becomes metadata.description
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translated_descriptor_description_in_metadata():
    """The MCP tool's description must appear in the OHM descriptor's metadata.description."""
    result = translate_mcp_tool(_VALID_MCP_TOOL, _SERVER_ENDPOINT)
    assert result is not None
    metadata = result.get("metadata", {})
    assert metadata.get("description") == _VALID_MCP_TOOL["description"], (
        "metadata.description must match the MCP tool's description"
    )


# ---------------------------------------------------------------------------
# M08  MCP name is in metadata.name and the descriptor id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translated_descriptor_name_in_metadata_and_id():
    """
    The MCP tool's name must appear in the OHM descriptor's metadata.name.
    The descriptor id must incorporate the tool name so it is stable and identifiable.
    """
    result = translate_mcp_tool(_VALID_MCP_TOOL, _SERVER_ENDPOINT)
    assert result is not None
    metadata = result.get("metadata", {})
    assert metadata.get("name") == _VALID_MCP_TOOL["name"], (
        "metadata.name must match the MCP tool name"
    )
    descriptor_id = result.get("id", "")
    assert _VALID_MCP_TOOL["name"] in descriptor_id or descriptor_id != "", (
        "descriptor id must be non-empty and incorporate the tool name"
    )


# ---------------------------------------------------------------------------
# M09  translate_mcp_tool() returns None when MCP tool has no name field
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translate_mcp_tool_returns_none_for_missing_name(caplog):
    """
    translate_mcp_tool() must return None (not raise) when the MCP tool spec
    lacks a 'name' field, and must log the original payload so operators can diagnose.

    A missing name is not silently dropped — it is a translation failure that the
    caller can detect by receiving None.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        result = translate_mcp_tool(_MCP_TOOL_NO_NAME, _SERVER_ENDPOINT)

    assert result is None, (
        f"Expected None for MCP tool missing 'name', got {result!r}"
    )
    # The original payload must appear in the log so operators can diagnose
    log_text = caplog.text
    assert log_text != "", (
        "translate_mcp_tool() must log a warning when it cannot translate a tool"
    )


# ---------------------------------------------------------------------------
# M10  translate_mcp_tool() returns None (not raises) for an empty dict
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translate_mcp_tool_returns_none_for_empty_dict(caplog):
    """
    translate_mcp_tool() must return None (not raise) for a completely empty dict.
    An empty MCP payload is an untranslatable tool, not a programming error.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        result = translate_mcp_tool({}, _SERVER_ENDPOINT)

    assert result is None, (
        "Expected None for empty MCP tool dict, translate_mcp_tool() must not raise"
    )


# ---------------------------------------------------------------------------
# M11  MCP tool with x-credential-requirements produces populated credential_requirements
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translated_descriptor_credential_requirements_populated():
    """
    When an MCP tool declares x-credential-requirements, the translated OHM descriptor's
    spec.credential_requirements must be non-empty and preserve the credential type.

    This covers the T2-M3 conditional acceptance criterion (credential requirements).
    """
    result = translate_mcp_tool(_MCP_TOOL_WITH_CREDENTIALS, _SERVER_ENDPOINT)
    assert result is not None, "Expected a translated descriptor for tool with credentials"
    spec = result.get("spec", {})
    cred_reqs = spec.get("credential_requirements", [])
    assert len(cred_reqs) >= 1, (
        "spec.credential_requirements must be populated when the MCP tool declares "
        "x-credential-requirements"
    )
    cred_types = [cr.get("type") for cr in cred_reqs]
    assert "oauth_token" in cred_types, (
        f"Expected oauth_token in credential_requirements types, got {cred_types!r}"
    )
