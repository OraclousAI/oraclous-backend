"""ADR-043 #552 PR-B2 — the BYOM loop coordinator: a bounded model turn that ONLY routes (picks the
next loop member). Two invariants this pins: (1) PICKS-ONLY — the coordinator sub-harness carries
zero capabilities and every call sends ``capability_ceiling=[]`` (it can never grant/call a tool);
(2) LEAK-SAFE — the prompt carries the loop's STRUCTURE (roles, routing intent, a produced/not flag)
but NEVER a member's raw output, so customer text is never re-emitted into a model prompt or a log.
Parsing is fail-closed (DONE / garbage / an outsider role → no dispatch).

RED until the coordinator lands — imported function-locally so the module still collects.
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
    OHMRuntime,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _team() -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=[
            OHMMember(role="writer", kind="agent", manifest_ref="o:x/writer@1"),
            OHMMember(role="critic", kind="agent", manifest_ref="o:x/critic@1"),
        ],
        runtime=OHMRuntime(entrypoint="writer"),
    )


def _loop() -> OHMLoop:
    return OHMLoop(members=["writer", "critic"], routing={"writer": "draft", "critic": "review"})


class _PickHarness:
    """A fake harness that returns a canned coordinator pick + records the ceilings it was sent."""

    def __init__(self, output: str) -> None:
        self.output = output
        self.ceilings: list[Any] = []
        self.prompts: list[str] = []

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        capability_ceiling: list[str] | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        self.ceilings.append(capability_ceiling)
        self.prompts.append(input_text)
        return {"status": "SUCCEEDED", "output": self.output}


def test_prompt_is_leak_safe_no_raw_member_output() -> None:
    from oraclous_execution_engine_service.services.team_run import _render_coordinator_prompt

    results = {"writer": {"output": "TOP-SECRET-CUSTOMER-DRAFT"}, "critic": None}
    prompt = _render_coordinator_prompt(_loop(), results, rounds_left=3)
    assert "TOP-SECRET-CUSTOMER-DRAFT" not in prompt  # NO raw output text
    assert "writer" in prompt and "critic" in prompt  # structure present
    assert "not yet produced" in prompt  # the produced/not flag, not the content


def test_parse_next_roles_is_fail_closed_and_picks_only() -> None:
    from oraclous_execution_engine_service.services.team_run import _parse_next_roles

    allowed = {"writer", "critic"}
    assert _parse_next_roles("critic", allowed=allowed) == ["critic"]
    assert _parse_next_roles("  writer \n", allowed=allowed) == ["writer"]
    assert _parse_next_roles("'critic'.", allowed=allowed) == ["critic"]  # quotes/punct tolerated
    assert _parse_next_roles("DONE", allowed=allowed) == []  # done → coded check decides
    assert _parse_next_roles("done — looks good", allowed=allowed) == []
    assert _parse_next_roles("outsider", allowed=allowed) == []  # hallucination → no-op, not abort
    assert _parse_next_roles("", allowed=allowed) == []
    assert _parse_next_roles(None, allowed=allowed) == []


def test_coordinator_subharness_is_tool_less() -> None:
    from oraclous_execution_engine_service.services.team_run import _coordinator_subharness

    sub = _coordinator_subharness(_team())
    assert sub["capabilities"] == []  # picks-only — zero tools (ADR-043 invariant)


async def test_coordinator_routes_only_to_loop_members_with_zero_ceiling() -> None:
    from oraclous_execution_engine_service.services.team_run import make_loop_coordinator

    h = _PickHarness(output="critic")
    coordinate = make_loop_coordinator(h, _team())
    picks = await coordinate(_loop(), {"writer": {"output": "x"}}, 5)
    assert picks == ["critic"]
    assert h.ceilings == [[]]  # capability_ceiling=[] — the router is granted no capability


async def test_coordinator_unreachable_router_fails_closed_to_no_pick() -> None:
    from oraclous_execution_engine_service.services.harness_client import HarnessClientError
    from oraclous_execution_engine_service.services.team_run import make_loop_coordinator

    class _Down:
        async def execute(self, **kw: Any) -> dict[str, Any]:
            raise HarnessClientError("router down")

    coordinate = make_loop_coordinator(_Down(), _team())
    picks = await coordinate(_loop(), {}, 5)
    assert picks == []  # unreachable router → [] → the coded done-check decides (never crashes)
