"""Capability descriptor → LLM tool schemas (domain layer).

A capability descriptor declares one or more *operations* (``spec.capabilities`` — e.g. a database
reader's ``list_tables`` / ``query``). Each operation becomes one LLM-callable ``ToolSpec`` named
``<binding>__<operation>`` so the model selects an operation; the loop maps the call back to
``registry.execute(instance, {"operation": <op>, **args})``. Mirrors the legacy
``agent_tool_schemas`` shape but is descriptor-driven rather than a static dict.

The inverse mapping lives here too (#956): ``dispatch_payload`` turns a model's call back into the
registry payload with the BOUND operation, never one the model chose.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from typing import Any

from oraclous_harness_runtime_service.domain.llm.base import ToolSpec

# Loose legacy parameter hints ("str"/"int"/…) → JSON-schema types.
_TYPE_MAP = {
    "str": "string",
    "string": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "list": "array",
    "dict": "object",
    "object": "object",
}


def _json_schema(parameters: Any, *, closed: bool) -> dict[str, Any]:
    """Build a minimal JSON-schema object from a descriptor operation's ``parameters`` map.

    ``closed`` sets ``additionalProperties: false`` (#956 ruling 2): the platform owns a first-party
    operation's schema, so a key it did not declare — ``operation`` above all — is refused at the
    model boundary by any schema-honouring provider, before the runtime's own strip at dispatch.
    """
    props: dict[str, Any] = {}
    if isinstance(parameters, dict):
        for key, hint in parameters.items():
            props[str(key)] = {"type": _TYPE_MAP.get(str(hint).lower(), "string")}
    schema: dict[str, Any] = {"type": "object", "properties": props, "required": []}
    if closed:
        schema["additionalProperties"] = False
    return schema


def _widen_type_to_nullable(prop: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Widen ``prop``'s declared ``type`` to also accept ``null``, leaving every other keyword
    (``minLength``, ``format``, ``minimum``, ``maximum``, ``default``, ``enum``, ``description``,
    …) untouched (#898 probe fact 3). A bare type becomes a two-element list; an existing type
    list gets ``"null"`` appended only if it is not already present, so a genuinely-nullable
    property (``["object", "null"]``) is never doubled up.

    Returns ``(widened_prop, changed)`` — ``changed`` is ``True`` only when a ``type`` was actually
    modified to add null-acceptance. It is ``False`` when the type already accepted null (nothing
    to change) and, notably, when ``prop`` has no ``type`` key at all — a bare ``enum`` or an
    ``anyOf``/``oneOf`` shape — because there is nothing for this function to widen. ``changed`` is
    ``render_strict_schema``'s single source of truth for "the platform made this key nullable";
    see its docstring for why that distinction matters.
    """
    widened = dict(prop)
    current = widened.get("type")
    if isinstance(current, list):
        if "null" not in current:
            widened["type"] = [*current, "null"]
            return widened, True
        return widened, False
    if isinstance(current, str):
        widened["type"] = [current, "null"]
        return widened, True
    return widened, False


def _is_object_type(type_value: Any) -> bool:
    """True for a schema's bare ``"object"`` type OR a type union that includes it
    (``["object", "null"]``) — the shape ``_widen_type_to_nullable`` itself produces when an
    optional, object-typed property is widened. Matching only the bare string would silently skip
    closing (and recursing into) exactly the properties this renderer just made nullable."""
    return type_value == "object" or (isinstance(type_value, list) and "object" in type_value)


def _is_array_type(type_value: Any) -> bool:
    """The array counterpart of ``_is_object_type``, same reason: a widened array-typed property
    carries ``["array", "null"]``, not the bare string."""
    return type_value == "array" or (isinstance(type_value, list) and "array" in type_value)


def _close_nested_object_schemas(value: Any) -> Any:
    """Recursively set ``additionalProperties: false`` on every NESTED object schema — a directly
    nested object property, or the item schema of an array — without touching that nested
    object's own ``required``/``type``. Only the top-level render (``render_strict_schema``)
    recomputes ``required``/nullability; an already-well-formed nested contract is left as its
    author wrote it, just closed.

    Nested ``required`` is left untouched deliberately, and this is now MEASURED, not assumed: a
    second real-provider probe (OpenRouter, 2026-09-13) built a nested object inside an array
    whose OWN ``required`` named only one of its two properties, with an ``enum`` on the other,
    and pushed hard for a value outside that enum. A partial nested ``required`` did not make the
    constraint inert — 0/5 forbidden values with a partial nested ``required``, 0/5 with a
    complete one — unlike probe fact 2 at the TOP level, where a partial list let 7/10 through. So
    only the top-level ``required`` needs forcing; a nested object's own partial ``required`` is
    left exactly as its author wrote it.
    """
    if not isinstance(value, dict):
        return value
    out = dict(value)
    type_value = out.get("type")
    if _is_object_type(type_value):
        nested_properties = out.get("properties")
        if isinstance(nested_properties, dict):
            out["properties"] = {
                key: _close_nested_object_schemas(nested_value)
                for key, nested_value in nested_properties.items()
            }
        out["additionalProperties"] = False
    if _is_array_type(type_value):
        items = out.get("items")
        if isinstance(items, dict):
            out["items"] = _close_nested_object_schemas(items)
    return out


def _render_strict_schema(
    schema: Any, *, force_nullable: frozenset[str] = frozenset()
) -> tuple[Any, frozenset[str]]:
    """Shared implementation behind ``render_strict_schema`` (the public, schema-only entry point
    below) and ``_project_input_schema`` (which also needs the widened-keys half). One
    implementation, one source of truth — see ``render_strict_schema``'s docstring for the full
    rationale and the probe facts driving it.

    Returns ``(rendered_schema, widened_keys)``. ``widened_keys`` is the SOLE source of truth for
    "the platform made this key nullable" — every property whose ``type`` this call actually
    changed to accept null, per ``_widen_type_to_nullable``'s own ``changed`` flag. A caller must
    use it directly rather than recomputing "not in the pre-render required set, or force-nullable"
    independently: that condition and this function's own widening decision can drift, and one case
    already does — a property with no declared ``type`` at all (a bare ``enum``, an
    ``anyOf``/``oneOf`` shape) satisfies the recomputed condition when it is optional, but
    ``_widen_type_to_nullable`` leaves it completely untouched (there is no ``type`` to widen), so a
    caller trusting its own copy of the condition would wrongly call it "made nullable" and a
    dispatch-time strip would then drop a value the caller legitimately sent. A non-object schema
    returns an empty ``widened_keys`` alongside the unchanged ``schema``.
    """
    if not isinstance(schema, dict) or not _is_object_type(schema.get("type")):
        return schema, frozenset()

    declared_properties = schema.get("properties")
    declared_properties = declared_properties if isinstance(declared_properties, dict) else {}
    original_required = set(schema.get("required") or [])

    rendered_properties: dict[str, Any] = {}
    required: list[str] = []
    widened_keys: set[str] = set()
    for key, prop in declared_properties.items():
        required.append(key)
        closed = _close_nested_object_schemas(prop)
        if isinstance(closed, dict) and (key in force_nullable or key not in original_required):
            closed, changed = _widen_type_to_nullable(closed)
            if changed:
                widened_keys.add(key)
        rendered_properties[key] = closed

    rendered = dict(schema)
    rendered["properties"] = rendered_properties
    rendered["required"] = required
    rendered["additionalProperties"] = False
    return rendered, frozenset(widened_keys)


def render_strict_schema(schema: Any, *, force_nullable: frozenset[str] = frozenset()) -> Any:
    """Render ``schema`` into the dialect a provider's ``strict`` function-calling flag needs to
    actually bind (#898 — real-provider probe, OpenRouter, 2026-09-13).

    Fact 2 of the probe: the flag is SILENTLY INERT unless every declared property is in
    ``required`` (a partial ``required`` list let a forbidden value through 7/10 times with the
    flag set; a complete one, 0/10 — no error either way). So every top-level property here ends
    up in ``required``, unconditionally. A property that was not already required — or is named in
    ``force_nullable`` (the #911 instance-bound-argument case: the model cannot know the value at
    all, so it must never be *asked* for it) — is instead widened to accept ``null`` rather than
    dropped, so the model can satisfy the requirement without guessing. A property already
    required and not force-nullable reaches the model as its plain, unwidened type: there is no
    null escape hatch for something the caller must always supply.

    ``additionalProperties: false`` is set at the top level and at every nested object schema too
    (see ``_close_nested_object_schemas``); a nested object's own ``required``/``type`` are left
    exactly as declared.

    A non-object schema — anything without ``{"type": "object"}``, including a non-dict value —
    passes through completely unchanged: this renderer only ever applies to the shape
    ``required``/``additionalProperties`` mean anything for.

    Pure and idempotent: rendering an already-rendered schema with the same ``force_nullable``
    reproduces it exactly.

    Returns the rendered schema alone. A caller that also needs to know WHICH keys were widened
    (``_project_input_schema``, for ``ToolSpec.nullable_keys``) uses the shared private helper
    ``_render_strict_schema`` directly rather than recomputing that set independently — see its
    docstring for why a recomputed copy of the condition can drift from what this function actually
    did.
    """
    rendered, _widened_keys = _render_strict_schema(schema, force_nullable=force_nullable)
    return rendered


_MCP_SPEC_TYPE = "mcp"

#: The one field the registry dispatches on, never one a model may fill in itself (see the
#: ``dispatch_payload`` section below for the full rule). Defined here because the #911 projection
#: needs it too; the constant is not redefined further down.
_OPERATION_KEY = "operation"


def _project_input_schema(
    op: dict[str, Any], input_schema: dict[str, Any], bound_config: Mapping[str, Any]
) -> tuple[dict[str, Any], frozenset[str]]:
    """Project the plugin-level ``spec.input_schema`` onto ONE operation (#911), then render it
    strict (#898).

    A plugin declares a single ``INPUT_SCHEMA`` for the whole class, but each operation only takes
    the arguments named in ITS OWN ``parameters`` hint map — restricting by those keys is what
    stops, e.g., the Postgres reader's ``query``/``params`` from leaking onto ``list_tables``,
    which takes no arguments at all.

    A key the DISPATCHING INSTANCE already binds (run-time harness config or an operator's
    tool-instance config, both arrive here as ``bound_config``) is never something the model can
    see a value for. Pre-#898 this was subtracted from ``required`` outright; probe fact 2 (a
    partial ``required`` list makes the provider's strict flag silently inert) means that would
    now break strict binding for every OTHER property on the same operation, so it is instead
    passed to ``render_strict_schema`` as ``force_nullable`` — forced back into ``required`` AND
    widened to accept null, so the model satisfies the schema without guessing. This is the
    outbound mirror of the capability registry's own inbound check
    (`services/capability-registry-service/src/oraclous_capability_registry_service/domain/
    executors/input_validation.py:91-112`, the `bound` parameter and its use in `_check`), which
    already treats a `configuration` key as satisfying `required` server-side; the two live in
    separate, untied test suites, so treat that file/line as a live cross-reference to keep in
    sync, not decoration.

    Returns the rendered schema and ``nullable_keys`` — every property the render widened, whether
    because the plugin's own declared schema left it genuinely optional or because ``bound_config``
    forced it — so the caller can populate ``ToolSpec.nullable_keys`` for the dispatch-time null
    strip (see ``dispatch_payload`` below).
    """
    parameters = op.get("parameters")
    hints: dict[str, Any] = parameters if isinstance(parameters, dict) else {}
    # Defence in depth, not a guard against any shipped descriptor: no first-party operation's own
    # hint map declares "operation" today (confirmed against the shipped connectors), but a
    # hypothetical future one might, so it is excluded from the op's own key set here too — never
    # relying solely on the "minus operation" subtraction in ``required`` below.
    op_keys = [str(k) for k in hints if str(k) != _OPERATION_KEY]
    op_key_set = set(op_keys)

    declared_properties = input_schema.get("properties")
    declared_properties = declared_properties if isinstance(declared_properties, dict) else {}
    declared_required = input_schema.get("required")
    declared_required = declared_required if isinstance(declared_required, list) else []

    properties: dict[str, Any] = {}
    for key in op_keys:
        declared_value = declared_properties.get(key)
        if key in declared_properties and isinstance(declared_value, dict):
            # Verbatim: union `type` lists, `description`, `enum`, `minLength`, `items`, anything
            # else the plugin declared travels unchanged.
            properties[key] = declared_value
        else:
            # A hint-map key the declared schema doesn't cover — or one whose declared VALUE is
            # not itself a schema object (a corrupted registry write) — must never silently
            # disappear or travel to the model verbatim: it keeps its current hint-mapped shape.
            hint = hints[key]
            properties[key] = {"type": _TYPE_MAP.get(str(hint).lower(), "string")}

    # The declared-required keys that are genuinely this operation's own, BEFORE the strict
    # render forces every property into `required` — this pre-render list is what tells a
    # genuinely-mandatory property (stays plain) from an optional-or-bound one (gets widened).
    # `isinstance(k, str)` short-circuits before any hashing, so a malformed `required` element
    # (a nested list/dict — unhashable, from a corrupted registry write) is silently ignored
    # rather than raising, matching how every other hostile value in this module degrades.
    pre_required = [
        k
        for k in declared_required
        if isinstance(k, str) and k in op_key_set and k != _OPERATION_KEY
    ]
    force_nullable = frozenset(k for k in op_key_set if k in bound_config)

    # `widened_keys` is the shared renderer's OWN account of which properties it actually made
    # nullable — never recomputed here from `pre_required`/`force_nullable` independently. Calls
    # the private `_render_strict_schema` (not the public `render_strict_schema`, which returns
    # only the schema) specifically to get that account without a second, driftable copy of its
    # condition. See its docstring: a property with no declared `type` at all would wrongly count
    # as "made nullable" under a recomputed condition even though the renderer left it untouched.
    rendered, widened_keys = _render_strict_schema(
        {"type": "object", "properties": properties, "required": pre_required},
        force_nullable=force_nullable,
    )
    return rendered, widened_keys


def _explicit_nullable_keys(op: dict[str, Any]) -> frozenset[str]:
    """The op's own explicit ``parameters_schema_nullable_keys`` declaration (#898), carried
    exactly the way ``parameters_schema_strict`` is: a sibling marker on the op, never inferred
    from the override schema's own shape.

    A hand-authored ``parameters_schema`` override (priority 1 in ``_parameters_for``) is the
    platform's own schema too — #900 lands next and authors overrides on exactly this path — so it
    must be able to say which of ITS OWN properties the platform rendered nullable, the same way
    ``_project_input_schema`` reports ``widened_keys`` for a projected one. Without this, an
    override with a genuinely nullable property is safe only by coincidence (today's two
    hand-authored overrides happen to be read with a plain, non-defaulted lookup); the moment one
    is read with ``input_data.get(key, default)``, an unstripped null silently destroys the
    default — the exact platform-wide fail-open this issue exists to close, reappearing on this
    path. An override declaring none behaves exactly as before. A hostile or non-iterable value
    (not a list/set/tuple, or containing a non-string) degrades to no declared keys rather than
    raising — a descriptor is data, same posture as every other hostile value in this module.
    """
    declared = op.get("parameters_schema_nullable_keys")
    if not isinstance(declared, list | set | frozenset | tuple):
        return frozenset()
    return frozenset(key for key in declared if isinstance(key, str))


def _parameters_for(
    op: dict[str, Any],
    *,
    imported: bool,
    input_schema: Any,
    bound_config: Mapping[str, Any],
) -> tuple[dict[str, Any], bool, frozenset[str]]:
    """The operation's JSON schema for the model, in priority order (#698 D1, #911, #898).

    Returns ``(parameters, strict, nullable_keys)``.

    1. A per-operation ``parameters_schema`` override wins outright, unchanged — existing
       behaviour, untouched by #911. ``strict`` is carried EXPLICITLY here too: a sibling
       ``parameters_schema_strict`` marker on the op, never inferred from the override's own shape
       (#898/#900 — a platform-authored override landing on a descriptor must not silently inherit
       or lose strictness via a schema that merely happens to look closed+required already).
       ``nullable_keys`` is carried the same explicit way, via ``parameters_schema_nullable_keys``
       (see ``_explicit_nullable_keys``) — so the override's own genuinely-nullable properties get
       the dispatch-time null strip too, without inferring anything from the schema's shape.
    2. An MCP-imported operation carries the server's own ``inputSchema`` verbatim (nested objects,
       enums, ``required`` lists the flat hint map cannot express) and is NEVER projected from
       ``spec.input_schema`` — that field belongs to a first-party plugin, not an imported server.
       ``tools/list`` is untrusted input, so a non-dict schema degrades to an empty object rather
       than reaching the model or raising. Always ``strict=False`` and ``nullable_keys=frozenset()``
       — an imported server's schema is its own untrusted contract, never ours to constrain or
       manage nulls for, however strict or nullable it happens to look.
    3. A first-party (non-MCP) operation with a dict-valued plugin-level ``spec.input_schema`` gets
       that schema PROJECTED onto its own hint-map keys (#911) and rendered strict (#898).
    4. Otherwise (no ``spec.input_schema``, or a hostile non-dict value) falls back to exactly
       today's hint-map-only dict, closed (#956 ruling 2), ``strict=False`` — the hint map alone
       carries no optionality information, so guessing "every key is required" here would be
       exactly the mistake the issue rejected. Never raises on a hostile value.
    """
    schema = op.get("parameters_schema")
    if isinstance(schema, dict):
        strict = bool(op.get("parameters_schema_strict")) and not imported
        nullable_keys = _explicit_nullable_keys(op) if not imported else frozenset()
        return schema, strict, nullable_keys
    if imported:
        return _json_schema(op.get("parameters"), closed=False), False, frozenset()
    if isinstance(input_schema, dict):
        rendered, nullable_keys = _project_input_schema(op, input_schema, bound_config)
        return rendered, True, nullable_keys
    return _json_schema(op.get("parameters"), closed=True), False, frozenset()


#: Providers accept a function name of at most 64 characters, matching ``[A-Za-z0-9_-]``.
_LLM_NAME_MAX = 64
_ILLEGAL_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_NAME_HASH_LEN = 8


def _function_name(binding: str, op_name: str) -> str:
    """``<binding>__<operation>``, sanitised and bounded to what a provider will accept.

    #698 D1: the importer bounds a discovered tool name at 255 characters — far past the provider
    limit — so an untrusted server could otherwise build a function name that the LLM adapter
    rejects, killing the whole run rather than the one tool. Sanitisation is DETERMINISTIC: a
    resumed run dispatches by the name it was offered, so the same descriptor must always produce
    the same name. When truncation bites, a short digest of the full name is appended so two
    operations differing only past the cut do not collapse into one and silently lose a tool.
    """
    name = _ILLEGAL_NAME_CHARS.sub("_", f"{binding}__{op_name}")
    if len(name) <= _LLM_NAME_MAX:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:_NAME_HASH_LEN]
    return f"{name[: _LLM_NAME_MAX - _NAME_HASH_LEN - 1]}_{digest}"


def tool_specs_for(
    binding: str,
    descriptor: dict[str, Any],
    *,
    bound_config: Mapping[str, Any] | None = None,
) -> list[ToolSpec]:
    """One ``ToolSpec`` per operation declared by the capability ``descriptor``.

    ``bound_config`` (#911) names the arguments the DISPATCHING INSTANCE already supplies — run-time
    harness config or an operator's tool-instance config. The model cannot see a value for one of
    these, so (#898) it is forced back into ``required`` and rendered nullable rather than hidden
    outright — a partial ``required`` list would silently defeat the provider's strict flag for
    every OTHER property on the same operation. Defaults to ``None`` (treated as empty) so every
    existing call site keeps working unchanged; only a first-party projection (priority 3 in
    ``_parameters_for``) ever consults it.
    """
    spec = descriptor.get("spec") or {}
    operations = spec.get("capabilities") or []
    name = (descriptor.get("metadata") or {}).get("name") or binding
    imported = spec.get("type") == _MCP_SPEC_TYPE
    input_schema = spec.get("input_schema")
    effective_bound_config: Mapping[str, Any] = bound_config if bound_config is not None else {}
    out: list[ToolSpec] = []
    for op in operations:
        if not isinstance(op, dict) or not op.get("name"):
            continue
        op_name = str(op["name"])
        parameters, strict, nullable_keys = _parameters_for(
            op,
            imported=imported,
            input_schema=input_schema,
            bound_config=effective_bound_config,
        )
        out.append(
            ToolSpec(
                name=_function_name(binding, op_name),
                description=op.get("description") or f"{name}: {op_name}",
                parameters=parameters,
                binding=binding,
                # the registry dispatches on this and the external server expects its own
                # spelling, so it keeps the server's name however the LLM-facing one was sanitised
                operation=op_name,
                strict=strict,
                nullable_keys=nullable_keys,
            )
        )
    return out


# ── #956: the model's call → the registry payload, with the BOUND operation ────────────────────
#
# ``_OPERATION_KEY`` is defined earlier in the file (next to ``_MCP_SPEC_TYPE``), reused here
# unchanged.

#: Closed-vocabulary code the refusal carries in ``str(exc)`` — the ``detail`` the loop feeds back
#: to the model — the way #692's registry codes do. A token, so the model can act on it.
OPERATION_OVERRIDE_REFUSED = "operation_override_refused"

#: Closed-vocabulary code for #1004 item 4: the model returned something that is not a JSON object
#: where its tool's arguments belong.
NON_OBJECT_ARGUMENTS_REFUSED = "tool_arguments_not_an_object"

#: How much of the model-supplied value a log line may carry. Enough to recognise a prompt
#: injection in a trace, too little to be a channel.
_SUPPLIED_PREVIEW_CHARS = 64

#: #1004 item 2: how many undeclared argument NAMES one warning may carry, and how long the
#: rendered list may be. A key name is itself model-authored text, so it is bounded as well as
#: counted — five 10,000-character names would be the channel the count alone did not close.
_MAX_UNKNOWN_NAMES = 5
_MAX_UNKNOWN_NAME_CHARS = 64

logger = logging.getLogger(__name__)


class ToolDispatchRefused(Exception):
    """A tool call was refused HERE, before the registry was ever called.

    #1004 item 4: both refusals are the same kind of event, so the dispatch closure can catch and
    log them under one name. ``OperationOverrideRefused`` keeps its own identity (and its own
    fields) for the call sites and tests that name it directly.
    """


class OperationOverrideRefused(ToolDispatchRefused):
    """The model put an ``operation`` in its arguments that is not the one its tool is bound to.

    Fail-closed (CLAUDE.md §3.5): the call is refused BEFORE the registry, not corrected. A
    silent correction would let a model probe which operations exist by watching which calls
    succeed; a refusal tells it the key is not accepted at all.

    The message never echoes the supplied value — that message is persisted into the run
    transcript and read by the model, and an unbounded echo there is the channel #956 closes. The
    bounded ``supplied_preview`` exists for the WARNING log only.
    """

    def __init__(self, *, tool: str, bound: str, supplied: object) -> None:
        self.tool = tool
        self.bound = bound
        self.supplied_preview = repr(supplied)[:_SUPPLIED_PREVIEW_CHARS]
        super().__init__(
            f"{OPERATION_OVERRIDE_REFUSED}: this tool runs the operation it is bound to "
            f"({bound!r}); an 'operation' argument is not accepted — call the tool without it"
        )


class NonObjectArgumentsRefused(ToolDispatchRefused):
    """The model's tool-call arguments were not a JSON object (#1004 item 4).

    ``args`` is the raw ``json.loads`` of what the model returned, so it can be a list, a string,
    a number or ``None``. That used to raise a ``TypeError`` out of a dict comprehension, which the
    loop's broad handler turned into a generic tool error naming nothing the model could fix
    (#693's lesson). Fail-closed by accident is not fail-closed by contract.

    The message names the shape that was expected and never the arguments themselves — it is the
    ``detail`` fed back to the model and persisted into the run transcript.
    """

    def __init__(self, *, tool: str) -> None:
        self.tool = tool
        super().__init__(
            f"{NON_OBJECT_ARGUMENTS_REFUSED}: this tool takes a JSON object of named arguments; "
            "the call supplied something else — send an object, even an empty one"
        )


def _operation_keys(args: dict[str, Any]) -> list[str]:
    """Every top-level key that MEANS ``operation``, however the model spelled its case.

    #1004 item 3: the match used to be exact, so ``Operation`` / ``OPERATION`` rode through as
    ordinary extra keys — harmless only because the sampled connectors read the lowercase
    spelling. Whether a given connector happens to read a variant is not the runtime's business:
    a key that means "pick the operation" is the binding's business however it is written.
    """
    return [k for k in args if isinstance(k, str) and k.lower() == _OPERATION_KEY]


def _report_unknown_keys(spec: ToolSpec, args: dict[str, Any]) -> None:
    """Log the NAMES of arguments a CLOSED schema did not declare (#1004 item 2).

    ``additionalProperties: false`` is a hint to the PROVIDER, not a server-side check: nothing on
    our side re-validates the model's arguments, and the ruling keeps it that way — real
    enforcement belongs with #898 (strict schemas) and #911 (``required``). What the runtime owes
    is visibility, so an operator can see a provider that ignored the hint instead of guessing.

    Only a schema that CLOSED itself is reported. An imported MCP operation's schema is the
    server's own contract, passed through as it came (#698 D1); an open schema declares extra keys
    legal, so warning on every argument of every imported tool would be noise, not signal.

    Names only, never values: a value is model-authored content, a key name is what an operator
    needs to diagnose. Bounded twice — at most ``_MAX_UNKNOWN_NAMES`` names, rendered into at most
    ``_MAX_UNKNOWN_NAME_CHARS`` characters — because a key name is model-authored text too.
    """
    schema = spec.parameters
    if not isinstance(schema, dict) or schema.get("additionalProperties") is not False:
        return
    properties = schema.get("properties")
    declared = set(properties) if isinstance(properties, dict) else set()
    unknown = sorted(k for k in args if k not in declared)
    if not unknown:
        return
    rendered = ", ".join(unknown[:_MAX_UNKNOWN_NAMES])[:_MAX_UNKNOWN_NAME_CHARS]
    logger.warning(
        "tool %s: %d argument(s) its closed schema does not declare were passed through "
        "unchecked (the schema is a provider hint, not a server-side check); first names, "
        "bounded: %s",
        spec.name,
        len(unknown),
        rendered,
    )


def dispatch_payload(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    """``{"operation": spec.operation, **args}`` with the model's own ``operation`` key removed.

    #956 ruling 1: the bound ``spec.operation`` always wins. The literal key used to be spread
    over by ``**args``, so a model-supplied ``operation`` chose which operation the connector
    ran. Now:

    * a key equal to the bound operation is stripped and the call proceeds — the model changed
      nothing, and refusing would cost it a turn for no gain;
    * a key that differs (any value, any type; the VALUE is matched exactly, with no case folding)
      raises ``OperationOverrideRefused`` and nothing is dispatched;
    * the strip is SHALLOW, matching the mcp connector's ``_arguments`` (#698 D3): a nested
      ``operation`` is the tool's own argument and travels intact.

    #1004 tightens three edges of the same rule. The KEY is matched case-insensitively (item 3), so
    ``Operation`` is not an ordinary extra key — and a payload carrying two spellings that disagree
    is refused rather than resolved by ordering. ``args`` that is not a JSON object is refused
    explicitly (item 4) rather than raising a ``TypeError`` from the comprehension below. And a key
    a CLOSED schema never declared is logged by name (item 2) while still travelling — the closed
    schema stays advisory.

    #898: a key in ``spec.nullable_keys`` — one the PLATFORM itself widened to accept null when
    rendering the schema, because the property was genuinely optional or an instance-bound
    argument the model cannot know a value for — has an explicit ``null`` STRIPPED here, before the
    registry ever sees it. Every connector reads an optional argument with
    ``input_data.get(key, default)``, and that returns the default only when the key is ABSENT;
    present with value ``null`` it returns ``None``, silently destroying the connector's own
    default. A null on any OTHER key (one the descriptor's own schema genuinely accepts, never
    widened by the platform) passes through untouched — this strip is opt-in per key, never a
    blanket null filter, and never mistakes a falsy-but-present value (``""``, ``0``, ``False``)
    for null.

    ``args`` is annotated ``dict`` because that is what a well-behaved provider sends; it is the
    raw ``json.loads`` of model output, so the annotation is a promise the input does not keep and
    the guard below is load-bearing.
    """
    if not isinstance(args, dict):
        raise NonObjectArgumentsRefused(tool=spec.name)
    keys = _operation_keys(args)
    for key in keys:
        supplied = args[key]
        if supplied != spec.operation:
            raise OperationOverrideRefused(tool=spec.name, bound=spec.operation, supplied=supplied)
    rest = {
        k: v
        for k, v in args.items()
        if k not in keys and not (v is None and k in spec.nullable_keys)
    }
    _report_unknown_keys(spec, rest)
    return {_OPERATION_KEY: spec.operation, **rest}
