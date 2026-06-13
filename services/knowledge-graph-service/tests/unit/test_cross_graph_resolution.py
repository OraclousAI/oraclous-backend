"""Unit: cross-graph SAME_AS pair generation (#330) — the pure signal logic.

No driver, no HTTP. Decisive: a canonical-key match flags a pair across the graph PAIR (same
label only — the in-graph over-merge guard); the embedding stage flags near-names at/above the
threshold and never duplicates a deterministic match; an embed() failure skips ONLY the semantic
stage (canonical-key matches stand — the embedder-off degrade); both graph ids ride on every
candidate; output is deterministic and capped.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.services.cross_graph_resolution import (
    generate_cross_graph_pairs,
)

pytestmark = pytest.mark.unit

_GA = "11111111-1111-1111-1111-111111111111"
_GB = "22222222-2222-2222-2222-222222222222"


def _ent(ident: str, name: str, label: str = "Company") -> dict:
    return {"id": ident, "name": name, "canonical_name": name.title(), "label": label}


class _VecEmbedder:
    """Deterministic test embedder: a fixed vector per name (cosine = dot of unit vectors)."""

    dim = 2

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[t] for t in texts]


class _FailingEmbedder:
    dim = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedder is off")


def _generate(entities_a, entities_b, *, embedder, threshold=0.85, limit=100):
    return generate_cross_graph_pairs(
        graph_id_a=_GA,
        entities_a=entities_a,
        graph_id_b=_GB,
        entities_b=entities_b,
        candidate_threshold=threshold,
        embedder=embedder,
        limit=limit,
    )


def test_canonical_key_match_flags_a_pair_with_both_graph_ids() -> None:
    cands, warnings = _generate(
        [_ent("a1", "acme corp")],
        [_ent("b1", "acme corp")],
        embedder=_VecEmbedder({"acme corp": [1.0, 0.0]}),
    )
    assert warnings == []
    assert len(cands) == 1
    c = cands[0]
    assert (c.node_id_a, c.node_id_b) == ("a1", "b1")
    assert (c.graph_id_a, c.graph_id_b) == (_GA, _GB)  # BOTH graph ids carried (ADR-026)
    assert c.method == "canonical_key" and c.score == 1.0
    assert c.candidate_id  # the stable pair id the verdict endpoints key on


def test_same_key_different_label_is_never_flagged() -> None:
    cands, _ = _generate(
        [_ent("a1", "mercury", label="Planet")],
        [_ent("b1", "mercury", label="Element")],
        embedder=_VecEmbedder({"mercury": [1.0, 0.0]}),
    )
    assert cands == []  # per-label only — the in-graph over-merge guard


def test_embedding_stage_flags_near_names_at_or_above_threshold() -> None:
    vectors = {
        "acme corp": [1.0, 0.0],
        "acme corporation": [0.9, 0.435889894354],  # cosine vs acme corp = 0.9
        "zenith ltd": [0.0, 1.0],  # cosine 0.0 — never flagged
    }
    cands, _ = _generate(
        [_ent("a1", "acme corp")],
        [_ent("b1", "acme corporation"), _ent("b2", "zenith ltd")],
        embedder=_VecEmbedder(vectors),
    )
    assert [(c.node_id_a, c.node_id_b, c.method) for c in cands] == [("a1", "b1", "embedding")]
    assert cands[0].score == pytest.approx(0.9, abs=1e-4)


def test_a_deterministic_match_is_not_duplicated_by_the_embedding_stage() -> None:
    cands, _ = _generate(
        [_ent("a1", "acme corp")],
        [_ent("b1", "acme corp")],
        embedder=_VecEmbedder({"acme corp": [1.0, 0.0]}),
    )
    assert len(cands) == 1  # one pair, one candidate — the seen-set dedupe


def test_embedder_failure_degrades_to_canonical_key_only() -> None:
    cands, warnings = _generate(
        [_ent("a1", "acme corp"), _ent("a2", "other co")],
        [_ent("b1", "acme corp"), _ent("b2", "another co")],
        embedder=_FailingEmbedder(),
    )
    assert [(c.node_id_a, c.node_id_b) for c in cands] == [("a1", "b1")]  # deterministic stands
    assert len(warnings) == 1 and "embedder failed" in warnings[0]


def test_limit_caps_the_strongest_first() -> None:
    vectors = {
        "alpha": [1.0, 0.0],
        "alpha inc": [0.95, 0.31224989992],  # 0.95
        "alpha co": [0.9, 0.435889894354],  # 0.9
    }
    cands, _ = _generate(
        [_ent("a1", "alpha")],
        [_ent("b1", "alpha inc"), _ent("b2", "alpha co"), _ent("b3", "alpha")],
        embedder=_VecEmbedder(vectors),
        limit=2,
    )
    # exact key match (1.0) first, then the strongest embedding pair; the third is capped away.
    assert [(c.node_id_b, c.score) for c in cands] == [("b3", 1.0), ("b1", pytest.approx(0.95))]
