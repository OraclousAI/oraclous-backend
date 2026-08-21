"""#828 — the three signals a run view needs, none of which a team run emits today.

#819 retired two of this issue's four items: ``results`` is checkpointed per settled member, so a
finished member is readable mid-run, and ``progress`` therefore advances instead of jumping 0→100.
What is still missing is everything about a member that has NOT settled:

1. **A ``running`` value, written at dispatch.** ``member_status`` is written only when a member
   reaches a terminal status, so "who is working right now" is unanswerable. On a fan-out stage of
   four concurrent members, inferring it as declared-minus-settled cannot tell which of the four
   are actually in flight.
2. **Timestamps.** No member carries ``started_at`` / ``ended_at``, so no client can render a
   duration or order anything by time. ``status.last_run_at`` reports ``created_at`` — the moment
   the run was QUEUED, not the moment it started driving.
4. **Role labels on the tree.** ``child_execution_ids`` is a flat list, so a child execution cannot
   be mapped back to the member that produced it. The role IS in scope at the emit site
   (``member.role``, two lines above the ``on_child`` call) and is discarded.

The owner's ruling (2026-08-21) settles the three open decisions:

* **D1. The ``running`` value goes into ``member_status``**, deliberately breaking that field's
  documented terminal-only guarantee, rather than living in a separate non-breaking field. The one
  console screen that depends on the guarantee is fixed in ``oraclous-frontend#231``; the shape
  change is recorded for ``solution-architect`` in #857.
* **D2. Step timestamps are in scope** (asserted in the harness service's own suite, not here).
* **D3. #832 is bundled**, because writing at dispatch roughly doubles the checkpoint write rate on
  the exact path that issue says is already racing.

The two properties that keep ``running`` from becoming a lie are pinned here rather than assumed:
it must not count toward ``progress`` (fabricated completion is what this issue and
``oraclous-frontend#211`` both forbid), and it must not seed a resume as a completed member.

RED until the ``on_dispatch`` hook, the ``member_timings`` / ``child_execution_roles`` columns and
the ``/status`` + ``/tree`` surfacing land. The engine's own modules already exist, so they are
imported at module level; nothing here reaches for an unbuilt package.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.team_run_service import TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


def _copy(value: Any) -> Any:
    return dict(value) if isinstance(value, dict) else value


class FakeTeamRunRepo:
    """In-memory mirror of TeamRunRepository, recording every checkpoint in order.

    The history is what most of this file asserts on: #828 is about WHEN a signal is written, not
    only what the row ends up holding, and a row read after the drive cannot distinguish a value
    written at dispatch from one written at settle.
    """

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, EngineTeamRun] = {}
        self.checkpoints: list[dict[str, Any]] = []

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
        row = self.rows.get(team_run_id)
        if row is None or row.organisation_id != organisation_id or row.state != "RUNNING":
            return False
        for key, value in fields.items():
            setattr(row, key, value)
        self.checkpoints.append({"state": row.state, **{k: _copy(v) for k, v in fields.items()}})
        return True


class ScriptedHarness:
    """Dispatches each member through a per-role script: a value to return, or a delay first."""

    def __init__(self, *, delays: dict[str, float] | None = None) -> None:
        self.roles: list[str] = []
        self.execution_ids: dict[str, str] = {}
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
        execution_id = str(uuid.uuid4())
        self.execution_ids[role] = execution_id
        return {
            "id": execution_id,
            "status": "SUCCEEDED",
            "output": f"{role}-out",
            "total_tokens": 100,
        }


def _svc(repo: FakeTeamRunRepo, harness: Any) -> TeamRunService:
    return TeamRunService(team_runs=repo, harness=harness, enqueue=lambda _r, _o, _u: None)


def _agent(role: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
    }


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


async def _run(svc: TeamRunService, principal: Principal, **kw: Any) -> EngineTeamRun:
    row = await svc.create(principal, **kw)
    return await svc.drive(row.id, principal)


# ── item 1 + criterion 1: a member is "running" while it runs ────────────────────────────────────


async def test_a_dispatched_member_reads_running_before_it_settles() -> None:
    # The headline. While 'b' is executing, the row must ALREADY say 'b' is running — not absent,
    # and not inferable only by subtracting the settled roles from the declared ones. Today
    # member_status holds {"a": "succeeded"} and says nothing at all about 'b'.
    repo = FakeTeamRunRepo()
    seen_by_b: dict[str, Any] = {}

    class _Peeking(ScriptedHarness):
        async def execute(self, **kw: Any) -> dict[str, Any]:
            role = self._role_of(kw.get("input_text", ""), kw.get("manifest_ref"))
            if role == "b":  # read the live row from inside the still-running drive
                row = next(iter(repo.rows.values()))
                seen_by_b["state"] = row.state
                seen_by_b["member_status"] = dict(row.member_status or {})
            return await super().execute(**kw)

    row = await _run(
        _svc(repo, _Peeking()),
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )

    assert seen_by_b["state"] == "RUNNING"
    assert seen_by_b["member_status"] == {"a": "succeeded", "b": "running"}
    assert row.member_status == {"a": "succeeded", "b": "succeeded"}  # overwritten at settle


async def test_every_member_of_a_wide_stage_reads_running_at_once() -> None:
    # Inference cannot do this. With three independent members dispatched together, "declared minus
    # settled" is the same set whether all three are in flight or none of them are; only a written
    # value distinguishes them. This is the case a fan-out run view actually renders.
    repo = FakeTeamRunRepo()
    barrier = asyncio.Event()
    seen: dict[str, Any] = {}

    class _Concurrent(ScriptedHarness):
        def __init__(self) -> None:
            super().__init__()
            self.arrived = 0

        async def execute(self, **kw: Any) -> dict[str, Any]:
            role = self._role_of(kw.get("input_text", ""), kw.get("manifest_ref"))
            self.arrived += 1
            if self.arrived == 3:  # the last of the three to be dispatched reads the row
                row = next(iter(repo.rows.values()))
                seen["member_status"] = dict(row.member_status or {})
                barrier.set()
            else:
                await barrier.wait()
            self.roles.append(role)
            return {
                "id": str(uuid.uuid4()),
                "status": "SUCCEEDED",
                "output": "x",
                "total_tokens": 1,
            }

    await _run(
        _svc(repo, _Concurrent()),
        _principal(),
        manifest=_team([_agent("a"), _agent("b"), _agent("c")]),
        sub_harnesses={},
        gate_decisions={},
    )

    assert seen["member_status"] == {"a": "running", "b": "running", "c": "running"}


async def test_running_is_written_at_dispatch_not_at_settle() -> None:
    # Ordering, asserted on the checkpoint history rather than the final row. A "running" value
    # written alongside the terminal status would satisfy every end-state assertion above and still
    # be useless: the point is that it lands BEFORE the member's own result does.
    repo = FakeTeamRunRepo()
    await _run(
        _svc(repo, ScriptedHarness()),
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )

    written = [c["member_status"] for c in repo.checkpoints if "member_status" in c]
    assert written, "the drive wrote no checkpoint at all"
    first_running = next(i for i, m in enumerate(written) if m.get("a") == "running")
    first_settled = next(i for i, m in enumerate(written) if m.get("a") == "succeeded")
    assert first_running < first_settled
    assert all(c["state"] == "RUNNING" for c in repo.checkpoints)  # never a state change


# ── the two properties that stop "running" from becoming a lie ───────────────────────────────────


async def test_a_running_member_does_not_count_toward_progress() -> None:
    # Fabricated completion is the one thing this issue and oraclous-frontend#211 both forbid, in
    # the same sentence. _member_completion_progress counts succeeded|skipped|partial today, so
    # this passes for free — which is exactly why it is pinned. A later edit that adds "running" to
    # that set turns an honest run view into a dishonest one with no other test objecting.
    from oraclous_execution_engine_service.services.team_run_service import (
        _member_completion_progress,
    )

    row = EngineTeamRun(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        manifest=_team([_agent("a"), _agent("b"), _agent("c"), _agent("d")]),
        sub_harnesses={},
        gate_decisions={},
        state="RUNNING",
        results={"a": {"output": "a-out"}},
        paused_at=[],
        member_status={"a": "succeeded", "b": "running", "c": "running"},
    )

    assert _member_completion_progress(row) == 25  # 1 of 4 delivered, not 3 of 4


async def test_a_running_member_is_not_resumed_as_completed() -> None:
    # A run killed mid-flight must re-dispatch the member that was running, not skip it. The guard
    # already exists — _completed_for_resume requires a role in BOTH member_status and results —
    # and this pins it against the new value rather than trusting the coincidence.
    repo = FakeTeamRunRepo()
    svc = _svc(repo, ScriptedHarness())
    row = await svc.create(
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )
    row.member_status = {"a": "succeeded", "b": "running"}
    row.results = {"a": {"output": "a-out"}}

    completed = svc._completed_for_resume(row)

    assert set(completed) == {"a"}, "a member that was merely dispatched is not a completed member"


# ── item 2: timestamps ───────────────────────────────────────────────────────────────────────────


async def test_a_member_carries_a_start_time_from_the_moment_it_is_dispatched() -> None:
    # Written at dispatch, on the same checkpoint as the "running" value — a start time that only
    # appears once the member finishes cannot answer "how long has this been running".
    repo = FakeTeamRunRepo()
    seen_by_b: dict[str, Any] = {}

    class _Peeking(ScriptedHarness):
        async def execute(self, **kw: Any) -> dict[str, Any]:
            role = self._role_of(kw.get("input_text", ""), kw.get("manifest_ref"))
            if role == "b":
                row = next(iter(repo.rows.values()))
                seen_by_b["timings"] = dict(row.member_timings or {})
            return await super().execute(**kw)

    await _run(
        _svc(repo, _Peeking()),
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )

    assert set(seen_by_b["timings"]) == {"a", "b"}
    assert seen_by_b["timings"]["b"]["started_at"] is not None
    assert seen_by_b["timings"]["b"]["ended_at"] is None  # still in flight
    assert seen_by_b["timings"]["a"]["ended_at"] is not None  # already settled


async def test_a_settled_member_carries_an_end_time_at_or_after_its_start() -> None:
    import datetime as _dt

    repo = FakeTeamRunRepo()
    row = await _run(
        _svc(repo, ScriptedHarness(delays={"a": 0.05})),
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )

    timings = row.member_timings or {}
    assert set(timings) == {"a", "b"}
    for role, window in timings.items():
        started = _dt.datetime.fromisoformat(window["started_at"])
        ended = _dt.datetime.fromisoformat(window["ended_at"])
        assert ended >= started, f"{role} ended before it started"
        assert started.tzinfo is not None, f"{role} start time is not timezone-aware"
    # 'a' slept 50ms, so its window is measurably non-zero rather than two identical stamps.
    a = timings["a"]
    span = _dt.datetime.fromisoformat(a["ended_at"]) - _dt.datetime.fromisoformat(a["started_at"])
    assert span.total_seconds() >= 0.04


async def test_last_run_at_reports_the_drive_start_not_the_queue_time() -> None:
    # status.last_run_at maps to created_at today, so a run that sat in the queue for ten minutes
    # reports a start time ten minutes before it started. Distinguishable only when the two differ.
    repo = FakeTeamRunRepo()
    svc = _svc(repo, ScriptedHarness())
    principal = _principal()
    created = await svc.create(
        principal,
        manifest=_team([_agent("a")]),
        sub_harnesses={},
        gate_decisions={},
    )
    await asyncio.sleep(0.05)
    await svc.drive(created.id, principal)

    status = await svc.status(created.id, principal)

    row = repo.rows[created.id]
    assert status.last_run_at is not None
    assert status.last_run_at != row.created_at
    assert status.last_run_at >= row.created_at


# ── item 4: the tree maps a child execution to the member that produced it ───────────────────────


async def test_every_child_execution_carries_the_role_that_produced_it() -> None:
    repo = FakeTeamRunRepo()
    harness = ScriptedHarness()
    row = await _run(
        _svc(repo, harness),
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )

    roles = row.child_execution_roles or {}
    assert set(roles.values()) == {"a", "b"}
    assert roles[harness.execution_ids["a"]] == "a"
    assert roles[harness.execution_ids["b"]] == "b"
    # the flat list stays exactly as it was — this is additive, not a replacement
    assert set(row.child_execution_ids) == set(roles)


async def test_a_failed_member_still_appears_in_the_role_map() -> None:
    # on_child fires BEFORE the fail-closed status check, deliberately, so a failed member is still
    # in the tree. Its role must be too, or per-member drill-down loses exactly the member an
    # operator most wants to open.
    repo = FakeTeamRunRepo()

    class _FailingB(ScriptedHarness):
        async def execute(self, **kw: Any) -> dict[str, Any]:
            role = self._role_of(kw.get("input_text", ""), kw.get("manifest_ref"))
            out = await super().execute(**kw)
            if role == "b":
                out["status"] = "FAILED"
                out["error_message"] = "b blew up"
            return out

    harness = _FailingB()
    row = await _run(
        _svc(repo, harness),
        _principal(),
        manifest=_team([_agent("a"), _agent("b", ["a"])]),
        sub_harnesses={},
        gate_decisions={},
    )

    assert row.state == "FAILED"
    assert (row.child_execution_roles or {}).get(harness.execution_ids["b"]) == "b"
