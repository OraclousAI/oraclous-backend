"""Unit (#898): the platform strips a null it asked for, and only that null.

The hazard this file guards against, stated in full because it is the whole reason #898 needs a
strip at all: making a strict schema bind (probe fact 2 — every declared property must be in
``required``) forces a previously-optional argument to be rendered as required-but-nullable
(``{"type": "string"}`` becomes ``{"type": ["string", "null"]}``). So the model will now routinely
send an explicit ``null`` for an argument it previously just omitted. Every connector in the
catalogue reads an optional argument with ``input_data.get(key, default)`` —
``services/capability-registry-service/.../domain/connectors/knowledge_retriever.py:93`` is one of
many — and ``dict.get(key, default)`` returns the DEFAULT when the key is absent, but returns
``None`` when the key is PRESENT with value ``null``. An explicit null therefore silently destroys
the connector's own default (``top_k``, ``min_score``, ``limit``, ``mode``, ``scope``,
``max_results``, and more, right across the catalogue) unless the platform strips it before the
registry ever sees it. That is a platform-wide fail-open regression and this file is what prevents
it shipping.

The containing rule (owner-approved, verbatim): the platform strips a null argument before
dispatching a tool call, but ONLY for a key the platform itself made nullable when rendering the
schema. A null that the descriptor's own schema genuinely accepts is passed through untouched.
Stripping every null blindly is wrong — it would silently swallow a value a connector's own schema
declares as a meaningful ``null`` (a real, if rare, shape) — so both halves of the rule are pinned
below, not just the strip.

``ToolSpec`` does not have a ``nullable_keys`` field yet (#898, built alongside ``strict`` — see
``test_tool_schemas.py``): every ``ToolSpec(...)`` construction below that passes it is an ordinary
keyword-argument call to an EXISTING, already-imported dataclass, so per
``.claude/rules/tests-seam-imports.md`` this is not a not-yet-built intra-repo seam import and is
not subject to the function-local-import rule — it fails at TEST RUNTIME with ``TypeError``
(unexpected keyword argument), not at collection time, exactly like every ``bound_config=`` call in
``test_tool_schemas.py`` before #911 landed.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.tool_schemas import dispatch_payload, tool_specs_for

pytestmark = pytest.mark.unit


def _spec(nullable_keys: frozenset[str] = frozenset(), **overrides: Any) -> ToolSpec:
    fields: dict[str, Any] = {
        "name": "knowledge-retriever__search",
        "description": "Search a knowledge graph",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": ["integer", "null"]},
                "notes": {"type": ["string", "null"]},  # genuinely nullable per the descriptor
            },
            "required": ["query", "top_k", "notes"],
            "additionalProperties": False,
        },
        "binding": "knowledge-retriever",
        "operation": "search",
        "nullable_keys": nullable_keys,
    }
    fields.update(overrides)
    return ToolSpec(**fields)


# ── the strip: a platform-made-nullable key ──────────────────────────────────────────────────────


def test_a_null_on_a_platform_made_nullable_key_is_stripped_before_dispatch() -> None:
    spec = _spec(nullable_keys=frozenset({"top_k"}))
    payload = dispatch_payload(spec, {"query": "revenue", "top_k": None})
    assert "top_k" not in payload
    assert payload["query"] == "revenue"


def test_the_connectors_own_default_applies_once_the_null_is_stripped() -> None:
    """The whole point: after the strip, the key is ABSENT, so ``input_data.get("top_k", 10)``
    (the connector's real read) returns its default — never ``None``."""
    spec = _spec(nullable_keys=frozenset({"top_k"}))
    payload = dispatch_payload(spec, {"query": "revenue", "top_k": None})
    assert payload.get("top_k", 10) == 10


# ── the pass-through: a genuinely-nullable key, not made nullable by the platform ─────────────────


def test_a_null_on_a_genuinely_nullable_key_is_passed_through_untouched() -> None:
    """``notes`` is nullable because the DESCRIPTOR says so, not because #898 widened it — it is
    not in ``nullable_keys``, so its null must reach the connector exactly as sent."""
    spec = _spec(nullable_keys=frozenset({"top_k"}))
    payload = dispatch_payload(spec, {"query": "revenue", "top_k": None, "notes": None})
    assert "top_k" not in payload
    assert payload["notes"] is None


def test_a_key_never_marked_nullable_by_anyone_is_never_stripped_on_null() -> None:
    """No ``nullable_keys`` at all on this spec (the default) — a null must never be dropped
    silently just because the runtime happens to see one; the strip is opt-in per key, never a
    general null filter."""
    spec = _spec()  # nullable_keys defaults to frozenset()
    payload = dispatch_payload(spec, {"query": "revenue", "top_k": None})
    assert "top_k" in payload
    assert payload["top_k"] is None


# ── #1059 quality-review follow-up: nullable_keys must reflect what actually changed ─────────────
#
# ``_project_input_schema`` recomputes which keys are nullable from a condition COPIED from inside
# the renderer ("not in the pre-render required set, or force-nullable") rather than reading back
# what the renderer itself actually widened. The two usually agree, but drift on a property with NO
# declared ``type`` at all (a bare ``enum``, or an either-or ``anyOf``/``oneOf`` shape): the
# renderer's own widening step only ever touches a ``type`` key, so a schema-less property is left
# completely untouched by it — the copied condition still calls it "made nullable" whenever it is
# optional, and ``dispatch_payload`` then strips a value on it the caller genuinely sent.

_NO_DECLARED_TYPE_DESCRIPTOR = {
    "id": "core-no-declared-type",
    "metadata": {"name": "No Declared Type"},
    "spec": {
        "type": "API",
        "capabilities": [
            {
                "name": "op",
                "description": "op",
                "parameters": {"query": "string", "mode": "string"},
            }
        ],
        "input_schema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                # No "type" key at all — a bare enum. There is nothing here for the renderer's own
                # widening step to touch, so this property reaches the model completely unchanged.
                "mode": {"enum": ["fast", "slow"]},
            },
        },
    },
}


def test_a_property_with_no_declared_type_is_not_reported_as_nullable() -> None:
    spec = tool_specs_for("no-declared-type", _NO_DECLARED_TYPE_DESCRIPTOR)[0]
    assert spec.parameters["properties"]["mode"] == {"enum": ["fast", "slow"]}, (
        "the renderer touched a property it had nothing (no 'type' key) to widen"
    )
    assert "mode" not in spec.nullable_keys, (
        "nullable_keys claims 'mode' was made nullable, but the renderer left it untouched"
    )


def test_a_null_on_that_property_is_never_stripped_as_though_it_were_platform_made_nullable() -> (
    None
):
    """The consequence, pinned end to end: even while the bug above stands, a caller's null on
    ``mode`` must survive dispatch — the descriptor never declared it nullable and the platform
    never made it nullable either, so ``dispatch_payload`` has no licence to drop it."""
    spec = tool_specs_for("no-declared-type", _NO_DECLARED_TYPE_DESCRIPTOR)[0]
    payload = dispatch_payload(spec, {"query": "q", "mode": None})
    assert "mode" in payload
    assert payload["mode"] is None


# ── a non-null value is never touched, whether or not the key is in nullable_keys ─────────────────


def test_a_non_null_value_on_a_nullable_key_is_never_touched() -> None:
    spec = _spec(nullable_keys=frozenset({"top_k"}))
    payload = dispatch_payload(spec, {"query": "revenue", "top_k": 5})
    assert payload["top_k"] == 5


# ── the classic falsy bug: empty string / zero / False are never mistaken for null ────────────────


@pytest.mark.parametrize("falsy_but_not_none", ["", 0, False, []])
def test_a_falsy_non_none_value_on_a_nullable_key_is_never_stripped(
    falsy_but_not_none: object,
) -> None:
    spec = _spec(nullable_keys=frozenset({"top_k"}))
    payload = dispatch_payload(spec, {"query": "revenue", "top_k": falsy_but_not_none})
    assert "top_k" in payload
    assert payload["top_k"] == falsy_but_not_none
    assert payload["top_k"] is not None


# ── the operation-key strip (#956) and the null-strip (#898) compose without interference ────────


def test_the_operation_strip_and_the_null_strip_both_apply_in_one_call() -> None:
    spec = _spec(nullable_keys=frozenset({"top_k"}))
    payload = dispatch_payload(spec, {"operation": "search", "query": "revenue", "top_k": None})
    assert payload == {"operation": "search", "query": "revenue"}


# ── end-to-end: the #911/#898 bound-config trap, through the real projection ──────────────────────

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


def test_a_bound_argument_the_model_sends_as_null_is_stripped_so_the_binding_wins() -> None:
    """The full round trip: ``graph_id`` is bound on this instance (#911) and #898 renders it
    required-but-nullable so the strict flag still binds; the model, following the schema, sends
    null for it; ``dispatch_payload`` must strip that null so the registry never sees a ``graph_id``
    key at all and the bound value (applied elsewhere, before this payload reaches the connector)
    is what actually governs — never overwritten by an explicit null.

    RED for the same compounding reason as ``test_tool_schemas.py``'s bound-config tests:
    ``tool_specs_for`` does not populate ``nullable_keys`` yet, so it is empty and this null is not
    stripped."""
    spec = tool_specs_for(
        "recall-memory",
        _RECALL_MEMORY_DESCRIPTOR,
        bound_config={"graph_id": "some-uuid"},
    )[0]
    payload = dispatch_payload(spec, {"graph_id": None, "query": "who approved this"})
    assert "graph_id" not in payload
    assert payload["query"] == "who approved this"
