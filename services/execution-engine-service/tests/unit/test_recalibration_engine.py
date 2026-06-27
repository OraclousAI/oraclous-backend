"""ADR-043 #553 (slice 2/3) PR-2 — the ENGINE half of bounded recalibration: the BYOM directive turn
(``make_recalibration_coordinator`` — tool-less, leak-safe, fail-closed), the closed-set parse, the
coded diagnosis side-channel the done-check writes (``_make_loop_done_check(..., diag=...)``), and
the ``run_team_hybrid`` wiring (recalibrate threads to the seam; the recalibration count persists
for a HITL resume). The model only PICKS a tactic; the coded done-check still rules (no self-grade).

RED until the engine [impl] lands — imported function-locally so the module still collects.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.manifest import (
    OHMLoop,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRuntime,
    OHMTermination,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, kind: str = "agent") -> OHMMember:
    return OHMMember(
        role=role,
        kind=kind,
        manifest_ref=(f"o:x/{role}@1" if kind == "agent" else None),
        human_role=("approver" if kind == "human" else None),
    )


def _team(members: list[OHMMember], loops: list[OHMLoop], **orch: Any) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        orchestration=OHMOrchestration(
            loops=loops, termination=OHMTermination(max_rounds=orch.pop("max_rounds", 8)), **orch
        ),
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _loop(*roles: str) -> OHMLoop:
    return OHMLoop(members=list(roles), routing={r: f"do {r}" for r in roles})


# ── _parse_recalibration_output — closed-set, FAIL-CLOSED ──────────────────────────────────────


def _parse(output: Any, allowed: set[str]):
    from oraclous_execution_engine_service.services.team_run import _parse_recalibration_output

    return _parse_recalibration_output(output, allowed=allowed)


async def test_parse_picks_one_action_and_in_loop_targets() -> None:
    d = _parse("change-strategy writer", {"writer", "critic"})
    assert d.action == "change-strategy"
    assert d.member_targets == ["writer"]


async def test_parse_normalises_action_and_drops_ghost_and_dedups() -> None:
    d = _parse("RE_PLAN writer writer ghost", {"writer", "critic"})
    assert d.action == "re-plan"  # RE_PLAN → re-plan
    assert d.member_targets == ["writer"]  # ghost dropped (not a loop member), de-duped


async def test_parse_fails_closed_to_escalate() -> None:
    # no recognised action token, or empty / non-string → escalate (never a silent no-op retry)
    assert _parse("let me think about this more", {"writer"}).action == "escalate"
    assert _parse("", {"writer"}).action == "escalate"
    assert _parse(None, {"writer"}).action == "escalate"


# ── _render_recalibration_prompt — leak-safe (coded signals, NEVER raw content) ─────────────────


async def test_render_is_leak_safe() -> None:
    from oraclous_execution_engine_service.services.team_run import _render_recalibration_prompt
    from oraclous_ohm.orchestrate import Diagnostic

    diag = Diagnostic(
        stall_kind="signature",
        missing_members=["critic"],
        failed_members={"writer": "SECRET upstream body"},
        artifacts_landed=False,
        evaluator_score=0.4,
        evaluator_floor=0.8,
    )
    prompt = _render_recalibration_prompt(_loop("writer", "critic"), diag, ["writer", "critic"])
    assert "SECRET" not in prompt  # the failure DETAIL never reaches the prompt — only role names
    assert "critic" in prompt and "0.4" in prompt and "0.8" in prompt  # coded signals do
    assert "change-strategy" in prompt and "escalate" in prompt  # the closed action menu


# ── make_recalibration_coordinator — the BYOM directive turn (tool-less, fail-closed) ───────────


class _Harness:
    """A fake harness for the recalibrator turn — returns a canned reply, or raises to simulate an
    unreachable router. Records input_text + capability_ceiling for leak-safety / tool-less asserts.
    """

    def __init__(self, reply: str | None, *, raises: bool = False) -> None:
        self.reply, self.raises = reply, raises
        self.input_text: str | None = None
        self.capability_ceiling: list[str] | None = None

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        capability_ceiling: list[str] | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        from oraclous_execution_engine_service.services.harness_client import HarnessClientError

        self.input_text, self.capability_ceiling = input_text, capability_ceiling
        if self.raises:
            raise HarnessClientError("router unreachable")
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": self.reply}


async def test_recalibrator_picks_a_directive_tool_less() -> None:
    from oraclous_execution_engine_service.services.team_run import make_recalibration_coordinator
    from oraclous_ohm.orchestrate import Diagnostic

    team = _team([_m("writer"), _m("critic")], [_loop("writer", "critic")])
    h = _Harness("change-strategy writer")
    recal = make_recalibration_coordinator(h, team)
    directive = await recal(_loop("writer", "critic"), Diagnostic(stall_kind="signature"))
    assert directive is not None
    assert directive.action == "change-strategy" and directive.member_targets == ["writer"]
    assert (
        h.capability_ceiling == []
    )  # the recalibrator is granted NO capability (ADR-043 invariant)


async def test_recalibrator_fails_closed_on_unreachable_router() -> None:
    from oraclous_execution_engine_service.services.team_run import make_recalibration_coordinator
    from oraclous_ohm.orchestrate import Diagnostic

    team = _team([_m("writer"), _m("critic")], [_loop("writer", "critic")])
    recal = make_recalibration_coordinator(_Harness(None, raises=True), team)
    assert await recal(_loop("writer", "critic"), Diagnostic(stall_kind="signature")) is None


async def test_recalibrator_excludes_a_human_gate_from_retry_targets() -> None:
    # a kind:human gate is never a retry target (the seam re-renders it); only work members offered
    from oraclous_execution_engine_service.services.team_run import make_recalibration_coordinator
    from oraclous_ohm.orchestrate import Diagnostic

    team = _team([_m("writer"), _m("gate", "human")], [_loop("writer", "gate")])
    h = _Harness("re-plan writer")
    recal = make_recalibration_coordinator(h, team)
    await recal(_loop("writer", "gate"), Diagnostic(stall_kind="signature"))
    assert "gate" not in h.input_text  # the gate is not offered as a retry target
    assert "writer" in h.input_text


# ── the coded diagnosis side-channel — the done-check writes WHICH gate failed ──────────────────


class _Artifacts:
    def __init__(self, n: int) -> None:
        self.n = n

    async def list_artifacts(self, graph_id: Any) -> list[dict[str, Any]]:
        return [{"id": str(uuid.uuid4())} for _ in range(self.n)]


class _Evaluate:
    def __init__(self, score: float) -> None:
        self.score = score

    async def evaluate(self, **kw: Any) -> dict[str, Any]:
        return {"score": self.score, "pass": self.score >= 0.8}


async def test_done_check_writes_the_diagnosis_side_channel() -> None:
    from oraclous_execution_engine_service.services.team_run_service import TeamRunService

    loop = OHMLoop(members=["writer", "critic"], routing={})
    team = _team(
        [_m("writer"), _m("critic")],
        [loop],
        success_criteria="an accurate draft",
        max_rounds=8,
    )
    # re-declare with the convergence threshold (the helper sets termination separately)
    team.orchestration.termination.convergence = "evaluator>=0.8"
    svc = TeamRunService(team_runs=object(), evaluate=_Evaluate(0.4), artifacts=_Artifacts(1))
    diag: dict[str, Any] = {}
    done = svc._make_loop_done_check(
        team, uuid.uuid4(), str(uuid.uuid4()), loop, artifacts_baseline=0, diag=diag
    )
    result = await done({"writer": {"out": "d"}, "critic": {"out": "r"}})
    assert result is False  # grade 0.4 < 0.8 → not converged
    assert diag["artifacts_ok"] is True  # 1 landed > baseline 0
    assert diag["evaluator_score"] == 0.4 and diag["evaluator_floor"] == 0.8  # the failing gate


# ── run_team_hybrid wiring — recalibrate threads through; the count persists for resume ──────────


class _RecoverHarness:
    """Member dispatch counter — a member STALLS until it has been (re)dispatched enough times; the
    recalibration is what frees it for the extra dispatch, so without recalibrate the loop fails."""

    def __init__(self) -> None:
        self.n: dict[str, int] = {}

    async def execute(self, *, manifest_ref: str | None = None, **kw: Any) -> dict[str, Any]:
        role = (manifest_ref or "?/?").split("/")[-1].split("@")[0]
        self.n[role] = self.n.get(role, 0) + 1
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": f"{role}-{self.n[role]}"}


async def _hybrid(manifest: OHMManifest, harness: Any, **kw: Any):
    from oraclous_execution_engine_service.services.team_run import run_team_hybrid

    return await run_team_hybrid(manifest, harness, **kw)


def _coordinate_unproduced():
    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return [r for r in loop.members if results.get(r) is None]

    return coordinate


async def test_run_team_hybrid_threads_recalibrate_and_recovers() -> None:
    from oraclous_ohm.orchestrate import RecalDirective

    h = _RecoverHarness()

    def done_for(loop: OHMLoop, diag: dict[str, Any] | None = None):
        async def done(results: dict[str, Any]) -> bool:
            return all(h.n.get(r, 0) >= 2 for r in loop.members)  # needs the post-recal redispatch

        return done

    async def recalibrate(loop: OHMLoop, diag: Any) -> RecalDirective:
        return RecalDirective(action="re-frame-objective", reason="x", member_targets=["w"])

    res = await _hybrid(
        _team([_m("w")], [_loop("w")], max_rounds=10),
        h,
        coordinate=_coordinate_unproduced(),
        done_check_for=done_for,
        recalibrate=recalibrate,
    )
    assert (
        res.status == "completed"
    )  # the recalibration freed the member for the converging redispatch
    assert res.loop_state["0"]["recalibration_count"] >= 1  # persisted for a HITL resume


async def test_run_team_hybrid_without_recalibrate_stalls_to_failed() -> None:
    # the SAME stalling loop with NO recalibrator wired → it fails (no_progress); proves recalibrate
    # is load-bearing for recovery AND that a None recalibrate is byte-compatible (the #552 path)
    h = _RecoverHarness()

    def done_for(loop: OHMLoop, diag: dict[str, Any] | None = None):
        async def done(results: dict[str, Any]) -> bool:
            return all(h.n.get(r, 0) >= 2 for r in loop.members)

        return done

    res = await _hybrid(
        _team([_m("w")], [_loop("w")], max_rounds=10),
        h,
        coordinate=_coordinate_unproduced(),
        done_check_for=done_for,
        recalibrate=None,
    )
    assert res.status == "failed"
    assert res.loop_state["0"]["recalibration_count"] == 0
