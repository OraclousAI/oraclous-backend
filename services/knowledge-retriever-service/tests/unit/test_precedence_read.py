"""Read-side Hierarchy-of-Truth ranking (#514) — the KRS ``_apply_precedence`` post-processor.

Pure, driver-free: given retrieved hits carrying their ``ingestion_source`` path, a declared
precedence order DEMOTES lower-tier/derived hits below canonical ones (stable, nothing dropped) and
stamps each with its path-derived ``precedence_tier``. Absent precedence → identical to today.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_retriever_service.contracts import NodeResult
from oraclous_knowledge_retriever_service.services.retrieval_service import _apply_precedence

pytestmark = pytest.mark.unit

_ORDER = ["rules", "bible", "toc", "drafts"]


def _hit(node_id: str, source: str) -> NodeResult:
    return NodeResult(
        id=node_id, type="Chunk", properties={"ingestion_source": source, "text": "x"}
    )


def test_a_bible_hit_outranks_a_drafts_hit_and_each_is_tier_stamped() -> None:
    hits = [_hit("d", "drafts/ch1.md"), _hit("b", "bible/canon.md")]
    ranked = _apply_precedence(hits, _ORDER, graph_authoritative=False)
    assert [h["id"] for h in ranked] == ["b", "d"]  # canonical first (demote, not drop)
    assert ranked[0]["properties"]["precedence_tier"] == "bible"
    assert ranked[1]["properties"]["precedence_tier"] == "drafts"


def test_a_derived_graph_hit_is_demoted_last_by_default() -> None:
    hits = [_hit("g", "scratch/auto.md"), _hit("b", "bible/canon.md")]
    ranked = _apply_precedence(hits, _ORDER, graph_authoritative=False)
    assert [h["id"] for h in ranked] == ["b", "g"]
    assert ranked[1]["properties"]["precedence_tier"] == "graph"  # no declared-layer path → derived


def test_graph_authoritative_lifts_a_graph_hit_above_a_file_tier() -> None:
    hits = [_hit("b", "bible/canon.md"), _hit("g", "scratch/auto.md")]
    ranked = _apply_precedence(hits, _ORDER, graph_authoritative=True)
    assert [h["id"] for h in ranked] == ["g", "b"]  # graph wins ONLY when declared authoritative


def test_no_precedence_returns_results_unchanged() -> None:
    hits = [_hit("d", "drafts/ch1.md"), _hit("b", "bible/canon.md")]
    assert _apply_precedence(hits, None, graph_authoritative=False) is hits  # exact pass-through
    assert _apply_precedence(hits, [], graph_authoritative=False) is hits  # empty order = no-op
    # untouched: no precedence_tier stamped when precedence is absent
    assert "precedence_tier" not in hits[0]["properties"]
