"""Deterministic tool ids (domain layer; port of legacy
``oraclous-core-service/app/utils/tool_id_generator.py``).

A tool's id is a deterministic UUIDv5 of its identity (name/version/category) so the same tool gets
the same id across deployments — re-seeding the registry is idempotent and a descriptor's id is
stable. Pure, no I/O.
"""

from __future__ import annotations

import uuid

# Fixed namespace for Oraclous tools (legacy parity — do not change; ids would shift).
_ORACLOUS_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def generate_tool_id(
    name: str,
    version: str = "1.0.0",
    category: str = "",
    namespace: str | None = None,
) -> uuid.UUID:
    """Return the deterministic UUIDv5 for a tool from its identity attributes."""
    prefix = namespace or "oraclous"
    tool_string = f"{prefix}:{name}:{version}:{category}".lower().strip().replace(" ", "-")
    return uuid.uuid5(_ORACLOUS_NAMESPACE, tool_string)
