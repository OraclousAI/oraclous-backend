"""Agent-memory decay + ranking math (ORAA-4 §21 domain layer — pure functions, no I/O).

Issue #332 / ADR-027 §2. The Ebbinghaus forgetting-curve math is LIFTED VERBATIM from the legacy
``knowledge-graph-builder/app/services/memory_service.py`` (develop@84152635):

    I(t) = base_importance · e^(−λ · days_since_last_access) + access_boost,   capped at 1.0
    λ:            episodic 0.05 · procedural 0.01 · semantic 0.005
    access_boost: min(0.3, 0.05 · ln(1 + access_count))
    base importance by source: user_feedback 1.0 · agent conf ≥ 0.9 → 0.8 · agent medium →
                               conf · 0.9 · episodic 0.4 · inference 0.3

There is NO decay cron: importance is recomputed lazily — at read time for ranking (here, from the
stored ``base_importance``/``access_count``/``last_accessed_at``) and persisted on access by the
repository's Cypher bump (the legacy ``_bump_access`` pattern, with the per-type λ).

Retrieval ranking is the legacy weighted blend extended to true hybrid recall (ADR-027 §3): the
legacy 0.5 "vector" weight (which was actually the fulltext score) splits evenly across the two
retrieval signals when a query vector exists; with no embeddings (fail-soft no-key) the fulltext
score takes the full retrieval weight, so recall degrades to the legacy fulltext-only blend.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime

# Ebbinghaus λ by memory type (legacy verbatim).
DECAY_LAMBDA: dict[str, float] = {
    "episodic": 0.05,
    "semantic": 0.005,
    "procedural": 0.01,
}
_DEFAULT_LAMBDA = 0.01

# Retrieval ranking weights. text + vector together carry the legacy 0.5 retrieval weight;
# importance/recency keep their legacy 0.3 / 0.2.
RANK_WEIGHTS: dict[str, float] = {
    "text": 0.25,
    "vector": 0.25,
    "importance": 0.30,
    "recency": 0.20,
}


def access_boost(access_count: int) -> float:
    """min(0.3, 0.05·ln(1 + access_count)) — the legacy access-boosting term."""
    return min(0.3, 0.05 * math.log1p(max(0, access_count)))


def compute_importance(
    base_importance: float,
    memory_type: str,
    last_accessed_at: datetime,
    access_count: int,
    now: datetime | None = None,
) -> float:
    """Ebbinghaus-inspired forgetting curve with access boosting (legacy verbatim).

    I(t) = base_importance · e^(−λ · days) + access_boost, capped at 1.0.
    """
    if now is None:
        now = datetime.now(UTC)
    if last_accessed_at.tzinfo is None:
        last_accessed_at = last_accessed_at.replace(tzinfo=UTC)
    lam = DECAY_LAMBDA.get(memory_type, _DEFAULT_LAMBDA)
    days = max(0.0, (now - last_accessed_at).total_seconds() / 86400)
    decayed = base_importance * math.exp(-lam * days)
    return min(1.0, decayed + access_boost(access_count))


def recency_factor(last_accessed_at: datetime, now: datetime | None = None) -> float:
    """e^(−0.02 · days_since_last_access) — the legacy recency term in retrieval ranking."""
    if now is None:
        now = datetime.now(UTC)
    if last_accessed_at.tzinfo is None:
        last_accessed_at = last_accessed_at.replace(tzinfo=UTC)
    days = max(0.0, (now - last_accessed_at).total_seconds() / 86400)
    return math.exp(-0.02 * days)


def base_importance_for(*, source: str, memory_type: str, confidence: float) -> float:
    """Base importance by source (legacy `_base_importance_for` verbatim, same precedence order)."""
    if source == "user_feedback":
        return 1.0
    if memory_type == "episodic":
        return 0.4
    if source == "inference":
        return 0.3
    if confidence >= 0.9:
        return 0.8
    return confidence * 0.9


def content_hash(content: str) -> str:
    """Whitespace/case-normalised sha256 of the content (the store-time dedup key, legacy)."""
    normalized = " ".join(content.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def hybrid_rank(
    *,
    text_score: float,
    vector_score: float | None,
    importance: float,
    recency: float,
) -> float:
    """The retrieval ranking blend (ADR-027 §3): weighted fulltext + vector + importance + recency.

    ``text_score`` must already be normalised to [0, 1] (Lucene scores are unbounded — the caller
    divides by the result set's max). ``vector_score`` is a cosine in [-1, 1]; negatives clamp to 0.
    ``vector_score is None`` means no query embedding exists (fail-soft no-key recall) — the text
    score then carries the combined retrieval weight, reproducing the legacy fulltext-only blend.
    """
    if vector_score is None:
        retrieval = (RANK_WEIGHTS["text"] + RANK_WEIGHTS["vector"]) * text_score
    else:
        retrieval = RANK_WEIGHTS["text"] * text_score + RANK_WEIGHTS["vector"] * max(
            0.0, vector_score
        )
    return retrieval + RANK_WEIGHTS["importance"] * importance + RANK_WEIGHTS["recency"] * recency
