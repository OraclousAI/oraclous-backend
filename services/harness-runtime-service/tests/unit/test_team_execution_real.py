"""STEP 3 — the orchestrator runs a team through the REAL harness execution loop (E3; ADR-035).

Not a mock: ``run_team`` drives each member through the actual ``run_tool_use_loop`` with the actual
``build_envelope`` policy (including the now-populated capability ceiling). Only the LLM is the
deterministic ``FakeLLMClient`` — the key-free client CI/smoke uses — so the run is reproducible.
This proves the orchestrator is wired to real execution, and the ceiling enforces end-to-end
(``build_envelope`` populates it → ``run_tool_use_loop`` denies an out-of-ceiling tool).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.llm.factory import build_fake_client
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import build_envelope, resolve_policy_set
from oraclous_harness_runtime_service.models.enums import HarnessStatus
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime
from oraclous_ohm.orchestrate import run_team
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _sub_harness(role: str, *, body: str, capabilities: list[dict[str, Any]] | None = None) -> dict:
    """A real single-agent sub-harness doc (actors-path) that ``load_ohm`` accepts and runs."""
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": role, "owner_organization_id": str(_ORG)},
        "capabilities": capabilities or [],
        "prompts": [{"role": "primary", "source": "inline", "body": body}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }


async def _real_harness_dispatch(member: OHMMember, envelopes: list, item: Any) -> dict:
    """Dispatch a member by running the REAL run_tool_use_loop with a real policy + the fake LLM."""
    manifest = load_ohm(_sub_harness(member.role, body=member.subgoal or "do the work"))
    envelope = build_envelope(manifest, resolve_policy_set(None), hard_max_iterations=8)
    user_input = (
        "\n".join(f"From {e.from_role}: {json.dumps(e.payload, default=str)}" for e in envelopes)
        or "go"
    )
    prompt = manifest.primary_prompt()

    async def _no_tools(spec: ToolSpec, args: dict) -> dict:  # reasoning-only member: never called
        return {}

    result = await run_tool_use_loop(
        llm=build_fake_client(),
        system=prompt.body if prompt else "",
        user_input=user_input,
        tool_specs=[],
        dispatch=_no_tools,
        policy=envelope,
    )
    return {"status": result.status.name, "output": result.output}


def _m(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        subgoal=f"work {role}",
        depends_on=deps or [],
    )


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def test_two_member_team_runs_through_the_real_harness_loop() -> None:
    # the orchestrator drives BOTH members through the actual run_tool_use_loop — not a mock harness
    res = await run_team(_team([_m("a"), _m("b", ["a"])]), _real_harness_dispatch)
    assert res.status == "completed"
    assert res.results["a"]["status"] == HarnessStatus.SUCCEEDED.name  # a really executed
    assert res.results["b"]["status"] == HarnessStatus.SUCCEEDED.name  # b really executed, after a
    assert res.results["b"]["output"]  # the real loop produced an answer


async def test_ceiling_from_build_envelope_denies_out_of_ceiling_tool_end_to_end() -> None:
    # The full item-4 chain, end-to-end on real code: build_envelope populates the ceiling from the
    # declared capabilities, and the real run_tool_use_loop then DENIES a tool outside it.
    manifest = load_ohm(
        _sub_harness(
            "a", body="go", capabilities=[{"ref": "core/postgresql-reader@1.0.0", "binding": "pg"}]
        )
    )
    envelope = build_envelope(manifest, resolve_policy_set(None), hard_max_iterations=8)
    assert envelope.tool_ceiling == frozenset(
        {"pg"}
    )  # populated for real, not a test-supplied value

    # a tool OUTSIDE the ceiling (as a coordinator/bug might provision); the fake LLM will try it
    outside = ToolSpec(
        name="shell_run",
        description="x",
        parameters={"type": "object", "properties": {}, "required": []},
        binding="shell",
        operation="run",
    )
    dispatched: list[str] = []

    async def _watch(spec: ToolSpec, args: dict) -> dict:
        dispatched.append(spec.binding)
        return {}

    result = await run_tool_use_loop(
        llm=build_fake_client(),
        system="",
        user_input="go",
        tool_specs=[outside],
        dispatch=_watch,
        policy=envelope,
    )
    assert "shell" not in dispatched  # the out-of-ceiling capability was NEVER dispatched
    assert any("capability_denied" in (s.detail or "") for s in result.steps)
