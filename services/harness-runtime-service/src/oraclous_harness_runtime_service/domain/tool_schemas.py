"""Capability descriptor → LLM tool schemas (domain layer).

A capability descriptor declares one or more *operations* (``spec.capabilities`` — e.g. a database
reader's ``list_tables`` / ``query``). Each operation becomes one LLM-callable ``ToolSpec`` named
``<binding>__<operation>`` so the model selects an operation; the loop maps the call back to
``registry.execute(instance, {"operation": <op>, **args})``. Mirrors the legacy
``agent_tool_schemas`` shape but is descriptor-driven rather than a static dict.
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


def _json_schema(parameters: Any) -> dict[str, Any]:
    """Build a minimal JSON-schema object from a descriptor operation's ``parameters`` map."""
    props: dict[str, Any] = {}
    if isinstance(parameters, dict):
        for key, hint in parameters.items():
            props[str(key)] = {"type": _TYPE_MAP.get(str(hint).lower(), "string")}
    return {"type": "object", "properties": props, "required": []}


def _parameters_for(op: dict[str, Any]) -> dict[str, Any]:
    """The operation's JSON schema for the model.

    #698 D1: an MCP-imported operation carries the server's own ``inputSchema`` verbatim as
    ``parameters_schema``. That schema is nested (objects inside objects, enums, ``required``
    lists) and the flat ``parameters`` hint map cannot express it, so it is passed through
    UNCHANGED. A first-party descriptor declares no ``parameters_schema`` and keeps the hint-map
    path. ``tools/list`` is untrusted input, so a non-dict schema degrades to an empty object
    rather than reaching the model or raising.
    """
    schema = op.get("parameters_schema")
    if isinstance(schema, dict):
        return schema
    return _json_schema(op.get("parameters"))


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
    out: list[ToolSpec] = []
    for op in operations:
        if not isinstance(op, dict) or not op.get("name"):
            continue
        op_name = str(op["name"])
        out.append(
            ToolSpec(
                name=_function_name(binding, op_name),
                description=op.get("description") or f"{name}: {op_name}",
                parameters=_parameters_for(op),
                binding=binding,
                # the registry dispatches on this and the external server expects its own
                # spelling, so it keeps the server's name however the LLM-facing one was sanitised
                operation=op_name,
            )
        )
    return out
