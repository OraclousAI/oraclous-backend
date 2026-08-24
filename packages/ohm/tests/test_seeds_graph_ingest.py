"""The seed inventory must offer a write-side GRAPH capability (#694, amendment 1).

Why this is part of the fix rather than a nicety. The drafter did not pick ``write`` on prior — it
picked the only write-shaped tool it was ever shown:

    writer archetype       ["write", "text-tools"]
    editor archetype       ["read", "edit"]
    tool_group filesystem  ["read", "write", "edit", "grep", "glob"]
    tool_group knowledge   ["knowledge-retriever", "find-similar", "recall-memory"]   ← read-only

``graph-ingest`` appears NOWHERE in the seed inventory, so the one seeded write-side graph
capability is missing from the catalog entirely. Filtering the file tools out under a graph
substrate WITHOUT adding it would leave the drafter able to read the graph and persist nothing —
a different failure with the same outcome as #694.

The inventory keeps the file tools: they are still the right answer under ``substrate="file"``
(the parked local single-tenant mode). The filtering is the CATALOG's job, not the inventory's —
see ``test_compiler_onramp_substrate.py``.

RED until the [impl] extends ``seed_capability_inventory``.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.seeds import default_seed_set

pytestmark = pytest.mark.unit


def _archetype(name: str) -> list[str]:
    inv = default_seed_set().inventory
    return next(a.tools for a in inv.archetypes if a.name == name)


def _tool_group(name: str) -> list[str]:
    inv = default_seed_set().inventory
    return next(g.tools for g in inv.tool_groups if g.name == name)


def test_the_writer_archetype_can_persist_to_the_graph() -> None:
    """A writer's whole job is producing a deliverable. Under the cloud default that deliverable
    belongs on the graph, so the archetype must offer the tool that puts it there."""
    assert "graph-ingest" in _archetype("writer")


def test_the_editor_archetype_can_persist_to_the_graph() -> None:
    """Run ``fe548aac`` FAILED on the Editor. It held ``core/edit@1`` and had no graph tool."""
    assert "graph-ingest" in _archetype("editor")


def test_the_knowledge_tool_group_is_no_longer_read_only() -> None:
    """``knowledge`` offered three RETRIEVAL tools and no way to write back."""
    knowledge = _tool_group("knowledge")
    assert "graph-ingest" in knowledge
    # the read side is unchanged — this adds, it does not replace
    assert {"knowledge-retriever", "find-similar", "recall-memory"} <= set(knowledge)


def test_the_file_tools_stay_in_the_inventory_for_the_file_substrate() -> None:
    """The parked local single-tenant mode still needs them. The graph substrate filters them out
    of the CATALOG; it does not delete them from the inventory."""
    assert set(_tool_group("filesystem")) == {"read", "write", "edit", "grep", "glob"}


def test_every_seeded_tool_stays_a_real_registered_capability() -> None:
    """The seed contract (ADR-047 §5): a survey that merges the inventory never offers a phantom.
    ``graph-ingest`` is the registry's ``GraphIngestPlugin`` (builtin.py), so it qualifies —
    this guards against the amendment being satisfied with an invented name."""
    inv = default_seed_set().inventory
    seeded = {t for a in inv.archetypes for t in a.tools} | {
        t for g in inv.tool_groups for t in g.tools
    }
    assert "graph-ingest" in seeded
    assert "graph-ingestion" not in seeded and "ingest" not in seeded
