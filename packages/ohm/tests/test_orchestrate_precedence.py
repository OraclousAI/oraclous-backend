"""Precedence enforcement THROUGH run_team (#514, E6) — the dispatch-boundary integration.

The pure resolver (test_precedence_resolution.py) is wired into run_team's dispatch boundary: each
member's inbound is resolved against ``manifest.precedence`` before dispatch (CTO ruling — resolve
at dispatch, where hand-offs + reads converge). Two run_team-level guarantees here:
  * a MEMBER's hand-off is fail-closed to the non-canonical floor tier — a member can never inject a
    canonical (rules/bible/toc) claim (the item-9/§22 book invariant, at the orchestrator);
  * with NO precedence declared, run_team behaves EXACTLY as before (fail-soft, no change).

Mirrors test_orchestrate.py's dispatch-double pattern. RED until #514 [impl] tags + resolves inbound
(``HandoffEnvelope.source_layer`` does not exist yet → AttributeError).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMPrecedence, OHMRuntime
from oraclous_ohm.orchestrate import run_team

pytestmark = [pytest.mark.unit, pytest.mark.integration]

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_ORDER = ["rules", "bible", "toc", "drafts"]


def _m(role: str, depends_on: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=depends_on or []
    )


def _team(members: list[OHMMember], *, precedence: OHMPrecedence | None = None) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        precedence=precedence,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


async def test_member_handoffs_are_tagged_with_the_non_canonical_floor_tier() -> None:
    """With precedence declared, a member's hand-off carries the LOWEST tier (drafts) — a member can
    never inject a canonical (rules/bible/toc) claim through a hand-off."""
    seen: list[HandoffEnvelope] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.extend(envs)
        return {"out": member.role}

    await run_team(
        _team([_m("a"), _m("b", ["a"])], precedence=OHMPrecedence(order=_ORDER)),
        dispatch,
    )
    # b received a's hand-off, tagged with the floor tier — never a canonical layer
    inbound = [e for e in seen if e.to_role == "b"]
    assert len(inbound) == 1
    assert inbound[0].source_layer == "drafts"  # member hand-off clamped to the non-canonical floor


async def test_without_precedence_inbound_is_unchanged_fail_soft() -> None:
    """No precedence on the manifest → run_team reasons EXACTLY as before (no enforce, no tag)."""
    calls: list[tuple[str, list[str]]] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        calls.append((member.role, [e.from_role for e in envs]))
        return {"out": member.role}

    res = await run_team(_team([_m("a"), _m("b", ["a"]), _m("c", ["b"])]), dispatch)
    # identical to the pre-#514 contract (test_orchestrate.py::test_sequential_pipeline_threads…)
    assert [c[0] for c in calls] == ["a", "b", "c"]
    assert calls[1][1] == ["a"] and calls[2][1] == ["b"]
    assert res.results["c"] == {"out": "c"}
