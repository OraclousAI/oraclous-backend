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
    """D1 must not change a first-party descriptor: no ``parameters_schema`` → hint map, as now."""
    params = {s.name: s for s in tool_specs_for("pg", _DESCRIPTOR)}["pg__query"].parameters
    assert params == {"type": "object", "properties": {"query": {"type": "string"}}, "required": []}


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
