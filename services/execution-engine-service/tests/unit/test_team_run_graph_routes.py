"""``GET /v1/engine/team-runs/{id}/graph`` (#1119/#1154) — wire-shape only.

The full response contract is recorded on #1154 (copied to
``oraclous-knowledge/flows/interface-contracts.md``): ``{team_run_id, state, nodes, edges}`` at the
top level, a fixed 10-key shape per node (``role, kind, status, error_code, skip_reason,
reason_role, input_from, has_output, loop, fan_out``), and a 2-key shape per edge (``from, to``).
Every key is always present; an unset value is ``null``, never a missing key.

Mirrors ``test_team_run_mid_run_routes.py``'s own stated scope: a fake service wired through
``dependency_overrides``, no DB and no drive. What the domain derivation itself computes (status
mapping, skip_reason, blocked's reason_role, loop/fan_out, ...) is pinned separately in
``test_run_graph.py`` against ``domain.run_graph.derive_run_graph`` — this file only pins the HTTP
boundary: does the route exist, does it 404 a cross-org id the same way ``/tree``/``/status`` do,
and does the JSON on the wire have exactly the contracted keys.

RED until ``domain/run_graph.py``, the route, ``TeamRunService.graph``, and the
``TeamRunGraphOut`` / ``RunGraphNodeOut`` / ``RunGraphEdgeOut`` schema classes ALL land. Per the
Tests Review ruling on #1119 (``TeamRunService.graph()`` returns the domain ``RunGraph``, mirroring
how ``TeamRunService.status()`` returns a plain ``TeamRunStatus`` for the route to map), every
``FakeService.graph`` below builds and returns a domain ``RunGraph``/``RunGraphNode``/
``RunGraphEdge`` — never the wire DTO directly — so the real route's own domain-to-wire mapping is
what these tests actually exercise (once it exists). The route does not exist yet, so a request to
it 404s through FastAPI's own unmatched-route handling before the request is ever dispatched to the
dependency-overridden service — a plain status-code assertion already fails RED for the two
200-expecting tests, and the fake's own function-local ``domain.run_graph`` import never even
executes (confirmed by running this file: every case fails on the route 404, not on the import).
The cross-org test asserts the exact ``TeamRunError`` body (not just the status code), since an
unmatched route ALSO 404s and would otherwise pass by accident. Both the domain and schema names
are imported function-locally per ``.claude/rules/tests-seam-imports.md`` — they do not exist yet,
so importing either at module level would abort collection for the whole run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_execution_engine_service.app.factory import create_app
from oraclous_execution_engine_service.core.dependencies import get_principal, get_team_run_service
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()

_NODE_KEYS = {
    "role",
    "kind",
    "status",
    "error_code",
    "skip_reason",
    "reason_role",
    "input_from",
    "has_output",
    "loop",
    "fan_out",
}
_EDGE_KEYS = {"from", "to"}


async def _client(service: Any) -> AsyncIterator[AsyncClient]:
    app = create_app()  # construction only — lifespan (DB bind) is not triggered by ASGITransport
    app.dependency_overrides[get_team_run_service] = lambda: service
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


async def test_graph_route_returns_contract_shape() -> None:
    """A run with a spread of statuses (succeeded / skipped / failed / a human waiting_approval
    gate / a pending fan_out loop member) plus at least one edge. Every top-level, node, and edge
    key is asserted by exact set-equality — not "contains" — per the #1154 contract's "every key is
    always present" rule."""
    rid = uuid.uuid4()

    class FakeService:
        async def graph(self, team_run_id: uuid.UUID, principal: Principal) -> Any:
            from oraclous_execution_engine_service.domain.run_graph import (
                RunGraph,
                RunGraphEdge,
                RunGraphNode,
            )

            return RunGraph(
                team_run_id=rid,
                state="PAUSED",
                nodes=[
                    RunGraphNode(
                        role="researcher",
                        kind="agent",
                        status="succeeded",
                        error_code=None,
                        skip_reason=None,
                        reason_role=None,
                        input_from=[],
                        has_output=True,
                        loop=None,
                        fan_out=False,
                    ),
                    RunGraphNode(
                        role="writer",
                        kind="agent",
                        status="skipped",
                        error_code=None,
                        skip_reason="condition_false",
                        reason_role="researcher",
                        input_from=["researcher"],
                        has_output=False,
                        loop=None,
                        fan_out=False,
                    ),
                    RunGraphNode(
                        role="reviewer",
                        kind="agent",
                        status="failed",
                        error_code="TOOL_TIMEOUT",
                        skip_reason=None,
                        reason_role=None,
                        input_from=[],
                        has_output=False,
                        loop=None,
                        fan_out=False,
                    ),
                    RunGraphNode(
                        role="approver",
                        kind="human",
                        status="waiting_approval",
                        error_code=None,
                        skip_reason=None,
                        reason_role=None,
                        input_from=["researcher"],
                        has_output=False,
                        loop=None,
                        fan_out=False,
                    ),
                    RunGraphNode(
                        role="finisher",
                        kind="agent",
                        status="pending",
                        error_code=None,
                        skip_reason=None,
                        reason_role=None,
                        input_from=[],
                        has_output=False,
                        loop=0,
                        fan_out=True,
                    ),
                ],
                edges=[
                    RunGraphEdge(from_="researcher", to="writer"),
                    RunGraphEdge(from_="writer", to="reviewer"),
                    RunGraphEdge(from_="researcher", to="approver"),
                ],
            )

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}/graph")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(body.keys()) == {"team_run_id", "state", "nodes", "edges"}
    assert body["team_run_id"] == str(rid)
    assert body["state"] == "PAUSED"
    assert len(body["nodes"]) == 5
    assert len(body["edges"]) == 3

    for node in body["nodes"]:
        assert set(node.keys()) == _NODE_KEYS
    for edge in body["edges"]:
        assert set(edge.keys()) == _EDGE_KEYS

    by_role = {n["role"]: n for n in body["nodes"]}
    assert by_role["writer"]["status"] == "skipped"
    assert by_role["writer"]["skip_reason"] == "condition_false"
    assert by_role["writer"]["reason_role"] == "researcher"
    assert by_role["reviewer"]["status"] == "failed"
    assert by_role["reviewer"]["error_code"] == "TOOL_TIMEOUT"
    assert by_role["approver"]["kind"] == "human"
    assert by_role["approver"]["status"] == "waiting_approval"
    assert by_role["finisher"]["loop"] == 0
    assert by_role["finisher"]["fan_out"] is True

    edge_pairs = {(e["from"], e["to"]) for e in body["edges"]}
    assert edge_pairs == {
        ("researcher", "writer"),
        ("writer", "reviewer"),
        ("researcher", "approver"),
    }


async def test_graph_route_cross_org_is_404() -> None:  # mirrors /tree's and /status's H1/H3/H4
    """The org-scoped ``graph()`` raises the same ``TeamRunError("team run not found", 404)`` a
    cross-org ``/tree`` or ``/status`` id does, mapped through the shared ``_http``. Asserts the
    EXACT error body, not just the status code: an unmatched route ALSO 404s (FastAPI's default,
    ``{"detail": "Not Found"}``), which would let this test pass by accident today if it only
    checked the status code."""

    class CrossOrgService:
        async def graph(self, team_run_id: uuid.UUID, principal: Principal) -> Any:
            raise TeamRunError("team run not found", 404)

    async with await _client(CrossOrgService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{uuid.uuid4()}/graph")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "team run not found"}


async def test_graph_on_pre_migration_row_is_not_a_500() -> None:
    """A run made before the ``member_skip_reasons`` migration has no recorded reason for a skipped
    member — the column literally didn't exist, so ``member_skip_reasons`` is ``None``, not ``{}``.
    #1154: that must read as ``skip_reason: "unrecorded"``, cleanly, never a 500. Built with the
    real ``derive_run_graph`` (not a hand-built node) so this test genuinely exercises the
    domain's "no recorded reason -> unrecorded" mechanism, not only the route's wire mapping
    (be-test-reviewer B1)."""
    rid = uuid.uuid4()

    class FakeService:
        async def graph(self, team_run_id: uuid.UUID, principal: Principal) -> Any:
            from oraclous_execution_engine_service.domain.run_graph import derive_run_graph

            manifest = {
                "ohm_version": "1.1",
                "metadata": {
                    "id": "24869709-764a-49d0-b259-ce561340a8ec",
                    "name": "t",
                    "owner_organization_id": "00000000-0000-0000-0000-0000000000a0",
                    "kind": "team",
                },
                "members": [
                    {
                        "role": "writer",
                        "kind": "agent",
                        "manifest_ref": "org:x/writer@1",
                        "depends_on": [],
                    }
                ],
                "runtime": {"entrypoint": "writer"},
            }
            return derive_run_graph(
                manifest=manifest,
                team_run_id=rid,
                state="SUCCEEDED",
                member_status={"writer": "skipped"},
                member_error_codes=None,
                member_skip_reasons=None,
                results=None,
                paused_at=None,
            )

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}/graph")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"][0]["skip_reason"] == "unrecorded"
    assert body["nodes"][0]["reason_role"] is None


async def test_graph_route_unset_fields_are_null_not_missing() -> None:
    """#1154: "every key is always present; unset is null, never missing". A bare pending member
    with no error/skip/loop data still carries every key, explicitly null, and an empty ``edges``
    list is present (not omitted) rather than the endpoint dropping the key entirely."""
    rid = uuid.uuid4()

    class FakeService:
        async def graph(self, team_run_id: uuid.UUID, principal: Principal) -> Any:
            from oraclous_execution_engine_service.domain.run_graph import RunGraph, RunGraphNode

            return RunGraph(
                team_run_id=rid,
                state="QUEUED",
                nodes=[
                    RunGraphNode(
                        role="bare",
                        kind="agent",
                        status="pending",
                        error_code=None,
                        skip_reason=None,
                        reason_role=None,
                        input_from=[],
                        has_output=False,
                        loop=None,
                        fan_out=False,
                    )
                ],
                edges=[],
            )

    async with await _client(FakeService()) as c:
        resp = await c.get(f"/v1/engine/team-runs/{rid}/graph")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "edges" in body
    assert body["edges"] == []

    node = body["nodes"][0]
    assert set(node.keys()) == _NODE_KEYS
    for key in ("error_code", "skip_reason", "reason_role", "loop"):
        assert key in node
        assert node[key] is None
    assert node["input_from"] == []
    assert node["has_output"] is False
    assert node["fan_out"] is False
