"""Unit (#898): the pure schema-closing renderer that makes strict function calling actually bind.

Three measured facts (real-provider probe, OpenRouter, 2026-09-13) drive every assertion here:

1. The provider's ``strict`` flag genuinely suppresses an out-of-enum value: 0/5 with a fully
   closed+required schema vs. 5/5 without the flag.
2. The flag is SILENTLY INERT unless *every* declared property is in ``required`` — a schema naming
   only one of four properties as required let the forbidden value through 7/10 times with the flag
   set; with all four required, 0/10. No error either way. This is why the renderer forces every
   property into ``required`` rather than leaving genuinely-optional ones out.
3. Every other JSON-schema annotation keyword (``minLength``, ``format``, ``minimum``, ``maximum``,
   ``default``, ``enum``) survived untouched in the 0/10 case — there is no need to strip or rewrite
   them, only to close the schema and fix up ``required``/``type``.

Consequence of fact 2: a property that used to be genuinely optional cannot simply be dropped into
``required`` with its original type — the model would then be asked to always supply an argument it
may not have a value for. It is instead widened to accept ``null`` (a bare type becomes
``[type, "null"]``; an existing type list gets ``"null"`` appended if not already present) so the
model can satisfy the requirement by sending null. ``force_nullable`` names properties that must be
widened+required this same way regardless of the schema's own ``required`` list — the #911
instance-bound-argument case: the model cannot know the value at all, so it must never be *asked*
for it, only allowed to satisfy the schema with null.

The renderer is a pure function over an already-built ``{"type": "object", "properties": {...},
"required": [...]}`` schema; it does not know about descriptors, hint maps, or bound_config — those
live in ``_project_input_schema`` (see ``test_tool_schemas.py`` and
``test_tool_schema_bound_config.py`` for the integration-level assertions on ``ToolSpec.strict`` /
``ToolSpec.nullable_keys``). This file pins the renderer itself, in isolation.

``render_strict_schema`` does not exist yet (RED by design, #898). It is a NEW name in an EXISTING
module, so importing it at module level would abort collection for this whole file the moment
`main` doesn't have it yet — imported function-locally in every test per
``.claude/rules/tests-seam-imports.md``.
"""

from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.unit


def _render(schema, *, force_nullable=frozenset()):
    from oraclous_harness_runtime_service.domain.tool_schemas import render_strict_schema

    return render_strict_schema(schema, force_nullable=force_nullable)


# ── every property lands in required ──────────────────────────────────────────────────────────────


def test_every_property_lands_in_required_even_when_none_were_declared_required() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": [],
    }
    rendered = _render(schema)
    assert set(rendered["required"]) == {"a", "b"}


def test_a_property_already_required_stays_required_and_is_not_widened() -> None:
    """Fact 2's mandatory half: an argument the descriptor genuinely demands must reach the model
    as the plain type it always was — no null escape hatch for something the caller must supply."""
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1}},
        "required": ["query"],
    }
    rendered = _render(schema)
    assert rendered["required"] == ["query"]
    assert rendered["properties"]["query"]["type"] == "string"


# ── optional → nullable, never a bare type replacement ──────────────────────────────────────────


def test_a_property_that_was_optional_becomes_a_nullable_type_union() -> None:
    schema = {
        "type": "object",
        "properties": {"top_k": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10}},
        "required": [],
    }
    rendered = _render(schema)
    assert "top_k" in rendered["required"]
    assert rendered["properties"]["top_k"]["type"] == ["integer", "null"]


def test_a_property_already_carrying_a_type_list_gets_null_appended_not_replaced() -> None:
    schema = {
        "type": "object",
        "properties": {"draft": {"type": ["object", "string"], "description": "a draft"}},
        "required": [],
    }
    rendered = _render(schema)
    assert rendered["properties"]["draft"]["type"] == ["object", "string", "null"]


def test_a_property_whose_declared_type_already_accepts_null_is_left_alone() -> None:
    """The descriptor's own schema already declared this genuinely nullable — the renderer must
    not double up the union or otherwise disturb it, only ensure it is required."""
    schema = {
        "type": "object",
        "properties": {"manifest": {"type": ["object", "null"]}},
        "required": [],
    }
    rendered = _render(schema)
    assert rendered["properties"]["manifest"]["type"] == ["object", "null"]
    assert "manifest" in rendered["required"]


# ── force_nullable: the instance-bound-argument trap ──────────────────────────────────────────────


def test_a_force_nullable_property_is_widened_even_if_it_was_already_required() -> None:
    """#911's trap: the plugin declares ``graph_id`` genuinely required, but THIS instance already
    binds it — the model cannot know its value and must never be asked to supply it, only allowed
    to satisfy the schema with null."""
    schema = {
        "type": "object",
        "properties": {
            "graph_id": {"type": "string", "format": "uuid"},
            "query": {"type": "string", "minLength": 1},
        },
        "required": ["graph_id", "query"],
    }
    rendered = _render(schema, force_nullable=frozenset({"graph_id"}))
    assert rendered["properties"]["graph_id"]["type"] == ["string", "null"]
    assert rendered["properties"]["graph_id"]["format"] == "uuid"  # annotation survives
    assert "graph_id" in rendered["required"]  # still required — this is what makes strict bind
    assert rendered["properties"]["query"]["type"] == "string"  # untouched, genuinely mandatory
    assert "query" in rendered["required"]


# ── annotation keywords are never stripped or rewritten (fact 3) ─────────────────────────────────


def test_annotation_keywords_survive_the_render_untouched() -> None:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            "mode": {"type": "string", "enum": ["semantic", "fulltext", "hybrid"]},
            "graph_id": {"type": "string", "format": "uuid", "description": "optional; …"},
        },
        "required": ["query"],
    }
    rendered = _render(schema)
    props = rendered["properties"]
    assert props["query"]["minLength"] == 1
    assert props["top_k"]["minimum"] == 1
    assert props["top_k"]["maximum"] == 100
    assert props["top_k"]["default"] == 10
    assert props["mode"]["enum"] == ["semantic", "fulltext", "hybrid"]
    assert props["graph_id"]["format"] == "uuid"
    assert props["graph_id"]["description"] == "optional; …"


# ── additionalProperties: false at every object level, including nested ──────────────────────────


def test_additional_properties_is_closed_at_the_top_level() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    rendered = _render(schema)
    assert rendered["additionalProperties"] is False


def test_additional_properties_is_closed_on_a_nested_object_inside_an_array(
) -> None:
    """The real shape: ``core/github-sink@1.0.0``'s ``deliver`` operation takes ``files``, an array
    of ``{path, content}`` objects. A model could otherwise invent a third key (``sha``, say)
    inside one array element and the provider would accept it."""
    schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            },
        },
        "required": ["repo", "files"],
    }
    rendered = _render(schema)
    assert rendered["additionalProperties"] is False
    assert rendered["properties"]["files"]["items"]["additionalProperties"] is False
    # the nested object's OWN required list is untouched — #898 forces every TOP-LEVEL property
    # into required; it does not reach into an already-well-formed nested contract and rewrite it
    assert rendered["properties"]["files"]["items"]["required"] == ["path", "content"]


def test_additional_properties_is_closed_on_a_directly_nested_object_property() -> None:
    schema = {
        "type": "object",
        "properties": {
            "filters": {
                "type": "object",
                "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            }
        },
        "required": [],
    }
    rendered = _render(schema)
    assert rendered["properties"]["filters"]["additionalProperties"] is False


# ── a non-object schema is refused rather than mangled ────────────────────────────────────────────


@pytest.mark.parametrize(
    "hostile",
    [
        {"type": "array", "items": {"type": "string"}},
        {"type": "string"},
        "not-a-schema-at-all",
        None,
        42,
    ],
)
def test_a_non_object_schema_is_returned_unchanged_never_mangled(hostile: object) -> None:
    """Rendering only ever applies to an object schema — the shape ``required``/
    ``additionalProperties`` mean anything for. Anything else must come back exactly as it went in,
    not partially rewritten with a nonsensical ``required``/``additionalProperties`` bolted on."""
    before = copy.deepcopy(hostile) if isinstance(hostile, dict) else hostile
    rendered = _render(hostile)
    assert rendered == before


# ── idempotency ──────────────────────────────────────────────────────────────────────────────────


def test_rendering_twice_gives_the_same_result_as_rendering_once() -> None:
    schema = {
        "type": "object",
        "properties": {
            "graph_id": {"type": "string", "format": "uuid"},
            "query": {"type": "string", "minLength": 1},
            "top_k": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    }
    once = _render(schema, force_nullable=frozenset({"graph_id"}))
    twice = _render(once, force_nullable=frozenset({"graph_id"}))
    assert twice == once
