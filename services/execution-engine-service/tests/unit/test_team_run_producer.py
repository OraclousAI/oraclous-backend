"""Team-run producer binding (#728) — an artifact knows which member of which run wrote it.

An artifact recorded nothing about its producer. Because the lexical document node keys on the
filename, and every agent write arrived as the constant ``inline.txt``, a run's whole output
collapsed onto ONE node: run ``dc167d8e`` landed 7 artifacts and kept 1.

The engine binds the producer onto every member dispatch, the same trusted path ``graph_id``
travels (#524), so the model can neither supply nor forge its own identity.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_run import run_team_harness
from oraclous_ohm.manifest import OHMFanOut, OHMManifest, OHMMember, OHMMetadata, OHMRuntime

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


class _RecordingHarness:
    """Records the ``producer`` each member execute() receives; always succeeds."""

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
        producer: dict[str, Any] | None = None,
        precedence_order: list[str] | None = None,
        graph_authoritative: bool = False,
        max_tokens: int | None = None,
        max_tool_calls: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"producer": producer})
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ran"}


def _m(role: str, deps: list[str] | None = None, fan: OHMFanOut | None = None) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=deps or [],
        fan_out=fan,
    )


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def test_every_member_is_told_who_it_is() -> None:
    """Each dispatch carries its own role, so two members' artifacts are distinguishable."""
    harness = _RecordingHarness()
    await run_team_harness(
        _team([_m("Adoption-writer"), _m("Partner-writer", ["Adoption-writer"])]), harness
    )
    roles = [c["producer"]["member_role"] for c in harness.calls]
    assert roles == ["Adoption-writer", "Partner-writer"]
    assert all(c["producer"]["producer_kind"] == "team-member" for c in harness.calls)


async def test_the_run_and_team_identity_ride_along() -> None:
    """team_run_id + team_id make an artifact findable by run and across a team's runs."""
    harness = _RecordingHarness()
    team = _team([_m("a")])
    trace = uuid.uuid4()
    await run_team_harness(team, harness, trace_id=trace)
    producer = harness.calls[0]["producer"]
    assert producer["team_run_id"] == str(trace)
    assert producer["team_id"] == str(team.metadata.id)


async def test_a_run_without_a_trace_still_names_its_member() -> None:
    """Fail-soft: no trace id yet (a direct/unit drive) still identifies WHO wrote."""
    harness = _RecordingHarness()
    await run_team_harness(_team([_m("Editor")]), harness)
    producer = harness.calls[0]["producer"]
    assert producer["member_role"] == "Editor"
    assert "team_run_id" not in producer


async def test_a_fan_out_member_disambiguates_its_sub_runs() -> None:
    """A fan-out member's sub-runs share one role, so the fan index separates their artifacts."""
    harness = _RecordingHarness()
    member = _m("Researcher", fan=OHMFanOut(over="$.items", max_parallel=2))
    await run_team_harness(
        _team([member]),
        harness,
        inputs={"items": [{"index": 0}, {"index": 1}]},
    )
    ordinals = [c["producer"].get("ordinal") for c in harness.calls]
    assert ordinals == [0, 1]


async def test_the_producer_is_never_absent_for_a_team_member() -> None:
    """The regression guard: a member dispatched with no producer writes an anonymous artifact."""
    harness = _RecordingHarness()
    await run_team_harness(_team([_m("a"), _m("b", ["a"])]), harness)
    assert all(c["producer"] is not None for c in harness.calls)
    assert all(c["producer"].get("member_role") for c in harness.calls)
