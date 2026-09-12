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


# ── #911: a first-party operation's REAL declared schema (required/union types/enum/items) ──────
#
# Today a first-party (non-MCP) operation with no per-op ``parameters_schema`` is built ENTIRELY
# from the flat ``parameters`` hint map (``{name: "str"}`` → ``{"type": "string"}``) and
# ``required`` is hardcoded to ``[]`` in ``_json_schema``. The plugin class already declares the
# real shape on ``INPUT_SCHEMA``, which reaches this module as
# ``descriptor["spec"]["input_schema"]`` (confirmed against ``domain/plugins/builtin.py`` and
# ``domain/executors/base.py`` in the
# capability-registry-service — the same snake_case convention as ``spec["capabilities"]`` and
# ``spec["credential_requirements"]`` at this layer). None of that reaches the model today.
#
# The target: when ``spec.input_schema`` is a dict, project it onto each operation restricted to
# that operation's own ``parameters`` hint-map keys — carrying ``type``/``description``/``enum``/
# ``minLength``/``items``/etc. verbatim — and compute ``required`` as the declared list intersected
# with those same keys, minus the dispatching instance's ``bound_config`` keys, minus the literal
# key ``"operation"``. Everything below is RED against the current implementation, which knows
# nothing of ``spec.input_schema`` and always emits ``required: []``.
#
# ``tool_specs_for`` does not yet accept ``bound_config`` at all (as of `main`) — the calls that
# pass it are expected to raise ``TypeError`` today; that is an ordinary call to an EXISTING
# function with a new keyword, not a not-yet-built ``oraclous_*`` seam import, so it is not subject
# to the function-local-import rule and is allowed to hard-fail at module-collection-safe runtime.

_MANIFEST_VALIDATE_DESCRIPTOR = {
    "id": "core-manifest-validate",
    "metadata": {"name": "Manifest Validate"},
    "spec": {
        "type": "API",
        "capabilities": [
            {
                "name": "validate_manifest",
                "description": "Validate an OHM Team Harness draft",
                "parameters": {"draft": "object"},
            }
        ],
        "input_schema": {
            "type": "object",
            "required": ["draft"],
            "properties": {
                "draft": {
                    "type": ["object", "string"],
                    "description": (
                        "the drafted OHM Team Harness (a JSON object or the drafter's text)"
                    ),
                },
            },
        },
    },
}


def test_a_first_party_schema_projects_required_and_union_types() -> None:
    """The worked example: ``required`` and the ``draft`` property's union ``type`` and
    ``description`` must all reach the model, none of which the hint-map path can express."""
    spec = tool_specs_for("core-manifest-validate", _MANIFEST_VALIDATE_DESCRIPTOR)[0]
    params = spec.parameters
    assert params["required"] == ["draft"]
    assert params["properties"]["draft"]["type"] == ["object", "string"]
    assert params["properties"]["draft"]["description"].strip()


# ── cross-operation leakage guard ────────────────────────────────────────────────────────────────
#
# A plugin declares ONE ``INPUT_SCHEMA`` for the whole class (``PostgreSQLReaderPlugin`` shape:
# ``list_tables`` takes nothing, ``query`` takes ``query``/``params``). The projection must restrict
# by the OPERATION's own hint-map keys — handing every operation the whole plugin schema would leak
# ``query``/``params`` onto ``list_tables``, which takes no arguments at all.

_MULTI_OP_DESCRIPTOR = {
    "id": "core-postgres-reader",
    "metadata": {"name": "PostgreSQL Reader"},
    "spec": {
        "type": "DATABASE",
        "capabilities": [
            {"name": "list_tables", "description": "List the tables", "parameters": {}},
            {
                "name": "query",
                "description": "Run a query",
                "parameters": {"query": "string", "params": "object"},
            },
        ],
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "params": {"type": "object"},
            },
        },
    },
}


def test_an_operations_own_hint_map_keys_bound_the_projection_not_the_whole_plugin_schema() -> None:
    specs = {s.name: s for s in tool_specs_for("pg2", _MULTI_OP_DESCRIPTOR)}
    list_tables_props = specs["pg2__list_tables"].parameters["properties"]
    assert "query" not in list_tables_props
    assert "params" not in list_tables_props


def test_the_sibling_operation_still_gets_its_own_declared_properties() -> None:
    specs = {s.name: s for s in tool_specs_for("pg2", _MULTI_OP_DESCRIPTOR)}
    query_props = specs["pg2__query"].parameters["properties"]
    assert query_props["query"] == {"type": "string"}
    assert query_props["params"] == {"type": "object"}


# ── #956: ``operation`` is never advertised as required, or even present ────────────────────────
#
# ``dispatch_payload`` (#956) raises ``OperationOverrideRefused`` for a model-supplied ``operation``
# that disagrees with the bound one. Advertising ``operation`` as required (or even present) in the
# schema would invite the model to make exactly the call the runtime refuses. This must hold via
# BOTH mechanisms: ``operation`` is not in the op's own hint-map keys (rule 1's restriction already
# excludes it) AND it is explicitly subtracted from ``required`` even if it somehow were declared —
# so this test would catch a regression in either one alone.

_LIBRARY_GROUP_DESCRIPTOR = {
    "id": "core-library-group",
    "metadata": {"name": "Library Group"},
    "spec": {
        "type": "LIBRARY",
        "capabilities": [
            {
                "name": "word_count",
                "description": "Count words in text",
                "parameters": {"text": "string"},
            }
        ],
        "input_schema": {
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {"type": "string", "enum": ["word_count", "char_count"]},
                "text": {"type": "string"},
            },
        },
    },
}


def test_the_operation_key_is_never_advertised_as_required_or_present() -> None:
    spec = tool_specs_for("library-group", _LIBRARY_GROUP_DESCRIPTOR)[0]
    params = spec.parameters
    assert "operation" not in params["required"]
    assert "operation" not in params["properties"]


# ── config subtraction: a required key bound by the dispatching instance's config drops out of
# ``required`` but the property itself stays, so the model still sees the argument exists ─────────

_RECALL_MEMORY_DESCRIPTOR = {
    "id": "core-recall-memory",
    "metadata": {"name": "Recall Memory"},
    "spec": {
        "type": "MEMORY",
        "capabilities": [
            {
                "name": "recall_memory",
                "description": "Recall from a knowledge graph",
                "parameters": {"graph_id": "str", "query": "str"},
            }
        ],
        "input_schema": {
            "type": "object",
            "required": ["graph_id", "query"],
            "properties": {
                "graph_id": {"type": "string", "format": "uuid"},
                "query": {"type": "string", "minLength": 1},
            },
        },
    },
}


def test_a_bound_config_key_is_dropped_from_required_but_the_property_survives() -> None:
    """``graph_id`` is bound by the dispatching instance's config, so the model must not be asked
    to supply it — but it must still see the property so it understands what the tool does with
    it. ``bound_config`` does not exist on ``tool_specs_for`` yet, so this raises ``TypeError``
    today — that is the RED failure this test asserts (see the section note above)."""
    spec = tool_specs_for(
        "recall-memory",
        _RECALL_MEMORY_DESCRIPTOR,
        bound_config={"graph_id": "some-uuid"},
    )[0]
    params = spec.parameters
    assert "graph_id" not in params["required"]
    assert "graph_id" in params["properties"]


def test_an_unbound_required_key_stays_required_beside_the_dropped_one() -> None:
    """Same descriptor, same call: proves SUBTRACTION of exactly the bound key, not a blanket
    clearing of ``required`` — ``query`` is not in ``bound_config`` and must remain required."""
    spec = tool_specs_for(
        "recall-memory",
        _RECALL_MEMORY_DESCRIPTOR,
        bound_config={"graph_id": "some-uuid"},
    )[0]
    assert "query" in spec.parameters["required"]


# ── enum / minLength / items survive the projection verbatim ────────────────────────────────────

_RECALL_MEMORY_WITH_TYPE_DESCRIPTOR = {
    "id": "core-recall-memory-typed",
    "metadata": {"name": "Recall Memory"},
    "spec": {
        "type": "MEMORY",
        "capabilities": [
            {
                "name": "recall_memory",
                "description": "Recall from a knowledge graph",
                "parameters": {"query": "str", "type": "str"},
            }
        ],
        "input_schema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "type": {
                    "type": "string",
                    "enum": ["episodic", "semantic", "procedural"],
                },
            },
        },
    },
}

_WEB_RESEARCH_WITH_ITEMS_DESCRIPTOR = {
    "id": "core-web-research-sites",
    "metadata": {"name": "Web Research"},
    "spec": {
        "type": "API",
        "capabilities": [
            {
                "name": "search",
                "description": "Search the live web",
                "parameters": {"query": "str", "sites": "list"},
            }
        ],
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "sites": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Website addresses to search within, e.g. theverge.com.",
                },
            },
        },
    },
}


def test_minlength_and_enum_survive_the_projection_verbatim() -> None:
    spec = tool_specs_for("recall-memory-typed", _RECALL_MEMORY_WITH_TYPE_DESCRIPTOR)[0]
    props = spec.parameters["properties"]
    assert props["query"]["minLength"] == 1
    assert props["type"]["enum"] == ["episodic", "semantic", "procedural"]


def test_items_survives_the_projection_verbatim() -> None:
    spec = tool_specs_for("web-research-sites", _WEB_RESEARCH_WITH_ITEMS_DESCRIPTOR)[0]
    props = spec.parameters["properties"]
    assert props["sites"]["items"] == {"type": "string"}
    assert props["sites"]["description"]


# ── regression guards: what must NOT change ──────────────────────────────────────────────────────


def test_a_per_op_parameters_schema_still_wins_outright_over_the_plugin_level_projection() -> None:
    """An op-level ``parameters_schema`` already wins over the hint map (#951 T6, pinned in
    ``test_first_party_declared_schema.py``). It must also win completely over the NEW plugin-level
    ``spec.input_schema`` projection when both are present and disagree — the projection must not
    be attempted at all once an op declares its own schema."""
    op_schema = {
        "type": "object",
        "required": ["only_this_field"],
        "properties": {"only_this_field": {"type": "string"}},
    }
    descriptor = {
        "id": "core-conflicting-schemas",
        "metadata": {"name": "Conflicting Schemas"},
        "spec": {
            "type": "API",
            "capabilities": [
                {
                    "name": "op",
                    "description": "op",
                    "parameters": {"only_this_field": "str"},
                    "parameters_schema": op_schema,
                }
            ],
            "input_schema": {
                "type": "object",
                "required": ["something_else_entirely"],
                "properties": {"something_else_entirely": {"type": "boolean"}},
            },
        },
    }
    spec = tool_specs_for("conflict", descriptor)[0]
    assert spec.parameters == op_schema


def test_a_descriptor_with_no_input_schema_at_all_yields_todays_exact_fallback_dict() -> None:
    """No ``spec.input_schema`` key anywhere on the descriptor → the fallback is UNCONDITIONAL
    (rule 6), producing exactly today's dict. A fresh descriptor variable, distinct from the
    shared ``_DESCRIPTOR`` fixture used by
    ``test_a_builtin_descriptor_still_uses_the_hint_map_path`` above, so this holds
    unambiguously even if that fixture ever changes."""
    descriptor = {
        "id": "core-no-input-schema",
        "metadata": {"name": "No Input Schema"},
        "spec": {
            "type": "DATABASE",
            "capabilities": [
                {"name": "query", "description": "Run a query", "parameters": {"query": "str"}}
            ],
        },
    }
    params = tool_specs_for("no-schema", descriptor)[0].parameters
    assert params == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": [],
        "additionalProperties": False,
    }


@pytest.mark.parametrize(
    "hostile_input_schema",
    [
        "not-a-dict",
        ["also", "not", "a", "dict"],
        42,
    ],
)
def test_a_hostile_non_dict_input_schema_never_raises_and_falls_back(
    hostile_input_schema: object,
) -> None:
    """A malformed ``spec.input_schema`` (from a corrupted registry write, say) must degrade to
    the hint-map-only fallback rather than raising — the same fail-safe posture #698 already
    requires of a hostile MCP ``parameters_schema``."""
    descriptor = {
        "id": "core-hostile-input-schema",
        "metadata": {"name": "Hostile"},
        "spec": {
            "type": "DATABASE",
            "capabilities": [
                {"name": "query", "description": "Run a query", "parameters": {"query": "str"}}
            ],
            "input_schema": hostile_input_schema,
        },
    }
    params = tool_specs_for("hostile", descriptor)[0].parameters
    assert params == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": [],
        "additionalProperties": False,
    }


def test_a_hint_map_key_the_declared_schema_does_not_cover_keeps_its_hint_mapped_form() -> None:
    """The op's hint map may declare a key the plugin's ``input_schema.properties`` does not
    describe. That key must not silently vanish from the model's view — it keeps its current
    hint-mapped ``{"type": ...}`` shape."""
    descriptor = {
        "id": "core-partial-schema",
        "metadata": {"name": "Partial Schema"},
        "spec": {
            "type": "DATABASE",
            "capabilities": [
                {
                    "name": "query",
                    "description": "Run a query",
                    "parameters": {"query": "string", "timeout": "int"},
                }
            ],
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "the SQL"}},
                # "timeout" is deliberately undeclared here
            },
        },
    }
    params = tool_specs_for("partial", descriptor)[0].parameters
    assert params["properties"]["timeout"] == {"type": "integer"}
    assert params["properties"]["query"]["description"] == "the SQL"
