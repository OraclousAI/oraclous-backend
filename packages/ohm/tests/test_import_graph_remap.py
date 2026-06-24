"""Importer file→graph tool remap (#509, E6 / ADR-040 Decision 7 — cloud-first / graph-primary).

The product is cloud-first: the graph is the primary substrate, so an imported team's declared file
tools are remapped onto the seeded GRAPH capabilities — members RETRIEVE FROM / WRITE TO the graph,
not a server-side file sandbox. The remap changes only the capability **ref** (the registry resolves
by slug, dropping ``@version``); the **binding** is preserved, so the member's ``tools`` ceiling
(ADR-032, binding-based) stays valid and the model keeps calling ``Read``/``Write`` — they just hit
graph retrieval / ingest. ``substrate="file"`` is the explicit opt-out for the parked
local-single-tenant mode (#512/#518, kept as-is, not extended).

RED until #509 [impl] adds the ``substrate`` parameter (default ``"graph"``) + the remap.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.import_.mapping import map_agent_to_member
from oraclous_ohm.import_.parse import AgentDefinition

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _agent(tools: list[str]) -> AgentDefinition:
    return AgentDefinition(
        name="researcher",
        description="Gathers evidence.",
        model="sonnet",
        tools=tools,
        skills=[],
        body="You are researcher.",
        source="researcher.md",
    )


def test_graph_is_the_default_substrate_file_tools_remap_to_graph_capabilities() -> None:
    """Cloud default: Read/Grep → knowledge-retriever, Glob → find-similar, Write → graph-ingest."""
    m = map_agent_to_member(
        _agent(["Read", "Grep", "Glob", "Write", "Edit", "Bash"]), owner_organization_id=_ORG
    )
    assert m.sub_harness is not None
    caps = {c.binding: c.ref for c in m.sub_harness.capabilities}
    assert caps == {
        "Read": "core/knowledge-retriever@1.0.0",
        "Grep": "core/knowledge-retriever@1.0.0",
        "Glob": "core/find-similar@1.0.0",
        "Write": "core/graph-ingest@1.0.0",
        "Edit": "core/graph-ingest@1.0.0",
        "Bash": "core/bash@1",  # the rare exec need stays the sandbox fallback (#507)
    }


def test_the_remap_preserves_bindings_so_the_capability_ceiling_stays_valid() -> None:
    """ADR-032: the ceiling is binding-based — remapping the ref must NOT change the binding."""
    m = map_agent_to_member(_agent(["Read", "Write"]), owner_organization_id=_ORG)
    assert m.sub_harness is not None
    assert m.member.tools == ["Read", "Write"]  # the raw ceiling, verbatim and unchanged
    assert {c.binding for c in m.sub_harness.capabilities} == {"Read", "Write"}


def test_file_substrate_keeps_the_sandbox_tools_local_parked_mode() -> None:
    """The explicit opt-out (local single-tenant, parked): the file-sandbox refs are unchanged."""
    m = map_agent_to_member(
        _agent(["Read", "Write", "Bash"]), owner_organization_id=_ORG, substrate="file"
    )
    assert m.sub_harness is not None
    caps = {c.binding: c.ref for c in m.sub_harness.capabilities}
    assert caps == {"Read": "core/read@1", "Write": "core/write@1", "Bash": "core/bash@1"}


def test_a_non_file_tool_keeps_its_synthesized_ref_under_graph() -> None:
    """Only the file tools remap; a web/other tool keeps core/<slug>@1 under the graph default."""
    m = map_agent_to_member(_agent(["WebSearch"]), owner_organization_id=_ORG)
    assert m.sub_harness is not None
    caps = {c.binding: c.ref for c in m.sub_harness.capabilities}
    assert caps["WebSearch"] == "core/websearch@1"
