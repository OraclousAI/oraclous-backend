"""Provider-formatted tool schemas sourced from the capability registry.

ORAA-76: Replaces the static _TOOL_SCHEMAS dict with registry-backed schema
generation. The capability registry is the single source of OHM descriptors;
this module translates them to OpenAI / Anthropic wire format on demand.

Two output formats:

- ``openai``  — {type: 'function', function: {name, description, parameters}}
- ``anthropic`` — {name, description, input_schema}

graph_id is stripped from every schema: the executor binds it before dispatch
so the LLM never sees it as a callable parameter.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from app.services.capability_registry_client import CapabilityRegistryClient

ProviderFormat = Literal["openai", "anthropic"]


def _strip_graph_id(input_schema: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *input_schema* with graph_id removed from
    properties and required.  No-op when graph_id is absent."""
    if "graph_id" not in input_schema.get("properties", {}) and "graph_id" not in input_schema.get(
        "required", []
    ):
        return input_schema
    schema = copy.copy(input_schema)
    if "properties" in schema and "graph_id" in schema["properties"]:
        schema["properties"] = {k: v for k, v in schema["properties"].items() if k != "graph_id"}
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r != "graph_id"]
    return schema


def _ohm_to_openai(descriptor: dict[str, Any]) -> dict[str, Any]:
    parameters = _strip_graph_id(descriptor["spec"]["input_schema"])
    return {
        "type": "function",
        "function": {
            "name": descriptor["metadata"]["name"],
            "description": descriptor["metadata"]["description"],
            "parameters": parameters,
        },
    }


def _ohm_to_anthropic(descriptor: dict[str, Any]) -> dict[str, Any]:
    input_schema = _strip_graph_id(descriptor["spec"]["input_schema"])
    return {
        "name": descriptor["metadata"]["name"],
        "description": descriptor["metadata"]["description"],
        "input_schema": input_schema,
    }


async def tool_schemas_from_registry(
    allowed_tools: list[str] | set[str],
    provider_format: ProviderFormat,
    *,
    registry_client: CapabilityRegistryClient,
) -> list[dict[str, Any]]:
    """Return provider-formatted schemas by fetching OHM descriptors from the
    capability registry for each tool in *allowed_tools*.

    Tools absent from the registry are silently dropped (preserves the
    pre-ORAA-76 contract of tool_schemas_for).  An empty allowlist returns []
    without calling the registry.  Registry errors propagate to the caller.
    """
    tool_list = list(allowed_tools)
    if not tool_list:
        return []

    convert = _ohm_to_openai if provider_format == "openai" else _ohm_to_anthropic
    result: list[dict[str, Any]] = []
    for name in tool_list:
        descriptor = await registry_client.get_tool_descriptor(name)
        if descriptor is not None:
            result.append(convert(descriptor))
    return result
