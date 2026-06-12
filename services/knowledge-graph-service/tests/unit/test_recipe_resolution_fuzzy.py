"""Entity-resolution enrichment toward the legacy 4-pass dedup (oraclous-backend #309).

#269 shipped a two-layer resolution_pass (deterministic canonical keying + a conservative embedding
semantic merge + SAME_AS_CANDIDATE band). The legacy `entity_dedup_service.py` was a richer 4-pass
(canonical-naming -> merge-canonical -> embedding dedup -> relationship consolidation) with a
blocking/fuzzy string stage. #309 lifts the missing FUZZY/BLOCKING stage onto the resolve-on-write
pass: an embedding-free difflib fold of near-duplicate canonical names (per label, within a blocking
key), running BEFORE the embedding pass and ALSO standing when the embedder is off/fails — keeping
the over-merge guards (per-label only, HIGH thresholds) + the SAME_AS_CANDIDATE band.

These tests exercise `cluster_canonical_keys` directly with a fake embedder (so the fuzzy + semantic
stages and their interaction are deterministic) — the same in-memory pattern the #269 resolution
tests use.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.services.recipes import resolution_pass
from oraclous_knowledge_graph_service.services.recipes.resolution_pass import (
    ResolutionPlan,
    _block_key,
    cluster_canonical_keys,
)

pytestmark = pytest.mark.unit


class _FakeEmbedder:
    dim = 3

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Unknown text -> a far-away unit vector so it never accidentally clusters semantically.
        return [list(self._vectors.get(t, [0.0, 0.0, 1.0])) for t in texts]


class _BoomEmbedder:
    dim = 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("simulated embedder failure")


def _cluster(keys_by_label, plan, embedder, monkeypatch):
    monkeypatch.setattr(resolution_pass, "make_embedder", lambda *a, **k: embedder)
    return cluster_canonical_keys(
        keys_by_label=keys_by_label, plan=plan, settings=Settings(embedder="openai")
    )


def _plan(*, fuzzy_threshold=0.0, merge_threshold=0.92, candidate_threshold=0.85) -> ResolutionPlan:
    return ResolutionPlan(
        use_canonical=True,
        alias_to_canonical={},
        merge_threshold=merge_threshold,
        candidate_threshold=candidate_threshold,
        fuzzy_threshold=fuzzy_threshold,
    )


# === A. the blocking key ==========================================================================
def test_block_key_groups_similar_lengths_and_first_char() -> None:
    # Same first char + same length bucket → same block (compared).
    assert _block_key("eurail") == _block_key("eurali")
    # Different first char → different block (never compared), even if otherwise similar.
    assert _block_key("eurail") != _block_key("aurail")
    # Very different length → different bucket (never compared).
    assert _block_key("ab") != _block_key("abcdefghij")


# === B. fuzzy folds a typo near-duplicate the transform can't (off by default) ====================
def test_fuzzy_off_by_default_does_not_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    # `eurail` and `euraill` (a typo) are distinct canonical keys; with fuzzy OFF and far-apart
    # embeddings they stay two nodes — the prior behaviour is unchanged.
    keys = {"Organization": {"eurail": 2, "euraill": 1}}
    embedder = _FakeEmbedder({"eurail": [1.0, 0.0, 0.0], "euraill": [0.0, 1.0, 0.0]})
    result = _cluster(keys, _plan(fuzzy_threshold=0.0), embedder, monkeypatch)
    assert result.representative[("Organization", "eurail")] == "eurail"
    assert result.representative[("Organization", "euraill")] == "euraill"
    assert result.merged == 0
    assert result.fuzzy_merged == 0


def test_fuzzy_folds_typo_near_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    # With fuzzy ON, the typo `euraill` (difflib ratio to `eurail` ~0.92) folds onto the more
    # frequent `eurail` — WITHOUT any embedding (the embedder vectors are far apart on purpose).
    keys = {"Organization": {"eurail": 2, "euraill": 1}}
    embedder = _FakeEmbedder({"eurail": [1.0, 0.0, 0.0], "euraill": [0.0, 1.0, 0.0]})
    result = _cluster(keys, _plan(fuzzy_threshold=0.9), embedder, monkeypatch)
    assert result.representative[("Organization", "euraill")] == "eurail"  # folded onto the rep
    assert result.representative[("Organization", "eurail")] == "eurail"
    assert result.merged == 1
    assert result.fuzzy_merged == 1
    assert result.candidates == []  # a clean fuzzy fold, not an ambiguous candidate


# === C. over-merge guard: fuzzy never crosses labels ==============================================
def test_fuzzy_never_merges_across_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    # Identical canonical names under two labels must NOT fuzzy-fold — every stage is per-label.
    keys = {"Organization": {"eurail": 1}, "Product": {"eurail": 1}}
    result = _cluster(keys, _plan(fuzzy_threshold=0.5), _FakeEmbedder({}), monkeypatch)
    assert result.representative[("Organization", "eurail")] == "eurail"
    assert result.representative[("Product", "eurail")] == "eurail"
    assert result.merged == 0
    assert result.fuzzy_merged == 0


# === D. over-merge guard: distinct names below the threshold stay distinct ========================
def test_fuzzy_distinct_names_below_threshold_stay_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `eurail` vs `sncf` are not string-similar (and a HIGH threshold) → never folded. Give them
    # orthogonal embeddings so the semantic stage doesn't fold them either (isolating fuzzy).
    keys = {"Organization": {"eurail": 1, "sncf": 1}}
    embedder = _FakeEmbedder({"eurail": [1.0, 0.0, 0.0], "sncf": [0.0, 1.0, 0.0]})
    result = _cluster(keys, _plan(fuzzy_threshold=0.92), embedder, monkeypatch)
    assert result.representative[("Organization", "eurail")] == "eurail"
    assert result.representative[("Organization", "sncf")] == "sncf"
    assert result.merged == 0


# === E. fuzzy survives an embedder failure (the gap the embedding-only legacy pass left) ==========
def test_fuzzy_folds_even_when_embedder_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # The embedder raises → the SEMANTIC stage is skipped, but the embedding-free FUZZY fold stands.
    keys = {"Organization": {"eurail": 2, "euraill": 1}}
    result = _cluster(keys, _plan(fuzzy_threshold=0.9), _BoomEmbedder(), monkeypatch)
    assert result.representative[("Organization", "euraill")] == "eurail"  # fuzzy fold survived
    assert result.fuzzy_merged == 1
    assert result.merged == 1
    assert any("embedder failed" in w for w in result.warnings)


def test_embedder_failure_without_fuzzy_keeps_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # With fuzzy OFF + an embed failure, the deterministic keying still stands: identity mapping,
    # nothing folded, a warning recorded (the prior fail-soft behaviour).
    keys = {"Organization": {"eurail": 1, "eurailways": 1}}
    result = _cluster(keys, _plan(fuzzy_threshold=0.0), _BoomEmbedder(), monkeypatch)
    assert result.representative[("Organization", "eurail")] == "eurail"
    assert result.representative[("Organization", "eurailways")] == "eurailways"
    assert result.merged == 0
    assert result.fuzzy_merged == 0
    assert any("embedder failed" in w for w in result.warnings)


# === F. fuzzy + semantic compose onto one union-find (transitive fold) ============================
def test_fuzzy_and_semantic_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    # `eurail`~`euraill` fold by FUZZY; `eurail`~`eurailgroup` fold by SEMANTIC (cosine 1). All
    # three end up in one cluster with a single representative; two names folded onto it.
    keys = {"Organization": {"eurail": 3, "euraill": 1, "eurailgroup": 1}}
    embedder = _FakeEmbedder(
        {
            "eurail": [1.0, 0.0, 0.0],
            "eurailgroup": [1.0, 0.0, 0.0],  # cosine 1 with eurail → semantic fold
            "euraill": [0.0, 1.0, 0.0],  # far from eurail semantically → only fuzzy can fold it
        }
    )
    result = _cluster(keys, _plan(fuzzy_threshold=0.9, merge_threshold=0.92), embedder, monkeypatch)
    rep = result.representative[("Organization", "eurail")]
    assert result.representative[("Organization", "euraill")] == rep
    assert result.representative[("Organization", "eurailgroup")] == rep
    assert rep == "eurail"  # most frequent
    assert result.merged == 2  # two names folded onto the representative
    assert result.fuzzy_merged == 1  # exactly one of those folds was the fuzzy stage's


# === G. a fuzzy-folded pair is NOT also flagged as a semantic candidate ===========================
def test_fuzzy_folded_pair_not_double_flagged_as_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two names the FUZZY stage already folds whose cosine happens to sit in the ambiguous band must
    # NOT also surface a SAME_AS_CANDIDATE edge — a fold wins over a flag.
    keys = {"Organization": {"eurail": 1, "euraill": 1}}
    # cosine(eurail, euraill) = 0.88 → ambiguous band, but fuzzy ratio ~0.92 folds them first.
    embedder = _FakeEmbedder({"eurail": [1.0, 0.0, 0.0], "euraill": [0.88, 0.4750, 0.0]})
    result = _cluster(
        keys,
        _plan(fuzzy_threshold=0.9, merge_threshold=0.92, candidate_threshold=0.85),
        embedder,
        monkeypatch,
    )
    assert result.fuzzy_merged == 1
    assert result.candidates == []  # folded, so never flagged as a candidate
