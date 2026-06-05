"""Capability-kind plugins (ORAA-4 §21 domain layer).

Importing this package imports ``builtin`` so the built-in tool plugins register themselves against
``plugin_registry`` (import side-effect = discovery).
"""

from __future__ import annotations

from oraclous_capability_registry_service.domain.plugins import builtin  # noqa: F401 (registers)
from oraclous_capability_registry_service.domain.plugins.base import (
    CapabilityKindPlugin,
    PluginRegistry,
    plugin_registry,
)

__all__ = ["CapabilityKindPlugin", "PluginRegistry", "plugin_registry"]
