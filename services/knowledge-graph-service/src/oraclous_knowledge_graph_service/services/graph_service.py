"""Graph use-cases (ORAA-4 §21 services layer — all business logic lives here, not in routes).

Replaces the legacy inline `GraphNodeService` stub that lived *inside* the route module
(a §21 violation). The org scope is enforced fail-closed in the repository
(`enforced_organisation_id`); this layer adds the per-user ownership gate.
"""

from __future__ import annotations

import uuid

from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository


class GraphNotFound(Exception):
    """Raised when a graph is not visible to the caller (wrong org or not owned). Maps to 404."""


class GraphService:
    """Graph CRUD use-cases. Owner-gated on top of org-scoping."""

    def __init__(self, repo: GraphRepository) -> None:
        self._repo = repo

    async def create_graph(
        self, *, user_id: uuid.UUID, name: str, description: str | None
    ) -> Graph:
        return await self._repo.create(user_id=user_id, name=name, description=description)

    async def list_graphs(self, *, user_id: uuid.UUID) -> list[Graph]:
        return await self._repo.list_for_user(user_id=user_id)

    async def get_graph(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> Graph:
        graph = await self._repo.get(graph_id)
        if graph is None or graph.user_id != user_id:
            raise GraphNotFound(str(graph_id))
        return graph

    async def update_graph(
        self, *, graph_id: uuid.UUID, user_id: uuid.UUID, name: str | None, description: str | None
    ) -> Graph:
        # owner gate first (a graph owned by another user in the same org -> 404, no leak)
        await self.get_graph(graph_id=graph_id, user_id=user_id)
        updated = await self._repo.update(graph_id, name=name, description=description)
        if updated is None:
            raise GraphNotFound(str(graph_id))
        return updated

    async def delete_graph(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self.get_graph(graph_id=graph_id, user_id=user_id)
        await self._repo.delete(graph_id)


# Legacy module-level name preserved (existing tests patch `GraphNodeService`).
GraphNodeService = GraphService
