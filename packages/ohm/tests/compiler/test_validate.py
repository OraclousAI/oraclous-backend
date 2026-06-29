"""#594 — the reviewer's validate gate: one validator, capability-absence (ADR-032), JSON peel.

``validate_draft`` is the deterministic ``manifest-validate`` tool the reviewer's in-harness repair
loop calls — ``would_block`` is a coded boolean, never the model's opinion.
"""

from __future__ import annotations

import json
import uuid

import pytest
from oraclous_ohm.compiler.validate import validate_draft

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _draft(tool: str) -> dict:
    return {
        "members": [
            {"role": "researcher", "kind": "agent", "manifest_ref": "org:x/r@1", "tools": [tool]},
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:x/w@1",
                "depends_on": ["researcher"],
            },
        ]
    }


def test_a_surveyed_tool_passes() -> None:
    catalog = {"tools": [{"name": "web-search", "ref": "core/web-search@1"}]}
    v = validate_draft(_draft("web-search"), catalog, owner_organization_id=_ORG)
    assert v["would_block"] is False  # the tool resolves to the surveyed catalog → ready


def test_an_unsurveyed_tool_fails_closed() -> None:
    # the drafter hallucinated 'teleport' — not surveyed → blocked, never run (ADR-032)
    v = validate_draft(_draft("teleport"), ["web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in v["blocking"])
    assert "GO: BLOCKED" in v["report"]  # render_report surfaces the block to the reviewer


def test_an_empty_draft_fails_closed_no_members() -> None:
    # a draft with no members (a drafter that produced nothing) → F-NO-MEMBERS, never a crash
    v = validate_draft({"members": []}, ["web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-NO-MEMBERS" in b for b in v["blocking"])


def test_a_tool_written_as_a_full_ref_matches_the_surveyed_name() -> None:
    # the drafter may write the surveyed REF (core/web-search@1.0.0) instead of the bare name; both
    # normalise to the same slug, so a legitimate surveyed tool is NOT falsely blocked (the deployed
    # run caught this — gpt-4o-mini drafted the full ref).
    catalog = {"tools": [{"name": "web-search", "ref": "core/web-search@1.0.0"}]}
    v = validate_draft(_draft("core/web-search@1.0.0"), catalog, owner_organization_id=_ORG)
    assert v["would_block"] is False


def test_prose_wrapped_json_is_peeled_not_misblocked() -> None:
    # a REAL drafter LLM wraps the JSON in prose / a ```json fence — it must still parse (#599)
    draft = (
        "Here is the team you asked for:\n```json\n" + json.dumps(_draft("web-search")) + "\n```\n"
    )
    v = validate_draft(draft, ["web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is False  # peeled + validated, NOT F-DRAFT-INVALID


def test_garbage_fails_closed() -> None:
    v = validate_draft(
        "Sorry, I could not build a team.", ["web-search"], owner_organization_id=_ORG
    )
    assert v["would_block"] is True
    assert any("F-DRAFT-INVALID" in b for b in v["blocking"])
