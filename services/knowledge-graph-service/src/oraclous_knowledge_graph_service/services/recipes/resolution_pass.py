"""Entity resolution / canonicalization (ORAA-4 §21 services layer — planning, no driver).

Recipe enrichment Slice 4 (#269), RESOLVE-ON-WRITE. The hybrid extraction pass (Slice 2) mines
entities from a record's prose field; THIS module canonicalizes those entities DURING ingestion so a
company's surface variants (`Eurail` / `eurail.com` / `Eurail B.V.`) become ONE node with an alias
audit trail — never created as separate nodes that a later cleanup must merge.

Two layers, both BEFORE the entity write (the cleaner of the two approaches the brief offers — we
resolve to a representative key first, then write entities already keyed to representatives, so
there is no post-write node surgery):

  1. DETERMINISTIC keying (`ResolutionPlan`): a surface form's canonical key is the explicit
     `aliases` map (if the form is a listed variant of some canonical) ELSE the `canonical`
     transform (casefold + bare-domain stem + legal-suffix strip). The extracted entity node MERGEs
     by `_deterministic_id(graph_id, label, canonical_key)`, so all variants collapse to ONE node;
     the node accumulates the ORIGINAL surface forms in an `aliases` set and carries `name` = the
     canonical key + `canonical_name` = a chosen display form (the longest surface form seen).

  2. SEMANTIC clustering (`cluster_canonical_keys`, the "deduction" beyond the rules; conservative):
     within EACH resolved label, embed the DISTINCT canonical names, L2-normalise, pairwise cosine;
     union-find cluster the pairs whose cosine >= `merge_threshold` (0.92, HIGH/conservative) → each
     cluster gets ONE representative canonical key (deterministic: the most-frequent, then longest,
     then lexically-smallest) and the other members are folded onto it BEFORE write (their canonical
     key maps to the representative). For pairs in the AMBIGUOUS band `[candidate_threshold,
     merge_threshold)` (0.85–0.92): DO NOT merge — a `SAME_AS_CANDIDATE {score}` edge is MERGEd
     between the two canonical nodes for review.

Over-merge guards (critical): never merge across different labels (clustering is per-label); a HIGH
merge threshold; the ambiguous band is flagged-not-merged; everything stays auditable via the
`aliases` set + the candidate edges. Fail-soft: an embed() error → the semantic pass is skipped (the
deterministic canonical keying still stands) + a warning is recorded.

Relation-type canonicalization is already enforced by the closed `relationship_types` ontology
(Slice B): a closed ontology yields canonical relation roots by construction, so there is no
separate relation-merge here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
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
    merged: int = 0  # non-representative canonical names folded onto a representative.
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
    Returns the `(label, key) -> representative_key` map (folding near-duplicates >= merge),
    the ambiguous-band candidate pairs, and the folded count. Fail-soft: an embed() error skips the
    semantic pass entirely (each key maps to itself — the deterministic canonical keying holds).

    Over-merge guards: the embed + cosine + union-find run PER LABEL (never across labels); only
    pairs >= the HIGH merge_threshold fold; pairs in [candidate_threshold, merge_threshold) are
    flagged as candidates, not merged.
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
        try:
            vectors = embedder.embed(distinct)
        except Exception:  # noqa: BLE001 — fail-soft: a failed embed skips the semantic pass.
            logger.warning(
                "resolution: embedder failed for label %r; semantic merge skipped "
                "(deterministic canonical keying still applies).",
                label,
                exc_info=True,
            )
            result.warnings.append(
                f"resolution: embedder failed for label {label!r}; semantic merge skipped."
            )
            return ClusterResult(
                representative={(label2, k): k for label2, c in keys_by_label.items() for k in c},
                candidates=[],
                merged=0,
                warnings=result.warnings,
            )
        normalized = [_l2_normalize(v) for v in vectors]
        uf = _UnionFind()
        for i in range(len(distinct)):
            for j in range(i + 1, len(distinct)):
                score = _dot(normalized[i], normalized[j])
                if score >= plan.merge_threshold:
                    uf.union(distinct[i], distinct[j])  # fold (conservative HIGH threshold)
                elif score >= plan.candidate_threshold:
                    # Ambiguous band: flag-not-merge a SAME_AS_CANDIDATE edge (canonical order).
                    a, b = sorted((distinct[i], distinct[j]))
                    result.candidates.append((label, a, b, round(score, _SCORE_DP)))
        # Pick ONE representative per cluster (deterministic: most frequent, then longest, then
        # lexically smallest) and fold the others onto it.
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


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return list(vector)
    return [v / norm for v in vector]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))
