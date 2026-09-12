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


# ── same guarantee, but where rule 1's restriction alone cannot be the reason it holds ───────────
#
# No SHIPPED operation's own hint map actually contains "operation" (the delivery sink's `deliver`
# declares `{"repo","base_branch","head_branch","files"}`; the library groups generate their hint
# maps from the Python function's own argument names, which never include the dispatch key) — so
# the test above only ever exercises rule 1 (operation is excluded because it isn't one of the
# op's own hint-map keys to begin with). It would still pass if the explicit "minus operation"
# subtraction were never implemented at all. This fixture manufactures the case rule 1 alone
# cannot catch: an operation whose OWN `parameters` hint map — and whose declared
# `input_schema.required` — both name "operation". Only the explicit subtraction keeps it out
# here. Defence in depth against a FUTURE descriptor shaped this way, not a guard against current
# data — do not delete this as "redundant" with the test above.

_LIBRARY_GROUP_WITH_OPERATION_HINT_DESCRIPTOR = {
    "id": "core-library-group-operation-hint",
    "metadata": {"name": "Library Group (hypothetical operation-in-hint-map shape)"},
    "spec": {
        "type": "LIBRARY",
        "capabilities": [
            {
                "name": "word_count",
                "description": "Count words in text",
                "parameters": {"operation": "string", "text": "string"},
            }
        ],
        "input_schema": {
            "type": "object",
            "required": ["operation", "text"],
            "properties": {
                "operation": {"type": "string", "enum": ["word_count", "char_count"]},
                "text": {"type": "string"},
            },
        },
    },
}


def test_operation_is_subtracted_even_when_its_own_hint_map_declares_it() -> None:
    spec = tool_specs_for("library-group-2", _LIBRARY_GROUP_WITH_OPERATION_HINT_DESCRIPTOR)[0]
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


# ── #542: a second, differently-sourced bound-key case (the delivery sink's ``repo``) ────────────
#
# ``graph_id`` above is bound by the HARNESS at run time (``_materialise``'s fresh-mint branch
# merges it into ``cap_config``). ``repo`` is bound a different way: an OPERATOR configures the
# tool instance with it once, before any run — the "configured, not passed" shape (#542). From
# ``tool_specs_for``'s point of view both arrive identically, as a key of ``bound_config`` — but
# #911 names both explicitly, and they differ in KIND (harness-bound vs operator-bound), so both
# get their own fixture rather than treating one as redundant with the other.

_GITHUB_SINK_DESCRIPTOR = {
    "id": "core-github-sink",
    "metadata": {"name": "GitHub Sink"},
    "spec": {
        "type": "API",
        "capabilities": [
            {
                "name": "deliver",
                "description": "Write changed files to a head branch + open a PR",
                "parameters": {
                    "repo": "str",
                    "base_branch": "str",
                    "head_branch": "str",
                    "files": "list",
                },
            }
        ],
        "input_schema": {
            "type": "object",
            "required": ["repo", "files"],
            "properties": {
                "repo": {"type": "string"},
                "base_branch": {"type": "string"},
                "head_branch": {"type": "string"},
                "files": {"type": "array", "items": {"type": "object"}},
            },
        },
    },
}


def test_an_operator_configured_key_is_dropped_from_required_too() -> None:
    """``repo`` is bound once, by an operator configuring the tool instance — never supplied by the
    harness at run time the way ``graph_id`` is — but it reaches ``tool_specs_for`` the same way,
    as a ``bound_config`` key, and must be dropped from ``required`` the same way."""
    spec = tool_specs_for(
        "github-sink",
        _GITHUB_SINK_DESCRIPTOR,
        bound_config={"repo": "octocat/example"},
    )[0]
    params = spec.parameters
    assert "repo" not in params["required"]
    assert "repo" in params["properties"]
    assert "files" in params["required"]  # unbound, stays required — subtraction not blanket


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


def test_bound_config_does_not_alter_an_mcp_schema_passthrough() -> None:
    """MCP passthrough is untouched by #911 (AC3) — a ``bound_config`` naming a key that happens to
    also appear in the server's declared ``inputSchema`` must not strip it out of ``required``. The
    projection/subtraction only ever applies to a FIRST-PARTY (non-MCP) operation; an imported
    server's schema is its own contract (#698 D1) and this must hold even when the caller passes a
    ``bound_config`` that would matter for a first-party operation.

    RED today for the same reason as every other ``bound_config=`` call in this file:
    ``tool_specs_for`` does not accept the keyword yet, so this raises ``TypeError`` — it is not
    yet passing "by luck," it cannot run at all until the kwarg exists, and it must still hold once
    it does."""
    spec = tool_specs_for(
        "github-mcp",
        _MCP_DESCRIPTOR,
        bound_config={"owner": "should-not-matter"},
    )[0]
    assert spec.parameters == _MCP_INPUT_SCHEMA


def test_bound_config_does_not_alter_a_per_op_parameters_schema_override() -> None:
    """A declared per-operation ``parameters_schema`` wins outright and is returned unchanged
    (test below). This pins that a ``bound_config`` naming one of ITS required keys does not leak
    a subtraction into that override either — the override is a promise to hand the model exactly
    what it declares, untouched by config-binding logic altogether.

    RED today: ``tool_specs_for`` does not accept ``bound_config`` yet, so this raises
    ``TypeError`` — same posture as every other ``bound_config=`` test in this file."""
    op_schema = {
        "type": "object",
        "required": ["only_this_field"],
        "properties": {"only_this_field": {"type": "string"}},
    }
    descriptor = {
        "id": "core-conflicting-schemas-bound-config",
        "metadata": {"name": "Conflicting Schemas (bound_config)"},
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
        },
    }
    spec = tool_specs_for(
        "conflict-bound-config",
        descriptor,
        bound_config={"only_this_field": "should-not-matter"},
    )[0]
    assert spec.parameters == op_schema


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


# ── a malformed ELEMENT inside an otherwise-valid declared schema must never raise ───────────────
#
# The hostile-schema tests above cover a malformed TOP-LEVEL `input_schema` (a string, a list, an
# int). `_project_input_schema` checks that `required` IS a list, but never checks what's IN it: an
# element that is itself a list or a dict is unhashable, so `k in op_key_set` raises `TypeError` —
# and nothing catches it (`tool_specs_for` doesn't, and its caller in
# `harness_execution_service.py` catches only the registry's own error type). A malformed declared
# schema then kills tool-schema building for the WHOLE run, before the model is ever built —
# violating the ruled contract the implementation's own docstring restates: a descriptor is data,
# validation may only reject what it positively understands to be wrong, nothing may raise on a
# hostile value.

_REQUIRED_LIST_WITH_UNHASHABLE_ELEMENTS_DESCRIPTOR = {
    "id": "core-required-list-hostile-elements",
    "metadata": {"name": "Hostile Required Elements"},
    "spec": {
        "type": "API",
        "capabilities": [{"name": "op", "description": "op", "parameters": {"q": "str"}}],
        "input_schema": {
            "type": "object",
            # a nested list, a dict, and an int alongside one genuinely valid string key — the
            # non-string entries must be silently ignored, matching how the rest of this code
            # treats data it does not understand.
            "required": ["q", ["nested"], {"a": 1}, 123],
            "properties": {"q": {"type": "string"}},
        },
    },
}


def test_a_required_list_with_unhashable_elements_never_raises() -> None:
    """RED against the current implementation: ``k in op_key_set`` hashes every declared
    ``required`` element unconditionally, so a nested list or dict in that list raises
    ``TypeError`` before the model is ever built — for the WHOLE run, not just this tool. The fix
    must silently ignore any non-string (or otherwise unusable) element and keep the valid ones."""
    spec = tool_specs_for("hostile-required", _REQUIRED_LIST_WITH_UNHASHABLE_ELEMENTS_DESCRIPTOR)[0]
    assert spec.parameters["required"] == ["q"]


# ── sibling degenerate cases (review-flagged): each must not raise, degradation shape unasserted
# where it is not obvious what "sensible" means ────────────────────────────────────────────────
#
# Unlike the test above, NONE of the three below are RED against the current implementation — each
# already degrades without raising, verified directly against `tool_schemas.py` before writing
# these. They are kept as permanent regression guards rather than dropped, since the review named
# them explicitly as untested degenerate inputs. Where "sensible" degradation is not obvious (a
# malformed `properties` value; a non-dict `parameters`), only "does not raise" is asserted — a
# narrow true assertion beats a wide guessed one.


def test_a_non_dict_properties_value_falls_back_to_the_hint_mapped_form() -> None:
    """A declared ``properties`` entry whose VALUE is not itself a schema object (a corrupted
    registry write, say) must not travel to the model verbatim — every other hostile-input path in
    this file degrades safely, this one does not: ``properties[key] = declared_properties[key]``
    copies the entry with no check. RED against the current implementation, which copies the
    hostile value through unchanged.

    The natural degradation is the SAME fallback already used for a hint-map key the declared
    schema does not cover at all (``test_a_hint_map_key_the_declared_schema_does_not_cover_keeps_
    its_hint_mapped_form`` above): the hint-mapped ``{"type": ...}`` shape, derived from the
    operation's own ``parameters`` hint for that key. Asserting that exact shape rather than only
    "does not raise" because it is consistent with surrounding code, not invented."""
    descriptor = {
        "id": "core-non-dict-property-value",
        "metadata": {"name": "Non-dict Property Value"},
        "spec": {
            "type": "API",
            "capabilities": [{"name": "op", "description": "op", "parameters": {"q": "str"}}],
            "input_schema": {
                "type": "object",
                "properties": {"q": "a string, not a schema object"},
            },
        },
    }
    params = tool_specs_for("non-dict-property", descriptor)[0].parameters
    assert params["properties"]["q"] == {"type": "string"}


def test_an_operations_parameters_as_a_list_never_raises() -> None:
    """An operation whose ``parameters`` hint map is a list rather than a dict (malformed registry
    data) must not crash schema building. Already does not raise today — a non-dict ``parameters``
    degrades to an empty hint map, the same as no ``parameters`` at all. What "sensible"
    degradation looks like beyond "does not raise" is deliberately NOT asserted here."""
    descriptor = {
        "id": "core-parameters-as-list",
        "metadata": {"name": "Parameters As List"},
        "spec": {
            "type": "API",
            "capabilities": [{"name": "op", "description": "op", "parameters": ["q", "r"]}],
            "input_schema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    }
    tool_specs_for("parameters-as-list", descriptor)  # must not raise


def test_an_operation_with_no_parameters_key_and_a_declared_input_schema_never_raises() -> None:
    """An operation that declares no ``parameters`` key at all, alongside a plugin-level
    ``spec.input_schema`` that DOES exist — distinct from the pre-existing MCP-only
    ``test_an_operation_with_neither_schema_nor_parameters_is_still_callable``, whose descriptor
    has no ``input_schema`` either, so it never reaches the #911 projection path at all. Already
    does not raise today. What "sensible" degradation looks like beyond "does not raise" is
    deliberately NOT asserted here."""
    descriptor = {
        "id": "core-no-parameters-key",
        "metadata": {"name": "No Parameters Key"},
        "spec": {
            "type": "API",
            "capabilities": [{"name": "op", "description": "op"}],
            "input_schema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    }
    tool_specs_for("no-parameters-key", descriptor)  # must not raise
