"""Activity + usage read service (ORAA-4 §21 services layer).

Two read surfaces over the engine's ``engine_provenance`` audit log: the org's recent activity feed,
and its RAW per-action usage counts. Both are org-scoped from the authenticated principal ONLY
(ADR-006, fail-closed) — a tenant never sees another's events or counts. Usage is a RAW signal
(counts), never a price/USD/credits (ADR-009); pricing is a downstream rate-table concern.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from oraclous_governance import Principal

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.provenance import EngineProvenanceEvent
from oraclous_execution_engine_service.repositories.provenance_repository import (
    ProvenanceRepository,
)

# The activity feed is a bounded read: the route's default + the hard cap a client cannot exceed.
DEFAULT_ACTIVITY_LIMIT = 50
MAX_ACTIVITY_LIMIT = 200


class ActivityError(Exception):
    """An activity/usage read could not be served (e.g. no organisation scope). Maps to HTTP 4xx."""


class ActivityService:
    def __init__(self, *, provenance: ProvenanceRepository) -> None:
        self._provenance = provenance

    async def recent_activity(
        self, principal: Principal, *, limit: int = DEFAULT_ACTIVITY_LIMIT
    ) -> list[EngineProvenanceEvent]:
        """The org's most-recent provenance events, newest-first. ``limit`` is clamped to
        ``[1, MAX_ACTIVITY_LIMIT]`` so a caller can never drain the table."""
        org_id = self._require_org(principal)
        bounded = max(1, min(limit, MAX_ACTIVITY_LIMIT))
        # ADR-030 §3: bind the org so the engine_provenance read runs with the GUC set on the
        # org-bound engine — else FORCE'd RLS fails it closed to zero rows (T1-M1).
        with org_scope(org_id):
            return await self._provenance.recent(org_id, limit=bounded)

    async def usage(
        self, principal: Principal, *, since: datetime | None = None
    ) -> list[tuple[str, int]]:
        """The org's RAW per-action usage counts (ADR-009 — counts, never money), optionally over a
        ``since`` window. Org-scoped to the caller only."""
        org_id = self._require_org(principal)
        # ADR-030 §3: bind the org so the aggregate read runs with the GUC set (else RLS → zero).
        with org_scope(org_id):
            return await self._provenance.usage_by_action(org_id, since=since)

    @staticmethod
    def _require_org(principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:
            raise ActivityError("authenticated principal has no organisation scope")
        return principal.organisation_id
