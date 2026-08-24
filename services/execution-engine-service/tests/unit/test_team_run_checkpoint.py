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
from collections.abc import Callable
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
        # a predicate over the write's fields -> seconds to sleep BEFORE the write lands, so a test
        # can force one specific checkpoint call (e.g. a dispatch-time write) to take the long road
        # to the row, the way #832 / review finding 1 (PR #859) forced the race.
        self.checkpoint_delay: Callable[[dict[str, Any]], float] | None = None

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
        if self.checkpoint_delay is not None:
            delay = self.checkpoint_delay(fields)
            if delay:
                await asyncio.sleep(delay)
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
    # #828: 'b' is dispatched by the time this peek fires, so it now reads "running" rather than
    # being absent — the terminal-only guarantee this test originally pinned is deliberately broken.
    assert seen_by_b["member_status"] == {"a": "succeeded", "b": "running"}
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
    # #828: the FIRST checkpoint is now the dispatch-time "running" write, not the settle one.
    assert written[0] == {"a": "running"}
    assert written[-1] == {"a": "succeeded", "b": "succeeded", "c": "succeeded"}
    assert all(c["state"] == "RUNNING" for c in repo.checkpoints)  # never a state change


async def test_a_dispatch_time_write_never_regresses_a_later_settle() -> None:
    # Review finding 1 (PR #859) on #828+#832: a member's dispatch-time "running" write and a
    # sibling's settle write both mutate this row from a wide stage; without both the local
    # bookkeeping AND the persisted write serialized under one lock, an older dispatch snapshot can
    # land AFTER a newer settle snapshot and regress an already-succeeded member back to "running"
    # on the durable row — the #832 class of bug, this time on the engine's own write path (which
    # packages/ohm's D3 checkpoint lock does not cover).
    repo = FakeTeamRunRepo()

    def _slow_dispatch_write(fields: dict[str, Any]) -> float:
        # only 'b's dispatch-time write carries no "results" key (only the settle checkpoint does)
        # and marks 'b' running — force it to take the long road to the row.
        status = fields.get("member_status") or {}
        return 0.05 if "results" not in fields and status.get("b") == "running" else 0.0

    repo.checkpoint_delay = _slow_dispatch_write
    # 'a' needs ONE real yield point (its harness call) so the event loop can interleave 'b's
    # dispatch onto the same tick — with no delay anywhere, 'a' runs start-to-settle in one
    # uninterrupted stretch and 'b' never gets scheduled until 'a' is already done.
    svc, _ = _svc(repo, ScriptedHarness(delays={"a": 0.01}))

    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b")]),  # one wide stage, both independent
        sub_harnesses={},
        gate_decisions={},
    )

    assert row.state == "SUCCEEDED"
    assert row.member_status == {"a": "succeeded", "b": "succeeded"}

    seen_a_succeeded = False
    for cp in repo.checkpoints:
        status = cp.get("member_status")
        if status is None:
            continue
        if seen_a_succeeded:
            assert status.get("a") == "succeeded", (
                f"'a' regressed from succeeded back to {status.get('a')!r}: {status}"
            )
        if status.get("a") == "succeeded":
            seen_a_succeeded = True


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


async def test_a_reaped_run_never_strands_a_member_on_running() -> None:
    # #828 gave member_status a NON-terminal value, and the reaper reads the DURABLE row (unlike
    # the in-process path, which reads the orchestrator's settled-only snapshot). A member that was
    # in flight when the SIGKILL landed therefore arrives here as "running", not as a missing key,
    # and setdefault leaves it alone. With every other member succeeded, /rerun's failed-or-blocked
    # gate then finds nothing: 409 forever, on a run with real unfinished work. The reaper write is
    # the LAST write the row gets — the drive that would have revised "running" is dead.
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
        state="RUNNING",
        results={"a": {"output": "a-out", "status": "SUCCEEDED"}},
        member_status={"a": "succeeded", "b": "running"},  # 'b' was mid-dispatch when it died
        paused_at=[],
    )
    repo.rows[killed.id] = killed

    reaped = await svc.reap_stale(FakeMaintenance([killed]), older_than=_dt.datetime.now(_dt.UTC))

    assert reaped == 1 and killed.state == "FAILED"
    assert killed.member_status == {"a": "succeeded", "b": "failed"}
    assert killed.results["a"]["output"] == "a-out"  # the finished member's output is untouched

    requeued = await svc.rerun(killed.id, _principal())
    assert requeued.state == "QUEUED"


async def test_a_run_reaped_with_only_a_running_member_is_still_a_409() -> None:
    # The guard's half of the same bug. "at least one settled member" must mean SETTLED — a lone
    # "running" entry is a member that was dispatched and delivered nothing, which is exactly the
    # no-partial-work case ADR-042 answers 409 on. Counting it as settled would let #828's
    # dispatch-time write silently narrow that 409 on the commonest failure path of all: a run that
    # dies before its first member delivers.
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
        member_status={"a": "running"},  # dispatched, then killed — nothing delivered
        paused_at=[],
    )
    repo.rows[killed.id] = killed

    await svc.reap_stale(FakeMaintenance([killed]), older_than=_dt.datetime.now(_dt.UTC))
    assert killed.state == "FAILED"

    with pytest.raises(TeamRunError) as ei:
        await svc.rerun(killed.id, _principal())
    assert ei.value.status_code == 409 and ei.value.error_type == "nothing_to_rerun"


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


async def test_a_drive_that_dies_before_any_member_settles_is_still_a_409() -> None:
    # The in-process twin of the test above, and they must agree. The 50-minute soft limit and the
    # 60-minute SIGKILL are the same event on two clocks; if the handler backfills unconditionally
    # while the reaper backfills only over real work, the same run answers /rerun differently
    # depending on which one got to it.
    #
    # The answer pinned here is the reaper's: backfill only when at least one member was
    # checkpointed. A drive that dies before member 1 — an unreachable harness, an immediate kill —
    # has no partial work to recover, so a fresh POST is the right recovery and today's
    # nothing_to_rerun behaviour on the commonest failure path is preserved.
    repo = FakeTeamRunRepo()
    svc, _ = _svc(
        repo, ScriptedHarness(delays={"a": 2.0})
    )  # 'a' is still in flight at the deadline

    row = await _run(
        svc,
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])], max_wall_seconds=1),
        sub_harnesses={},
        gate_decisions={},
    )

    assert row.state == "FAILED"
    # #828: 'a' was dispatched (so it reads "running", not absent), but never SETTLED — the
    # backfill guard keys off settled work (``settled_status``, terminal statuses only), which stays
    # empty here, so nothing_to_rerun below is unchanged.
    assert row.member_status == {"a": "running"}
    assert not (row.results or {})

    with pytest.raises(TeamRunError) as ei:
        await svc.rerun(row.id, _principal())
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


# ── decision 4, mock-free: the real hybrid driver must not leak its internal node ─────────────────
#
# The test above monkeypatches run_team_hybrid, so it pins only that the drive passes the kwarg
# down. These drive the REAL hybrid, because what happens downstream of that kwarg is where the
# feature can reintroduce the very bug #819 exists to fix.
#
# run_team_hybrid runs the acyclic skeleton over a CONDENSED member list in which each loop is one
# synthetic `__loop__<i>` node. The settle path pops that node because it is internal — but a
# checkpoint fires before the pop. A completed drive is therefore clean and a KILLED one is not,
# which is the worst possible split: the leak is invisible until the exact moment it matters.


class _CountingHarness:
    """Records every role the harness really dispatched, so a skipped loop is visible."""

    def __init__(self) -> None:
        self.roles: list[str] = []

    async def execute(self, *, manifest_ref: str | None = None, **kw: Any) -> dict[str, Any]:
        role = (manifest_ref or "?/?").split("/")[-1].split("@")[0]
        self.roles.append(role)
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": f"{role}-out",
            "total_tokens": 100,
        }


def _ohm_loop_team() -> Any:
    """a → loop(b) → c, as OHM objects: a skeleton member on each side of a genuine loop."""
    from oraclous_ohm.manifest import (
        OHMLoop,
        OHMManifest,
        OHMMember,
        OHMMetadata,
        OHMOrchestration,
        OHMRuntime,
        OHMTermination,
    )

    def _member(role: str, deps: list[str] | None = None) -> OHMMember:
        return OHMMember(
            role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=deps or []
        )

    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=[_member("a"), _member("b", ["a"]), _member("c", ["b"])],
        orchestration=OHMOrchestration(
            loops=[OHMLoop(members=["b"], routing={"b": "keep writing"})],
            termination=OHMTermination(max_rounds=8),
        ),
        runtime=OHMRuntime(entrypoint="a"),
    )


def _loop_conductor() -> tuple[Any, Any]:
    """A coordinator that dispatches whatever the loop has not produced, and a coded done-check
    that converges once every loop member has produced. No model, no evaluator — the loop
    machinery is real, only its two injected decisions are deterministic."""

    async def coordinate(loop: Any, results: dict[str, Any], rounds_left: int) -> list[str]:
        return [r for r in loop.members if results.get(r) is None]

    def done_check_for(loop: Any, diag: dict[str, Any] | None = None) -> Any:
        async def done(results: dict[str, Any]) -> bool:
            return all(results.get(r) is not None for r in loop.members)

        return done

    return coordinate, done_check_for


async def _hybrid(manifest: Any, harness: Any, **kw: Any) -> Any:
    from oraclous_execution_engine_service.services.team_run import run_team_hybrid

    return await run_team_hybrid(manifest, harness, **kw)


async def test_a_loop_drive_never_checkpoints_the_internal_condensed_node() -> None:
    # Every checkpoint must contain only roles the user declared. `__loop__0` is an implementation
    # detail of the condensation; on the row it is both a leak of internal state to the API and a
    # poisoned resume seed (see the test below).
    harness = _CountingHarness()
    coordinate, done_check_for = _loop_conductor()
    snapshots: list[tuple[dict[str, Any], dict[str, str]]] = []

    async def record(results: dict[str, Any], member_status: dict[str, str]) -> None:
        snapshots.append((dict(results), dict(member_status)))

    res = await _hybrid(
        _ohm_loop_team(),
        harness,
        coordinate=coordinate,
        done_check_for=done_check_for,
        on_checkpoint=record,
    )

    assert res.status == "completed"
    assert snapshots, "the loop drive checkpointed nothing"
    declared = {"a", "b", "c"}
    for results, member_status in snapshots:
        assert set(member_status) <= declared, f"internal role leaked: {set(member_status)}"
        assert set(results) <= declared, f"internal role leaked: {set(results)}"
    # decision 4 has substance only if the loop's own member reaches the row, not just the skeleton
    assert any("b" in status for _, status in snapshots)
    # ...and the snapshot stays COMPLETE — a loop-side write must not drop the skeleton member
    assert all("a" in status for _, status in snapshots if "b" in status)


async def test_a_killed_loop_run_resumes_with_its_loop_member_intact() -> None:
    # The consequence, end to end. Take the checkpoint the row would be holding if the worker were
    # killed after the loop converged but before 'c' finished, rebuild `completed` exactly as
    # `_completed_for_resume` does, and re-drive.
    #
    # With the internal node leaking, `completed` carries `__loop__0`, run_team marks the condensed
    # node succeeded without ever entering the conductor, and 'b' vanishes from the results while
    # the run still reports completed. That is #819's own failure, reappearing inside the fix.
    coordinate, done_check_for = _loop_conductor()
    first_harness = _CountingHarness()
    snapshots: list[tuple[dict[str, Any], dict[str, str]]] = []

    async def record(results: dict[str, Any], member_status: dict[str, str]) -> None:
        snapshots.append((dict(results), dict(member_status)))

    manifest = _ohm_loop_team()
    await _hybrid(
        manifest,
        first_harness,
        coordinate=coordinate,
        done_check_for=done_check_for,
        on_checkpoint=record,
    )

    # the last state written before 'c' settled — what a kill at that instant leaves on the row
    mid = next((s for s in reversed(snapshots) if "c" not in s[1]), None)
    assert mid is not None, "no checkpoint landed before the final member"
    results, member_status = mid
    completed = {
        role: results[role]
        for role, status in member_status.items()
        if status in ("succeeded", "partial") and role in results
    }

    second_harness = _CountingHarness()
    resumed = await _hybrid(
        manifest,
        second_harness,
        coordinate=coordinate,
        done_check_for=done_check_for,
        completed=completed,
    )

    assert resumed.status == "completed"
    assert set(resumed.member_status) == {"a", "b", "c"}  # every declared member, no internal node
    assert resumed.results.get("b") is not None  # the loop's output survived the kill
    assert "__loop__0" not in resumed.results
    assert "a" not in second_harness.roles  # the durable skeleton member was not re-dispatched
