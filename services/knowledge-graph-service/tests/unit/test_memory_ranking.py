"""Unit: MemoryService._rank hybrid-fallback semantics (#332 MED hybrid-rank fallback).

The no-vector fallback (text carries the full retrieval weight) is a WHOLE-QUERY property — it
fires only when there is NO query embedding at all. A candidate that simply had no vector hit
(fulltext-only, or below the vector cutoff) when a query vector DID exist must score vector=0
(text*.25 + 0), NOT be promoted to text*.5. These drive ``_rank`` directly (pure ranking math, no
substrate) over controlled candidate rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from oraclous_knowledge_graph_service.domain.memory_decay import RANK_WEIGHTS
from oraclous_knowledge_graph_service.services.memory_service import MemoryService

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _svc() -> MemoryService:
    # _rank touches none of the collaborators; pass trivial stand-ins.
    return MemoryService(
        graphs=object(),  # type: ignore[arg-type]
        repo_factory=lambda _g: object(),  # type: ignore[arg-type,return-value]
        embedder=None,
        enqueue_consolidation=lambda _g, _o: "job",
    )


def _cand(mid: str, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "memory_id": mid,
        "memory_type": "semantic",
        "base_importance": 0.0,  # zero so importance contributes 0 → isolate the retrieval term
        "access_count": 0,
        "confidence": 0.8,
        "last_accessed_at": _NOW,  # recency = 1.0 (subtracted out below)
    }
    row.update(kw)
    return row


def test_no_query_embedding_promotes_text_to_full_weight() -> None:
    """hybrid=False (no query vector) → text carries the text+vector weight (the legacy blend)."""
    svc = _svc()
    cands = {"a": _cand("a", text_score=1.0)}  # max_text=1 → text_norm=1
    ranked = svc._rank(cands, hybrid=False, now=_NOW)
    retrieval = ranked[0]["relevance_score"] - RANK_WEIGHTS["recency"] * 1.0  # peel recency
    assert retrieval == pytest.approx(RANK_WEIGHTS["text"] + RANK_WEIGHTS["vector"])  # 0.5


def test_query_embedding_but_no_vector_hit_scores_zero_not_promotion() -> None:
    """hybrid=True (a query vector exists) but THIS candidate had no vector hit → vector=0, so the
    retrieval term is text*.25 only — NOT promoted to text*.5."""
    svc = _svc()
    cands = {"a": _cand("a", text_score=1.0)}  # no 'vector_score' key → no vector hit
    ranked = svc._rank(cands, hybrid=True, now=_NOW)
    retrieval = ranked[0]["relevance_score"] - RANK_WEIGHTS["recency"] * 1.0
    assert retrieval == pytest.approx(RANK_WEIGHTS["text"])  # 0.25, not 0.5


def test_query_embedding_with_vector_hit_blends_both() -> None:
    svc = _svc()
    cands = {"a": _cand("a", text_score=1.0, vector_score=1.0)}
    ranked = svc._rank(cands, hybrid=True, now=_NOW)
    retrieval = ranked[0]["relevance_score"] - RANK_WEIGHTS["recency"] * 1.0
    assert retrieval == pytest.approx(RANK_WEIGHTS["text"] + RANK_WEIGHTS["vector"])  # 0.5
