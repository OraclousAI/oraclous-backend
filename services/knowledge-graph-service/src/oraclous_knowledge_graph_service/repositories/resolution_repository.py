"""Entity-resolution audit repository (ORAA-4 §21 repositories layer — the only home for the
`entity_resolutions` SQL).

Records + reads the HITL verdict on a `SAME_AS_CANDIDATE` pair (#279). Org-scoped via
`enforced_organisation_id()` (ADR-006, fail-closed) — a verdict is always written under, and read
within, the caller's bound organisation. The `(organisation_id, graph_id, candidate_id)` unique key
makes a decision idempotent and lets the service detect a concurrent second-reviewer conflict. SQL
lives here only.
"""

from __future__ import annotations

import uuid

from oraclous_substrate.access import enforced_organisation_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_knowledge_graph_service.domain.resolution import ResolutionAction
from oraclous_knowledge_graph_service.repositories.models import EntityResolution


class ResolutionRepository:
    """Org-scoped audit log over `entity_resolutions`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _org(self) -> uuid.UUID:
        return uuid.UUID(enforced_organisation_id())

    async def find(self, *, graph_id: uuid.UUID, candidate_id: str) -> EntityResolution | None:
        """The existing verdict for this pair in this graph (org-scoped), or None."""
        stmt = select(EntityResolution).where(
            EntityResolution.organisation_id == self._org(),
            EntityResolution.graph_id == graph_id,
            EntityResolution.candidate_id == candidate_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record(
        self,
        *,
        graph_id: uuid.UUID,
        candidate_id: str,
        node_id_a: str,
        node_id_b: str,
        action: ResolutionAction,
        canonical_node_id: str | None,
        decided_by: uuid.UUID,
        other_graph_id: uuid.UUID | None = None,
    ) -> EntityResolution:
        """Insert the verdict row. The caller (service) has already checked there is no conflicting
        prior verdict; the DB unique key is the backstop against a racing duplicate insert. For a
        cross-graph verdict the caller passes the canonicalised pair (`graph_id` the smaller of the
        two, `other_graph_id` the larger) so a verdict from either direction keys the same row."""
        row = EntityResolution(
            organisation_id=self._org(),
            graph_id=graph_id,
            other_graph_id=other_graph_id,
            candidate_id=candidate_id,
            node_id_a=node_id_a,
            node_id_b=node_id_b,
            action=action.value,
            canonical_node_id=canonical_node_id,
            decided_by=decided_by,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row
