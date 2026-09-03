"""#718 — a member that holds a tool must say WHY, and a team that armed itself with none is a
signal worth a human's attention, not a silent pass.

Two new checks in ``validate_draft`` (packages/ohm/src/oraclous_ohm/compiler/validate.py), inside
the existing per-member/per-tool loop that already raises F-CAPABILITY-MISSING / F-SUBSTRATE-FILE:

- ``F-TOOL-UNJUSTIFIED`` (blocking): a member holds a tool with no non-blank
  ``tool_rationale[tool]`` entry. Compiler run history shows a member handed a tool that could not
  do its job (``knowledge-retriever`` given to a member reviewing an unmerged pull request, run
  ``a3443e24``) — the gate cannot judge FIT, but it CAN require the drafter to have stated a reason
  tied to THIS member's sub-goal, which is cheap to check and expensive to skip.
- ``F-TEAM-NO-TOOLS`` (confirm, non-blocking): every member declares ``tools: []`` while the
  surveyed catalog is non-empty. Never blocks — ``would_block`` stays driven only by
  blocking-severity flags — but worth a human's look.

RED-by-design until the ``[impl]`` lands: ``OHMMember`` carries no ``tool_rationale`` field yet, so
a draft using it is silently dropped (``model_config = ConfigDict(extra="ignore")``) and neither
check exists in ``validate_draft`` at all.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.compiler.validate import validate_draft

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_CATALOG = ["web-search", "graph-ingest"]


def _member(role: str, **over: object) -> dict:
    base: dict = {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "tools": [],
        "depends_on": [],
        "outputs_schema": {"required": ["summary"]},  # #697 — kept satisfied throughout this file
    }
    base.update(over)
    return base


def _draft(members: list[dict]) -> dict:
    return {"members": members}


# ── F-TOOL-UNJUSTIFIED: blocking, per member/tool ────────────────────────────


def test_a_tool_with_no_rationale_entry_blocks() -> None:
    draft = _draft(
        [
            _member("researcher", tools=["web-search"]),
            _member("writer", depends_on=["researcher"]),
        ]
    )
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True, v
    assert any("F-TOOL-UNJUSTIFIED" in b for b in v["blocking"]), v["blocking"]


def test_a_stated_rationale_clears_this_check() -> None:
    draft = _draft(
        [
            _member(
                "researcher",
                tools=["web-search"],
                tool_rationale={"web-search": "needs live results to answer the objective"},
            ),
            _member("writer", depends_on=["researcher"]),
        ]
    )
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert not any("F-TOOL-UNJUSTIFIED" in b for b in v["blocking"]), v["blocking"]
    assert v["would_block"] is False, v


def test_a_blank_rationale_still_blocks() -> None:
    # the check is .strip()-based — whitespace is not a reason
    draft = _draft(
        [
            _member("researcher", tools=["web-search"], tool_rationale={"web-search": "   "}),
            _member("writer", depends_on=["researcher"]),
        ]
    )
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True, v
    assert any("F-TOOL-UNJUSTIFIED" in b for b in v["blocking"]), v["blocking"]


def test_the_unjustified_member_is_named_so_the_reviewer_can_repair_it() -> None:
    draft = _draft(
        [
            _member("researcher", tools=["web-search"]),
            _member("writer", depends_on=["researcher"]),
        ]
    )
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert any("researcher" in b for b in v["blocking"]), v["blocking"]


# ── F-TEAM-NO-TOOLS: confirm, team-level, never blocks ───────────────────────


def test_every_member_with_zero_tools_and_a_nonempty_catalog_gets_a_confirm_flag() -> None:
    draft = _draft([_member("researcher"), _member("writer", depends_on=["researcher"])])
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is False, v  # confirm severity never blocks
    assert not any("F-TEAM-NO-TOOLS" in b for b in v["blocking"])  # never in blocking
    assert "F-TEAM-NO-TOOLS" in v["report"]  # but visible in the rendered report (CONFIRM line)


def test_an_empty_catalog_never_gets_the_no_tools_confirm_flag() -> None:
    # nothing was on offer, so an empty tools[] is not a signal worth flagging
    draft = _draft([_member("researcher"), _member("writer", depends_on=["researcher"])])
    v = validate_draft(draft, [], owner_organization_id=_ORG)
    assert "F-TEAM-NO-TOOLS" not in v["report"]


def test_at_least_one_member_with_a_tool_suppresses_the_no_tools_confirm_flag() -> None:
    draft = _draft(
        [
            _member(
                "researcher",
                tools=["web-search"],
                tool_rationale={"web-search": "needs live results"},
            ),
            _member("writer", depends_on=["researcher"]),
        ]
    )
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert "F-TEAM-NO-TOOLS" not in v["report"]
