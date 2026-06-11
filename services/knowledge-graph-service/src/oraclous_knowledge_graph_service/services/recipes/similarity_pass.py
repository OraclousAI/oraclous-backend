"""Content-similarity post-projection pass (ORAA-4 §21 services layer — planning, no driver).

Recipe enrichment Slice 3 (#269). The deterministic recipe engine projects each structured record
into its node/edge graph; THIS pass runs AFTER that projection (alongside the Slice-2 extraction
pass) and links records by the SIMILARITY of a designated field's content: it embeds the `from`
field per record, runs a cosine kNN over the embeddings, and MERGEs an `edge_type` (default
`SIMILAR_TO`) edge between records whose text is close — so records that say similar things connect
even when they share no explicit identity/entity.

For each `similarities[]` rule in a validated recipe:
  - collect `(primary_node_deterministic_id, text)` per record for the rule's `from` field (the
    primary node is the per-record node the rule's `node_rule` projected; its deterministic id is
    handed off by the engine on the projection result via `node_index_by_rule`), skipping empty
    text and any record whose primary node was not projected;
  - if fewer than 2 records have text → no pair exists → no edges (return);
  - `make_embedder(settings).embed(texts)` → one vector per record; L2-normalise each so a dot
    product IS the cosine similarity;
  - for each record keep up to `top_k` neighbours with `score >= min_score`, self excluded;
  - MERGE ONE `edge_type` edge per UNORDERED pair — canonical direction `min(id) -> max(id)` so a
    pair is written exactly once regardless of which side surfaced it — carrying a rounded `score`
    float property (the cosine of the two embeddings).

Fail-soft (matches the structured path's other enrichment passes):
  - `embed()` raises (e.g. the OpenAI embedder's network/key error) → the WHOLE pass is skipped with
    a warning; the deterministic projection + the Slice-2 extraction pass are untouched.

Reuses the SAME org-scoped `RecipeGraphWriter` the deterministic projection uses (the edge MERGE
carries the `score` via the writer's per-edge `properties`); it never touches a driver.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.domain.structural import StructuralRepresentation
from oraclous_knowledge_graph_service.repositories.recipe_write_repository import RecipeGraphWriter
from oraclous_knowledge_graph_service.services.embedder import make_embedder

logger = logging.getLogger(__name__)

_SCORE_DP = 4  # round the cosine score to this many decimal places on the edge property.


class _SimilarityStats:
    """Running totals the structured service folds onto the job stats."""

    def __init__(self) -> None:
        self.similarity_edges = 0
        self.warnings: list[str] = []


def run_similarity_pass(
    *,
    recipe: dict[str, Any],
    representation: StructuralRepresentation,
    writer: RecipeGraphWriter,
    node_index_by_rule: dict[str, dict[str, str]],
    settings: Settings,
    engine: Any,
    meta: dict[str, Any],
    source_id: str,
) -> _SimilarityStats:
    """Run every `similarities[]` rule over the projected records; return the edge total.

    `node_index_by_rule` is the engine's hand-off: `{node_rule_id: {unit_id: deterministic_id}}`
    from the deterministic projection — used to resolve each record's per-record (link) node id.
    """
    stats = _SimilarityStats()
    rules = recipe.get("similarities", [])
    if not rules:
        return stats

    record_units = [u for u in representation.units if u.kind.value == "record"]

    for rule in rules:
        node_rule = rule["node_rule"]
        edge_type = rule.get("edge_type", "SIMILAR_TO")
        top_k = int(rule.get("top_k", 5))
        min_score = float(rule.get("min_score", 0.5))
        from_ref = rule["from"]
        primary_by_unit = node_index_by_rule.get(node_rule, {})

        # Collect (primary_node_id, text) per record; skip empty text and any record whose primary
        # node was not projected (e.g. ontology-rejected primary label).
        node_ids: list[str] = []
        texts: list[str] = []
        for unit in record_units:
            primary_id = primary_by_unit.get(unit.unit_id)
            if primary_id is None:
                continue
            value = engine.read_record_field(unit, from_ref)
            text = "" if value is None else str(value)
            if not text.strip():
                continue
            node_ids.append(primary_id)
            texts.append(text)

        # A similarity needs at least one PAIR — fewer than 2 texts can yield no edge.
        if len(texts) < 2:
            continue

        try:
            vectors = make_embedder(settings).embed(texts)
        except Exception:  # noqa: BLE001 — fail-soft: a failed embed never sinks the ingest.
            logger.warning(
                "similarity rule %r: embedder failed; skipping the similarity pass "
                "(the deterministic projection + prior passes are unaffected).",
                rule["id"],
                exc_info=True,
            )
            stats.warnings.append(
                f"similarity rule {rule['id']!r}: embedder failed; similarity pass skipped."
            )
            continue

        normalized = [_l2_normalize(v) for v in vectors]
        edges = _knn_edges(
            node_ids=node_ids,
            normalized=normalized,
            top_k=top_k,
            min_score=min_score,
        )
        if not edges:
            continue
        stats.similarity_edges += writer.merge_edge(
            rel_type=edge_type,
            edges=edges,
            source_id=source_id,
            provenance="INFERRED",
            meta=meta,
        )
    return stats


def _l2_normalize(vector: list[float]) -> list[float]:
    """L2-normalise a vector so a dot product with another normalised vector IS the cosine. A
    zero vector (no signal) stays zero — its cosine with anything is 0, so it links to nothing."""
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return list(vector)
    return [v / norm for v in vector]


def _knn_edges(
    *,
    node_ids: list[str],
    normalized: list[list[float]],
    top_k: int,
    min_score: float,
) -> list[dict[str, Any]]:
    """Cosine kNN → one edge row per UNORDERED similar pair (canonical min->max id direction).

    For each record, score it against every other (dot of the L2-normalised vectors = cosine), keep
    the `top_k` highest with `score >= min_score` (self excluded), then collapse to unordered pairs:
    a pair surfaced from either side is emitted ONCE, keyed by `(min(id), max(id))`, with the score
    as a `properties.score` float. The edge direction is canonical (min id -> max id) so the writer
    MERGEs a single edge per pair regardless of which record found which.
    """
    n = len(node_ids)
    pair_scores: dict[tuple[str, str], float] = {}
    for i in range(n):
        scored: list[tuple[float, int]] = []
        vi = normalized[i]
        for j in range(n):
            if j == i:
                continue  # self excluded
            score = _dot(vi, normalized[j])
            if score >= min_score:
                scored.append((score, j))
        # Highest score first; cap at top_k neighbours for this record.
        scored.sort(key=lambda s: s[0], reverse=True)
        for score, j in scored[:top_k]:
            a, b = node_ids[i], node_ids[j]
            if a == b:
                continue  # two records sharing one primary node id — no self-loop.
            key = (a, b) if a < b else (b, a)
            # A pair can surface from both sides; keep the higher score (they should match, but be
            # robust to float asymmetry).
            existing = pair_scores.get(key)
            if existing is None or score > existing:
                pair_scores[key] = score
    return [
        {"from": a, "to": b, "properties": {"score": round(score, _SCORE_DP)}}
        for (a, b), score in pair_scores.items()
    ]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))
