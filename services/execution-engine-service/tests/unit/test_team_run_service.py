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
        capability_ceiling: list[str] | None = None,
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


def _agent(
    role: str, deps: list[str] | None = None, tools: list[str] | None = None
) -> dict[str, Any]:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
        "tools": tools or [],
    }


def _sub(role: str, *, bindings: list[str]) -> dict[str, Any]:
    """A single-agent sub-harness declaring the given capability bindings."""
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": role, "owner_organization_id": str(_ORG)},
        "capabilities": [{"ref": f"core/{b}@1", "binding": b} for b in bindings],
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
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


# ── audit remediation (G-A ceiling, G-C fail-not-strand + reap, G-D no double-exec) ──────────


async def test_subharness_exceeding_member_tools_ceiling_is_rejected_422() -> None:
    # G-A: member declares tools=['Read'] but the sub-harness smuggles a 'shell' capability — the
    # harness would build its ceiling from the sub-harness, so this MUST be rejected before run.
    svc = TeamRunService(team_runs=FakeTeamRunRepo(), harness=FakeHarness())
    manifest = _team([_agent("r", tools=["Read"])])
    with pytest.raises(TeamRunError) as exc:
        await svc.create_and_run(
            _principal(),
            manifest=manifest,
            sub_harnesses={"r": _sub("r", bindings=["shell"])},  # outside the ['Read'] ceiling
            gate_decisions={},
        )
    assert exc.value.status_code == 422


async def test_subharness_within_member_tools_ceiling_is_accepted() -> None:
    # G-A: a sub-harness whose capabilities are within the member's ceiling runs normally.
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc = TeamRunService(team_runs=repo, harness=harness)
    manifest = _team([_agent("r", tools=["Read", "Grep"])])
    row = await svc.create_and_run(
        _principal(),
        manifest=manifest,
        sub_harnesses={"r": _sub("r", bindings=["Read"])},  # subset of the ceiling
        gate_decisions={},
    )
    assert row.state == "SUCCEEDED"


async def test_non_harness_error_mid_drive_fails_run_not_strands_it() -> None:
    # G-C: any drive exception (not just HarnessClientError) transitions RUNNING -> FAILED, never
    # leave the row stuck RUNNING forever.
    class BoomHarness:
        async def execute(self, **kwargs: Any) -> dict[str, Any]:
            raise ValueError("decode blew up")  # NOT a HarnessClientError

    repo = FakeTeamRunRepo()
    svc = TeamRunService(team_runs=repo, harness=BoomHarness())
    row = await svc.create_and_run(
        _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )
    assert row.state == "FAILED"  # not stranded in RUNNING
    assert "decode blew up" in (row.error_message or "")


async def test_reap_stale_fails_stranded_running_team_runs() -> None:
    # G-C: the reaper FAILs a run stuck RUNNING past the lease (a driver that died mid-drive).
    repo = FakeTeamRunRepo()
    stranded = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest={},
        sub_harnesses={},
        gate_decisions={},
        state="RUNNING",
        results={},
        paused_at=[],
    )
    repo.rows[stranded.id] = stranded

    class FakeMaintenance:
        async def list_stale_team_runs(self, older_than: Any, *, limit: int = 100) -> list:
            return [stranded]

    svc = TeamRunService(team_runs=repo)  # reaper path: no harness
    import datetime as _dt

    reaped = await svc.reap_stale(
        FakeMaintenance(),  # type: ignore[arg-type]
        older_than=_dt.datetime(2026, 1, 1, tzinfo=_dt.UTC),
    )
    assert reaped == 1
    assert repo.rows[stranded.id].state == "FAILED"


async def test_advance_does_not_re_execute_completed_members() -> None:
    # G-D: resuming past a gate must NOT re-dispatch the already-completed pre-gate member.
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc = TeamRunService(team_runs=repo, harness=harness)
    manifest = _team(
        [_agent("researcher"), _human("approval", ["researcher"]), _agent("writer", ["approval"])]
    )
    paused = await svc.create_and_run(
        _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={}
    )
    assert paused.state == "PAUSED"
    assert len(harness.inputs) == 1  # researcher ran once before the gate

    await svc.advance(paused.id, _principal(), {"approval": "approve"})

    researcher_runs = sum(1 for i in harness.inputs if "researcher" in i)
    assert researcher_runs == 1  # researcher fired exactly once across create + advance (not twice)
    assert len(harness.inputs) == 2  # only researcher + writer, never a re-run


async def test_cancellation_mid_drive_marks_failed_then_propagates() -> None:
    # Red-team G-C: a cancellation (asyncio.CancelledError is BaseException, not Exception, in 3.12)
    # must NOT strand the row RUNNING — it is marked FAILED, then the CancelledError propagates
    # (a cancellation is never swallowed).
    import asyncio

    class CancelHarness:
        async def execute(self, **kwargs: Any) -> dict[str, Any]:
            raise asyncio.CancelledError

    repo = FakeTeamRunRepo()
    svc = TeamRunService(team_runs=repo, harness=CancelHarness())
    with pytest.raises(asyncio.CancelledError):
        await svc.create_and_run(
            _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
        )
    row = next(iter(repo.rows.values()))
    assert row.state == "FAILED"  # marked FAILED before propagation, not left stranded RUNNING
