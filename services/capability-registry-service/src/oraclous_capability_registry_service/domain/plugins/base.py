"""Capability-kind plugin contract + registry (domain layer; reshape of legacy
``oraclous-core-service/app/tools/plugin.py``).

A plugin contributes one OHM descriptor to the registry. Built-in tools register themselves at
module import (``builtin.py``); discovery returns the registered set so the startup hook can seed
them idempotently. Pure: a plugin describes a capability, it does not perform I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from oraclous_capability_registry_service.models.enums import DescriptorKind


class CapabilityKindPlugin(ABC):
    """Base class for capability-kind plugins. The three classmethods form the contract."""

    @classmethod
    @abstractmethod
    def plugin_id(cls) -> str:
        """Stable plugin id — must equal ``descriptor()["id"]``."""

    @classmethod
    @abstractmethod
    def kind(cls) -> DescriptorKind:
        """The DescriptorKind this plugin contributes."""

    @classmethod
    @abstractmethod
    def descriptor(cls) -> dict:
        """The OHM descriptor dict for this capability."""


class PluginRegistry:
    """In-process registry of capability-kind plugin classes (keyed by plugin_id)."""

    def __init__(self) -> None:
        self._plugins: dict[str, type[CapabilityKindPlugin]] = {}

    def register(self, plugin_cls: type[CapabilityKindPlugin]) -> type[CapabilityKindPlugin]:
        """Register a plugin class (usable as a class decorator). Returns the class."""
        self._plugins[plugin_cls.plugin_id()] = plugin_cls
        return plugin_cls

    def unregister(self, plugin_cls: type[CapabilityKindPlugin]) -> None:
        self._plugins.pop(plugin_cls.plugin_id(), None)

    def discover(self) -> list[type[CapabilityKindPlugin]]:
        """All registered plugin classes (order-stable by plugin_id for deterministic seeding)."""
        return [self._plugins[k] for k in sorted(self._plugins)]


# The process-wide registry. Built-in plugins register against it at import time.
plugin_registry = PluginRegistry()
