"""#819 — a team run's member results are durable DURING the drive, not only when it settles.

Today the drive writes nothing between the ``QUEUED→RUNNING`` claim and the settle write. Celery
kills the task with ``SoftTimeLimitExceeded`` at 50 minutes and SIGKILLs it at 60; either way the
run lands FAILED with ``member_status`` still ``{}`` and ``results`` still empty, so ``POST /rerun``
answers ``409 nothing_to_rerun`` and EVERY member's output is unrecoverable — including the ones
that finished forty minutes earlier.

The owner's ruling on the issue (2026-08-16) settles the four open decisions:

1. **Checkpoint per member completion**, through a new repository ``checkpoint`` method that writes
   the accumulated state onto the RUNNING row without a state change.
2. **A drive that dies backfills every unreached member to "failed"**, so ``/rerun``'s existing gate
   finds a target. A member that was in flight when the kill landed otherwise has no entry at all —
   not "failed", not "succeeded" — and the run stays a 409 even with checkpointing in place.
3. **A ``max_wall_seconds`` breach becomes re-runnable like any other failure**, overruling
   acceptance criterion 4. The carve-out's premise ("re-running the same DAG would just time out
   again") only held while a re-run re-drove the whole DAG; once finished members are durable a
   re-run drives strictly less work, on a wall clock that starts fresh each drive.
4. **Loop teams are in scope**, since they are the likeliest thing to run past fifty minutes.

RED until the ``checkpoint`` repository method and the drive-side wiring land. The engine's own
modules already exist, so they are imported at module level; nothing here reaches for an unbuilt
package.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import uuid
from typing import Any

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.team_run_service import TeamRunError, TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class FakeTeamRunRepo:
    """In-memory mirror of TeamRunRepository, including the #819 ``checkpoint`` write.

    ``checkpoint`` is deliberately NOT ``transition`` with ``RUNNING→RUNNING``: a checkpoint is a
    durability write on a live row, not a state-machine move, and modelling it as a transition
    would let a checkpoint silently resurrect a settled run. It records its history so a test can
    assert WHEN a write happened, not just what the row ended up holding.
    """

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EngineTeamRun] = {}
        self.checkpoints: list[dict[str, Any]] = []
        self.checkpoint_raises: Exception | None = None

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

    async def checkpoint(
        self, team_run_id: uuid.UUID, organisation_id: uuid.UUID, **fields: Any
    ) -> bool:
        if self.checkpoint_raises is not None:
            raise self.checkpoint_raises
        row = self.rows.get(team_run_id)
        if row is None or row.organisation_id != organisation_id or row.state != "RUNNING":
            return False  # only a live drive checkpoints; a settled row is never rewritten
        for key, value in fields.items():
            setattr(row, key, value)
        self.checkpoints.append({"state": row.state, **{k: _copy(v) for k, v in fields.items()}})
        return True


class FakeMaintenance:
    """The ADR-030 §3 cross-org reader the reaper enumerates through."""

    def __init__(self, stale: list[EngineTeamRun] | None = None) -> None:
        self.stale = stale or []

    async def list_stale_team_runs(self, older_than: _dt.datetime) -> list[EngineTeamRun]:
        return list(self.stale)


def _copy(value: Any) -> Any:
    return dict(value) if isinstance(value, dict) else value


class ScriptedHarness:
    """Dispatches each member through a per-role script: a value to return, a delay, or a raise."""

    def __init__(self, *, delays: dict[str, float] | None = None) -> None:
        self.roles: list[str] = []
        self._delays = delays or {}

    def _role_of(self, input_text: str, manifest_ref: str | None) -> str:
        if manifest_ref:
            return manifest_ref.split("/")[-1].split("@")[0]
        return input_text.replace("do ", "").strip()[:32]

    async def execute(self, **kw: Any) -> dict[str, Any]:
        role = self._role_of(kw.get("input_text", ""), kw.get("manifest_ref"))
        self.roles.append(role)
        delay = self._delays.get(role)
        if delay:
            await asyncio.sleep(delay)
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": f"{role}-out",
            "total_tokens": 100,
        }


def _svc(repo: FakeTeamRunRepo, harness: Any) -> tuple[TeamRunService, list[uuid.UUID]]:
    enqueued: list[uuid.UUID] = []
    return (
        TeamRunService(
            team_runs=repo, harness=harness, enqueue=lambda rid, _o, _u: enqueued.append(rid)
        ),
        enqueued,
    )


def _agent(role: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
    }


def _team(members: list[dict[str, Any]], *, max_wall_seconds: int | None = None) -> dict[str, Any]:
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
    if max_wall_seconds is not None:
        team["orchestration"] = {"termination": {"max_wall_seconds": max_wall_seconds}}
    return team


async def _run(svc: TeamRunService, principal: Principal, **kw: Any) -> EngineTeamRun:
    row = await svc.create(principal, **kw)
    return await svc.drive(row.id, principal)


# ── criterion 1: a finished member is durable BEFORE the run settles ─────────────────────────────


async def test_a_finished_member_is_readable_while_the_run_is_still_running() -> None:
    # The whole feature in one assertion. While 'b' is executing, the row must ALREADY carry 'a'
    # with state RUNNING. Today the row holds nothing until the settle write, which is why a kill
    # at minute 50 loses forty minutes of finished work.
    repo = FakeTeamRunRepo()
    seen_by_b: dict[str, Any] = {}

    class _Peeking(ScriptedHarness):
        async def execute(self, **kw: Any) -> dict[str, Any]:
            role = self._role_of(kw.get("input_text", ""), kw.get("manifest_ref"))
            if role == "b":  # read the durable row from inside the still-running drive
                row = next(iter(repo.rows.values()))
                seen_by_b["state"] = row.state
                seen_by_b["results"] = dict(row.results or {})
                seen_by_b["member_status"] = dict(row.member_status or {})
            return await super().execute(**kw)

    svc, _ = _svc(repo, _Peeking())
    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )

    assert row.state == "SUCCEEDED"
    assert seen_by_b["state"] == "RUNNING"  # the checkpoint did not settle the run
    assert seen_by_b["member_status"] == {"a": "succeeded"}
    assert seen_by_b["results"]["a"]["output"] == "a-out"  # 'a' was readable mid-drive


async def test_the_checkpoint_accumulates_rather_than_replacing() -> None:
    # Each checkpoint carries the whole accumulated state, so the row is always a complete snapshot
    # of what has finished — never a delta the reader has to reassemble.
    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, ScriptedHarness())
    await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"]), _agent("c", ["b"])]),
        sub_harnesses={},
        gate_decisions={},
    )

    written = [c["member_status"] for c in repo.checkpoints if "member_status" in c]
    assert written, "the drive wrote no checkpoint at all"
    assert written[0] == {"a": "succeeded"}
    assert written[-1] == {"a": "succeeded", "b": "succeeded", "c": "succeeded"}
    assert all(c["state"] == "RUNNING" for c in repo.checkpoints)  # never a state change


async def test_a_checkpoint_write_failure_does_not_fail_the_run() -> None:
    # Durability is best-effort, matching the existing `_accrue_schedule_cost` posture: losing a
    # checkpoint costs recovery granularity, letting it kill the drive costs the whole DAG.
    repo = FakeTeamRunRepo()
    repo.checkpoint_raises = RuntimeError("connection reset by peer")
    svc, _ = _svc(repo, ScriptedHarness())

    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )
    assert row.state == "SUCCEEDED"
    assert row.member_status == {"a": "succeeded", "b": "succeeded"}


# ── criteria 2, 3 + decisions 2, 3: a killed drive is recoverable ────────────────────────────────


async def test_a_wall_clock_breach_keeps_the_finished_members_output() -> None:
    # A real orchestrator-level raise, no mocking: 'a' settles, then the team's own deadline expires
    # while 'b' is in flight and run_team raises OHMError into the drive's blanket handler. That
    # handler must settle FAILED OVER the checkpoint, never blank it.
    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, ScriptedHarness(delays={"b": 2.0}))

    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])], max_wall_seconds=1),
        sub_harnesses={},
        gate_decisions={},
    )

    assert row.state == "FAILED"
    assert "max_wall_seconds" in (row.error_message or "")
    assert row.results["a"]["output"] == "a-out"  # the finished member's OUTPUT survived
    assert row.member_status.get("a") == "succeeded"


async def test_a_killed_drive_marks_the_in_flight_members_failed() -> None:
    # Decision 2. 'b' was mid-dispatch when the deadline fired, so it has no status of its own.
    # Without the backfill it stays absent, /rerun finds nothing to re-run, and the recovery the
    # checkpoint just made possible is unreachable.
    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, ScriptedHarness(delays={"b": 2.0}))

    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])], max_wall_seconds=1),
        sub_harnesses={},
        gate_decisions={},
    )

    assert row.member_status == {"a": "succeeded", "b": "failed"}
    assert set(row.member_status) == {"a", "b"}  # every declared member accounted for


async def test_a_killed_drive_marks_members_that_were_never_reached_failed() -> None:
    # The same backfill, for a member in a stage the drive never got to. 'c' was never dispatched
    # and never blocked by a failure — it simply ran out of time, and it is still a re-run target.
    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, ScriptedHarness(delays={"b": 2.0}))

    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"]), _agent("c", ["b"])], max_wall_seconds=1),
        sub_harnesses={},
        gate_decisions={},
    )

    assert row.member_status == {"a": "succeeded", "b": "failed", "c": "failed"}


async def test_a_wall_clock_breach_is_rerunnable_and_resumes_only_the_unfinished() -> None:
    # Decision 3, the criterion-4 override, end to end. The re-run must dispatch ONLY the members
    # that never finished; 'a' is reused from the checkpoint. That is the whole basis of the
    # ruling — a re-run is strictly less work than the drive that breached, so it can now fit.
    repo = FakeTeamRunRepo()
    harness = ScriptedHarness(delays={"b": 2.0})
    svc, _ = _svc(repo, harness)

    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])], max_wall_seconds=1),
        sub_harnesses={},
        gate_decisions={},
    )
    assert row.state == "FAILED" and harness.roles == ["a", "b"]

    requeued = await svc.rerun(row.id, _principal())  # no longer a 409 nothing_to_rerun
    assert requeued.state == "QUEUED"

    harness._delays = {}  # the slow member is quick this time (a transient that has cleared)
    final = await svc.drive(row.id, _principal())

    assert final.state == "SUCCEEDED"
    assert final.member_status == {"a": "succeeded", "b": "succeeded"}
    assert harness.roles == ["a", "b", "b"]  # 'a' was NOT re-dispatched; only 'b' re-ran


def _kill_after_one_checkpoint(monkeypatch: Any, exc: BaseException) -> dict[str, Any]:
    """Stand in for the worker being killed mid-drive: the orchestrator checkpoints one finished
    member, then ``exc`` is raised into the running task.

    The orchestrator is replaced rather than driven to a real timeout because the two kills this
    issue is about arrive from OUTSIDE the DAG — a Celery signal, not a member failing — and there
    is no honest way to make a member's own error escape ``run_team``'s per-member handler. What is
    under test here is the DRIVE's handling of a mid-run death; the orchestrator's own checkpoint
    emission is proven for real in ``packages/ohm/tests/test_orchestrate_checkpoint.py``.

    Returns a dict the test can inspect afterwards. ``wired`` records whether the drive passed a
    checkpoint hook down at all — asserted OUTSIDE the stub, because an assertion raised inside it
    would be swallowed by the drive's own blanket handler and reported as an unrelated failure.
    """
    from oraclous_execution_engine_service.services import team_run_service as trs

    seen: dict[str, Any] = {"wired": False}

    async def _killed(*args: Any, **kw: Any) -> Any:
        hook = kw.get("on_checkpoint")
        seen["wired"] = hook is not None
        if hook is not None:
            await hook({"a": {"output": "a-out", "status": "SUCCEEDED"}}, {"a": "succeeded"})
        raise exc

    monkeypatch.setattr(trs, "run_team_hybrid", _killed)
    return seen


async def test_a_soft_time_limit_kill_leaves_a_rerunnable_run(monkeypatch: Any) -> None:
    # The issue's headline case. Celery raises SoftTimeLimitExceeded into the running task at 50
    # minutes; when it lands in the orchestrator rather than inside a member's dispatch it reaches
    # the drive's blanket handler. Per decision 3 the handler needs NO special case for it — the
    # checkpoint already holds the finished members and the backfill supplies the rest.
    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, ScriptedHarness())
    seen = _kill_after_one_checkpoint(monkeypatch, SoftTimeLimitExceeded())

    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )

    assert seen["wired"], "the drive did not wire a checkpoint hook into the orchestrator"
    assert row.state == "FAILED"
    assert row.results["a"]["output"] == "a-out"  # the finished member survived the kill
    assert row.member_status == {"a": "succeeded", "b": "failed"}

    requeued = await svc.rerun(row.id, _principal())  # criterion 5: it is re-runnable
    assert requeued.state == "QUEUED"


async def test_cost_tokens_are_not_double_counted_across_a_resume() -> None:
    # The checkpoint now writes cost_tokens mid-drive, and the settle write computes
    # `prior_cost + sum(cost_deltas)` where prior_cost is read off the row. A checkpoint that
    # writes a DELTA rather than the running total would be counted twice on the resume.
    repo = FakeTeamRunRepo()
    harness = ScriptedHarness(delays={"b": 2.0})
    svc, _ = _svc(repo, harness)

    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])], max_wall_seconds=1),
        sub_harnesses={},
        gate_decisions={},
    )
    assert row.cost_tokens == 100  # 'a' alone (100/member); 'b' never returned a cost

    harness._delays = {}
    await svc.rerun(row.id, _principal())
    final = await svc.drive(row.id, _principal())

    # 'a' is counted ONCE (reused, never re-dispatched) plus 'b' on the re-run = 200, not 300.
    assert final.cost_tokens == 200


# ── the 60-minute SIGKILL: no in-process handler runs, the reaper settles the row ────────────────


async def test_a_reaped_run_keeps_its_checkpoints_and_is_rerunnable() -> None:
    # At 60 minutes the child process is SIGKILLed, so NO except clause runs. The redelivered
    # message no-ops on the QUEUED→RUNNING CAS and the row sits RUNNING until the lease sweep
    # fails it. The reaper must therefore apply decision 2's backfill too — otherwise the hardest
    # kill of the two is the one that stays unrecoverable.
    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, ScriptedHarness())
    manifest = _team([_agent("a"), _agent("b", ["a"])])
    killed = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=manifest,
        sub_harnesses={},
        gate_decisions={},
        state="RUNNING",  # the SIGKILLed drive left it here
        results={"a": {"output": "a-out", "status": "SUCCEEDED"}},  # 'a' already checkpointed
        member_status={"a": "succeeded"},
        paused_at=[],
    )
    repo.rows[killed.id] = killed

    reaped = await svc.reap_stale(FakeMaintenance([killed]), older_than=_dt.datetime.now(_dt.UTC))

    assert reaped == 1 and killed.state == "FAILED"
    assert killed.results["a"]["output"] == "a-out"  # the reaper did not blank the checkpoint
    assert killed.member_status == {"a": "succeeded", "b": "failed"}  # 'b' backfilled

    requeued = await svc.rerun(killed.id, _principal())
    assert requeued.state == "QUEUED"


async def test_a_run_reaped_before_any_member_finished_is_still_a_409() -> None:
    # The backfill must not manufacture a re-run target out of nothing. A run killed before ANY
    # member settled has no partial work to keep, so a fresh POST is the right recovery and
    # /rerun stays a 409 — the ADR-042 nothing_to_rerun path is narrowed, not deleted.
    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, ScriptedHarness())
    killed = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=_team([_agent("a")]),
        sub_harnesses={},
        gate_decisions={},
        state="RUNNING",
        results={},
        member_status={},  # nothing was ever checkpointed
        paused_at=[],
    )
    repo.rows[killed.id] = killed

    await svc.reap_stale(FakeMaintenance([killed]), older_than=_dt.datetime.now(_dt.UTC))
    assert killed.state == "FAILED"

    with pytest.raises(TeamRunError) as ei:
        await svc.rerun(killed.id, _principal())
    assert ei.value.status_code == 409 and ei.value.error_type == "nothing_to_rerun"


# ── decision 4: loop teams get the same durability ───────────────────────────────────────────────


async def test_a_loop_team_drive_wires_the_checkpoint_hook(monkeypatch: Any) -> None:
    # Decision 4. A loop team is a skeleton with loop nodes in it, and the expensive part of a long
    # research run is usually the acyclic stages around the loop — today a kill anywhere loses all
    # of them. The drive must therefore hand the hook down for a LOOP manifest too, not only the
    # acyclic path; the round-boundary emission itself is proven in the ohm loop-seam test.
    #
    # NB a loop killed mid-drive still RESTARTS on re-run (round 0, fresh wall clock). That is
    # run_team_hybrid's existing rule — only a PAUSED loop resumes its round, a spent attempt
    # restarts — and #819 does not change it. What the checkpoint adds is that the skeleton
    # members around the loop survive and are not re-dispatched.
    repo = FakeTeamRunRepo()
    svc, _ = _svc(repo, ScriptedHarness())
    manifest = _team([_agent("a"), _agent("b", ["a"])])
    manifest["orchestration"] = {"loops": [{"members": ["b"], "routing": {"b": "keep writing"}}]}
    seen = _kill_after_one_checkpoint(monkeypatch, SoftTimeLimitExceeded())

    row = await _run(svc, _principal(), manifest=manifest, sub_harnesses={}, gate_decisions={})

    assert seen["wired"], "a LOOP team's drive did not wire the checkpoint hook"
    assert row.state == "FAILED"
    assert row.results["a"]["output"] == "a-out"  # the skeleton member is durable
    assert row.member_status == {"a": "succeeded", "b": "failed"}

    requeued = await svc.rerun(row.id, _principal())
    assert requeued.state == "QUEUED"
