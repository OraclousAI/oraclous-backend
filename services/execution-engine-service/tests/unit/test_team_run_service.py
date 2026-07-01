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
        workspace_root: str | None = None,
        graph_id: str | None = None,
        inputs: dict[str, Any] | None = None,
        seed_from_run_id: uuid.UUID | None = None,
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
            workspace_root=workspace_root,
            graph_id=graph_id,
            inputs=inputs,
            seed_from_run_id=seed_from_run_id,
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
        self.calls: list[dict[str, Any]] = []  # #471: record the threaded trace_id/parent per call

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
        graph_id: str | None = None,
        team_id: str | None = None,
        precedence_order: list[str] | None = None,  # additive (#538) — accepted, ignored here
        graph_authoritative: bool = False,
    ) -> dict[str, Any]:
        self.inputs.append(input_text)
        eid = uuid.uuid4()  # each member 'execution' gets an id → the engine records the tree
        self.calls.append(
            {
                "id": eid,
                "parent_execution_id": parent_execution_id,
                "trace_id": trace_id,
                "precedence_order": precedence_order,  # #538: assert _drive extracts from manifest
                "graph_authoritative": graph_authoritative,
            }
        )
        return {
            "id": str(eid),
            "status": "SUCCEEDED",
            "output": f"done: {input_text[:30]}",
            "total_tokens": 100,  # #472: each member 'costs' 100 raw tokens → engine accumulates
        }


class FakeEvaluate:
    """A stand-in EvaluateClient (#477): records each call, returns a scripted Verdict or raises."""

    def __init__(
        self,
        *,
        score: float = 0.8,
        passed: bool = True,
        raise_exc: Exception | None = None,
        recommended_action: str | None = None,  # #604: override the derived action (revise/reject…)
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._score, self._passed, self._raise = score, passed, raise_exc
        self._action = recommended_action

    async def evaluate(
        self,
        *,
        target_ref: str,
        target_output: str,
        success_criteria: str,
        target_kind: str = "run",
        pass_threshold: float = 0.7,
        judge_credential_id: str | None = None,
        judge_model: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "target_ref": target_ref,
                "success_criteria": success_criteria,
                "output": target_output,
                "judge_credential_id": judge_credential_id,
                "judge_model": judge_model,
            }
        )
        if self._raise is not None:
            raise self._raise
        return {
            "pass": self._passed,
            "score": self._score,
            "recommended_action": self._action or ("accept" if self._passed else "escalate_human"),
        }

    async def aclose(self) -> None:  # pragma: no cover - parity with the real client
        return None


def _svc(
    repo: FakeTeamRunRepo, harness: Any, *, evaluate: Any = None
) -> tuple[TeamRunService, list[uuid.UUID]]:
    """A service with the enqueue captured (not a broker) — create/advance is a pure DB write."""
    enqueued: list[uuid.UUID] = []
    svc = TeamRunService(
        team_runs=repo,
        harness=harness,
        enqueue=lambda rid, _org, _user: enqueued.append(rid),
        evaluate=evaluate,
    )
    return svc, enqueued


async def _run(svc: TeamRunService, principal: Principal, **kwargs: Any) -> EngineTeamRun:
    """Simulate the full path: the request (create → QUEUED + enqueue) then the WORKER (drive)."""
    row = await svc.create(principal, **kwargs)
    return await svc.drive(row.id, principal)


def _team(
    members: list[dict[str, Any]],
    *,
    success_criteria: str | None = None,
    precedence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    team: dict[str, Any] = {
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
    if success_criteria is not None:  # #477 — declare the flow-evaluation gate
        team["orchestration"] = {"success_criteria": success_criteria}
    if precedence is not None:  # #538 — declare the Hierarchy of Truth (order + graph mode)
        team["precedence"] = precedence
    return team


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


def test_create_team_run_request_carries_inputs() -> None:
    # #599: the request schema accepts the user-seeded `inputs` dict and defaults it to None.
    from oraclous_execution_engine_service.schema.engine_schemas import CreateTeamRunRequest

    seeded = {"items": ["a", "b"]}
    req = CreateTeamRunRequest(manifest={"k": "v"}, inputs=seeded)
    assert req.inputs == seeded  # the field round-trips
    assert CreateTeamRunRequest(manifest={"k": "v"}).inputs is None  # optional, defaults None


async def test_create_threads_user_seeded_inputs_to_the_repo(monkeypatch: Any) -> None:
    # #599: the per-run `inputs` (user-seeded team state for a fan_out.over: "$.<key>") flows from
    # the request → service.create → repo.create → the persisted row, so the worker's run_team can
    # resolve a fan-out's seeded list. Assert the repo received it AND the row carries it.
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    captured: dict[str, Any] = {}
    orig_create = repo.create

    async def _capturing_create(**kwargs: Any) -> EngineTeamRun:
        captured.update(kwargs)
        return await orig_create(**kwargs)

    monkeypatch.setattr(repo, "create", _capturing_create)
    svc, _ = _svc(repo, harness)
    seeded = {"items": ["i1", "i2", "i3"]}

    row = await svc.create(
        _principal(),
        manifest=_team([_agent("w")]),
        sub_harnesses={},
        gate_decisions={},
        inputs=seeded,
    )
    assert captured["inputs"] == seeded  # the repo create received the seeded state
    assert repo.rows[row.id].inputs == seeded  # and the persisted row carries it


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


async def test_drive_extracts_authoritative_precedence_from_the_manifest() -> None:
    """#538 _drive extraction: a declared `graph: authoritative` Hierarchy of Truth is read off the
    MANIFEST and threaded to every member's harness call (the layer above run_team_harness, which is
    where the `graph == "authoritative"` comparison + the order ternary live)."""
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    order = ["rules", "bible", "toc", "drafts"]
    await _run(
        svc,
        _principal(),
        manifest=_team(
            [_agent("a"), _agent("b", ["a"])],
            precedence={"order": order, "graph": "authoritative"},
        ),
        sub_harnesses={},
        gate_decisions={},
    )
    assert len(harness.calls) == 2
    assert all(c["precedence_order"] == order for c in harness.calls)
    assert all(c["graph_authoritative"] is True for c in harness.calls)  # graph == "authoritative"


async def test_drive_extracts_derived_precedence_as_graph_authoritative_false() -> None:
    """The default/derived mode: `graph: derived` threads the order but `graph_authoritative=False`
    (the comparison is against "authoritative", not "derived") — the common production case."""
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    order = ["rules", "bible", "drafts"]
    await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a")], precedence={"order": order, "graph": "derived"}),
        sub_harnesses={},
        gate_decisions={},
    )
    assert harness.calls[0]["precedence_order"] == order
    assert harness.calls[0]["graph_authoritative"] is False


async def test_drive_without_precedence_threads_none_fail_soft() -> None:
    """Back-compat: a team with no declared Hierarchy of Truth threads None/False to members."""
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a")]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert harness.calls[0]["precedence_order"] is None
    assert harness.calls[0]["graph_authoritative"] is False


async def test_run_tree_records_root_and_children_and_threads_trace(  # ADR-037 D3 / #471
) -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )
    # the run is its own tree root (root_execution_id minted = its own id)
    assert row.root_execution_id == row.id
    # every member's harness execution id is recorded as a child → the tree is reassemblable
    member_ids = [str(c["id"]) for c in harness.calls]
    assert sorted(row.child_execution_ids) == sorted(member_ids)
    assert len(row.child_execution_ids) == 2
    # each member run was threaded with trace_id == the root and parent == the root
    assert all(c["trace_id"] == row.id for c in harness.calls)
    assert all(c["parent_execution_id"] == row.id for c in harness.calls)
    # O4 metering (#472): the run's cost accumulates each member's total_tokens (2 × 100)
    assert row.cost_tokens == 200


async def test_status_reports_full_progress_and_cost_on_completion() -> None:  # ADR-037 D5 / #472
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    p = _principal()
    row = await _run(
        svc,
        p,
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )
    st = await svc.status(row.id, p)
    assert st.state == "SUCCEEDED" and st.healthy is True
    assert st.progress == 100  # both members done → goal attained (not the old hardcoded 5/100)
    assert st.cost_tokens == 200  # Σ the members' total_tokens (2 × 100)


async def test_status_progress_is_partial_when_paused_at_a_gate() -> None:  # #472
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness)
    p = _principal()
    manifest = _team(
        [_agent("researcher"), _human("approval", ["researcher"]), _agent("writer", ["approval"])]
    )
    row = await _run(svc, p, manifest=manifest, sub_harnesses={}, gate_decisions={})
    st = await svc.status(row.id, p)
    assert st.state == "PAUSED" and st.healthy is True  # paused at a human gate is HEALTHY
    assert st.progress == 33  # 1 of 3 members done before the gate → goal-attainment 33%


async def test_gate_below_threshold_escalate_verdict_is_consumed_pauses_for_hitl() -> None:  # #604
    """E8 (#604, ADR-048 dec 5): the E4/E8 boundary MOVED — the settled verdict is now CONSUMED. A
    below-threshold verdict whose recommended_action is escalate_human PAUSES the run for HITL (with
    the verdict-escalation sentinel), NOT a blind stay-SUCCEEDED (the pre-E8 deferral this test used
    to guard). It does NOT re-dispatch (escalate ≠ re_task), so nothing new is enqueued."""
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(score=0.2, passed=False)  # a real FAILING verdict → escalate_human
    svc, enqueued = _svc(repo, harness, evaluate=evaluate)
    manifest = _team([_agent("a"), _agent("b", ["a"])], success_criteria="the result is correct")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    # the verdict is produced + stored, and the run is CONSUMED — escalated to PAUSED for a human
    assert row.verdict is not None and row.verdict["pass"] is False
    assert evaluate.calls and evaluate.calls[0]["success_criteria"] == "the result is correct"
    assert row.state == "PAUSED"  # #604: escalate_human → HITL, not a silent SUCCEEDED
    assert row.paused_at == ["__verdict_escalation__"]  # the sentinel (distinct from a member gate)
    assert row.escalation_kind == "verdict"  # the CONTROL marker advance() keys off (review F1)
    # escalate does NOT re-dispatch (only the create's enqueue — no re_task enqueue)
    assert enqueued == [row.id]


async def test_gate_eval_failure_is_fail_closed_and_run_still_succeeds() -> None:  # #477
    from oraclous_execution_engine_service.services.evaluate_client import EvaluateClientError

    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(raise_exc=EvaluateClientError("judge unreachable"))
    svc, _ = _svc(repo, harness, evaluate=evaluate)
    manifest = _team([_agent("a")], success_criteria="is correct")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert (
        row.state == "SUCCEEDED"
    )  # the run's success is INDEPENDENT of the grader being reachable
    assert (
        row.verdict is not None and row.verdict["pass"] is False
    )  # a fail-closed verdict recorded


async def test_undeclared_battery_success_criteria_is_422_at_create() -> None:  # #479
    """A `battery:<name>` success_criteria naming a battery NOT in manifest.batteries is rejected at
    CREATE (422) — fail-fast, so it can never reach grade time as an UnknownBattery that strands the
    run in RUNNING (the original #477 defect)."""
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, _ = _svc(repo, harness, evaluate=FakeEvaluate())
    manifest = _team([_agent("a")], success_criteria="battery:does-not-exist")
    with pytest.raises(TeamRunError) as exc:
        await svc.create(_principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert exc.value.status_code == 422


async def test_unexpected_grader_error_is_fail_closed_not_a_strand() -> None:  # #479
    """An UNEXPECTED grader error type (not in the old narrow except tuple) must STILL fail closed —
    the catch-all keeps the grade off _drive's try/except path from stranding the run RUNNING."""
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(raise_exc=RuntimeError("boom"))  # RuntimeError — not in the old tuple
    svc, _ = _svc(repo, harness, evaluate=evaluate)
    manifest = _team([_agent("a")], success_criteria="is correct")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert row.state == "SUCCEEDED"  # the run is NOT stranded — the catch-all caught it
    assert row.verdict is not None and row.verdict["pass"] is False  # fail-closed verdict recorded


async def test_no_success_criteria_skips_grading() -> None:  # #477
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate()
    svc, _ = _svc(repo, harness, evaluate=evaluate)
    row = await _run(
        svc, _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )
    assert evaluate.calls == []  # no gate declared → the grader is never called
    assert row.verdict is None and row.state == "SUCCEEDED"


async def test_gate_threads_evaluator_credential_when_declared() -> None:  # BYOM-judge
    """A manifest role='evaluator' model makes the engine thread judge_credential_id + the whole
    binding into the core/evaluate call (so KRS grades with the user's own key)."""
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(score=0.9, passed=True)
    svc, _ = _svc(repo, harness, evaluate=evaluate)
    manifest = _team([_agent("a")], success_criteria="is good")
    manifest["models"] = [
        {
            "role": "evaluator",
            "binding": "openrouter/openai/gpt-4o-mini",
            "protocol_shape": "openai-compatible",
            "config": {"credential_id": "cred-byom"},
        }
    ]
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert row.state == "SUCCEEDED" and row.verdict["pass"] is True
    call = evaluate.calls[0]
    assert call["judge_credential_id"] == "cred-byom"
    assert call["judge_model"] == "openrouter/openai/gpt-4o-mini"  # WHOLE binding; KRS splits


async def test_gate_omits_evaluator_credential_when_not_declared() -> None:  # BYOM-judge fallback
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate()
    svc, _ = _svc(repo, harness, evaluate=evaluate)
    manifest = _team([_agent("a")], success_criteria="is good")  # no evaluator model
    await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    call = evaluate.calls[0]
    assert call["judge_credential_id"] is None  # → KRS uses the operator key
    assert call["judge_model"] is None


async def test_progress_blends_verdict_score_capped_by_completion() -> None:  # #477
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(score=0.6, passed=True)
    svc, _ = _svc(repo, harness, evaluate=evaluate)
    manifest = _team([_agent("a"), _agent("b", ["a"])], success_criteria="is good")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    st = await svc.status(row.id, _principal())
    # both members done (completion 100) but the evaluator graded 0.6 → goal-attainment 60
    assert st.progress == 60


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


async def test_member_failure_persists_per_member_status_and_keeps_independent_output() -> None:
    # ADR-042 (#551): member 'b' fails; the independent 'a' (same stage, no dep) still SUCCEEDS. The
    # drive persists each member's terminal status, the run verdict is FAILED (not SUCCEEDED), 'a''s
    # output is KEPT (not discarded by a sibling's failure), and the failed member's safe detail
    # is surfaced in error_message — so the failed member is both debuggable and re-runnable.
    class _MixedHarness:
        async def execute(self, **kw: Any) -> dict[str, Any]:
            if "do b" in kw.get("input_text", ""):  # fail ONLY member 'b' (subgoal "do b")
                return {"status": "FAILED", "output": None, "error_message": "b blew up"}
            return {"status": "SUCCEEDED", "output": "ok"}

    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, _MixedHarness())
    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b")]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert row.state == "FAILED"  # SUCCEEDED iff EVERY member delivered — b did not
    assert row.member_status == {"a": "succeeded", "b": "failed"}
    assert row.results.get("a") == {
        "output": "ok",
        "status": "SUCCEEDED",
    }  # the peer's work is kept
    assert "b blew up" in (row.error_message or "")  # the failed member's detail is surfaced


async def test_rerun_redispatches_only_the_failed_member_and_reaches_succeeded() -> None:
    # ADR-042 (#551): first drive — 'b' fails (a transient that has cleared by the re-run), 'a'
    # succeeds. rerun() re-drives ONLY 'b' (now succeeds) and KEEPS 'a' (never re-dispatched), so
    # the run reaches SUCCEEDED with every member — the re-run-from-the-durable-team-state path.
    class _FlakyHarness:
        def __init__(self) -> None:
            self.a_calls = 0
            self.b_attempts = 0

        async def execute(self, **kw: Any) -> dict[str, Any]:
            if "do b" in kw.get("input_text", ""):
                self.b_attempts += 1
                if self.b_attempts == 1:  # fails the FIRST time, succeeds on the re-run
                    return {
                        "status": "FAILED",
                        "output": None,
                        "error_message": "b throttled",
                        "total_tokens": 100,
                    }
                return {"status": "SUCCEEDED", "output": "b-ok", "total_tokens": 100}
            self.a_calls += 1
            return {"status": "SUCCEEDED", "output": "a-ok", "total_tokens": 100}

    repo = FakeTeamRunRepo()
    harness = _FlakyHarness()
    svc, _ = _svc(repo, harness)
    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b")]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert row.state == "FAILED" and row.member_status == {"a": "succeeded", "b": "failed"}
    assert harness.a_calls == 1

    requeued = await svc.rerun(row.id, _principal())  # FAILED → QUEUED
    assert requeued.state == "QUEUED"
    final = await svc.drive(row.id, _principal())  # the worker re-drives only the failures
    assert final.state == "SUCCEEDED"  # every member delivered on the re-run
    assert final.member_status == {"a": "succeeded", "b": "succeeded"}
    assert harness.a_calls == 1  # 'a' was NOT re-dispatched (its success is reused)
    assert harness.b_attempts == 2  # 'b' re-dispatched exactly once on the re-run
    assert final.error_message is None  # the prior failure summary is cleared on a clean re-run
    # O4 cost (#472) accumulates every REAL attempt: a once (100) + b's failed attempt (100, counted
    # before the fail-closed check) + b's re-run (100) = 300 — every real attempt, not a dup.
    assert final.cost_tokens == 300


async def test_rerun_rejects_a_non_failed_run() -> None:
    # only a FAILED run is re-runnable; a SUCCEEDED (or QUEUED/RUNNING) run → 409.
    class _OkHarness:
        async def execute(self, **kw: Any) -> dict[str, Any]:
            return {"status": "SUCCEEDED", "output": "ok"}

    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, _OkHarness())
    row = await _run(
        svc, _principal(), manifest=_team([_agent("a")]), sub_harnesses={}, gate_decisions={}
    )
    assert row.state == "SUCCEEDED"
    with pytest.raises(TeamRunError) as ei:
        await svc.rerun(row.id, _principal())
    assert ei.value.status_code == 409


async def test_rerun_409_when_failed_run_has_no_failed_or_blocked_members() -> None:
    # ADR-042 (#551): a FAILED run with NO recorded member failure (e.g. a hard mid-drive crash /
    # reaped run, member_status left {}) has nothing to recover → rerun is a 409 nothing_to_rerun.
    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, FakeHarness())
    crashed = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=_team([_agent("a")]),
        sub_harnesses={},
        gate_decisions={},
        state="FAILED",
        results={},
        paused_at=[],
        member_status={},  # no per-member failure recorded (the run crashed before recording)
    )
    repo.rows[crashed.id] = crashed
    with pytest.raises(TeamRunError) as ei:
        await svc.rerun(crashed.id, _principal())
    assert ei.value.status_code == 409
    assert ei.value.error_type == "nothing_to_rerun"


async def test_failed_run_reports_true_progress_not_inflated_to_100() -> None:
    # ADR-042 (#551): a failed/blocked member now populates results[role]=None, so progress must be
    # computed from member_status (delivered), NOT len(results) — else a FAILED run reports ~100%.
    class _MixedHarness:
        async def execute(self, **kw: Any) -> dict[str, Any]:
            if "do b" in kw.get("input_text", ""):
                return {"status": "FAILED", "output": None, "error_message": "boom"}
            return {"status": "SUCCEEDED", "output": "ok"}

    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, _MixedHarness())
    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b")]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert row.state == "FAILED" and row.member_status == {"a": "succeeded", "b": "failed"}
    status = await svc.status(row.id, _principal())
    assert status.state == "FAILED"
    assert status.healthy is False
    assert status.progress == 50  # 1 of 2 members delivered — NOT 100 (the inflated len(results))


async def test_back_compat_resume_of_a_pre_member_status_paused_run_does_not_re_dispatch() -> None:
    # ADR-042 (#551) back-compat: a PAUSED run created BEFORE the member_status column existed has
    # member_status={} but real pre-gate results. _completed_for_resume falls back to all-results
    # so its gate-resume still REUSES the pre-gate member (G-D, no double-exec), not re-dispatch.
    harness = FakeHarness()
    repo = FakeTeamRunRepo()
    svc, enq = _svc(repo, harness)
    paused = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=_team([_agent("a"), _human("gate", ["a"]), _agent("writer", ["gate"])]),
        sub_harnesses={},
        gate_decisions={},
        state="PAUSED",
        results={
            "a": {"output": "a-done", "status": "SUCCEEDED"}
        },  # the pre-gate member already ran
        paused_at=["gate"],
        member_status={},  # the pre-ADR-042 row: no per-member status recorded
    )
    repo.rows[paused.id] = paused
    await svc.advance(paused.id, _principal(), {"gate": "approve"})  # decide the gate → QUEUED
    final = await svc.drive(paused.id, _principal())
    assert final.state == "SUCCEEDED"
    # 'a' was seeded from the durable results (back-compat fallback) and NOT re-dispatched; only the
    # gate-downstream 'writer' ran on the resume.
    assert not any("do a" in i for i in harness.inputs)
    assert any("do writer" in i for i in harness.inputs)


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


def test_completed_for_resume_seeds_partial_members_so_they_are_not_redispatched() -> None:
    # #587 (review SHOULD-FIX): a "partial" (degraded) member is terminal/done — seeded on re-run so
    # it is NOT re-dispatched (no token re-spend, no side-effect re-fire), like a succeeded member.
    from oraclous_execution_engine_service.services.team_run_service import TeamRunService

    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest={},
        sub_harnesses={},
        gate_decisions={},
        state="FAILED",
        results={"a": {"output": "ok"}, "b": {"output": "best-effort"}, "c": None},
        member_status={"a": "succeeded", "b": "partial", "c": "failed"},
        paused_at=[],
    )
    seeded = TeamRunService._completed_for_resume(row)
    assert set(seeded) == {"a", "b"}  # succeeded + partial reused; the failed member 'c' re-runs


# ── #601: per-cadence cost accrual (a scheduled run's settled cost → its schedule) ────────────────


class _FakeSchedAccrual:
    def __init__(self) -> None:
        self.accrued: list[tuple[uuid.UUID, uuid.UUID, int]] = []

    async def accrue_recurring_cost(
        self, schedule_id: uuid.UUID, organisation_id: uuid.UUID, delta: int
    ) -> None:
        self.accrued.append((schedule_id, organisation_id, delta))


async def test_scheduled_run_accrues_its_cost_into_the_schedule() -> None:
    from types import SimpleNamespace

    sched = _FakeSchedAccrual()
    svc = TeamRunService(team_runs=FakeTeamRunRepo(), schedules=sched)  # type: ignore[arg-type]
    sid = uuid.uuid4()
    row = SimpleNamespace(id=uuid.uuid4(), organisation_id=_ORG, schedule_id=sid)
    await svc._accrue_schedule_cost(row, _ORG, 1234)  # type: ignore[arg-type]
    assert sched.accrued == [(sid, _ORG, 1234)]  # the THIS-DRIVE delta accrued into the schedule


async def test_non_scheduled_run_accrues_nothing() -> None:
    from types import SimpleNamespace

    sched = _FakeSchedAccrual()
    svc = TeamRunService(team_runs=FakeTeamRunRepo(), schedules=sched)  # type: ignore[arg-type]
    row = SimpleNamespace(id=uuid.uuid4(), organisation_id=_ORG, schedule_id=None)
    await svc._accrue_schedule_cost(row, _ORG, 1234)  # type: ignore[arg-type]
    assert sched.accrued == []  # a direct (request-path) run carries no schedule_id → no accrual


# ── #602 seeded-refresh: validation + seed-thread + settle delta + default-OFF ──────────────────
_MANIFEST = _team([_agent("reporter")])
_SEED_LEDGER = '[{"id": "1", "v": "x"}, {"id": "2", "v": "y"}, {"id": "3", "v": "z"}]'
# fresh vs seed: id 1 carried-forward (skip marker), id 2 changed, id 3 removed, id 4 added
_FRESH_LEDGER = (
    '[{"id": "1", "v": "x", "refresh_status": "unchanged"}, '
    '{"id": "2", "v": "Y-NEW"}, '
    '{"id": "4", "v": "new"}]'
)


class _LedgerHarness:
    """A harness whose sink member emits a fixed JSON-array ledger — the deliverable to diff."""

    def __init__(self, ledger_json: str) -> None:
        self._ledger = ledger_json
        self.inputs: list[str] = []

    async def execute(self, *, input_text: str, **_kw: Any) -> dict[str, Any]:
        self.inputs.append(input_text)
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": self._ledger,
            "total_tokens": 100,
        }


def _succeeded_seed(
    repo: FakeTeamRunRepo, ledger_json: str, *, org: uuid.UUID = _ORG, state: str = "SUCCEEDED"
) -> EngineTeamRun:
    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=org,
        user_id=_USER,
        manifest=_MANIFEST,
        sub_harnesses={},
        gate_decisions={},
        state=state,
        results={"reporter": ledger_json},
        paused_at=[],
    )
    repo.rows[row.id] = row
    return row


async def _create_refresh(svc: TeamRunService, seed_id: uuid.UUID) -> EngineTeamRun:
    return await svc.create(
        _principal(),
        manifest=_MANIFEST,
        sub_harnesses={},
        gate_decisions={},
        seed_from_run_id=seed_id,
    )


async def test_create_rejects_a_missing_seed_run() -> None:
    svc, _ = _svc(FakeTeamRunRepo(), FakeHarness())
    with pytest.raises(TeamRunError) as exc:
        await _create_refresh(svc, uuid.uuid4())
    assert exc.value.status_code == 422 and exc.value.error_type == "invalid_seed_run"


async def test_create_rejects_a_cross_org_seed_run() -> None:
    # a seed run belonging to ANOTHER org is invisible to the org-scoped get → 422, never a leak
    repo = FakeTeamRunRepo()
    seed = _succeeded_seed(repo, _SEED_LEDGER, org=uuid.uuid4())
    svc, _ = _svc(repo, FakeHarness())
    with pytest.raises(TeamRunError) as exc:
        await _create_refresh(svc, seed.id)
    assert exc.value.status_code == 422 and exc.value.error_type == "invalid_seed_run"


async def test_create_rejects_a_non_succeeded_seed_run() -> None:
    repo = FakeTeamRunRepo()
    seed = _succeeded_seed(repo, _SEED_LEDGER, state="FAILED")
    svc, _ = _svc(repo, FakeHarness())
    with pytest.raises(TeamRunError) as exc:
        await _create_refresh(svc, seed.id)
    assert exc.value.status_code == 422 and exc.value.error_type == "invalid_seed_run"


async def test_create_threads_the_seed_records_into_inputs() -> None:
    repo = FakeTeamRunRepo()
    seed = _succeeded_seed(repo, _SEED_LEDGER)
    svc, _ = _svc(repo, FakeHarness())
    row = await _create_refresh(svc, seed.id)
    assert row.seed_from_run_id == seed.id
    seeded = (row.inputs or {}).get("_refresh_seed")
    assert seeded is not None and [r["id"] for r in seeded["records"]] == ["1", "2", "3"]


async def test_refresh_run_settles_with_a_5way_delta() -> None:
    repo = FakeTeamRunRepo()
    seed = _succeeded_seed(repo, _SEED_LEDGER)
    svc, _ = _svc(repo, _LedgerHarness(_FRESH_LEDGER))
    row = await _run(
        svc,
        _principal(),
        manifest=_MANIFEST,
        sub_harnesses={},
        gate_decisions={},
        seed_from_run_id=seed.id,
    )
    d = row.refresh_delta
    assert d is not None and d["seed_from_run_id"] == str(seed.id)
    assert d["counts"] == {
        "added": 1,
        "removed": 1,
        "changed": 1,
        "unchanged": 1,
        "re_confirmed": 0,
    }
    assert d["added"][0]["id"] == "4" and d["removed"][0]["id"] == "3"
    assert d["unchanged"][0]["id"] == "1" and d["skipped"] == 1


async def test_non_refresh_run_is_default_off() -> None:
    # a run with no seed: no delta, and its inputs are never touched by the refresh path
    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, _LedgerHarness(_FRESH_LEDGER))
    row = await _run(svc, _principal(), manifest=_MANIFEST, sub_harnesses={}, gate_decisions={})
    assert row.seed_from_run_id is None
    assert row.refresh_delta is None
    assert "_refresh_seed" not in (row.inputs or {})


# ── #604 closed-loop verdict-consumption (ADR-048 decision 5) ──────────────────────────────
async def test_re_task_re_dispatches_the_sink_with_a_revised_objective_not_a_blind_rerun() -> None:
    # a below-threshold `revise` verdict on a SUCCEEDED run → re-dispatch: the SINK member is forced
    # to re-run (its output was below threshold), the objective carries a revision directive (so the
    # task DIFFERS — never a blind identical re-run), and the run goes QUEUED (drawing the pool).
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(passed=False, score=0.5, recommended_action="revise")
    svc, enqueued = _svc(repo, harness, evaluate=evaluate)
    manifest = _team([_agent("a"), _agent("b", ["a"])], success_criteria="the result is correct")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert row.state == "QUEUED"  # re-dispatched, not left SUCCEEDED (consumed the verdict)
    assert row.re_dispatch_count == 1  # the loop counter bumped (the MAX-ceiling basis)
    assert row.member_status.get("b") == "re_task"  # the SINK is forced to re-run
    assert row.member_status.get("a") == "succeeded"  # the upstream member is reused (not re-run)
    sink = next(m for m in row.manifest["members"] if m["role"] == "b")
    assert "Re-task attempt 1" in (sink.get("subgoal") or "")  # objective DIFFERS (revision)
    assert enqueued == [row.id, row.id]  # the create's enqueue + the re_task enqueue


async def test_reject_verdict_escalates_to_hitl_and_never_re_dispatches() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(passed=False, recommended_action="reject")  # HITL-class (ADR-037 Dec 4)
    svc, enqueued = _svc(repo, harness, evaluate=evaluate)
    manifest = _team([_agent("a")], success_criteria="is correct")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert row.state == "PAUSED" and row.paused_at == ["__verdict_escalation__"]
    assert enqueued == [row.id]  # reject NEVER autonomously re-dispatches (no re_task enqueue)


async def test_max_re_dispatches_ceiling_escalates_instead_of_re_tasking() -> None:
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(passed=False, score=0.5, recommended_action="revise")
    svc, enqueued = _svc(repo, harness, evaluate=evaluate)
    manifest = _team([_agent("a")], success_criteria="is correct")
    created = await svc.create(_principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    repo.rows[created.id].re_dispatch_count = 3  # already at the ceiling
    row = await svc.drive(created.id, _principal())
    assert row.state == "PAUSED"  # a closed loop MUST terminate — escalate at the ceiling
    assert row.paused_at == ["__verdict_escalation__"]
    assert enqueued == [created.id]  # no further re_task enqueue


async def test_advance_of_a_verdict_escalation_re_tasks_never_blindly_re_drives() -> None:
    # the Q3 guard: a human advancing a VERDICT-escalation must NOT blindly re-drive the seeded-
    # complete run (a no-op re-grade) — it re-tasks the faulted members with a FRESH loop counter.
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(passed=False, recommended_action="reject")
    svc, enqueued = _svc(repo, harness, evaluate=evaluate)
    manifest = _team([_agent("a"), _agent("b", ["a"])], success_criteria="is correct")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert row.state == "PAUSED" and row.paused_at == ["__verdict_escalation__"]
    repo.rows[row.id].re_dispatch_count = 2  # a prior autonomous history
    advanced = await svc.advance(row.id, _principal(), {})
    assert advanced.state == "QUEUED"  # re-dispatched (NOT a blind seeded re-drive)
    assert advanced.member_status.get("b") == "re_task"  # the sink forced to re-run
    assert advanced.re_dispatch_count == 0  # a fresh human attempt — the livelock counter resets
    assert advanced.paused_at == []  # the escalation sentinel cleared
    assert row.id in enqueued


async def test_re_task_enqueue_failure_fails_the_run_not_phantom_queued() -> None:
    # a broker fault on the re_task hand-off must FAIL the run, not orphan it QUEUED (#620).
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(passed=False, score=0.5, recommended_action="revise")
    calls = {"n": 0}

    def flaky(_rid: uuid.UUID, _org: uuid.UUID, _user: uuid.UUID) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:  # the create's enqueue works; the re_task hand-off (the 2nd) fails
            raise RuntimeError("broker down")

    svc = TeamRunService(team_runs=repo, harness=harness, enqueue=flaky, evaluate=evaluate)
    manifest = _team([_agent("a")], success_criteria="is correct")
    created = await svc.create(_principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    with pytest.raises(RuntimeError):
        await svc.drive(created.id, _principal())
    assert repo.rows[created.id].state == "FAILED"  # compensated QUEUED→FAILED, not phantom-QUEUED
    assert repo.rows[created.id].error_message == "re_dispatch enqueue failed"


async def test_a_member_named_the_sentinel_cannot_hijack_the_verdict_escalation_resume() -> None:
    # review F1-sentinel: a NORMAL human gate whose member role happens to be the escalation
    # sentinel must advance through the ordinary gate path — the Q3 re-task guard keys off
    # ``escalation_kind`` (NULL here), NOT ``paused_at``, so a tenant-named member cannot hijack it.
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    svc, enqueued = _svc(repo, harness)
    created = await svc.create(
        _principal(),
        manifest=_team([_agent("a")]),
        sub_harnesses={},
        gate_decisions={},
    )
    row = repo.rows[created.id]
    row.state = "PAUSED"  # a normal mid-drive gate paused ON a member literally named the sentinel
    row.paused_at = ["__verdict_escalation__"]
    row.escalation_kind = (
        None  # a real gate is NOT a verdict escalation — the discriminator is NULL
    )
    advanced = await svc.advance(created.id, _principal(), {"__verdict_escalation__": "approve"})
    assert advanced.state == "QUEUED"  # the ordinary gate advance, NOT a re-task hijack
    assert advanced.gate_decisions == {"__verdict_escalation__": "approve"}  # the decision recorded
    assert advanced.re_dispatch_count in (0, None)  # never went through the re_task counter
    assert created.id in enqueued


async def test_re_task_revision_reaches_a_handoff_driven_sink_not_just_its_subgoal() -> None:
    # review F1-team_run: a sink RENDERS its objective from its producer's ``handoff_objective``
    # (objective_slice takes precedence over the sink's static subgoal). The revision directive must
    # therefore land on the producer's handoff_objective too, or the re-run is a blind identical.
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(passed=False, score=0.5, recommended_action="revise")
    svc, _ = _svc(repo, harness, evaluate=evaluate)
    producer = {**_agent("a"), "handoff_objective": "Draft chapter 4"}
    sink = _agent("b", ["a"])  # nothing depends on b → the sink; b renders a's handoff_objective
    manifest = _team([producer, sink], success_criteria="the result is correct")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert (
        row.state == "QUEUED" and row.member_status.get("b") == "re_task"
    )  # the sink re-dispatched
    prod = next(m for m in row.manifest["members"] if m["role"] == "a")
    # the revision landed on the producer's handoff_objective → it reaches the sink's rendered input
    assert "Re-task attempt 1" in prod["handoff_objective"]
    assert prod["handoff_objective"].startswith(
        "Draft chapter 4"
    )  # the original objective preserved


async def test_resume_verdict_escalation_enqueue_failure_fails_the_run_not_phantom_queued() -> None:
    # review F1-org_scope: the human-resume compensation runs under org_scope, so a broker fault
    # FAILS the run (not a silent RLS no-op leaving it phantom-QUEUED). Proven with the fake repo's
    # CAS semantics; the RLS-real path is the same transition wrapped in the same org_scope.
    repo, harness = FakeTeamRunRepo(), FakeHarness()
    evaluate = FakeEvaluate(passed=False, recommended_action="reject")  # → escalate → PAUSED
    calls = {"n": 0}

    def flaky(_rid: uuid.UUID, _org: uuid.UUID, _user: uuid.UUID) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:  # create's enqueue works; the human-resume re_task hand-off fails
            raise RuntimeError("broker down")

    svc = TeamRunService(team_runs=repo, harness=harness, enqueue=flaky, evaluate=evaluate)
    manifest = _team([_agent("a"), _agent("b", ["a"])], success_criteria="is correct")
    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})
    assert row.state == "PAUSED" and row.escalation_kind == "verdict"
    with pytest.raises(RuntimeError):
        await svc.advance(row.id, _principal(), {})
    assert repo.rows[row.id].state == "FAILED"  # compensated, not phantom-QUEUED
    assert repo.rows[row.id].error_message == "re_dispatch enqueue failed"
