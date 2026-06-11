"""Graph use-cases (ORAA-4 §21 services layer — all business logic lives here, not in routes).

Replaces the legacy inline `GraphNodeService` stub that lived *inside* the route module
(a §21 violation). The org scope is enforced fail-closed in the repository
(`enforced_organisation_id`); this layer adds the per-user ownership gate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid

from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
)

logger = logging.getLogger(__name__)


class GraphNotFound(Exception):
    """Raised when a graph is not visible to the caller (wrong org or not owned). Maps to 404."""


class GraphService:
    """Graph CRUD use-cases. Owner-gated on top of org-scoping."""

    def __init__(
        self, repo: GraphRepository, write_repo: GraphWriteRepository | None = None
    ) -> None:
        self._repo = repo
        self._write_repo = write_repo

    async def _with_live_counts(self, graph: Graph) -> Graph:
        """Overlay the LIVE Neo4j node/relationship counts onto the graph (org+graph scoped).

        The `node_count`/`relationship_count` Postgres columns are stale (ingestion writes real
        nodes to Neo4j, never back to Postgres), so when a Neo4j-backed write repo is wired we
        replace them with the live counts. When it is not (unit tests / unconfigured substrate),
        the graph is returned unchanged — the Postgres-column fallback.
        """
        if self._write_repo is None:
            return graph
        try:
            node_count, relationship_count = await asyncio.to_thread(
                self._write_repo.count_for_graph,
                graph_id=str(graph.id),
                organisation_id=str(graph.organisation_id),
            )
        except Exception as exc:  # noqa: BLE001 — degrade-don't-crash: a Neo4j hiccup must not
            # turn a Postgres-backed metadata read into a 500; fall back to the stored columns.
            logger.warning(
                "live Neo4j count failed for graph %s; using stored counts: %s", graph.id, exc
            )
            return graph
        return dataclasses.replace(
            graph, node_count=node_count, relationship_count=relationship_count
        )

    async def create_graph(
        self, *, user_id: uuid.UUID, name: str, description: str | None
    ) -> Graph:
        return await self._repo.create(user_id=user_id, name=name, description=description)

    async def list_graphs(self, *, user_id: uuid.UUID) -> list[Graph]:
        graphs = await self._repo.list_for_user(user_id=user_id)
        return [await self._with_live_counts(g) for g in graphs]

    async def _owned_or_404(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> Graph:
        """Owner gate (no live-count overlay) — the raw stored graph or a 404-mapped error."""
        graph = await self._repo.get(graph_id)
        if graph is None or graph.user_id != user_id:
            raise GraphNotFound(str(graph_id))
        return graph

    async def get_graph(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> Graph:
        graph = await self._owned_or_404(graph_id=graph_id, user_id=user_id)
        return await self._with_live_counts(graph)

    async def update_graph(
        self, *, graph_id: uuid.UUID, user_id: uuid.UUID, name: str | None, description: str | None
    ) -> Graph:
        # owner gate first (a graph owned by another user in the same org -> 404, no leak)
        await self._owned_or_404(graph_id=graph_id, user_id=user_id)
        updated = await self._repo.update(graph_id, name=name, description=description)
        if updated is None:
            raise GraphNotFound(str(graph_id))
        return updated

    async def delete_graph(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> None:
        # Owner gate first (a graph in another org/owner -> 404, no leak). Org-scoping is enforced
        # by `_owned_or_404`; the Neo4j cascade below is graph_id-scoped to the owned graph.
        await self._owned_or_404(graph_id=graph_id, user_id=user_id)
        # Cascade the graph's Neo4j nodes/edges before the Postgres row, so a Neo4j failure aborts
        # the delete (surfaced, not swallowed) and the metadata row survives to retry — never an
        # orphaned graph (the leak ORAA-261 fixes). When the substrate is unwired (unit tests /
        # Neo4j unconfigured) `write_repo` is None and only the Postgres row is removed.
        if self._write_repo is not None:
            await asyncio.to_thread(self._write_repo.delete_graph_nodes, graph_id=str(graph_id))
        await self._repo.delete(graph_id)


# Legacy module-level name preserved (existing tests patch `GraphNodeService`).
GraphNodeService = GraphService
