"""Executor factory (ORAA-4 §21 domain layer; reshape of legacy ``app/tools/factory.py`` +
``app/tools/registry.py`` in-process executor cache).

Maps a tool descriptor to its concrete executor class by the descriptor's deterministic id. Only
tools whose executor is implemented in this release are registered; a descriptor with no registered
executor raises ``NoExecutorError`` (surfaced as a configuration error, never a silent no-op).
"""

from __future__ import annotations

from typing import Any

from oraclous_capability_registry_service.domain.connectors.postgresql import PostgreSQLReader
from oraclous_capability_registry_service.domain.executors.base import BaseToolExecutor
from oraclous_capability_registry_service.domain.plugins.builtin import PostgreSQLReaderPlugin


class NoExecutorError(Exception):
    """No executor is registered for the descriptor (the tool is registered but not executable)."""


# descriptor id (deterministic tool UUIDv5, as str) -> executor class
_EXECUTORS: dict[str, type[BaseToolExecutor]] = {
    PostgreSQLReaderPlugin.plugin_id(): PostgreSQLReader,
}


def has_executor(descriptor: dict[str, Any]) -> bool:
    return descriptor.get("id") in _EXECUTORS


def create_executor(descriptor: dict[str, Any]) -> BaseToolExecutor:
    executor_cls = _EXECUTORS.get(descriptor.get("id"))
    if executor_cls is None:
        raise NoExecutorError(
            f"no executor registered for tool '{descriptor.get('id')}'"
            f" ({(descriptor.get('metadata') or {}).get('name')})"
        )
    return executor_cls(descriptor)
