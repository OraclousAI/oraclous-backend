"""``GET /team-drafts/{id}/succeeded-versions`` + the ``?has_succeeded_run`` list filter
(#1163; unit, fake service via dependency_overrides, no DB).

Pins: the per-team succeeded-versions envelope (``{team_draft_id, versions, total}``), limit/offset
forwarding (default 50/0), the empty-page shape, the 404 mapping for a missing/foreign draft (R12,
R16), and the ``has_succeeded_run`` query parameter being forwarded to ``list_for_org`` (R14) rather
than silently dropped.

RED (#1163, ADR-010): the route ``/team-drafts/{team_draft_id}/succeeded-versions`` does not exist
yet, and ``list_team_drafts`` does not forward ``has_succeeded_run`` to the service yet. Every name
here is on an object that already exists (``TeamDraftService``, ``create_app`` etc.), so this is a
routing/behaviour gap, not a missing module — no seam-import concern (``.claude/rules/tests-seam-
imports.md`` covers a missing MODULE, not a missing route or a missing keyword forwarded through an
existing one).

Trap avoided (an earlier #1163 worker hit this): a GET on a path FastAPI has no route for also
returns a bare 404, which would coincidentally match a naive ``resp.status_code == 404`` assertion
for the "draft not found" case even with zero implementation. Test 4 below also asserts the fake's
``succeeded_versions`` was actually invoked and that the 404 body carries OUR detail string, not
Starlette's generic ``"Not Found"`` — so it fails today because the route is missing, and would
also have failed had the route silently swallowed the raised error.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_execution_engine_service.app.factory import create_app
from oraclous_execution_engine_service.core.dependencies import (
    get_principal,
    get_team_draft_service,
)
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _client(draft_service: Any) -> AsyncClient:
    app = create_app()  # construction only — lifespan (DB bind) is not triggered by ASGITransport
    app.dependency_overrides[get_team_draft_service] = lambda: draft_service
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


class _VersionsSvc:
    """Fake ``TeamDraftService`` exposing only ``succeeded_versions`` (records every call)."""

    def __init__(
        self,
        versions: list[dict[str, Any]] | None = None,
        total: int = 0,
        error: TeamRunError | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._versions = versions or []
        self._total = total
        self._error = error

    async def succeeded_versions(
        self,
        draft_id: uuid.UUID,
        principal: Principal,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls.append({"draft_id": draft_id, "limit": limit, "offset": offset})
        if self._error is not None:
            raise self._error
        return self._versions, self._total


async def test_returns_the_teams_succeeded_versions_envelope_with_the_default_page() -> None:
    draft_id = uuid.uuid4()
    run_id = uuid.uuid4()
    finished_at = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    version_row = {"version": 3, "team_run_id": run_id, "finished_at": finished_at}
    svc = _VersionsSvc(versions=[version_row], total=1)

    async with _client(svc) as c:
        resp = await c.get(f"/v1/engine/team-drafts/{draft_id}/succeeded-versions")

    assert resp.status_code == 200, resp.text
    assert svc.calls == [{"draft_id": draft_id, "limit": 50, "offset": 0}]
    body = resp.json()
    assert body["team_draft_id"] == str(draft_id)
    assert body["total"] == 1
    assert len(body["versions"]) == 1
    item = body["versions"][0]
    assert item["version"] == 3
    assert item["team_run_id"] == str(run_id)
    assert datetime.fromisoformat(item["finished_at"]) == finished_at


async def test_limit_and_offset_are_forwarded() -> None:
    draft_id = uuid.uuid4()
    svc = _VersionsSvc(versions=[], total=0)

    path = f"/v1/engine/team-drafts/{draft_id}/succeeded-versions?limit=5&offset=10"
    async with _client(svc) as c:
        resp = await c.get(path)

    assert resp.status_code == 200, resp.text
    assert svc.calls == [{"draft_id": draft_id, "limit": 5, "offset": 10}]


async def test_a_draft_with_no_qualifying_versions_is_an_empty_page() -> None:
    draft_id = uuid.uuid4()
    svc = _VersionsSvc(versions=[], total=0)

    async with _client(svc) as c:
        resp = await c.get(f"/v1/engine/team-drafts/{draft_id}/succeeded-versions")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["versions"] == []
    assert body["total"] == 0


async def test_a_missing_or_foreign_draft_is_a_404_from_the_service_not_a_bare_route_miss() -> None:
    draft_id = uuid.uuid4()
    svc = _VersionsSvc(error=TeamRunError("team draft not found", 404))

    async with _client(svc) as c:
        resp = await c.get(f"/v1/engine/team-drafts/{draft_id}/succeeded-versions")

    assert resp.status_code == 404, resp.text
    # the fake MUST have been called — rules out a plain "no such route" 404 passing by accident
    assert svc.calls == [{"draft_id": draft_id, "limit": 50, "offset": 0}]
    # OUR message, not Starlette's generic "Not Found" — proves the fake actually ran
    assert resp.json()["detail"] == "team draft not found"


async def test_has_succeeded_run_is_forwarded_to_list_for_org() -> None:
    calls: list[dict[str, Any]] = []

    class _ListSvc:
        async def list_for_org(
            self,
            principal: Principal,
            *,
            limit: int = 50,
            offset: int = 0,
            has_succeeded_run: bool | None = None,
        ) -> tuple[list[dict[str, Any]], int]:
            calls.append({"has_succeeded_run": has_succeeded_run})
            return [], 0

    async with _client(_ListSvc()) as c:
        await c.get("/v1/engine/team-drafts?has_succeeded_run=true")
        await c.get("/v1/engine/team-drafts?has_succeeded_run=false")
        await c.get("/v1/engine/team-drafts")

    assert calls == [
        {"has_succeeded_run": True},
        {"has_succeeded_run": False},
        {"has_succeeded_run": None},
    ]
