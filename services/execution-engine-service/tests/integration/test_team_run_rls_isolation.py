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
    """Every member 'executes' to SUCCEEDED — tests persistence + RLS, not the loop."""

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        manifest_ref: str | None = None,
        capability_ceiling: list[str] | None = None,
    ) -> dict[str, Any]:
        return {"status": "SUCCEEDED", "output": f"done: {input_text[:30]}"}


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
    """create_and_run on the org-bound engine binds the org itself, so the RLS-backstopped INSERT is
    admitted (without org_scope it would raise 42501 against the empty GUC) — and the tenant
    reads its own run back."""
    manifest = _team(ORG_A, [_agent("researcher"), _agent("writer", ["researcher"])])

    run = await team_run_service.create_and_run(
        _principal(ORG_A, USER_A), manifest=manifest, sub_harnesses={}, gate_decisions={}
    )
    assert run.state == "SUCCEEDED"
    assert run.organisation_id == ORG_A
    assert set(run.results) == {"researcher", "writer"}  # the whole team really ran + persisted

    fetched = await team_run_service.get(run.id, _principal(ORG_A, USER_A))
    assert fetched.id == run.id and fetched.state == "SUCCEEDED"


async def test_cross_org_team_run_read_is_denied_by_rls(
    team_run_service: TeamRunService,
) -> None:
    """Org A runs a team; org B's request-path read never sees it — the team_runs RLS policy scopes
    the org-bound engine to the request-bound org (the backstop bites, not just app-layer WHERE)."""
    a_run = await team_run_service.create_and_run(
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
    b_run = await team_run_service.create_and_run(
        _principal(ORG_B, USER_B),
        manifest=_team(ORG_B, [_agent("b")]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert (await team_run_service.get(b_run.id, _principal(ORG_B, USER_B))).id == b_run.id
    assert (await team_run_service.get(a_run.id, _principal(ORG_A, USER_A))).id == a_run.id
