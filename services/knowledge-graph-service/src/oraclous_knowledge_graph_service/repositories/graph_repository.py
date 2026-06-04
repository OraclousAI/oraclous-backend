"""Graph repository (ORAA-4 §21 repositories layer — the only home for graph-metadata SQL).

Every query is scoped to the caller's organisation via
`oraclous_substrate.access.enforced_organisation_id()` (ADR-006 / ADR-012, fail-closed): the org id
is taken from the bound governance context, never from a request. The `user_id` owner gate is
applied on top for ownership semantics. SQL lives here only.
"""

from __future__ import annotations

import uuid

from oraclous_substrate.access import enforced_organisation_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.repositories.models import KnowledgeGraph


def _to_domain(row: KnowledgeGraph) -> Graph:
    return Graph(
        id=row.id,
        organisation_id=row.organisation_id,
        user_id=row.user_id,
        name=row.name,
        description=row.description,
        status=row.status,
        node_count=row.node_count,
        relationship_count=row.relationship_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class GraphRepository:
    """Org-scoped CRUD over `knowledge_graphs`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _org(self) -> uuid.UUID:
        return uuid.UUID(enforced_organisation_id())

    async def create(self, *, user_id: uuid.UUID, name: str, description: str | None) -> Graph:
        row = KnowledgeGraph(
            organisation_id=self._org(), user_id=user_id, name=name, description=description
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_domain(row)

    async def list_for_user(self, *, user_id: uuid.UUID) -> list[Graph]:
        stmt = (
            select(KnowledgeGraph)
            .where(
                KnowledgeGraph.organisation_id == self._org(),
                KnowledgeGraph.user_id == user_id,
            )
            .order_by(KnowledgeGraph.created_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]

    async def get(self, graph_id: uuid.UUID) -> Graph | None:
        """Org-scoped fetch — a graph in another org is invisible (returns None)."""
        stmt = select(KnowledgeGraph).where(
            KnowledgeGraph.id == graph_id,
            KnowledgeGraph.organisation_id == self._org(),
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def update(
        self, graph_id: uuid.UUID, *, name: str | None, description: str | None
    ) -> Graph | None:
        stmt = select(KnowledgeGraph).where(
            KnowledgeGraph.id == graph_id,
            KnowledgeGraph.organisation_id == self._org(),
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        if name is not None:
            row.name = name
        if description is not None:
            row.description = description
        await self._session.flush()
        await self._session.refresh(row)
        return _to_domain(row)

    async def delete(self, graph_id: uuid.UUID) -> bool:
        stmt = select(KnowledgeGraph).where(
            KnowledgeGraph.id == graph_id,
            KnowledgeGraph.organisation_id == self._org(),
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def get_ontology(self, graph_id: uuid.UUID) -> dict | None:
        """The graph's ontology config (from schema_config.ontology), org-scoped. None if unset."""
        row = await self._get_row(graph_id)
        if row is None:
            return None
        return (row.schema_config or {}).get("ontology")

    async def set_ontology(self, graph_id: uuid.UUID, ontology: dict) -> bool:
        row = await self._get_row(graph_id)
        if row is None:
            return False
        config = dict(row.schema_config or {})
        config["ontology"] = ontology
        row.schema_config = config
        await self._session.flush()
        return True

    async def _get_row(self, graph_id: uuid.UUID) -> KnowledgeGraph | None:
        stmt = select(KnowledgeGraph).where(
            KnowledgeGraph.id == graph_id,
            KnowledgeGraph.organisation_id == self._org(),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
