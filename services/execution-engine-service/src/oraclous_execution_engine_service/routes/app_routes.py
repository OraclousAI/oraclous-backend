"""App routes (routes layer) — parse → ONE service call → HTTP map. #932.

The Apps tab's HTTP surface: list what a caller can see (their own apps AND the Oraclous-provided
ones, in one page, told apart by ``origin``), open one, ask what still has to be connected, run it
on the caller's own key, and read that app's own run history.

There is deliberately NO create, update or delete. Turning a team into an app is deferred — not
every team is an app, and the conversion needs designing first — so the only apps that exist are
the ones Oraclous seeds.

NOTE: the static collection path (``/apps``) and the slug path (``/apps/by-slug/{slug}``) are
registered BEFORE ``/apps/{app_id}`` so neither is captured as an id.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse

from oraclous_execution_engine_service.core.dependencies import AppServiceDep, PrincipalDep
from oraclous_execution_engine_service.routes.preflight_response import preflight_409
from oraclous_execution_engine_service.schema.engine_schemas import (
    AppListItem,
    AppListOut,
    AppOut,
    AppRequirementsOut,
    RunAppRequest,
    TeamRunListItem,
    TeamRunListOut,
    TeamRunOut,
)
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunPreflightError,
)

router = APIRouter(prefix="/v1/engine", tags=["engine-apps"])


def _http(exc: TeamRunError) -> HTTPException:
    # #483 Option A: a STRUCTURED 422 detail (leak-safe machine token in `type`) so the gateway
    # maps it to VALIDATION_FAILED + a field-level issue; other statuses keep a plain detail.
    if exc.status_code == 422:
        return HTTPException(
            status_code=422,
            detail=[{"loc": ["body"], "type": exc.error_type, "msg": str(exc)}],
        )
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/apps", response_model=AppListOut)
async def list_apps(
    principal: PrincipalDep,
    service: AppServiceDep,
    limit: Annotated[int, Query()] = 50,
    offset: Annotated[int, Query()] = 0,
) -> AppListOut:
    """The Apps tab. ONE page holding both kinds — the caller's own apps and the ones Oraclous
    provides — each carrying ``origin`` so the console renders the split from the data rather than
    from which endpoint a row arrived on. Paginated (``limit`` default 50 / max 200, both clamped
    server-side; born bounded, WP-10). REGISTERED BEFORE ``/apps/{app_id}``."""
    try:
        rows, total = await service.list_for_org(principal, limit=limit, offset=offset)
    except TeamRunError as exc:  # a principal with no org → the contracted 403, not a 500
        raise _http(exc) from exc
    return AppListOut(apps=[AppListItem.model_validate(r) for r in rows], total=total)


@router.get("/apps/by-slug/{slug}", response_model=AppOut)
async def get_app_by_slug(slug: str, principal: PrincipalDep, service: AppServiceDep) -> AppOut:
    """The deep-link read: the console finds an Oraclous-provided app by its stable handle rather
    than by a uuid pasted into an environment variable. REGISTERED BEFORE ``/apps/{app_id}``."""
    try:
        return AppOut.model_validate(await service.get_by_slug(slug, principal))
    except TeamRunError as exc:
        raise _http(exc) from exc


@router.get("/apps/{app_id}", response_model=AppOut)
async def get_app(app_id: uuid.UUID, principal: PrincipalDep, service: AppServiceDep) -> AppOut:
    """One app, opened: where it came from and the form to fill in.

    Carries no team documents. A person running an app is not opening the plan behind it — that is
    the whole difference between an app and a team."""
    try:
        return AppOut.model_validate(await service.get(app_id, principal))
    except TeamRunError as exc:
        raise _http(exc) from exc


@router.get("/apps/{app_id}/requirements", response_model=AppRequirementsOut)
async def get_app_requirements(
    app_id: uuid.UUID, principal: PrincipalDep, service: AppServiceDep
) -> AppRequirementsOut:
    """What this app needs before it will run, and what the caller has not connected.

    The console shows this on the app's own page, so "connect a search key to use this" appears
    before someone fills in the form rather than only as the 409 when they press Run. It asks the
    SAME computation the GO gate asks, so the page and the gate cannot disagree."""
    try:
        return AppRequirementsOut.model_validate(await service.requirements(app_id, principal))
    except TeamRunError as exc:
        raise _http(exc) from exc


@router.post("/apps/{app_id}/runs", response_model=TeamRunOut, status_code=status.HTTP_202_ACCEPTED)
async def run_app(
    app_id: uuid.UUID,
    body: RunAppRequest,
    principal: PrincipalDep,
    service: AppServiceDep,
) -> TeamRunOut | JSONResponse:
    """Run the app on the CALLER's own model key, and follow it as an ordinary team run.

    202, because the run is validated, persisted QUEUED and handed to the worker — the same shape
    ``POST /team-runs`` returns, so a client follows one run type rather than two. A 409 carrying
    a top-level ``needs_credential`` means a tool is not connected yet; nothing was created."""
    try:
        row = await service.run(
            app_id,
            principal,
            inputs=body.inputs,
            models=body.models,
            graph_id=body.graph_id,
            workspace_root=body.workspace_root,
        )
    except TeamRunPreflightError as exc:
        return preflight_409(exc)  # #664: the connect prompt, in the shape the gateway relays
    except TeamRunError as exc:
        raise _http(exc) from exc
    return TeamRunOut.model_validate(row)


@router.get("/apps/{app_id}/runs", response_model=TeamRunListOut)
async def list_app_runs(
    app_id: uuid.UUID,
    principal: PrincipalDep,
    service: AppServiceDep,
    limit: Annotated[int, Query()] = 20,
    offset: Annotated[int, Query()] = 0,
) -> TeamRunListOut:
    """This app's runs — STRICTLY the caller's organisation. The app may be one every organisation
    can read; its runs never are, so two organisations running the same Oraclous-provided app never
    see each other's inputs or results."""
    try:
        rows, total = await service.list_runs(app_id, principal, limit=limit, offset=offset)
    except TeamRunError as exc:
        raise _http(exc) from exc
    return TeamRunListOut(team_runs=[TeamRunListItem.model_validate(r) for r in rows], total=total)
