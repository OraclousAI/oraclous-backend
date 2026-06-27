"""#576 — the per-member cap resolves from the team manifest and threads to each member's harness.

The team manifest carries the user's choice (``budget.max_tokens_per_member`` team-wide default +
``member.max_tokens`` per-member override). The team-run bridge must resolve each member's effective
cap and pass it to that member's harness execution, so the harness enforces the user's budget rather
than the hardcoded policy tier. This asserts the threading contract
``run_team_harness`` → ``make_harness_dispatch`` → ``resolve_member_caps`` → ``harness.execute``.

RED until #576 [impl] resolves + threads ``max_tokens`` / ``max_tool_calls`` in the team-run bridge.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_run import run_team_harness
from oraclous_ohm.manifest import OHMBudget, OHMManifest, OHMMember, OHMMetadata, OHMRuntime

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


class _RecordingHarness:
    """Records the per-member caps each execute() receives; always succeeds."""

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
        precedence_order: list[str] | None = None,
        graph_authoritative: bool = False,
        max_tokens: int | None = None,
        max_tool_calls: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "role": (manifest_ref or ""),
                "max_tokens": max_tokens,
                "max_tool_calls": max_tool_calls,
            }
        )
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ran"}


def _m(role: str, *, max_tokens: int | None = None, max_tool_calls: int | None = None) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        max_tokens=max_tokens,
        max_tool_calls=max_tool_calls,
    )


def _team(members: list[OHMMember], budget: OHMBudget | None = None) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        budget=budget,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _by_role(calls: list[dict[str, Any]], role: str) -> dict[str, Any]:
    return next(c for c in calls if c["role"].endswith(f"/{role}@1"))


async def test_member_override_and_team_default_thread_to_each_harness() -> None:
    harness = _RecordingHarness()
    team = _team(
        [_m("fact-checker", max_tokens=600_000), _m("researcher")],
        budget=OHMBudget(max_tokens_per_member=300_000, max_tool_calls_per_member=90),
    )
    await run_team_harness(team, harness)

    fc = _by_role(harness.calls, "fact-checker")
    rs = _by_role(harness.calls, "researcher")
    assert fc["max_tokens"] == 600_000  # the heavy member's own override
    assert rs["max_tokens"] == 300_000  # the team-wide per-member default
    assert fc["max_tool_calls"] == 90 and rs["max_tool_calls"] == 90  # team default for both


async def test_no_budget_threads_none_back_compat() -> None:
    # A team with no budget → no per-member cap → the harness keeps the policy tier (unchanged).
    harness = _RecordingHarness()
    await run_team_harness(_team([_m("a")]), harness)
    assert harness.calls[0]["max_tokens"] is None
    assert harness.calls[0]["max_tool_calls"] is None


async def test_member_cap_is_clamped_to_the_pooled_total() -> None:
    # ADR-031 keystone: a member asking for more than the team's pool is clamped to the pool.
    harness = _RecordingHarness()
    team = _team([_m("greedy", max_tokens=9_000_000)], budget=OHMBudget(max_tokens_total=1_000_000))
    await run_team_harness(team, harness)
    assert harness.calls[0]["max_tokens"] == 1_000_000
