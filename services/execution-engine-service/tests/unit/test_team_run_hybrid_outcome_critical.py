"""#834 (DESIGN §C site 3) — the SECOND verdict site: ``run_team_hybrid``'s flat
``if any(s in ("failed", "blocked") ...)`` check at
``services/.../services/team_run.py:1268`` must apply the SAME ``outcome_critical`` rule as
``orchestrate.py``'s own verdict (site 1, pinned in ``packages/ohm/tests/
test_orchestrate_outcome_critical.py``).

Why this is a SEPARATE site and not automatically covered by fixing ``orchestrate.py`` alone: a
loop member's per-round dispatch is recorded ``member_status[role] = "partial"`` INSIDE
``run_loop_seam`` (``orchestrate.py:1111-1114``, mirroring the acyclic ``#587`` path), and that
status is merged into the hybrid's ``skeleton.member_status`` verbatim
(``team_run.py``, ``_emit_checkpoint`` / the loop-results merge) — it never passes back through
``orchestrate.py``'s own ``has_failure`` computation a second time. The flat check at
``team_run.py:1268`` is the ONLY place that sees the merged status and must independently apply the
emptiness rule to a critical member sitting inside a converged loop.

Modelled on ``test_team_run_hybrid.py`` (read it first) — same ``_FakeHarness``/``_loop``/``_team``
shape, same ``coordinate``/``done_check_for`` fixtures. RED until the [impl] lands.
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
_ABSENT = object()


class _PartialCriticalHarness:
    """Every member SUCCEEDS, except ``critical_role`` which degrades (PARTIAL) with its declared
    ``summary`` key set to ``summary_value`` (``_ABSENT`` → the key is omitted entirely)."""

    def __init__(self, critical_role: str, summary_value: Any) -> None:
        self.critical_role = critical_role
        self.summary_value = summary_value
        self.calls: list[str] = []

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        manifest_ref: str | None = None,
        capability_ceiling: list[str] | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        role = (manifest_ref or "?/?").split("/")[-1].split("@")[0]
        self.calls.append(role)
        if role != self.critical_role:
            return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": f"{role}-out"}
        output: Any = f"{role}-best-effort"
        if self.summary_value is not _ABSENT:
            output = {"summary": self.summary_value}  # a member's answer, parsed for declared keys
        return {"id": str(uuid.uuid4()), "status": "PARTIAL", "output": output}


def _m(
    role: str,
    deps: list[str] | None = None,
    *,
    outcome_critical: bool = False,
    outputs_schema: dict[str, Any] | None = None,
) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=deps or [],
        outcome_critical=outcome_critical,
        outputs_schema=outputs_schema or {},
    )


def _loop(*roles: str) -> OHMLoop:
    return OHMLoop(members=list(roles), routing={r: f"do {r}" for r in roles})


def _team(members: list[OHMMember], loops: list[OHMLoop], max_rounds: int = 5) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        orchestration=OHMOrchestration(
            loops=loops, termination=OHMTermination(max_rounds=max_rounds)
        ),
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _coordinate_until_all_produced():
    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return [r for r in loop.members if results.get(r) is None]

    return coordinate


def _done_when_all_produced(loop: OHMLoop, diag: dict[str, Any] | None = None):
    async def done(results: dict[str, Any]) -> bool:
        return all(results.get(r) is not None for r in loop.members)

    return done


async def _hybrid(manifest: OHMManifest, harness: Any, **kw: Any):
    from oraclous_execution_engine_service.services.team_run import run_team_hybrid

    return await run_team_hybrid(manifest, harness, **kw)


async def test_a_critical_loop_member_that_delivered_still_completes() -> None:
    # RULING-1 regression guard, inside a loop this time — the SAME narrowing applies here.
    h = _PartialCriticalHarness("w", ["a", "b"])
    res = await _hybrid(
        _team(
            [_m("w", outcome_critical=True, outputs_schema={"required": ["summary"]})], [_loop("w")]
        ),
        h,
        coordinate=_coordinate_until_all_produced(),
        done_check_for=_done_when_all_produced,
    )
    assert res.member_status["w"] == "partial"
    assert res.status == "completed"  # delivered content inside the loop → not a failure


async def test_a_critical_loop_member_with_a_missing_declared_key_fails_the_hybrid_run() -> None:
    h = _PartialCriticalHarness("w", _ABSENT)
    res = await _hybrid(
        _team(
            [_m("w", outcome_critical=True, outputs_schema={"required": ["summary"]})], [_loop("w")]
        ),
        h,
        coordinate=_coordinate_until_all_produced(),
        done_check_for=_done_when_all_produced,
    )
    assert res.member_status["w"] == "partial"
    assert res.status == "failed"  # the merged (loop-side) status must fail the hybrid verdict too


async def test_a_critical_loop_member_with_an_empty_declared_key_fails_the_hybrid_run() -> None:
    h = _PartialCriticalHarness("w", [])
    res = await _hybrid(
        _team(
            [_m("w", outcome_critical=True, outputs_schema={"required": ["summary"]})], [_loop("w")]
        ),
        h,
        coordinate=_coordinate_until_all_produced(),
        done_check_for=_done_when_all_produced,
    )
    assert res.member_status["w"] == "partial"
    assert res.status == "failed"


async def test_a_non_critical_loop_member_with_empty_output_is_unaffected() -> None:
    h = _PartialCriticalHarness("w", [])
    res = await _hybrid(
        _team([_m("w", outputs_schema={"required": ["summary"]})], [_loop("w")]),  # not critical
        h,
        coordinate=_coordinate_until_all_produced(),
        done_check_for=_done_when_all_produced,
    )
    assert res.member_status["w"] == "partial"
    assert res.status == "completed"  # unchanged from today


async def test_a_failed_verdict_still_surfaces_with_a_downstream_skeleton_member_present() -> None:
    # a critical loop member's empty output fails the run even when a downstream skeleton member
    # exists — the loop itself CONVERGED (done_check saw "w" produce something), so this does not
    # exercise the non-convergence/#551 blocking path; it only pins that the merged verdict still
    # comes out "failed" in a team shaped with downstream skeleton members, not only a bare loop.
    h = _PartialCriticalHarness("w", [])
    mf = _team(
        [
            _m("w", outcome_critical=True, outputs_schema={"required": ["summary"]}),
            _m("publish", ["w"]),
        ],
        [_loop("w")],
    )
    res = await _hybrid(
        mf, h, coordinate=_coordinate_until_all_produced(), done_check_for=_done_when_all_produced
    )
    assert res.status == "failed"
    assert res.member_status["w"] == "partial"
