"""Team-run service against a REAL Postgres + the RLS backstop (ADR-030) — docker-required.

The durable, reachable team-run entry point, end-to-end on the substrate it ships on: the REAL
``TeamRunService`` + ``TeamRunRepository`` on the NOSUPERUSER ``oraclous_app`` org-bound engine (the
GUC guard installed by default), wired exactly as ``get_team_run_service`` wires it in deployment.
The harness is a deterministic stand-in (the real loop is proven in the harness-runtime
real-execution test); what is proven HERE is that the run PERSISTS, READS BACK under the
request-bound org, and is ISOLATED across orgs by the RLS policy on the team_runs table.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.team_run_service import TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
    pytest.mark.isolation,
]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _principal(org: uuid.UUID, user: uuid.UUID) -> Principal:
    return Principal(principal_id=user, principal_type=PrincipalType.USER, organisation_id=org)


class _FakeHarness:
    """Every member 'executes' to SUCCEEDED — tests persistence + RLS, not the loop. Records each
    member input so a test can assert WHO ran (and that a resumed member is not re-executed)."""

    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        manifest_ref: str | None = None,
        capability_ceiling: list[str] | None = None,
        parent_execution_id: uuid.UUID | None = None,
        trace_id: uuid.UUID | None = None,
        workspace_root: str | None = None,
    ) -> dict[str, Any]:
        self.inputs.append(input_text)
        # #471: a real execution id per member → the engine records child_execution_ids (the tree).
        # #472: total_tokens → the engine accumulates the run's cost for the O4 status surface.
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": f"done: {input_text[:30]}",
            "total_tokens": 100,
        }


def _team(org: uuid.UUID, members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "team",
            "owner_organization_id": str(org),
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"]},
    }


def _agent(role: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
    }


def _human(role: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {"role": role, "kind": "human", "human_role": "author", "depends_on": deps or []}


async def _run(svc: TeamRunService, principal: Principal, **kwargs: Any) -> Any:
    """The request (create → QUEUED) then the WORKER (drive) — the async path, inline here."""
    row = await svc.create(principal, **kwargs)
    return await svc.drive(row.id, principal)


@pytest.fixture
async def team_run_service(engine_dsns) -> AsyncIterator[TeamRunService]:  # noqa: ANN001
    """The REAL request-path TeamRunService on the org-bound ``oraclous_app`` engine — the service
    binds the org per request via ``org_scope`` (the repo's GUC guard then sets the RLS GUC)."""
    _owner_async_dsn, app_async_dsn = engine_dsns
    repo = TeamRunRepository(app_async_dsn)
    try:
        yield TeamRunService(team_runs=repo, harness=_FakeHarness())
    finally:
        await repo.close()


async def test_team_run_persists_and_reads_back_on_org_bound_engine(
    team_run_service: TeamRunService,
) -> None:
    """create+drive on the org-bound engine binds the org itself, so the RLS-backstopped INSERT is
    admitted (without org_scope it would raise 42501 against the empty GUC) — and the tenant
    reads its own run back."""
    manifest = _team(ORG_A, [_agent("researcher"), _agent("writer", ["researcher"])])

    run = await _run(
        team_run_service,
        _principal(ORG_A, USER_A),
        manifest=manifest,
        sub_harnesses={},
        gate_decisions={},
    )
    assert run.state == "SUCCEEDED"
    assert run.organisation_id == ORG_A
    assert set(run.results) == {"researcher", "writer"}  # the whole team really ran + persisted

    fetched = await team_run_service.get(run.id, _principal(ORG_A, USER_A))
    assert fetched.id == run.id and fetched.state == "SUCCEEDED"
    # run-tree (#471) persists on the real org-bound engine: the run is its own root + both members
    # are recorded as children, read back under the request-bound org (RLS-admitted).
    assert fetched.root_execution_id == run.id
    assert len(fetched.child_execution_ids) == 2  # researcher + writer, the tree's leaves

    # O4 status (#472) on the real org-bound engine: goal-attainment progress + accumulated cost,
    # read through the request-path org-scoped status (H3 — not the cross-org maintenance reader).
    status = await team_run_service.status(run.id, _principal(ORG_A, USER_A))
    assert status.progress == 100 and status.healthy is True  # both members done → 100
    assert status.cost_tokens == 200  # Σ the members' total_tokens (2 × 100)


async def test_cross_org_team_run_read_is_denied_by_rls(
    team_run_service: TeamRunService,
) -> None:
    """Org A runs a team; org B's request-path read never sees it — the team_runs RLS policy scopes
    the org-bound engine to the request-bound org (the backstop bites, not just app-layer WHERE)."""
    a_run = await _run(
        team_run_service,
        _principal(ORG_A, USER_A),
        manifest=_team(ORG_A, [_agent("a")]),
        sub_harnesses={},
        gate_decisions={},
    )

    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    with pytest.raises(
        TeamRunError
    ) as exc:  # org B cannot see org A's run — RLS-filtered to absent
        await team_run_service.get(a_run.id, _principal(ORG_B, USER_B))
    assert exc.value.status_code == 404

    # org B's own run succeeds + is isolated; org A still sees only its own.
    b_run = await _run(
        team_run_service,
        _principal(ORG_B, USER_B),
        manifest=_team(ORG_B, [_agent("b")]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert (await team_run_service.get(b_run.id, _principal(ORG_B, USER_B))).id == b_run.id
    assert (await team_run_service.get(a_run.id, _principal(ORG_A, USER_A))).id == a_run.id


async def test_cross_request_gate_resume_against_real_db(engine_dsns) -> None:  # noqa: ANN001
    """Step 4 (the durable gate-resume seam) end-to-end on REAL Postgres + RLS — not a fake repo.

    Request 1 drives a gated team and it PAUSES at the human gate, durably persisted. A SEPARATE
    request (a fresh service on a fresh connection — as a different worker/process would) reads the
    PAUSED state back from the DB and ADVANCES it past the gate to SUCCEEDED. The pre-gate member is
    NOT re-executed on resume (its result is read back from the row, G-D) — proving the pause truly
    survives across requests and resume is idempotent over side effects, against real persistence.
    """
    _owner, app_dsn = engine_dsns
    manifest = _team(
        ORG_A,
        [_agent("researcher"), _human("approval", ["researcher"]), _agent("writer", ["approval"])],
    )

    # ── request 1: create + worker-drive -> PAUSED at the gate, persisted to the DB ──
    repo1 = TeamRunRepository(app_dsn)
    harness1 = _FakeHarness()
    try:
        paused = await _run(
            TeamRunService(team_runs=repo1, harness=harness1),
            _principal(ORG_A, USER_A),
            manifest=manifest,
            sub_harnesses={},
            gate_decisions={},
        )
    finally:
        await repo1.close()
    assert paused.state == "PAUSED"
    assert paused.paused_at == ["approval"]
    assert len(harness1.inputs) == 1  # only the researcher ran before the gate
    run_id = paused.id

    # ── a SEPARATE request/connection reads PAUSED back, ADVANCES (→QUEUED), then the worker
    # drives the resume to SUCCEEDED — pause + gate decision survive across requests ──
    repo2 = TeamRunRepository(app_dsn)
    harness2 = _FakeHarness()
    try:
        svc2 = TeamRunService(team_runs=repo2, harness=harness2)
        refetched = await svc2.get(run_id, _principal(ORG_A, USER_A))
        assert refetched.state == "PAUSED"  # the pause survived across the request boundary (DB)
        assert "researcher" in refetched.results  # the pre-gate result is durably persisted

        advanced = await svc2.advance(run_id, _principal(ORG_A, USER_A), {"approval": "approve"})
        assert advanced.state == "QUEUED"  # advance re-queues; the worker drives the resume
        resumed = await svc2.drive(run_id, _principal(ORG_A, USER_A))
        assert resumed.state == "SUCCEEDED"  # the worker resumed past the gate to completion
        assert "writer" in resumed.results  # the gated-off member ran only after the gate opened
        # the researcher (pre-gate) is NOT re-run on resume — request-2's harness never saw it (G-D)
        assert all("researcher" not in i for i in harness2.inputs)
    finally:
        await repo2.close()

    # the researcher (pre-gate) executed EXACTLY once across BOTH requests — the resume reused the
    # persisted result instead of re-running it (G-D, proven against the real DB).
    assert (
        sum(1 for i in harness2.inputs if "researcher" in i) == 0
    )  # request-2 harness never reran it
    assert any("writer" in i for i in harness2.inputs)  # request-2 only ran the post-gate member
