"""Descriptor → ToolSpec marshalling (slice 1): one spec per operation, name + dispatch mapping."""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.tool_schemas import tool_specs_for

pytestmark = pytest.mark.unit

_DESCRIPTOR = {
    "id": "75304fdb-de39-56f2-acd6-915c87934a99",
    "metadata": {"name": "PostgreSQL Reader"},
    "spec": {
        "type": "DATABASE",
        "capabilities": [
            {"name": "list_tables", "description": "List the tables", "parameters": {}},
            {"name": "query", "description": "Run a query", "parameters": {"query": "str"}},
        ],
    },
}


def test_one_spec_per_operation() -> None:
    specs = tool_specs_for("pg", _DESCRIPTOR)
    names = {s.name for s in specs}
    assert names == {"pg__list_tables", "pg__query"}


def test_spec_carries_binding_and_operation_for_dispatch() -> None:
    specs = {s.name: s for s in tool_specs_for("pg", _DESCRIPTOR)}
    assert specs["pg__query"].binding == "pg"
    assert specs["pg__query"].operation == "query"


def test_parameters_become_json_schema() -> None:
    specs = {s.name: s for s in tool_specs_for("pg", _DESCRIPTOR)}
    params = specs["pg__query"].parameters
    assert params["type"] == "object"
    assert params["properties"]["query"]["type"] == "string"


def test_operations_without_a_name_are_skipped() -> None:
    descriptor = {"metadata": {"name": "X"}, "spec": {"capabilities": [{"description": "no name"}]}}
    assert tool_specs_for("x", descriptor) == []


# ── #698 D1: an MCP-imported operation carries the server's real ``inputSchema`` ──────────────────
#
# The importer discovers each tool's ``inputSchema`` from ``tools/list`` and stores it as the
# operation's ``parameters_schema``. That schema is NESTED (objects inside objects, enums, required
# lists) and the flat ``parameters`` hint map cannot express it — so it must reach the model
# UNCHANGED. Passing it through the hint-map path would flatten every nested property to
# ``{"type": "string"}`` and the model would guess arguments instead of having them shape-validated.

_MCP_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "owner": {"type": "string", "description": "Repository owner"},
        "repo": {"type": "string"},
        "pullNumber": {"type": "integer"},
        "method": {"type": "string", "enum": ["get", "diff", "status"]},
        "filters": {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
        },
    },
    "required": ["owner", "repo", "pullNumber"],
}

_MCP_DESCRIPTOR = {
    "kind": "tool",
    "metadata": {"name": "github-mcp-pull_request_read"},
    "spec": {
        "type": "mcp",
        "server_url": "https://api.githubcopilot.com/mcp/",
        "tool_name": "pull_request_read",
        "label": "github-mcp",
        "capabilities": [
            {
                "name": "pull_request_read",
                "description": "Read a pull request",
                "parameters_schema": _MCP_INPUT_SCHEMA,
            }
        ],
    },
}


def test_an_mcp_operation_reaches_the_model_with_its_schema_unchanged() -> None:
    """The nested ``inputSchema`` is passed through verbatim — nesting, enum and required intact."""
    specs = tool_specs_for("github-mcp", _MCP_DESCRIPTOR)
    assert len(specs) == 1
    assert specs[0].parameters == _MCP_INPUT_SCHEMA


def test_the_mcp_spec_keeps_the_binding_and_operation_for_dispatch() -> None:
    spec = tool_specs_for("github-mcp", _MCP_DESCRIPTOR)[0]
    assert spec.name == "github-mcp__pull_request_read"
    assert spec.binding == "github-mcp"
    assert spec.operation == "pull_request_read"


def test_a_builtin_descriptor_still_uses_the_hint_map_path() -> None:
    """D1 must not change a first-party descriptor: no ``parameters_schema`` → hint map, as now.

    #956 ruling 2 (test-author): the hint-map shape gains ``additionalProperties: False`` — the
    platform owns a first-party schema and closes it. The D1 point (hint map, not pass-through) is
    unchanged; the exact-equality pin is updated so it does not contradict ruling 2's tests below.
    """
    params = {s.name: s for s in tool_specs_for("pg", _DESCRIPTOR)}["pg__query"].parameters
    assert params == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": [],
        "additionalProperties": False,
    }


@pytest.mark.parametrize(
    "schema",
    [
        None,  # the server declared no inputSchema
        "not-a-dict",  # a hostile server sent a string
        [{"type": "object"}],  # a hostile server sent a list
        42,
    ],
)
def test_a_missing_or_hostile_input_schema_falls_back_without_raising(schema: object) -> None:
    """A hostile ``tools/list`` must never crash schema building — it degrades to an empty object
    schema so the tool stays callable with no declared arguments."""
    descriptor = {
        "kind": "tool",
        "metadata": {"name": "x-t"},
        "spec": {"type": "mcp", "capabilities": [{"name": "t", "parameters_schema": schema}]},
    }
    specs = tool_specs_for("x", descriptor)
    assert len(specs) == 1
    assert specs[0].parameters == {"type": "object", "properties": {}, "required": []}


def test_an_operation_with_neither_schema_nor_parameters_is_still_callable() -> None:
    descriptor = {
        "kind": "tool",
        "metadata": {"name": "x-t"},
        "spec": {"type": "mcp", "capabilities": [{"name": "t"}]},
    }
    assert tool_specs_for("x", descriptor)[0].parameters == {
        "type": "object",
        "properties": {},
        "required": [],
    }


# ── #698 D1: the LLM function-name limit ─────────────────────────────────────────────────────────
#
# The importer caps a discovered tool name at 255 characters, which is far longer than a provider
# will accept as a function name (64 chars, ``[A-Za-z0-9_-]`` only). ``<binding>__<operation>`` can
# therefore build a name the LLM adapter rejects, killing the whole run rather than one tool. The
# schema builder must sanitise and truncate DETERMINISTICALLY — the same descriptor must always
# produce the same function name, or a resumed run would dispatch to a name it never offered.

_LLM_NAME_MAX = 64
_LLM_NAME_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _long_name_descriptor(op_name: str) -> dict[str, object]:
    return {
        "kind": "tool",
        "metadata": {"name": "srv-t"},
        "spec": {"type": "mcp", "capabilities": [{"name": op_name}]},
    }


def test_an_overlong_function_name_is_truncated_to_the_provider_limit() -> None:
    spec = tool_specs_for("github-mcp", _long_name_descriptor("a" * 200))[0]
    assert len(spec.name) <= _LLM_NAME_MAX


@pytest.mark.parametrize("op_name", ["read pull request", "read/pull:request", "réad-pr", "a.b.c"])
def test_illegal_function_name_characters_are_sanitised(op_name: str) -> None:
    spec = tool_specs_for("gh", _long_name_descriptor(op_name))[0]
    assert set(spec.name) <= _LLM_NAME_ALLOWED, spec.name


def test_name_sanitisation_is_deterministic() -> None:
    """Same descriptor in, same function name out — a resumed run dispatches by the offered name."""
    first = tool_specs_for("gh", _long_name_descriptor("read pull request " * 10))[0].name
    second = tool_specs_for("gh", _long_name_descriptor("read pull request " * 10))[0].name
    assert first == second


def test_the_operation_stays_the_real_tool_name_after_sanitisation() -> None:
    """Only the LLM-facing ``name`` is sanitised. ``operation`` is what the registry dispatches and
    what the external server expects, so it must keep the server's own spelling."""
    spec = tool_specs_for("gh", _long_name_descriptor("read pull request"))[0]
    assert spec.operation == "read pull request"


# ── #946 T1: the model-facing schema for a first-party search operation ───────────────────────────
#
# The capability registry drops `provider` from the `search` operation's hint map, because that
# argument names the search VENDOR (an operator setting) and reached models as a bare unexplained
# string they filled with website names. This is the other end of that change: the schema built
# from such a descriptor must carry no `provider` property, because THIS is the surface a model
# actually reads. Asserting only on the registry's hint map leaves the hop untested.

_WEB_RESEARCH_DESCRIPTOR = {
    "id": "0f9a4d02-6c1a-5b7e-9a3d-2f6c8e1b4a70",
    "metadata": {"name": "Web Research"},
    "spec": {
        "type": "API",
        "capabilities": [
            {
                "name": "search",
                "description": "Search the live web and return ranked hits (BYOM api_key).",
                "parameters": {"query": "str", "max_results": "int"},
            },
            {"name": "read", "description": "Read a URL", "parameters": {"url": "str"}},
        ],
    },
}


def test_a_search_operation_offers_the_model_no_vendor_property() -> None:
    specs = {s.name: s for s in tool_specs_for("web-research", _WEB_RESEARCH_DESCRIPTOR)}
    properties = specs["web-research__search"].parameters["properties"]
    assert "provider" not in properties


def test_the_arguments_a_model_does_need_are_still_there() -> None:
    specs = {s.name: s for s in tool_specs_for("web-research", _WEB_RESEARCH_DESCRIPTOR)}
    properties = specs["web-research__search"].parameters["properties"]
    assert properties["query"]["type"] == "string"
    assert properties["max_results"]["type"] == "integer"


# ── #956 ruling 2: the model-facing schema refuses unknown keys at the boundary ─────────────────
#
# The dispatch line ``{"operation": spec.operation, **args}`` let a model-supplied ``operation``
# key pick the operation (#956). The runtime now strips it at dispatch (ruling 1); this is the
# OTHER half — the schema handed to the model says the key is not accepted at all, so a
# schema-honouring provider refuses it before the runtime ever sees it. Two lines of defence,
# one per ruling.
#
# Scope, deliberately: the platform owns a FIRST-PARTY operation's schema (it is built from the
# descriptor's hint map right here), so it is closed. An imported MCP operation's schema is the
# SERVER's own contract (#698 D1: verbatim) and is left as the server wrote it — an object schema
# with no ``additionalProperties`` is open by JSON-schema default, and closing it would refuse
# arguments a schema-less server tool legitimately takes. The mcp connector strips ``operation``
# itself (#698 D3), and the dispatch strip now covers it too; the reference page records the rule.
#
# #911 (the schema drops ``required``) is a SEPARATE issue and is not fixed here — these tests
# assert only ``additionalProperties``.


def test_a_first_party_operation_schema_refuses_unknown_keys() -> None:
    params = {s.name: s for s in tool_specs_for("pg", _DESCRIPTOR)}["pg__query"].parameters
    assert params["additionalProperties"] is False


def test_a_first_party_operation_with_no_parameters_is_closed_too() -> None:
    """``list_tables`` declares no parameters — the empty object is where an injected key would
    otherwise slip in unnoticed, because nothing else is there to compare it with."""
    params = {s.name: s for s in tool_specs_for("pg", _DESCRIPTOR)}["pg__list_tables"].parameters
    assert params["properties"] == {}
    assert params["additionalProperties"] is False


def test_the_first_party_schema_keeps_its_declared_properties_beside_the_closure() -> None:
    """Closing the schema adds one key; it must not disturb what was already there."""
    params = {s.name: s for s in tool_specs_for("pg", _DESCRIPTOR)}["pg__query"].parameters
    assert params["type"] == "object"
    assert params["properties"] == {"query": {"type": "string"}}


def test_an_mcp_schema_without_additional_properties_stays_the_servers_own() -> None:
    """The server declared an open object; the platform does not rewrite a third party's
    contract. (``test_an_mcp_operation_reaches_the_model_with_its_schema_unchanged`` already pins
    verbatim pass-through; this pins the one key #956 could have been tempted to add.)"""
    spec = tool_specs_for("github-mcp", _MCP_DESCRIPTOR)[0]
    assert "additionalProperties" not in _MCP_INPUT_SCHEMA  # the fixture is open on purpose
    assert "additionalProperties" not in spec.parameters


def test_an_mcp_schema_that_closes_itself_is_passed_through_closed() -> None:
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "additionalProperties": False,
    }
    descriptor = {
        "kind": "tool",
        "metadata": {"name": "x-t"},
        "spec": {"type": "mcp", "capabilities": [{"name": "t", "parameters_schema": schema}]},
    }
    assert tool_specs_for("x", descriptor)[0].parameters == schema
