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


_MCP_SPEC_TYPE = "mcp"

#: The one field the registry dispatches on, never one a model may fill in itself (see the
#: ``dispatch_payload`` section below for the full rule). Defined here because the #911 projection
#: needs it too; the constant is not redefined further down.
_OPERATION_KEY = "operation"


def _project_input_schema(
    op: dict[str, Any], input_schema: dict[str, Any], bound_config: Mapping[str, Any]
) -> dict[str, Any]:
    """Project the plugin-level ``spec.input_schema`` onto ONE operation (#911).

    A plugin declares a single ``INPUT_SCHEMA`` for the whole class, but each operation only takes
    the arguments named in ITS OWN ``parameters`` hint map — restricting by those keys is what
    stops, e.g., the Postgres reader's ``query``/``params`` from leaking onto ``list_tables``,
    which takes no arguments at all.
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
        if key in declared_properties:
            # Verbatim: union `type` lists, `description`, `enum`, `minLength`, `items`, anything
            # else the plugin declared travels unchanged.
            properties[key] = declared_properties[key]
        else:
            # A hint-map key the declared schema doesn't cover must never silently disappear from
            # the model's view — it keeps its current hint-mapped shape.
            hint = hints[key]
            properties[key] = {"type": _TYPE_MAP.get(str(hint).lower(), "string")}

    # A required key that the DISPATCHING INSTANCE already binds (run-time harness config or an
    # operator's tool-instance config, both arrive here as `bound_config`) must not be advertised
    # as required — the model is never asked to supply an argument it cannot see and does not
    # control. This is the outbound mirror of the capability registry's own inbound check
    # (`services/capability-registry-service/src/oraclous_capability_registry_service/domain/
    # executors/input_validation.py:92-98`, the `bound` set in `_check`), which already treats a
    # `configuration` key as satisfying `required` server-side; the two live in separate, untied
    # test suites, so treat that file/line as a live cross-reference to keep in sync, not
    # decoration. `!= _OPERATION_KEY` is redundant with `op_key_set`'s exclusion above — kept
    # anyway as the second, independent line of defence.
    required = [
        k
        for k in declared_required
        if isinstance(k, str) and k in op_key_set and k not in bound_config and k != _OPERATION_KEY
    ]

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _parameters_for(
    op: dict[str, Any],
    *,
    imported: bool,
    input_schema: Any,
    bound_config: Mapping[str, Any],
) -> dict[str, Any]:
    """The operation's JSON schema for the model, in priority order (#698 D1, #911).

    1. A per-operation ``parameters_schema`` override wins outright, unchanged — existing
       behaviour, untouched by #911.
    2. An MCP-imported operation carries the server's own ``inputSchema`` verbatim (nested objects,
       enums, ``required`` lists the flat hint map cannot express) and is NEVER projected from
       ``spec.input_schema`` — that field belongs to a first-party plugin, not an imported server.
       ``tools/list`` is untrusted input, so a non-dict schema degrades to an empty object rather
       than reaching the model or raising.
    3. A first-party (non-MCP) operation with a dict-valued plugin-level ``spec.input_schema`` gets
       that schema PROJECTED onto its own hint-map keys (#911) — carrying ``required``, union
       types, ``enum``, ``minLength``, ``items``, etc. that the flat hint map alone cannot express.
    4. Otherwise (no ``spec.input_schema``, or a hostile non-dict value) falls back to exactly
       today's hint-map-only dict, closed (#956 ruling 2). Never raises on a hostile value.
    """
    schema = op.get("parameters_schema")
    if isinstance(schema, dict):
        return schema
    if imported:
        return _json_schema(op.get("parameters"), closed=False)
    if isinstance(input_schema, dict):
        return _project_input_schema(op, input_schema, bound_config)
    return _json_schema(op.get("parameters"), closed=True)


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
    harness config or an operator's tool-instance config — so they are dropped from the projected
    ``required`` list without hiding the property itself. Defaults to ``None`` (treated as empty) so
    every existing call site keeps working unchanged; only a first-party projection (priority 3 in
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
        out.append(
            ToolSpec(
                name=_function_name(binding, op_name),
                description=op.get("description") or f"{name}: {op_name}",
                parameters=_parameters_for(
                    op,
                    imported=imported,
                    input_schema=input_schema,
                    bound_config=effective_bound_config,
                ),
                binding=binding,
                # the registry dispatches on this and the external server expects its own
                # spelling, so it keeps the server's name however the LLM-facing one was sanitised
                operation=op_name,
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
    rest = {k: v for k, v in args.items() if k not in keys}
    _report_unknown_keys(spec, rest)
    return {_OPERATION_KEY: spec.operation, **rest}
