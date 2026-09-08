"""Unit (#951 T6): a first-party operation may describe its own arguments to the model.

The descriptor's flat ``parameters`` hint map carries a TYPE and nothing else — no sentence, no
``items`` for a list, no example. That is how ``provider`` reached a model as a bare unexplained
string named after the search vendor, which models then filled with website names until the run's
budget was gone (#946).

The fix #951 needs is that ``search`` declares a real JSON schema with a description per argument.
#698 built the pass-through for an MCP-imported server's own ``inputSchema``; this pins that the
same hop works for a FIRST-PARTY descriptor, because the whole of T6 rests on it. The two services
cannot import each other, so the descriptor half is asserted in the capability registry's own suite
and the hop is asserted here.

This file pins a hop that already works. It is the assumption T6 is built on, and an accidental
"first-party descriptors always use the hint map" change would silently strip every sentence back
out again, leaving the model exactly as uninformed as before — green, and useless.
"""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.tool_schemas import tool_specs_for

pytestmark = pytest.mark.unit

#: The shape `core/web-research`'s `search` operation takes in #951 — a first-party (non-MCP)
#: descriptor that declares its own model-facing schema instead of the flat hint map.
_SEARCH_SCHEMA = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "description": "What to search the web for."},
        "max_results": {"type": "integer", "description": "How many results to return."},
        "sites": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Website addresses to search within, e.g. theverge.com.",
        },
    },
}

_FIRST_PARTY_DESCRIPTOR = {
    "kind": "tool",
    "metadata": {"name": "Web Research"},
    "spec": {
        "type": "API",
        "capabilities": [
            {
                "name": "search",
                "description": "Search the live web and return ranked hits.",
                "parameters": {"query": "str", "max_results": "int", "sites": "list"},
                "parameters_schema": _SEARCH_SCHEMA,
            }
        ],
    },
}


def _search_spec() -> ToolSpec:
    return tool_specs_for("web-research", _FIRST_PARTY_DESCRIPTOR)[0]


def test_a_first_party_declared_schema_reaches_the_model_unchanged() -> None:
    assert _search_spec().parameters == _SEARCH_SCHEMA


def test_the_declared_schema_wins_over_the_hint_map_beside_it() -> None:
    """Both are present on the operation. The hint map would flatten ``sites`` to a bare array
    with no ``items`` and drop every description, so the schema must be the one the model gets."""
    params = _search_spec().parameters
    assert params["properties"]["sites"]["items"] == {"type": "string"}
    assert params["properties"]["sites"]["description"]
    assert params["required"] == ["query"]


def test_the_descriptions_survive_the_hop() -> None:
    """The point of T6: without these the model is handed the same bare unnamed strings #946 was
    about, one argument along."""
    properties = _search_spec().parameters["properties"]
    for name, prop in properties.items():
        assert prop.get("description", "").strip(), name


def test_dispatch_still_maps_back_to_the_operation() -> None:
    """Declaring a schema must not disturb how the loop routes the call back to the registry."""
    spec = _search_spec()
    assert spec.name == "web-research__search"
    assert spec.binding == "web-research"
    assert spec.operation == "search"
