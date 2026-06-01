import hashlib
import json
import logging
import re
from typing import List, Optional
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind
from app.services.capability_registry import CapabilityRegistryService

logger = logging.getLogger(__name__)


def _server_slug(server_endpoint: str) -> str:
    parsed = urlparse(server_endpoint)
    netloc = parsed.netloc or server_endpoint
    return re.sub(r"[^a-zA-Z0-9_]", "-", netloc)


def _compute_content_hash(descriptor: dict) -> str:
    """sha256 over canonical JSON of descriptor with version.hash excluded from input."""
    to_hash = {
        k: (
            {vk: vv for vk, vv in v.items() if vk != "hash"}
            if k == "version"
            else v
        )
        for k, v in descriptor.items()
    }
    canonical = json.dumps(to_hash, sort_keys=True, separators=(",", ":"))
    hex_digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"sha256:{hex_digest}"


def translate_mcp_tool(mcp_tool: dict, server_endpoint: str) -> Optional[dict]:
    """
    Translate an MCP tool spec into an OHM kind:tool descriptor.
    Returns None and logs WARNING when the tool cannot be translated.
    """
    name = mcp_tool.get("name")
    if not name:
        logger.warning(
            "MCP tool is untranslatable (missing 'name'): %s",
            json.dumps(mcp_tool),
        )
        return None

    slug = _server_slug(server_endpoint)
    descriptor_id = f"mcp-{slug}-{name}"

    descriptor: dict = {
        "kind": "tool",
        "id": descriptor_id,
        "version": {
            "hash": "",
            "tags": ["1.0.0"],
        },
        "metadata": {
            "name": name,
            "description": mcp_tool.get("description", ""),
        },
        "spec": {
            "implementation": {
                "type": "mcp",
                "endpoint": server_endpoint,
            },
            "input_schema": mcp_tool.get("inputSchema", {"type": "object"}),
            "output_schema": {"type": "object"},
            "credential_requirements": mcp_tool.get("x-credential-requirements", []),
        },
    }

    descriptor["version"]["hash"] = _compute_content_hash(descriptor)
    return descriptor


async def import_mcp_server(
    server_spec: dict,
    org_id: UUID,
    session: AsyncSession,
) -> List[CapabilityDescriptorDB]:
    """
    Import all translatable tools from an MCP server spec into the capability registry.
    Idempotent: re-importing returns existing rows without creating duplicates.
    Untranslatable tools are skipped with a WARNING log.
    """
    svc = CapabilityRegistryService(session)
    server_endpoint = server_spec.get("url", "")
    if not server_endpoint:
        logger.warning(
            "MCP server spec has no 'url'; cannot import tools: %s",
            json.dumps(server_spec),
        )
        return []
    tools = server_spec.get("tools", [])

    result: List[CapabilityDescriptorDB] = []

    for mcp_tool in tools:
        descriptor = translate_mcp_tool(mcp_tool, server_endpoint)
        if descriptor is None:
            continue

        descriptor_id = descriptor["id"]
        content_hash = descriptor["version"]["hash"]

        existing = await svc.search_by_descriptor(org_id, {"id": descriptor_id})
        if existing:
            result.append(existing[0])
            continue

        row = await svc.create(
            org_id=org_id,
            kind=DescriptorKind.TOOL,
            descriptor=descriptor,
            content_hash=content_hash,
        )
        result.append(row)

    return result


class MCPInboundAdapter:
    """Namespace for MCP inbound adapter functions."""

    translate_mcp_tool = staticmethod(translate_mcp_tool)
    import_mcp_server = staticmethod(import_mcp_server)
