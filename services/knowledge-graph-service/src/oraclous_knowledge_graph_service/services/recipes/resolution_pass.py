"""Entity resolution / canonicalization (ORAA-4 §21 services layer — planning, no driver).

Recipe enrichment Slice 4 (#269), RESOLVE-ON-WRITE. The hybrid extraction pass (Slice 2) mines
entities from a record's prose field; THIS module canonicalizes those entities DURING ingestion so a
company's surface variants (`Eurail` / `eurail.com` / `Eurail B.V.`) become ONE node with an alias
audit trail — never created as separate nodes that a later cleanup must merge.

Three layers, all BEFORE the entity write (the cleaner of the approaches the brief offers — we
resolve to a representative key first, then write entities already keyed to representatives, so
there is no post-write node surgery). The three layers lift-and-reshape the legacy 4-pass dedup
(`entity_dedup_service.py`: canonical-naming → merge-by-canonical → embedding dedup → relationship
consolidation) onto the resolve-on-write recipe path (#309 enriches #269's two-layer pass):

  1. DETERMINISTIC keying (`ResolutionPlan`; legacy passes 1+2: canonical-name + merge-canonical).
     A surface form's canonical key is the explicit `aliases` map (if the form is a listed variant
     of some canonical) ELSE the `canonical` transform (casefold + bare-domain stem + legal-suffix
     strip — the legacy corporate-suffix list). The extracted entity node MERGEs by
     `_deterministic_id(graph_id, label, canonical_key)`, so all variants collapse to ONE node; the
     node accumulates the ORIGINAL surface forms in an `aliases` set and carries `name` = the
     canonical key + `canonical_name` = a chosen display form (the longest surface form seen).

  2. FUZZY string folding (`_fuzzy_fold`, BLOCKED; #309 — the legacy "blocking/fuzzy" stage):
     opt-in via `fuzzy_threshold > 0`. Within EACH label, fold canonical-name pairs whose difflib
     `SequenceMatcher` ratio >= `fuzzy_threshold` (HIGH/conservative). A BLOCKING key (shared first
     char + length bucket) restricts the per-label O(n^2) to small blocks. Embedding-free, so it
     ALWAYS runs — catching typo/near-duplicate keys even when the embedder is off or fails.

  3. SEMANTIC clustering (`_semantic_fold`; legacy pass 3 = embedding dedup; conservative): within
     EACH label, embed the DISTINCT canonical names, L2-normalise, pairwise cosine; fold pairs whose
     cosine >= `merge_threshold` (0.92, HIGH/conservative). For pairs in the AMBIGUOUS band
     `[candidate_threshold, merge_threshold)` (0.85–0.92): DO NOT merge — a `SAME_AS_CANDIDATE
     {score}` edge is MERGEd between the two canonical nodes for review.

Fuzzy + semantic feed ONE per-label union-find; each connected cluster then gets ONE representative
canonical key (deterministic: the most-frequent, then longest, then lexically-smallest), and the
other members are folded onto it BEFORE write (their canonical key maps to the representative).

Over-merge guards (critical): never merge across different labels (every stage is per-label); HIGH
fuzzy + merge thresholds; the semantic ambiguous band is flagged-not-merged; everything stays
auditable via the `aliases` set + the candidate edges. Fail-soft: an embed() error skips ONLY the
semantic stage for that label (the deterministic keying + any fuzzy folds still stand) + a warning.

The legacy pass 4 (parallel-relationship consolidation, collapsing N duplicate same-type edges into
one with a `count`) is NOT needed here: the recipe write path MERGEs edges idempotently by type +
endpoints + scope, so duplicate parallel relationships are never created in the first place.

Relation-type canonicalization is already enforced by the closed `relationship_types` ontology
(Slice B): a closed ontology yields canonical relation roots by construction, so there is no
separate relation-merge here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.domain.recipes.transforms import (
    canonical as canonical_transform,
)
from oraclous_knowledge_graph_service.services.embedder import make_embedder

logger = logging.getLogger(__name__)

_SCORE_DP = 4  # round the cosine on a SAME_AS_CANDIDATE edge to this many decimal places.
# The relationship type MERGEd between two canonical nodes in the ambiguous band (for human review).
SAME_AS_CANDIDATE = "SAME_AS_CANDIDATE"
# The length-bucket width for the fuzzy BLOCKING key: two canonical names are only string-compared
# when they share a first character AND fall in the same length bucket — so an O(n^2) per-label
# comparison shrinks to small blocks, and wildly different-length names never even get compared.
_FUZZY_LEN_BUCKET = 4


@dataclass
class ResolutionPlan:
    """The deterministic keying half of a resolution rule (built once per extraction rule).

    `canonical` toggles the `canonical` transform; `alias_to_canonical` is the explicit map's
    variant→canonical lookup (variants casefolded for a case-insensitive match), applied first.
    Thresholds drive the clustering. A plan is only built when the rule carries `resolution`.
    """

    use_canonical: bool
    alias_to_canonical: dict[str, str]
    merge_threshold: float
    candidate_threshold: float
    # #309: a cheap, deterministic, embedding-free fuzzy fold (legacy 4-pass dedup's string stage).
    # `fuzzy_threshold` in (0,1] is the difflib SequenceMatcher ratio at/above which two canonical
    # names within a label are folded BEFORE the (optional, embedding-dependent) semantic pass; 0
    # disables it (default off — the existing semantic-only behaviour is preserved).
    fuzzy_threshold: float

    @classmethod
    def from_rule(cls, resolution: dict[str, Any]) -> ResolutionPlan:
        alias_to_canonical: dict[str, str] = {}
        for canonical_key, variants in (resolution.get("aliases") or {}).items():
            for variant in variants:
                alias_to_canonical[str(variant).strip().casefold()] = canonical_key
        return cls(
            use_canonical=bool(resolution.get("canonical", False)),
            alias_to_canonical=alias_to_canonical,
            merge_threshold=float(resolution.get("merge_threshold", 0.92)),
            candidate_threshold=float(resolution.get("candidate_threshold", 0.85)),
            fuzzy_threshold=float(resolution.get("fuzzy_threshold", 0.0)),
        )

    def canonical_key(self, surface_form: str) -> str:
        """Derive a surface form's canonical key: the explicit alias map FIRST (a listed variant →
        its canonical), else the `canonical` transform (when enabled), else a casefold/collapse
        fallback. The key is what the entity node MERGEs by, so variants collapse to one node."""
        text = "" if surface_form is None else str(surface_form)
        keyed = self.alias_to_canonical.get(text.strip().casefold())
        if keyed is not None:
            return keyed
        if self.use_canonical:
            return canonical_transform(text)
        # Resolution present but canonical off + no alias hit: fall back to the same
        # casefold/collapse the non-resolution path uses, so keying is still deterministic.
        return " ".join(text.strip().casefold().split())


@dataclass
class ClusterResult:
    """The semantic clustering outcome the extraction pass applies before/at write time."""

    # canonical_key -> representative canonical_key (a key maps to itself when it is its own rep).
    representative: dict[tuple[str, str], str] = field(default_factory=dict)
    # Ambiguous-band pairs to MERGE a SAME_AS_CANDIDATE edge between (label, key_a, key_b, score).
    candidates: list[tuple[str, str, str, float]] = field(default_factory=list)
    merged: int = (
        0  # non-representative canonical names folded onto a representative (fuzzy + sem).
    )
    fuzzy_merged: int = 0  # the subset of `merged` folded by the embedding-free fuzzy stage (#309).
    warnings: list[str] = field(default_factory=list)


class _UnionFind:
    """Minimal union-find over hashable items (per-label clustering of canonical names)."""

    def __init__(self) -> None:
        self._parent: dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:  # path compression
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: Any, b: Any) -> None:
        self._parent[self.find(a)] = self.find(b)


def cluster_canonical_keys(
    *,
    keys_by_label: dict[str, dict[str, int]],
    plan: ResolutionPlan,
    settings: Settings,
) -> ClusterResult:
    """Cluster the DISTINCT canonical names within each label and pick a representative per cluster.

    `keys_by_label` is `{label: {canonical_key: occurrence_count}}` — the distinct canonical keys
    seen per resolved label and how often each appeared (used for the representative tie-break).
    Returns the `(label, key) -> representative_key` map (folding near-duplicates), the
    ambiguous-band candidate pairs, and the folded counts.

    Two folding stages run PER LABEL onto one union-find, toward the legacy 4-pass dedup (#309):

      1. FUZZY (embedding-free, blocked; #309): when `plan.fuzzy_threshold > 0`, fold canonical-name
         pairs whose difflib `SequenceMatcher` ratio >= `fuzzy_threshold` (HIGH/conservative). A
         BLOCKING key (shared first char + same length bucket) means only plausibly-similar names
         are string-compared, so the per-label O(n^2) shrinks to small blocks AND wildly different
         names never compare. This stage needs no embeddings, so it ALWAYS runs — and still applies
         when the embedder is off or fails (the gap the legacy embedding-only pass left).
      2. SEMANTIC (embedding; conservative): fold pairs whose cosine >= `merge_threshold`; flag
         pairs in `[candidate_threshold, merge_threshold)` as SAME_AS_CANDIDATE (not merged).
         Fail-soft: an embed() error skips ONLY that label's semantic stage (fuzzy folds stand).

    Over-merge guards: every comparison runs PER LABEL (never across labels); both stages use HIGH
    thresholds; the semantic ambiguous band is flagged-not-merged; a representative is picked once,
    after both stages, per connected cluster.
    """
    result = ClusterResult()
    # Default identity mapping (also the fail-soft outcome): every key is its own representative.
    for label, counts in keys_by_label.items():
        for key in counts:
            result.representative[(label, key)] = key

    embedder = make_embedder(settings)
    for label, counts in keys_by_label.items():
        distinct = sorted(counts)  # deterministic order in + out
        if len(distinct) < 2:
            continue  # no pair within this label → nothing to cluster
        uf = _UnionFind()

        # Stage 1 — fuzzy (embedding-free, blocked). Folds typo/near-duplicate canonical names the
        # transform cannot collapse, before (and independent of) the embedding stage.
        fuzzy_folds = _fuzzy_fold(distinct, uf, plan.fuzzy_threshold)
        result.fuzzy_merged += fuzzy_folds

        # Stage 2 — semantic (embedding; fail-soft per label).
        _semantic_fold(label, distinct, counts, uf, plan, embedder, result)

        # Pick ONE representative per connected cluster (deterministic: most frequent, then longest,
        # then lexically smallest) and fold the others onto it.
        clusters: dict[Any, list[str]] = {}
        for key in distinct:
            clusters.setdefault(uf.find(key), []).append(key)
        for members in clusters.values():
            rep = max(members, key=lambda k: (counts[k], len(k), [-ord(c) for c in k]))
            for key in members:
                result.representative[(label, key)] = rep
                if key != rep:
                    result.merged += 1
    return result


def _block_key(name: str) -> tuple[str, int]:
    """The fuzzy BLOCKING key for a canonical name: (first character, length bucket). Two names are
    only string-compared when their block keys match — so the per-label comparison is restricted to
    small blocks of plausibly-similar names and never compares names of very different length."""
    first = name[0] if name else ""
    return (first, len(name) // _FUZZY_LEN_BUCKET)


def _fuzzy_fold(distinct: list[str], uf: _UnionFind, fuzzy_threshold: float) -> int:
    """Fold canonical names within one label by string similarity (difflib ratio >= threshold),
    comparing only within a shared blocking key. Returns the number of UNION operations performed
    (an upper-bound proxy for the eventual fold count). No-op when `fuzzy_threshold <= 0`."""
    if fuzzy_threshold <= 0:
        return 0
    blocks: dict[tuple[str, int], list[str]] = {}
    for name in distinct:
        blocks.setdefault(_block_key(name), []).append(name)
    folds = 0
    for members in blocks.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if uf.find(a) == uf.find(b):
                    continue  # already in the same cluster — don't double-count.
                ratio = SequenceMatcher(None, a, b).ratio()
                if ratio >= fuzzy_threshold:
                    uf.union(a, b)
                    folds += 1
    return folds


def _semantic_fold(
    label: str,
    distinct: list[str],
    counts: dict[str, int],
    uf: _UnionFind,
    plan: ResolutionPlan,
    embedder: Any,
    result: ClusterResult,
) -> None:
    """Fold canonical names within one label by embedding cosine (>= merge_threshold), flag the
    ambiguous band as SAME_AS_CANDIDATE. Mutates `uf` and `result` in place. Fail-soft: an embed()
    error skips ONLY this label's semantic stage (a warning is recorded; any fuzzy folds stand)."""
    try:
        vectors = embedder.embed(distinct)
    except Exception:  # noqa: BLE001 — fail-soft: a failed embed skips this label's semantic stage.
        logger.warning(
            "resolution: embedder failed for label %r; semantic merge skipped for it "
            "(deterministic keying + fuzzy folds still apply).",
            label,
            exc_info=True,
        )
        result.warnings.append(
            f"resolution: embedder failed for label {label!r}; semantic merge skipped."
        )
        return
    normalized = [_l2_normalize(v) for v in vectors]
    for i in range(len(distinct)):
        for j in range(i + 1, len(distinct)):
            score = _dot(normalized[i], normalized[j])
            if score >= plan.merge_threshold:
                uf.union(distinct[i], distinct[j])  # fold (conservative HIGH threshold)
            elif score >= plan.candidate_threshold and uf.find(distinct[i]) != uf.find(distinct[j]):
                # Ambiguous band AND not already folded (e.g. by fuzzy): flag-not-merge a
                # SAME_AS_CANDIDATE edge (canonical order) for human review.
                a, b = sorted((distinct[i], distinct[j]))
                result.candidates.append((label, a, b, round(score, _SCORE_DP)))


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return list(vector)
    return [v / norm for v in vector]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))
