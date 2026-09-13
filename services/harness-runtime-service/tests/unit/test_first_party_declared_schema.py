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


def test_a_declared_schema_with_no_explicit_strict_marker_stays_non_strict() -> None:
    """#898: strictness is carried EXPLICITLY, never inferred — this fixture's schema is a
    perfectly ordinary object schema (not even closed), so there is no shape to infer from either
    way, but the point holds regardless of shape (see the MCP counterpart in
    ``test_tool_schemas.py``, where the schema DOES look strict-shaped and still stays non-strict
    without the marker)."""
    assert _search_spec().strict is False


# ── #898: the explicit "parameters_schema_strict" carrier ─────────────────────────────────────────
#
# #900 (landing next) will inject a platform-AUTHORED schema onto a first-party descriptor, exactly
# the shape this file already exercises. Priority 1 in ``_parameters_for`` returns an op's
# ``parameters_schema`` verbatim regardless of whether the descriptor is first-party or MCP-imported
# (#698 D1 reuses the very same key for the remote server's own ``inputSchema``) — so the ONLY thing
# that can tell "ours, and meant to be strict" apart from "an untrusted server's contract" is an
# explicit marker on the op, never the schema's own shape. This is what stops #900's authored
# override from silently inheriting `strict` the moment it lands on an MCP descriptor.

_STRICT_SEARCH_SCHEMA = {
    "type": "object",
    "required": ["query"],
    "properties": {"query": {"type": "string", "description": "What to search the web for."}},
    "additionalProperties": False,
}

_FIRST_PARTY_DESCRIPTOR_WITH_STRICT_MARKER = {
    "kind": "tool",
    "metadata": {"name": "Web Research"},
    "spec": {
        "type": "API",
        "capabilities": [
            {
                "name": "search",
                "description": "Search the live web and return ranked hits.",
                "parameters": {"query": "str"},
                "parameters_schema": _STRICT_SEARCH_SCHEMA,
                "parameters_schema_strict": True,
            }
        ],
    },
}


def test_the_explicit_marker_makes_a_first_party_declared_override_strict() -> None:
    spec = tool_specs_for("web-research", _FIRST_PARTY_DESCRIPTOR_WITH_STRICT_MARKER)[0]
    assert spec.strict is True


def test_the_marker_does_not_disturb_the_declared_schema_itself() -> None:
    """The override still wins outright, unchanged (#951 T6) — marking it strict is metadata about
    it, never a rewrite of it."""
    spec = tool_specs_for("web-research", _FIRST_PARTY_DESCRIPTOR_WITH_STRICT_MARKER)[0]
    assert spec.parameters == _STRICT_SEARCH_SCHEMA
