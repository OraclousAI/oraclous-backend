"""Team-run bridge (#419 wiring): run_team driven by the real harness-execution path.

Pure unit with a fake harness client — proves each member becomes a harness call, typed hand-offs
thread into the harness input, a member failure fails closed, an inline sub-harness is passed, and a
human gate pauses the run through the bridge. (The durable persistence is a later wiring step.)
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_run import (
    render_member_input,
    run_team_harness,
)
from oraclous_ohm.manifest import (
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


async def test_member_harness_failure_fails_closed() -> None:
    class _Failing:
        async def execute(self, **kw: Any) -> dict[str, Any]:
            return {"status": "FAILED", "output": None}

    with pytest.raises(Exception):  # noqa: B017,PT011 — a member that doesn't SUCCEED fails the run
        await run_team_harness(_team([_m("a")]), _Failing())


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
