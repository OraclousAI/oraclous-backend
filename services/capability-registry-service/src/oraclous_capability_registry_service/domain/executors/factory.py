"""Executor factory (ORAA-4 §21 domain layer; reshape of legacy ``app/tools/factory.py`` +
``app/tools/registry.py`` in-process executor cache).

Maps a tool descriptor to its concrete executor class by the descriptor's deterministic id. Only
tools whose executor is implemented in this release are registered; a descriptor with no registered
executor raises ``NoExecutorError`` (surfaced as a configuration error, never a silent no-op).
"""

from __future__ import annotations

from typing import Any

from oraclous_capability_registry_service.domain.connectors.find_similar import (
    FindSimilarConnector,
)
from oraclous_capability_registry_service.domain.connectors.github import GitHubReader
from oraclous_capability_registry_service.domain.connectors.graph_ingest import (
    GraphIngestConnector,
)
from oraclous_capability_registry_service.domain.connectors.knowledge_retriever import (
    KnowledgeRetrieverConnector,
)
from oraclous_capability_registry_service.domain.connectors.mcp import McpToolExecutor
from oraclous_capability_registry_service.domain.connectors.mysql import MySQLReader
from oraclous_capability_registry_service.domain.connectors.notion import NotionReader
from oraclous_capability_registry_service.domain.connectors.postgresql import PostgreSQLReader
from oraclous_capability_registry_service.domain.executors.base import BaseToolExecutor
from oraclous_capability_registry_service.domain.plugins.builtin import (
    FindSimilarPlugin,
    GitHubReaderPlugin,
    GraphIngestPlugin,
    KnowledgeRetrieverPlugin,
    MySQLReaderPlugin,
    NotionReaderPlugin,
    PostgreSQLReaderPlugin,
)


class NoExecutorError(Exception):
    """No executor is registered for the descriptor (the tool is registered but not executable)."""


# descriptor id (deterministic tool UUIDv5, as str) -> executor class. The Google Drive Reader's
# live OAuth connector is deferred (no key-free smoke); its descriptor stays registered.
_EXECUTORS: dict[str, type[BaseToolExecutor]] = {
    PostgreSQLReaderPlugin.plugin_id(): PostgreSQLReader,
    MySQLReaderPlugin.plugin_id(): MySQLReader,
    NotionReaderPlugin.plugin_id(): NotionReader,
    GitHubReaderPlugin.plugin_id(): GitHubReader,
    KnowledgeRetrieverPlugin.plugin_id(): KnowledgeRetrieverConnector,
    FindSimilarPlugin.plugin_id(): FindSimilarConnector,
    GraphIngestPlugin.plugin_id(): GraphIngestConnector,
}


def _is_mcp(descriptor: dict[str, Any]) -> bool:
    """A dynamically-imported external MCP tool dispatches by ``spec.type`` (no fixed plugin id,
    since
    each imported tool is a distinct per-org descriptor pointing at its own server)."""
    return (descriptor.get("spec") or {}).get("type") == "mcp"


def has_executor(descriptor: dict[str, Any]) -> bool:
    return descriptor.get("id") in _EXECUTORS or _is_mcp(descriptor)


def create_executor(descriptor: dict[str, Any]) -> BaseToolExecutor:
    executor_cls = _EXECUTORS.get(descriptor.get("id"))
    if executor_cls is not None:
        return executor_cls(descriptor)
    if _is_mcp(descriptor):
        return McpToolExecutor(descriptor)
    raise NoExecutorError(
        f"no executor registered for tool '{descriptor.get('id')}'"
        f" ({(descriptor.get('metadata') or {}).get('name')})"
    )
