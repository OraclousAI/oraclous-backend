"""Registry provenance read service (services layer; #826, the 11 September ruling).

The org's own read surface over ``registry_provenance`` — this service's §3.7 audit log. Org-scoped
to the authenticated principal ONLY (ADR-006, fail-closed): a tenant never sees another's events.
The repository binds the org-GUC itself (it is a STRICT table, like ``executions``), so this service
carries no org_scope of its own — it only resolves the caller's org and clamps the page size.
"""

from __future__ import annotations

import uuid

from oraclous_governance import Principal

from oraclous_capability_registry_service.models.registry_provenance import RegistryProvenance
from oraclous_capability_registry_service.repositories.registry_provenance_repository import (
    RegistryProvenanceRepository,
)

# The read is a bounded page: the route's default + the hard cap a client cannot exceed (mirrors
# the engine activity feed's DEFAULT_ACTIVITY_LIMIT / MAX_ACTIVITY_LIMIT).
DEFAULT_PROVENANCE_LIMIT = 50
MAX_PROVENANCE_LIMIT = 200


class ProvenanceReadError(Exception):
    """A provenance read could not be served (e.g. no organisation scope). Maps to HTTP 401."""


class RegistryProvenanceService:
    def __init__(self, *, provenance: RegistryProvenanceRepository) -> None:
        self._provenance = provenance

    async def list_events(
        self, principal: Principal, *, limit: int = DEFAULT_PROVENANCE_LIMIT
    ) -> tuple[list[RegistryProvenance], int]:
        """The org's most-recent events (newest-first, ``limit``-capped) and the org's full count —
        the count is independent of ``limit``, never merely ``len(events)``."""
        org_id = self._require_org(principal)
        bounded = max(1, min(limit, MAX_PROVENANCE_LIMIT))
        events = await self._provenance.list_by_org(org_id, limit=bounded)
        total = await self._provenance.count_by_org(org_id)
        return events, total

    @staticmethod
    def _require_org(principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:
            raise ProvenanceReadError("authenticated principal has no organisation scope")
        return principal.organisation_id
