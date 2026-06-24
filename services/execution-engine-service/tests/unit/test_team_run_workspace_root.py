"""Team-run file-native path (#518): the per-run ``workspace_root`` threads to every member.

A file-native team supplies its real git-markdown working tree ONCE, at the team run (the trusted,
engine-bound ``workspace_root`` — not user-controlled tool config). It must reach EVERY member's
harness execution so each member's file tools operate in place on that tree. This asserts the
threading contract through ``run_team_harness`` → ``make_harness_dispatch`` → ``harness.execute``.

RED until #518 [impl] adds the ``workspace_root`` parameter to the team-run bridge.
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
    """Records the ``workspace_root`` each member execute() receives; always succeeds."""

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
    ) -> dict[str, Any]:
        self.calls.append({"role_input": input_text, "workspace_root": workspace_root})
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


async def test_workspace_root_threads_to_every_member_harness_call() -> None:
    """Each member's harness execution receives the run's workspace_root."""
    harness = _RecordingHarness()
    work = "/tmp/oraclous-agent-workspaces/87654321-4321-8765-4321-876543210000/book"  # noqa: S108

    await run_team_harness(_team([_m("a"), _m("b", ["a"])]), harness, workspace_root=work)

    assert len(harness.calls) == 2
    assert all(c["workspace_root"] == work for c in harness.calls)


async def test_without_a_workspace_root_members_get_none() -> None:
    """Back-compat: a non-file-native team threads no workspace_root (default per-org scratch)."""
    harness = _RecordingHarness()
    await run_team_harness(_team([_m("a")]), harness)
    assert harness.calls[0]["workspace_root"] is None
