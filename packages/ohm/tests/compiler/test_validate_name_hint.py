"""#899 — a blocked tool name must tell the re-drafting member what the right name would have been.

``validate_draft`` already fails closed on a tool outside the surveyed catalogue (ADR-032), and
that must not change. What it does not do is say what the drafter should have written. The member
reading the verdict is a MODEL with two repair attempts, and #705 is the recorded cost of leaving
it to guess: one name dropped while re-typing a 72-entry list blocked an entire compile.

The neighbouring flags already learned this. ``F-SUBSTRATE-FILE`` names the tools to use instead
(#694), and #751 made the schema failure name the field and the reason. This flag was missed.

These tests pin the hint, not its wording, and they pin the parts that must NOT move.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.compiler.validate import validate_draft

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")

_CATALOG = ["web-search", "knowledge-retriever", "graph-ingest", "find-similar"]


def _draft(tool: str) -> dict:
    return {
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:x/r@1",
                "tools": [tool],
                "outputs_schema": {"required": ["summary"]},
            },
        ]
    }


def _capability_blocks(verdict: dict) -> list[str]:
    return [b for b in verdict["blocking"] if "F-CAPABILITY-MISSING" in b]


def test_a_misspelled_tool_is_told_the_real_name() -> None:
    v = validate_draft(_draft("web-serch"), _CATALOG, owner_organization_id=_ORG)

    assert v["would_block"] is True
    blocks = _capability_blocks(v)
    assert blocks, "the fail-closed gate must still fire"
    assert "web-search" in blocks[0]
    assert "web-serch" in blocks[0]  # the rejected name is still named


def test_the_hint_compares_normalised_names() -> None:
    """``Web Search`` and ``core/web-search@1`` are the same tool to this gate, so the suggestion
    has to be measured on the normalised form. Comparing raw strings would score a legitimate
    spelling of a surveyed tool as far from its own catalogue entry."""
    v = validate_draft(_draft("Web  Serch"), _CATALOG, owner_organization_id=_ORG)

    assert v["would_block"] is True
    assert "web-search" in _capability_blocks(v)[0]


def test_a_name_with_no_near_miss_gets_no_invented_suggestion() -> None:
    """A hallucinated name that resembles nothing must not be answered with the least-bad match.
    A wrong suggestion is worse than none: the member will take it, and the gate will block again
    for a new reason."""
    v = validate_draft(_draft("teleport"), _CATALOG, owner_organization_id=_ORG)

    assert v["would_block"] is True
    block = _capability_blocks(v)[0]
    assert "teleport" in block
    assert not any(name in block for name in _CATALOG)


@pytest.mark.parametrize(
    "tool",
    ["evil/web-search", "😈/web-search", "./web-search", "@", ""],
)
def test_the_fail_closed_gate_is_unchanged(tool: str) -> None:
    """The hint is added ALONGSIDE the gate. A namespaced or degenerate identifier that slugs away
    from a surveyed name must still block, and must never be waved through because it LOOKS close
    to one — that is exactly what ``tool_slug``'s ``ns--`` marker exists to prevent (#594)."""
    v = validate_draft(_draft(tool), _CATALOG, owner_organization_id=_ORG)

    assert v["would_block"] is True
    assert _capability_blocks(v), f"{tool!r} must block as a missing capability"


def test_a_surveyed_tool_still_passes() -> None:
    v = validate_draft(_draft("web-search"), _CATALOG, owner_organization_id=_ORG)

    assert v["would_block"] is False
