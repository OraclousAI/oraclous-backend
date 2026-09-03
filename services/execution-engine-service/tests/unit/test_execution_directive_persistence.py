"""The run directive must describe where a member's output ACTUALLY goes (#694, defect 3).

``EXECUTION_DIRECTIVE`` is a constant today and it asserts, unconditionally:

    your Write tool persists your output to the team's shared knowledge graph

True on the import on-ramp, where ``Write`` resolves to ``graph-ingest``. False for every compiled
member on run ``fe548aac``, all 14 of which held ``core/write@1`` and wrote disposable files while
being told they were saving to the graph.

This is not cosmetic. Telling a model a false fact about its own tools is the mechanism by which
that run looked successful and delivered nothing: each member persisted "successfully", reported
done, and the next member read an empty graph.

So the persistence sentence is DERIVED from the member's resolved sub-harness capability refs, and
a member holding neither kind gets the directive with that sentence OMITTED — not a guess.

RED until the [impl] replaces the constant with a builder.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_GRAPH = "core/graph-ingest@1.0.0"
_RETRIEVE = "core/knowledge-retriever@1.0.0"
_SANDBOX_WRITE = "core/write@1"
_SANDBOX_READ = "core/read@1"


def _directive(refs: list[str]) -> str:
    from oraclous_execution_engine_service.services.team_run import execution_directive

    return execution_directive(refs)


def test_a_graph_member_is_told_about_the_knowledge_graph() -> None:
    text = _directive([_GRAPH, _RETRIEVE]).lower()
    assert "knowledge graph" in text
    assert "sandbox" not in text


def test_a_sandbox_member_is_told_about_its_workspace() -> None:
    """The file substrate is legitimate — the member just has to be told the truth about it."""
    text = _directive([_SANDBOX_WRITE, _SANDBOX_READ]).lower()
    assert "sandbox" in text
    assert "knowledge graph" not in text


def test_a_member_with_neither_gets_no_persistence_sentence() -> None:
    """A pure-reasoning stage, or one holding only ``web-research``. Silence beats a guess: a
    guess here is precisely what produced 14 misinformed members."""
    text = _directive(["core/web-research@1", "core/websearch@1"]).lower()
    assert "knowledge graph" not in text
    assert "sandbox" not in text


def test_a_tool_less_member_gets_no_persistence_sentence() -> None:
    # Not total silence any more: #696 gives a genuinely tool-less member the
    # "no tools" sentence (below). This pins only the #694 half — no substrate is NAMED.
    text = _directive([]).lower()
    assert "knowledge graph" not in text
    assert "sandbox" not in text


def test_the_executor_reframing_survives_in_every_case() -> None:
    """The directive's OTHER job (#543): re-frame an imported conductor persona as the EXECUTOR so
    it does the work instead of proposing a handoff. Deriving the persistence sentence must not
    drop it — a member that only proposes a '## Handoff' is still not an acceptable result."""
    for refs in ([_GRAPH], [_SANDBOX_WRITE], [], ["core/web-research@1"]):
        text = _directive(refs).lower()
        assert "handoff" in text
        assert "use your tools" in text


def test_the_version_ignores_the_ref_version_suffix() -> None:
    """The registry resolves by slug and drops ``@version``. A member holding
    ``core/graph-ingest@2`` is the same kind of member as one holding ``@1.0.0``."""
    for ref in ("core/graph-ingest@1", "core/graph-ingest@1.0.0", "core/graph-ingest@2.1.0"):
        assert "knowledge graph" in _directive([ref]).lower()


def test_a_member_holding_both_kinds_is_told_about_the_graph() -> None:
    """Ambiguous, and the graph is the invariant (ADR-041 Decision 3) — a sink that writes
    externally without graph-indexing is non-conformant, so the graph sentence wins."""
    text = _directive([_GRAPH, _SANDBOX_WRITE]).lower()
    assert "knowledge graph" in text


# --- #696: a member with no tools is TOLD it has none, before it is graded on claiming otherwise -


def test_a_tool_less_member_is_told_it_cannot_persist_or_fetch_anything() -> None:
    """Prevention before detection. Run fe548aac's reviewer held no tools and still closed with two
    file paths it had "documented"; nothing had told it that it could not. The sentence names the
    consequence in the words the #697 contract uses (`artifact_refs`), so the reply shape it is
    asked for and the claim it must not make are the same instruction. (The directive cannot see
    ``outputs_schema``, so a member with no declared keys is told about an ``artifact_refs`` it
    never emits — harmless, and the sentence still says what it must not claim.)"""
    text = _directive([])
    lowered = text.lower()
    assert "no tools" in lowered
    assert "artifact_refs" in text
    # it still says nothing about a substrate it does not have (#694's silence rule holds)
    assert "knowledge graph" not in lowered
    assert "sandbox" not in lowered


def test_a_member_holding_tools_is_not_told_it_has_none() -> None:
    for refs in ([_GRAPH], [_SANDBOX_WRITE], ["core/web-research@1"]):
        assert "no tools" not in _directive(refs).lower(), refs


def test_a_manifest_ref_dispatch_of_a_tooled_member_is_not_told_the_false_fact() -> None:
    """#694's own failure shape, reopened by #696: a ``manifest_ref`` dispatch resolves NO
    sub-harness, so ``capability_refs`` is always ``[]`` for it — even for a member that DID
    declare tools (``member.tools``, the ceiling checked at dispatch). Telling such a member it
    has none would be exactly the false fact #694 exists to prevent, in the new sentence's words
    instead of the graph one."""
    from oraclous_execution_engine_service.services.team_run import execution_directive

    text = execution_directive([], declared_tools=["core/write@1"])
    assert "no tools" not in text.lower()
