"""The validator blocks a file-write tool under the graph substrate, with a NAMED reason (#694).

``validate_draft`` checks catalog membership and DAG acyclicity today, so run ``fe548aac``'s draft
passed clean while declaring ``write`` on a graph-bound team. Filtering the catalog (see
``test_compiler_onramp_substrate.py``) stops the drafter CHOOSING a file tool; this gate stops one
reaching storage by any other route — a hand-edited draft, a refine op, a draft compiled before
the fix, or a live-registry capability whose name collides.

The reason is coded ``F-SUBSTRATE-FILE`` and names the member and the tool, because the reviewer
member is a MODEL: it re-drafts against ``blocking``, and "something is wrong" is not something it
can act on. A bare ``F-CAPABILITY-MISSING`` would be technically true here (the graph catalog no
longer lists ``write``) and would send the reviewer looking for a typo instead of a substrate.

RED until the [impl] adds the ``substrate`` parameter and the gate.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("aaaabbbb-cccc-dddd-eeee-ffff00001111")

#: what the catalog offers under the graph substrate — no file tools, plus the graph write side.
_GRAPH_CATALOG = ["web-research", "knowledge-retriever", "find-similar", "graph-ingest", "bash"]
#: the file substrate's catalog, where the file tools are legitimate.
_FILE_CATALOG = ["web-research", "read", "write", "edit", "grep", "glob", "bash"]


def _draft(tools: list[str], role: str = "editor") -> dict:
    return {
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:compiled/researcher@1",
                "subgoal": "gather the evidence",
                "tools": ["web-research"],
                "depends_on": [],
                "outputs_schema": {"required": ["summary"]},  # #697
            },
            {
                "role": role,
                "kind": "agent",
                "manifest_ref": f"org:compiled/{role}@1",
                "subgoal": "write the assessment",
                "tools": tools,
                "depends_on": ["researcher"],
                "outputs_schema": {"required": ["summary"]},  # #697
            },
        ],
        "orchestration": {"style": "pipeline", "success_criteria": "the assessment exists"},
    }


def _validate(draft: dict, catalog: list[str], substrate: str = "graph") -> dict:
    from oraclous_ohm.compiler.validate import validate_draft

    return validate_draft(
        draft,
        catalog,
        owner_organization_id=_ORG,
        name="compiled-team",
        substrate=substrate,
    )


@pytest.mark.parametrize("tool", ["write", "edit", "Write", "Edit"])
def test_a_file_write_tool_blocks_under_the_graph_substrate(tool: str) -> None:
    verdict = _validate(_draft([tool]), _GRAPH_CATALOG)
    assert verdict["would_block"] is True
    assert any("F-SUBSTRATE-FILE" in str(b) for b in verdict["blocking"])


def test_the_block_names_the_member_and_the_tool() -> None:
    """The reviewer re-drafts from this text. It has to know WHICH member and WHICH tool."""
    verdict = _validate(_draft(["write"], role="assessor"), _GRAPH_CATALOG)
    blocking = " ".join(str(b) for b in verdict["blocking"])
    assert "assessor" in blocking
    assert "write" in blocking


def test_the_substrate_reason_replaces_the_missing_capability_one() -> None:
    """Both are true — the graph catalog no longer lists ``write``, so the capability-absence gate
    fires too. Reporting both sends the reviewer model looking for a typo. The substrate reason
    takes precedence and is the ONLY finding raised for that tool."""
    verdict = _validate(_draft(["write"]), _GRAPH_CATALOG)
    findings = [str(b) for b in verdict["blocking"] if "write" in str(b)]
    assert len(findings) == 1, findings
    assert "F-SUBSTRATE-FILE" in findings[0]
    assert "F-CAPABILITY-MISSING" not in findings[0]


def test_a_genuinely_unknown_tool_still_reports_the_absence() -> None:
    """The precedence above must not swallow the ADR-032 gate for a hallucinated tool."""
    verdict = _validate(_draft(["totally-invented-tool"]), _GRAPH_CATALOG)
    assert verdict["would_block"] is True
    blocking = " ".join(str(b) for b in verdict["blocking"])
    assert "F-CAPABILITY-MISSING" in blocking
    assert "F-SUBSTRATE-FILE" not in blocking


def test_the_graph_write_tool_passes_under_the_graph_substrate() -> None:
    """The gate blocks the file sink, not writing. ``graph-ingest`` is the sanctioned one."""
    verdict = _validate(_draft(["graph-ingest"]), _GRAPH_CATALOG)
    assert verdict["would_block"] is False, verdict["blocking"]


def test_the_same_draft_passes_under_the_file_substrate() -> None:
    """The parked local single-tenant mode is unchanged — ``write`` is correct there."""
    verdict = _validate(_draft(["write"]), _FILE_CATALOG, substrate="file")
    assert verdict["would_block"] is False, verdict["blocking"]


def test_bash_is_not_a_file_write_tool_and_still_passes() -> None:
    """``bash`` stays the sandbox exec fallback (#507) under both substrates."""
    verdict = _validate(_draft(["bash"]), _GRAPH_CATALOG)
    assert verdict["would_block"] is False, verdict["blocking"]


def test_a_read_side_file_tool_also_blocks() -> None:
    """``read``/``grep``/``glob`` against an EMPTY per-org sandbox is #509's Gap 1: the model loops
    looking for files nobody wrote. Under the graph substrate the retrieval tools are the answer."""
    verdict = _validate(_draft(["read"]), _GRAPH_CATALOG)
    assert verdict["would_block"] is True
    assert any("F-SUBSTRATE-FILE" in str(b) for b in verdict["blocking"])


def test_the_substrate_defaults_to_graph() -> None:
    """Cloud-first (ADR-040 Decision 7). A caller that passes nothing gets the safe substrate —
    a default of ``file`` would put a tenant's deliverables in a server-side tmp tree."""
    from oraclous_ohm.compiler.validate import validate_draft

    verdict = validate_draft(
        _draft(["write"]), _GRAPH_CATALOG, owner_organization_id=_ORG, name="compiled-team"
    )
    assert verdict["would_block"] is True
    assert any("F-SUBSTRATE-FILE" in str(b) for b in verdict["blocking"])
