"""#585 (ADR-031 §D3) — the engine halts a fan-out at the TEAM-POOLED budget ceiling, fail-closed.

A ``fan_out.over`` resolving to N items today dispatches all N (concurrency-capped only) — the only
aggregate bound is the wall deadline, so a runaway ``over`` (a bad path, an adversarial objective)
can blow arbitrary spend (the per-member cap #576 bounds each member, never the SUM). This pins it:
``run_team`` maintains a running pooled tally (tokens via the engine's ``cost_so_far`` callback +
sub-runs counted at admission) and checks the team ceiling (``OHMBudget.max_tokens_total`` /
``max_sub_runs`` / ``max_usd_total``) BEFORE admitting each fan-out item; on breach it HALTS with a
flagged partial (``status="cost_budget"``, ``partial=True``) — the un-admitted items never run.

RED until the [impl] adds: the ``cost_so_far`` param + the pooled pre-dispatch check + the
sequential-admission fan-out loop + the ``TeamRunResult`` ``cost_budget``/``partial`` shape. The
per-member path (#576) stays byte-for-byte unchanged when no pool ceiling resolves (test 3).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import (
    OHMBudget,
    OHMFanOut,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMRuntime,
)
from oraclous_ohm.orchestrate import run_team

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(
    role: str, *, depends_on: list[str] | None = None, fan_out: OHMFanOut | None = None
) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=depends_on or [],
        fan_out=fan_out,
    )


def _team(members: list[OHMMember], *, budget: OHMBudget | None = None) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        budget=budget,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def test_fan_out_halts_before_dispatching_all_items_on_token_ceiling() -> None:
    # 5 items × 100 tokens each, pooled ceiling 250 (max_parallel=1 → deterministic): the running
    # tally crosses 250 after ~3 items; the 4th+ never dispatch — a flagged partial, not a full run.
    seen: list[Any] = []
    spent = {"t": 0}

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(item)
        spent["t"] += 100  # the engine's on_cost tally would carry this post-return
        return {"item": item}

    members = [_m("w", fan_out=OHMFanOut(over="$.items", max_parallel=1))]
    res = await run_team(
        _team(members, budget=OHMBudget(max_tokens_total=250)),
        dispatch,
        state={"items": ["i1", "i2", "i3", "i4", "i5"]},
        cost_so_far=lambda: spent["t"],
    )
    assert res.status == "cost_budget"  # the governed budget-halt terminal (NOT "failed")
    assert res.partial is True  # the run completed PARTIALLY at the pool ceiling
    assert len(seen) < 5  # fewer sub-runs than items — halted before the runaway
    assert res.results["w"]  # the already-produced partial outputs are surfaced, not discarded
    assert res.member_status.get("w") == "budget_skipped"  # distinguishable from a member error


async def test_fan_out_halts_by_sub_run_count_ceiling() -> None:
    # the COUNT axis halts independently of tokens: max_sub_runs=2 admits exactly 2, then stops. The
    # sub-run count increments at admission, so this is exact (no soft overshoot, unlike tokens).
    seen: list[Any] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(item)
        return {"item": item}

    members = [_m("w", fan_out=OHMFanOut(over="$.items", max_parallel=1))]
    res = await run_team(
        _team(members, budget=OHMBudget(max_sub_runs=2)),
        dispatch,
        state={"items": ["i1", "i2", "i3", "i4", "i5"]},
    )
    assert res.status == "cost_budget"
    assert res.partial is True
    assert len(seen) == 2  # exactly the sub-run ceiling


async def test_multi_member_team_halts_before_the_next_member_on_token_ceiling() -> None:
    # the pooled check gates EVERY dispatch, not only a fan-out: a sequential a→b→c→d→e team (100
    # tokens/member, ceiling 250) halts before the member that would cross it. This is the
    # DEPLOYED-PROOF shape — the gateway can't seed a fan-out's `over`, but it runs multi-member.
    seen: list[str] = []
    spent = {"t": 0}

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(member.role)
        spent["t"] += 100
        return {"out": member.role}

    members = [
        _m("a"),
        _m("b", depends_on=["a"]),
        _m("c", depends_on=["b"]),
        _m("d", depends_on=["c"]),
        _m("e", depends_on=["d"]),
    ]
    res = await run_team(
        _team(members, budget=OHMBudget(max_tokens_total=250)),
        dispatch,
        cost_so_far=lambda: spent["t"],
    )
    assert res.status == "cost_budget"
    assert res.partial is True
    assert len(seen) < 5  # halted before the runaway — not every member dispatched
    assert any(v == "budget_skipped" for v in res.member_status.values())  # un-run members flagged


@pytest.mark.parametrize("budget", [None, OHMBudget()])
async def test_no_pool_ceiling_path_is_byte_for_byte_unchanged(budget: OHMBudget | None) -> None:
    # the #576 invariant: with NO pooled ceiling (budget None, or all max_*_total None) the run is
    # identical to today — every item dispatched, status completed, NOT flagged partial, no halt.
    seen: list[Any] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(item)
        return {"item": item}

    members = [_m("w", fan_out=OHMFanOut(over="$.items", max_parallel=2))]
    res = await run_team(
        _team(members, budget=budget),
        dispatch,
        state={"items": ["a", "b", "c", "d", "e"]},
    )
    assert res.status == "completed"
    assert res.partial is False
    assert len(seen) == 5  # every fan-out item ran
    assert "budget_skipped" not in res.member_status.values()


async def test_fan_out_sub_run_ceiling_is_exact_under_concurrency() -> None:
    # review BLOCKER: the COUNT axis must be exact even when max_parallel > the headroom — the batch
    # is clamped to the remaining sub-runs, so max_sub_runs=3 with max_parallel=4 admits 3, NOT 4.
    seen: list[Any] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(item)
        return {"item": item}

    members = [_m("w", fan_out=OHMFanOut(over="$.items", max_parallel=4))]
    res = await run_team(
        _team(members, budget=OHMBudget(max_sub_runs=3)),
        dispatch,
        state={"items": ["i1", "i2", "i3", "i4", "i5", "i6"]},
    )
    assert res.status == "cost_budget"
    assert len(seen) == 3  # EXACT — the clamp prevents the max_parallel overshoot (was 4)


async def test_token_ceiling_without_a_tally_fails_closed() -> None:
    # review BLOCKER: a token ceiling with NO tally wired (cost_so_far=None) must FAIL-CLOSED — an
    # unmeasurable ceiling is spent, never silent headroom (§3.5). Nothing should dispatch.
    seen: list[str] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(member.role)
        return {"out": member.role}

    members = [_m("a"), _m("b", depends_on=["a"])]
    # NB: no cost_so_far passed — the token ceiling is unmeasurable
    res = await run_team(_team(members, budget=OHMBudget(max_tokens_total=250)), dispatch)
    assert res.status == "cost_budget"
    assert len(seen) == 0  # fail-closed: the unmeasurable token ceiling halted everything


async def test_a_member_failure_outranks_the_budget_halt() -> None:
    # review SHOULD-FIX: a real member FAILURE must outrank the budget halt — else the failed member
    # is masked as the non-re-runnable COST_BUDGET terminal and stranded (re-run needs FAILED).
    # Staged deterministically: doomed fails in stage 1 (spent still 0, so it dispatches), 'a'
    # exhausts in stage 2, victim is budget_skipped in stage 3 — so a FAILURE and a budget halt
    # genuinely co-occur, and the verdict must be "failed".
    spent = {"t": 0}

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "doomed":
            raise RuntimeError("boom")  # fails in stage 1 (costs nothing)
        if member.role == "a":
            spent["t"] += 200  # exhausts the pool in stage 2
        return {"out": member.role}

    members = [
        _m("doomed"),  # stage 1 — fails
        _m("seed"),  # stage 1 — a's clean upstream (so 'a' isn't blocked by doomed)
        _m("a", depends_on=["seed"]),  # stage 2 — exhausts the pool
        _m("victim", depends_on=["a"]),  # stage 3 — budget_skipped
    ]
    res = await run_team(
        _team(members, budget=OHMBudget(max_tokens_total=150)),
        dispatch,
        cost_so_far=lambda: spent["t"],
    )
    assert res.status == "failed"  # the failure wins over the budget halt → re-runnable
    assert res.member_status["doomed"] == "failed"
    assert res.member_status.get("victim") == "budget_skipped"  # the pool still halted downstream
