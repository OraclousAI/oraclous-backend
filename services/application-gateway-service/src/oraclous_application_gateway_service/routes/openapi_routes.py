"""Published OpenAPI contract routes (routes layer).

Serves the canonical R6 public contract (ADR-015) at ``/v1/openapi.json`` +
``/v1/openapi.yaml`` and a Swagger UI at ``/docs``. Registered BEFORE the reverse-proxy
catch-all so the edge serves them, never forwards them to an upstream. They are public
(no edge auth) — the contract is a deliberate, curated disclosure surface: it carries only
the intended public operations (never the ``/internal/*`` plane), and its error component
is the closed error envelope.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, Response

from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.services.openapi_service import load_contract

router = APIRouter(tags=["gateway"])

_OPENAPI_JSON_PATH = "/v1/openapi.json"


@router.get(_OPENAPI_JSON_PATH, include_in_schema=False)
async def openapi_json() -> JSONResponse:
    spec, _ = load_contract(get_settings().OPENAPI_SPEC_PATH)
    return JSONResponse(spec)


@router.get("/v1/openapi.yaml", include_in_schema=False)
async def openapi_yaml() -> Response:
    _, text = load_contract(get_settings().OPENAPI_SPEC_PATH)
    return Response(text, media_type="application/yaml")


@router.get("/docs", include_in_schema=False)
async def docs() -> HTMLResponse:
    return get_swagger_ui_html(openapi_url=_OPENAPI_JSON_PATH, title="Oraclous Platform API — docs")
