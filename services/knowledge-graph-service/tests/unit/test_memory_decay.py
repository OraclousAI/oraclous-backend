"""Unit: agent-memory decay math (#332 / ADR-027 §2) against HAND-COMPUTED values.

Every decay function is pinned to explicit numbers worked out from the formula
I(t) = base · e^(−λ·days) + min(0.3, 0.05·ln(1+count)), capped at 1.0 — so a drive-by edit of a
constant (λ, the boost cap, a base-importance bucket) fails loudly. The clustering used by
consolidation is pinned with controlled vectors.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from oraclous_knowledge_graph_service.domain.memory_consolidation import (
    MemoryVector,
    cluster_by_similarity,
    cosine,
)
from oraclous_knowledge_graph_service.domain.memory_decay import (
    DECAY_LAMBDA,
    access_boost,
    base_importance_for,
    compute_importance,
    content_hash,
    hybrid_rank,
    recency_factor,
    sanitize_lucene_query,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _ago(days: float) -> datetime:
    return _NOW - timedelta(days=days)


# ---------------------------------------------------------------- λ constants


def test_lambda_constants_are_the_legacy_values() -> None:
    assert DECAY_LAMBDA == {"episodic": 0.05, "semantic": 0.005, "procedural": 0.01}


# ---------------------------------------------------------------- access boost


def test_access_boost_hand_computed() -> None:
    assert access_boost(0) == 0.0
    # 0.05 * ln(2) = 0.034657...
    assert access_boost(1) == pytest.approx(0.05 * math.log(2), abs=1e-12)
    # 0.05 * ln(6) = 0.0895879...
    assert access_boost(5) == pytest.approx(0.0895879734, abs=1e-9)


def test_access_boost_caps_at_0_3() -> None:
    # 0.05·ln(1+count) reaches 0.3 at count = e^6 − 1 ≈ 402.4 — beyond that the cap binds.
    assert access_boost(402) < 0.3
    assert access_boost(403) == 0.3
    assert access_boost(10_000) == 0.3


# ---------------------------------------------------------------- importance


def test_importance_episodic_10_days_no_access() -> None:
    # 0.8 · e^(−0.05·10) = 0.8 · e^−0.5 = 0.485224...
    got = compute_importance(0.8, "episodic", _ago(10), 0, now=_NOW)
    assert got == pytest.approx(0.8 * math.exp(-0.5), abs=1e-12)
    assert got == pytest.approx(0.4852245, abs=1e-6)


def test_importance_semantic_100_days_no_access() -> None:
    # 1.0 · e^(−0.005·100) = e^−0.5 = 0.6065306...
    got = compute_importance(1.0, "semantic", _ago(100), 0, now=_NOW)
    assert got == pytest.approx(0.6065306597, abs=1e-9)


def test_importance_procedural_30_days_with_boost() -> None:
    # 0.9 · e^(−0.01·30) + 0.05·ln(8) = 0.9·0.7408182 + 0.1039721 = 0.7707085...
    got = compute_importance(0.9, "procedural", _ago(30), 7, now=_NOW)
    expected = 0.9 * math.exp(-0.3) + 0.05 * math.log(8)
    assert got == pytest.approx(expected, abs=1e-12)
    assert got == pytest.approx(0.7707085, abs=1e-6)


def test_importance_caps_at_1() -> None:
    # fresh user_feedback (base 1.0) + any boost would exceed 1.0 → capped.
    assert compute_importance(1.0, "semantic", _NOW, 100, now=_NOW) == 1.0


def test_importance_unknown_type_uses_default_lambda() -> None:
    # default λ = 0.01: 0.5 · e^(−0.01·50) = 0.5·e^−0.5
    got = compute_importance(0.5, "mystery", _ago(50), 0, now=_NOW)
    assert got == pytest.approx(0.5 * math.exp(-0.5), abs=1e-12)


def test_importance_naive_datetime_treated_as_utc() -> None:
    naive = _ago(10).replace(tzinfo=None)
    aware = compute_importance(0.8, "episodic", _ago(10), 0, now=_NOW)
    assert compute_importance(0.8, "episodic", naive, 0, now=_NOW) == pytest.approx(aware)


def test_old_unaccessed_ranks_below_fresh_then_access_bump_resurfaces_it() -> None:
    """The decay contract in one place: an old episodic memory decays below a fresh one, and an
    access bump (count↑ + last_accessed_at reset) restores it — the no-cron lazy-decay story."""
    old_unaccessed = compute_importance(0.8, "episodic", _ago(60), 0, now=_NOW)
    fresh = compute_importance(0.8, "episodic", _ago(0.1), 0, now=_NOW)
    assert old_unaccessed < 0.05 < fresh  # e^(−3) ≈ 0.0498
    # the bump: re-accessed now with one access → decay window resets + boost applies.
    resurfaced = compute_importance(0.8, "episodic", _NOW, 1, now=_NOW)
    assert resurfaced > fresh > old_unaccessed


# ---------------------------------------------------------------- recency


def test_recency_factor_hand_computed() -> None:
    assert recency_factor(_NOW, now=_NOW) == pytest.approx(1.0)
    # e^(−0.02·25) = e^−0.5 = 0.6065306...
    assert recency_factor(_ago(25), now=_NOW) == pytest.approx(0.6065306597, abs=1e-9)


# ---------------------------------------------------------------- base importance


@pytest.mark.parametrize(
    ("source", "memory_type", "confidence", "expected"),
    [
        ("user_feedback", "semantic", 0.5, 1.0),  # user feedback always wins
        ("user_feedback", "episodic", 0.5, 1.0),  # …even over the episodic bucket
        ("agent", "episodic", 0.99, 0.4),  # episodic bucket beats confidence
        ("inference", "semantic", 0.99, 0.3),  # inference bucket beats confidence
        ("agent", "semantic", 0.9, 0.8),  # high-confidence agent
        ("agent", "semantic", 0.95, 0.8),
        ("agent", "semantic", 0.7, 0.63),  # medium: conf · 0.9
        ("agent", "procedural", 0.8, 0.7200000000000001),
    ],
)
def test_base_importance_buckets(
    source: str, memory_type: str, confidence: float, expected: float
) -> None:
    got = base_importance_for(source=source, memory_type=memory_type, confidence=confidence)
    assert got == pytest.approx(expected, abs=1e-12)


# ---------------------------------------------------------------- content hash


def test_content_hash_normalises_case_and_whitespace() -> None:
    assert content_hash("User prefers   dark\tmode") == content_hash("user prefers dark mode")
    assert content_hash("a") != content_hash("b")


# ---------------------------------------------------------------- hybrid ranking


def test_hybrid_rank_weights_hand_computed() -> None:
    # 0.25·0.8 + 0.25·0.6 + 0.30·0.5 + 0.20·1.0 = 0.2 + 0.15 + 0.15 + 0.2 = 0.7
    got = hybrid_rank(text_score=0.8, vector_score=0.6, importance=0.5, recency=1.0)
    assert got == pytest.approx(0.7, abs=1e-12)


def test_hybrid_rank_no_vector_gives_text_the_full_retrieval_weight() -> None:
    # 0.5·0.8 + 0.3·0.5 + 0.2·1.0 = 0.4 + 0.15 + 0.2 = 0.75 (the legacy fulltext-only blend)
    got = hybrid_rank(text_score=0.8, vector_score=None, importance=0.5, recency=1.0)
    assert got == pytest.approx(0.75, abs=1e-12)


def test_hybrid_rank_clamps_negative_cosine() -> None:
    with_neg = hybrid_rank(text_score=0.0, vector_score=-0.9, importance=0.0, recency=0.0)
    assert with_neg == 0.0


# ---------------------------------------------------------------- clustering


def test_cosine_controlled_vectors() -> None:
    assert cosine((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert cosine((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)
    assert cosine((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(-1.0)
    assert cosine((0.0, 0.0), (1.0, 0.0)) == 0.0  # zero vector → 0, never NaN
    assert cosine((1.0,), (1.0, 0.0)) == 0.0  # dim mismatch → 0


def test_cluster_merges_near_duplicates_and_winner_absorbs_importance() -> None:
    a = MemoryVector("a", (1.0, 0.0), importance=0.6)  # winner (highest importance)
    b = MemoryVector("b", (0.999, 0.0447), importance=0.5)  # cos(a,b) ≈ 0.999 → merges
    c = MemoryVector("c", (0.0, 1.0), importance=0.9)  # orthogonal → own (unmerged) seed
    clusters = cluster_by_similarity([a, b, c], threshold=0.92)
    assert len(clusters) == 1
    (cluster,) = clusters
    assert cluster.winner_id == "a"
    assert cluster.loser_ids == ("b",)
    assert cluster.merged_importance == pytest.approx(1.0)  # 0.6 + 0.5 capped at 1.0
    assert cluster.members == 2


def test_cluster_is_seed_anchored_not_transitive() -> None:
    # b is similar to both a and c, but c is NOT similar to a: with a as the seed, b joins a's
    # cluster and c stays out (chaining would wrongly fold c in).
    a = MemoryVector("a", (1.0, 0.0), importance=0.9)
    b = MemoryVector("b", (math.cos(0.25), math.sin(0.25)), importance=0.5)  # cos≈0.969 to a
    c = MemoryVector("c", (math.cos(0.5), math.sin(0.5)), importance=0.5)  # cos≈0.878 to a
    clusters = cluster_by_similarity([a, b, c], threshold=0.95)
    assert len(clusters) == 1
    assert clusters[0].winner_id == "a"
    assert clusters[0].loser_ids == ("b",)


def test_cluster_below_threshold_merges_nothing() -> None:
    a = MemoryVector("a", (1.0, 0.0), importance=0.5)
    b = MemoryVector("b", (0.0, 1.0), importance=0.5)
    assert cluster_by_similarity([a, b], threshold=0.92) == []


# ------------------------------------------------ clustering partition guard (#332 HIGH-1)


def test_cluster_never_merges_across_partitions() -> None:
    """Two near-identical vectors that differ ONLY in their (type, scope, agent) partition must NOT
    merge — an episodic never absorbs a semantic, a session memory never invalidates an org one,
    agent A never absorbs agent B — regardless of cosine."""
    vec = (1.0, 0.0)
    # same content vector, but each lands in a DIFFERENT partition.
    semantic = MemoryVector("sem", vec, importance=0.9, partition=("semantic", "agent", "a1"))
    episodic = MemoryVector("epi", vec, importance=0.8, partition=("episodic", "agent", "a1"))
    org_scoped = MemoryVector(
        "org", vec, importance=0.7, partition=("semantic", "organization", "a1")
    )
    other_agent = MemoryVector("b", vec, importance=0.6, partition=("semantic", "agent", "a2"))
    clusters = cluster_by_similarity([semantic, episodic, org_scoped, other_agent], threshold=0.92)
    assert clusters == []  # every pair is in a distinct partition → nothing merges


def test_cluster_merges_only_within_a_partition() -> None:
    """Within ONE partition near-duplicates merge; an identical vector in a sibling partition is
    untouched."""
    part = ("semantic", "agent", "a1")
    a = MemoryVector("a", (1.0, 0.0), importance=0.9, partition=part)
    b = MemoryVector("b", (0.999, 0.0447), importance=0.5, partition=part)  # ≈0.999 to a → merges
    other = MemoryVector("c", (1.0, 0.0), importance=0.4, partition=("episodic", "agent", "a1"))
    clusters = cluster_by_similarity([a, b, other], threshold=0.92)
    assert len(clusters) == 1
    assert clusters[0].winner_id == "a" and clusters[0].loser_ids == ("b",)


# ------------------------------------------------ Lucene query sanitisation (#332 HIGH-2)


@pytest.mark.parametrize(
    "raw",
    [
        'unbalanced "quote',
        "title:admin AND role:root",
        "(group OR other",
        "wildcard* and fuzzy~",
        "path/to\\thing",
        "trailing AND",
        "NOT alone",
        "a +b -c !d {e} [f] ^g",
        ":::",
    ],
)
def test_sanitize_lucene_query_neutralises_every_metachar(raw: str) -> None:
    """Every Lucene metacharacter is escaped and bare boolean keywords are de-cased, so the result
    carries no UNescaped special char and no standalone operator (would otherwise be a parse-error
    → 500). The result is non-empty for non-empty input (the terms survive as literals)."""
    out = sanitize_lucene_query(raw)
    assert out  # non-empty input → non-empty safe query
    # no bare (unescaped) double-quote survives — every quote is backslash-escaped.
    assert '"' not in out.replace('\\"', "")
    # the de-cased operators are present as lower-case literal terms, never as operators.
    for op in ("AND", "OR", "NOT"):
        assert f" {op} " not in f" {out} "


def test_sanitize_lucene_query_empty_is_empty() -> None:
    assert sanitize_lucene_query("") == ""
    assert sanitize_lucene_query("   \t  ") == ""


def test_sanitize_lucene_query_plain_text_passes_through() -> None:
    assert sanitize_lucene_query("dark mode preference") == "dark mode preference"


# ------------------------------------------------ default-λ alignment (#332 LOW)


def test_default_lambda_matches_unknown_type_decay() -> None:
    """The Python default λ for an unknown memory type must equal the Cypher bump's ELSE branch
    (0.01) — they are kept in lockstep so the read-time recompute and the lazy bump never drift."""
    from oraclous_knowledge_graph_service.domain.memory_decay import _DEFAULT_LAMBDA

    assert _DEFAULT_LAMBDA == 0.01
    assert DECAY_LAMBDA["procedural"] == _DEFAULT_LAMBDA  # the value the Cypher ELSE mirrors
