"""Capability descriptor → LLM tool schemas (ORAA-4 §21 domain layer).

A capability descriptor declares one or more *operations* (``spec.capabilities`` — e.g. a database
reader's ``list_tables`` / ``query``). Each operation becomes one LLM-callable ``ToolSpec`` named
``<binding>__<operation>`` so the model selects an operation; the loop maps the call back to
``registry.execute(instance, {"operation": <op>, **args})``. Mirrors the legacy
``agent_tool_schemas`` shape but is descriptor-driven rather than a static dict.
"""

from __future__ import annotations

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
                name=f"{binding}__{op_name}",
                description=op.get("description") or f"{name}: {op_name}",
                parameters=_json_schema(op.get("parameters")),
                binding=binding,
                operation=op_name,
            )
        )
    return out
