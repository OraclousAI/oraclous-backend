"""Team-run precedence threading (#538, E6 / ADR-040) — manifest.precedence → every member's run.

The engine threads the team's Hierarchy of Truth (``order`` high→low + the ``graph`` mode) to each
member's harness execution — exactly like the #524 ``graph_id`` binding — so the harness binds it on
each knowledge-retriever instance and a member's in-loop retrieval is auto-ranked canonical-first
(#536 does the ranking). Captured at the ``run_team_harness`` bridge with a recording double;
no live harness, no LLM. Back-compat: a run without precedence threads ``None``/``False`` to every
member (byte-for-byte the pre-#538 behaviour).
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
    """Records the precedence each member execute() receives; always succeeds."""

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
    ) -> dict[str, Any]:
        self.calls.append(
            {"precedence_order": precedence_order, "graph_authoritative": graph_authoritative}
        )
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


async def test_precedence_threads_to_every_member_harness_call() -> None:
    """Each member's harness execution receives the team's precedence (so its retriever ranks)."""

    harness = _RecordingHarness()
    order = ["rules", "bible", "toc", "drafts"]
    await run_team_harness(
        _team([_m("a"), _m("b", ["a"])]),
        harness,
        precedence_order=order,
        graph_authoritative=True,
    )
    assert len(harness.calls) == 2
    assert all(c["precedence_order"] == order for c in harness.calls)
    assert all(c["graph_authoritative"] is True for c in harness.calls)


async def test_without_precedence_members_get_none_fail_soft() -> None:
    """Back-compat: a run with no declared Hierarchy of Truth threads no precedence (None/False)."""
    harness = _RecordingHarness()
    await run_team_harness(_team([_m("a")]), harness)
    assert harness.calls[0]["precedence_order"] is None
    assert harness.calls[0]["graph_authoritative"] is False
