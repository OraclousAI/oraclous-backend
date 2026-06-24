"""Team-run per-run graph_id binding (#524 Phase-0, E6 / ADR-040 Decision 7 — cloud-first).

A cloud team's tools are remapped onto the graph capabilities (knowledge-retriever / graph-ingest /
find-similar), which all REQUIRE a ``graph_id``. The run supplies it ONCE — a trusted per-run
``graph_id`` binding the engine threads to every member's harness execution → the graph-tool
instance config → ExecutionContext — so the model never has to invent a UUID. Same as #518's
``workspace_root``.

RED until #524 [impl] adds the ``graph_id`` parameter to the team-run bridge.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_run import run_team_harness
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


class _RecordingHarness:
    """Records the ``graph_id`` each member execute() receives; always succeeds."""

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
    ) -> dict[str, Any]:
        self.calls.append({"graph_id": graph_id})
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ran"}


def _m(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=deps or [])


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def test_graph_id_threads_to_every_member_harness_call() -> None:
    """Each member's harness execution receives the run's graph_id (its graph tools target it)."""
    harness = _RecordingHarness()
    gid = "11111111-2222-3333-4444-555555555555"
    await run_team_harness(_team([_m("a"), _m("b", ["a"])]), harness, graph_id=gid)
    assert len(harness.calls) == 2
    assert all(c["graph_id"] == gid for c in harness.calls)


async def test_without_a_graph_id_members_get_none() -> None:
    """Back-compat: a run without a bound graph (local/non-graph team) threads no graph_id."""
    harness = _RecordingHarness()
    await run_team_harness(_team([_m("a")]), harness)
    assert harness.calls[0]["graph_id"] is None
