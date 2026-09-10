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
import re
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


def _parameters_for(op: dict[str, Any], *, imported: bool) -> dict[str, Any]:
    """The operation's JSON schema for the model.

    #698 D1: an MCP-imported operation carries the server's own ``inputSchema`` verbatim as
    ``parameters_schema``. That schema is nested (objects inside objects, enums, ``required``
    lists) and the flat ``parameters`` hint map cannot express it, so it is passed through
    UNCHANGED. A first-party descriptor declares no ``parameters_schema`` and keeps the hint-map
    path. ``tools/list`` is untrusted input, so a non-dict schema degrades to an empty object
    rather than reaching the model or raising.

    #956 ruling 2: a first-party schema is CLOSED (``additionalProperties: false``). An imported
    server's schema is that server's contract and is not rewritten — open or closed as it came,
    and its no-schema fallback stays open, because a schema-less server tool takes whatever
    arguments the server accepts. The ``operation`` key is stripped for both at dispatch
    (``dispatch_payload``) and, for the imported path, again in the mcp connector (#698 D3).
    """
    schema = op.get("parameters_schema")
    if isinstance(schema, dict):
        return schema
    return _json_schema(op.get("parameters"), closed=not imported)


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


def tool_specs_for(binding: str, descriptor: dict[str, Any]) -> list[ToolSpec]:
    """One ``ToolSpec`` per operation declared by the capability ``descriptor``."""
    spec = descriptor.get("spec") or {}
    operations = spec.get("capabilities") or []
    name = (descriptor.get("metadata") or {}).get("name") or binding
    imported = spec.get("type") == _MCP_SPEC_TYPE
    out: list[ToolSpec] = []
    for op in operations:
        if not isinstance(op, dict) or not op.get("name"):
            continue
        op_name = str(op["name"])
        out.append(
            ToolSpec(
                name=_function_name(binding, op_name),
                description=op.get("description") or f"{name}: {op_name}",
                parameters=_parameters_for(op, imported=imported),
                binding=binding,
                # the registry dispatches on this and the external server expects its own
                # spelling, so it keeps the server's name however the LLM-facing one was sanitised
                operation=op_name,
            )
        )
    return out


# ── #956: the model's call → the registry payload, with the BOUND operation ────────────────────

#: The one field the registry dispatches on. A model that writes it into its arguments is trying
#: to pick the operation itself; the binding the runtime made decides, never the argument.
_OPERATION_KEY = "operation"

#: Closed-vocabulary code the refusal carries in ``str(exc)`` — the ``detail`` the loop feeds back
#: to the model — the way #692's registry codes do. A token, so the model can act on it.
OPERATION_OVERRIDE_REFUSED = "operation_override_refused"

#: How much of the model-supplied value a log line may carry. Enough to recognise a prompt
#: injection in a trace, too little to be a channel.
_SUPPLIED_PREVIEW_CHARS = 64


class OperationOverrideRefused(Exception):
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


def dispatch_payload(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    """``{"operation": spec.operation, **args}`` with the model's own ``operation`` key removed.

    #956 ruling 1: the bound ``spec.operation`` always wins. The literal key used to be spread
    over by ``**args``, so a model-supplied ``operation`` chose which operation the connector
    ran. Now:

    * a key equal to the bound operation is stripped and the call proceeds — the model changed
      nothing, and refusing would cost it a turn for no gain;
    * a key that differs (any value, any type; exact match, no case folding) raises
      ``OperationOverrideRefused`` and nothing is dispatched;
    * the strip is SHALLOW, matching the mcp connector's ``_arguments`` (#698 D3): a nested
      ``operation`` is the tool's own argument and travels intact.
    """
    if _OPERATION_KEY in args:
        supplied = args[_OPERATION_KEY]
        if supplied != spec.operation:
            raise OperationOverrideRefused(tool=spec.name, bound=spec.operation, supplied=supplied)
        args = {k: v for k, v in args.items() if k != _OPERATION_KEY}
    return {_OPERATION_KEY: spec.operation, **args}
