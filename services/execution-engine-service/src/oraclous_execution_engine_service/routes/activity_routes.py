"""Engine activity + usage read routes (routes layer) — parse → ONE service call → map.

``GET /v1/engine/activity`` returns the org's most-recent provenance/audit events (newest-first,
``limit``-capped). ``GET /v1/engine/usage`` returns the org's RAW per-action usage counts (ADR-009 —
counts, never a price/USD/credits), optionally over a ``since`` window. Both are strictly org-scoped
to the caller's principal; an auth/scope failure is 401. They mount under the same ``/v1/engine``
prefix the engine already exposes, so the gateway proxying ``/v1/engine`` fronts them automatically.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from oraclous_execution_engine_service.core.dependencies import ActivityServiceDep, PrincipalDep
from oraclous_execution_engine_service.schema.engine_schemas import (
    ActivityEvent,
    ActivityResponse,
    UsageCount,
    UsageResponse,
)
from oraclous_execution_engine_service.services.activity_service import (
    DEFAULT_ACTIVITY_LIMIT,
    MAX_ACTIVITY_LIMIT,
    ActivityError,
)

router = APIRouter(prefix="/v1/engine", tags=["engine-activity"])


@router.get("/activity", response_model=ActivityResponse)
async def list_activity(
    principal: PrincipalDep,
    service: ActivityServiceDep,
    limit: Annotated[
        int, Query(ge=1, le=MAX_ACTIVITY_LIMIT, description="max events")
    ] = DEFAULT_ACTIVITY_LIMIT,
) -> ActivityResponse:
    """The org's most-recent provenance events, newest-first (org-scoped to the caller only)."""
    try:
        rows = await service.recent_activity(principal, limit=limit)
    except ActivityError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    events = [ActivityEvent.model_validate(r) for r in rows]
    return ActivityResponse(events=events, total=len(events))


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    principal: PrincipalDep,
    service: ActivityServiceDep,
    since: Annotated[
        datetime | None, Query(description="window lower-bound (UTC); omit for all-time")
    ] = None,
) -> UsageResponse:
    """The org's RAW per-action usage counts (ADR-009 — counts, never money), org-scoped."""
    try:
        counts = await service.usage(principal, since=since)
    except ActivityError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    usage = [UsageCount(action=action, count=count) for action, count in counts]
    return UsageResponse(usage=usage, total_events=sum(u.count for u in usage), since=since)
