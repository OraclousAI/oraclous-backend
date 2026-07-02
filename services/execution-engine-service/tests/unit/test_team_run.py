"""Team-run bridge (#419 wiring): run_team driven by the real harness-execution path.

Pure unit with a fake harness client — proves each member becomes a harness call, typed hand-offs
thread into the harness input, a member failure is RECORDED (ADR-042 non-aborting: it does not abort
the team, the verdict is "failed"), an inline sub-harness is passed, and a human gate pauses the run
through the bridge. (The durable persistence is a later wiring step.)
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.domain.refresh import REFRESH_SEED_KEY
from oraclous_execution_engine_service.services.team_run import (
    REFRESH_CARRY_FORWARD_DIRECTIVE,
    refresh_dispatch_args,
    render_member_input,
    run_team_harness,
)
from oraclous_ohm.manifest import (
    OHMBudget,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMRunIf,
    OHMRuntime,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


class _FakeHarness:
    """Records every execute() call and always succeeds."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "input": input_text,
                "ref": manifest_ref,
                "inline": manifest_inline is not None,
                "ceiling": capability_ceiling,
                "parent_execution_id": parent_execution_id,
                "trace_id": trace_id,
                "workspace_root": workspace_root,
            }
        )
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ran"}


def _m(role: str, deps: list[str] | None = None, tools: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=deps or [],
        tools=tools or [],
    )


def _gate(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="human", human_role="author", depends_on=deps or [])


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def test_each_member_runs_as_a_harness_call_with_threaded_handoff() -> None:
    harness = _FakeHarness()
    res = await run_team_harness(_team([_m("a"), _m("b", ["a"])]), harness)
    assert res.status == "completed"
    assert len(harness.calls) == 2  # a and b each executed as a harness call
    assert any(
        "From a:" in c["input"] for c in harness.calls
    )  # b got a's typed hand-off, not a blob


async def test_member_harness_failure_is_recorded_not_aborted() -> None:
    # ADR-042 (#551): a member whose harness does not SUCCEED no longer ABORTS the team run (it used
    # to raise out of run_team_harness). The failure is RECORDED — the member is "failed" and the
    # team verdict is "failed" (→ FAILED) — so independent members still run and the failed member
    # is re-runnable. A single-member team that fails returns a "failed" result, not a raise.
    class _Failing:
        async def execute(self, **kw: Any) -> dict[str, Any]:
            return {"status": "FAILED", "output": None}

    res = await run_team_harness(_team([_m("a")]), _Failing())
    assert res.status == "failed"
    assert res.member_status == {"a": "failed"}


async def test_inline_subharness_is_passed_when_provided() -> None:
    harness = _FakeHarness()
    await run_team_harness(_team([_m("a")]), harness, sub_harnesses={"a": {"ohm_version": "1.0"}})
    assert harness.calls[0]["inline"] is True  # the generated sub-harness went inline, not by ref


async def test_member_tools_are_passed_as_the_capability_ceiling_inline_and_by_ref() -> None:
    # Red-team G-A: the member's tools[] must cap the harness for BOTH the inline AND the
    # manifest_ref path, so a registered manifest_ref harness can never exceed what the member
    # declared. The dispatch always sends capability_ceiling=member.tools.
    harness = _FakeHarness()
    # 'a' runs inline (sub-harness given); 'b' runs by manifest_ref (no sub-harness) — both capped
    await run_team_harness(
        _team([_m("a", tools=["Read", "Write"]), _m("b", ["a"], tools=["Grep"])]),
        harness,
        sub_harnesses={"a": {"ohm_version": "1.0"}},
    )
    by_input = {("a" if c["inline"] else "b"): c for c in harness.calls}
    assert by_input["a"]["ceiling"] == ["Read", "Write"]  # inline member: capped by its tools[]
    assert by_input["b"]["ceiling"] == ["Grep"]  # manifest_ref member: ALSO capped (the bypass fix)
    assert (
        by_input["b"]["ref"] is not None and by_input["b"]["inline"] is False
    )  # truly the ref path


async def test_human_gate_pauses_through_the_bridge() -> None:
    harness = _FakeHarness()
    res = await run_team_harness(_team([_m("a"), _gate("g", ["a"]), _m("b", ["g"])]), harness)
    assert res.status == "paused" and res.paused_at == ["g"]
    assert all("Objective" not in c["input"] or "b" not in c["input"] for c in harness.calls)
    assert len(harness.calls) == 1  # only 'a' ran; 'b' is past the gate and never dispatched


def test_render_member_input_threads_envelopes() -> None:
    text = render_member_input(_m("c", ["a"]), [], fan_item={"k": 1})
    assert "Item:" in text


def test_render_member_input_prefers_the_inbound_objective_slice() -> None:
    # #577: the per-edge handoff objective_slice scopes the dispatch, overriding the subgoal — this
    # is the consumer that makes the threaded objective actually bind (it was a dead field).
    from oraclous_ohm.envelope import HandoffEnvelope

    member = OHMMember(
        role="writer", kind="agent", manifest_ref="org:x/writer@1", subgoal="draft a chapter"
    )
    env = HandoffEnvelope(
        from_role="showrunner", to_role="writer", objective_slice="Draft Chapter 04"
    )
    text = render_member_input(member, [env])
    assert "Objective: Draft Chapter 04" in text  # the producer's scoped per-edge objective leads
    assert "draft a chapter" not in text  # NOT the static subgoal blurb (the "Chapter XX" symptom)


def test_render_member_input_falls_back_to_subgoal_without_an_objective_slice() -> None:
    member = OHMMember(
        role="writer", kind="agent", manifest_ref="org:x/writer@1", subgoal="draft a chapter"
    )
    text = render_member_input(member, [])  # no inbound handoff
    assert "Objective: draft a chapter" in text  # back-compat: the static subgoal stands


# ── #602 cost lever: the sink member receives its prior records to carry forward ──────────────────


def test_render_member_input_without_refresh_records_is_unchanged_default_off() -> None:
    # a normal (non-refresh) dispatch: no carry-forward directive, no prior-records block.
    text = render_member_input(_m("reporter"), [])
    assert "REFRESH run" not in text
    assert "prior records" not in text.lower()


def test_render_member_input_renders_the_prior_records_and_carry_forward_directive() -> None:
    prior = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    text = render_member_input(_m("reporter"), [], refresh_records=prior)
    assert REFRESH_CARRY_FORWARD_DIRECTIVE in text  # the member is told to carry forward
    assert '"refresh_status": "unchanged"' in text  # the exact marker to emit
    assert '"id": "a"' in text and '"id": "b"' in text  # its actual prior records are present
    assert "Your prior records (2)" in text


def test_refresh_dispatch_args_targets_the_single_sink_of_a_seeded_refresh() -> None:
    # source -> reporter (the sink). A seeded refresh threads the seed records to the sink only.
    team = _team([_m("source"), _m("reporter", ["source"])])
    inputs = {REFRESH_SEED_KEY: {"records": [{"id": "a"}], "seed_records_parsed": True}}
    records, sink = refresh_dispatch_args(team, inputs)
    assert sink == "reporter" and records == [{"id": "a"}]


def test_refresh_dispatch_args_is_off_for_a_non_refresh_or_empty_or_multi_sink() -> None:
    team = _team([_m("source"), _m("reporter", ["source"])])
    assert refresh_dispatch_args(team, None) == (None, None)  # not a refresh
    assert refresh_dispatch_args(team, {}) == (None, None)  # no seed key
    empty = {REFRESH_SEED_KEY: {"records": [], "seed_records_parsed": False}}
    assert refresh_dispatch_args(team, empty) == (None, None)  # unparseable/empty seed → no lever
    two_sinks = _team([_m("a"), _m("b")])  # two independent sinks → ambiguous, no carry-forward
    seed = {REFRESH_SEED_KEY: {"records": [{"id": "x"}], "seed_records_parsed": True}}
    assert refresh_dispatch_args(two_sinks, seed) == (None, None)


def test_refresh_dispatch_args_suppresses_carry_forward_for_a_fan_out_sink() -> None:
    # #602 review Finding 1: a fan_out sink would re-render the seed per fan-item (inverting the
    # saving) — the lever is suppressed for it (the delta still computes at settle).
    from oraclous_ohm.manifest import OHMFanOut

    fan_sink = OHMMember(
        role="reporter",
        kind="agent",
        manifest_ref="org:x/reporter@1",
        fan_out=OHMFanOut(over="$.items"),
    )
    team = _team([fan_sink])
    seed = {REFRESH_SEED_KEY: {"records": [{"id": "a"}], "seed_records_parsed": True}}
    assert refresh_dispatch_args(team, seed) == (None, None)


def test_render_member_input_fan_in_takes_the_first_objective_and_keeps_all_payloads() -> None:
    # #577 documented limitation: a FAN-IN consumer takes the FIRST inbound objective_slice (dep
    # order) for its Objective line; per-producer objective composition is out of scope for this
    # slice (the targeted pipeline artifacts have one handoff producer per consumer). EVERY
    # producer's payload still reaches the member via the From-lines — a deliberate, tested choice.
    from oraclous_ohm.envelope import HandoffEnvelope

    member = OHMMember(
        role="gamma", kind="agent", manifest_ref="org:x/gamma@1", subgoal="integrate"
    )
    e1 = HandoffEnvelope(
        from_role="alpha", to_role="gamma", objective_slice="task A", payload={"a": 1}
    )
    e2 = HandoffEnvelope(
        from_role="beta", to_role="gamma", objective_slice="task B", payload={"b": 2}
    )
    text = render_member_input(member, [e1, e2])
    assert "Objective: task A" in text and "Objective: task B" not in text  # first inbound wins
    assert "From alpha:" in text and "From beta:" in text  # both producers' payloads still reach it


async def test_run_if_conditional_dispatch_is_honoured_through_the_team_run_bridge() -> None:
    # Connectedness: conditional dispatch reaches the team-run path — run_team_harness -> run_team
    # evaluates OHMMember.run_if from the manifest (reachable via POST /v1/engine/team-runs), not an
    # injected predicate. research SUCCEEDS, so 'run only if status != SUCCEEDED' skips instrument.
    harness = _FakeHarness()
    instrument = OHMMember(
        role="instrument",
        kind="agent",
        manifest_ref="org:x/instrument@1",
        depends_on=["research"],
        run_if=OHMRunIf(from_role="research", field="status", op="ne", value="SUCCEEDED"),
    )
    res = await run_team_harness(_team([_m("research"), instrument]), harness)
    assert "instrument" in res.skipped  # conditionally skipped on research's status
    assert len(harness.calls) == 1  # only research ran; instrument never dispatched
    assert all(c["ref"] != "org:x/instrument@1" for c in harness.calls)


class _TokenHarness:
    """A fake harness returning a fixed RAW token cost per call, so the running pooled tally (fed by
    on_cost) can cross a team ceiling mid-run — the #585 pooled-budget pre-dispatch gate."""

    def __init__(self, tokens_per_call: int) -> None:
        self.tokens = tokens_per_call
        self.calls = 0

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": "ran",
            "total_tokens": self.tokens,
        }


def _budget_team(members: list[OHMMember], budget: OHMBudget) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        budget=budget,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def test_run_team_harness_halts_a_multi_member_run_at_the_pooled_token_ceiling() -> None:
    # #585 the PRODUCTION dispatch factory (make_harness_dispatch) gates EVERY member on the pooled
    # 100 tokens/member, max_tokens_total=250 → after ~2-3 members the running tally crosses 250 and
    # the next member is never dispatched. A flagged partial halt (cost_budget), not failed.
    harness = _TokenHarness(tokens_per_call=100)
    members = [_m("a"), _m("b", ["a"]), _m("c", ["b"]), _m("d", ["c"]), _m("e", ["d"])]
    res = await run_team_harness(_budget_team(members, OHMBudget(max_tokens_total=250)), harness)
    assert res.status == "cost_budget"  # the governed budget-halt terminal, NOT "failed"
    assert res.partial is True
    assert harness.calls < 5  # halted before the last member(s) — fewer dispatches than members
    assert any(v == "budget_skipped" for v in res.member_status.values())  # not a member error


async def test_run_team_harness_no_pool_ceiling_runs_every_member_unchanged() -> None:
    # the #576 invariant at the engine seam: no pooled ceiling → identical to today (every member
    # runs, completed, not partial). Locks that the new gate is inert when max_*_total is unset.
    harness = _TokenHarness(tokens_per_call=100)
    members = [_m("a"), _m("b", ["a"]), _m("c", ["b"])]
    res = await run_team_harness(_budget_team(members, OHMBudget()), harness)
    assert res.status == "completed"
    assert res.partial is False
    assert harness.calls == 3  # every member dispatched


class _RecordingHarness:
    """Records every execute() kwarg + succeeds — to assert what the dispatch threads (#587)."""

    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    async def execute(self, **kw: Any) -> dict[str, Any]:
        self.kwargs.append(kw)
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ok", "total_tokens": 0}


async def test_dispatch_threads_on_exhaustion_to_the_harness() -> None:
    # #587: a member's resolved on_exhaustion rides to harness.execute exactly like the #576 caps.
    harness = _RecordingHarness()
    member = OHMMember(role="a", kind="agent", manifest_ref="org:x/a@1", on_exhaustion="degrade")
    await run_team_harness(_team([member]), harness)
    assert harness.kwargs[0].get("on_exhaustion") == "degrade"


class _PartialHarness:
    """A member whose loop DEGRADED — a flagged PARTIAL (best-effort output), not a fault."""

    async def execute(self, **kw: Any) -> dict[str, Any]:
        return {
            "id": str(uuid.uuid4()),
            "status": "PARTIAL",
            "output": "best-effort",
            "total_tokens": 50,
        }


async def test_run_team_harness_partial_member_does_not_fail_the_team() -> None:
    # #587: a PARTIAL member (on_exhaustion=degrade) is recorded "partial" and does NOT make the
    # team failed — the dispatch must NOT raise on PARTIAL (only FAILED raises); the team completes.
    res = await run_team_harness(_team([_m("a")]), _PartialHarness())
    assert res.member_status["a"] == "partial"
    assert res.status == "completed"
