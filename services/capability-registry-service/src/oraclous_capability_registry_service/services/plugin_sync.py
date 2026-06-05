"""Plugin discovery + manifest seeding (ORAA-4 §21 services layer; reshape of legacy
``sync_plugins_to_registry``).

Seeds every registered capability-kind plugin's OHM descriptor into an organisation's registry.
Idempotent: matched by the descriptor's deterministic id, a re-run is a no-op (``unchanged``); a
changed manifest updates in place. Returns the per-plugin sync status for observability/tests.
"""

from __future__ import annotations

import uuid

from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)


async def sync_plugins(
    *, repository: CapabilityRepository, organisation_id: uuid.UUID
) -> dict[str, str]:
    """Seed all registered plugins into ``organisation_id``. Returns {plugin_id: status}."""
    statuses: dict[str, str] = {}
    for plugin_cls in plugin_registry.discover():
        descriptor = plugin_cls.descriptor()
        descriptor_id = uuid.UUID(plugin_cls.plugin_id())
        _, status = await repository.upsert_by_id(
            organisation_id=organisation_id,
            descriptor_id=descriptor_id,
            kind=plugin_cls.kind(),
            descriptor=descriptor,
        )
        statuses[plugin_cls.plugin_id()] = status
    return statuses
