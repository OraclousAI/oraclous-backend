"""Registry provenance read routes (routes layer) — parse → ONE service call → map.

``GET /api/v1/provenance`` returns the org's most-recent §3.7 audit events (newest-first,
``limit``-capped): ``capability.invoke`` on every dispatch, ``capability.refused`` on every
pre-dispatch readiness gate (#826, the 24 August + 11 September rulings). Strictly org-scoped to the
caller's principal; a missing org scope is 401.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from oraclous_capability_registry_service.core.dependencies import (
    PrincipalDep,
    RegistryProvenanceServiceDep,
)
from oraclous_capability_registry_service.schema.provenance_schema import (
    ProvenanceEvent,
    ProvenanceListResponse,
)
from oraclous_capability_registry_service.services.registry_provenance_service import (
    DEFAULT_PROVENANCE_LIMIT,
    MAX_PROVENANCE_LIMIT,
    ProvenanceReadError,
)

router = APIRouter(prefix="/api/v1", tags=["provenance"])


@router.get("/provenance", response_model=ProvenanceListResponse)
async def list_provenance(
    principal: PrincipalDep,
    service: RegistryProvenanceServiceDep,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PROVENANCE_LIMIT, description="max events")
    ] = DEFAULT_PROVENANCE_LIMIT,
) -> ProvenanceListResponse:
    """The org's most-recent provenance events, newest-first (org-scoped to the caller only)."""
    try:
        rows, total = await service.list_events(principal, limit=limit)
    except ProvenanceReadError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    events = [ProvenanceEvent.model_validate(r) for r in rows]
    return ProvenanceListResponse(events=events, total=total)
