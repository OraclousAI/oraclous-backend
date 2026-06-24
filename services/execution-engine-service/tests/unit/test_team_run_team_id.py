"""Team-run team_id threading (#513 — graph-adopt team-scope blackboard, E6 / ADR-027 reshape).

A team's members share a TEAM-scope memory blackboard: each member's writes/reads are scoped to the
**team**, not the lone agent, so concurrent members see each other and the team's world-model
accumulates across runs (the bitcoin/DoefinGPT shared world-model). The team is identified by the
STABLE OHM team-manifest id (`metadata.id`) — the engine derives it from the team manifest and
threads it to every member's harness execution (the same execution-context carrier #524 used for
``graph_id``), so a member's memory hook + per-turn read are bound to the team.

RED until #513 [impl] derives + threads ``team_id`` through the team-run bridge.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_run import run_team_harness
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_TEAM_ID = uuid.UUID("11112222-3333-4444-5555-666677778888")


class _RecordingHarness:
    """Records the ``team_id`` each member execute() receives; always succeeds."""

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
        self.calls.append({"team_id": team_id})
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ran"}


def _m(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=deps or [])


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=_TEAM_ID, name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def test_team_id_is_the_team_manifest_id_threaded_to_every_member() -> None:
    """Each member's harness execution is bound to the STABLE team-manifest id (metadata.id), so a
    member's team-scope memory write/read shares one team identity across the run (and runs)."""
    harness = _RecordingHarness()
    await run_team_harness(_team([_m("a"), _m("b", ["a"])]), harness)
    assert len(harness.calls) == 2
    assert all(c["team_id"] == str(_TEAM_ID) for c in harness.calls)
