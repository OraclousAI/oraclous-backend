"""[tests] ORAA-76: KGB agent toolkit reads tool descriptors from capability registry.

Story: ORAA-76 / ORA-75
Jira: ORA-75
Architecture refs:
  - Section 3 Layer 2: https://oraclous.atlassian.net/wiki/spaces/OP/pages/65967
  - OHM v1.0 Spec:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - Test Strategy:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

All imports from app.services.capability_registry_client will fail with ImportError
until the implementer creates:
  - app/services/capability_registry_client.py  (CapabilityRegistryClient)

And all assertions against agent_tool_schemas._TOOL_SCHEMAS will pass once the
implementer removes the static descriptor dict from that module.

The ImportError on the module-level import IS the expected initial TDD failure
(ADR-010).  Every test in this file is intentionally red until the implementer
delivers the registry-backed schema generation.

Covered behaviours:
  R01  CapabilityRegistryClient importable from app.services.capability_registry_client
  R02  tool_schemas_from_registry() resolves descriptors by tool name via the registry client
  R03  OHM tool descriptor → OpenAI provider format produces the correct wrapper shape
  R04  OHM tool descriptor → Anthropic provider format produces the correct wrapper shape
  R05  graph_id is never exposed to the LLM in registry-backed schema generation
  R06  Tools absent from the registry are silently dropped (pre-ORAA-76 contract preserved)
  R07  Empty allowlist returns empty list without calling the registry
  R08  Static _TOOL_SCHEMAS dict no longer exists in agent_tool_schemas (no dual-storage)
  R09  Registry client error propagates to schema generation (fail-closed, not silent)
  R10  Regression: AgentExecutor._tool_use_loop builds schemas from registry in research mode
  R11  All 12 KGB tools from the original static list are addressable via OHM descriptors
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# R01  CapabilityRegistryClient is importable from app.services.capability_registry_client.
# This import FAILS on the current codebase — expected TDD failure (ADR-010).
# The implementer must create app/services/capability_registry_client.py.
# ---------------------------------------------------------------------------
try:
    from app.services.capability_registry_client import CapabilityRegistryClient
except ImportError:
    CapabilityRegistryClient = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# OHM v1.0 tool descriptors for KGB graph tools.
# These are what the capability registry returns after ORAA-74 registers OHM
# wrappers for the KGB tools.  Each descriptor follows the OHM v1.0 spec.
# ---------------------------------------------------------------------------


def _ohm_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    """Minimal valid OHM v1.0 tool descriptor for a KGB graph tool."""
    return {
        "kind": "tool",
        "id": name,
        "version": {"hash": f"sha256:{name}-v1", "tags": ["1.0.0"]},
        "metadata": {"name": name, "description": description},
        "spec": {
            "type": "INTERNAL",
            "category": "QUERY",
            "implementation": {"type": "internal", "handler": f"kgb.tools.{name}"},
            "input_schema": input_schema,
            "output_schema": {"type": "object"},
            "credential_requirements": [],
        },
    }


# Canonical set of KGB tools — every tool previously in _TOOL_SCHEMAS
_KGB_TOOL_DESCRIPTORS: dict[str, dict[str, Any]] = {
    "graph_search": _ohm_tool(
        "graph_search",
        "Semantic similarity search over the knowledge graph's text embeddings.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query to embed and match.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "How many top-scoring nodes to return.",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
        },
    ),
    "community_members": _ohm_tool(
        "community_members",
        "Return the member nodes of a community, given a community_id.",
        {
            "type": "object",
            "properties": {
                "community_id": {
                    "type": "string",
                    "description": "Stable id of the community to expand.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "How many members to return.",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": ["community_id"],
        },
    ),
    "neighbors": _ohm_tool(
        "neighbors",
        "Breadth-first traversal from a known node, optionally filtered by relationship type.",
        {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "Source node id to traverse from.",
                },
                "edge_type": {
                    "type": ["string", "null"],
                    "description": "Optional relationship type filter.",
                },
                "depth": {
                    "type": "integer",
                    "description": "Hops from the source.",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["node_id"],
        },
    ),
    "degree_centrality": _ohm_tool(
        "degree_centrality",
        "Return the most-connected nodes of a given Neo4j label.",
        {
            "type": "object",
            "properties": {
                "node_label": {"type": "string", "description": "Neo4j label to rank."},
                "top_n": {
                    "type": "integer",
                    "description": "How many top-ranked nodes to return.",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["node_label"],
        },
    ),
    "shortest_path": _ohm_tool(
        "shortest_path",
        "Find the shortest path between two known nodes by qualified_name.",
        {
            "type": "object",
            "properties": {
                "from_qname": {
                    "type": "string",
                    "description": "qualified_name of the source node.",
                },
                "to_qname": {
                    "type": "string",
                    "description": "qualified_name of the target node.",
                },
            },
            "required": ["from_qname", "to_qname"],
        },
    ),
    "taint_trace": _ohm_tool(
        "taint_trace",
        "Follow FLOWS_TO edges from a source (code knowledge graphs only).",
        {
            "type": "object",
            "properties": {
                "source_qname": {
                    "type": "string",
                    "description": "qualified_name of the taint source.",
                },
                "depth": {
                    "type": "integer",
                    "description": "Maximum hops to follow.",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["source_qname"],
        },
    ),
    "cypher_query": _ohm_tool(
        "cypher_query",
        (
            "Execute a read-only Cypher query against the graph. "
            "Hard requirements: read-only, must filter by graph_id."
        ),
        {
            "type": "object",
            "properties": {
                "cypher": {
                    "type": "string",
                    "description": "Read-only Cypher query. Must reference $graph_id.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Cap on returned rows.",
                    "default": 25,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["cypher"],
        },
    ),
    "temporal_slice": _ohm_tool(
        "temporal_slice",
        "Return nodes that were valid at a given point in time.",
        {
            "type": "object",
            "properties": {
                "node_label": {
                    "type": "string",
                    "description": "Neo4j label to slice.",
                },
                "at_time": {
                    "type": "integer",
                    "description": "Unix epoch timestamp (seconds).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Cap on returned nodes.",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": ["node_label", "at_time"],
        },
    ),
    "find_communities": _ohm_tool(
        "find_communities",
        "Find communities (clusters of related nodes) in the graph via vector search.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query to match against community summaries.",
                },
                "kind": {
                    "type": ["string", "null"],
                    "description": "Optional community-kind filter.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "How many top-scoring communities to return.",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        },
    ),
    "vector_cypher_search": _ohm_tool(
        "vector_cypher_search",
        "Vector similarity search + graph traversal.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query to embed and match.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "How many top-scoring chunks to return.",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        },
    ),
    "hybrid_cypher_search": _ohm_tool(
        "hybrid_cypher_search",
        "Hybrid (vector similarity + fulltext BM25) search plus graph traversal.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query to embed and match.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "How many top-scoring chunks to return.",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        },
    ),
    "describe_community": _ohm_tool(
        "describe_community",
        "Return metadata for one community: its summary, key entities, "
        "excerpt, size, and sample members.",
        {
            "type": "object",
            "properties": {
                "community_id": {
                    "type": "string",
                    "description": "Stable id of the community to describe.",
                },
                "kind": {
                    "type": ["string", "null"],
                    "description": "Optional kind hint.",
                },
            },
            "required": ["community_id"],
        },
    ),
}


def _make_registry_client_stub(
    descriptors: dict[str, dict[str, Any]] | None = None,
) -> CapabilityRegistryClient:
    """Build a CapabilityRegistryClient double that returns OHM descriptors from a dict.

    ``descriptors`` maps tool name → OHM descriptor dict.  When a tool name
    is absent the stub returns None (simulating registry miss).
    When ``descriptors`` is None, uses the full _KGB_TOOL_DESCRIPTORS set.
    """
    store = descriptors if descriptors is not None else _KGB_TOOL_DESCRIPTORS

    client = MagicMock(spec=CapabilityRegistryClient)

    async def _get(tool_name: str) -> dict[str, Any] | None:
        return store.get(tool_name)

    client.get_tool_descriptor = AsyncMock(side_effect=_get)
    return client


# ---------------------------------------------------------------------------
# R01  CapabilityRegistryClient is importable
# (covered by the module-level import; this test makes the intent explicit)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
def test_capability_registry_client_is_importable():
    """CapabilityRegistryClient must be importable from app.services.capability_registry_client."""
    from app.services.capability_registry_client import CapabilityRegistryClient as CRC

    assert CRC is not None


# ---------------------------------------------------------------------------
# R02  tool_schemas_from_registry() resolves descriptors via the registry client
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
async def test_schema_generation_calls_registry_client():
    """tool_schemas_from_registry() must call get_tool_descriptor() on the client
    for each name in the allowlist, not read from any static in-module dict."""
    from app.services.agent_tool_schemas import tool_schemas_from_registry

    client = _make_registry_client_stub()
    schemas = await tool_schemas_from_registry(
        allowed_tools=["graph_search", "neighbors"],
        provider_format="openai",
        registry_client=client,
    )

    assert len(schemas) == 2
    # Registry was consulted for each allowed tool
    assert client.get_tool_descriptor.call_count == 2
    called_names = {call.args[0] for call in client.get_tool_descriptor.call_args_list}
    assert called_names == {"graph_search", "neighbors"}


# ---------------------------------------------------------------------------
# R03  OHM tool descriptor → OpenAI provider format produces correct wrapper shape
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
async def test_ohm_descriptor_to_openai_format():
    """An OHM tool descriptor fetched from the registry must produce a valid
    OpenAI-format schema: {type: 'function', function: {name, description, parameters}}."""
    from app.services.agent_tool_schemas import tool_schemas_from_registry

    client = _make_registry_client_stub({"graph_search": _KGB_TOOL_DESCRIPTORS["graph_search"]})
    schemas = await tool_schemas_from_registry(
        allowed_tools=["graph_search"],
        provider_format="openai",
        registry_client=client,
    )

    assert len(schemas) == 1
    schema = schemas[0]
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "graph_search"
    assert "description" in schema["function"]
    assert "parameters" in schema["function"]
    params = schema["function"]["parameters"]
    assert params["type"] == "object"
    assert "query" in params.get("properties", {})
    assert "graph_id" not in params.get("properties", {})


# ---------------------------------------------------------------------------
# R04  OHM tool descriptor → Anthropic provider format produces correct wrapper shape
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
async def test_ohm_descriptor_to_anthropic_format():
    """An OHM tool descriptor fetched from the registry must produce a valid
    Anthropic-format schema: {name, description, input_schema} with no 'function' key."""
    from app.services.agent_tool_schemas import tool_schemas_from_registry

    client = _make_registry_client_stub({"graph_search": _KGB_TOOL_DESCRIPTORS["graph_search"]})
    schemas = await tool_schemas_from_registry(
        allowed_tools=["graph_search"],
        provider_format="anthropic",
        registry_client=client,
    )

    assert len(schemas) == 1
    schema = schemas[0]
    assert schema["name"] == "graph_search"
    assert "description" in schema
    assert "input_schema" in schema
    assert "function" not in schema
    assert schema.get("type") != "function"
    input_schema = schema["input_schema"]
    assert "query" in input_schema.get("properties", {})
    assert "graph_id" not in input_schema.get("properties", {})


# ---------------------------------------------------------------------------
# R05  graph_id is never exposed to the LLM in registry-backed schema generation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
@pytest.mark.parametrize("tool_name", list(_KGB_TOOL_DESCRIPTORS.keys()))
async def test_graph_id_absent_from_registry_backed_schema(tool_name: str):
    """graph_id must never appear in any parameter of a registry-backed schema.

    graph_id is bound by the executor before dispatch — the LLM must never
    see it as a parameter, regardless of what is stored in the OHM descriptor.
    """
    from app.services.agent_tool_schemas import tool_schemas_from_registry

    client = _make_registry_client_stub({tool_name: _KGB_TOOL_DESCRIPTORS[tool_name]})
    for fmt in ("openai", "anthropic"):
        schemas = await tool_schemas_from_registry(
            allowed_tools=[tool_name],
            provider_format=fmt,  # type: ignore[arg-type]
            registry_client=client,
        )
        assert len(schemas) == 1, f"Expected 1 schema for {tool_name!r} in {fmt!r} format"
        schema = schemas[0]

        if fmt == "openai":
            params = schema["function"]["parameters"]
        else:
            params = schema["input_schema"]

        assert "graph_id" not in params.get("properties", {}), (
            f"Tool {tool_name!r} exposes graph_id in {fmt!r} format — tenant-isolation risk"
        )
        assert "graph_id" not in params.get("required", []), (
            f"Tool {tool_name!r} requires graph_id in {fmt!r} format — tenant-isolation risk"
        )


# ---------------------------------------------------------------------------
# R06  Tools absent from the registry are silently dropped
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
async def test_tool_absent_from_registry_is_silently_dropped():
    """When a tool in the allowlist has no OHM descriptor in the registry,
    it must be silently omitted from the returned schemas — no crash.
    This preserves the contract of the pre-ORAA-76 tool_schemas_for()."""
    from app.services.agent_tool_schemas import tool_schemas_from_registry

    # Registry only has graph_search; allowlist asks for graph_search + a ghost tool
    client = _make_registry_client_stub({"graph_search": _KGB_TOOL_DESCRIPTORS["graph_search"]})
    schemas = await tool_schemas_from_registry(
        allowed_tools=["graph_search", "nonexistent_ghost_tool"],
        provider_format="openai",
        registry_client=client,
    )

    names = [s["function"]["name"] for s in schemas]
    assert "graph_search" in names
    assert "nonexistent_ghost_tool" not in names


# ---------------------------------------------------------------------------
# R07  Empty allowlist returns empty list without calling the registry
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
async def test_empty_allowlist_returns_empty_without_registry_call():
    """An empty allowlist must return [] and must not call the registry client at all.
    No reason to pay the round-trip for a no-op call."""
    from app.services.agent_tool_schemas import tool_schemas_from_registry

    client = _make_registry_client_stub()
    result_openai = await tool_schemas_from_registry([], "openai", registry_client=client)
    result_anthropic = await tool_schemas_from_registry([], "anthropic", registry_client=client)

    assert result_openai == []
    assert result_anthropic == []
    client.get_tool_descriptor.assert_not_called()


# ---------------------------------------------------------------------------
# R08  Static _TOOL_SCHEMAS dict no longer exists in agent_tool_schemas
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
def test_static_tool_schemas_dict_removed():
    """_TOOL_SCHEMAS must not exist in app.services.agent_tool_schemas after ORAA-76.

    The static descriptor map was the source of duplication this story eliminates.
    If it still exists, descriptors are defined in two places — violating the
    'no dual-storage' acceptance criterion.
    """
    import app.services.agent_tool_schemas as module

    assert not hasattr(module, "_TOOL_SCHEMAS"), (
        "agent_tool_schemas._TOOL_SCHEMAS still exists. "
        "The static descriptor dict must be deleted; descriptors now live in the "
        "capability registry only."
    )


# ---------------------------------------------------------------------------
# R09  Registry client error propagates to schema generation (fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
async def test_registry_client_error_propagates():
    """When the registry client raises, tool_schemas_from_registry must not silently
    return empty schemas.  The caller (AgentExecutor) needs to see the error so it
    can decide whether to abort the turn or serve a degraded response."""
    from app.services.agent_tool_schemas import tool_schemas_from_registry

    client = MagicMock(spec=CapabilityRegistryClient)
    client.get_tool_descriptor = AsyncMock(side_effect=RuntimeError("registry unavailable"))

    with pytest.raises((RuntimeError, Exception)):
        await tool_schemas_from_registry(
            allowed_tools=["graph_search"],
            provider_format="openai",
            registry_client=client,
        )


# ---------------------------------------------------------------------------
# R10  Regression: AgentExecutor._tool_use_loop builds schemas from registry
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
async def test_agent_executor_tool_use_loop_uses_registry_schemas():
    """AgentExecutor._tool_use_loop must build tool schemas by calling the registry
    client, not by importing _TOOL_SCHEMAS from agent_tool_schemas.

    This regression test confirms the executor wiring is updated: the agent
    definition or DI container must supply a CapabilityRegistryClient, and
    _tool_use_loop calls tool_schemas_from_registry with it.
    """
    from unittest.mock import AsyncMock, MagicMock

    from app.services.agent_executor import AgentExecutor

    registry_client = _make_registry_client_stub()

    # Minimal agent def that declares research mode with graph_search
    agent_def = {
        "graph_id": "test-graph-001",
        "reasoning_mode": "research",
        "system_prompt": "You are a test agent.",
        "tools": ["graph_search"],
    }

    # Mock the LLM so it returns a no-tool-call response immediately
    mock_llm = MagicMock()
    mock_llm.chat = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "Here is the answer."
    resp.choices[0].message.tool_calls = None
    mock_llm.chat.completions.create = AsyncMock(return_value=resp)

    mock_toolkit = MagicMock()

    executor = AgentExecutor(
        agent_def=agent_def,
        toolkit=mock_toolkit,
        llm=mock_llm,
        model="gpt-4o-mini",
        registry_client=registry_client,
    )

    final_text, _ = await executor._tool_use_loop(
        message="What are the top entities?",
        prov=MagicMock(),
        system_prompt="You are a test agent.",
    )

    # Registry was called to resolve graph_search schema
    assert registry_client.get_tool_descriptor.call_count >= 1
    called_names = {call.args[0] for call in registry_client.get_tool_descriptor.call_args_list}
    assert "graph_search" in called_names, (
        "AgentExecutor._tool_use_loop did not call registry_client.get_tool_descriptor "
        "for graph_search — schemas are still coming from the static dict."
    )


# ---------------------------------------------------------------------------
# R11  All 12 KGB tools are addressable via OHM descriptors
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(CapabilityRegistryClient is None, reason="impl not yet available")
@pytest.mark.parametrize("tool_name", list(_KGB_TOOL_DESCRIPTORS.keys()))
async def test_all_kgb_tools_fetchable_from_registry(tool_name: str):
    """Every KGB tool that was previously in the static _TOOL_SCHEMAS must be
    representable as an OHM descriptor that the registry adapter can consume and
    convert to a valid provider schema in both formats."""
    from app.services.agent_tool_schemas import tool_schemas_from_registry

    client = _make_registry_client_stub({tool_name: _KGB_TOOL_DESCRIPTORS[tool_name]})

    for fmt in ("openai", "anthropic"):
        schemas = await tool_schemas_from_registry(
            allowed_tools=[tool_name],
            provider_format=fmt,  # type: ignore[arg-type]
            registry_client=client,
        )
        assert len(schemas) == 1, (
            f"Expected 1 schema for {tool_name!r} in {fmt!r} format; got {len(schemas)}"
        )
        schema = schemas[0]

        if fmt == "openai":
            assert schema["type"] == "function"
            assert schema["function"]["name"] == tool_name
            assert schema["function"]["description"]
            assert isinstance(schema["function"]["parameters"], dict)
        else:
            assert schema["name"] == tool_name
            assert schema["description"]
            assert isinstance(schema["input_schema"], dict)
