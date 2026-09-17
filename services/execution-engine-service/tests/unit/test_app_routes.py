"""App routes — 422 field attribution for an unusable graph_id (unit; fake service via
dependency_overrides, no DB). #1108 ruling 6.

There was no route-level test for ``POST /v1/engine/apps/{app_id}/runs`` before this — the
existing app-route coverage stops at the domain layer (``test_app_run_binding.py``). This mirrors
the ``test_team_run_routes.py`` client-fixture pattern for the sibling ``/team-runs`` route.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_execution_engine_service.app.factory import create_app
from oraclous_execution_engine_service.core.dependencies import get_app_service, get_principal
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


async def _client(service: Any) -> AsyncIterator[AsyncClient]:
    app = create_app()  # construction only — lifespan (DB bind) is not triggered by ASGITransport
    app.dependency_overrides[get_app_service] = lambda: service
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


async def test_run_app_invalid_graph_id_attributes_the_field() -> None:
    # #1108 ruling 6: TeamRunError carries an optional `field` so an unusable graph_id points loc
    # at ["body", "graph_id"] — not the bare "body" every other 422 falls back to — so the
    # gateway's extract_validation_details reports details:[{"field":"graph_id",
    # "issue":"INVALID_GRAPH_ID"}] instead of a field-less one the console cannot highlight. RED
    # until TeamRunError accepts `field` and this route's `_http` reads it.
    class BadService:
        async def run(self, app_id: uuid.UUID, principal: Principal, **kwargs: Any) -> Any:
            raise TeamRunError(
                "graph_id does not exist in your organisation",
                422,
                error_type="invalid_graph_id",
                field="graph_id",
            )

    async with await _client(BadService()) as c:
        resp = await c.post(
            f"/v1/engine/apps/{uuid.uuid4()}/runs",
            json={"inputs": {}, "models": [], "graph_id": "not-a-real-graph"},
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and len(detail) == 1, detail
    assert detail[0]["loc"] == ["body", "graph_id"], detail
    assert detail[0]["type"] == "invalid_graph_id", detail
