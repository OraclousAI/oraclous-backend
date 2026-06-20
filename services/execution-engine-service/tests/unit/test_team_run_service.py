"""Team-run service — drive / persist / pause / advance (unit; fake repo + fake harness).

Proves the reachable team-run entry point's logic without a DB: ``create_and_run`` drives the member
DAG through the (fake) harness and persists the outcome, a human gate PAUSES the run durably, and
``advance`` resumes it past the decided gate. Org-scoping + the not-a-team / cross-org guards are
exercised too. The DB-backed RLS isolation is proven separately in the integration test.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.team_run_service import TeamRunError, TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class FakeTeamRunRepo:
    """In-memory mirror of TeamRunRepository's create/get/transition (CAS) semantics."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EngineTeamRun] = {}

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
        gate_decisions: dict[str, Any],
    ) -> EngineTeamRun:
        row = EngineTeamRun(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            manifest=manifest,
            sub_harnesses=sub_harnesses,
            gate_decisions=gate_decisions,
            state="QUEUED",
            results={},
            paused_at=[],
        )
        self.rows[row.id] = row
        return row

    async def get(self, team_run_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineTeamRun | None:
        row = self.rows.get(team_run_id)
        return row if row is not None and row.organisation_id == organisation_id else None

    async def transition(
        self,
        team_run_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        new_state: str,
        allowed_from: frozenset[str],
        **fields: Any,
    ) -> tuple[EngineTeamRun | None, bool]:
        row = self.rows.get(team_run_id)
        if row is None or row.organisation_id != organisation_id or row.state not in allowed_from:
            return row, False
        row.state = new_state
        for key, value in fields.items():
            setattr(row, key, value)
        return row, True


class FakeHarness:
    """A stand-in HarnessClient: every member 'execution' SUCCEEDS (the real loop is proven in the
    harness-runtime real-execution test). Records each member input so we can assert who ran."""

    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        manifest_ref: str | None = None,
    ) -> dict[str, Any]:
        self.inputs.append(input_text)
        return {"status": "SUCCEEDED", "output": f"done: {input_text[:30]}"}


def _team(members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "team",
            "owner_organization_id": str(_ORG),
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
    return {"role": role, "kind": "human", "human_role": "reviewer", "depends_on": deps or []}


async def test_create_and_run_drives_team_through_harness_and_persists_succeeded() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc = TeamRunService(team_runs=repo, harness=harness)
    manifest = _team([_agent("researcher"), _agent("writer", ["researcher"])])

    row = await svc.create_and_run(
        _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={}
    )

    assert row.state == "SUCCEEDED"
    assert len(harness.inputs) == 2  # both members ran through the real-shaped harness call
    assert set(row.results) == {"researcher", "writer"}
    assert repo.rows[row.id].state == "SUCCEEDED"  # persisted, not just returned


async def test_human_gate_pauses_the_run_durably() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc = TeamRunService(team_runs=repo, harness=harness)
    manifest = _team(
        [_agent("researcher"), _human("approval", ["researcher"]), _agent("writer", ["approval"])]
    )

    row = await svc.create_and_run(
        _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={}
    )

    assert row.state == "PAUSED"
    assert row.paused_at == ["approval"]
    assert "writer" not in row.results  # downstream did NOT cross the gate
    assert len(harness.inputs) == 1  # only the researcher ran; the writer is gated off


async def test_advance_resumes_a_paused_run_past_the_gate() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc = TeamRunService(team_runs=repo, harness=harness)
    manifest = _team(
        [_agent("researcher"), _human("approval", ["researcher"]), _agent("writer", ["approval"])]
    )
    paused = await svc.create_and_run(
        _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={}
    )
    assert paused.state == "PAUSED"

    resumed = await svc.advance(paused.id, _principal(), {"approval": "approve"})

    assert resumed.state == "SUCCEEDED"
    assert "writer" in resumed.results  # the gate opened and the writer finally ran
    assert repo.rows[paused.id].gate_decisions == {"approval": "approve"}  # decision persisted


async def test_a_rejected_gate_halts_the_run() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc = TeamRunService(team_runs=repo, harness=harness)
    manifest = _team([_agent("researcher"), _human("approval", ["researcher"])])
    paused = await svc.create_and_run(
        _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={}
    )

    rejected = await svc.advance(paused.id, _principal(), {"approval": "reject"})

    assert rejected.state == "REJECTED"


async def test_not_a_team_manifest_is_422() -> None:
    svc = TeamRunService(team_runs=FakeTeamRunRepo(), harness=FakeHarness())
    agent_doc = {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": "a", "owner_organization_id": str(_ORG)},
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }
    with pytest.raises(TeamRunError) as exc:
        await svc.create_and_run(
            _principal(), manifest=agent_doc, sub_harnesses={}, gate_decisions={}
        )
    assert exc.value.status_code == 422


async def test_principal_without_org_is_403() -> None:
    svc = TeamRunService(team_runs=FakeTeamRunRepo(), harness=FakeHarness())
    manifest = _team([_agent("a")])
    with pytest.raises(TeamRunError) as exc:
        await svc.create_and_run(
            _principal(org=None), manifest=manifest, sub_harnesses={}, gate_decisions={}
        )
    assert exc.value.status_code == 403


async def test_get_is_org_scoped_cross_org_is_not_found() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc = TeamRunService(team_runs=repo, harness=harness)
    row = await svc.create_and_run(
        _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )
    with pytest.raises(TeamRunError) as exc:
        await svc.get(row.id, _principal(org=uuid.uuid4()))  # a different org
    assert exc.value.status_code == 404
