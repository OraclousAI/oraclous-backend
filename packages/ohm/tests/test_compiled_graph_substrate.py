"""A COMPILED member's file tools must remap to the graph, exactly as an IMPORTED member's do
(#694, ADR-041 Decision 3 / ADR-040 Decision 7).

``test_import_graph_remap.py`` already pins this for the import on-ramp, whose tools arrive as
Claude-Code names (``Write``). This file pins the SAME contract for the compiler on-ramp, whose
tools arrive as lower-cased catalog slugs (``write``) — the case the shipped remap misses.

Live evidence (team run ``fe548aac``, real org, 14 members, graph ``a2815be3`` bound): every
member resolved to ``core/write@1`` / ``core/edit@1``, ~10 KB of deliverables landed in
``/tmp/oraclous-agent-sandbox/<org>/``, and the bound graph kept the 4 nodes the compiler team
itself had written. ADR-041 Decision 3 names a sink that writes without graph-indexing
NON-CONFORMANT, so this is a conformance defect, not a feature request.

RED until the [impl] slug-normalises ``_GRAPH_REMAP``.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.import_.mapping import build_subharness

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("11112222-3333-4444-5555-666677778888")

#: what the compiler's drafter actually emits — the surveyed catalog is lower-cased slugs.
_COMPILER_CASE = ["read", "write", "edit", "grep", "glob", "bash"]
#: what the importer emits — the Claude-Code tool names. Already remapped correctly today.
_IMPORT_CASE = ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]

_GRAPH_EXPECTED = {
    "read": "core/knowledge-retriever@1.0.0",
    "grep": "core/knowledge-retriever@1.0.0",
    "glob": "core/find-similar@1.0.0",
    "write": "core/graph-ingest@1.0.0",
    "edit": "core/graph-ingest@1.0.0",
    "bash": "core/bash@1",  # the rare exec need stays the sandbox fallback (#507)
}


def _caps(tools: list[str], substrate: str = "graph") -> dict[str, str]:
    sub = build_subharness(
        "writer",
        owner_organization_id=_ORG,
        body="You are the writer.",
        tools=tools,
        substrate=substrate,  # type: ignore[arg-type] — Literal["graph","file"]
    )
    return {c.binding: c.ref for c in sub.capabilities}


def test_lower_cased_compiler_tools_remap_to_the_graph_capabilities() -> None:
    """THE bug. ``write`` is the same tool as ``Write`` and must reach ``graph-ingest``."""
    caps = _caps(_COMPILER_CASE)
    assert caps == _GRAPH_EXPECTED


def test_the_two_on_ramps_agree_on_where_a_file_tool_goes() -> None:
    """Case is the ONLY difference between the two on-ramps' tool names. It must not be a
    difference in behaviour — that divergence is the whole of #694."""
    compiled = _caps(_COMPILER_CASE)
    imported = _caps(_IMPORT_CASE)
    assert {k.lower(): v for k, v in imported.items()} == compiled


@pytest.mark.parametrize("tool", ["write", "Write", "WRITE"])
def test_every_casing_of_write_reaches_graph_ingest(tool: str) -> None:
    assert _caps([tool])[tool] == "core/graph-ingest@1.0.0"


def test_the_remap_preserves_the_binding_so_the_ceiling_stays_valid() -> None:
    """ADR-032: the member's ``tools`` ceiling is BINDING-based. Remapping the ref must leave the
    binding byte-for-byte, or the ceiling check at team_run_service stops matching."""
    caps = _caps(["write", "read"])
    assert set(caps) == {"write", "read"}  # the declared names, verbatim


def test_the_file_substrate_opt_out_still_keeps_the_sandbox_refs() -> None:
    """The parked local single-tenant mode (#512/#518) is unchanged, in BOTH casings — the fix is
    a normalisation of the lookup, not a removal of the file substrate."""
    assert _caps(["write", "read", "bash"], substrate="file") == {
        "write": "core/write@1",
        "read": "core/read@1",
        "bash": "core/bash@1",
    }
    assert _caps(["Write", "Read"], substrate="file") == {
        "Write": "core/write@1",
        "Read": "core/read@1",
    }


def test_a_non_file_tool_is_untouched_under_either_substrate() -> None:
    """Only the five file tools remap. A connector keeps its synthesized ``core/<slug>@1``."""
    assert _caps(["web-research"])["web-research"] == "core/web-research@1"
    assert _caps(["graph-ingest"])["graph-ingest"] == "core/graph-ingest@1"


def test_a_tool_less_member_still_builds_a_reasoning_only_sub_harness() -> None:
    """A pure-reasoning stage is valid and must survive the change untouched."""
    sub = build_subharness(
        "planner", owner_organization_id=_ORG, body="You are the planner.", tools=[]
    )
    assert sub.capabilities == []
    assert sub.metadata.kind == "agent"
