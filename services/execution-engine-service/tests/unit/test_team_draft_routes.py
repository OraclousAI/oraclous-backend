"""Team-draft + compiler-run routes (#635) — unit, fake services via dependency_overrides, no DB.

Pins the edge contract: the ``{draft, would_block, blocking, report}`` write envelope, the
``{team_drafts, total}`` list shape with a LEAN row, limit/offset forwarding, the static
``/team-drafts/from-run`` path NOT being captured as an id, the structured 422 (leak-safe machine
token) and 404 mappings, DELETE 204, refine-nl's 200-vs-202 split, and the compiler-run 202.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_execution_engine_service.app.factory import create_app
from oraclous_execution_engine_service.core.dependencies import (
    get_compiler_run_service,
    get_principal,
    get_team_draft_service,
)
from oraclous_execution_engine_service.services.team_draft_service import (
    DraftVerdict,
    PendingOpDraft,
    RefineOutcome,
)
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


class _Row:
    def __init__(self, version: int = 1) -> None:
        self.id = uuid.uuid4()
        self.organisation_id = _ORG
        self.user_id = _USER
        self.name = "draft"
        self.manifest = {"metadata": {"kind": "team"}, "members": []}
        self.sub_harnesses = {}
        self.version = version
        self.created_at = None
        self.updated_at = None


def _verdict(would_block: bool = False, blocking: list[str] | None = None) -> DraftVerdict:
    return DraftVerdict(would_block=would_block, blocking=blocking or [], report="rendered dry-run")


def _client(draft_service: Any = None, compiler_service: Any = None) -> AsyncClient:
    app = create_app()  # construction only — lifespan (DB bind) is not triggered by ASGITransport
    if draft_service is not None:
        app.dependency_overrides[get_team_draft_service] = lambda: draft_service
    if compiler_service is not None:
        app.dependency_overrides[get_compiler_run_service] = lambda: compiler_service
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


async def test_create_returns_201_with_the_draft_plus_verdict_envelope() -> None:
    row = _Row()

    class _Svc:
        async def create(self, principal: Principal, **kw: Any) -> tuple[_Row, DraftVerdict]:
            return row, _verdict(would_block=True, blocking=["F-CAPABILITY-MISSING: x"])

    async with _client(_Svc()) as c:
        resp = await c.post(
            "/v1/engine/team-drafts",
            json={"name": "d", "manifest": {"metadata": {"kind": "team"}}},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["draft"]["id"] == str(row.id) and body["draft"]["version"] == 1
    assert body["would_block"] is True
    assert body["blocking"] == ["F-CAPABILITY-MISSING: x"]
    assert body["report"] == "rendered dry-run"


async def test_list_returns_team_drafts_total_and_forwards_pagination() -> None:
    calls: list[dict[str, int]] = []

    class _Svc:
        async def list_for_org(
            self, principal: Principal, *, limit: int = 50, offset: int = 0
        ) -> tuple[list[dict[str, Any]], int]:
            calls.append({"limit": limit, "offset": offset})
            return [
                {
                    "id": uuid.uuid4(),
                    "name": "d1",
                    "version": 3,
                    "member_count": 4,
                    "created_at": None,
                    "updated_at": None,
                }
            ], 9

    async with _client(_Svc()) as c:
        body = (await c.get("/v1/engine/team-drafts?limit=2&offset=5")).json()
    assert calls == [{"limit": 2, "offset": 5}]
    assert body["total"] == 9 and len(body["team_drafts"]) == 1
    item = body["team_drafts"][0]
    assert item["version"] == 3 and item["member_count"] == 4
    # a table row, NOT the document — the heavy fields never appear
    for forbidden in ("manifest", "sub_harnesses"):
        assert forbidden not in item


async def test_from_run_is_not_captured_as_a_draft_id() -> None:
    seen: list[uuid.UUID] = []

    class _Svc:
        async def create_from_run(
            self, principal: Principal, *, team_run_id: uuid.UUID, name: str | None = None
        ) -> tuple[_Row, DraftVerdict]:
            seen.append(team_run_id)
            return _Row(), _verdict()

    run_id = uuid.uuid4()
    async with _client(_Svc()) as c:
        resp = await c.post("/v1/engine/team-drafts/from-run", json={"team_run_id": str(run_id)})
    assert resp.status_code == 201, resp.text  # not a 422 uuid-parse of "from-run"
    assert seen == [run_id]


async def test_team_run_error_maps_to_its_status_not_a_500() -> None:
    class _Svc:
        async def get(self, draft_id: uuid.UUID, principal: Principal) -> Any:
            raise TeamRunError("team draft not found", 404)

    async with _client(_Svc()) as c:
        resp = await c.get(f"/v1/engine/team-drafts/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "team draft not found"


async def test_422_carries_the_leak_safe_machine_token() -> None:
    class _Svc:
        async def create_from_run(self, principal: Principal, **kw: Any) -> Any:
            raise TeamRunError(
                "only a SUCCEEDED run can seed a draft", 422, error_type="run_not_succeeded"
            )

    async with _client(_Svc()) as c:
        resp = await c.post(
            "/v1/engine/team-drafts/from-run", json={"team_run_id": str(uuid.uuid4())}
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail[0]["type"] == "run_not_succeeded"  # the #483 structured shape


async def test_delete_returns_204() -> None:
    class _Svc:
        async def delete(self, draft_id: uuid.UUID, principal: Principal) -> None:
            return None

    async with _client(_Svc()) as c:
        resp = await c.delete(f"/v1/engine/team-drafts/{uuid.uuid4()}")
    assert resp.status_code == 204 and resp.content == b""


async def test_refine_returns_the_op_applied_verdict_draft_shape() -> None:
    row = _Row(version=2)

    class _Svc:
        async def refine(
            self,
            draft_id: uuid.UUID,
            principal: Principal,
            *,
            edit_op: dict[str, Any],
            dry_run: bool = False,
        ) -> RefineOutcome:
            return RefineOutcome(row=row, verdict=_verdict(), applied=True, op=edit_op)

    async with _client(_Svc()) as c:
        resp = await c.post(
            f"/v1/engine/team-drafts/{uuid.uuid4()}/refine",
            json={"edit_op": {"op": "add_member", "role": "x"}},
        )
    body = resp.json()
    assert resp.status_code == 200
    assert body["applied"] is True and body["op"] == {"op": "add_member", "role": "x"}
    assert body["draft"]["version"] == 2


async def test_refine_nl_pending_returns_202_with_the_run_id() -> None:
    run_id = uuid.uuid4()

    class _Svc:
        async def refine_nl(self, draft_id: uuid.UUID, principal: Principal, **kw: Any) -> Any:
            return PendingOpDraft(run_id)

    async with _client(_Svc()) as c:
        resp = await c.post(
            f"/v1/engine/team-drafts/{uuid.uuid4()}/refine-nl",
            json={"instruction": "add a fact-checker", "models": [{"role": "primary"}]},
        )
    assert resp.status_code == 202
    assert resp.json() == {"op_drafter_run_id": str(run_id), "status": "running"}


async def test_refine_nl_requires_exactly_one_of_instruction_or_run_id() -> None:
    async with _client(object()) as c:
        both = await c.post(
            f"/v1/engine/team-drafts/{uuid.uuid4()}/refine-nl",
            json={
                "instruction": "x",
                "op_drafter_run_id": str(uuid.uuid4()),
                "models": [{}],
            },
        )
        neither = await c.post(f"/v1/engine/team-drafts/{uuid.uuid4()}/refine-nl", json={})
    assert both.status_code == 422 and neither.status_code == 422


async def test_compiler_run_returns_202_with_the_queued_run() -> None:
    captured: list[dict[str, Any]] = []

    class _Run:
        id = uuid.uuid4()
        organisation_id = _ORG
        state = "QUEUED"
        results: dict[str, Any] = {}
        paused_at: list[str] = []
        error_message = None
        created_at = None
        verdict = None
        re_dispatch_count = 0
        refresh_delta = None
        member_status: dict[str, str] = {}
        revision_rounds: dict[str, int] = {}
        loop_state: dict[str, Any] = {}

    class _Svc:
        async def create(self, principal: Principal, **kw: Any) -> _Run:
            captured.append(kw)
            return _Run()

    async with _client(compiler_service=_Svc()) as c:
        resp = await c.post(
            "/v1/engine/compiler-runs",
            json={
                "objective": "build a digest team",
                "models": [{"role": "primary", "binding": "openrouter/x"}],
                "graph_id": "g-1",
            },
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["state"] == "QUEUED"
    assert captured[0]["objective"] == "build a digest team"
    assert captured[0]["graph_id"] == "g-1"


async def test_compiler_run_requires_models() -> None:
    async with _client(compiler_service=object()) as c:
        resp = await c.post("/v1/engine/compiler-runs", json={"objective": "x", "models": []})
    assert resp.status_code == 422  # min_length=1 at the schema edge
