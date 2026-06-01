from __future__ import annotations

import hashlib
import json
import uuid
from abc import ABC, abstractmethod
from typing import List, Type

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind


class CapabilityKindPlugin(ABC):
    """Base class for all capability-kind plugins.

    Subclasses register themselves at module scope via:
        plugin_registry.register(MyPlugin)

    The three classmethods form the plugin contract; no core-file edits are needed
    to add a new capability kind to the discoverable set.
    """

    @classmethod
    @abstractmethod
    def get_ohm_descriptor(cls) -> dict:
        """Return the OHM-format descriptor dict for this capability kind."""

    @classmethod
    @abstractmethod
    def get_kind(cls) -> DescriptorKind:
        """Return the DescriptorKind enum value for this capability kind."""

    @classmethod
    @abstractmethod
    def get_plugin_id(cls) -> str:
        """Return the stable plugin ID (must match descriptor['id'])."""


class _PluginRegistry:
    """In-process registry of capability-kind plugin classes."""

    def __init__(self) -> None:
        self._plugins: dict[str, Type[CapabilityKindPlugin]] = {}

    def register(self, plugin_cls: Type[CapabilityKindPlugin]) -> None:
        """Register a plugin class by its plugin_id."""
        self._plugins[plugin_cls.get_plugin_id()] = plugin_cls

    def unregister(self, plugin_cls: Type[CapabilityKindPlugin]) -> None:
        """Unregister a plugin class (used in test teardown)."""
        self._plugins.pop(plugin_cls.get_plugin_id(), None)


plugin_registry = _PluginRegistry()


def discover_registered_plugins() -> List[Type[CapabilityKindPlugin]]:
    """Return all currently registered plugin classes."""
    return list(plugin_registry._plugins.values())


async def sync_plugins_to_registry(
    org_id: uuid.UUID,
    session: AsyncSession,
) -> List[CapabilityDescriptorDB]:
    """Persist every registered plugin's OHM descriptor to capability_descriptor for org_id.

    Idempotent: existing rows (matched by descriptor["id"]) are not duplicated.
    Returns the rows synced this call (created or already present).
    """
    from app.services.capability_registry import CapabilityRegistryService

    svc = CapabilityRegistryService(session)
    existing = await svc.list_by_org(org_id)
    existing_by_id = {r.descriptor.get("id"): r for r in existing}

    synced: List[CapabilityDescriptorDB] = []
    for plugin_cls in discover_registered_plugins():
        pid = plugin_cls.get_plugin_id()
        if pid in existing_by_id:
            synced.append(existing_by_id[pid])
            continue
        descriptor = plugin_cls.get_ohm_descriptor()
        to_hash = {
            k: (
                {vk: vv for vk, vv in v.items() if vk != "hash"}
                if k == "version"
                else v
            )
            for k, v in descriptor.items()
        }
        canonical = json.dumps(to_hash, sort_keys=True, separators=(",", ":"))
        content_hash = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
        descriptor["version"]["hash"] = content_hash
        row = await svc.create(
            org_id=org_id,
            kind=plugin_cls.get_kind(),
            descriptor=descriptor,
            content_hash=content_hash,
        )
        synced.append(row)
    return synced
