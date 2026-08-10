"""Plugin discovery + manifest seeding (services layer; reshape of legacy
``sync_plugins_to_registry``).

Seeds every registered capability-kind plugin's OHM descriptor into an organisation's registry.
Idempotent: matched by the descriptor's deterministic id, a re-run is a no-op (``unchanged``); a
changed manifest updates in place. Returns the per-plugin sync status for observability/tests.

Retirement (#770): ``sync_plugins`` is upsert-only, so removing a plugin class alone leaves
already-seeded orgs offering a dead descriptor. When the caller supplies an
``instance_repository``, the descriptors in ``_RETIRED_DESCRIPTOR_IDS`` are retired for that org:
instances first (``tool_instances.descriptor_id`` is ``ondelete=RESTRICT``), then the descriptor
row, idempotently — a re-run with nothing left to retire is a no-op.
"""

from __future__ import annotations

import uuid

from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityInUseError,
    CapabilityRepository,
)
from oraclous_capability_registry_service.repositories.instance_repository import (
    InstanceRepository,
)

#: Seeded descriptors whose plugin class is gone — the ids are pinned literally because the class
#: no longer exists to derive them from. ``d5c5a63d-…`` was ``GoogleDriveReaderPlugin.plugin_id()``
#: (#770): it advertised ``list_files``/``read_file`` and could execute neither (no executor, no
#: connector module), so no working configuration is lost by deleting its instances.
_RETIRED_DESCRIPTOR_IDS: tuple[uuid.UUID, ...] = (
    uuid.UUID("d5c5a63d-0979-59e7-b82c-46714c204d4e"),
)


async def sync_plugins(
    *,
    repository: CapabilityRepository,
    organisation_id: uuid.UUID,
    instance_repository: InstanceRepository | None = None,
) -> dict[str, str]:
    """Seed all registered plugins into ``organisation_id``. Returns {plugin_id: status}.

    With ``instance_repository`` supplied, retired descriptors are also removed for this org and
    reported as ``retired``; without it (the pre-#770 signature) seeding behaves exactly as before.
    """
    statuses: dict[str, str] = {}
    if instance_repository is not None:
        for retired_id in _RETIRED_DESCRIPTOR_IDS:
            try:
                await instance_repository.delete_by_capability(retired_id, organisation_id)
                if await repository.delete(retired_id, organisation_id):
                    statuses[str(retired_id)] = "retired"
            except CapabilityInUseError:
                # A DIFFERENT org still holds instances of this descriptor (ondelete=RESTRICT
                # sees every org; the org-scoped delete above only clears this one). Leave the
                # row for a later sweep — a blocked retirement never breaks the catalogue seed.
                statuses[str(retired_id)] = "retire_blocked"
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
