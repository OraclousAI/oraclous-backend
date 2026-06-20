"""Team-run service — async create→worker-drive / pause / advance (unit; fake repo + fake harness).

The request path (``create``/``advance``) validates + persists QUEUED + ENQUEUES; the WORKER
claims QUEUED→RUNNING and drives the member DAG through the harness. These tests prove that split
plus a durable human-gate pause and resume, org-scoping, the ceiling 422, fail-not-strand (G-C), and
no-double-exec on resume (G-D). DB-backed RLS + cross-request resume are in the integration test.
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


def _svc(repo: FakeTeamRunRepo, harness: Any) -> tuple[TeamRunService, list[uuid.UUID]]:
    """A service with the enqueue captured (not a broker) — create/advance is a pure DB write."""
    enqueued: list[uuid.UUID] = []
    svc = TeamRunService(
        team_runs=repo, harness=harness, enqueue=lambda rid, _org, _user: enqueued.append(rid)
    )
    return svc, enqueued


async def _run(svc: TeamRunService, principal: Principal, **kwargs: Any) -> EngineTeamRun:
    """Simulate the full path: the request (create → QUEUED + enqueue) then the WORKER (drive)."""
    row = await svc.create(principal, **kwargs)
    return await svc.drive(row.id, principal)


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


# ── the async create → worker-drive split ────────────────────────────────────────────────────


async def test_create_enqueues_a_queued_run_without_driving() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, enqueued = _svc(repo, harness)
    manifest = _team([_agent("researcher"), _agent("writer", ["researcher"])])

    row = await svc.create(_principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})

    assert row.state == "QUEUED"  # the request returns immediately; the worker drives
    assert enqueued == [row.id]  # the run was handed to the worker
    assert harness.inputs == []  # nothing executed on the request path


async def test_worker_drive_runs_the_team_through_the_harness_and_persists() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert row.state == "SUCCEEDED"
    assert len(harness.inputs) == 2  # both members ran on the worker
    assert set(row.results) == {"a", "b"}
    assert repo.rows[row.id].state == "SUCCEEDED"  # persisted


async def test_human_gate_pauses_the_run_durably() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    manifest = _team(
        [_agent("researcher"), _human("approval", ["researcher"]), _agent("writer", ["approval"])]
    )
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert row.state == "PAUSED"
    assert row.paused_at == ["approval"]
    assert "writer" not in row.results
    assert len(harness.inputs) == 1  # only the researcher ran; the writer is gated off


async def test_advance_re_enqueues_a_queued_run_without_driving() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, enqueued = _svc(repo, harness)
    manifest = _team([_agent("researcher"), _human("approval", ["researcher"])])
    paused = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert paused.state == "PAUSED"
    enqueued.clear()

    advanced = await svc.advance(paused.id, _principal(), {"approval": "approve"})

    assert advanced.state == "QUEUED"  # advance returns it to QUEUED; the worker drives the resume
    assert enqueued == [paused.id]  # re-handed to the worker
    assert len(harness.inputs) == 1  # advance itself did NOT drive (still just the researcher)


async def test_advance_then_worker_drive_resumes_past_the_gate() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    manifest = _team(
        [_agent("researcher"), _human("approval", ["researcher"]), _agent("writer", ["approval"])]
    )
    paused = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert paused.state == "PAUSED"

    await svc.advance(paused.id, _principal(), {"approval": "approve"})
    resumed = await svc.drive(paused.id, _principal())  # the worker picks up the re-queued run

    assert resumed.state == "SUCCEEDED"
    assert "writer" in resumed.results  # the gate opened and the writer finally ran
    assert repo.rows[paused.id].gate_decisions == {"approval": "approve"}  # decision persisted


async def test_a_rejected_gate_halts_the_run() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    manifest = _team([_agent("researcher"), _human("approval", ["researcher"])])
    paused = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})

    await svc.advance(paused.id, _principal(), {"approval": "reject"})
    rejected = await svc.drive(paused.id, _principal())

    assert rejected.state == "REJECTED"


async def test_not_a_team_manifest_is_422() -> None:
    svc, _ = _svc(FakeTeamRunRepo(), FakeHarness())
    agent_doc = {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": "a", "owner_organization_id": str(_ORG)},
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }
    with pytest.raises(TeamRunError) as exc:
        await svc.create(_principal(), manifest=agent_doc, sub_harnesses={}, gate_decisions={})
    assert exc.value.status_code == 422


async def test_principal_without_org_is_403() -> None:
    svc, _ = _svc(FakeTeamRunRepo(), FakeHarness())
    with pytest.raises(TeamRunError) as exc:
        await svc.create(
            _principal(org=None), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
        )
    assert exc.value.status_code == 403


async def test_get_is_org_scoped_cross_org_is_not_found() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    row = await _run(
        svc, _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )
    with pytest.raises(TeamRunError) as exc:
        await svc.get(row.id, _principal(org=uuid.uuid4()))  # a different org
    assert exc.value.status_code == 404


# ── G-A ceiling, G-C fail-not-strand + reap, G-D no double-exec ──────────────────────────────


async def test_subharness_exceeding_member_tools_ceiling_is_rejected_422() -> None:
    svc, _ = _svc(FakeTeamRunRepo(), FakeHarness())
    manifest = _team([_agent("r", tools=["Read"])])
    with pytest.raises(TeamRunError) as exc:
        await svc.create(
            _principal(),
            manifest=manifest,
            sub_harnesses={"r": _sub("r", bindings=["shell"])},  # outside the ['Read'] ceiling
            gate_decisions={},
        )
    assert exc.value.status_code == 422


async def test_subharness_within_member_tools_ceiling_is_accepted() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    manifest = _team([_agent("r", tools=["Read", "Grep"])])
    row = await _run(
        svc,
        _principal(),
        manifest=manifest,
        sub_harnesses={"r": _sub("r", bindings=["Read"])},
        gate_decisions={},
    )
    assert row.state == "SUCCEEDED"


async def test_non_harness_error_mid_drive_fails_run_not_strands_it() -> None:
    # G-C: any drive exception (not just HarnessClientError) transitions RUNNING -> FAILED, never
    # leaves the row stuck RUNNING forever.
    class BoomHarness:
        async def execute(self, **kwargs: Any) -> dict[str, Any]:
            raise ValueError("decode blew up")  # NOT a HarnessClientError

    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, BoomHarness())
    row = await _run(
        svc, _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
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

    svc = TeamRunService(team_runs=repo)  # reaper path: no harness, no enqueue
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
    svc, _ = _svc(repo, harness)
    manifest = _team(
        [_agent("researcher"), _human("approval", ["researcher"]), _agent("writer", ["approval"])]
    )
    paused = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert paused.state == "PAUSED"
    assert len(harness.inputs) == 1  # researcher ran once before the gate

    await svc.advance(paused.id, _principal(), {"approval": "approve"})
    await svc.drive(paused.id, _principal())

    researcher_runs = sum(1 for i in harness.inputs if "researcher" in i)
    assert researcher_runs == 1  # researcher fired exactly once across create + resume (not twice)
    assert len(harness.inputs) == 2  # only researcher + writer, never a re-run


async def test_cancellation_mid_drive_marks_failed_then_propagates() -> None:
    # G-C: a cancellation (asyncio.CancelledError is BaseException, not Exception, in 3.12) must NOT
    # strand the row RUNNING — it is marked FAILED, then the CancelledError propagates.
    import asyncio

    class CancelHarness:
        async def execute(self, **kwargs: Any) -> dict[str, Any]:
            raise asyncio.CancelledError

    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, CancelHarness())
    row = await svc.create(
        _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )
    with pytest.raises(asyncio.CancelledError):
        await svc.drive(row.id, _principal())
    assert repo.rows[row.id].state == "FAILED"  # marked FAILED before propagation, not stranded
